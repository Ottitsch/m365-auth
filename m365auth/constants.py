"""Shared constants for the captured M365 Copilot session tooling."""

from __future__ import annotations

from pathlib import Path

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
CHAT_SCOPE = "https://substrate.office.com/sydney/.default openid profile offline_access"
DEFAULT_TOKEN_REFRESH_SKEW_SECONDS = 300
ACTIVE_CONVERSATION_ENV = "M365_CHAT_CONVERSATION_ID"
