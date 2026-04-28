"""Auto-save daemon.

Watches state.vscdb. Whenever the active account name or installation id
changes vs the last seen value, snapshot the new state into the matching
profile (or create one if none matches).

Designed to run as a systemd --user service or via `wind-server daemon`.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import profile as prof
from . import vscdb
from .paths import DAEMON_PID, GLOBAL_STORAGE, STATE_VSCDB, ensure_dirs


def _is_daemon_running() -> bool:
    """Check if another daemon instance is already running by checking PID file."""
    if not DAEMON_PID.exists():
        return False
    try:
        pid = int(DAEMON_PID.read_text().strip())
        # Check if process exists
        os.kill(pid, 0)
        return True
    except (ValueError, OSError, ProcessLookupError):
        # Stale PID file
        try:
            DAEMON_PID.unlink()
        except FileNotFoundError:
            pass
        return False


def _acquire_daemon_lock() -> bool:
    """Acquire PID file lock. Returns True if successful."""
    if _is_daemon_running():
        return False
    try:
        DAEMON_PID.write_text(str(os.getpid()))
        return True
    except OSError:
        return False


def _log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class _Handler(FileSystemEventHandler):
    def __init__(self) -> None:
        self.last_account: str | None = None
        self.last_install: str | None = None
        self._cooldown_until = 0.0

    def _maybe_save(self) -> None:
        # Throttle: at most once every 3 seconds.
        now = time.time()
        if now < self._cooldown_until:
            return

        try:
            account = vscdb.get_active_account_name()
            install = vscdb.get_installation_id()
        except Exception:
            return
        if not account:
            return
        if account == self.last_account and install == self.last_install:
            return

        # Identity changed — snapshot.
        self.last_account = account
        self.last_install = install
        self._cooldown_until = now + 3.0
        try:
            current = prof.snapshot_current()
            match = prof.find_matching_profile(account, install or "")
            if match:
                prof.inherit_persistent_meta(current, match)
                current.save()
                _log(f"auto-saved -> {match.meta.slug} ({account})")
            else:
                current.save()
                _log(f"new profile -> {current.meta.slug} ({account})")
        except Exception as e:
            _log(f"snapshot failed: {e}")

    def on_modified(self, event: FileSystemEvent) -> None:
        # Match exact basename to avoid triggering on journal/wal/backup files
        if not event.is_directory and os.path.basename(event.src_path) == "state.vscdb":
            self._maybe_save()

    def on_created(self, event: FileSystemEvent) -> None:
        self.on_modified(event)


def run_daemon() -> None:
    ensure_dirs()
    if not _acquire_daemon_lock():
        _log("daemon already running; exiting")
        sys.exit(1)
    if not STATE_VSCDB.exists():
        _log("state.vscdb not found; waiting for it to appear...")
    handler = _Handler()
    # Pre-seed last_* so we don't snapshot on first tick.
    handler.last_account = vscdb.get_active_account_name()
    handler.last_install = vscdb.get_installation_id()

    observer = Observer()
    observer.schedule(handler, str(GLOBAL_STORAGE), recursive=False)
    observer.start()
    _log(f"watching {GLOBAL_STORAGE} (current account: {handler.last_account})")
    # Set up signal handlers for clean shutdown
    stop_requested = False

    def _signal_handler(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        while not stop_requested:
            time.sleep(2.0)
            # Periodic re-check in case watchdog missed an event.
            handler._maybe_save()
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join(timeout=3.0)
        try:
            DAEMON_PID.unlink()
        except FileNotFoundError:
            pass
