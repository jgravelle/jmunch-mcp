"""End-to-end tests for the Anthropic /v1/messages gateway route:
 * fat tool_result content blocks get handle-ified on the request path
 * jmunch tool_use blocks short-circuit with a synthesized follow-up
 * streaming buffer-then-replay emits Anthropic-shaped SSE events
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator


from jmunch_mcp.gateway.anthropic_route import handle_messages, stream_messages
from jmunch_mcp.gateway.anthropic_sse import (
    assemble_message_from_events,
    encode_message_as_sse,
    parse_anthropic_sse,
)
from jmunch_mcp.gateway.config import GatewayConfig, Interception, UpstreamSpec
from jmunch_mcp.gateway.handleify import maybe_handleify
from jmunch_mcp.meta import SavingsTracker
from jmunch_mcp.metrics import MetricsDB
from jmunch_mcp.registry import HandleRegistry
from jmunch_mcp.stats import SessionStats
from jmunch_mcp.verbs import Dispatcher


class FakeAnthropicUpstream:
    def __init__(self, *, complete_script=None, sse_script=None):
        self.complete_script = list(complete_script or [])
        self.sse_script = list(sse_script or [])
        self.complete_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.spec = UpstreamSpec(name="fake-anth", kind="anthropic", base_url="http://fake")

    async def complete(self, req):
        self.complete_calls.append(req)
        if not self.complete_script:
            raise AssertionError("complete() called more times than scripted")
        return self.complete_script.pop(0)

    async def stream(self, req) -> AsyncIterator[bytes]:
        self.stream_calls.append(req)
        if not self.sse_script:
            raise AssertionError("stream() called more times than scripted")
        for piece in self.sse_script.pop(0):
            yield piece

    async def close(self):
        return None


def _config():
    return GatewayConfig(
        listen="127.0.0.1:0",
        default_upstream="fake-anth",
        upstreams=[UpstreamSpec(name="fake-anth", kind="anthropic", base_url="http://fake")],
        interception=Interception(threshold_tokens=100, inject_tools="auto"),
    )


def _make_tmp_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("JMUNCH_METRICS_DB", str(tmp_path / "metrics.db"))
    return MetricsDB()


def _tracker(tmp_path):
    return SavingsTracker(path=tmp_path / "_savings.json")


def _fat():
    return json.dumps([{"id": i, "name": f"n-{i}", "note": "z" * 80} for i in range(200)])


def test_request_tool_result_handleified(tmp_path, monkeypatch):
    registry = HandleRegistry()
    tracker = _tracker(tmp_path)
    dispatcher = Dispatcher(registry, SessionStats())
    metrics = _make_tmp_metrics(tmp_path, monkeypatch)

    final = {
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
        "stop_reason": "end_turn", "model": "claude-opus",
    }
    fake = FakeAnthropicUpstream(complete_script=[final])

    req = {
        "model": "claude-opus",
        "max_tokens": 256,
        "tools": [{"name": "get_rows", "description": "x",
                   "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "list rows"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "get_rows", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": _fat()}]},
        ],
    }

    status, resp = asyncio.run(handle_messages(
        req, upstream_override=None, config=_config(),
        upstream_factory=lambda s: fake,
        registry=registry, tracker=tracker,
        dispatcher=dispatcher, metrics=metrics,
    ))
    assert status == 200
    assert len(fake.complete_calls) == 1
    sent = fake.complete_calls[0]
    # Find the tool_result block in the forwarded messages.
    user_blocks = next(m["content"] for m in sent["messages"] if m["role"] == "user"
                       and isinstance(m.get("content"), list))
    tr = next(b for b in user_blocks if b.get("type") == "tool_result")
    assert isinstance(tr["content"], str)  # was a string; preserved
    env = json.loads(tr["content"])
    assert "_meta" in env and env["result"]["handle"].startswith("h_")
    assert len(registry) == 1


def test_tool_use_short_circuit(tmp_path, monkeypatch):
    registry = HandleRegistry()
    tracker = _tracker(tmp_path)
    dispatcher = Dispatcher(registry, SessionStats())
    metrics = _make_tmp_metrics(tmp_path, monkeypatch)

    env_text, handle_id = maybe_handleify(
        _fat(), registry=registry, tracker=tracker, threshold_tokens=100,
    )
    assert handle_id

    turn1 = {
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me peek."},
            {"type": "tool_use", "id": "tu_2", "name": "jmunch_peek",
             "input": {"handle": handle_id, "n": 2}},
        ],
        "stop_reason": "tool_use", "model": "claude-opus",
    }
    turn2 = {
        "id": "msg_2", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "Here are 2 rows."}],
        "stop_reason": "end_turn", "model": "claude-opus",
    }
    fake = FakeAnthropicUpstream(complete_script=[turn1, turn2])

    req = {
        "model": "claude-opus", "max_tokens": 256,
        "tools": [{"name": "get_rows", "description": "x",
                   "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "peek it"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "get_rows", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": env_text}]},
        ],
    }

    status, resp = asyncio.run(handle_messages(
        req, upstream_override=None, config=_config(),
        upstream_factory=lambda s: fake,
        registry=registry, tracker=tracker,
        dispatcher=dispatcher, metrics=metrics,
    ))
    assert status == 200
    assert resp["content"][0]["text"] == "Here are 2 rows."
    # Two upstream calls: original + follow-up after jmunch_peek resolution.
    assert len(fake.complete_calls) == 2
    followup = fake.complete_calls[1]
    # The follow-up messages should include the assistant tool_use turn and
    # a user message with a tool_result for tu_2 containing the peek envelope.
    followup_user_tr = None
    for m in followup["messages"]:
        if m["role"] == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result" \
                        and b.get("tool_use_id") == "tu_2":
                    followup_user_tr = b
                    break
    assert followup_user_tr is not None
    peek_env = json.loads(followup_user_tr["content"])
    assert isinstance(peek_env["result"], list)
    assert len(peek_env["result"]) == 2


# ---------------------------------------------------------------------------
# Anthropic SSE helpers — pure unit tests
# ---------------------------------------------------------------------------

def test_anthropic_sse_roundtrip():
    message = {
        "id": "msg_x", "type": "message", "role": "assistant",
        "content": [
            {"type": "text", "text": "Hello world"},
            {"type": "tool_use", "id": "tu_z", "name": "jmunch_peek",
             "input": {"handle": "h_abc", "n": 3}},
        ],
        "model": "claude-opus", "stop_reason": "tool_use",
    }
    chunks = encode_message_as_sse(message)

    async def src():
        for c in chunks:
            yield c
    events = asyncio.run(parse_anthropic_sse(src()))
    recovered = assemble_message_from_events(events)
    assert recovered["content"][0]["text"] == "Hello world"
    tu = recovered["content"][1]
    assert tu["type"] == "tool_use" and tu["name"] == "jmunch_peek"
    assert tu["input"] == {"handle": "h_abc", "n": 3}
    assert recovered["stop_reason"] == "tool_use"


def test_stream_messages_verb_short_circuit(tmp_path, monkeypatch):
    registry = HandleRegistry()
    tracker = _tracker(tmp_path)
    dispatcher = Dispatcher(registry, SessionStats())
    metrics = _make_tmp_metrics(tmp_path, monkeypatch)

    env_text, handle_id = maybe_handleify(
        _fat(), registry=registry, tracker=tracker, threshold_tokens=100,
    )

    # First upstream turn: streaming; model emits a jmunch_peek tool_use.
    turn1_message = {
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "tu_2", "name": "jmunch_peek",
             "input": {"handle": handle_id, "n": 2}},
        ],
        "stop_reason": "tool_use", "model": "claude-opus",
    }
    sse_turn1 = encode_message_as_sse(turn1_message)
    # Follow-up is non-streaming complete().
    turn2 = {
        "id": "msg_2", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "done."}],
        "stop_reason": "end_turn", "model": "claude-opus",
    }
    fake = FakeAnthropicUpstream(sse_script=[sse_turn1], complete_script=[turn2])

    req = {
        "model": "claude-opus", "max_tokens": 256, "stream": True,
        "tools": [{"name": "get_rows", "description": "x",
                   "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "peek"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "get_rows", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": env_text}]},
        ],
    }

    status, chunks = asyncio.run(stream_messages(
        req, upstream_override=None, config=_config(),
        upstream_factory=lambda s: fake,
        registry=registry, tracker=tracker,
        dispatcher=dispatcher, metrics=metrics,
    ))
    assert status == 200
    assert len(fake.stream_calls) == 1
    assert len(fake.complete_calls) == 1

    async def src():
        for c in chunks:
            yield c
    events = asyncio.run(parse_anthropic_sse(src()))
    final = assemble_message_from_events(events)
    assert final["content"][0]["text"] == "done."
    assert final["stop_reason"] == "end_turn"
