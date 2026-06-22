"""Reconstructing request headers and URLs with env-substituted secrets."""

from __future__ import annotations

from urllib import parse

from .constants import (
    SENSITIVE_FORM_FIELDS,
    SENSITIVE_HEADERS,
    SENSITIVE_QUERY_FIELDS,
    SKIP_REQUEST_HEADERS,
)
from .env import header_env_name, indexed_env_name
from .har import HarEntry, body_text_from_bytes


def substitute_sensitive_url(entry: HarEntry, env: dict[str, str]) -> str:
    parts = parse.urlsplit(entry.url)
    pairs = parse.parse_qsl(parts.query, keep_blank_values=True)
    changed = False
    rewritten: list[tuple[str, str]] = []

    for key, value in pairs:
        if key in SENSITIVE_QUERY_FIELDS:
            env_key = indexed_env_name(entry.index, key)
            if env_key in env:
                value = env[env_key]
                changed = True
        rewritten.append((key, value))

    if not changed:
        return entry.url

    return parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            parse.urlencode(rewritten),
            parts.fragment,
        )
    )


def replace_url_query(url: str, replacements: dict[str, str]) -> str:
    parts = parse.urlsplit(url)
    pairs = parse.parse_qsl(parts.query, keep_blank_values=True)
    rewritten = [(key, replacements.get(key, value)) for key, value in pairs]
    return parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            parse.urlencode(rewritten),
            parts.fragment,
        )
    )


def get_query_value(url: str, name: str) -> str | None:
    parts = parse.urlsplit(url)
    query = dict(parse.parse_qsl(parts.query, keep_blank_values=True))
    return query.get(name)


def substitute_sensitive_body(entry: HarEntry, data: bytes | None, env: dict[str, str]) -> bytes | None:
    text = body_text_from_bytes(data)
    if text is None:
        return data

    content_type = next(
        (
            h.get("value", "")
            for h in entry.request_headers
            if h.get("name", "").lower() == "content-type"
        ),
        "",
    )
    if "application/x-www-form-urlencoded" not in content_type.lower():
        return data

    pairs = parse.parse_qsl(text, keep_blank_values=True)
    changed = False
    rewritten: list[tuple[str, str]] = []
    for key, value in pairs:
        if key in SENSITIVE_FORM_FIELDS:
            env_key = indexed_env_name(entry.index, key)
            if env_key in env:
                value = env[env_key]
                changed = True
        rewritten.append((key, value))

    if not changed:
        return data
    return parse.urlencode(rewritten).encode("utf-8")


def build_headers(entry: HarEntry, env: dict[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header in entry.request_headers:
        name = header.get("name", "")
        value = header.get("value", "")
        lower = name.lower()

        if lower in SKIP_REQUEST_HEADERS:
            continue

        if lower in SENSITIVE_HEADERS:
            value = env.get(header_env_name(entry.host, lower), value)

        headers[name] = value

    return headers


def websocket_headers(entry: HarEntry, env: dict[str, str]) -> dict[str, str]:
    headers = build_headers(entry, env)
    for name in list(headers):
        if name.lower().startswith("sec-fetch"):
            headers.pop(name)
    return headers
