from __future__ import annotations

import time


from jmunch_mcp import metrics


def test_metrics_record_and_totals(tmp_path):
    db = tmp_path / "m.db"
    m = metrics.MetricsDB(db)
    assert m.enabled

    m.record(upstream="github", tool="search_issues",
             raw_bytes=4000, response_bytes=400, saved_bytes=3600,
             duration_ms=120, handle_created=True)
    m.record(upstream="github", tool="jmunch_peek",
             response_bytes=250, duration_ms=3)
    m.record(upstream="firecrawl", tool="scrape",
             raw_bytes=20000, response_bytes=300, saved_bytes=19700,
             duration_ms=800, handle_created=True)
    m.close()

    t = metrics.totals(db)
    # Dashboard rule: rows with saved_bytes=0 (jmunch_peek here) are hidden everywhere.
    assert t["calls"] == 2
    assert t["saved_bytes"] == 23300
    assert t["tokens_saved"] == 23300 // 4
    assert t["handles_created"] == 2


def test_metrics_per_upstream(tmp_path):
    db = tmp_path / "m.db"
    m = metrics.MetricsDB(db)
    m.record(upstream="github", tool="x", raw_bytes=100, response_bytes=50, saved_bytes=50)
    m.record(upstream="github", tool="y", raw_bytes=200, response_bytes=20, saved_bytes=180)
    m.record(upstream="firecrawl", tool="z", raw_bytes=1000, response_bytes=10, saved_bytes=990)
    m.close()

    rows = metrics.per_upstream(db)
    assert len(rows) == 2
    # ordered by saved DESC
    assert rows[0]["upstream"] == "firecrawl"
    assert rows[0]["calls"] == 1
    assert rows[0]["saved_bytes"] == 990
    assert rows[1]["upstream"] == "github"
    assert rows[1]["calls"] == 2
    assert rows[1]["saved_bytes"] == 230


def test_metrics_recent_calls_order(tmp_path):
    db = tmp_path / "m.db"
    m = metrics.MetricsDB(db)
    for i in range(5):
        m.record(upstream="github", tool=f"t{i}", raw_bytes=i, response_bytes=0, saved_bytes=i)
    m.close()

    rows = metrics.recent_calls(limit=3, path=db)
    assert len(rows) == 3
    # newest first
    assert rows[0]["tool"] == "t4"
    assert rows[-1]["tool"] == "t2"


def test_metrics_series_buckets(tmp_path):
    db = tmp_path / "m.db"
    m = metrics.MetricsDB(db)
    # Align to a bucket boundary so both records land in the same 5-min slot
    # regardless of when the test runs.
    base = (time.time() // 300) * 300 + 10
    # Two calls in same 5-min bucket
    m.record(upstream="a", tool="x", raw_bytes=100, saved_bytes=50, ts=base)
    m.record(upstream="a", tool="y", raw_bytes=100, saved_bytes=50, ts=base + 10)
    m.close()

    s = metrics.series(bucket_seconds=300, hours=1, path=db)
    assert len(s) == 1
    assert s[0]["calls"] == 2
    assert s[0]["saved_bytes"] == 100
    assert s[0]["tokens_saved"] == 25


def test_zero_saved_rows_hidden_everywhere(tmp_path):
    """Dashboard rule: rows with saved_bytes=0 never surface. Covers jmunch_*
    handle ops, below-threshold passthroughs, and pure errors."""
    db = tmp_path / "m.db"
    m = metrics.MetricsDB(db)
    m.record(upstream="github", tool="search_issues",
             raw_bytes=4000, response_bytes=400, saved_bytes=3600)
    m.record(upstream="github", tool="jmunch_slice", response_bytes=250)  # saved=0
    m.record(upstream="passthru", tool="ping", raw_bytes=50, response_bytes=50)  # saved=0
    m.close()

    tools = [r["tool"] for r in metrics.recent_calls(path=db)]
    assert tools == ["search_issues"]

    ups = [r["upstream"] for r in metrics.per_upstream(db)]
    assert ups == ["github"]

    t = metrics.totals(db)
    assert t["calls"] == 1
    assert t["saved_bytes"] == 3600


def test_totals_missing_db_returns_empty(tmp_path):
    db = tmp_path / "does-not-exist.db"
    t = metrics.totals(db)
    assert t["calls"] == 0
    assert t["saved_bytes"] == 0
