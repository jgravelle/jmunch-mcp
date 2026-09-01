"""Discover MCP servers worth wrapping with jmunch-mcp.

Three sources, merged and de-duplicated by (command, args) signature:

1. **Client configs** — the JSON `mcpServers` entries in Claude Desktop,
   Claude Code CLI, Cursor, Windsurf, and Continue. Anything already
   registered there is presumed relevant.
2. **Running processes** — a best-effort `tasklist` / `ps` scan that
   fingerprints node/npx/uvx/python processes launched with MCP-looking
   arguments. Stdlib only; no psutil dependency.
3. **Catalog** — a small curated list of well-known upstreams as fallback
   when the user hasn't installed any yet.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# --- public types -----------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A discovered upstream MCP server jmunch-mcp could wrap."""

    name: str                       # stable handle, e.g. "github"
    command: str                    # launch command (e.g. "npx")
    args: tuple[str, ...]           # launch args
    env_keys: tuple[str, ...] = ()  # env vars the upstream typically needs
    source: str = "catalog"         # "client:<name>" | "running" | "catalog"
    description: str = ""
    # For client-sourced candidates: the config file + key under mcpServers
    # that this entry came from. Needed to rewrite it in place.
    source_path: Path | None = None
    server_key: str = ""
    # Empty = top-level mcpServers; otherwise the path key under `projects`
    # (Claude Code's per-project section).
    source_project: str = ""

    @property
    def signature(self) -> tuple[str, tuple[str, ...]]:
        return (self.command.lower(), self.args)


# --- catalog ----------------------------------------------------------------

# Small, curated — servers known to emit large responses where jmunch-mcp
# earns its keep. Keep this list focused; broad coverage comes from scanning.
_CATALOG: list[Candidate] = [
    Candidate(
        name="github",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        env_keys=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        description="GitHub issues/PRs/commits/search — official MCP server",
    ),
    Candidate(
        name="firecrawl",
        command="npx",
        args=("-y", "firecrawl-mcp"),
        env_keys=("FIRECRAWL_API_KEY",),
        description="Web scraping, mapping, and search via Firecrawl",
    ),
    Candidate(
        name="filesystem",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem"),
        description="Local filesystem access (large directory listings)",
    ),
    Candidate(
        name="fetch",
        command="uvx",
        args=("mcp-server-fetch",),
        description="HTTP fetch — returns raw page bodies",
    ),
    Candidate(
        name="brave-search",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-brave-search"),
        env_keys=("BRAVE_API_KEY",),
        description="Web + local search via Brave",
    ),
    Candidate(
        name="slack",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-slack"),
        env_keys=("SLACK_BOT_TOKEN", "SLACK_TEAM_ID"),
        description="Slack channel history and search",
    ),
]


# --- client-config scan -----------------------------------------------------


def _appdata(*parts: str) -> Path:
    if platform.system() == "Windows":
        root = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(root, *parts)
    return Path.home().joinpath(*parts)


