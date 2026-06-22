"""Local stdlib HTTP gateway for the captured M365 Copilot ChatHub flow."""

from __future__ import annotations

from .cli import main, parse_args
from .server import Gateway, Handler, MODEL_ID

__all__ = ["Gateway", "Handler", "MODEL_ID", "main", "parse_args"]
