"""Search backend adapter.

`SearchBackend` is the seam. Tools in `server.py` speak only to this protocol,
so replacing obsidian-hybrid-search later means writing one class here and
changing one env var — the MCP tool contract your agents depend on does not
move. That is the argument for an explicit adapter over a generic tool proxy:
a proxy would make OHS's schema your public API.

`OHSBackend` supervises `obsidian-hybrid-search serve` as a child process on
loopback and calls it over JSON-RPC. Deliberately not the CLI per query: a cold
`obsidian-hybrid-search "query" --json` costs ~500 ms of Node startup, database
open, and embedding-endpoint probe every single time, versus ~10 ms against the
long-lived server. Same work, 50x the latency.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
from typing import Any, Protocol, runtime_checkable

try:  # the mcp SDK vendors httpx2; plain httpx works too
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]

log = logging.getLogger(__name__)

_HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


@runtime_checkable
class SearchBackend(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def search(self, **kwargs: Any) -> dict[str, Any]: ...
    async def read(self, paths: list[str], snippet_length: int | None = None) -> dict[str, Any]: ...
    async def reindex(self, path: str | None = None) -> dict[str, Any]: ...


class BackendError(RuntimeError):
    pass


class NullBackend:
    """Used when OHS_ENABLED=false. The write tools still work."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def _refuse(self) -> dict[str, Any]:
        raise BackendError("no search backend configured (OHS_ENABLED=false)")

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        return await self._refuse()

    async def read(self, paths: list[str], snippet_length: int | None = None) -> dict[str, Any]:
        return await self._refuse()

    async def reindex(self, path: str | None = None) -> dict[str, Any]:
        return await self._refuse()


def _parse(body: str) -> dict[str, Any]:
    """Streamable HTTP replies as SSE or as a bare JSON object."""
    payload: dict[str, Any] | None = None
    for line in body.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line[5:].strip())
            break
    if payload is None:
        payload = json.loads(body)
    if "error" in payload:
        raise BackendError(str(payload["error"]))
    return payload.get("result", {})


def _text(result: dict[str, Any]) -> str:
    return "\n".join(
        b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"
    )


def _content(result: dict[str, Any]) -> dict[str, Any]:
    """A tool result is content blocks; OHS returns one JSON text block."""
    if result.get("isError"):
        raise BackendError(_text(result) or "backend reported an error")
    text = _text(result)
    if not text:
        return {}
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return decoded if isinstance(decoded, dict) else {"results": decoded}


class OHSBackend:
    def __init__(
        self,
        *,
        vault_path: str,
        url: str = "http://127.0.0.1:3939/mcp",
        spawn: bool = True,
        command: str = "obsidian-hybrid-search",
        port: int = 3939,
        startup_timeout: float = 120.0,
        request_timeout: float = 60.0,
    ) -> None:
        self.vault_path = vault_path
        self.url = url
        self.spawn = spawn
        self.command = command
        self.port = port
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._client: Any = None
        self._session_id: str | None = None
        self._id = 0

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=self.request_timeout)
        if self.spawn:
            await self._spawn()
        await self._handshake()

    async def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            if self._proc.returncode is None:
                self._proc.kill()
        self._proc = None
        if self._drain_task:
            self._drain_task.cancel()
            self._drain_task = None

    async def _spawn(self) -> None:
        argv = [
            *shlex.split(self.command),
            "serve", "--http", "--foreground",
            "--host", "127.0.0.1", "--port", str(self.port),
        ]
        env = dict(os.environ, OBSIDIAN_VAULT_PATH=self.vault_path)
        log.info("spawning search backend: %s", " ".join(argv))
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        # Held so the task is not garbage-collected mid-flight.
        self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        if not (self._proc and self._proc.stdout):
            return
        async for line in self._proc.stdout:
            log.info("[ohs] %s", line.decode("utf-8", "replace").rstrip())

    async def _handshake(self) -> None:
        """Poll until the backend answers, keeping any session id it hands back.

        OHS's server is stateless today, so the id is normally absent; capturing
        it means a future stateful release keeps working unchanged.
        """
        loop = asyncio.get_running_loop()
        end = loop.time() + self.startup_timeout
        last: Exception | None = None
        while loop.time() < end:
            if self._proc and self._proc.returncode is not None:
                raise BackendError(f"search backend exited with {self._proc.returncode}")
            try:
                response = await self._client.post(
                    self.url,
                    headers=_HEADERS,
                    json={
                        "jsonrpc": "2.0", "id": 0, "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "obsidian-vault-mcp", "version": "0.2.0"},
                        },
                    },
                )
                response.raise_for_status()
                self._session_id = response.headers.get("mcp-session-id")
                tools = await self._rpc("tools/list", {})
                log.info(
                    "search backend ready at %s (tools: %s)",
                    self.url,
                    ", ".join(t["name"] for t in tools.get("tools", [])),
                )
                return
            except Exception as exc:
                last = exc
                await asyncio.sleep(1.0)
        raise BackendError(f"search backend not reachable at {self.url}: {last}")

    # ---- calls ----------------------------------------------------------

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise BackendError("search backend is not started")
        self._id += 1
        headers = dict(_HEADERS)
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        try:
            response = await self._client.post(
                self.url,
                headers=headers,
                json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
            )
            response.raise_for_status()
        except Exception as exc:
            raise BackendError(f"search backend request failed: {exc}") from exc
        return _parse(response.text)

    async def _tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = {k: v for k, v in arguments.items() if v is not None}
        return _content(await self._rpc("tools/call", {"name": name, "arguments": args}))

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        return await self._tool("search", kwargs)

    async def read(self, paths: list[str], snippet_length: int | None = None) -> dict[str, Any]:
        return await self._tool("read", {"paths": paths, "snippet_length": snippet_length})

    async def reindex(self, path: str | None = None) -> dict[str, Any]:
        return await self._tool("reindex", {"path": path})
