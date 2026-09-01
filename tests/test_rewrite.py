from __future__ import annotations

import json
from pathlib import Path


from jmunch_mcp.cli import rewrite
from jmunch_mcp.cli.discovery import Candidate


def _write_client_config(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")


def _candidate_for(cfg_path: Path, key: str, entry: dict) -> Candidate:
    return Candidate(
        name=key,
        command=entry["command"],
        args=tuple(entry.get("args", [])),
        source="client:Test",
        source_path=cfg_path,
        server_key=key,
    )


def test_plan_rewrite_reports_before_after(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    entry = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
             "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "xxx"}}
    _write_client_config(cfg, {"github": entry})
    cand = _candidate_for(cfg, "github", entry)

    toml_path = tmp_path / "configs" / "github.toml"
    plan = rewrite.plan_rewrite(cand, toml_path)
    assert plan.status == "rewrote"
    assert plan.before["command"] == "npx"
    assert plan.after["command"] == "jmunch-mcp"
    assert plan.after["args"] == ["--config", str(toml_path)]
    # env preserved
    assert plan.after["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "xxx"}


def test_apply_rewrite_writes_bak_and_replaces_entry(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    entry = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}
    _write_client_config(cfg, {"github": entry, "other": {"command": "foo", "args": []}})
    cand = _candidate_for(cfg, "github", entry)
    toml_path = tmp_path / "configs" / "github.toml"

    result = rewrite.apply_rewrite(cand, toml_path)
    assert result.status == "rewrote"

    bak = cfg.with_suffix(cfg.suffix + ".bak")
    assert bak.exists(), ".bak should be written on rewrite"
    # bak preserves original
    assert json.loads(bak.read_text())["mcpServers"]["github"]["command"] == "npx"

    # Live file has the wrapped entry
    live = json.loads(cfg.read_text())
    assert live["mcpServers"]["github"]["command"] == "jmunch-mcp"
    assert live["mcpServers"]["github"]["args"] == ["--config", str(toml_path)]
    # Other entries left alone
    assert live["mcpServers"]["other"] == {"command": "foo", "args": []}


def test_apply_rewrite_is_idempotent(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    entry = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}
    _write_client_config(cfg, {"github": entry})
    cand = _candidate_for(cfg, "github", entry)
    toml_path = tmp_path / "configs" / "github.toml"

    rewrite.apply_rewrite(cand, toml_path)

    # Second pass: plan now says already_wrapped (rebuild candidate from current file)
    live = json.loads(cfg.read_text())
    cand2 = _candidate_for(cfg, "github", live["mcpServers"]["github"])
    result2 = rewrite.apply_rewrite(cand2, toml_path)
    assert result2.status == "already_wrapped"


def test_bak_not_overwritten_on_second_rewrite(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    entry = {"command": "npx", "args": ["original"]}
    _write_client_config(cfg, {"github": entry})
    cand = _candidate_for(cfg, "github", entry)
    toml_path = tmp_path / "configs" / "github.toml"

    rewrite.apply_rewrite(cand, toml_path)
    bak_before = (cfg.with_suffix(cfg.suffix + ".bak")).read_text()

    # Simulate user manually edited the live file to a new (non-wrapped) state
    data = json.loads(cfg.read_text())
    data["mcpServers"]["github"] = {"command": "npx", "args": ["modified"]}
    cfg.write_text(json.dumps(data))

    cand3 = _candidate_for(cfg, "github", data["mcpServers"]["github"])
    rewrite.apply_rewrite(cand3, toml_path)

    bak_after = (cfg.with_suffix(cfg.suffix + ".bak")).read_text()
    assert bak_before == bak_after, "first .bak must remain the true original"


def test_restore_from_bak(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    entry = {"command": "npx", "args": ["orig"]}
    _write_client_config(cfg, {"github": entry})
    cand = _candidate_for(cfg, "github", entry)
    toml_path = tmp_path / "configs" / "github.toml"

    rewrite.apply_rewrite(cand, toml_path)
    assert json.loads(cfg.read_text())["mcpServers"]["github"]["command"] == "jmunch-mcp"

    result = rewrite.restore(cfg)
    assert result.status == "restored"
    assert json.loads(cfg.read_text())["mcpServers"]["github"]["command"] == "npx"


def test_plan_rewrite_missing_key_returns_not_found(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    _write_client_config(cfg, {"something-else": {"command": "x", "args": []}})
    cand = Candidate(
        name="github", command="npx", args=("-y", "x"),
        source="client:Test", source_path=cfg, server_key="github",
    )
    result = rewrite.plan_rewrite(cand, tmp_path / "x.toml")
    assert result.status == "not_found"


def test_plan_rewrite_unsupported_when_no_source_path():
    cand = Candidate(name="catalog-only", command="npx", args=(), source="catalog")
    result = rewrite.plan_rewrite(cand, Path("/tmp/x.toml"))
    assert result.status == "unsupported"


def test_unwrap_entry_selective_restore(tmp_path):
    """Wrapping A then B, then unwrapping only A, should leave B still wrapped."""
    cfg = tmp_path / "claude_desktop_config.json"
    a_orig = {"command": "npx", "args": ["-y", "pkg-a"]}
    b_orig = {"command": "uvx", "args": ["pkg-b"]}
    _write_client_config(cfg, {"a": a_orig, "b": b_orig})

    cand_a = _candidate_for(cfg, "a", a_orig)
    cand_b = _candidate_for(cfg, "b", b_orig)
    toml_a = tmp_path / "configs" / "a.toml"
    toml_b = tmp_path / "configs" / "b.toml"

    rewrite.apply_rewrite(cand_a, toml_a)
    rewrite.apply_rewrite(cand_b, toml_b)

    live = json.loads(cfg.read_text())
    assert live["mcpServers"]["a"]["command"] == "jmunch-mcp"
    assert live["mcpServers"]["b"]["command"] == "jmunch-mcp"

    result = rewrite.unwrap_entry(cfg, "a")
    assert result.status == "unwrapped"

    live = json.loads(cfg.read_text())
    # A is back to original
    assert live["mcpServers"]["a"]["command"] == "npx"
    assert live["mcpServers"]["a"]["args"] == ["-y", "pkg-a"]
    # B is still wrapped
    assert live["mcpServers"]["b"]["command"] == "jmunch-mcp"


def test_unwrap_entry_no_backup_returns_no_backup(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    _write_client_config(cfg, {"a": {"command": "jmunch-mcp", "args": ["--config", "x.toml"]}})
    result = rewrite.unwrap_entry(cfg, "a")
    assert result.status == "no_backup"


def test_per_project_wrap_and_unwrap_roundtrip(tmp_path):
    """Claude Code stores per-project MCPs under projects[<path>].mcpServers.
    Rewrite should navigate to the right container, not blindly touch top-level.
    """
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"global-thing": {"command": "x", "args": []}},
        "projects": {
            "C:/MCPs": {
                "mcpServers": {
                    "playwright": {"command": "cmd",
                                   "args": ["/c", "npx", "-y", "@playwright/mcp@latest"]}
                }
            }
        }
    }), encoding="utf-8")

    cand = Candidate(
        name="playwright",
        command="cmd",
        args=("/c", "npx", "-y", "@playwright/mcp@latest"),
        source="client:Claude Code",
        source_path=cfg,
        server_key="playwright",
        source_project="C:/MCPs",
    )
    toml = tmp_path / "configs" / "playwright.toml"

    r = rewrite.apply_rewrite(cand, toml)
    assert r.status == "rewrote"
    live = json.loads(cfg.read_text())
    # Per-project entry got wrapped
    assert live["projects"]["C:/MCPs"]["mcpServers"]["playwright"]["command"] == "jmunch-mcp"
    # Top-level entry NOT touched
    assert live["mcpServers"]["global-thing"]["command"] == "x"

    # Unwrap
    u = rewrite.unwrap_entry(cfg, "playwright", source_project="C:/MCPs")
    assert u.status == "unwrapped"
    live = json.loads(cfg.read_text())
    assert live["projects"]["C:/MCPs"]["mcpServers"]["playwright"]["command"] == "cmd"


def test_unwrap_entry_not_wrapped_returns_not_wrapped(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    _write_client_config(cfg, {"a": {"command": "npx", "args": ["x"]}})
    # .bak doesn't matter here — the entry isn't wrapped
    (cfg.with_suffix(cfg.suffix + ".bak")).write_text(cfg.read_text())
    result = rewrite.unwrap_entry(cfg, "a")
    assert result.status == "not_wrapped"
