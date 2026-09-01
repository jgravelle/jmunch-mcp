"""End-to-end non-streaming round-trip: fat tool_result → handle-ified →
model emits jmunch_peek → gateway short-circuits locally → app gets final
response. Uses a fake upstream; no network.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


from jmunch_mcp.gateway.config import (
    GatewayConfig,
    Interception,
    UpstreamSpec,
)
from jmunch_mcp.gateway.openai_route import handle_chat_completions
from jmunch_mcp.meta import SavingsTracker
from jmunch_mcp.metrics import MetricsDB
from jmunch_mcp.registry import HandleRegistry
from jmunch_mcp.stats import SessionStats
from jmunch_mcp.verbs import Dispatcher


class FakeUpstream:
    """Records every request and serves a scripted sequence of responses."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.spec = UpstreamSpec(name="fake", kind="openai", base_url="http://fake")

    async def complete(self, request):
        self.calls.append(request)
        if not self.script:
            raise AssertionError("upstream called more times than scripted")
        return self.script.pop(0)

    async def close(self):
        return None


def _make_fake_tmp_metrics(tmp_path, monkeypatch):
    # Isolate the metrics DB so tests don't pollute ~/.jmunch
    monkeypatch.setenv("JMUNCH_METRICS_DB", str(tmp_path / "metrics.db"))
    return MetricsDB()


def _make_tracker(tmp_path):
    return SavingsTracker(path=tmp_path / "_savings.json")


def _config():
    return GatewayConfig(
        listen="127.0.0.1:0",
        default_upstream="fake",
        upstreams=[UpstreamSpec(name="fake", kind="openai", base_url="http://fake")],
        interception=Interception(threshold_tokens=100, inject_tools="auto"),
    )


def _fat_tool_result_text():
    # 200 rows of ~100 bytes each → ~20 KB. Easily over threshold.
    rows = [{"id": i, "name": f"row-{i}", "description": "x" * 80} for i in range(200)]
    return json.dumps(rows)


def test_handleify_on_request_messages(tmp_path, monkeypatch):
    """A fat tool_result in the request is handle-ified before upstream call."""
    registry = HandleRegistry()
    tracker = _make_tracker(tmp_path)
    dispatcher = Dispatcher(registry, SessionStats())
    metrics = _make_fake_tmp_metrics(tmp_path, monkeypatch)
    config = _config()

    # Response with no jmunch tool_calls → single upstream call.
    fake = FakeUpstream([{
        "id": "c1",
        "choices": [{"message": {"role": "assistant", "content": "done"},
                      "finish_reason": "stop", "index": 0}],
    }])

    req = {
        "model": "gpt-4",
        "tools": [{"type": "function", "function": {
            "name": "get_rows", "description": "x", "parameters": {"type": "object"}
        }}],
        "messages": [
            {"role": "user", "content": "list the rows"},
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_rows", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1",
             "content": _fat_tool_result_text()},
        ],
    }

    status, resp = asyncio.run(handle_chat_completions(
        req,
        upstream_override=None,
        config=config,
        upstream_factory=lambda spec: fake,
        registry=registry,
        tracker=tracker,
        dispatcher=dispatcher,
        metrics=metrics,
    ))
    assert status == 200
    assert len(fake.calls) == 1

    sent = fake.calls[0]
    # The tool message content should now be a jMRI envelope (small).
    tool_msgs = [m for m in sent["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    envelope = json.loads(tool_msgs[0]["content"])
    assert "_meta" in envelope
    assert "result" in envelope
    assert envelope["result"]["handle"].startswith("h_")
    # Envelope is much smaller than the raw 20KB blob.
    assert len(tool_msgs[0]["content"]) < 2000
    # A handle was registered.
    assert len(registry) == 1


def test_verb_short_circuit(tmp_path, monkeypatch):
    """Model emits jmunch_peek; gateway resolves locally and re-queries upstream
    with a synthesized tool_result. App never sees the jmunch tool_call."""
    registry = HandleRegistry()
    tracker = _make_tracker(tmp_path)
    dispatcher = Dispatcher(registry, SessionStats())
    metrics = _make_fake_tmp_metrics(tmp_path, monkeypatch)
    config = _config()

    # Turn 1: upstream asks to call jmunch_peek.
    # Turn 2 (after local dispatch): upstream returns a normal assistant reply.
    turn1 = {
        "id": "c1",
        "choices": [{
            "finish_reason": "tool_calls",
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "tc_1",
                    "type": "function",
                    "function": {
                        "name": "jmunch_peek",
                        "arguments": json.dumps({"handle": "PLACEHOLDER", "n": 3}),
                    },
                }],
            },
        }],
    }
    turn2 = {
        "id": "c2",
        "choices": [{
            "finish_reason": "stop",
            "index": 0,
            "message": {"role": "assistant", "content": "here is the peek"},
        }],
    }
    fake = FakeUpstream([turn1, turn2])

    # Prime a handle so jmunch_peek can resolve against it.
    req = {
        "model": "gpt-4",
        "tools": [{"type": "function", "function": {
            "name": "get_rows", "description": "x", "parameters": {"type": "object"}
        }}],
        "messages": [
            {"role": "user", "content": "peek my rows"},
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_rows", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1",
             "content": _fat_tool_result_text()},
        ],
    }

    # Run request-side handle-ify so the handle exists; we need to know the
    # handle id to patch turn1's arguments.
    from jmunch_mcp.gateway.handleify import maybe_handleify
    env_text, handle_id = maybe_handleify(
        req["messages"][-1]["content"],
        registry=registry, tracker=tracker, threshold_tokens=100,
    )
    req["messages"][-1]["content"] = env_text

    # Patch the tool_call to reference the real handle id.
    turn1["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(
        {"handle": handle_id, "n": 3}
    )

    status, resp = asyncio.run(handle_chat_completions(
        req,
        upstream_override=None,
        config=config,
        upstream_factory=lambda spec: fake,
        registry=registry,
        tracker=tracker,
        dispatcher=dispatcher,
        metrics=metrics,
    ))
    assert status == 200
    # Final assistant message reached the app — jmunch_peek never did.
    assert resp["choices"][0]["message"]["content"] == "here is the peek"
    # Two upstream calls: original + follow-up after the local verb dispatch.
    assert len(fake.calls) == 2
    # The second call's messages include a tool_result with the peek output.
    second_msgs = fake.calls[1]["messages"]
    tool_results = [m for m in second_msgs if m.get("role") == "tool"
                    and m.get("tool_call_id") == "tc_1"]
    assert len(tool_results) == 1
    peek_env = json.loads(tool_results[0]["content"])
    assert "_meta" in peek_env and "result" in peek_env
    # The peek result is a list of 3 rows.
    assert isinstance(peek_env["result"], list)
    assert len(peek_env["result"]) == 3


def test_nonstreaming_handler_rejects_stream_flag(tmp_path, monkeypatch):
    """Non-streaming handler must refuse stream=true requests — the server
    dispatches those to `stream_chat_completions` instead."""
    registry = HandleRegistry()
    tracker = _make_tracker(tmp_path)
    dispatcher = Dispatcher(registry, SessionStats())
    metrics = _make_fake_tmp_metrics(tmp_path, monkeypatch)
    fake = FakeUpstream([])

    status, resp = asyncio.run(handle_chat_completions(
        {"model": "gpt-4", "messages": [], "stream": True},
        upstream_override=None,
        config=_config(),
        upstream_factory=lambda spec: fake,
        registry=registry, tracker=tracker,
        dispatcher=dispatcher, metrics=metrics,
    ))
    assert status == 400
    assert len(fake.calls) == 0
