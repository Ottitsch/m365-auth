"""Emulated tool calling over the text-only ChatHub backend.

The M365 Copilot ChatHub returns plain text and has no native function-calling
API. To expose OpenAI/Anthropic-style tool calling to clients, we inject the
tool definitions plus a strict output protocol into the prompt, then parse any
tool call back out of the model's text reply.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:24]}")

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "type": "tool_use",
            "id": self.id,
            "name": self.name,
            "input": self.arguments,
        }


def render_tool_preamble(tools: list[dict[str, Any]]) -> str:
    """Build the tool-call instructions, framed as a benign formatting request.

    Appended AFTER the conversation (recency helps). Kept cooperative on purpose:
    Copilot refuses "system override" style framing, but happily emits a JSON
    code block when asked to as a normal formatting task.
    """
    lines = [
        "You are connected to an application that can run tools for you and return"
        " their results, so you can use up-to-date data and take actions.",
        "",
        "When one of these tools can help with the request, please request it"
        " instead of answering directly, by replying with a single fenced code"
        " block in exactly this format:",
        "```tool_call",
        '{"name": "<tool name>", "arguments": { ... }}',
        "```",
        "The application will run the tool and send you the result so you can"
        " continue. Reply with only the tool_call block (no other text) when you"
        " use a tool, and one block per tool if you need several.",
        "",
        "Available tools:",
    ]
    for tool in tools:
        schema = json.dumps(tool.get("parameters") or {}, ensure_ascii=False)
        lines.append(f"- {tool['name']}: {tool.get('description', '')}".rstrip())
        lines.append(f"  arguments JSON schema: {schema}")
    lines += [
        "",
        "If none of the tools apply, just answer the user normally.",
    ]
    return "\n".join(lines)


def parse_tool_calls(text: str, valid_names: set[str]) -> list[ToolCall] | None:
    """Extract tool calls from the model's text reply, or None if there are none.

    Fence-agnostic: scans for any balanced JSON object carrying a ``name`` that
    matches a known tool plus an ``arguments`` field, so it tolerates the model
    wrapping the call in markdown, prose, or omitting the fence entirely.
    """
    calls: list[ToolCall] = []
    for raw in _json_candidates(text):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for candidate in obj if isinstance(obj, list) else [obj]:
            if not isinstance(candidate, dict):
                continue
            name = candidate.get("name")
            if name not in valid_names or "arguments" not in candidate:
                continue
            arguments = candidate.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(ToolCall(name=name, arguments=arguments))
    return calls or None


def _json_candidates(text: str) -> list[str]:
    """Yield every top-level balanced ``{...}`` substring, string-literal aware."""
    objects: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start : index + 1])
                start = None
    return objects
