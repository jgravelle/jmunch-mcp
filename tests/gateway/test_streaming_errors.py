"""Upstream failures must reach the client as readable errors with the real status.

Regression cover for #5 (contributed) and the three sibling sites found while
reviewing it. The shared defect: an error dict handed to a *completion* encoder.
`encode_as_sse` walks `choices`, `encode_message_as_sse` walks `content` —
neither exists on an error, so the client gets a well-formed but empty turn and
the upstream's message is gone. A 429 also arrived as a hardcoded 502.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import pytest

from jmunch_mcp.errors import UPSTREAM_ERROR, make_error
from jmunch_mcp.gateway.anthropic_route import stream_messages
from jmunch_mcp.gateway.anthropic_sse import encode_error_as_sse
from jmunch_mcp.gateway.config import GatewayConfig, Interception, UpstreamSpec
from jmunch_mcp.gateway.openai_route import stream_chat_completions
from jmunch_mcp.gateway.upstreams import UpstreamError
from jmunch_mcp.meta import SavingsTracker
from jmunch_mcp.metrics import MetricsDB
from jmunch_mcp.registry import HandleRegistry
from jmunch_mcp.stats import SessionStats
from jmunch_mcp.verbs import Dispatcher


class FailingUpstream:
    """Raises UpstreamError on the first streamed turn."""

    def __init__(self, status: int, body: str, kind: str = "openai"):
        self.status = status
        self.body = body
        self.spec = UpstreamSpec(name="fake", kind=kind, base_url="http://fake")

    async def stream(self, request) -> AsyncIterator[bytes]:
        raise UpstreamError(self.status, self.body)
        yield b""  # pragma: no cover - generator marker

    async def complete(self, request):
        raise UpstreamError(self.status, self.body)

    async def close(self):
        return None


def _config(kind: str = "openai"):
    return GatewayConfig(
        listen="127.0.0.1:0",
        default_upstream="fake",
        upstreams=[UpstreamSpec(name="fake", kind=kind, base_url="http://fake")],
        interception=Interception(threshold_tokens=100, inject_tools="auto"),
    )


def _deps(tmp_path, monkeypatch):
    monkeypatch.setenv("JMUNCH_METRICS_DB", str(tmp_path / "metrics.db"))
    registry = HandleRegistry()
    return {
        "registry": registry,
        "tracker": SavingsTracker(path=tmp_path / "_savings.json"),
        "dispatcher": Dispatcher(registry, SessionStats()),
        "metrics": MetricsDB(),
    }


def _joined(chunks) -> str:
    return b"".join(chunks).decode("utf-8")


@pytest.mark.parametrize("status", [429, 503])
def test_openai_stream_propagates_upstream_status_and_message(tmp_path, monkeypatch, status):
    got_status, chunks = asyncio.run(stream_chat_completions(
        {"model": "gpt-4", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        upstream_override=None,
        config=_config(),
        upstream_factory=lambda spec: FailingUpstream(status, "rate limit exceeded"),
        **_deps(tmp_path, monkeypatch),
    ))

    assert got_status == status, "the upstream status must survive, not collapse to 502"
    text = _joined(chunks)
    assert "rate limit exceeded" in text, "upstream detail must reach the client"
    assert "[DONE]" in text, "stream must still terminate"
    # The defect signature: a well-formed chunk carrying nothing.
    assert '"choices": []' not in text and '"choices":[]' not in text


def test_anthropic_stream_emits_a_real_error_event(tmp_path, monkeypatch):
    got_status, chunks = asyncio.run(stream_messages(
        {"model": "claude-opus-5", "stream": True, "max_tokens": 16,
         "messages": [{"role": "user", "content": "hi"}]},
        upstream_override=None,
        config=_config(kind="anthropic"),
        upstream_factory=lambda spec: FailingUpstream(429, "overloaded", kind="anthropic"),
        **_deps(tmp_path, monkeypatch),
    ))

    assert got_status == 429
    text = _joined(chunks)
    assert "event: error" in text, "must be an error event, not an assistant turn"
    # An error dressed as a message reads to an SDK as a successful reply.
    assert "message_start" not in text
    assert "stop_reason" not in text


def test_bad_upstream_kind_is_not_an_empty_chunk(tmp_path, monkeypatch):
    """The 400 path had the same defect as the two 502s the PR fixed."""
    got_status, chunks = asyncio.run(stream_chat_completions(
        {"model": "gpt-4", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        upstream_override=None,
        config=_config(kind="anthropic"),  # mismatched: openai route, anthropic upstream
        upstream_factory=lambda spec: FailingUpstream(500, "unused", kind="anthropic"),
        **_deps(tmp_path, monkeypatch),
    ))

    assert got_status == 400
    text = _joined(chunks)
    assert "jmunch_bad_upstream" in text
    assert "[DONE]" in text


def test_encode_error_as_sse_shape():
    out = encode_error_as_sse(make_error(UPSTREAM_ERROR, "boom", status=503))
    assert len(out) == 1
    raw = out[0].decode("utf-8")
    assert raw.startswith("event: error\ndata: ")
    assert raw.endswith("\n\n")
    payload = json.loads(raw.split("data: ", 1)[1])
    assert payload["type"] == "error"
    assert payload["error"]["message"] == "boom"
    assert payload["error"]["detail"]["status"] == 503
