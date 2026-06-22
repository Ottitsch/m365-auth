"""The local HTTP gateway: request routing, streaming, and ChatHub bridging."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace
from typing import Any, Callable

from ..chat import ChatResult, extract_chat_template, send_chat_prompt
from ..env import load_env
from ..har import get_raw_entry, read_har
from ..tokens import ensure_chat_token
from .translate import (
    extract_text_content,
    prompt_from_messages,
    prompt_from_responses_input,
)
from .usage import estimate_tokens, estimated_anthropic_usage, estimated_usage

MODEL_ID = "m365-copilot"


class Gateway:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.entries = read_har(args.har)
        self.websocket_entry = self.entries[args.websocket_entry]
        self.chat_template = extract_chat_template(get_raw_entry(args.har, args.websocket_entry))
        self.default_conversation_id = str(uuid.uuid4())
        self.proxy_api_key = load_env(args.env).get("M365_PROXY_API_KEY", "")

    def env(self) -> dict[str, str]:
        return load_env(self.args.env)

    def ensure_token(self, env: dict[str, str]) -> None:
        if self.args.no_auto_refresh_token:
            return
        token_args = SimpleNamespace(
            env=self.args.env,
            timeout=self.args.timeout,
            websocket_entry=self.args.websocket_entry,
            oauth_refresh_entry=self.args.oauth_refresh_entry,
            chat_scope=self.args.chat_scope,
            refresh_chat_token=False,
            token_refresh_skew=self.args.token_refresh_skew,
        )
        ensure_chat_token(token_args, self.entries, env)

    def resolve_conversation_id(self, conversation_id: str | None = None) -> str:
        if conversation_id:
            return conversation_id
        if self.args.new_conversation_per_request:
            return str(uuid.uuid4())
        return self.default_conversation_id

    def complete(
        self,
        prompt: str,
        conversation_id: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult:
        env = self.env()
        self.ensure_token(env)
        resolved_conversation_id = self.resolve_conversation_id(conversation_id)
        result = send_chat_prompt(
            har_path=self.args.har,
            websocket_entry=self.args.websocket_entry,
            websocket_timeout=self.args.websocket_timeout,
            env=env,
            prompt=prompt,
            conversation_id=resolved_conversation_id,
            stream=False,
            raw_websocket=False,
            entry=self.websocket_entry,
            template=self.chat_template,
            on_delta=on_delta,
        )
        self.note_conversation_usage(resolved_conversation_id, result)
        return result

    def note_conversation_usage(self, conversation_id: str, result: ChatResult) -> None:
        metrics = result.metrics or {}
        throttling = metrics.get("throttling")
        if not isinstance(throttling, dict):
            return
        current = throttling.get("numUserMessagesInConversation")
        maximum = throttling.get("maxNumUserMessagesInConversation")
        if conversation_id == self.default_conversation_id and isinstance(current, int) and isinstance(maximum, int) and current >= maximum:
            self.default_conversation_id = str(uuid.uuid4())


class Handler(BaseHTTPRequestHandler):
    server_version = "M365Gateway/0.1"

    @property
    def gateway(self) -> Gateway:
        return self.server.gateway  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.gateway.args.quiet:
            super().log_message(fmt, *args)

    def do_GET(self) -> None:
        if self.path in {"/health", "/healthz"}:
            self.write_json({"ok": True})
            return
        if self.path == "/v1/models":
            self.write_json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL_ID,
                            "object": "model",
                            "created": 0,
                            "owned_by": "m365",
                            "display_name": "M365 Copilot",
                        }
                    ],
                }
            )
            return
        self.write_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown endpoint")

    def do_POST(self) -> None:
        if not self.authorized():
            return

        try:
            payload = self.read_json()
            if self.path == "/v1/chat/completions":
                self.handle_chat_completions(payload)
            elif self.path == "/v1/responses":
                self.handle_responses(payload)
            elif self.path == "/v1/messages":
                self.handle_anthropic_messages(payload)
            elif self.path == "/v1/messages/count_tokens":
                self.handle_anthropic_count_tokens(payload)
            else:
                self.write_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown endpoint")
        except Exception as exc:
            self.write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "gateway_error", str(exc))

    def authorized(self) -> bool:
        expected = self.gateway.proxy_api_key
        if not expected:
            return True

        auth = self.headers.get("Authorization", "")
        api_key = self.headers.get("X-API-Key", "")
        supplied = ""
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        elif api_key:
            supplied = api_key.strip()

        if supplied == expected:
            return True

        self.write_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "Missing or invalid API key")
        return False

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > self.gateway.args.max_body_bytes:
            raise ValueError("Request body too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def conversation_id_from(self, payload: dict[str, Any]) -> str | None:
        if self.headers.get("X-M365-Conversation-Id"):
            return self.headers.get("X-M365-Conversation-Id")
        if payload.get("conversation_id"):
            return str(payload["conversation_id"])
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("conversation_id"):
            return str(metadata["conversation_id"])
        return None

    def handle_chat_completions(self, payload: dict[str, Any]) -> None:
        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        prompt = prompt_from_messages(messages)
        if not prompt:
            raise ValueError("No text prompt found in messages")

        conversation_id = self.gateway.resolve_conversation_id(self.conversation_id_from(payload))
        if payload.get("stream"):
            self.write_openai_chat_stream(prompt, conversation_id)
            return
        result = self.gateway.complete(prompt, conversation_id)
        text = result.text

        created = int(time.time())
        self.write_json(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": created,
                "model": payload.get("model") or MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": estimated_usage(prompt, text),
                "m365_conversation_id": conversation_id,
                "m365_metrics": result.metrics or {},
            },
            headers={"X-M365-Conversation-Id": conversation_id},
        )

    def handle_responses(self, payload: dict[str, Any]) -> None:
        prompt = prompt_from_responses_input(payload.get("input", ""))
        if not prompt:
            raise ValueError("No text prompt found in input")

        conversation_id = self.gateway.resolve_conversation_id(self.conversation_id_from(payload))
        if payload.get("stream"):
            self.write_responses_stream(prompt, conversation_id)
            return
        result = self.gateway.complete(prompt, conversation_id)
        text = result.text

        response_id = f"resp_{uuid.uuid4().hex}"
        self.write_json(
            {
                "id": response_id,
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": payload.get("model") or MODEL_ID,
                "output": [
                    {
                        "id": f"msg_{uuid.uuid4().hex}",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "output_text": text,
                "usage": estimated_usage(prompt, text),
                "metadata": {"m365_conversation_id": conversation_id, "m365_metrics": result.metrics or {}},
            },
            headers={"X-M365-Conversation-Id": conversation_id},
        )

    def handle_anthropic_messages(self, payload: dict[str, Any]) -> None:
        system = payload.get("system")
        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        prompt = prompt_from_messages(messages)
        if system:
            prompt = f"system: {extract_text_content(system)}\n\n{prompt}".strip()
        if not prompt:
            raise ValueError("No text prompt found in messages")

        conversation_id = self.gateway.resolve_conversation_id(self.conversation_id_from(payload))
        if payload.get("stream"):
            self.write_anthropic_stream(prompt, conversation_id)
            return
        result = self.gateway.complete(prompt, conversation_id)
        text = result.text

        self.write_json(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "model": payload.get("model") or MODEL_ID,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": estimated_anthropic_usage(prompt, text),
                "m365_conversation_id": conversation_id,
                "m365_metrics": result.metrics or {},
            },
            headers={"X-M365-Conversation-Id": conversation_id},
        )

    def handle_anthropic_count_tokens(self, payload: dict[str, Any]) -> None:
        messages = payload.get("messages") or []
        prompt = prompt_from_messages(messages) if isinstance(messages, list) else json.dumps(payload)
        self.write_json({"input_tokens": estimate_tokens(prompt)})

    def write_openai_chat_stream(self, prompt: str, conversation_id: str) -> None:
        created = int(time.time())
        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
        self.start_sse(headers={"X-M365-Conversation-Id": conversation_id})
        self.write_sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        )

        def on_delta(delta: str) -> None:
            self.write_sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": MODEL_ID,
                    "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                }
            )

        self.gateway.complete(prompt, conversation_id, on_delta=on_delta)
        self.write_sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def write_responses_stream(self, prompt: str, conversation_id: str) -> None:
        response_id = f"resp_{uuid.uuid4().hex}"
        self.start_sse(headers={"X-M365-Conversation-Id": conversation_id})
        self.write_sse({"type": "response.created", "response": {"id": response_id, "status": "in_progress", "model": MODEL_ID}})
        result = self.gateway.complete(
            prompt,
            conversation_id,
            on_delta=lambda delta: self.write_sse({"type": "response.output_text.delta", "item_id": "output_0", "output_index": 0, "content_index": 0, "delta": delta}),
        )
        self.write_sse({"type": "response.completed", "response": {"id": response_id, "status": "completed", "output_text": result.text, "metadata": {"m365_metrics": result.metrics or {}}}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def write_anthropic_stream(self, prompt: str, conversation_id: str) -> None:
        message_id = f"msg_{uuid.uuid4().hex}"
        self.start_sse(headers={"X-M365-Conversation-Id": conversation_id})
        self.write_sse_event("message_start", {"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": MODEL_ID, "content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}})
        self.write_sse_event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        result = self.gateway.complete(
            prompt,
            conversation_id,
            on_delta=lambda delta: self.write_sse_event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta}}),
        )
        self.write_sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
        self.write_sse_event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": estimate_tokens(result.text)}, "m365_metrics": result.metrics or {}})
        self.write_sse_event("message_stop", {"type": "message_stop"})

    def start_sse(self, headers: dict[str, str] | None = None) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True

    def write_sse(self, data: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def write_sse_event(self, event: str, data: dict[str, Any]) -> None:
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.write_sse(data)

    def write_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def write_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self.write_json(
            {"error": {"type": code, "message": message}},
            status=status,
        )
