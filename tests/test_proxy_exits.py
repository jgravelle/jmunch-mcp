"""The proxy exits when its client or its upstream is gone, and takes the
upstream with it.

Before this, the proxy ended only on a stdin EOF, and even that could hang:
the stdin reader ran on asyncio's default executor, and `asyncio.run` waits
for that executor at shutdown, so an upstream that exited while the client
kept the pipe open left the proxy blocked in `readline` forever. A client
that died with the pipe held open (a proxy in front, a handle a sibling
inherited) left both the proxy and its upstream running with nobody to
answer.

Each test runs the real entry point against a fake upstream, with HOME and
the metrics database pointed into the test's own directory.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from jmunch_mcp import lifeline

SRC = str(Path(__file__).resolve().parents[1] / "src")

# Upstream that runs until its stdin closes, after writing its pid.
_UPSTREAM_FOREVER = textwrap.dedent(
    """
    import os, sys
    open(sys.argv[1], "w").write(str(os.getpid()))
    for _ in sys.stdin:
        pass
    """
)

# Upstream that exits on its own after a moment.
_UPSTREAM_EXITS = textwrap.dedent(
    """
    import os, sys, time
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(1)
    """
)

# Starts the proxy with its stdin inherited from this script, which the
# test holds open, then sleeps until killed.
_PARENT = textwrap.dedent(
    """
    import subprocess, sys, time
    child = subprocess.Popen(
        [sys.executable, "-m", "jmunch_mcp", "--config", sys.argv[1]],
        stdout=subprocess.DEVNULL, stderr=open(sys.argv[2], "w"),
    )
    print(child.pid, flush=True)
    time.sleep(600)
    """
)


def _alive(pid: int) -> bool:
    return lifeline._is_alive(pid)


def _wait_until(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def _kill(pid: int, tree: bool = True) -> None:
    """Kill ``pid``; with ``tree`` (cleanup only) its children too. A test of
    the parent dying must kill the parent alone, or the proxy dies with it."""
    try:
        if sys.platform == "win32":
            flags = ["/T"] if tree else []
            subprocess.run(["taskkill", "/F", *flags, "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def _env(tmp_path: Path, interval: str = "1") -> dict:
    env = dict(os.environ)
    env.update(
        PYTHONPATH=SRC,
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        JMUNCH_METRICS_DB=str(tmp_path / "metrics.db"),
        JMUNCH_PARENT_WATCH_SECONDS=interval,
    )
    return env


def _config(tmp_path: Path, upstream_script: str) -> Path:
    script = tmp_path / "upstream.py"
    script.write_text(upstream_script)
    pid_file = tmp_path / "upstream.pid"
    cfg = tmp_path / "proxy.toml"
    py = sys.executable.replace("\\", "/")
    cfg.write_text(
        "[upstream]\n"
        f'command = "{py}"\n'
        f'args = ["{script.as_posix()}", "{pid_file.as_posix()}"]\n'
    )
    return cfg


def _upstream_pid(tmp_path: Path) -> int:
    f = tmp_path / "upstream.pid"
    assert _wait_until(lambda: f.is_file() and f.read_text().strip(), 30), "upstream never started"
    return int(f.read_text())


def _err(tmp_path: Path) -> str:
    try:
        return (tmp_path / "proxy.err").read_text()
    except OSError:
        return ""


def _start_proxy(tmp_path: Path, upstream_script: str) -> subprocess.Popen:
    cfg = _config(tmp_path, upstream_script)
    return subprocess.Popen(
        [sys.executable, "-m", "jmunch_mcp", "--config", str(cfg)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=open(tmp_path / "proxy.err", "w"),
        cwd=str(tmp_path),
        env=_env(tmp_path),
    )


def _start_parent(tmp_path: Path, upstream_script: str, interval: str = "1"):
    cfg = _config(tmp_path, upstream_script)
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT, str(cfg), str(tmp_path / "proxy.err")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        cwd=str(tmp_path),
        env=_env(tmp_path, interval),
    )
    line = parent.stdout.readline().decode().strip()
    assert line.isdigit(), f"parent printed {line!r}"
    return parent, int(line)


# -- integration -------------------------------------------------------------


def test_the_proxy_exits_when_its_upstream_exits_with_stdin_open(tmp_path):
    """The stdin reader must not hold shutdown."""
    proxy = _start_proxy(tmp_path, _UPSTREAM_EXITS)
    try:
        _upstream_pid(tmp_path)
        assert _wait_until(lambda: proxy.poll() is not None, 20), _err(tmp_path)
    finally:
        _kill(proxy.pid)
        proxy.stdin.close()


def test_a_closed_stdin_still_ends_the_proxy_and_its_upstream(tmp_path):
    proxy = _start_proxy(tmp_path, _UPSTREAM_FOREVER)
    try:
        upstream = _upstream_pid(tmp_path)
        proxy.stdin.close()
        assert _wait_until(lambda: proxy.poll() is not None, 20), _err(tmp_path)
        assert _wait_until(lambda: not _alive(upstream), 10), "upstream left running"
    finally:
        _kill(proxy.pid)


def test_a_proxy_whose_parent_is_killed_exits_and_stops_its_upstream(tmp_path):
    parent, proxy = _start_parent(tmp_path, _UPSTREAM_FOREVER)
    upstream = None
    try:
        upstream = _upstream_pid(tmp_path)
        assert _alive(proxy), _err(tmp_path)
        _kill(parent.pid, tree=False)
        parent.wait(10)
        # parent.stdin stays open in this process: no EOF reaches the proxy.
        assert _wait_until(lambda: not _alive(proxy), 30), _err(tmp_path)
        assert _wait_until(lambda: not _alive(upstream), 10), "upstream left running"
    finally:
        _kill(proxy)
        if upstream:
            _kill(upstream)
        parent.stdin.close()


def test_a_proxy_whose_parent_lives_keeps_running(tmp_path):
    parent, proxy = _start_parent(tmp_path, _UPSTREAM_FOREVER)
    upstream = None
    try:
        upstream = _upstream_pid(tmp_path)
        time.sleep(4)  # several 1 s polls
        assert _alive(proxy), _err(tmp_path)
        assert _alive(upstream)
    finally:
        _kill(parent.pid)
        _kill(proxy)
        if upstream:
            _kill(upstream)
        parent.stdin.close()


# -- unit --------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [(None, 5.0), ("", 5.0), ("2.5", 2.5), ("0", 0.0), ("-1", 0.0), ("five", 5.0)],
)
def test_the_poll_interval(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(lifeline.ENV, raising=False)
    else:
        monkeypatch.setenv(lifeline.ENV, raw)
    assert lifeline.interval_seconds() == expected


def test_the_watch_is_off_at_zero(monkeypatch):
    monkeypatch.setenv(lifeline.ENV, "0")
    assert lifeline.start(lambda: None) is None


def test_a_live_parent_is_not_gone():
    w = lifeline.Watch.of_parent()
    assert w is not None
    assert w.gone() is False


def test_a_changed_parent_is_gone(monkeypatch):
    """Unix reparents an orphan; a parent that changed since import is gone."""
    w = lifeline.Watch.of_parent()
    assert w is not None
    monkeypatch.setattr(lifeline.os, "getppid", lambda: lifeline.ORIGIN_PPID + 1)
    assert w.gone() is True


def test_a_failed_query_is_not_gone(monkeypatch):
    """Uncertainty never exits: a process we cannot query may be alive."""
    w = lifeline.Watch.of_parent()
    assert w is not None
    monkeypatch.setattr(lifeline, "_is_alive", lambda pid: False)
    monkeypatch.setattr(lifeline, "_no_such_process", lambda pid: False)
    assert w.gone() is False


def test_a_check_that_raises_is_not_gone(monkeypatch):
    w = lifeline.Watch.of_parent()
    assert w is not None

    def boom(pid):
        raise OSError("query failed")

    monkeypatch.setattr(lifeline, "_is_alive", boom)
    assert w.gone() is False


def test_the_disclosed_interval_is_the_default():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert f"every {int(lifeline.DEFAULT_SECONDS)} seconds" in readme
    assert lifeline.ENV in readme
