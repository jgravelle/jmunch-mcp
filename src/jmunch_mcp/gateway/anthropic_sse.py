"""Anthropic-shaped SSE helpers.

Anthropic's streaming protocol is event-typed (message_start, content_block_*,
message_delta, message_stop) rather than OpenAI's uniform data chunks. For
buffer-then-replay we:
  1. Parse upstream SSE events into a logical sequence.
  2. Assemble a non-streaming `Message` shape for the verb-loop decision.
  3. After verb resolution, re-emit the final response as a fresh Anthropic
     event stream so the client sees proper message_* events.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator


async def parse_anthropic_sse(chunks: AsyncIterator[bytes]) -> list[dict[str, Any]]:
    """Decode the Anthropic SSE byte stream into a list of events.

    Each returned dict is the parsed `data:` payload (event name is already
    encoded in the payload's `type` field, so we don't track `event:` lines).
    """
    events: list[dict[str, Any]] = []
    buffer = b""
    async for piece in chunks:
        buffer += piece
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].lstrip()
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
    return events


def assemble_message_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold Anthropic SSE events into a non-streaming `Message` shape.

    Handled events:
      - message_start.message → base fields (id, model, role, usage, ...)
      - content_block_start.content_block → seed a slot at `index`
      - content_block_delta.delta.text_delta.text → append to a text block
      - content_block_delta.delta.input_json_delta.partial_json → append
        to a tool_use's input (assembled as JSON string, parsed at end)
      - message_delta.delta → stop_reason, stop_sequence
    """
    msg: dict[str, Any] = {
        "id": "", "type": "message", "role": "assistant",
        "content": [], "model": "", "stop_reason": None,
    }
    blocks_by_idx: dict[int, dict[str, Any]] = {}
    tool_use_input_json: dict[int, str] = {}

    for ev in events:
        t = ev.get("type")
        if t == "message_start":
            base = ev.get("message") or {}
            for k in ("id", "model", "role", "usage"):
                if k in base:
                    msg[k] = base[k]
        elif t == "content_block_start":
            idx = ev.get("index", 0)
            block = dict(ev.get("content_block") or {})
            blocks_by_idx[idx] = block
            if block.get("type") == "tool_use":
                tool_use_input_json.setdefault(idx, "")
        elif t == "content_block_delta":
            idx = ev.get("index", 0)
            delta = ev.get("delta") or {}
            dt = delta.get("type")
            block = blocks_by_idx.setdefault(idx, {"type": "text", "text": ""})
            if dt == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif dt == "input_json_delta":
                tool_use_input_json[idx] = (
                    tool_use_input_json.get(idx, "") + delta.get("partial_json", "")
                )
        elif t == "message_delta":
            d = ev.get("delta") or {}
            if "stop_reason" in d:
                msg["stop_reason"] = d["stop_reason"]
            if "stop_sequence" in d:
                msg["stop_sequence"] = d["stop_sequence"]

    # Finalize tool_use input fields.
    for idx, block in blocks_by_idx.items():
        if block.get("type") == "tool_use":
            raw = tool_use_input_json.get(idx, "")
            try:
                block["input"] = json.loads(raw) if raw else block.get("input", {})
            except json.JSONDecodeError:
                block["input"] = block.get("input", {})

    msg["content"] = [blocks_by_idx[i] for i in sorted(blocks_by_idx)]
    return msg


def encode_error_as_sse(err: dict[str, Any]) -> list[bytes]:
    """Emit an upstream failure as an Anthropic `error` event.

    The alternative — dressing the error up as an assistant message whose text
    happens to be JSON — reaches the client as a successful turn, so an SDK
    reports a model reply rather than a failure. Anthropic's stream spec has a
    real error event; use it.
    """
    payload = json.dumps({"type": "error", "error": err}, default=str)
    return [b"event: error\ndata: " + payload.encode("utf-8") + b"\n\n"]


def encode_message_as_sse(message: dict[str, Any]) -> list[bytes]:
    """Re-emit a non-streaming Message as Anthropic-shaped SSE events.

    Emits (per Anthropic spec):
      message_start → [content_block_start, content_block_delta*, content_block_stop]*
      → message_delta → message_stop
    """
    out: list[bytes] = []

    def ev(event: str, data: dict[str, Any]) -> None:
        out.append(
            ("event: " + event + "\n").encode("utf-8")
            + b"data: " + json.dumps(data, default=str).encode("utf-8") + b"\n\n"
        )

    base = {
        "id": message.get("id", ""),
        "type": "message",
        "role": message.get("role", "assistant"),
        "model": message.get("model", ""),
        "content": [],
        "stop_reason": None,
        "usage": message.get("usage", {}),
    }
    ev("message_start", {"type": "message_start", "message": base})

    for idx, block in enumerate(message.get("content") or []):
        btype = block.get("type")
        if btype == "text":
            ev("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            text = block.get("text", "")
            if text:
                ev("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "text_delta", "text": text},
                })
            ev("content_block_stop", {"type": "content_block_stop", "index": idx})
        elif btype == "tool_use":
            ev("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": {},
                },
            })
            # Emit the entire input JSON as a single partial_json delta.
            input_json = json.dumps(block.get("input") or {}, default=str)
            ev("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": input_json},
            })
            ev("content_block_stop", {"type": "content_block_stop", "index": idx})
        else:
            # Unknown block types pass through as-is.
            ev("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": block,
            })
            ev("content_block_stop", {"type": "content_block_stop", "index": idx})

    ev("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": message.get("stop_reason") or "end_turn"},
        "usage": message.get("usage", {}),
    })
    ev("message_stop", {"type": "message_stop"})
    return out
