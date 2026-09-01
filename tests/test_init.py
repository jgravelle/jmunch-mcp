from __future__ import annotations

import json


from jmunch_mcp.cli import discovery, init
from jmunch_mcp.cli.discovery import Candidate, discover, scan_client_configs


def test_catalog_is_nonempty_and_dedupes_by_signature():
    d = discover(include_running=False)
    assert d.candidates, "catalog should always seed at least one candidate"
    seen = set()
    for c in d.candidates:
        assert c.signature not in seen
        seen.add(c.signature)


def test_scan_client_configs_reads_per_project_section(tmp_path, monkeypatch):
    """Claude Code's ~/.claude.json puts per-project MCPs under projects[<path>]."""
    fake = tmp_path / ".claude.json"
    fake.write_text(json.dumps({
        "mcpServers": {"g": {"command": "x", "args": []}},
        "projects": {
            "C:/proj": {
                "mcpServers": {
                    "playwright": {"command": "cmd",
                                   "args": ["/c", "npx", "-y", "@playwright/mcp@latest"]}
                }
            }
        }
    }), encoding="utf-8")
    monkeypatch.setattr(discovery, "_client_config_paths", lambda: [("Claude Code", fake)])
    got = scan_client_configs()
    names = {c.name for c in got}
    assert "g" in names
    assert "playwright" in names
    pw = next(c for c in got if c.name == "playwright")
    assert pw.source_project == "C:/proj"
    assert "project C:/proj" in pw.source


def test_scan_client_configs_reads_mcpservers(tmp_path, monkeypatch):
    fake = tmp_path / "claude_desktop_config.json"
    fake.write_text(json.dumps({
        "mcpServers": {
            "demo-github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "xxx"},
            },
            "already-wrapped": {
                # Wrapped entries must remain visible so the dashboard can
                # show them as Optimized and offer an unwrap.
                "command": "jmunch-mcp",
                "args": ["--config", "foo.toml"],
            },
        }
    }))
    monkeypatch.setattr(discovery, "_client_config_paths", lambda: [("Test", fake)])
    got = scan_client_configs()
    names = {c.name for c in got}
    assert "demo-github" in names
    assert "already-wrapped" in names
    demo = next(c for c in got if c.name == "demo-github")
    assert demo.env_keys == ("GITHUB_PERSONAL_ACCESS_TOKEN",)
    assert demo.source == "client:Test"


def test_merge_prefers_client_over_catalog(monkeypatch):
    client = Candidate(
        name="github",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        env_keys=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        source="client:Cursor",
        description="from client",
    )
    monkeypatch.setattr(discovery, "scan_client_configs", lambda: [client])
    monkeypatch.setattr(discovery, "scan_running_processes", lambda: [])
    d = discover()
    gh = [c for c in d.candidates if c.name == "github"]
    assert len(gh) == 1
    assert gh[0].source == "client:Cursor"


def test_render_toml_round_trip(tmp_path):
    c = Candidate(
        name="github",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        env_keys=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        source="catalog",
        description="GitHub MCP",
    )
    text = init._render_toml(c, threshold_tokens=1500)
    target = tmp_path / "github.toml"
    target.write_text(text, encoding="utf-8")
    # Must be loadable by our own config loader.
    from jmunch_mcp.config import load
    cfg = load(target)
    assert cfg.upstream.command == "npx"
    assert cfg.upstream.args == ["-y", "@modelcontextprotocol/server-github"]
    assert cfg.threshold_tokens == 1500


def test_safe_filename_strips_unsafe_chars():
    assert init._safe_filename("@modelcontextprotocol/server-github") == "modelcontextprotocol-server-github"
    assert init._safe_filename("") == "upstream"


def test_run_init_writes_files(tmp_path, monkeypatch, capsys):
    fake = [Candidate(
        name="github",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        env_keys=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        source="client:Test",
        description="github",
    )]
    monkeypatch.setattr("jmunch_mcp.cli.init.discover",
                        lambda **_: type("D", (), {"candidates": fake})())
    out = tmp_path / "configs"
    rc = init.run_init(out_dir=out, yes=True, dry_run=False, no_backup=True,
                       no_running=True, no_catalog=True)
    assert rc == 0
    written = list(out.glob("*.toml"))
    assert len(written) == 1
    assert "npx" in written[0].read_text(encoding="utf-8")


def test_run_init_dry_run_writes_nothing(tmp_path, monkeypatch):
    fake = [Candidate(
        name="github",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        source="client:Test",
    )]
    monkeypatch.setattr("jmunch_mcp.cli.init.discover",
                        lambda **_: type("D", (), {"candidates": fake})())
    out = tmp_path / "configs"
    rc = init.run_init(out_dir=out, yes=True, dry_run=True, no_backup=True,
                       no_running=True, no_catalog=True)
    assert rc == 0
    assert not out.exists() or not any(out.glob("*.toml"))


def test_run_init_no_candidates_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr("jmunch_mcp.cli.init.discover",
                        lambda **_: type("D", (), {"candidates": []})())
    rc = init.run_init(out_dir=tmp_path, yes=True, no_running=True, no_catalog=True)
    assert rc == 1


def test_run_init_rewrite_flag_rewrites_client_config(tmp_path, monkeypatch):
    # Fake a client config on disk
    client_cfg = tmp_path / "claude_desktop_config.json"
    client_cfg.write_text(json.dumps({
        "mcpServers": {
            "github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "xxx"},
            }
        }
    }), encoding="utf-8")

    fake = [Candidate(
        name="github",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        env_keys=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        source="client:Test",
        description="from client",
        source_path=client_cfg,
        server_key="github",
    )]
    monkeypatch.setattr("jmunch_mcp.cli.init.discover",
                        lambda **_: type("D", (), {"candidates": fake})())
    out = tmp_path / "configs"
    rc = init.run_init(out_dir=out, yes=True, no_backup=True,
                       no_running=True, no_catalog=True, rewrite=True)
    assert rc == 0

    # Client config got rewritten
    live = json.loads(client_cfg.read_text())
    assert live["mcpServers"]["github"]["command"] == "jmunch-mcp"
    assert live["mcpServers"]["github"]["args"][0] == "--config"
    # env preserved
    assert live["mcpServers"]["github"]["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "xxx"}
    # .bak created
    assert (client_cfg.with_suffix(client_cfg.suffix + ".bak")).exists()


def test_run_init_non_interactive_skips_rewrite_by_default(tmp_path, monkeypatch):
    client_cfg = tmp_path / "claude_desktop_config.json"
    client_cfg.write_text(json.dumps({
        "mcpServers": {"github": {"command": "npx", "args": ["-y", "x"]}}
    }), encoding="utf-8")

    fake = [Candidate(
        name="github", command="npx", args=("-y", "x"),
        source="client:Test",
        source_path=client_cfg, server_key="github",
    )]
    monkeypatch.setattr("jmunch_mcp.cli.init.discover",
                        lambda **_: type("D", (), {"candidates": fake})())
    rc = init.run_init(out_dir=tmp_path / "configs", yes=True, no_backup=True,
                       no_running=True, no_catalog=True)
    assert rc == 0
    # Client config unchanged
    live = json.loads(client_cfg.read_text())
    assert live["mcpServers"]["github"]["command"] == "npx"
