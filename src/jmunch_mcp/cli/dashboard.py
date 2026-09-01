"""`jmunch-mcp dashboard` — a read-only local web UI over the metrics DB.

Stdlib-only HTTP server. Serves:

    GET /                → dashboard.html
    GET /api/stats       → {totals, per_upstream, series}
    GET /api/servers     → {servers: [...], own_suite: [...]}
    GET /api/calls       → [{ts, upstream, tool, ...}, ...]
    GET /api/export.csv  → CSV dump of the calls table

No writes, no config mutation. The "Optimized" toggle is a future step
(it reuses `cli.rewrite`, which has already been validated by `init`).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import metrics
from . import rewrite
from .discovery import Candidate, discover
from .init import _render_toml, _safe_filename

log = logging.getLogger("jmunch.dashboard")

# Own suite — never offered as a proxy target (would loop).
# Stems match both the -mcp form and bare form (e.g. jcodemunch-mcp, jcodemunch).
_OWN_SUITE_STEMS = ("jmunch", "jcodemunch", "jdocmunch", "jdatamunch")
_OWN_SUITE = {s for stem in _OWN_SUITE_STEMS for s in (stem, f"{stem}-mcp")}


def _is_own_suite(c: Candidate) -> bool:
    # We classify on identity, not on launch command — because a wrapped
    # third-party entry has command == "jmunch-mcp" and must NOT be lumped in.
    # But own-suite entries come in many shapes we need to recognize:
    #   - server_key / name == "jcodemunch-mcp"                 (plain)
    #   - name == r'"C:\Python314\Scripts\jcodemunch-mcp.exe"'  (running scan)
    #   - args include "jcodemunch_mcp.server"                  (python -m ...)
    #   - args include "...\configs\jcodemunch-mcp.toml"        (already-wrapped)
    # Normalize each candidate token: strip quotes/paths, lowercase,
    # map _ → -, drop trailing extension. Then check any token ∈ _OWN_SUITE.
    tokens = [c.server_key or "", c.name or "", *c.args]
    for raw in tokens:
        s = str(raw).strip().strip('"').strip("'").lower().replace("_", "-")
        # Take the final path component (handles both / and \).
        s = re.split(r"[\\/]", s)[-1]
        # Drop a trailing .ext (e.g. .exe, .toml, .server).
        s = s.split(".", 1)[0]
        if s in _OWN_SUITE:
            return True
    return False


def _is_wrapped(c: Candidate) -> bool:
    """True if this client-sourced entry is already launching jmunch-mcp."""
    return c.command == "jmunch-mcp"


def _server_payload() -> dict:
    disco = discover(include_catalog=False)
    servers = []
    own = []
    for c in disco.candidates:
        record = {
            "name": c.name,
            "command": c.command,
            "args": list(c.args),
            "source": c.source,
            "source_path": str(c.source_path) if c.source_path else None,
            "server_key": c.server_key,
            "source_project": c.source_project,
            "env_keys": list(c.env_keys),
            "description": c.description,
            "wrapped": _is_wrapped(c),
            "own_suite": _is_own_suite(c),
        }
        (own if record["own_suite"] else servers).append(record)
    return {"servers": servers, "own_suite": own}


def _stats_payload(db_path: Path, surface: str | None = None) -> dict:
    return {
        "totals": metrics.totals(db_path, surface=surface),
        "per_upstream": metrics.per_upstream(db_path, surface=surface),
        "series": metrics.series(bucket_seconds=300, hours=24, path=db_path, surface=surface),
        "surface": surface or "all",
        "generated_at": time.time(),
    }


def _calls_payload(db_path: Path, limit: int, surface: str | None = None) -> list[dict]:
    return metrics.recent_calls(limit=limit, path=db_path, surface=surface)


def _export_csv(db_path: Path) -> str:
    rows = metrics.recent_calls(limit=100_000, path=db_path)
    buf = io.StringIO()
    if not rows:
        return "ts,upstream,tool,raw_bytes,response_bytes,saved_bytes,duration_ms,handle_created,is_error\n"
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Version lookup — best-effort, cached, never blocks the UI hard.
# ---------------------------------------------------------------------------

# package key -> (fetched_at, latest_version_or_None)
_VERSION_CACHE: dict[str, tuple[float, str | None]] = {}
_VERSION_TTL = 3600.0  # 1 hour


def _pkg_from_args(command: str, args: list[str]) -> tuple[str, str] | None:
    """Return (registry, pkg_name) or None if we can't identify a package.
    registry is "npm" or "pypi".
    """
    cmd = command.lower().removesuffix(".cmd").removesuffix(".exe")
    if cmd in ("npx", "npm"):
        for a in args:
            if a.startswith("-"):
                continue
            # strip version pin like foo@1.2.3 (registry call wants the bare name)
            base = a.split("@", 2)
            if a.startswith("@") and len(base) >= 2:
                name = "@" + base[1]  # "@scope/pkg"
            else:
                name = base[0]
            if name:
                return ("npm", name)
        return None
    if cmd in ("uvx", "uv"):
        for a in args:
            if a.startswith("-"):
                continue
            return ("pypi", a.split("@", 1)[0].split("==", 1)[0])
        return None
    return None


def _fetch_npm(pkg: str) -> str | None:
    url = f"https://registry.npmjs.org/{urllib.parse.quote(pkg, safe='@')}/latest"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read())
        v = data.get("version")
        return str(v) if isinstance(v, str) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _fetch_pypi(pkg: str) -> str | None:
    url = f"https://pypi.org/pypi/{urllib.parse.quote(pkg)}/json"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read())
        v = data.get("info", {}).get("version")
        return str(v) if isinstance(v, str) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _latest_version(registry: str, pkg: str) -> str | None:
    key = f"{registry}:{pkg}"
    now = time.time()
    cached = _VERSION_CACHE.get(key)
    if cached and now - cached[0] < _VERSION_TTL:
        return cached[1]
    v = _fetch_npm(pkg) if registry == "npm" else _fetch_pypi(pkg)
    _VERSION_CACHE[key] = (now, v)
    return v


def _versions_payload() -> dict:
    disco = discover(include_catalog=False)
    out: dict[str, dict] = {}
    for c in disco.candidates:
        if _is_own_suite(c) or _is_wrapped(c):
            continue
        pkg = _pkg_from_args(c.command, list(c.args))
        if not pkg:
            continue
        registry, name = pkg
        out[c.name] = {
            "registry": registry,
            "package": name,
            "latest": _latest_version(registry, name),
        }
    return out


# ---------------------------------------------------------------------------
# Optimize/unoptimize handler
# ---------------------------------------------------------------------------


def _default_configs_dir() -> Path:
    return Path.home() / ".jmunch" / "configs"


def _do_optimize(body: dict) -> dict:
    """Wrap or unwrap a single mcpServers entry. Body:
        {action: "wrap"|"unwrap", source_path: str, server_key: str}
    """
    action = body.get("action")
    source_path = body.get("source_path")
    server_key = body.get("server_key")
    source_project = body.get("source_project") or ""
    if action not in ("wrap", "unwrap") or not source_path or not server_key:
        return {"ok": False, "error": "missing action / source_path / server_key"}

    path = Path(source_path)
    if not path.exists():
        return {"ok": False, "error": f"config file not found: {path}"}

    if action == "unwrap":
        result = rewrite.unwrap_entry(path, server_key, source_project)
        return {"ok": result.status == "unwrapped", "status": result.status,
                "path": str(path), "server_key": server_key}

    # wrap: need the live entry to render a TOML for it
    try:
        live = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "error": f"cannot read config: {e}"}
    container = rewrite._servers_container(live, source_project)
    entry = (container or {}).get(server_key)
    if not isinstance(entry, dict):
        return {"ok": False, "error": f"entry not found: {server_key}"}
    if entry.get("command") == "jmunch-mcp":
        return {"ok": True, "status": "already_wrapped",
                "path": str(path), "server_key": server_key}

    cand = Candidate(
        name=server_key,
        command=str(entry.get("command") or ""),
        args=tuple(str(a) for a in (entry.get("args") or [])),
        env_keys=tuple((entry.get("env") or {}).keys()),
        source=f"client:{path.name}",
        source_path=path,
        server_key=server_key,
        source_project=source_project,
    )
    configs_dir = _default_configs_dir()
    toml_path = configs_dir / f"{_safe_filename(server_key)}.toml"
    configs_dir.mkdir(parents=True, exist_ok=True)
    if not toml_path.exists():
        toml_path.write_text(_render_toml(cand), encoding="utf-8")

    result = rewrite.apply_rewrite(cand, toml_path)
    return {
        "ok": result.status == "rewrote",
        "status": result.status,
        "path": str(path),
        "server_key": server_key,
        "toml_path": str(toml_path),
    }


def _primary_claude_code_config() -> Path | None:
    """Best available Claude Code config on this system, or None."""
    for cand in (Path.home() / ".claude.json", Path.home() / ".claude/settings.json"):
        if cand.exists():
            return cand
    return None


def _do_install(body: dict) -> dict:
    """Wire a local .toml into the user's Claude Code config as a wrapped entry.

    Body: {toml_path: str, server_key?: str}
    Adds top-level mcpServers[<server_key>] = {command: jmunch-mcp, args: [--config, <toml_path>]}
    """
    toml_path_str = body.get("toml_path")
    if not isinstance(toml_path_str, str) or not toml_path_str:
        return {"ok": False, "error": "missing toml_path"}
    toml_path = Path(toml_path_str)
    if not toml_path.exists():
        return {"ok": False, "error": f"toml not found: {toml_path}"}

    server_key = body.get("server_key") or toml_path.stem
    cfg = _primary_claude_code_config()
    if cfg is None:
        return {"ok": False, "error": "no Claude Code config found (~/.claude.json)"}

    try:
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.stat().st_size else {}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"cannot read {cfg}: {e}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": f"unexpected JSON shape in {cfg}"}

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return {"ok": False, "error": "mcpServers is not an object"}
    if server_key in servers:
        return {"ok": False, "error": f"entry already exists: {server_key}"}

    servers[server_key] = {
        "type": "stdio",
        "command": "jmunch-mcp",
        "args": ["--config", str(toml_path)],
        "env": {},
    }

    bak = cfg.with_suffix(cfg.suffix + ".bak")
    if not bak.exists():
        bak.write_text(cfg.read_text(encoding="utf-8"), encoding="utf-8")
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"ok": True, "status": "installed",
            "config": str(cfg), "server_key": server_key}


# ---------------------------------------------------------------------------
# HTML asset — lives next to this file so we can ship it in the wheel.
# ---------------------------------------------------------------------------


def _html_path() -> Path:
    return Path(__file__).parent / "dashboard.html"


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


def _make_handler(db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # quiet
            log.debug(fmt, *args)

        def _send_json(self, payload, status: int = 200) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, body: str, content_type: str, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            path = url.path

            if path in ("/", "/index.html"):
                html = _html_path()
                if html.exists():
                    self._send_text(html.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                else:
                    self._send_text("<h1>dashboard.html missing</h1>", "text/html", 500)
                return

            qs = parse_qs(url.query)
            surface = qs.get("surface", [None])[0]
            if surface not in (None, "all", "mcp", "gateway"):
                surface = None

            if path == "/api/stats":
                self._send_json(_stats_payload(db_path, surface=surface))
                return

            if path == "/api/servers":
                self._send_json(_server_payload())
                return

            if path == "/api/calls":
                limit = int(qs.get("limit", ["100"])[0])
                self._send_json(_calls_payload(db_path, limit, surface=surface))
                return

            if path == "/api/versions":
                self._send_json(_versions_payload())
                return

            if path == "/api/export.csv":
                csv_body = _export_csv(db_path)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 'attachment; filename="jmunch-metrics.csv"')
                self.send_header("Content-Length", str(len(csv_body.encode("utf-8"))))
                self.end_headers()
                self.wfile.write(csv_body.encode("utf-8"))
                return

            self._send_text("not found", "text/plain", 404)

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                self._send_json({"ok": False, "error": "invalid JSON"}, status=400)
                return
            if not isinstance(body, dict):
                self._send_json({"ok": False, "error": "body must be an object"}, status=400)
                return

            if url.path == "/api/optimize":
                result = _do_optimize(body)
                self._send_json(result, status=200 if result.get("ok") else 400)
                return

            if url.path == "/api/install":
                result = _do_install(body)
                self._send_json(result, status=200 if result.get("ok") else 400)
                return

            if url.path == "/api/clear-stats":
                n = metrics.clear_all(db_path)
                self._send_json({"ok": True, "cleared": n}, status=200)
                return

            self._send_json({"ok": False, "error": "unknown endpoint"}, status=404)

    return Handler


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


class _ReuseServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jmunch-mcp dashboard",
        description="Local read-only dashboard for jmunch-mcp proxies.",
    )
    p.add_argument("--port", type=int, default=7878, help="Listen port (default: 7878)")
    p.add_argument("--host", default="127.0.0.1", help="Listen host (default: 127.0.0.1)")
    p.add_argument("--db", type=Path, default=None, help="Override metrics DB path")
    p.add_argument("--open", dest="open_browser", action="store_true",
                   help="Open the dashboard in your default browser")
    return p


def serve(host: str, port: int, db_path: Path, open_browser: bool = False) -> int:
    handler = _make_handler(db_path)
    server = _ReuseServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"jmunch-mcp dashboard listening on {url}")
    print(f"  metrics db: {db_path}")
    print("  Ctrl-C to quit.")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print("shutting down")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = args.db or metrics.default_db_path()
    return serve(args.host, args.port, db_path, open_browser=args.open_browser)
