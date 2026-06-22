"""Optional Playwright helper to refresh the .env credentials by logging in.

The captured HAR's SPA refresh token has a hard ~24h lifetime, so credentials go
stale daily. This reuses the existing HAR as the stable request template and only
harvests fresh tokens, cookies, and Authorization headers from a live browser
session into .env. A persistent browser profile means you log in (and do MFA)
once, then later runs reuse the session silently.

This is an OPTIONAL extra. It needs Playwright, which the core gateway does not:

    pip install playwright
    playwright install chromium

Then refresh credentials with:

    python -m m365auth.login            # headed; log in on first run
    python -m m365auth.login --headless # once the profile is logged in

The core gateway stays pure stdlib; Playwright is imported lazily below.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from urllib import parse

from .cli import configure_stdio
from .constants import DEFAULT_ENV, DEFAULT_HAR, SENSITIVE_HEADERS
from .env import header_env_name, indexed_env_name, set_env_values
from .har import HarEntry, read_har, resolve_entry_indices

CHAT_URL = "https://m365.cloud.microsoft/chat"
TOKEN_ENDPOINT_MARKER = "oauth2/v2.0/token"
DEFAULT_PROFILE_DIR = Path.home() / ".m365auth" / "pw-profile"


def credential_targets(
    entries: list[HarEntry],
    websocket_entry: int,
    oauth_refresh_entry: int,
) -> tuple[dict[str, dict[str, str]], str, str]:
    """Map the live values we capture onto the .env keys the gateway reads.

    Returns ``(header_keys, access_key, refresh_key)`` where ``header_keys`` is
    ``{host: {header_name: env_key}}`` for every sensitive cookie/authorization
    header in the HAR, ``access_key`` is the ChatHub websocket access-token key,
    and ``refresh_key`` is the OAuth refresh-token key.
    """
    header_keys: dict[str, dict[str, str]] = {}
    for entry in entries:
        for header in entry.request_headers:
            name = header.get("name", "").lower()
            if name in SENSITIVE_HEADERS:
                header_keys.setdefault(entry.host, {})[name] = header_env_name(entry.host, name)
    access_key = indexed_env_name(websocket_entry, "access_token")
    refresh_key = indexed_env_name(oauth_refresh_entry, "refresh_token")
    return header_keys, access_key, refresh_key


def _cookie_header_from_jar(cookies: list[dict], host: str) -> str | None:
    """Reconstruct a Cookie header for ``host`` from a Playwright cookie jar."""
    matched = [
        c for c in cookies
        if host == c.get("domain", "").lstrip(".") or host.endswith(c.get("domain", "").lstrip("."))
    ]
    if not matched:
        return None
    return "; ".join(f"{c['name']}={c['value']}" for c in matched)


def refresh_credentials(args: argparse.Namespace) -> int:
    entries = read_har(args.har)
    websocket_entry, oauth_refresh_entry = resolve_entry_indices(
        args.har, args.websocket_entry, args.oauth_refresh_entry
    )
    header_keys, access_key, refresh_key = credential_targets(entries, websocket_entry, oauth_refresh_entry)
    sensitive_hosts = set(header_keys)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is not installed. Install it with:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )
        return 1

    captured: dict[str, object] = {"access_token": None, "refresh_token": None, "headers": {}}

    def on_request(request) -> None:
        host = parse.urlsplit(request.url).netloc
        if host not in sensitive_hosts:
            return
        try:
            headers = request.all_headers()
        except Exception:
            return
        store = captured["headers"].setdefault(host, {})  # type: ignore[union-attr]
        for name in ("cookie", "authorization"):
            if headers.get(name):
                store[name] = headers[name]

    def on_response(response) -> None:
        if TOKEN_ENDPOINT_MARKER not in response.url:
            return
        try:
            body = response.json()
        except Exception:
            return
        if isinstance(body, dict) and body.get("refresh_token"):
            captured["refresh_token"] = body["refresh_token"]

    def on_websocket(ws) -> None:
        token = parse.parse_qs(parse.urlsplit(ws.url).query).get("access_token")
        if token:
            captured["access_token"] = token[0]

    args.profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(args.profile_dir),
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context.on("request", on_request)
        context.on("response", on_response)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("websocket", on_websocket)

        print(f"Opening {args.url}")
        print(f"  Profile: {args.profile_dir} (log in / do MFA on first run; it persists)")
        if not args.headless:
            print("  If no access token is captured, send one chat message to open the ChatHub socket.")
        page.goto(args.url, wait_until="domcontentloaded", timeout=int(args.timeout * 1000))

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if captured["access_token"]:
                break
            page.wait_for_timeout(500)

        jar = context.cookies()
        context.close()

    updates: dict[str, str] = {}
    if captured["access_token"]:
        updates[access_key] = str(captured["access_token"])
    if captured["refresh_token"]:
        updates[refresh_key] = str(captured["refresh_token"])

    captured_headers: dict[str, dict[str, str]] = captured["headers"]  # type: ignore[assignment]
    for host, names in header_keys.items():
        seen = captured_headers.get(host, {})
        for name, env_key in names.items():
            value = seen.get(name)
            if not value and name == "cookie":
                value = _cookie_header_from_jar(jar, host)
            if value:
                updates[env_key] = value

    if not updates:
        print("No credentials captured. Try again headed and send a chat message.")
        return 1

    set_env_values(args.env, updates)

    print(f"\nUpdated {len(updates)} value(s) in {args.env}:")
    print(f"  access_token : {'yes' if access_key in updates else 'MISSING'}")
    print(f"  refresh_token: {'yes' if refresh_key in updates else 'missing (token may still work)'}")
    header_count = sum(1 for h in header_keys.values() for k in h.values() if k in updates)
    total_headers = sum(len(h) for h in header_keys.values())
    print(f"  headers      : {header_count}/{total_headers}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh .env credentials via a Playwright login session.")
    parser.add_argument("--har", type=Path, default=DEFAULT_HAR)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--url", default=CHAT_URL, help="page that triggers the ChatHub session")
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR, help="persistent browser profile directory")
    parser.add_argument("--headless", action="store_true", help="run without a visible browser (only once logged in)")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for the access token")
    parser.add_argument("--websocket-entry", type=int, default=None)
    parser.add_argument("--oauth-refresh-entry", type=int, default=None)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    configure_stdio()
    return refresh_credentials(parse_args(argv))


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
