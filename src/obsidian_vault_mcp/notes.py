"""Frontmatter construction, filename derivation, path containment, atomic writes."""

from __future__ import annotations

import os
import re
import stat as stat_module
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


def diagnose_path(vault: Path, target: Path) -> str | None:
    """Explain precisely why `target` is not a readable, writable note.

    `Path.is_file()` returns False for "does not exist" and for "a parent
    directory is not traversable by this uid" alike, so a permissions problem
    reads as a missing file and sends you hunting for a path that is right
    there. Walk the chain and name the component that actually failed.
    """
    vault = vault.resolve()
    parts = target.relative_to(vault).parts
    current = vault
    for i, part in enumerate(parts):
        current = current / part
        last = i == len(parts) - 1
        try:
            st = os.stat(current)
        except FileNotFoundError:
            return None if last else f"directory does not exist: {_rel(vault, current)}"
        except PermissionError:
            return (
                f"permission denied on {_rel(vault, current)} — the uid this "
                "container runs as cannot traverse it. Check ownership of the "
                "vault against the `user:` setting."
            )
        if last:
            if not stat_module.S_ISREG(st.st_mode):
                return f"{_rel(vault, current)} is not a regular file"
            if not os.access(current, os.R_OK):
                return f"permission denied reading {_rel(vault, current)}"
            if not os.access(current.parent, os.W_OK | os.X_OK):
                return (
                    f"permission denied writing in {_rel(vault, current.parent)} — "
                    "the note exists but cannot be replaced by this uid."
                )
        elif not os.access(current, os.X_OK):
            return (
                f"permission denied entering {_rel(vault, current)} — the uid this "
                "container runs as cannot traverse it."
            )
    return None


def _rel(vault: Path, path: Path) -> str:
    return path.relative_to(vault).as_posix()


def suggest_paths(vault: Path, allowed: Sequence[str], wanted: str, limit: int = 5) -> list[str]:
    """Nearest existing notes to a path that missed, for the error message.

    A bare "no such note" leaves an agent with nowhere to go but retrying the
    same wrong path, which is exactly what it does.
    """
    import difflib

    roots = [vault] if any(r in (".", "", "/") for r in allowed) else [
        (vault / r) for r in allowed
    ]
    candidates: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            if DENIED_COMPONENTS.intersection(path.parts):
                continue
            candidates.append(path.relative_to(vault).as_posix())
            if len(candidates) > 5000:  # don't walk an enormous vault forever
                break
    # A path that looks identical but differs in Unicode is the nastiest miss:
    # NBSP vs space, NFD vs NFC from a macOS-written filename, a curly
    # apostrophe. It is invisible in terminal output and in the error message,
    # so match on a folded form and then say exactly what differs.
    folded_wanted = _fold(wanted)
    for candidate in candidates:
        if candidate != wanted and _fold(candidate) == folded_wanted:
            return [f"{candidate} (differs only in unicode: {_escape(candidate)})"]

    wanted_name = Path(wanted).name.lower()
    by_name = [c for c in candidates if Path(c).name.lower() == wanted_name]
    if by_name:
        return by_name[:limit]
    return difflib.get_close_matches(wanted, candidates, n=limit, cutoff=0.5)


def _fold(text: str) -> str:
    """Normalize away the differences that make two paths look identical."""
    normalized = unicodedata.normalize("NFC", text)
    return "".join(" " if ch.isspace() else ch for ch in normalized).casefold()


def _escape(text: str) -> str:
    """Show non-ASCII characters as escapes so the difference is visible."""
    return "".join(ch if ch.isascii() and ch.isprintable() else f"\\u{ord(ch):04x}" for ch in text)


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
