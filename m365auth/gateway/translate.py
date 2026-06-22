"""Translate OpenAI/Anthropic request shapes into a single ChatHub prompt."""

from __future__ import annotations

import json
from typing import Any


def extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return "" if content is None else str(content)


def prompt_from_messages(messages: list[dict[str, Any]]) -> str:
    """Render a list of {role, content} messages into a single prompt string.

    Used for both OpenAI-style and Anthropic-style message arrays; they share
    this shape.
    """
    rendered: list[str] = []
    for message in messages:
        role = message.get("role", "user")
        content = extract_text_content(message.get("content"))
        if content:
            rendered.append(f"{role}: {content}")
    return "\n\n".join(rendered).strip()


def prompt_from_responses_input(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        messages: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                if "role" in item:
                    messages.append(item)
                elif item.get("type") in {"message", "input_text"}:
                    messages.append({"role": item.get("role", "user"), "content": item.get("content") or item.get("text")})
        if messages:
            return prompt_from_messages(messages)
    return json.dumps(value, ensure_ascii=False)
