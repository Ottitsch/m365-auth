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
                if item.get("type") in {"tool_use", "tool_result"}:
                    continue  # handled separately by _render_message
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _render_message(message: dict[str, Any]) -> str | None:
    """Render one message, including tool calls/results, into prompt text."""
    role = message.get("role", "user")
    content = message.get("content")

    # OpenAI tool-result message.
    if role == "tool":
        text = extract_text_content(content)
        name = message.get("name") or message.get("tool_call_id") or "tool"
        return f"tool result ({name}): {text}" if text else None

    parts: list[str] = []
    text = extract_text_content(content)
    if text:
        parts.append(text)

    # Anthropic tool_use / tool_result content blocks.
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                args = json.dumps(item.get("input") or {}, ensure_ascii=False)
                parts.append(f"[called tool {item.get('name')} with arguments {args}]")
            elif item.get("type") == "tool_result":
                result = extract_text_content(item.get("content"))
                parts.append(f"[tool result: {result}]")

    # OpenAI assistant tool_calls.
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        parts.append(f"[called tool {fn.get('name')} with arguments {fn.get('arguments')}]")

    if not parts:
        return None
    return f"{role}: " + " ".join(parts)


def prompt_from_messages(messages: list[dict[str, Any]]) -> str:
    """Render a list of messages into a single prompt string.

    Used for both OpenAI-style and Anthropic-style message arrays, including
    tool calls and tool results.
    """
    rendered = [line for message in messages if (line := _render_message(message))]
    return "\n\n".join(rendered).strip()


def tools_from_request(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize OpenAI/Anthropic tool definitions into {name, description, parameters}."""
    tools: list[dict[str, Any]] = []

    raw = payload.get("tools")
    if isinstance(raw, list):
        for tool in raw:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                fn = tool["function"]  # OpenAI
                tools.append(
                    {
                        "name": fn.get("name"),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters") or {},
                    }
                )
            elif tool.get("name"):
                tools.append(  # Anthropic
                    {
                        "name": tool.get("name"),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema") or tool.get("parameters") or {},
                    }
                )

    legacy = payload.get("functions")  # deprecated OpenAI functions
    if isinstance(legacy, list):
        for fn in legacy:
            if isinstance(fn, dict) and fn.get("name"):
                tools.append(
                    {
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters") or {},
                    }
                )

    return [tool for tool in tools if tool.get("name")]


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
