"""All git access to the vault working tree, serialized behind one lock.

Every mutation of the vault goes through here. The lock matters more than it
looks: a webhook-triggered pull and an MCP-triggered commit racing each other
will collide on `.git/index.lock` and leave the tree in a rebase-in-progress
state that nothing else in the stack knows how to recover from.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


class VaultGit:
    def __init__(
        self,
        vault_path: Path,
        *,
        remote: str = "origin",
        branch: str = "main",
        author_name: str = "obsidian-vault-mcp",
        author_email: str = "vault-mcp@localhost",
        auto_push: bool = True,
        enabled: bool = True,
    ) -> None:
        self.vault_path = vault_path
        self.remote = remote
        self.branch = branch
        self.author_name = author_name
        self.author_email = author_email
        self.auto_push = auto_push
        self.enabled = enabled
        self.lock = asyncio.Lock()
        # Shas this process pushed. Webhook deliveries for our own pushes are
        # dropped against this set, so NAS -> Gitea -> NAS does not loop.
        self._own_pushes: set[str] = set()

    # ---- plumbing -------------------------------------------------------

    async def _git(self, *args: str, check: bool = True) -> tuple[int, str]:
        env = dict(os.environ)
        env.update(
            {
                "GIT_AUTHOR_NAME": self.author_name,
                "GIT_AUTHOR_EMAIL": self.author_email,
                "GIT_COMMITTER_NAME": self.author_name,
                "GIT_COMMITTER_EMAIL": self.author_email,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.vault_path),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        raw, _ = await proc.communicate()
        out = raw.decode("utf-8", "replace").strip()
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {out}")
        return proc.returncode or 0, out

    async def head(self) -> str:
        _, out = await self._git("rev-parse", "HEAD")
        return out

    def is_own_push(self, sha: str | None) -> bool:
        return bool(sha) and sha in self._own_pushes

    # ---- operations (call with self.lock held) --------------------------

    async def _pull(self) -> tuple[str, str]:
        before = await self.head()
        await self._git("fetch", "--prune", self.remote)
        code, out = await self._git(
            "rebase", "--autostash", f"{self.remote}/{self.branch}", check=False
        )
        if code != 0:
            log.error("rebase failed, aborting: %s", out)
            await self._git("rebase", "--abort", check=False)
            raise GitError(f"rebase onto {self.remote}/{self.branch} failed: {out}")
        after = await self.head()
        return before, after

    async def _commit(self, paths: list[str], message: str) -> str | None:
        if paths:
            await self._git("add", "--", *paths)
        else:
            await self._git("add", "-A")
        code, _ = await self._git("diff", "--cached", "--quiet", check=False)
        if code == 0:
            return None  # nothing staged
        await self._git("commit", "-m", message)
        return await self.head()

    async def _push(self) -> None:
        code, out = await self._git("push", self.remote, f"HEAD:{self.branch}", check=False)
        if code == 0:
            self._own_pushes.add(await self.head())
            return
        log.warning("push rejected, rebasing and retrying: %s", out)
        await self._pull()
        await self._git("push", self.remote, f"HEAD:{self.branch}")
        self._own_pushes.add(await self.head())

    # ---- public API -----------------------------------------------------

    async def sync(self) -> dict[str, str]:
        """Pull remote changes into the working tree."""
        if not self.enabled:
            return {"status": "disabled"}
        async with self.lock:
            before, after = await self._pull()
        return {
            "status": "unchanged" if before == after else "updated",
            "before": before,
            "after": after,
        }

    async def record(self, paths: list[str], message: str) -> dict[str, str]:
        """Commit the given vault-relative paths and push."""
        if not self.enabled:
            return {"status": "disabled"}
        async with self.lock:
            sha = await self._commit(paths, message)
            if sha is None:
                return {"status": "nothing-to-commit"}
            if self.auto_push:
                await self._push()
                return {"status": "pushed", "commit": sha}
            return {"status": "committed", "commit": sha}
