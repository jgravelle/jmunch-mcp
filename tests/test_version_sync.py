"""Every version pin site must agree with `pyproject.toml`.

Adding `server.json` on 2026-09-01 took this repo from one pin site to two, and
a second site is where release checklists rot: the sibling repos' checklist read
"pyproject AND server.json" straight through the release where a third site had
already appeared. A test does not rot.

`server.json` carries the version TWICE — top level and inside `packages[]` — and
a publish can advance one without the other, which the registry accepts and
displays.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "no version found in pyproject.toml"
    return m.group(1)


def test_server_json_matches_pyproject():
    expected = _pyproject_version()
    server = json.loads((REPO / "server.json").read_text(encoding="utf-8"))

    assert server["version"] == expected, (
        f"server.json version {server['version']!r} != pyproject {expected!r}. "
        "Bump every pin site, not just pyproject.toml."
    )
    for pkg in server["packages"]:
        assert pkg["version"] == expected, (
            f"server.json packages[].version {pkg['version']!r} != pyproject "
            f"{expected!r}. The registry accepts one moving without the other."
        )


def test_server_json_identifies_the_right_package():
    server = json.loads((REPO / "server.json").read_text(encoding="utf-8"))
    assert server["name"] == "io.github.jgravelle/jmunch-mcp"
    assert [p["identifier"] for p in server["packages"]] == ["jmunch-mcp"]


def test_every_pin_site_is_known():
    """Fails when a NEW file starts carrying the version.

    The point of the sibling checklist's warning is that the list grows silently.
    This turns 'grep for the old version' from a step someone must remember into
    one the suite performs.
    """
    version = _pyproject_version()
    known = {"pyproject.toml", "server.json"}
    found: set[str] = set()

    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in {".toml", ".json", ".lock"}:
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith((".git/", "dist/", "build/", ".venv/", ".pytest_cache/")):
            continue
        if "egg-info" in rel or ".local." in rel:
            continue
        try:
            if version in path.read_text(encoding="utf-8"):
                found.add(rel)
        except (UnicodeDecodeError, OSError):
            continue

    unexpected = found - known
    assert not unexpected, (
        f"New version pin site(s): {sorted(unexpected)}. Add them to RELEASING.md "
        "step 1 and to `known` here, or the next bump will miss them."
    )


# ---------------------------------------------------------------------------
# Registry schema constraints
#
# Restated from the ServerDetail definition in
# https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json
# read 2026-09-01. Literals on purpose: fetching the schema at test time would
# make the suite depend on the network, and a pin that reads the value it checks
# asserts nothing.
#
# The publish is typed by a human and the JWT lives five minutes, so a rejection
# there costs a full round trip. On 2026-09-01 the first attempt failed with
# 422 on a 141-character description. These run in milliseconds instead.
# ---------------------------------------------------------------------------

_MAX_DESCRIPTION = 100
_MAX_NAME = 200
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
_REQUIRED = ("name", "description", "version")


def _server_json() -> dict:
    return json.loads((REPO / "server.json").read_text(encoding="utf-8"))


def test_server_json_meets_registry_constraints():
    server = _server_json()

    for field in _REQUIRED:
        assert server.get(field), f"registry requires a non-empty {field!r}"

    desc = server["description"]
    assert len(desc) <= _MAX_DESCRIPTION, (
        f"description is {len(desc)} chars; the registry rejects anything over "
        f"{_MAX_DESCRIPTION} with a 422. Shorten it before publishing."
    )

    name = server["name"]
    assert len(name) <= _MAX_NAME
    assert _NAME_PATTERN.match(name), (
        f"name {name!r} must match {_NAME_PATTERN.pattern} - "
        "'<namespace>/<server>', e.g. io.github.jgravelle/jmunch-mcp"
    )


def test_readme_carries_the_registry_ownership_marker():
    """The registry proves you own the PyPI name by looking for this marker in
    the PUBLISHED package README - not in the GitHub one.

    Publishing 0.2.2 failed 400 without it:
      "PyPI package 'jmunch-mcp' ownership validation failed. The server name
       must appear as 'mcp-name: ...' in the package README"

    The consequence is the expensive part: PyPI versions are immutable, so
    adding the marker means cutting a NEW release, not editing a file. Keeping
    it in step with server.json costs nothing here and a whole version bump if
    it drifts.
    """
    name = _server_json()["name"]
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    marker = f"<!-- mcp-name: {name} -->"
    assert marker in readme, (
        f"README.md must contain {marker!r} for registry ownership validation. "
        "Without it, `mcp-publisher publish` fails 400 - and fixing it after a "
        "release requires another release, because PyPI versions are immutable."
    )
