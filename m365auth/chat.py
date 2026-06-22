"""ChatHub invocation building and the streaming send/receive flow."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .constants import ACTIVE_CONVERSATION_ENV, SIGNALR_SEPARATOR
from .env import set_env_values
from .har import HarEntry, get_raw_entry, read_har
from .headers import get_query_value, replace_url_query, substitute_sensitive_url, websocket_headers
from .websocket import WebSocketClient, signalr_messages


@dataclass
class ChatResult:
    text: str
    conversation_id: str
    trace_id: str
    metrics: dict[str, Any] | None = None


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


def chat_message_text(item: dict[str, Any]) -> str | None:
    if isinstance(item.get("text"), str):
        return item["text"]
    for card in item.get("adaptiveCards", []):
        for body in card.get("body", []):
            if isinstance(body, dict) and isinstance(body.get("text"), str):
                return body["text"]
    return None


def make_chat_url(entry: HarEntry, env: dict[str, str], trace_id: str, session_id: str, conversation_id: str) -> str:
    url = substitute_sensitive_url(entry, env)
    return replace_url_query(
        url,
        {
            "chatsessionid": trace_id,
            "XRoutingParameterSessionKey": trace_id,
            "clientrequestid": trace_id,
            "X-SessionId": session_id,
            "ConversationId": conversation_id,
        },
    )


def chat_frames(template: dict[str, Any], prompt: str, trace_id: str, session_id: str) -> tuple[str, str, str]:
    invocation = make_chat_invocation(template, prompt, trace_id, session_id)
    chat_frame = json.dumps(invocation, separators=(",", ":")) + SIGNALR_SEPARATOR
    ping_frame = json.dumps({"type": 6}, separators=(",", ":")) + SIGNALR_SEPARATOR
    handshake_frame = json.dumps({"protocol": "json", "version": 1}, separators=(",", ":")) + SIGNALR_SEPARATOR
    return handshake_frame, ping_frame, chat_frame


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
    entry: HarEntry | None = None,
    template: dict[str, Any] | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> ChatResult:
    if entry is None:
        entry = read_har(har_path)[websocket_entry]
    if template is None:
        template = extract_chat_template(get_raw_entry(har_path, websocket_entry))

    def print_delta(delta: str) -> None:
        if stream:
            print(delta, end="", flush=True)
        if on_delta:
            on_delta(delta)

    delta_callback = print_delta if stream or on_delta else None
    trace_id = uuid.uuid4().hex
    session_id = str(uuid.uuid4())
    url = make_chat_url(entry, env, trace_id, session_id, conversation_id)
    handshake_frame, ping_frame, chat_frame = chat_frames(template, prompt, trace_id, session_id)
    metrics: dict[str, Any] = {}

    chunks: list[str] = []
    final_text: str | None = None
    emitted_text = ""
    pending_cursor_deltas: list[str] = []
    first_frame_ms: float | None = None
    first_delta_ms: float | None = None

    def emit_delta(delta: str, now: float, send_start: float) -> None:
        nonlocal emitted_text, first_delta_ms
        emitted_text += delta
        if first_delta_ms is None:
            first_delta_ms = round((now - send_start) * 1000, 1)
        if delta_callback:
            delta_callback(delta)

    def emit_cursor_delta(delta: str, now: float, send_start: float) -> None:
        if delta_callback and not emitted_text and (pending_cursor_deltas or delta.startswith(" ")):
            pending_cursor_deltas.append(delta)
            return
        emit_delta(delta, now, send_start)

    def emit_text_snapshot(text: str, now: float, send_start: float) -> None:
        pending_text = "".join(pending_cursor_deltas)
        if delta_callback and pending_text and not emitted_text and text.endswith(pending_text):
            pending_cursor_deltas.clear()
            emit_delta(text, now, send_start)
            return
        pending_cursor_deltas.clear()
        if delta_callback and text.startswith(emitted_text) and len(text) > len(emitted_text):
            emit_delta(text[len(emitted_text):], now, send_start)

    start = time.perf_counter()
    with WebSocketClient(url, websocket_headers(entry, env), websocket_timeout) as ws:
        metrics["ws_connect_ms"] = round((time.perf_counter() - start) * 1000, 1)

        start = time.perf_counter()
        ws.send_text(handshake_frame)
        handshake_response = ws.recv_text()
        if handshake_response is None:
            raise ConnectionError("WebSocket closed during SignalR handshake")
        metrics["signalr_handshake_ms"] = round((time.perf_counter() - start) * 1000, 1)

        send_start = time.perf_counter()
        ws.send_text(ping_frame)
        ws.send_text(chat_frame)
        metrics["send_chat_frame_ms"] = round((time.perf_counter() - send_start) * 1000, 1)
        deadline = time.monotonic() + websocket_timeout

        while time.monotonic() < deadline:
            frame = ws.recv_text()
            now = time.perf_counter()
            if frame is None:
                raise ConnectionError("WebSocket closed before the chat stream completed")
            for message in signalr_messages(frame):
                if message.get("type") == 6:
                    continue
                if raw_websocket:
                    print(json.dumps(message, ensure_ascii=False))

                for arg in message.get("arguments", []):
                    request_id = arg.get("requestId")
                    if request_id and request_id != trace_id:
                        continue
                    if first_frame_ms is None:
                        first_frame_ms = round((now - send_start) * 1000, 1)
                    throttling = arg.get("throttling")
                    if isinstance(throttling, dict):
                        metrics["throttling"] = throttling
                    if "writeAtCursor" in arg:
                        delta = arg["writeAtCursor"]
                        chunks.append(delta)
                        emit_cursor_delta(delta, now, send_start)

                    for item in arg.get("messages", []):
                        if item.get("author") == "bot" and item.get("messageType", "Chat") not in {"EscapeHatch"}:
                            text = chat_message_text(item)
                            if text:
                                final_text = text
                                emit_text_snapshot(text, now, send_start)

                    if arg.get("isLastUpdate"):
                        metrics["first_frame_ms"] = first_frame_ms
                        metrics["first_delta_ms"] = first_delta_ms
                        metrics["remote_total_after_send_ms"] = round((now - send_start) * 1000, 1)
                        return ChatResult(
                            text=final_text or "".join(chunks),
                            conversation_id=conversation_id,
                            trace_id=trace_id,
                            metrics=metrics,
                        )

                if message.get("type") == 2:
                    item = message.get("item")
                    if isinstance(item, dict):
                        throttling = item.get("throttling")
                        if isinstance(throttling, dict):
                            metrics["throttling"] = throttling
                        for chat_message in item.get("messages", []):
                            if (
                                isinstance(chat_message, dict)
                                and chat_message.get("author") == "bot"
                                and chat_message.get("messageType", "Chat") not in {"EscapeHatch", "ReferencesListComplete"}
                            ):
                                text = chat_message_text(chat_message)
                                if text:
                                    final_text = text
                                    emit_text_snapshot(text, now, send_start)
                    if not chunks and not final_text:
                        continue
                    metrics["first_frame_ms"] = first_frame_ms
                    metrics["first_delta_ms"] = first_delta_ms
                    metrics["remote_total_after_send_ms"] = round((now - send_start) * 1000, 1)
                    return ChatResult(
                        text=final_text or "".join(chunks),
                        conversation_id=conversation_id,
                        trace_id=trace_id,
                        metrics=metrics,
                    )

    raise TimeoutError("Timed out before the chat stream completed.")


def run_chat_prompt(
    args: argparse.Namespace,
    entries: list[HarEntry],
    env: dict[str, str],
    prompt: str,
    conversation_id: str,
    template: dict[str, Any] | None = None,
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
            entry=entries[args.websocket_entry],
            template=template,
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
    template = extract_chat_template(get_raw_entry(args.har, args.websocket_entry))
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
            status = run_chat_prompt(args, entries, env, prompt, conversation_id, template)
            if status:
                return status

    if not args.chat:
        print("--chat is required unless --interactive is used.", file=sys.stderr)
        return 1

    return run_chat_prompt(args, entries, env, args.chat, conversation_id, template)
