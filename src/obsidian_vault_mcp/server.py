"""MCP write surface for an Obsidian vault, plus a Gitea webhook receiver.

Pairs with obsidian-hybrid-search, which owns the read path (search/read).
This process owns the write path and keeps the on-disk vault in sync with
Gitea in both directions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
from collections.abc import AsyncIterator
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .backends import BackendError, NullBackend, OHSBackend, SearchBackend
from .config import Config
from .notes import (
    NoteError,
    atomic_write,
    build_note,
    render,
    resolve_writable,
    slugify,
    split_frontmatter,
    unique_path,
    utcnow,
)
from .ulid import ulid
from .vaultgit import GitError, VaultGit
from .webhook import WebhookError, parse_push, verify

log = logging.getLogger("obsidian-vault-mcp")

# Background webhook pulls. Held so they are not garbage-collected mid-flight.
_background: set[asyncio.Task[None]] = set()


@contextlib.contextmanager
def _actionable(what: str) -> Any:
    """Turn expected failures into ToolError so the message reaches the caller.

    Anything else raised from a tool becomes the SDK's generic "Error executing
    tool <name>", which tells the agent — and you — nothing. These are the
    failures we can name, so we name them.
    """
    try:
        yield
    except (NoteError, BackendError) as exc:
        raise ToolError(str(exc)) from exc
    except PermissionError as exc:
        raise ToolError(
            f"{what}: permission denied ({exc.filename}). The vault is not writable "
            "by the uid this container runs as; check the `user:` setting against "
            "the dataset owner."
        ) from exc
    except OSError as exc:
        raise ToolError(f"{what}: {exc.strerror or exc} ({exc.filename})") from exc


def build_server(cfg: Config) -> MCPServer:
    git = VaultGit(
        cfg.vault_path,
        remote=cfg.git_remote,
        branch=cfg.git_branch,
        author_name=cfg.git_author_name,
        author_email=cfg.git_author_email,
        auto_push=cfg.auto_push,
        enabled=cfg.git_enabled,
        remote_url=cfg.git_remote_url,
        auto_init=cfg.git_auto_init,
    )

    backend: SearchBackend
    if cfg.ohs_enabled:
        backend = OHSBackend(
            vault_path=str(cfg.vault_path),
            url=cfg.ohs_url,
            spawn=cfg.ohs_spawn,
            command=cfg.ohs_command,
            port=cfg.ohs_port,
            startup_timeout=cfg.ohs_startup_timeout,
        )
    else:
        backend = NullBackend()

    @contextlib.asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        await git.provision()
        await backend.start()
        try:
            yield
        finally:
            await backend.stop()

    mcp = MCPServer(
        lifespan=lifespan,
        name="obsidian-vault",
        instructions=(
            "Search, read, and write an Obsidian vault. Use search_notes to find "
            "notes by meaning or exact text, read_notes to fetch their full "
            "contents, and save_note / update_note to record new knowledge in the "
            "AI inbox. Always cite the vault-relative path of any note you use."
        ),
    )

    async def _guard(awaitable: Any) -> Any:
        try:
            return await awaitable
        except BackendError as exc:
            raise ToolError(str(exc)) from exc

    async def _record(paths: list[str], message: str) -> dict[str, Any]:
        """Commit and push, but never fail the tool over it.

        The note is already on disk at this point. Raising here would make an
        agent retry and write a duplicate note on every attempt, so a git
        failure is reported alongside the successful save instead.
        """
        try:
            return await git.record(paths, message)
        except GitError as exc:
            log.error("git record failed for %s: %s", paths, exc)
            return {"status": "error", "error": str(exc)}

    async def reindex(rel_path: str = "") -> None:
        if cfg.ohs_enabled and cfg.reindex_cmd == "":
            # Ask the backend directly; cheaper and more reliable than a shell out.
            with contextlib.suppress(BackendError):
                await backend.reindex(rel_path or None)
            return
        if not cfg.reindex_cmd:
            return  # the watcher will pick it up
        cmd = cfg.reindex_cmd.replace("{path}", shlex.quote(rel_path))
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cfg.vault_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode:
            log.warning("reindex failed (%s): %s", proc.returncode, err.decode("utf-8", "replace"))

    # ---- tools ----------------------------------------------------------

    @mcp.tool()
    async def save_note(
        title: str,
        body: str,
        tags: list[str] | None = None,
        source: str | None = None,
        frontmatter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a new markdown note into the vault's AI inbox.

        The server assigns the id, filename, and creation timestamp; supply the
        title, the markdown body, and optionally tags, a source description, and
        extra frontmatter fields. Returns the vault-relative path so the note can
        be cited or read back.
        """
        note_id = ulid()
        content = build_note(
            note_id=note_id,
            title=title,
            body=body,
            created=utcnow(),
            tags=tags,
            source=source,
            status="active",
            extra=frontmatter,
            reserved=cfg.reserved_frontmatter,
        )
        with _actionable("save_note"):
            cfg.inbox_root.mkdir(parents=True, exist_ok=True)
            dest = unique_path(cfg.inbox_root, slugify(title), note_id)
            rel = dest.relative_to(cfg.vault_path).as_posix()
            atomic_write(dest, content, cfg.tmp_root)

        await reindex(rel)
        return {"path": rel, "id": note_id, "git": await _record([rel], f"note: {title[:72]}")}

    @mcp.tool()
    async def update_note(
        path: str,
        body: str | None = None,
        frontmatter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Revise an existing note.

        `path` is vault-relative, e.g. "Networking/VLANs/Home VLANs.md". Supply
        `body` to replace the note body, and/or `frontmatter` to merge keys into
        the existing frontmatter (a null value removes a key); `id` and
        `created` are immutable. Writes are confined to the server's configured
        scope, and never touch .git, .obsidian, .ai-tmp, or .trash. Call
        read_notes first if you intend to preserve any of the existing body.
        """
        with _actionable("update_note"):
            target = resolve_writable(cfg.vault_path, cfg.writable_paths, path)
            if not target.is_file():
                raise NoteError(f"no such note: {path}")

            meta, current_body = split_frontmatter(target.read_text(encoding="utf-8"))
            for key, value in (frontmatter or {}).items():
                if key in {"id", "created"}:
                    raise NoteError(f"frontmatter key {key!r} is immutable")
                if value is None:
                    meta.pop(key, None)
                else:
                    meta[key] = value
            meta["updated"] = utcnow()

        rel = target.relative_to(cfg.vault_path).as_posix()
        with _actionable("update_note"):
            atomic_write(
                target, render(meta, body if body is not None else current_body), cfg.tmp_root
            )
        await reindex(rel)
        return {"path": rel, "git": await _record([rel], f"update: {rel}")}

    @mcp.tool()
    async def sync_vault() -> dict[str, Any]:
        """Pull the latest vault changes from the git remote immediately.

        Normally unnecessary: pushes arrive via the Gitea webhook. Use this if
        you suspect the working tree is stale.
        """
        return await git.sync()

    @mcp.tool()
    async def vault_status() -> dict[str, Any]:
        """Report the vault path, tracked branch, current commit, and inbox size."""
        inbox = sorted(cfg.inbox_root.glob("*.md")) if cfg.inbox_root.is_dir() else []
        status: dict[str, Any] = {
            "vault": str(cfg.vault_path),
            "inbox": cfg.inbox_dir,
            "inbox_notes": len(inbox),
            "writable_paths": list(cfg.writable_paths),
            "git_enabled": cfg.git_enabled,
        }
        if git.disabled_reason:
            status["git_enabled"] = False
            status["git_problem"] = git.disabled_reason
        if git.enabled:
            status["branch"] = cfg.git_branch
            try:
                status["head"] = await git.head()
            except GitError as exc:
                status["head_error"] = str(exc)
        return status

    @mcp.tool()
    async def search_notes(
        query: str | None = None,
        queries: list[str] | None = None,
        mode: str = "hybrid",
        path: str | None = None,
        related: bool = False,
        scope: list[str] | None = None,
        tag: list[str] | None = None,
        limit: int = 10,
        threshold: float | None = None,
        rerank: bool = False,
        snippet_length: int | None = None,
    ) -> dict[str, Any]:
        """Search the vault. Returns ranked notes with paths, snippets, and scores.

        Use `query` for text search: `mode` is hybrid (default, BM25 + semantic),
        semantic, fulltext, or title. Pass `queries` instead to search several
        phrasings at once and merge the rankings — useful when the note may not
        use your vocabulary. Use `path` alone to find notes similar to a given
        note, or `path` with `related=true` to walk its links and backlinks.
        Narrow with `scope` (folders) and `tag`; prefix either with "-" to
        exclude. Set `rerank=true` for higher precision at 1-3s extra latency.
        Always cite the returned paths.
        """
        return await _guard(backend.search(
            query=query,
            queries=queries,
            mode=mode,
            path=path,
            related=related or None,
            scope=scope,
            tag=tag,
            limit=limit,
            threshold=threshold,
            rerank=rerank or None,
            snippet_length=snippet_length,
        ))

    @mcp.tool()
    async def read_notes(paths: list[str], snippet_length: int | None = None) -> dict[str, Any]:
        """Read one or more notes in full by vault-relative path.

        Returns content, title, aliases, tags, links, and backlinks. On a path
        miss you get fuzzy suggestions rather than an error. Use `snippet_length`
        to cap the content returned per note.
        """
        return await _guard(backend.read(paths, snippet_length))

    # ---- webhook --------------------------------------------------------

    async def _pull_and_reindex() -> None:
        try:
            result = await git.sync()
            if result.get("status") == "updated":
                await reindex("")
            log.info("webhook sync: %s", result)
        except GitError as exc:
            log.error("webhook sync failed: %s", exc)

    @mcp.custom_route(cfg.webhook_path, methods=["POST"])
    async def gitea_webhook(request: Request) -> JSONResponse:
        if not cfg.git_enabled:
            return JSONResponse({"ok": False, "reason": "git disabled"}, status_code=503)
        raw = await request.body()
        try:
            verify(cfg.webhook_secret, raw, dict(request.headers))
            event = parse_push(raw, cfg.git_branch)
        except WebhookError as exc:
            log.warning("rejected webhook: %s", exc)
            return JSONResponse({"ok": False, "reason": str(exc)}, status_code=exc.status)

        detail = {"ref": event.get("ref"), "after": event.get("after")}

        if not event["relevant"]:
            return JSONResponse({"ok": True, "skipped": event["reason"], **detail})
        if git.is_own_push(event.get("after")):
            return JSONResponse({"ok": True, "skipped": "our own push", **detail})

        # An all-zero `after` means "no commit". That is a branch deletion on a
        # live repo, but it is also what Gitea's Test Delivery sends and what a
        # push to a repo with no commits looks like. Ask the remote rather than
        # trusting the payload, so a real push is never dropped as a deletion.
        if event.get("zero_after"):
            if await git.remote_branch_exists():
                log.info("zero after-sha but %s still exists on the remote; syncing",
                         cfg.git_branch)
            else:
                return JSONResponse(
                    {"ok": True, "skipped": f"branch {cfg.git_branch} not on remote", **detail}
                )

        task = asyncio.create_task(_pull_and_reindex())
        _background.add(task)
        task.add_done_callback(_background.discard)
        return JSONResponse({"ok": True, "queued": event.get("after"), **detail}, status_code=202)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return mcp
