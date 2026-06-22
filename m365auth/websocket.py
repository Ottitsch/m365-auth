"""A minimal RFC 6455 WebSocket client and SignalR frame helpers."""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import ssl
import struct
import json
from typing import Any
from urllib import parse

from .constants import SIGNALR_SEPARATOR


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
        fragments: list[bytes] = []
        while True:
            fin, opcode, payload = self.recv_frame()
            if opcode == 0x1:
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")
                continue
            if opcode == 0x0:
                if not fragments:
                    raise ConnectionError("Received an unexpected WebSocket continuation frame")
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self.send_frame(0xA, payload)

    def recv_frame(self) -> tuple[bool, int, bytes]:
        if not self.sock:
            raise ConnectionError("WebSocket is not connected")

        first, second = read_exact(self.sock, 2)
        fin = bool(first & 0x80)
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
        return fin, opcode, payload


def signalr_messages(frame: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for raw in frame.split(SIGNALR_SEPARATOR):
        raw = raw.strip()
        if not raw:
            continue
        messages.append(json.loads(raw))
    return messages
