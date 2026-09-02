from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_vault_mcp.notes import (
    NoteError,
    atomic_write,
    build_note,
    render,
    resolve_within,
    slugify,
    split_frontmatter,
    unique_path,
)
from obsidian_vault_mcp.ulid import ulid
from obsidian_vault_mcp.webhook import WebhookError, parse_push, verify

# ---- path containment: the security boundary ---------------------------

@pytest.mark.parametrize(
    "evil",
    [
        "../../.obsidian/plugins/x.md",
        "/etc/passwd",
        "sub/../../outside.md",
        "a\x00b.md",
        "..",
    ],
)
def test_resolve_within_rejects_escapes(tmp_path: Path, evil: str) -> None:
    root = tmp_path / "ai-inbox"
    root.mkdir()
    with pytest.raises(NoteError):
        resolve_within(root, evil)


def test_resolve_within_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "ai-inbox"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (root / "link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(NoteError):
        resolve_within(root, "link/note.md")


def test_resolve_within_allows_normal(tmp_path: Path) -> None:
    root = tmp_path / "ai-inbox"
    root.mkdir()
    assert resolve_within(root, "note.md") == (root / "note.md").resolve()


# ---- frontmatter -------------------------------------------------------

def test_build_note_roundtrip() -> None:
    text = build_note(
        note_id="01J",
        title="Traefik VLAN routing",
        body="Body text.",
        created="2026-09-01T00:00:00Z",
        tags=["#homelab", "networking"],
        source="chat",
    )
    meta, body = split_frontmatter(text)
    assert meta["id"] == "01J"
    assert meta["status"] == "active"
    assert meta["tags"] == ["homelab", "networking"]
    assert body.strip() == "Body text."


def test_build_note_rejects_reserved_and_bad_values() -> None:
    common = {"note_id": "x", "title": "T", "body": "B", "created": "c",
              "reserved": frozenset({"id"})}
    with pytest.raises(NoteError):
        build_note(**common, extra={"id": "spoofed"})
    with pytest.raises(NoteError):
        build_note(**common, extra={"nested": {"a": 1}})
    with pytest.raises(NoteError):
        build_note(**common, extra={"bad key!": 1})


def test_build_note_requires_content() -> None:
    with pytest.raises(NoteError):
        build_note(note_id="x", title="  ", body="B", created="c")
    with pytest.raises(NoteError):
        build_note(note_id="x", title="T", body="   ", created="c")


def test_yaml_injection_via_title_is_escaped() -> None:
    text = build_note(
        note_id="x",
        title='evil: "yes"\nstatus: superseded',
        body="B",
        created="c",
    )
    meta, _ = split_frontmatter(text)
    assert meta["status"] == "active"
    assert "evil" not in meta


def test_render_preserves_frontmatter() -> None:
    meta, body = split_frontmatter(render({"id": "x", "status": "active"}, "Hello"))
    assert meta == {"id": "x", "status": "active"}
    assert body.strip() == "Hello"


# ---- filenames ---------------------------------------------------------

def test_slugify() -> None:
    assert slugify("Traefik: VLAN routing!") == "traefik-vlan-routing"
    assert slugify("///") == "note"


def test_unique_path_avoids_clobber(tmp_path: Path) -> None:
    first = unique_path(tmp_path, "note", "01ABCDEFGH")
    first.write_text("x")
    second = unique_path(tmp_path, "note", "01ABCDEFGH")
    assert first != second


# ---- atomic write ------------------------------------------------------

def test_atomic_write_leaves_no_temp(tmp_path: Path) -> None:
    tmp_root = tmp_path / ".ai-tmp"
    dest = tmp_path / "ai-inbox" / "n.md"
    atomic_write(dest, "hello\n", tmp_root)
    assert dest.read_text() == "hello\n"
    assert list(tmp_root.iterdir()) == []


# ---- ulid --------------------------------------------------------------

def test_ulid_is_sortable_and_unique() -> None:
    a = ulid(now_ms=1_000_000_000_000)
    b = ulid(now_ms=1_000_000_000_001)
    assert len(a) == 26 and a < b
    assert len({ulid() for _ in range(500)}) == 500


# ---- webhook -----------------------------------------------------------

def test_verify_accepts_valid_and_rejects_tampered() -> None:
    body = b'{"ref":"refs/heads/main"}'
    import hmac
    from hashlib import sha256

    sig = hmac.new(b"s3cret", body, sha256).hexdigest()
    verify("s3cret", body, {"X-Gitea-Signature": sig})
    verify("s3cret", body, {"X-Hub-Signature-256": f"sha256={sig}"})
    with pytest.raises(WebhookError):
        verify("s3cret", body + b" ", {"X-Gitea-Signature": sig})
    with pytest.raises(WebhookError):
        verify("s3cret", body, {})


def test_parse_push_filters() -> None:
    ok = json.dumps({"ref": "refs/heads/main", "after": "abc"}).encode()
    assert parse_push(ok, "main")["relevant"] is True
    other = json.dumps({"ref": "refs/heads/dev", "after": "abc"}).encode()
    assert parse_push(other, "main")["relevant"] is False
    deletion = json.dumps({"ref": "refs/heads/main", "after": "0" * 40}).encode()
    assert parse_push(deletion, "main")["relevant"] is False
    assert parse_push(json.dumps({"action": "opened"}).encode(), "main")["relevant"] is False
    with pytest.raises(WebhookError):
        parse_push(b"not json", "main")


# ---- backend adapter ---------------------------------------------------

def test_parse_handles_sse_and_bare_json() -> None:
    from obsidian_vault_mcp.backends import BackendError, _parse

    sse = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":1}}\n\n'
    assert _parse(sse) == {"ok": 1}
    assert _parse('{"jsonrpc":"2.0","id":1,"result":{"ok":2}}') == {"ok": 2}
    with pytest.raises(BackendError):
        _parse('{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"bad"}}')


def test_content_decodes_and_surfaces_tool_errors() -> None:
    from obsidian_vault_mcp.backends import BackendError, _content

    block = {"content": [{"type": "text", "text": '{"results":[{"path":"a.md"}]}'}]}
    assert _content(block)["results"][0]["path"] == "a.md"
    # a bare JSON array is normalized to the same shape
    assert _content({"content": [{"type": "text", "text": '[{"path":"b.md"}]'}]})["results"]
    # non-JSON text is passed through rather than crashing
    assert _content({"content": [{"type": "text", "text": "plain"}]}) == {"text": "plain"}
    with pytest.raises(BackendError):
        _content({"isError": True, "content": [{"type": "text", "text": "boom"}]})


@pytest.mark.asyncio
async def test_null_backend_refuses_cleanly() -> None:
    from obsidian_vault_mcp.backends import BackendError, NullBackend

    backend = NullBackend()
    await backend.start()
    with pytest.raises(BackendError):
        await backend.search(query="x")
    await backend.stop()
