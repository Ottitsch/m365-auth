"""ChatHub OAuth token decoding and refresh."""

from __future__ import annotations

import argparse
import base64
import json
import time
from typing import Any
from urllib import error, parse, request

from .env import indexed_env_name, set_env_values
from .har import HarEntry, body_text_from_bytes, decode_body
from .headers import build_headers, substitute_sensitive_url


def jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def token_seconds_left(token: str) -> int | None:
    payload = jwt_payload(token)
    exp = payload.get("exp")
    if not isinstance(exp, int):
        return None
    return exp - int(time.time())


def refresh_chat_token(args: argparse.Namespace, entries: list[HarEntry], env: dict[str, str]) -> dict[str, str]:
    oauth_entry = entries[args.oauth_refresh_entry]
    body = body_text_from_bytes(decode_body(oauth_entry))
    if not body:
        raise ValueError(f"HAR entry {args.oauth_refresh_entry} does not contain an OAuth token request body")

    values = dict(parse.parse_qsl(body, keep_blank_values=True))
    refresh_key = indexed_env_name(args.oauth_refresh_entry, "refresh_token")
    refresh_token = env.get(refresh_key)
    if not refresh_token:
        raise ValueError(f"{refresh_key} is missing from {args.env}")

    values["scope"] = args.chat_scope
    values["refresh_token"] = refresh_token

    headers = build_headers(oauth_entry, env)
    headers = {
        name: value
        for name, value in headers.items()
        if name.lower()
        in {"accept", "accept-language", "content-type", "origin", "referer", "user-agent"}
    }

    req = request.Request(
        substitute_sensitive_url(oauth_entry, env),
        data=parse.urlencode(values).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=args.timeout) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Token refresh failed with HTTP {exc.code}: {detail[:500]}") from exc

    access_token = response.get("access_token")
    if not access_token:
        raise RuntimeError("Token refresh response did not include access_token")

    updates = {
        indexed_env_name(args.websocket_entry, "access_token"): access_token,
    }
    if response.get("refresh_token"):
        updates[refresh_key] = response["refresh_token"]

    set_env_values(args.env, updates)
    env.update(updates)

    payload = jwt_payload(access_token)
    print(
        "Refreshed ChatHub token "
        f"aud={payload.get('aud', '<unknown>')} "
        f"seconds_left={token_seconds_left(access_token)}"
    )
    return updates


def ensure_chat_token(args: argparse.Namespace, entries: list[HarEntry], env: dict[str, str]) -> None:
    token_key = indexed_env_name(args.websocket_entry, "access_token")
    token = env.get(token_key)
    seconds_left = token_seconds_left(token) if token else None

    if args.refresh_chat_token or seconds_left is None or seconds_left <= args.token_refresh_skew:
        if not args.refresh_chat_token:
            remaining = "unknown" if seconds_left is None else f"{seconds_left}s"
            print(f"ChatHub token needs refresh; remaining={remaining}")
        refresh_chat_token(args, entries, env)
