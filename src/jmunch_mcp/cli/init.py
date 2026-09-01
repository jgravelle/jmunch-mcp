"""`jmunch-mcp init` — one-command onboarding.

Scans the user's system for MCP servers (client configs + running
processes + catalog), lets them pick which to wrap, and writes one
`<name>.toml` per selected upstream. Optionally rewrites the originating
client config to launch jmunch-mcp in front of the upstream instead of
the upstream directly.

Designed to be useful non-interactively (`--yes`) and safe (`--dry-run`,
`.bak` files on any write).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Iterable

from .discovery import Candidate, Discovery, discover
from .rewrite import apply_rewrite, plan_rewrite, render_diff

# ---------------------------------------------------------------------------
# TOML rendering (stdlib tomllib is read-only; we hand-render a tiny subset)
# ---------------------------------------------------------------------------


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _render_toml(c: Candidate, *, threshold_tokens: int = 2000) -> str:
    lines: list[str] = []
    lines.append(f"# jmunch-mcp config for '{c.name}' upstream.")
    if c.description:
        lines.append(f"# {c.description}")
    if c.env_keys:
        required = ", ".join(c.env_keys)
        lines.append(f"# Requires {required} in your environment.")
    lines.append("")
    # Top-level keys MUST precede any [table] header in TOML.
    lines.append(f"threshold_tokens = {threshold_tokens}")
    lines.append('log_level = "INFO"')
    lines.append("report = false")
    lines.append("")
    lines.append("[upstream]")
    lines.append(f'command = "{_toml_escape(c.command)}"')
    args_lit = ", ".join(f'"{_toml_escape(a)}"' for a in c.args)
    lines.append(f"args = [{args_lit}]")
    lines.append("")
    return "\n".join(lines)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    return _SAFE_NAME.sub("-", name).strip("-") or "upstream"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _prompt_checklist(
    candidates: list[Candidate],
    *,
    preselect_sources: Iterable[str] = ("client:",),
) -> list[Candidate]:
    """Render a numbered checklist. Items matching a preselect source prefix
    start ticked. User types space/comma-separated numbers, `all`, or `none`
    to edit the selection. Hit enter to accept.
    """
    picked = [
        any(c.source.startswith(p) for p in preselect_sources)
        for c in candidates
    ]

    def render() -> None:
        print()
        print("Found MCP upstreams:")
        for i, c in enumerate(candidates, 1):
            tick = "[x]" if picked[i - 1] else "[ ]"
            detail = c.description or f"{c.command} {' '.join(c.args)}"
            print(f"  {tick} {i:>2}. {c.name}  — {detail}")
            print(f"          source: {c.source}")

    while True:
        render()
        try:
            raw = input(
                "\nToggle numbers (e.g. '1 3 5'), 'all', 'none', or Enter to accept: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        if raw == "":
            break
        if raw == "all":
            picked = [True] * len(candidates)
            continue
        if raw == "none":
            picked = [False] * len(candidates)
            continue
        for part in raw.replace(",", " ").split():
            try:
                idx = int(part) - 1
            except ValueError:
                continue
            if 0 <= idx < len(candidates):
                picked[idx] = not picked[idx]

    return [c for c, p in zip(candidates, picked) if p]


def _prompt_yn(message: str, default: bool = True) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        answer = input(message + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------


def _write_toml(
    target: Path,
    content: str,
    *,
    dry_run: bool,
    backup: bool,
    overwrite: bool,
) -> str:
    if target.exists() and not overwrite:
        return f"  exists (skipped): {target}"
    if dry_run:
        return f"  would write: {target}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if backup and target.exists():
        shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
    target.write_text(content, encoding="utf-8")
    return f"  wrote: {target}"


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def run_init(
    *,
    out_dir: Path | None = None,
    yes: bool = False,
    dry_run: bool = False,
    no_backup: bool = False,
    no_running: bool = False,
    no_catalog: bool = False,
    threshold_tokens: int = 2000,
    overwrite: bool = False,
    rewrite: bool | None = None,  # None = ask (interactive) / skip (non-interactive)
) -> int:
    backup = not no_backup
    interactive = not yes and sys.stdin.isatty()

    out_dir = out_dir or Path.cwd() / "configs"

    print()
    print("jmunch-mcp init — wrap your upstream MCP servers")
    print()

    disco: Discovery = discover(
        include_catalog=not no_catalog,
        include_running=not no_running,
    )
    if not disco.candidates:
        print("  No MCP servers detected anywhere. Nothing to configure.")
        print("  Re-run with --no-catalog=false or install an MCP client first.")
        return 1

    # Summarize sources
    by_client = sum(1 for c in disco.candidates if c.source.startswith("client:"))
    running = sum(1 for c in disco.candidates if c.source == "running")
    catalog = sum(1 for c in disco.candidates if c.source == "catalog")
    print(f"  sources: {by_client} from client configs, {running} running, {catalog} from catalog")

    if interactive:
        selected = _prompt_checklist(disco.candidates)
    else:
        # Non-interactive: pick everything already registered in a client;
        # skip catalog entries unless the user really asked for all.
        selected = [c for c in disco.candidates if c.source.startswith("client:")]
        if not selected:
            # Nothing registered — fall back to all running processes.
            selected = [c for c in disco.candidates if c.source == "running"]

    if not selected:
        print("\n  Nothing selected — exiting.")
        return 0

    print()
    print(f"  output directory: {out_dir}")
    print()
    toml_paths: dict[str, Path] = {}  # signature → toml path, for rewrite step
    for c in selected:
        fname = f"{_safe_filename(c.name)}.toml"
        target = out_dir / fname
        content = _render_toml(c, threshold_tokens=threshold_tokens)
        msg = _write_toml(target, content, dry_run=dry_run, backup=backup, overwrite=overwrite)
        print(msg)
        toml_paths[c.name] = target.resolve()
        # Remind about required env vars
        if c.env_keys:
            print(f"    env required: {', '.join(c.env_keys)}")

    # ----- client-config rewrite (the onboarding-completer) -----
    rewrite_candidates = [
        c for c in selected
        if c.source_path is not None and c.server_key
    ]
    touched_clients: set[Path] = set()
    if rewrite_candidates and not dry_run:
        plans = []
        for c in rewrite_candidates:
            plan = plan_rewrite(c, toml_paths[c.name])
            plans.append((c, plan))

        actionable = [(c, p) for c, p in plans if p.status == "rewrote"]
        if actionable:
            print()
            print("Client config rewrite — the following entries would be switched to jmunch-mcp:")
            for c, plan in actionable:
                print(render_diff(plan))

            do_rewrite = rewrite
            if do_rewrite is None:
                if interactive:
                    do_rewrite = _prompt_yn(
                        "\nRewrite these client configs now? (.bak will be written)",
                        default=True,
                    )
                else:
                    do_rewrite = False  # safe non-interactive default
                    print("\n  (skipped — pass --rewrite to enable in non-interactive mode)")

            if do_rewrite:
                print()
                for c, _plan in actionable:
                    result = apply_rewrite(c, toml_paths[c.name])
                    if result.status == "rewrote":
                        print(f"  rewrote: {result.path}  [{result.server_key}]")
                        touched_clients.add(result.path)
                    else:
                        print(f"  {result.status}: {result.path}  [{result.server_key}]")

        # Report anything skipped so the user knows why
        skipped = [(c, p) for c, p in plans if p.status != "rewrote"]
        if skipped:
            print()
            print("Skipped (no rewrite needed):")
            for c, p in skipped:
                print(f"  {p.status}: {c.source_path}  [{c.server_key}]")

    # Usage hint
    print()
    if touched_clients:
        print("Restart your MCP client(s) to pick up the new config:")
        for p in sorted(touched_clients):
            print(f"  {p}")
        print()
        print("To undo: restore from the .bak file alongside each config.")
    else:
        print("Launch any wrapped upstream with:")
        example = selected[0]
        example_path = out_dir / f"{_safe_filename(example.name)}.toml"
        print(f"  jmunch-mcp --config {example_path}")
        print()
        print(
            "To wire one of these into an MCP client manually, replace the client's entry "
            "for that server so its command becomes `jmunch-mcp` with `--config <path>`."
        )

    if dry_run:
        print("\nDry run complete — no files were written.")
    return 0


# ---------------------------------------------------------------------------
# argparse helper — called from __main__.py
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jmunch-mcp init",
        description="Scan for MCP servers and generate wrapper configs.",
    )
    p.add_argument("--out", type=Path, default=None,
                   help="Directory for generated .toml files (default: ./configs)")
    p.add_argument("--yes", action="store_true",
                   help="Non-interactive: pick all servers already registered in a client")
    p.add_argument("--dry-run", action="store_true", help="Print what would happen")
    p.add_argument("--no-backup", action="store_true", help="Don't write .bak on overwrite")
    p.add_argument("--no-running", action="store_true", help="Skip running-process scan")
    p.add_argument("--no-catalog", action="store_true", help="Skip the built-in catalog")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing .toml files")
    p.add_argument("--threshold-tokens", type=int, default=2000,
                   help="Handle-ification threshold to write into each config")
    rw = p.add_mutually_exclusive_group()
    rw.add_argument("--rewrite", dest="rewrite", action="store_true", default=None,
                    help="Rewrite client configs to launch jmunch-mcp in front of each upstream")
    rw.add_argument("--no-rewrite", dest="rewrite", action="store_false",
                    help="Never rewrite client configs (default in non-interactive mode)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_init(
        out_dir=args.out,
        yes=args.yes,
        dry_run=args.dry_run,
        no_backup=args.no_backup,
        no_running=args.no_running,
        no_catalog=args.no_catalog,
        threshold_tokens=args.threshold_tokens,
        overwrite=args.overwrite,
        rewrite=args.rewrite,
    )
