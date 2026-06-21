#!/usr/bin/env python3
"""
Auth/session helper and ChatHub client for a captured M365 Copilot browser session.

Credentials are loaded from .env instead of being embedded in source. The HAR is
used only as a template for extracting login material and reconstructing the
ChatHub request shape.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import secrets
import socket
import ssl
import struct
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_HAR = Path("m365.cloud.microsoft.har")
DEFAULT_ENV = Path(".env")

SKIP_REQUEST_HEADERS = {
    ":authority",
    ":method",
    ":path",
    ":scheme",
    "accept-encoding",
    "connection",
    "content-length",
    "host",
    "priority",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "upgrade-insecure-requests",
}

SENSITIVE_HEADERS = {"authorization", "cookie"}
SENSITIVE_FORM_FIELDS = {"refresh_token"}
SENSITIVE_QUERY_FIELDS = {"access_token"}
SIGNALR_SEPARATOR = "\x1e"
CHAT_TOKEN_ENTRY = 576
OAUTH_REFRESH_ENTRY = 563
CHAT_SCOPE = "https://substrate.office.com/sydney/.default openid profile offline_access"
DEFAULT_TOKEN_REFRESH_SKEW_SECONDS = 300
ACTIVE_CONVERSATION_ENV = "M365_CHAT_CONVERSATION_ID"


@dataclass(frozen=True)
class HarEntry:
    index: int
    method: str
    url: str
    host: str
    path: str
    resource_type: str
    request_headers: list[dict[str, str]]
    post_text: str | None
    post_encoding: str | None
    expected_status: int | None


def env_name(*parts: str) -> str:
    joined = "_".join(parts)
    return re.sub(r"[^A-Z0-9]+", "_", joined.upper()).strip("_")


def host_key(host: str) -> str:
    return env_name(host)


def header_env_name(host: str, header_name: str) -> str:
    return env_name(host, header_name)


def form_env_name(index: int, field_name: str) -> str:
    return env_name("HAR_ENTRY", str(index), field_name)


def query_env_name(index: int, field_name: str) -> str:
    return env_name("HAR_ENTRY", str(index), field_name)


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key] = value

    return {**values, **os.environ}


def set_env_values(path: Path, updates: dict[str, str]) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    rewritten: list[str] = []

    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rewritten.append(line)
            continue

        key = line.split("=", 1)[0].strip()
        if key in updates:
            rewritten.append(f"{key}={dotenv_quote(updates[key])}")
            seen.add(key)
        else:
            rewritten.append(line)

    for key, value in updates.items():
        if key not in seen:
            rewritten.append(f"{key}={dotenv_quote(value)}")

    path.write_text("\n".join(rewritten).rstrip() + "\n", encoding="utf-8")


def dotenv_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    return f'"{escaped}"'


def read_har(path: Path) -> list[HarEntry]:
    with path.open("r", encoding="utf-8") as f:
        har = json.load(f)

    entries: list[HarEntry] = []
    for index, raw in enumerate(har["log"]["entries"]):
        req = raw["request"]
        parsed = parse.urlsplit(req["url"])
        post_data = req.get("postData") or {}
        entries.append(
            HarEntry(
                index=index,
                method=req["method"].upper(),
                url=req["url"],
                host=parsed.netloc,
                path=parsed.path or "/",
                resource_type=raw.get("_resourceType", ""),
                request_headers=req.get("headers", []),
                post_text=post_data.get("text"),
                post_encoding=post_data.get("encoding"),
                expected_status=(raw.get("response") or {}).get("status"),
            )
        )

    return entries


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


def decode_body(entry: HarEntry) -> bytes | None:
    if entry.post_text is None:
        return None
    if entry.post_encoding == "base64":
        return base64.b64decode(entry.post_text)
    return entry.post_text.encode("utf-8")


def substitute_sensitive_url(entry: HarEntry, env: dict[str, str]) -> str:
    parts = parse.urlsplit(entry.url)
    pairs = parse.parse_qsl(parts.query, keep_blank_values=True)
    changed = False
    rewritten: list[tuple[str, str]] = []

    for key, value in pairs:
        if key in SENSITIVE_QUERY_FIELDS:
            env_key = query_env_name(entry.index, key)
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


def masked_url(url: str) -> str:
    parts = parse.urlsplit(url)
    pairs = parse.parse_qsl(parts.query, keep_blank_values=True)
    rewritten = [
        (key, "<secret>" if key in SENSITIVE_QUERY_FIELDS else value)
        for key, value in pairs
    ]
    return parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            parse.urlencode(rewritten),
            parts.fragment,
        )
    )


def body_text_from_bytes(data: bytes | None) -> str | None:
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")


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
            env_key = form_env_name(entry.index, key)
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
            key = query_env_name(entry.index, field)
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
            key = form_env_name(entry.index, field)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{key}={dotenv_quote(form[field])}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(seen)} secret values to {env_path}")


def refresh_chat_token(args: argparse.Namespace, entries: list[HarEntry], env: dict[str, str]) -> dict[str, str]:
    oauth_entry = entries[args.oauth_refresh_entry]
    body = body_text_from_bytes(decode_body(oauth_entry))
    if not body:
        raise ValueError(f"HAR entry {args.oauth_refresh_entry} does not contain an OAuth token request body")

    values = dict(parse.parse_qsl(body, keep_blank_values=True))
    refresh_key = form_env_name(args.oauth_refresh_entry, "refresh_token")
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
        query_env_name(args.websocket_entry, "access_token"): access_token,
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
    token_key = query_env_name(args.websocket_entry, "access_token")
    token = env.get(token_key)
    seconds_left = token_seconds_left(token) if token else None

    if args.refresh_chat_token or seconds_left is None or seconds_left <= args.token_refresh_skew:
        if not args.refresh_chat_token:
            remaining = "unknown" if seconds_left is None else f"{seconds_left}s"
            print(f"ChatHub token needs refresh; remaining={remaining}")
        refresh_chat_token(args, entries, env)


def update_gitignore(path: Path) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    wanted = [".env", "*.env.local", "__pycache__/", "*.py[cod]"]
    changed = False
    for item in wanted:
        if item not in existing:
            existing.append(item)
            changed = True
    if changed:
        path.write_text("\n".join(existing).rstrip() + "\n", encoding="utf-8")


def read_exact(sock: ssl.SSLSocket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("WebSocket connection closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class WebSocketClient:
    def __init__(self, url: str, headers: dict[str, str], timeout: float) -> None:
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.sock: ssl.SSLSocket | None = None

    def __enter__(self) -> "WebSocketClient":
        self.connect()
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def connect(self) -> None:
        parts = parse.urlsplit(self.url)
        if parts.scheme != "wss":
            raise ValueError(f"Only wss:// URLs are supported for WebSocket replay: {parts.scheme}")

        host = parts.hostname
        if not host:
            raise ValueError("WebSocket URL is missing a host")
        port = parts.port or 443
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        raw = socket.create_connection((host, port), timeout=self.timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(self.timeout)

        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request_headers = {
            "Host": parts.netloc,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
        }

        for name, value in self.headers.items():
            lower = name.lower()
            if lower in {
                "accept-encoding",
                "connection",
                "host",
                "sec-websocket-extensions",
                "sec-websocket-key",
                "sec-websocket-version",
                "upgrade",
            }:
                continue
            request_headers[name] = value

        lines = [f"GET {path} HTTP/1.1"]
        lines.extend(f"{name}: {value}" for name, value in request_headers.items())
        lines.extend(["", ""])
        self.sock.sendall("\r\n".join(lines).encode("utf-8"))

        response = b""
        while b"\r\n\r\n" not in response:
            response += read_exact(self.sock, 1)
            if len(response) > 65536:
                raise ConnectionError("WebSocket handshake response was too large")

        header_text = response.decode("iso-8859-1", errors="replace")
        status_line = header_text.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            raise ConnectionError(f"WebSocket handshake failed: {status_line}")

        expected_accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if f"sec-websocket-accept: {expected_accept.lower()}" not in header_text.lower():
            raise ConnectionError("WebSocket handshake did not include the expected accept key")

    def send_text(self, text: str) -> None:
        self.send_frame(0x1, text.encode("utf-8"))

    def send_frame(self, opcode: int, payload: bytes) -> None:
        if not self.sock:
            raise ConnectionError("WebSocket is not connected")

        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def recv_text(self) -> str | None:
        while True:
            opcode, payload = self.recv_frame()
            if opcode == 0x1:
                return payload.decode("utf-8", errors="replace")
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self.send_frame(0xA, payload)

    def recv_frame(self) -> tuple[int, bytes]:
        if not self.sock:
            raise ConnectionError("WebSocket is not connected")

        first, second = read_exact(self.sock, 2)
        opcode = first & 0x0F
        if first & 0x70:
            raise ConnectionError("Received a compressed/reserved WebSocket frame")

        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", read_exact(self.sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", read_exact(self.sock, 8))[0]

        mask = read_exact(self.sock, 4) if masked else b""
        payload = read_exact(self.sock, length)
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, payload


def signalr_messages(frame: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for raw in frame.split(SIGNALR_SEPARATOR):
        raw = raw.strip()
        if not raw:
            continue
        messages.append(json.loads(raw))
    return messages


def get_raw_entry(har_path: Path, index: int) -> dict[str, Any]:
    with har_path.open("r", encoding="utf-8") as f:
        har = json.load(f)
    return har["log"]["entries"][index]


def extract_chat_template(raw_entry: dict[str, Any]) -> dict[str, Any]:
    for message in raw_entry.get("_webSocketMessages", []):
        if message.get("type") != "send":
            continue
        for item in signalr_messages(message.get("data", "")):
            if item.get("target") == "chat":
                return item
    raise ValueError("No sent SignalR target='chat' invocation was found in this WebSocket entry")


def make_chat_invocation(template: dict[str, Any], prompt: str, trace_id: str, session_id: str) -> dict[str, Any]:
    invocation = copy.deepcopy(template)
    args = invocation.get("arguments") or []
    if not args:
        raise ValueError("Chat invocation does not contain arguments")

    arg = args[0]
    arg["clientCorrelationId"] = trace_id
    arg["traceId"] = trace_id
    arg["sessionId"] = session_id

    client_info = arg.setdefault("clientInfo", {})
    client_info["clientSessionId"] = session_id

    message = arg.setdefault("message", {})
    message["text"] = prompt
    message["requestId"] = trace_id

    return invocation


def websocket_headers(entry: HarEntry, env: dict[str, str]) -> dict[str, str]:
    headers = build_headers(entry, env)
    for name in list(headers):
        if name.lower().startswith("sec-fetch"):
            headers.pop(name)
    return headers


@dataclass
class ChatResult:
    text: str
    conversation_id: str
    trace_id: str


def resolve_conversation_id(args: argparse.Namespace, entry: HarEntry, env: dict[str, str]) -> str:
    if args.conversation_id:
        conversation_id = args.conversation_id
    elif args.new_conversation:
        conversation_id = str(uuid.uuid4())
    elif args.continue_chat or args.interactive:
        conversation_id = env.get(ACTIVE_CONVERSATION_ENV) or str(uuid.uuid4())
    else:
        conversation_id = get_query_value(entry.url, "ConversationId") or str(uuid.uuid4())

    if args.continue_chat or args.interactive or args.save_conversation:
        set_env_values(args.env, {ACTIVE_CONVERSATION_ENV: conversation_id})
        env[ACTIVE_CONVERSATION_ENV] = conversation_id

    return conversation_id


def send_chat_prompt(
    *,
    har_path: Path,
    websocket_entry: int,
    websocket_timeout: float,
    env: dict[str, str],
    prompt: str,
    conversation_id: str,
    stream: bool = False,
    raw_websocket: bool = False,
) -> ChatResult:
    entries = read_har(har_path)
    entry = entries[websocket_entry]
    raw_entry = get_raw_entry(har_path, websocket_entry)
    template = extract_chat_template(raw_entry)

    trace_id = uuid.uuid4().hex
    session_id = str(uuid.uuid4())

    url = substitute_sensitive_url(entry, env)
    replacements = {
        "chatsessionid": trace_id,
        "XRoutingParameterSessionKey": trace_id,
        "clientrequestid": trace_id,
        "X-SessionId": session_id,
        "ConversationId": conversation_id,
    }
    url = replace_url_query(url, replacements)

    invocation = make_chat_invocation(template, prompt, trace_id, session_id)
    chat_frame = json.dumps(invocation, separators=(",", ":")) + SIGNALR_SEPARATOR
    ping_frame = json.dumps({"type": 6}, separators=(",", ":")) + SIGNALR_SEPARATOR
    handshake_frame = json.dumps({"protocol": "json", "version": 1}, separators=(",", ":")) + SIGNALR_SEPARATOR

    print(f"Connecting to {masked_url(url)}", file=sys.stderr)
    with WebSocketClient(url, websocket_headers(entry, env), websocket_timeout) as ws:
        ws.send_text(handshake_frame)
        handshake_response = ws.recv_text()
        if handshake_response is None:
            raise ConnectionError("WebSocket closed during SignalR handshake")
        ws.send_text(ping_frame)
        ws.send_text(chat_frame)

        chunks: list[str] = []
        final_text: str | None = None
        deadline = time.monotonic() + websocket_timeout

        while time.monotonic() < deadline:
            frame = ws.recv_text()
            if frame is None:
                break
            for message in signalr_messages(frame):
                if message.get("type") == 6:
                    continue
                if raw_websocket:
                    print(json.dumps(message, ensure_ascii=False))

                for arg in message.get("arguments", []):
                    if "writeAtCursor" in arg:
                        chunks.append(arg["writeAtCursor"])
                        if stream:
                            print(arg["writeAtCursor"], end="", flush=True)

                    for item in arg.get("messages", []):
                        if item.get("author") == "bot" and item.get("messageType", "Chat") == "Chat":
                            final_text = item.get("text") or final_text

                    if arg.get("isLastUpdate"):
                        return ChatResult(
                            text=final_text or "".join(chunks),
                            conversation_id=conversation_id,
                            trace_id=trace_id,
                        )

                if message.get("type") == 2:
                    return ChatResult(
                        text=final_text or "".join(chunks),
                        conversation_id=conversation_id,
                        trace_id=trace_id,
                    )

    raise TimeoutError("Timed out before the chat stream completed.")


def run_chat_prompt(
    args: argparse.Namespace,
    entries: list[HarEntry],
    env: dict[str, str],
    prompt: str,
    conversation_id: str,
) -> int:
    try:
        result = send_chat_prompt(
            har_path=args.har,
            websocket_entry=args.websocket_entry,
            websocket_timeout=args.websocket_timeout,
            env=env,
            prompt=prompt,
            conversation_id=conversation_id,
            stream=args.stream,
            raw_websocket=args.raw_websocket,
        )
        if not args.raw_websocket:
            if args.stream:
                print()
            else:
                print(result.text)
        return 0
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def run_chat(args: argparse.Namespace, entries: list[HarEntry], env: dict[str, str]) -> int:
    entry = entries[args.websocket_entry]
    conversation_id = resolve_conversation_id(args, entry, env)
    print(f"ConversationId={conversation_id}")

    if args.interactive:
        print("Interactive chat. Submit an empty line, /exit, or /quit to stop.")
        while True:
            try:
                prompt = input("> ").strip()
            except EOFError:
                print()
                return 0
            if not prompt or prompt.lower() in {"/exit", "/quit"}:
                return 0
            status = run_chat_prompt(args, entries, env, prompt, conversation_id)
            if status:
                return status

    if not args.chat:
        print("--chat is required unless --interactive is used.", file=sys.stderr)
        return 1

    return run_chat_prompt(args, entries, env, args.chat, conversation_id)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--har", type=Path, default=DEFAULT_HAR)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--init-env", action="store_true", help="extract credentials from the HAR into .env")
    parser.add_argument("--chat", help="send this prompt through the captured ChatHub WebSocket flow")
    parser.add_argument("--refresh-chat-token", action="store_true", help="refresh the ChatHub access token in .env and exit unless --chat is also set")
    parser.add_argument("--chat-scope", default=CHAT_SCOPE, help="OAuth scope used when refreshing the ChatHub token")
    parser.add_argument("--oauth-refresh-entry", type=int, default=OAUTH_REFRESH_ENTRY, help="HAR entry index for the refresh-token OAuth request template")
    parser.add_argument("--websocket-entry", type=int, default=CHAT_TOKEN_ENTRY, help="HAR entry index for the ChatHub WebSocket")
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

    env = load_env(args.env)

    if args.refresh_chat_token and not args.chat and not args.interactive:
        refresh_chat_token(args, entries, env)
        return 0

    if args.chat or args.interactive:
        if not args.no_auto_refresh_token:
            ensure_chat_token(args, entries, env)
        return run_chat(args, entries, env)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
