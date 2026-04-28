"""Read/write Windsurf's state.vscdb (the SQLite store backing VS Code's globalStorage).

Only the auth + identity related rows are touched. We never delete unrelated keys.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from . import paths

# Keys that together uniquely identify the active account.
# Anything matching these patterns is part of a profile snapshot.
AUTH_KEY_PREFIXES: tuple[str, ...] = (
    "windsurf_auth-",
    "windsurf_auth.",
)
AUTH_KEY_EXACT: tuple[str, ...] = (
    "windsurfAuthStatus",
    "codeium.windsurf",
    "codeium.windsurf-windsurf_auth",
)
# Encrypted secret blobs live under keys like:
#   secret://{"extensionId":"codeium.windsurf","key":"windsurf_auth.sessions"}
AUTH_KEY_SUBSTRINGS: tuple[str, ...] = (
    '"extensionId":"codeium.windsurf"',
)


def _is_auth_key(key: str) -> bool:
    return (
        key in AUTH_KEY_EXACT
        or any(key.startswith(p) for p in AUTH_KEY_PREFIXES)
        or any(s in key for s in AUTH_KEY_SUBSTRINGS)
    )


def _resolve(db_path: Path | None) -> Path:
    return db_path if db_path is not None else paths.STATE_VSCDB


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = _resolve(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Windsurf state DB not found: {db_path}")
    # Use a short timeout; if Windsurf is running it has a write lock and we'll
    # surface that to the caller fast.
    return sqlite3.connect(str(db_path), timeout=2.0, isolation_level=None)


def read_auth_rows(db_path: Path | None = None) -> dict[str, str]:
    """Return {key: value} for every auth/identity row currently in the DB."""
    out: dict[str, str] = {}
    with _connect(db_path) as conn:
        cur = conn.execute("SELECT key, value FROM ItemTable")
        for key, value in cur:
            if _is_auth_key(key):
                out[key] = value
    return out


def get_active_account_name(db_path: Path | None = None) -> str | None:
    """Return the human-readable account name currently logged in, or None."""
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'codeium.windsurf-windsurf_auth'"
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    return str(row[0]).strip()


def get_session_jwt(db_path: Path | None = None) -> str | None:
    """Pull the devin-session-token JWT from windsurfAuthStatus.apiKey."""
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'windsurfAuthStatus'"
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data.get("apiKey")


def read_cached_plan_info(db_path: Path | None = None) -> dict | None:
    """Return the parsed `windsurf.settings.cachedPlanInfo` JSON, or None.

    Windsurf itself keeps this row up to date as the user consumes quota.
    Schema (observed on a Free-tier account):
      {
        "planName": "Free",
        "usage": {...},
        "billingStrategy": "quota",
        "quotaUsage": {
          "dailyRemainingPercent": 96,
          "weeklyRemainingPercent": 98,
          "overageBalanceMicros": 9275930,
          "dailyResetAtUnix": <unix>,
          "weeklyResetAtUnix": <unix>
        },
        ...
      }

    `quotaUsage` is the field we care about — that's the live remaining quota
    Windsurf displays in its own UI. Reading it lets us bypass the encrypted
    Authorization header entirely.
    """
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'windsurf.settings.cachedPlanInfo'"
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def get_installation_id(db_path: Path | None = None) -> str | None:
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'codeium.windsurf'"
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data.get("codeium.installationId")


def get_active_email(db_path: Path | None = None) -> str | None:
    """Return the email address of the currently logged in account, or None."""
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'codeium.windsurf'"
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data.get("lastLoginEmail")


def write_auth_rows(rows: dict[str, str], db_path: Path | None = None) -> None:
    """Atomically replace every auth row with the given mapping.

    Strategy:
      1. backup current DB,
      2. delete every existing auth-shaped key,
      3. insert/replace from `rows`.

    Caller MUST ensure Windsurf is not running.
    """
    db_path = _resolve(db_path)
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    # Local backup with timestamp (separate from Windsurf's own .backup).
    backup = db_path.with_suffix(db_path.suffix + f".wind-server.{int(time.time())}.bak")
    shutil.copy2(db_path, backup)
    paths.prune_old_backups(db_path.parent, db_path.name)

    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = [
                k for (k,) in conn.execute("SELECT key FROM ItemTable").fetchall()
                if _is_auth_key(k)
            ]
            for k in existing:
                conn.execute("DELETE FROM ItemTable WHERE key = ?", (k,))
            for k, v in rows.items():
                conn.execute(
                    "INSERT OR REPLACE INTO ItemTable(key, value) VALUES (?, ?)",
                    (k, v),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            # Restore from our local backup just in case the partial write corrupted things.
            shutil.copy2(backup, db_path)
            raise


def is_db_locked(db_path: Path | None = None) -> bool:
    """Return True if Windsurf currently holds a write lock on the DB."""
    try:
        with _connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE").fetchall()
            conn.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError:
        return True
    except FileNotFoundError:
        return False


def clear_cached_plan_info(db_path: Path | None = None) -> None:
    """Delete the windsurf.settings.cachedPlanInfo row so Windsurf re-fetches it.

    When switching accounts (e.g. Free → Trial), the old cachedPlanInfo row
    persists and Windsurf may use it to invalidate the new session on launch.
    Removing it forces Windsurf to fetch fresh plan info from the server.
    """
    db_path = _resolve(db_path)
    if not db_path.exists():
        return
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "DELETE FROM ItemTable WHERE key = 'windsurf.settings.cachedPlanInfo'"
            )
    except sqlite3.OperationalError:
        pass


def list_all_auth_keys(db_path: Path | None = None) -> Iterable[str]:
    """Diagnostic helper: yield every auth-shaped key currently in the DB."""
    with _connect(db_path) as conn:
        for (key,) in conn.execute("SELECT key FROM ItemTable"):
            if _is_auth_key(key):
                yield key
