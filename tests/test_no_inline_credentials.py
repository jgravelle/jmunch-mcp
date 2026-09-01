"""No tracked config may carry an inline credential.

On 2026-09-01 `configs/brave-search.toml` was found holding a live
`BRAVE_API_KEY` in an `[upstream.env]` block. It was never committed and never
published — but only because the working tree happened to be stashed before the
release build. Hatchling builds from the working tree, so nothing structural
stopped it; `configs/` ships in the sdist.

The sdist allowlist in `pyproject.toml` is the boundary at build time. This is
the check one step earlier, at the source, where the fix is free: a real secret
in a tracked config fails the suite the moment it is written.

Real credentials belong in `configs/*.local.toml`, which is gitignored.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

try:  # stdlib on 3.11+; the package already depends on tomli below that
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib  # type: ignore[no-redef]

REPO = Path(__file__).resolve().parents[1]

# Key names whose VALUE would be the secret itself.
_SECRET_KEY = re.compile(r"key|token|secret|password|passwd|credential|auth", re.I)

# A bare environment-variable NAME is a reference, not a secret:
# `api_key_env = "OPENAI_API_KEY"` names where to read the key from.
_ENV_VAR_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

_PLACEHOLDER = re.compile(
    r"^(<.*>|\$\{.*\}|your[-_ ]|change[-_ ]?me|example|placeholder|x{3,}|\.\.\.)",
    re.I,
)


def _tracked_config_files() -> list[Path]:
    """Only files git tracks. A gitignored *.local.toml is where secrets belong,
    so scanning it would fail the suite for doing the right thing."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "configs", "examples"],
            cwd=REPO, capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git unavailable — cannot determine tracked files")
    return [REPO / line for line in out.splitlines() if line.endswith(".toml")]


def _walk(node: Any, trail: tuple[str, ...] = ()) -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, trail + (str(k),))
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, trail)
    elif isinstance(node, str):
        yield ".".join(trail), node


def _looks_like_a_secret(key_path: str, value: str) -> bool:
    leaf = key_path.rsplit(".", 1)[-1]
    if not _SECRET_KEY.search(leaf):
        return False
    if len(value) < 12:
        return False
    if _ENV_VAR_NAME.match(value) or _PLACEHOLDER.match(value):
        return False
    return True


def test_no_tracked_config_contains_an_inline_credential():
    offenders: list[str] = []
    for path in _tracked_config_files():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for key_path, value in _walk(data):
            if _looks_like_a_secret(key_path, value):
                rel = path.relative_to(REPO).as_posix()
                offenders.append(f"{rel}: {key_path} = <redacted, {len(value)} chars>")

    assert not offenders, (
        "Inline credential in a tracked config - these ship in the sdist:\n  "
        + "\n  ".join(offenders)
        + "\nMove it to configs/<name>.local.toml (gitignored) and pass it with --config."
    )


def test_detector_catches_the_shape_that_got_through():
    """The 2026-09-01 miss, and the references that must NOT trip it."""
    assert _looks_like_a_secret("upstream.env.BRAVE_API_KEY", "BSA4gi" + "x" * 25)
    assert _looks_like_a_secret("upstream.env.GITHUB_TOKEN", "ghp_" + "a" * 36)

    assert not _looks_like_a_secret("upstream.env.OPENAI_API_KEY", "OPENAI_API_KEY")
    assert not _looks_like_a_secret("api_key_env", "ANTHROPIC_API_KEY")
    assert not _looks_like_a_secret("upstream.env.JDOCMUNCH_EMBED_WARMUP", "0")
    assert not _looks_like_a_secret("command", "jdocmunch-mcp")
    assert not _looks_like_a_secret("upstream.env.API_KEY", "<your-key-here>")


def test_local_overrides_are_gitignored():
    """The escape hatch must actually be ignored, or the advice is a trap."""
    probe = "configs/brave-search.local.toml"
    result = subprocess.run(
        ["git", "check-ignore", probe],
        cwd=REPO, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"{probe} is NOT gitignored. Tests tell people to put real credentials "
        "there, so it must be ignored before that advice is safe."
    )
