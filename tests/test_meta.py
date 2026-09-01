from pathlib import Path

from jmunch_mcp.meta import SavingsTracker, envelope, estimate_savings


def test_savings_calc_matches_jmri_spec():
    assert estimate_savings(4000, 400) == 900  # (4000-400)//4
    assert estimate_savings(100, 500) == 0  # no negative


def test_envelope_shape(tmp_path: Path):
    tracker = SavingsTracker(path=tmp_path / "_savings.json")
    env = envelope(result={"hello": "world"}, raw_bytes=4000, response_bytes=400, tracker=tracker)
    assert "result" in env
    assert "error" not in env
    meta = env["_meta"]
    assert meta["tokens_saved"] == 900
    assert meta["total_tokens_saved"] == 900
    assert meta["response_tokens"] == 100
    assert meta["naive_tokens"] == 1000
    assert meta["retrieval_engine"] == "jmunch"
    assert meta["retrieval_version"] == "1.0"
    assert "powered_by" in meta


def test_envelope_error_shape(tmp_path: Path):
    tracker = SavingsTracker(path=tmp_path / "_savings.json")
    env = envelope(
        error={"code": "NOT_FOUND", "message": "nope"},
        raw_bytes=0,
        response_bytes=0,
        tracker=tracker,
    )
    assert "error" in env
    assert env["error"]["code"] == "NOT_FOUND"
    assert env["_meta"]["tokens_saved"] == 0


def test_tracker_persists(tmp_path: Path):
    path = tmp_path / "_savings.json"
    t1 = SavingsTracker(path=path)
    t1.record(1000)
    t1.record(500)
    assert t1.total == 1500

    t2 = SavingsTracker(path=path)
    assert t2.total == 1500


def test_model_prices_pinned_to_published_rates():
    """Restated literals, sourced 2026-09-01 from
    platform.claude.com/docs/en/about-claude/pricing (Base Input Tokens).
    Do not import the constant to build these — a pin that reads the value it
    checks asserts nothing.
    """
    from jmunch_mcp.meta import _MODEL_PRICES_PER_1M

    assert _MODEL_PRICES_PER_1M == {
        "claude_opus": 5.00,  # Claude Opus 5 — $5/MTok input
        "claude_sonnet": 2.00,  # Claude Sonnet 5 — $2/MTok input
        "gpt5_latest": 10.00,  # not Anthropic; unverified, left as-is
    }


def test_cost_avoided_round_trip():
    from jmunch_mcp.meta import cost_avoided

    assert cost_avoided(1_000_000) == {
        "claude_opus": 5.0,
        "claude_sonnet": 2.0,
        "gpt5_latest": 10.0,
    }
    assert cost_avoided(0) == {"claude_opus": 0.0, "claude_sonnet": 0.0, "gpt5_latest": 0.0}
    # 200k tokens: rounding to 4dp is part of the wire contract.
    assert cost_avoided(200_000) == {
        "claude_opus": 1.0,
        "claude_sonnet": 0.4,
        "gpt5_latest": 2.0,
    }
