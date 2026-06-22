"""Command line entry point for the auth/session helper and ChatHub client.

Credentials are loaded from .env instead of being embedded in source. The HAR is
used only as a template for extracting login material and reconstructing the
ChatHub request shape.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .chat import run_chat
from .constants import (
    ACTIVE_CONVERSATION_ENV,
    CHAT_SCOPE,
    DEFAULT_ENV,
    DEFAULT_HAR,
    DEFAULT_TOKEN_REFRESH_SKEW_SECONDS,
)
from .env import load_env, update_gitignore
from .har import read_har, resolve_entry_indices, write_env_from_har
from .tokens import ensure_chat_token, refresh_chat_token


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--har", type=Path, default=DEFAULT_HAR)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--init-env", action="store_true", help="extract credentials from the HAR into .env")
    parser.add_argument("--chat", help="send this prompt through the captured ChatHub WebSocket flow")
    parser.add_argument("--refresh-chat-token", action="store_true", help="refresh the ChatHub access token in .env and exit unless --chat is also set")
    parser.add_argument("--chat-scope", default=CHAT_SCOPE, help="OAuth scope used when refreshing the ChatHub token")
    parser.add_argument("--oauth-refresh-entry", type=int, default=None, help="HAR entry index for the refresh-token OAuth request template (auto-detected if unset)")
    parser.add_argument("--websocket-entry", type=int, default=None, help="HAR entry index for the ChatHub WebSocket (auto-detected if unset)")
    parser.add_argument("--websocket-timeout", type=float, default=90.0, help="ChatHub WebSocket timeout in seconds")
    parser.add_argument("--token-refresh-skew", type=int, default=DEFAULT_TOKEN_REFRESH_SKEW_SECONDS, help="refresh ChatHub token when it expires within this many seconds")
    parser.add_argument("--no-auto-refresh-token", action="store_true", help="do not refresh ChatHub token automatically before --chat")
    parser.add_argument("--raw-websocket", action="store_true", help="print raw SignalR messages while chatting")
    parser.add_argument("--stream", action="store_true", help="print cursor deltas while the answer streams")
    parser.add_argument("--new-conversation", action="store_true", help="replace the captured ConversationId with a new UUID")
    parser.add_argument("--conversation-id", help="use this explicit ConversationId for ChatHub requests")
    parser.add_argument("--continue-chat", action="store_true", help=f"reuse or create {ACTIVE_CONVERSATION_ENV} in .env")
    parser.add_argument("--save-conversation", action="store_true", help=f"save the selected ConversationId to {ACTIVE_CONVERSATION_ENV} in .env")
    parser.add_argument("--interactive", action="store_true", help="keep sending prompts to the same conversation until exit")
    parser.add_argument("--timeout", type=float, default=30.0, help="request timeout in seconds")
    args = parser.parse_args(argv)

    if (
        not args.init_env
        and not args.chat
        and not args.interactive
        and not args.refresh_chat_token
    ):
        parser.print_help()
        raise SystemExit(0)

    return args


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    configure_stdio()
    args = parse_args(argv)
    entries = read_har(args.har)

    if args.init_env:
        write_env_from_har(entries, args.env)
        update_gitignore(Path(".gitignore"))
        return 0

    args.websocket_entry, args.oauth_refresh_entry = resolve_entry_indices(
        args.har, args.websocket_entry, args.oauth_refresh_entry
    )

    env = load_env(args.env)

    if args.refresh_chat_token and not args.chat and not args.interactive:
        refresh_chat_token(args, entries, env)
        return 0

    if args.chat or args.interactive:
        if not args.no_auto_refresh_token:
            ensure_chat_token(args, entries, env)
        return run_chat(args, entries, env)

    return 0
