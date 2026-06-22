"""Command line entry point for the local HTTP gateway.

This intentionally binds to 127.0.0.1 by default. It exposes small
OpenAI-compatible and Anthropic-compatible surfaces that translate requests into
the ChatHub WebSocket call implemented in the m365auth package.
"""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path

from ..cli import configure_stdio
from ..constants import (
    CHAT_SCOPE,
    DEFAULT_ENV,
    DEFAULT_HAR,
    DEFAULT_TOKEN_REFRESH_SKEW_SECONDS,
)
from ..har import resolve_entry_indices
from .server import Gateway, Handler


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--har", type=Path, default=DEFAULT_HAR)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--websocket-entry", type=int, default=None)
    parser.add_argument("--oauth-refresh-entry", type=int, default=None)
    parser.add_argument("--chat-scope", default=CHAT_SCOPE)
    parser.add_argument("--websocket-timeout", type=float, default=90.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--token-refresh-skew", type=int, default=DEFAULT_TOKEN_REFRESH_SKEW_SECONDS)
    parser.add_argument("--no-auto-refresh-token", action="store_true")
    parser.add_argument("--new-conversation-per-request", action="store_true")
    parser.add_argument("--max-body-bytes", type=int, default=2_000_000)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    configure_stdio()
    args = parse_args(argv)
    args.websocket_entry, args.oauth_refresh_entry = resolve_entry_indices(
        args.har, args.websocket_entry, args.oauth_refresh_entry
    )
    gateway = Gateway(args)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.gateway = gateway  # type: ignore[attr-defined]

    auth_state = "enabled" if gateway.proxy_api_key else "disabled"
    print(f"Serving M365 gateway on http://{args.host}:{args.port} auth={auth_state}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0
