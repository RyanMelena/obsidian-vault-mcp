"""Frontmatter construction, filename derivation, path containment, atomic writes."""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)

_SLUG_STRIP = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_SPACE = re.compile(r"[\s_-]+")


class NoteError(ValueError):
    """Raised for any caller-supplied input we refuse."""


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(title: str, max_len: int = 60) -> str:
    norm = unicodedata.normalize("NFKD", title)
    norm = _SLUG_STRIP.sub("", norm).strip()
    slug = _SLUG_SPACE.sub("-", norm).strip("-").lower()
    slug = slug[:max_len].rstrip("-")
    return slug or "note"


# Never writable, wherever they appear in the path. Corrupting these breaks the
# vault, the repo, or the index in ways a note edit should never be able to.
DENIED_COMPONENTS = frozenset({".git", ".obsidian", ".ai-tmp", ".trash"})


def resolve_writable(vault: Path, allowed: Sequence[str], relative: str) -> Path:
    """Resolve a vault-relative path, refusing anything outside the writable scope.

    `allowed` is a list of vault-relative folders; an entry of "." means the
    whole vault. DENIED_COMPONENTS are refused regardless of scope.
    """
    if "\x00" in relative:
        raise NoteError("path contains a NUL byte")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise NoteError("path must be vault-relative")

    vault = vault.resolve()
    target = (vault / candidate).resolve()
    if target == vault or vault not in target.parents:
        raise NoteError(f"path escapes the vault: {relative!r}")

    parts = target.relative_to(vault).parts
    if DENIED_COMPONENTS.intersection(parts):
        raise NoteError(f"path is in a protected directory: {relative!r}")

    if any(root in (".", "", "/") for root in allowed):
        return target
    for root in allowed:
        scope = (vault / root).resolve()
        if target == scope or scope in target.parents:
            return target
    raise NoteError(
        f"{relative!r} is outside the writable scope ({', '.join(allowed)}). "
        "Set WRITABLE_PATHS to widen it."
    )


def resolve_within(root: Path, relative: str) -> Path:
    """Resolve `relative` under `root`, refusing anything that escapes it.

    Guards against absolute paths, `..` traversal, NUL bytes, and
    symlinked parents that point outside the vault.
    """
    if "\x00" in relative:
        raise NoteError("path contains a NUL byte")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise NoteError("path must be vault-relative")
    root = root.resolve()
    target = (root / candidate).resolve()
    if target == root or root not in target.parents:
        raise NoteError(f"path escapes {root}: {relative!r}")
    return target


def _validate_extra(extra: dict[str, Any] | None, reserved: frozenset[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (extra or {}).items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][\w-]*", key):
            raise NoteError(f"invalid frontmatter key: {key!r}")
        if key in reserved:
            raise NoteError(f"frontmatter key {key!r} is managed by the server")
        if not _is_scalar_or_list(value):
            raise NoteError(f"frontmatter value for {key!r} must be a scalar or list of scalars")
        out[key] = value
    return out


def _is_scalar_or_list(value: Any) -> bool:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    return isinstance(value, list) and all(
        v is None or isinstance(v, str | int | float | bool) for v in value
    )


def build_note(
    *,
    note_id: str,
    title: str,
    body: str,
    created: str,
    tags: list[str] | None = None,
    source: str | None = None,
    status: str = "active",
    extra: dict[str, Any] | None = None,
    reserved: frozenset[str] = frozenset(),
) -> str:
    title = title.strip()
    if not title:
        raise NoteError("title must not be empty")
    if not body.strip():
        raise NoteError("body must not be empty")

    fm: dict[str, Any] = {"id": note_id, "title": title, "created": created, "status": status}
    if source:
        fm["source"] = source
    if tags:
        bad = [t for t in tags if not isinstance(t, str) or not t.strip()]
        if bad:
            raise NoteError("tags must be non-empty strings")
        fm["tags"] = [t.strip().lstrip("#") for t in tags]
    fm.update(_validate_extra(extra, reserved))

    head = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{head}---\n\n{body.strip()}\n"


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    loaded = yaml.safe_load(match.group(1)) or {}
    if not isinstance(loaded, dict):
        raise NoteError("frontmatter is not a mapping")
    return loaded, text[match.end():]


def render(frontmatter: dict[str, Any], body: str) -> str:
    if not frontmatter:
        return body
    head = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return f"---\n{head}---\n\n{body.lstrip()}"


def unique_path(directory: Path, slug: str, note_id: str) -> Path:
    """`slug.md`, falling back to `slug-<id8>.md` when taken.

    Keeps wikilink-friendly filenames in the common case without letting a
    second note with the same title clobber the first.
    """
    candidate = directory / f"{slug}.md"
    if not candidate.exists():
        return candidate
    return directory / f"{slug}-{note_id[-8:].lower()}.md"


def atomic_write(dest: Path, content: str, tmp_root: Path) -> None:
    """Write via a temp file on the same filesystem, then rename into place.

    The rename is what makes this safe: OHS's chokidar watcher and any git
    operation see either the old file or the complete new one, never a
    half-written note.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=tmp_root, suffix=".md")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    dir_fd = os.open(dest.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