def _client_config_paths() -> list[tuple[str, Path]]:
    """(client_name, path) for every known MCP client config on this system."""
    paths: list[tuple[str, Path]] = []

    # Claude Desktop
    if platform.system() == "Darwin":
        paths.append(("Claude Desktop",
                      Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"))
    elif platform.system() == "Windows":
        paths.append(("Claude Desktop", _appdata("Claude", "claude_desktop_config.json")))
    else:
        paths.append(("Claude Desktop", Path.home() / ".config/claude/claude_desktop_config.json"))

    # Claude Code (CLI) global config
    paths.append(("Claude Code", Path.home() / ".claude.json"))
    paths.append(("Claude Code", Path.home() / ".claude/settings.json"))

    # Cursor
    paths.append(("Cursor", Path.home() / ".cursor/mcp.json"))

    # Windsurf
    paths.append(("Windsurf", Path.home() / ".windsurf/mcp_config.json"))
    paths.append(("Windsurf", Path.home() / ".codeium/windsurf/mcp_config.json"))

    # Continue
    paths.append(("Continue", Path.home() / ".continue/config.json"))

    return paths


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _name_from_args(command: str, args: list[str]) -> str:
    """Heuristic short name for a detected server."""
    for a in args:
        if a.startswith("-"):
            continue
        # npm package like @modelcontextprotocol/server-github → github
        if "/" in a:
            a = a.rsplit("/", 1)[-1]
        a = a.removeprefix("server-").removeprefix("mcp-").removesuffix("-mcp")
        if a and a not in ("-y", "-n"):
            return a
    return Path(command).stem or "upstream"


def _emit_entries(
    client_name: str, path: Path, servers: dict, found: list[Candidate],
    *, scope: str = "", source_project: str = "",
) -> None:
    for server_name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if not isinstance(command, str):
            continue
        args = tuple(str(a) for a in entry.get("args", []))
        # Entries whose *command* is jmunch-mcp are already wrapped — surface
        # them so the dashboard can show "Optimized" and offer an unwrap.
        # (Earlier we filtered these out entirely, which made wrapped servers
        # vanish from the dashboard — the opposite of what users want.)
        env = entry.get("env") or {}
        env_keys = tuple(env.keys()) if isinstance(env, dict) else ()
        label = f"{client_name}{(' · ' + scope) if scope else ''}"
        found.append(Candidate(
            name=str(server_name) or _name_from_args(command, list(args)),
            command=command,
            args=args,
            env_keys=env_keys,
            source=f"client:{label}",
            description=f"Registered in {label}",
            source_path=path,
            server_key=str(server_name),
            source_project=source_project,
        ))


def scan_client_configs() -> list[Candidate]:
    """Read every known MCP client config and surface its registered servers.
    Covers both top-level `mcpServers` and Claude Code's per-project
    `projects[<path>].mcpServers` shape."""
    found: list[Candidate] = []
    for client_name, path in _client_config_paths():
        if not path.exists():
            continue
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        top = data.get("mcpServers")
        if isinstance(top, dict):
            _emit_entries(client_name, path, top, found)
        # Claude Code stores per-project MCPs under projects["<path>"].mcpServers
        projects = data.get("projects")
        if isinstance(projects, dict):
            for proj_path, proj in projects.items():
                if not isinstance(proj, dict):
                    continue
                p_servers = proj.get("mcpServers")
                if isinstance(p_servers, dict) and p_servers:
                    _emit_entries(client_name, path, p_servers, found,
                                  scope=f"project {proj_path}",
                                  source_project=str(proj_path))
    return found


# --- running-process scan ---------------------------------------------------

_MCP_HINTS = ("mcp", "modelcontextprotocol", "firecrawl", "brave-search")
_LAUNCHERS = ("node", "npx", "uvx", "uv", "python", "python3", "bun", "deno")


def scan_running_processes() -> list[Candidate]:
    """Best-effort: enumerate processes whose command line looks MCP-shaped.

    Stdlib only. Returns [] on platforms we can't scan or when the enum
    command isn't available.
    """
    rows = _ps_rows()
    found: list[Candidate] = []
    for cmdline in rows:
        parts = cmdline.split()
        if not parts:
            continue
        launcher = Path(parts[0]).stem.lower()
        if launcher not in _LAUNCHERS:
            continue
        joined = " ".join(parts).lower()
        if not any(h in joined for h in _MCP_HINTS):
            continue
        if "jmunch-mcp" in joined:
            continue  # don't wrap ourselves
        command = parts[0]
        args = tuple(parts[1:])
        found.append(Candidate(
            name=_name_from_args(command, list(args)),
            command=command,
            args=args,
            source="running",
            description="Detected as a running process",
        ))
    return found


def _ps_rows() -> list[str]:
    """Return one command-line string per process, or [] if we can't enumerate."""
    try:
        if platform.system() == "Windows":
            if not shutil.which("wmic"):
                return []
            out = subprocess.run(
                ["wmic", "process", "get", "CommandLine", "/FORMAT:LIST"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            rows = []
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("CommandLine="):
                    v = line[len("CommandLine="):].strip()
                    if v:
                        rows.append(v)
            return rows
        # POSIX
        if not shutil.which("ps"):
            return []
        out = subprocess.run(
            ["ps", "-eo", "args="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return []


# --- local-toml scan --------------------------------------------------------
#
# `jmunch-mcp init` writes one <name>.toml per selected upstream. Without
# --rewrite, those configs exist on disk but aren't referenced by any client —
# so the dashboard would hide them. Surfacing them as a discovery source lets
# users see which upstreams they've prepared but haven't wired up yet.


def _local_config_dirs() -> list[Path]:
    dirs: list[Path] = []
    home_cfg = Path.home() / ".jmunch" / "configs"
    dirs.append(home_cfg)
    # Also check ./configs under cwd — `init` defaults to writing there.
    cwd_cfg = Path.cwd() / "configs"
    if cwd_cfg != home_cfg:
        dirs.append(cwd_cfg)
    return dirs


def scan_local_configs() -> list[Candidate]:
    """Surface upstream configs the user has prepared on disk but not yet
    wired into any client. Parses each .toml minimally for [upstream]."""
    try:
        import tomllib  # py311+
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            return []

    found: list[Candidate] = []
    for d in _local_config_dirs():
        if not d.is_dir():
            continue
        for toml_path in sorted(d.glob("*.toml")):
            try:
                with toml_path.open("rb") as f:
                    data = tomllib.load(f)
            except (OSError, ValueError):
                continue
            up = data.get("upstream") or {}
            # Skip gateway-shaped files (`[[upstream]]` arrays) and any other
            # TOML that isn't an MCP wrapper config.
            if not isinstance(up, dict):
                continue
            command = up.get("command")
            if not isinstance(command, str):
                continue
            args = tuple(str(a) for a in up.get("args", []) or [])
            name = toml_path.stem
            found.append(Candidate(
                name=name,
                command=command,
                args=args,
                source=f"local-toml:{toml_path}",
                description="Prepared by init — not wired into any client",
            ))
    return found


# --- merge ------------------------------------------------------------------


@dataclass
class Discovery:
    candidates: list[Candidate] = field(default_factory=list)

    def by_source(self, prefix: str) -> list[Candidate]:
        return [c for c in self.candidates if c.source.startswith(prefix)]


def discover(*, include_catalog: bool = True, include_running: bool = True) -> Discovery:
    """Merge the three sources, de-duping by (command, args). Source priority:
    client configs > running processes > catalog (first wins)."""
    buckets: list[list[Candidate]] = [scan_client_configs(), scan_local_configs()]
    if include_running:
        buckets.append(scan_running_processes())
    if include_catalog:
        buckets.append(list(_CATALOG))

    seen: dict[tuple[str, tuple[str, ...]], Candidate] = {}
    order: list[Candidate] = []
    for bucket in buckets:
        for c in bucket:
            if c.signature in seen:
                continue
            seen[c.signature] = c
            order.append(c)

    # Cross-source dedup: a client entry that already wraps a local .toml
    # (command=jmunch-mcp with --config <path>) makes the local-toml row
    # redundant. Drop the local-toml twin so Playwright stops appearing twice.
    wrapped_toml_paths: set[str] = set()
    for c in order:
        if c.command == "jmunch-mcp" and "--config" in c.args:
            idx = list(c.args).index("--config")
            if idx + 1 < len(c.args):
                wrapped_toml_paths.add(str(Path(c.args[idx + 1]).resolve()))
    if wrapped_toml_paths:
        def _keep(c: Candidate) -> bool:
            if not c.source.startswith("local-toml:"):
                return True
            toml_path = c.source.split(":", 1)[1]
            try:
                return str(Path(toml_path).resolve()) not in wrapped_toml_paths
            except OSError:
                return True
        order = [c for c in order if _keep(c)]

    return Discovery(candidates=order)
