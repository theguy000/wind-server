"""Tests for the SQLite read/write layer using a synthetic state DB."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from wind_server import vscdb


@pytest.fixture
def fake_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    rows = {
        "windsurfAuthStatus": json.dumps({"apiKey": "devin-session-token$abc.def.ghi"}),
        "codeium.windsurf": json.dumps({"codeium.installationId": "install-123"}),
        "codeium.windsurf-windsurf_auth": "Test User",
        "windsurf_auth-Test User": "[]",
        "windsurf_auth-Test User-usages": "{}",
        'secret://{"extensionId":"codeium.windsurf","key":"windsurf_auth.sessions"}':
            '{"type":"Buffer","data":[1,2,3]}',
        "windsurf.settings.cachedPlanInfo": json.dumps({
            "planName": "Free",
            "billingStrategy": "quota",
            "quotaUsage": {
                "dailyRemainingPercent": 73,
                "weeklyRemainingPercent": 88,
                "dailyResetAtUnix": 1777276800,
                "weeklyResetAtUnix": 1777795200,
            },
        }),
        # Unrelated rows must be left alone.
        "telemetry.foo": "bar",
        "theme": "vs-dark",
    }
    for k, v in rows.items():
        conn.execute("INSERT INTO ItemTable(key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()
    return db


def test_read_auth_rows_filters_correctly(fake_db: Path) -> None:
    rows = vscdb.read_auth_rows(fake_db)
    assert "windsurfAuthStatus" in rows
    assert "codeium.windsurf" in rows
    assert "codeium.windsurf-windsurf_auth" in rows
    assert "windsurf_auth-Test User" in rows
    assert "windsurf_auth-Test User-usages" in rows
    assert any("windsurf_auth.sessions" in k for k in rows)
    # Unrelated keys must NOT be captured.
    assert "telemetry.foo" not in rows
    assert "theme" not in rows


def test_get_active_account_name(fake_db: Path) -> None:
    assert vscdb.get_active_account_name(fake_db) == "Test User"


def test_get_session_jwt(fake_db: Path) -> None:
    jwt = vscdb.get_session_jwt(fake_db)
    assert jwt == "devin-session-token$abc.def.ghi"


def test_get_installation_id(fake_db: Path) -> None:
    assert vscdb.get_installation_id(fake_db) == "install-123"


def test_read_cached_plan_info_returns_quota_usage(fake_db: Path) -> None:
    info = vscdb.read_cached_plan_info(fake_db)
    assert info is not None
    assert info["planName"] == "Free"
    assert info["quotaUsage"]["dailyRemainingPercent"] == 73
    assert info["quotaUsage"]["weeklyRemainingPercent"] == 88


def test_read_cached_plan_info_missing_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "empty.vscdb"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    conn.commit()
    conn.close()
    assert vscdb.read_cached_plan_info(db) is None


def test_write_auth_rows_replaces_only_auth_keys(fake_db: Path) -> None:
    new_rows = {
        "windsurfAuthStatus": json.dumps({"apiKey": "devin-session-token$NEW.NEW.NEW"}),
        "codeium.windsurf": json.dumps({"codeium.installationId": "install-999"}),
        "codeium.windsurf-windsurf_auth": "Other User",
        "windsurf_auth-Other User": "[]",
    }
    vscdb.write_auth_rows(new_rows, fake_db)

    # New auth state applied.
    assert vscdb.get_active_account_name(fake_db) == "Other User"
    assert vscdb.get_installation_id(fake_db) == "install-999"
    # Old per-account row gone.
    rows = vscdb.read_auth_rows(fake_db)
    assert "windsurf_auth-Test User" not in rows
    assert "windsurf_auth-Other User" in rows
    # Unrelated rows untouched.
    conn = sqlite3.connect(str(fake_db))
    val = conn.execute("SELECT value FROM ItemTable WHERE key = 'telemetry.foo'").fetchone()
    conn.close()
    assert val[0] == "bar"
