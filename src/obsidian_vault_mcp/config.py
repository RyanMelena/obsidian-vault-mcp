"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"{name} is required")
    return val


@dataclass(frozen=True)
class Config:
    vault_path: Path
    inbox_dir: str = "ai-inbox"
    tmp_dir: str = ".ai-tmp"

    host: str = "0.0.0.0"
    port: int = 3940
    mcp_path: str = "/mcp"
    webhook_path: str = "/hooks/gitea"

    git_enabled: bool = True
    git_remote: str = "origin"
    git_branch: str = "main"
    git_author_name: str = "obsidian-vault-mcp"
    git_author_email: str = "vault-mcp@localhost"
    auto_push: bool = True
    git_remote_url: str = ""
    git_auto_init: bool = True

    webhook_secret: str = ""

    # --- search backend ---
    ohs_enabled: bool = True
    ohs_spawn: bool = True
    ohs_url: str = "http://127.0.0.1:3939/mcp"
    ohs_command: str = "obsidian-hybrid-search"
    ohs_port: int = 3939
    ohs_startup_timeout: float = 120.0
    # Optional shell command run after a write or pull. OHS's chokidar watcher
    # normally handles this on its own; set it only if you want indexing to be
    # synchronous. "{path}" is substituted with the vault-relative path, or the
    # empty string for a full reindex.
    reindex_cmd: str = ""

    reserved_frontmatter: frozenset[str] = field(
        default_factory=lambda: frozenset({"id", "title", "created", "updated", "status"})
    )

    @property
    def inbox_root(self) -> Path:
        return (self.vault_path / self.inbox_dir).resolve()

    @property
    def tmp_root(self) -> Path:
        return (self.vault_path / self.tmp_dir).resolve()

    @classmethod
    def from_env(cls) -> Config:
        vault = Path(_req("VAULT_PATH")).resolve()
        if not vault.is_dir():
            raise SystemExit(f"VAULT_PATH does not exist or is not a directory: {vault}")
        git_enabled = _bool("GIT_ENABLED", True)
        secret = os.environ.get("GITEA_WEBHOOK_SECRET", "")
        if git_enabled and not secret:
            raise SystemExit(
                "GITEA_WEBHOOK_SECRET is required when GIT_ENABLED=true. "
                "Set GIT_ENABLED=false to run without the git/webhook path."
            )
        return cls(
            vault_path=vault,
            inbox_dir=os.environ.get("INBOX_DIR", "ai-inbox"),
            tmp_dir=os.environ.get("TMP_DIR", ".ai-tmp"),
            host=os.environ.get("HTTP_HOST", "0.0.0.0"),
            port=int(os.environ.get("HTTP_PORT", "3940")),
            mcp_path=os.environ.get("MCP_PATH", "/mcp"),
            webhook_path=os.environ.get("WEBHOOK_PATH", "/hooks/gitea"),
            git_enabled=git_enabled,
            git_remote=os.environ.get("GIT_REMOTE", "origin"),
            git_branch=os.environ.get("GIT_BRANCH", "main"),
            git_author_name=os.environ.get("GIT_AUTHOR_NAME", "obsidian-vault-mcp"),
            git_author_email=os.environ.get("GIT_AUTHOR_EMAIL", "vault-mcp@localhost"),
            auto_push=_bool("AUTO_PUSH", True),
            git_remote_url=os.environ.get("GIT_REMOTE_URL", ""),
            git_auto_init=_bool("GIT_AUTO_INIT", True),
            webhook_secret=secret,
            reindex_cmd=os.environ.get("REINDEX_CMD", ""),
            ohs_enabled=_bool("OHS_ENABLED", True),
            ohs_spawn=_bool("OHS_SPAWN", True),
            ohs_url=os.environ.get("OHS_URL", "http://127.0.0.1:3939/mcp"),
            ohs_command=os.environ.get("OHS_COMMAND", "obsidian-hybrid-search"),
            ohs_port=int(os.environ.get("OHS_PORT", "3939")),
            ohs_startup_timeout=float(os.environ.get("OHS_STARTUP_TIMEOUT", "120")),
        )
