"""Notice when the client that started this proxy is gone.

The proxy ends on a stdin EOF. A client that dies while something else keeps
the pipe open (a proxy in front of us, a handle a sibling inherited) sends no
EOF, and the proxy and its upstream then run on with nobody to answer.

A daemon thread checks the parent every ``JMUNCH_PARENT_WATCH_SECONDS``
(default 5, ``0`` turns it off) and calls back once it is gone.

⚠⚠ Uncertainty never exits. A false "gone" kills a live client's tools, which
is worse than a proxy that lingers. A parent that cannot be queried at start
turns the watch off, and a check that fails or raises counts as alive.
⚠ On Unix an orphan is reparented, so a parent PID that changed since import
is the whole answer. Windows never reparents and reuses PIDs, so there the
parent is identified by PID plus creation time.
⚠ On Windows a pip console script is a launcher ``.exe`` that starts Python
and waits, and a venv's ``python.exe`` is a redirector that does the same.
Either outlives its own client, so the watch steps past them to the process
that started them.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable

log = logging.getLogger("jmunch.lifeline")

# The parent as it was when this module was imported (proxy.py imports it at
# start-up). A client that dies before the watch starts shows up as a change.
ORIGIN_PPID = os.getppid()

DEFAULT_SECONDS = 5.0
ENV = "JMUNCH_PARENT_WATCH_SECONDS"

_RUNNER_NAMES = frozenset({"uv.exe", "uvx.exe", "py.exe", "pyw.exe"})


def interval_seconds() -> float:
    """The poll interval; 0 means off. A value that does not parse keeps the
    default, so a typo cannot switch the watch off."""
    raw = os.environ.get(ENV)
    if raw is None or not raw.strip():
        return DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        log.debug("bad %s=%r, using the default", ENV, raw)
        return DEFAULT_SECONDS
    return value if value > 0 else 0.0


# -- process queries ---------------------------------------------------------


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetExitCodeProcess.restype = wintypes.BOOL
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k.GetProcessTimes.restype = wintypes.BOOL
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    k.QueryFullProcessImageNameW.restype = wintypes.BOOL
    return k


_QUERY = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
_STILL_ACTIVE = 259


def _is_alive(pid: int) -> bool:
    """True when ``pid`` names a running process. False also for a process we
    may not query, which is why ``gone`` asks ``_no_such_process`` too."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k = _kernel32()
        h = k.OpenProcess(_QUERY, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, but not ours to signal
    return True


def _no_such_process(pid: int) -> bool:
    """True only when the OS says no process has this PID, or the process has
    exited. A process we may not open is NOT reported gone."""
    if sys.platform == "win32":
        import ctypes

        k = _kernel32()
        h = k.OpenProcess(_QUERY, False, pid)
        if h:
            k.CloseHandle(h)
            return True  # openable but not alive: exited, a handle keeps it listed
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: no such PID
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _create_time(pid: int) -> int | None:
    """Windows creation time (FILETIME ticks), or None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    k = _kernel32()
    h = k.OpenProcess(_QUERY, False, pid)
    if not h:
        return None
    try:
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
            return None
        return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
    finally:
        k.CloseHandle(h)


def _image_path(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    k = _kernel32()
    h = k.OpenProcess(_QUERY, False, pid)
    if not h:
        return None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        return buf.value or None
    finally:
        k.CloseHandle(h)


def _parent_of(pid: int) -> int | None:
    """Parent PID from a Toolhelp snapshot, or None."""
    import ctypes
    from ctypes import wintypes

    class _ENTRY(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ENTRY)]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ENTRY)]
    k.Process32NextW.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return None
    try:
        e = _ENTRY()
        e.dwSize = ctypes.sizeof(_ENTRY)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.th32ProcessID == pid:
                return int(e.th32ParentProcessID)
            ok = k.Process32NextW(snap, ctypes.byref(e))
        return None
    finally:
        k.CloseHandle(snap)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _launcher_images() -> set[str]:
    """Executables that, as our parent, only started us: our own console-script
    ``.exe`` (``sys.argv[0]``) and the venv redirector (``sys.executable`` when
    it differs from the base interpreter, which every real Python process in
    the venv runs instead, so a Python client never matches it)."""
    images: set[str] = set()
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0.lower().endswith(".exe") and os.path.isfile(argv0):
        images.add(_norm(argv0))
    base = getattr(sys, "_base_executable", sys.executable)
    if sys.executable and base and _norm(sys.executable) != _norm(base):
        images.add(_norm(sys.executable))
    return images


def _watched_pid() -> int:
    """The parent, past any Windows launcher that only started us. If a step
    cannot be made the launcher itself is watched, which can only make the
    proxy linger, never exit early."""
    pid = os.getppid()
    if sys.platform != "win32":
        return pid
    try:
        launchers = _launcher_images()
        for _ in range(4):
            image = _image_path(pid)
            if image is None:
                break
            if _norm(image) not in launchers and os.path.basename(image).lower() not in _RUNNER_NAMES:
                break
            up = _parent_of(pid)
            if not up or up <= 1:
                break
            pid = up
    except Exception:
        log.debug("launcher walk failed; watching the direct parent", exc_info=True)
    return pid


# -- the watch ---------------------------------------------------------------


class Watch:
    """One parent process: PID, plus its creation time on Windows."""

    __slots__ = ("anchor", "pid")

    def __init__(self, pid: int, anchor: int | None) -> None:
        self.pid = pid
        self.anchor = anchor

    @classmethod
    def of_parent(cls) -> Watch | None:
        """A watch on our client, or None when there is none to watch (PID 0
        or 1: started by init or already orphaned) or it cannot be queried."""
        try:
            pid = _watched_pid()
            if pid <= 1:
                return None
            if sys.platform == "win32":
                anchor = _create_time(pid)
                if anchor is None:
                    return None  # cannot identify it: never exit on it
                return cls(pid, anchor)
            return cls(pid, None)
        except Exception:
            log.debug("parent cannot be queried; watch off", exc_info=True)
            return None

    def gone(self) -> bool:
        """True only when the parent is known to be gone."""
        try:
            if ORIGIN_PPID > 1 and os.getppid() != ORIGIN_PPID:
                return True  # reparented (Unix): the client is gone
            if not _is_alive(self.pid):
                return _no_such_process(self.pid)
            if self.anchor is not None:
                now = _create_time(self.pid)
                if now is not None and now != self.anchor:
                    return True  # the PID now names another process
        except Exception:
            log.debug("parent check failed; counting it alive", exc_info=True)
        return False


def start(on_gone: Callable[[], None], interval: float | None = None) -> threading.Thread | None:
    """Call ``on_gone`` once, from a daemon thread, when our client is gone.
    Returns the thread, or None when the watch is off or has nothing to watch."""
    seconds = interval_seconds() if interval is None else interval
    if seconds <= 0:
        return None
    watch = Watch.of_parent()
    if watch is None:
        return None
    log.debug("parent watch on pid %s every %ss", watch.pid, seconds)
    stop = threading.Event()

    def _run() -> None:
        while not stop.wait(seconds):
            if watch.gone():
                try:
                    on_gone()
                except Exception:
                    log.debug("parent-gone callback failed", exc_info=True)
                return

    t = threading.Thread(target=_run, name="jmunch-parent-watch", daemon=True)
    t.start()
    return t
