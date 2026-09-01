from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from jmunch_mcp import metrics
from jmunch_mcp.cli import dashboard


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "m.db"
    m = metrics.MetricsDB(db)
    m.record(upstream="github", tool="search_issues",
             raw_bytes=5000, response_bytes=400, saved_bytes=4600,
             duration_ms=80, handle_created=True)
    m.record(upstream="firecrawl", tool="scrape",
             raw_bytes=30000, response_bytes=300, saved_bytes=29700,
             duration_ms=500, handle_created=True)
    m.close()
    return db


def _run_server(host, port, db_path):
    handler = dashboard._make_handler(db_path)
    server = dashboard._ReuseServer((host, port), handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def _get(url: str) -> tuple[int, bytes, str]:
    with urllib.request.urlopen(url, timeout=3) as r:
        return r.status, r.read(), r.headers.get("Content-Type", "")


def test_api_stats_returns_totals_and_upstreams(seeded_db):
    server = _run_server("127.0.0.1", 0, seeded_db)
    port = server.server_address[1]
    try:
        status, body, ct = _get(f"http://127.0.0.1:{port}/api/stats")
        assert status == 200
        assert "application/json" in ct
        data = json.loads(body)
        assert data["totals"]["calls"] == 2
        assert data["totals"]["saved_bytes"] == 4600 + 29700
        upstreams = {r["upstream"] for r in data["per_upstream"]}
        assert upstreams == {"github", "firecrawl"}
    finally:
        server.shutdown()


def test_api_calls_tail(seeded_db):
    server = _run_server("127.0.0.1", 0, seeded_db)
    port = server.server_address[1]
    try:
        status, body, _ = _get(f"http://127.0.0.1:{port}/api/calls?limit=10")
        assert status == 200
        rows = json.loads(body)
        assert len(rows) == 2
        assert rows[0]["tool"] in ("search_issues", "scrape")
    finally:
        server.shutdown()


def test_api_servers_tags_own_suite(monkeypatch, tmp_path):
    from jmunch_mcp.cli.discovery import Candidate

    fake = [
        Candidate(name="github", command="npx", args=("-y", "@modelcontextprotocol/server-github"),
                  source="client:Test"),
        Candidate(name="jmunch-mcp", command="jmunch-mcp", args=("--config", "x.toml"),
                  source="client:Test"),
    ]
    monkeypatch.setattr(
        "jmunch_mcp.cli.dashboard.discover",
        lambda **_: type("D", (), {"candidates": fake})(),
    )
    server = _run_server("127.0.0.1", 0, tmp_path / "empty.db")
    port = server.server_address[1]
    try:
        status, body, _ = _get(f"http://127.0.0.1:{port}/api/servers")
        assert status == 200
        data = json.loads(body)
        names = {s["name"] for s in data["servers"]}
        own_names = {s["name"] for s in data["own_suite"]}
        assert "github" in names
        assert "jmunch-mcp" in own_names
    finally:
        server.shutdown()


def test_index_serves_html(seeded_db):
    server = _run_server("127.0.0.1", 0, seeded_db)
    port = server.server_address[1]
    try:
        status, body, ct = _get(f"http://127.0.0.1:{port}/")
        assert status == 200
        assert "text/html" in ct
        assert b"jmunch-mcp dashboard" in body
    finally:
        server.shutdown()


def test_post_optimize_wraps_entry(tmp_path, monkeypatch):
    client_cfg = tmp_path / "claude_desktop_config.json"
    client_cfg.write_text(json.dumps({
        "mcpServers": {
            "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}
        }
    }), encoding="utf-8")
    # Keep the dashboard's default configs dir inside tmp_path so we don't
    # dirty the user's home during tests.
    monkeypatch.setattr(
        "jmunch_mcp.cli.dashboard._default_configs_dir",
        lambda: tmp_path / "configs",
    )

    server = _run_server("127.0.0.1", 0, tmp_path / "empty.db")
    port = server.server_address[1]
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/optimize",
            data=json.dumps({"action": "wrap",
                             "source_path": str(client_cfg),
                             "server_key": "github"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
        assert data["ok"] is True
        assert data["status"] == "rewrote"

        live = json.loads(client_cfg.read_text())
        assert live["mcpServers"]["github"]["command"] == "jmunch-mcp"
        # TOML was generated
        toml = tmp_path / "configs" / "github.toml"
        assert toml.exists()
    finally:
        server.shutdown()


def test_post_optimize_unwrap_round_trip(tmp_path, monkeypatch):
    client_cfg = tmp_path / "claude_desktop_config.json"
    client_cfg.write_text(json.dumps({
        "mcpServers": {
            "github": {"command": "npx", "args": ["-y", "pkg"], "env": {"X": "1"}}
        }
    }), encoding="utf-8")
    monkeypatch.setattr(
        "jmunch_mcp.cli.dashboard._default_configs_dir",
        lambda: tmp_path / "configs",
    )

    server = _run_server("127.0.0.1", 0, tmp_path / "empty.db")
    port = server.server_address[1]
    try:
        def post(body):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/optimize",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read())

        w = post({"action": "wrap", "source_path": str(client_cfg), "server_key": "github"})
        assert w["ok"]

        u = post({"action": "unwrap", "source_path": str(client_cfg), "server_key": "github"})
        assert u["ok"]
        assert u["status"] == "unwrapped"

        live = json.loads(client_cfg.read_text())
        assert live["mcpServers"]["github"]["command"] == "npx"
        assert live["mcpServers"]["github"]["env"] == {"X": "1"}
    finally:
        server.shutdown()


def test_pkg_from_args_detects_npm_and_pypi():
    from jmunch_mcp.cli import dashboard as D
    assert D._pkg_from_args("npx", ["-y", "@modelcontextprotocol/server-github"]) == \
        ("npm", "@modelcontextprotocol/server-github")
    assert D._pkg_from_args("npx.cmd", ["-y", "firecrawl-mcp"]) == ("npm", "firecrawl-mcp")
    assert D._pkg_from_args("uvx", ["mcp-server-fetch"]) == ("pypi", "mcp-server-fetch")
    assert D._pkg_from_args("python", ["-m", "something"]) is None


def test_version_cache_avoids_refetch(monkeypatch):
    from jmunch_mcp.cli import dashboard as D
    D._VERSION_CACHE.clear()
    calls = {"n": 0}

    def fake_fetch(pkg):
        calls["n"] += 1
        return "9.9.9"

    monkeypatch.setattr(D, "_fetch_npm", fake_fetch)
    v1 = D._latest_version("npm", "foo")
    v2 = D._latest_version("npm", "foo")
    assert v1 == v2 == "9.9.9"
    assert calls["n"] == 1


def test_csv_export(seeded_db):
    server = _run_server("127.0.0.1", 0, seeded_db)
    port = server.server_address[1]
    try:
        status, body, ct = _get(f"http://127.0.0.1:{port}/api/export.csv")
        assert status == 200
        assert "text/csv" in ct
        text = body.decode("utf-8")
        # Header starts with ts,upstream; column order after that evolves.
        assert text.startswith("ts,upstream,")
        assert "tool" in text.split("\n", 1)[0]
        assert "github" in text
        assert "firecrawl" in text
    finally:
        server.shutdown()
