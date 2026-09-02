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

from .vault_gitignore import VAULT_GITIGNORE

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
        remote_url: str = "",
        auto_init: bool = True,
    ) -> None:
        self.vault_path = vault_path
        self.remote = remote
        self.branch = branch
        self.author_name = author_name
        self.author_email = author_email
        self.auto_push = auto_push
        self.enabled = enabled
        self.remote_url = remote_url
        self.auto_init = auto_init
        self.lock = asyncio.Lock()
        self.disabled_reason: str | None = None
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

    # ---- provisioning ---------------------------------------------------

    async def _probe_remote(self, url: str) -> str:
        """Classify the remote: 'empty', 'populated', or 'unreachable: <why>'."""
        code, out = await self._git("ls-remote", "--heads", url, check=False)
        if code != 0:
            # git's remote errors are multi-line and the last line is usually
            # the least informative half of a sentence; prefer the fatal line.
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            detail = next((ln for ln in lines if "fatal" in ln.lower()), lines[0] if lines else "")
            return f"unreachable: {detail or 'unknown error'}"
        return "populated" if out.strip() else "empty"

    def _vault_is_empty(self) -> bool:
        """Empty enough to clone into: no notes, no .git, no user content."""
        ignorable = {".ai-tmp", ".gitignore", ".DS_Store", ".stfolder"}
        return not any(p.name not in ignorable for p in self.vault_path.iterdir())

    async def _ensure_remote(self, url: str) -> None:
        code, current = await self._git("remote", "get-url", self.remote, check=False)
        if code != 0:
            await self._git("remote", "add", self.remote, url)
        elif current.strip() != url:
            log.info("remote %s: %s -> %s", self.remote, current.strip(), url)
            await self._git("remote", "set-url", self.remote, url)

    async def _ensure_branch(self) -> None:
        code, _ = await self._git("rev-parse", "--verify", "HEAD", check=False)
        if code != 0:
            return  # unborn branch; the first commit creates it
        _, current = await self._git("rev-parse", "--abbrev-ref", "HEAD")
        if current.strip() != self.branch:
            log.info("checking out %s (was on %s)", self.branch, current.strip())
            await self._git("checkout", "-B", self.branch)

    async def _init_and_push(self, url: str, remote_state: str) -> None:
        await self._git("init", "-b", self.branch)
        gitignore = self.vault_path / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(VAULT_GITIGNORE, encoding="utf-8")
        await self._ensure_remote(url)
        await self._git("add", "-A")
        code, _ = await self._git("diff", "--cached", "--quiet", check=False)
        if code == 0:
            log.info("vault initialized; nothing to commit yet")
            return
        await self._git("commit", "-m", "init: provision vault")
        if remote_state == "empty":
            await self._git("push", "-u", self.remote, f"HEAD:{self.branch}")
            self._own_pushes.add(await self.head())
            log.info("pushed initial commit to %s/%s", self.remote, self.branch)

    async def remote_branch_exists(self) -> bool:
        """Does the tracked branch still exist on the remote?"""
        code, out = await self._git(
            "ls-remote", "--heads", self.remote, self.branch, check=False
        )
        return code == 0 and bool(out.strip())

    async def provision(self) -> None:
        """Bring the vault to a usable git state, or disable git and say why.

        Only unambiguous moves are automatic. A vault with notes in it and a
        remote that already has unrelated history is a merge decision, not a
        provisioning step, so that case stops and asks rather than guessing at
        `--allow-unrelated-histories` on someone's knowledge base.
        """
        if not self.enabled:
            return
        problem: str | None = None
        has_repo = (await self._git("rev-parse", "--git-dir", check=False))[0] == 0

        try:
            if has_repo:
                if self.remote_url:
                    await self._ensure_remote(self.remote_url)
                await self._ensure_branch()
                code, out = await self._git("remote", "get-url", self.remote, check=False)
                if code != 0 or not out.strip():
                    problem = (
                        f"no remote named {self.remote!r} in {self.vault_path}. "
                        "Set GIT_REMOTE_URL, or add it manually."
                    )
            elif not self.remote_url:
                problem = (
                    f"{self.vault_path} is not a git repository and GIT_REMOTE_URL is "
                    "unset, so there is nothing to provision from. Set it, run "
                    f"`git init -b {self.branch}` there, or set GIT_ENABLED=false."
                )
            elif not self.auto_init:
                problem = (
                    f"{self.vault_path} is not a git repository and GIT_AUTO_INIT is "
                    "false."
                )
            else:
                state = await self._probe_remote(self.remote_url)
                if state.startswith("unreachable"):
                    problem = f"cannot reach {self.remote_url}: {state.split(': ', 1)[1]}"
                elif state == "populated" and self._vault_is_empty():
                    log.info("cloning %s into empty vault", self.remote_url)
                    await self._git("clone", "--branch", self.branch, "--", self.remote_url, ".")
                elif state == "populated":
                    problem = (
                        f"{self.vault_path} has files but is not a git repository, and "
                        f"{self.remote_url} already has history. Refusing to guess: "
                        "clone the remote elsewhere and merge by hand, or empty the "
                        "vault and restart to clone it."
                    )
                else:
                    log.info("initializing vault and pushing to %s", self.remote_url)
                    await self._init_and_push(self.remote_url, state)
        except GitError as exc:
            problem = f"provisioning failed: {exc}"

        if problem:
            log.error("git disabled: %s", problem)
            self.enabled = False
            self.disabled_reason = problem

    # ---- public API -----------------------------------------------------

    async def sync(self) -> dict[str, str]:
        """Pull remote changes into the working tree."""
        if not self.enabled:
            return {"status": "disabled", "reason": self.disabled_reason}
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
            return {"status": "disabled", "reason": self.disabled_reason}
        async with self.lock:
            sha = await self._commit(paths, message)
            if sha is None:
                return {"status": "nothing-to-commit"}
            if self.auto_push:
                await self._push()
                return {"status": "pushed", "commit": sha}
            return {"status": "committed", "commit": sha}
