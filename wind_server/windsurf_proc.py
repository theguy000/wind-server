"""Detect, stop, and relaunch the Windsurf process."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import psutil


WINDSURF_BINARIES = ("windsurf", "Windsurf")


def _is_windsurf_proc(info: dict) -> bool:
    """Decide whether a psutil process info dict belongs to Windsurf.

    Most Linux builds ship Windsurf as a generic ``electron`` binary
    (e.g. ``/usr/lib/electron39/electron --app=/usr/share/windsurf/...``),
    so neither ``name`` nor ``exe`` mentions Windsurf — only ``cmdline``
    does. We match if any of name/exe/cmdline references a ``windsurf``
    path component. The check is intentionally strict (``/windsurf``,
    not bare ``windsurf``) to avoid matching unrelated tools whose argv
    merely contain the literal string (e.g. ``wind-server`` itself, or
    a python ``-c`` script that mentions the word).
    """
    name = (info.get("name") or "").lower()
    exe = (info.get("exe") or "").lower()
    if "windsurf" in name or "windsurf" in exe:
        return True
    for arg in info.get("cmdline") or ():
        lowered = arg.lower()
        if "/windsurf/" in lowered or lowered.endswith("/windsurf"):
            return True
    return False


def find_windsurf_processes() -> list[psutil.Process]:
    procs = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if _is_windsurf_proc(proc.info):
                procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def is_running() -> bool:
    return bool(find_windsurf_processes())


def find_windsurf_binary() -> str | None:
    for b in WINDSURF_BINARIES:
        path = shutil.which(b)
        if path:
            return path
    # Fallback common install locations
    for p in (
        "/usr/bin/windsurf",
        "/usr/local/bin/windsurf",
        Path.home() / ".local/bin/windsurf",
    ):
        if Path(p).exists():
            return str(p)
    return None


def _all_windsurf_procs_with_children() -> list[psutil.Process]:
    """Return all Windsurf processes plus their descendants (Electron spawns
    a multi-process tree: main, renderer, GPU, utility, extension host, ...).
    """
    seen: dict[int, psutil.Process] = {}
    for proc in find_windsurf_processes():
        try:
            seen[proc.pid] = proc
            for child in proc.children(recursive=True):
                seen[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return list(seen.values())


def stop(graceful_timeout: float = 8.0, hard_timeout: float = 5.0) -> bool:
    """Terminate all Windsurf processes (and children) and **wait** until
    every one is gone. Returns True only when no Windsurf process remains.
    """
    procs = _all_windsurf_procs_with_children()
    if not procs:
        return True
    # Phase 1: SIGTERM the whole tree.
    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    try:
        psutil.wait_procs(procs, timeout=graceful_timeout)
    except OSError:
        pass

    # Phase 2: SIGKILL anything still alive (re-scan in case Electron
    # respawned helpers or new children appeared between phase 1 and now).
    remaining = _all_windsurf_procs_with_children()
    for p in remaining:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    try:
        psutil.wait_procs(remaining, timeout=hard_timeout)
    except OSError:
        pass

    # Phase 3: poll until is_running() is definitively False.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not is_running():
            return True
        time.sleep(0.1)
    return False


def wait_until_db_unlocked(timeout: float = 5.0) -> bool:
    """Poll state.vscdb until SQLite no longer reports a write lock.

    Even after every Windsurf process has exited, the kernel may take a
    few hundred ms to release fcntl locks. Call this after stop() and
    before writing to the DB.
    """
    from . import vscdb  # local import to avoid cycle

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not vscdb.is_db_locked():
            return True
        time.sleep(0.1)
    return not vscdb.is_db_locked()


def launch(workspace: str | None = None) -> subprocess.Popen | None:
    binary = find_windsurf_binary()
    if not binary:
        return None
    args = [binary]
    if workspace:
        args.append(workspace)
    # Detach so wind-server can exit without killing Windsurf.
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def restart(workspace: str | None = None) -> bool:
    stop()
    # Give the FS a moment to release locks.
    time.sleep(0.5)
    proc = launch(workspace)
    return proc is not None
