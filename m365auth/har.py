"""Parsing the captured HAR and extracting credential material from it."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import parse

from .constants import (
    SENSITIVE_FORM_FIELDS,
    SENSITIVE_HEADERS,
    SENSITIVE_QUERY_FIELDS,
    SIGNALR_SEPARATOR,
)
from .env import (
    dotenv_quote,
    header_env_name,
    indexed_env_name,
)


@dataclass(frozen=True)
class HarEntry:
    index: int
    url: str
    host: str
    request_headers: list[dict[str, str]]
    post_text: str | None
    post_encoding: str | None


def read_har(path: Path) -> list[HarEntry]:
    with path.open("r", encoding="utf-8") as f:
        har = json.load(f)

    entries: list[HarEntry] = []
    for index, raw in enumerate(har["log"]["entries"]):
        req = raw["request"]
        post_data = req.get("postData") or {}
        entries.append(
            HarEntry(
                index=index,
                url=req["url"],
                host=parse.urlsplit(req["url"]).netloc,
                request_headers=req.get("headers", []),
                post_text=post_data.get("text"),
                post_encoding=post_data.get("encoding"),
            )
        )

    return entries


def read_raw_entries(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        har = json.load(f)
    return har["log"]["entries"]


def get_raw_entry(har_path: Path, index: int) -> dict[str, Any]:
    return read_raw_entries(har_path)[index]


def find_chat_websocket_index(raw_entries: list[dict[str, Any]]) -> int | None:
    """Index of the WebSocket entry carrying a sent SignalR target='chat' invocation."""
    for index, raw in enumerate(raw_entries):
        for message in raw.get("_webSocketMessages") or []:
            if message.get("type") != "send":
                continue
            for chunk in (message.get("data") or "").split(SIGNALR_SEPARATOR):
                chunk = chunk.strip()
                if not chunk:
                    continue
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("target") == "chat":
                    return index
    return None


def find_oauth_refresh_index(raw_entries: list[dict[str, Any]]) -> int | None:
    """Index of the first urlencoded POST carrying a refresh_token grant."""
    for index, raw in enumerate(raw_entries):
        req = raw.get("request") or {}
        if (req.get("method") or "").upper() != "POST":
            continue
        text = (req.get("postData") or {}).get("text")
        if not text:
            continue
        content_type = next(
            (
                h.get("value", "")
                for h in req.get("headers", [])
                if h.get("name", "").lower() == "content-type"
            ),
            "",
        )
        if "application/x-www-form-urlencoded" not in content_type.lower():
            continue
        form = dict(parse.parse_qsl(text, keep_blank_values=True))
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token"):
            return index
    return None


def resolve_entry_indices(
    path: Path,
    websocket_entry: int | None = None,
    oauth_refresh_entry: int | None = None,
) -> tuple[int, int]:
    """Fill in any unset HAR entry index by detecting it from the capture's contents."""
    if websocket_entry is not None and oauth_refresh_entry is not None:
        return websocket_entry, oauth_refresh_entry

    raw_entries = read_raw_entries(path)

    if websocket_entry is None:
        websocket_entry = find_chat_websocket_index(raw_entries)
        if websocket_entry is None:
            raise ValueError(
                "Could not find the ChatHub WebSocket in the HAR (no sent SignalR "
                "target='chat' invocation). Pass --websocket-entry to set it manually."
            )

    if oauth_refresh_entry is None:
        oauth_refresh_entry = find_oauth_refresh_index(raw_entries)
        if oauth_refresh_entry is None:
            raise ValueError(
                "Could not find an OAuth refresh-token request in the HAR. "
                "Pass --oauth-refresh-entry to set it manually."
            )

    return websocket_entry, oauth_refresh_entry


def decode_body(entry: HarEntry) -> bytes | None:
    if entry.post_text is None:
        return None
    if entry.post_encoding == "base64":
        return base64.b64decode(entry.post_text)
    return entry.post_text.encode("utf-8")


def body_text_from_bytes(data: bytes | None) -> str | None:
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")


def write_env_from_har(entries: list[HarEntry], env_path: Path) -> None:
    lines: list[str] = [
        "# Generated from m365.cloud.microsoft.har.",
        "# Do not commit this file; it contains cookies, bearer tokens, and refresh tokens.",
        "",
    ]
    seen: set[str] = set()

    for entry in entries:
        for header in entry.request_headers:
            name = header.get("name", "")
            lower = name.lower()
            if lower not in SENSITIVE_HEADERS:
                continue
            key = header_env_name(entry.host, lower)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{key}={dotenv_quote(header.get('value', ''))}")

        parts = parse.urlsplit(entry.url)
        query = dict(parse.parse_qsl(parts.query, keep_blank_values=True))
        for field in SENSITIVE_QUERY_FIELDS:
            if field not in query:
                continue
            key = indexed_env_name(entry.index, field)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{key}={dotenv_quote(query[field])}")

        body = body_text_from_bytes(decode_body(entry))
        if not body:
            continue
        content_type = next(
            (
                h.get("value", "")
                for h in entry.request_headers
                if h.get("name", "").lower() == "content-type"
            ),
            "",
        )
        if "application/x-www-form-urlencoded" not in content_type.lower():
            continue
        form = dict(parse.parse_qsl(body, keep_blank_values=True))
        for field in SENSITIVE_FORM_FIELDS:
            if field not in form:
                continue
            key = indexed_env_name(entry.index, field)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{key}={dotenv_quote(form[field])}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(seen)} secret values to {env_path}")
