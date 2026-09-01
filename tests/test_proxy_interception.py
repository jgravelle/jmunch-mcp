"""Unit-level interception tests.

Avoid the subprocess/asyncio boot path — test the pure logic of the
three interception branches plus a full ingest → dispatch round-trip.
"""
from __future__ import annotations

import json
from pathlib import Path


from jmunch_mcp.config import Config, UpstreamConfig
from jmunch_mcp.meta import SavingsTracker
from jmunch_mcp.proxy import Proxy
from jmunch_mcp.verbs import TOOL_SCHEMAS


def _proxy(tmp_path: Path, threshold: int = 2000) -> Proxy:
    p = Proxy(
        Config(
            upstream=UpstreamConfig(command="noop"),
            threshold_tokens=threshold,
        )
    )
    # Redirect savings persistence into the tmp dir so tests don't touch ~
    p.tracker = SavingsTracker(path=tmp_path / "_savings.json")
    return p


def _tools_list_response(names: list[str]) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": [{"name": n, "inputSchema": {}} for n in names]},
    }


def _tool_call_response(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


def test_inject_tools_adds_jmunch_schemas(tmp_path):
    p = _proxy(tmp_path)
    p._pending[1] = "tools/list"
    out = p._maybe_rewrite_response(_tools_list_response(["upstream.search"]))
    names = [t["name"] for t in out["result"]["tools"]]
    assert "upstream.search" in names
    for schema in TOOL_SCHEMAS:
        assert schema["name"] in names


def test_inject_does_not_duplicate_existing_jmunch(tmp_path):
    p = _proxy(tmp_path)
    p._pending[1] = "tools/list"
    out = p._maybe_rewrite_response(_tools_list_response(["jmunch_peek"]))
    names = [t["name"] for t in out["result"]["tools"]]
    assert names.count("jmunch_peek") == 1


def test_small_response_passes_through(tmp_path):
    p = _proxy(tmp_path, threshold=2000)
    p._pending[2] = "tools/call"
    small_text = json.dumps([{"x": 1}])
    msg = _tool_call_response(small_text)
    out = p._maybe_rewrite_response(msg)
    assert out is msg


def test_large_tabular_becomes_handle(tmp_path):
    p = _proxy(tmp_path, threshold=100)  # low threshold to trigger
    p._pending[2] = "tools/call"
    rows = [
        {"id": i, "state": "open" if i % 2 else "closed", "title": f"issue {i}"}
        for i in range(50)
    ]
    text = json.dumps(rows)
    assert len(text) > 100 * 4

    msg = _tool_call_response(text)
    out = p._maybe_rewrite_response(msg)

    assert out is not msg
    inner = json.loads(out["result"]["content"][0]["text"])
    assert "_meta" in inner
    assert inner["_meta"]["retrieval_engine"] == "jmunch"
    assert inner["_meta"]["tokens_saved"] > 0
    assert inner["result"]["kind"] == "tabular"
    assert inner["result"]["handle"].startswith("h_")
    assert inner["result"]["summary"]["row_count"] == 50
    assert "_hint" in inner["result"]


def test_handle_then_peek_roundtrip(tmp_path):
    p = _proxy(tmp_path, threshold=100)
    p._pending[2] = "tools/call"
    rows = [{"id": i, "name": f"n{i}"} for i in range(20)]
    msg = _tool_call_response(json.dumps(rows))
    out = p._maybe_rewrite_response(msg)
    handle_id = json.loads(out["result"]["content"][0]["text"])["result"]["handle"]

    peeked = p.dispatcher.dispatch("jmunch_peek", {"handle": handle_id, "n": 3})
    assert isinstance(peeked, list)
    assert [r["id"] for r in peeked] == [0, 1, 2]


def test_handle_aggregate_count_by_group(tmp_path):
    p = _proxy(tmp_path, threshold=100)
    p._pending[2] = "tools/call"
    rows = [
        {"id": i, "state": "open" if i % 3 else "closed"} for i in range(30)
    ]
    msg = _tool_call_response(json.dumps(rows))
    out = p._maybe_rewrite_response(msg)
    handle_id = json.loads(out["result"]["content"][0]["text"])["result"]["handle"]

    grouped = p.dispatcher.dispatch(
        "jmunch_aggregate", {"handle": handle_id, "op": "count", "group_by": "state"}
    )
    as_map = {g["group"]: g["value"] for g in grouped}
    # 10 i's where i%3==0 → closed; 20 others → open
    assert as_map == {"closed": 10, "open": 20}


def test_large_json_tree_is_handle_ified(tmp_path):
    p = _proxy(tmp_path, threshold=100)
    p._pending[2] = "tools/call"
    payload = {"nested": {"deeply": ["a"] * 1000}}  # JSON tree, not tabular
    msg = _tool_call_response(json.dumps(payload))
    out = p._maybe_rewrite_response(msg)
    assert out is not msg
    inner = json.loads(out["result"]["content"][0]["text"])
    assert inner["result"]["kind"] == "json"
    assert inner["result"]["handle"].startswith("h_")
    # Verb dispatch on the fresh handle should work
    handle_id = inner["result"]["handle"]
    sliced = p.dispatcher.dispatch(
        "jmunch_slice", {"handle": handle_id, "selector": "$.nested.deeply[0]"}
    )
    assert sliced["count"] == 1
    assert sliced["matches"][0]["value"] == "a"


def test_large_non_json_text_becomes_text_handle(tmp_path):
    p = _proxy(tmp_path, threshold=100)
    p._pending[2] = "tools/call"
    # Non-JSON text blob — decodes as a bare string via json.loads, triggering
    # the TEXT classification branch.
    blob = "\n".join(f"line {i}: neural networks and machine learning" for i in range(200))
    msg = _tool_call_response(blob)
    out = p._maybe_rewrite_response(msg)
    assert out is not msg
    inner = json.loads(out["result"]["content"][0]["text"])
    assert inner["result"]["kind"] == "text"
    handle_id = inner["result"]["handle"]
    summary = p.dispatcher.dispatch("jmunch_summarize", {"handle": handle_id})
    assert summary["line_count"] == 200
    assert any(kw["token"] == "neural" for kw in summary["keywords"])


def test_summarize_on_non_text_handle_returns_not_applicable(tmp_path):
    p = _proxy(tmp_path, threshold=100)
    p._pending[2] = "tools/call"
    payload = {"nested": {"deeply": ["a"] * 1000}}
    msg = _tool_call_response(json.dumps(payload))
    p._maybe_rewrite_response(msg)
    handle_id = next(iter(p.registry.list())).id
    err = p.dispatcher.dispatch("jmunch_summarize", {"handle": handle_id})
    assert err["code"] == "NOT_APPLICABLE"


def test_short_json_tree_below_threshold_passes_through(tmp_path):
    p = _proxy(tmp_path, threshold=100_000)
    p._pending[2] = "tools/call"
    msg = _tool_call_response(json.dumps({"small": True}))
    out = p._maybe_rewrite_response(msg)
    assert out is msg


def test_expired_handle_returns_structured_error(tmp_path):
    p = _proxy(tmp_path)
    result = p.dispatcher.dispatch("jmunch_peek", {"handle": "h_doesnotexist"})
    assert result["code"] == "HANDLE_EXPIRED"
