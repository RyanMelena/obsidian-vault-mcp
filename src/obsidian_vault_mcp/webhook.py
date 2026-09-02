"""Gitea webhook verification.

Gitea signs the raw request body with HMAC-SHA256 using the webhook secret and
sends it as `X-Gitea-Signature` (hex). `X-Hub-Signature-256` (`sha256=<hex>`) is
accepted too, so a GitHub-style sender or a plain `curl` test also works.
"""

from __future__ import annotations

import hmac
import json
from hashlib import sha256
from typing import Any


class WebhookError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _expected(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def verify(secret: str, body: bytes, headers: dict[str, str]) -> None:
    lowered = {k.lower(): v for k, v in headers.items()}
    got = lowered.get("x-gitea-signature") or lowered.get("x-hub-signature-256", "")
    if got.startswith("sha256="):
        got = got[len("sha256="):]
    if not got:
        raise WebhookError("missing signature header", 401)
    if not hmac.compare_digest(got.strip().lower(), _expected(secret, body)):
        raise WebhookError("signature mismatch", 401)


def parse_push(body: bytes, branch: str) -> dict[str, Any]:
    """Return {'relevant': bool, 'after': sha, 'reason': str} for a push payload."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookError(f"malformed JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise WebhookError("payload is not an object")

    ref = payload.get("ref")
    after = payload.get("after")
    if ref is None:
        # Not a push event (issue comment, release, ...). Harmless; ignore.
        return {"relevant": False, "after": after, "reason": "not a push event"}
    if ref != f"refs/heads/{branch}":
        return {"relevant": False, "after": after, "reason": f"ref {ref} is not the tracked branch"}
    if isinstance(after, str) and set(after) == {"0"}:
        return {"relevant": False, "after": after, "reason": "branch deletion"}
    return {"relevant": True, "after": after, "reason": "push to tracked branch"}
