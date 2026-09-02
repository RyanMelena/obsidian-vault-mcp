from __future__ import annotations

import logging
import os

from .config import Config
from .server import build_server


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Per-request chatter from the SDK and the HTTP client: one
    # "Terminating session: None" per tool call (stateless_http assigns no
    # session id) and one request line per search backend call. Useful when
    # debugging the transport, noise the rest of the time.
    if os.environ.get("LOG_LEVEL", "INFO").upper() != "DEBUG":
        for noisy in ("mcp.server.streamable_http", "httpx", "httpx2"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = Config.from_env()
    logging.getLogger("obsidian-vault-mcp").info(
        "vault=%s inbox=%s git=%s listening on %s:%s%s",
        cfg.vault_path, cfg.inbox_dir, cfg.git_enabled, cfg.host, cfg.port, cfg.mcp_path,
    )
    build_server(cfg).run(
        transport="streamable-http",
        host=cfg.host,
        port=cfg.port,
        streamable_http_path=cfg.mcp_path,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
