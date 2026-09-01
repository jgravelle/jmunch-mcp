"""jMRI _meta envelope + savings persistence.

Every jmunch-mcp response (result or error) MUST be wrapped via `envelope()`.
Token accounting follows the jMRI spec: bytes/4, no tokenizer dependency.
Cumulative `total_tokens_saved` is persisted to ~/.jmunch/_savings.json.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__

BYTES_PER_TOKEN = 4  # jMRI spec: conservative, zero-overhead approximation

RETRIEVAL_ENGINE = "jmunch"
RETRIEVAL_VERSION = "1.0"
POWERED_BY = "jmunch-mcp by jgravelle · https://github.com/jgravelle/jmunch-mcp"

# Per-model USD per 1M INPUT tokens. Verified 2026-09-01 against
# platform.claude.com/docs/en/about-claude/pricing.
# ⚠⚠ A key that names a FAMILY inherits whichever member's price someone last
# looked at. Price the CURRENT member and name it; a superseded member's rate
# goes in the comment, never in the value.
# Keys are wire-visible in _meta.cost_avoided — do not rename them.
_MODEL_PRICES_PER_1M: dict[str, float] = {
    # Claude Opus 5 (also 4.8/4.7/4.6). Superseded: Opus 4.1/4.0 were $15.00.
    "claude_opus": 5.00,
    # Claude Sonnet 5. The $2 launch price became standard on 2026-08-31; the
    # increase to $3 scheduled for 2026-09-01 was cancelled. Superseded:
    # Sonnet 4.6/4.5 are $3.00.
    "claude_sonnet": 2.00,
    # Not an Anthropic model; value unverified against any first-party source.
    "gpt5_latest": 10.00,
}


def estimate_tokens(n_bytes: int) -> int:
    return max(0, n_bytes // BYTES_PER_TOKEN)


def estimate_savings(raw_bytes: int, response_bytes: int) -> int:
    return max(0, (raw_bytes - response_bytes) // BYTES_PER_TOKEN)


def cost_avoided(tokens: int) -> dict[str, float]:
    return {k: round(tokens * price / 1_000_000, 4) for k, price in _MODEL_PRICES_PER_1M.items()}


class SavingsTracker:
    """Persists cumulative tokens saved across process restarts."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path.home() / ".jmunch" / "_savings.json")
        self._lock = threading.Lock()
        self._total_tokens_saved = 0
        self._total_cost_avoided: dict[str, float] = {k: 0.0 for k in _MODEL_PRICES_PER_1M}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._total_tokens_saved = int(data.get("total_tokens_saved", 0))
            stored_cost = data.get("total_cost_avoided", {})
            for k in self._total_cost_avoided:
                self._total_cost_avoided[k] = float(stored_cost.get(k, 0.0))
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "total_tokens_saved": self._total_tokens_saved,
                    "total_cost_avoided": self._total_cost_avoided,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def record(self, tokens_saved: int) -> tuple[int, dict[str, float]]:
        with self._lock:
            self._total_tokens_saved += tokens_saved
            delta_cost = cost_avoided(tokens_saved)
            for k, v in delta_cost.items():
                self._total_cost_avoided[k] = round(self._total_cost_avoided[k] + v, 4)
            self._persist()
            return self._total_tokens_saved, dict(self._total_cost_avoided)

    @property
    def total(self) -> int:
        return self._total_tokens_saved


def envelope(
    *,
    result: Any = None,
    error: dict[str, Any] | None = None,
    raw_bytes: int,
    response_bytes: int,
    tracker: SavingsTracker,
    timing_ms: float | None = None,
) -> dict[str, Any]:
    """Wrap a result or error in the jMRI response envelope.

    `raw_bytes` is the original upstream payload size; `response_bytes` is the
    compact response we're about to emit. For pure passthrough responses, pass
    equal values — savings will be zero.
    """
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved, total_cost = tracker.record(tokens_saved)

    meta: dict[str, Any] = {
        "tokens_saved": tokens_saved,
        "total_tokens_saved": total_saved,
        "response_tokens": estimate_tokens(response_bytes),
        "naive_tokens": estimate_tokens(raw_bytes),
        "cost_avoided": cost_avoided(tokens_saved),
        "total_cost_avoided": total_cost,
        "retrieval_engine": RETRIEVAL_ENGINE,
        "retrieval_version": RETRIEVAL_VERSION,
        "jmunch_version": __version__,
        "powered_by": POWERED_BY,
    }
    if timing_ms is not None:
        meta["timing_ms"] = round(timing_ms, 2)

    out: dict[str, Any] = {"_meta": meta}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return out


def timer_ms(start_ns: int) -> float:
    return (time.perf_counter_ns() - start_ns) / 1_000_000
