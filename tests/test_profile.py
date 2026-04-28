"""Tests for profile snapshot/restore round-tripping through a fake state DB."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from wind_server import profile as prof
from wind_server import storage_json, vscdb


def _build_db(
    path: Path,
    account: str,
    install_id: str,
    *,
    daily_remaining: int | None = None,
    weekly_remaining: int | None = None,
    daily_reset_at: int | None = None,
) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    rows = [
        ("windsurfAuthStatus", json.dumps({"apiKey": f"devin-session-token$jwt-{account}"})),
        ("codeium.windsurf", json.dumps({"codeium.installationId": install_id})),
        ("codeium.windsurf-windsurf_auth", account),
        (f"windsurf_auth-{account}", "[]"),
        ("theme", "vs-dark"),  # untouched
    ]
    if daily_remaining is not None or weekly_remaining is not None:
        quota: dict = {}
        if daily_remaining is not None:
            quota["dailyRemainingPercent"] = daily_remaining
        if weekly_remaining is not None:
            quota["weeklyRemainingPercent"] = weekly_remaining
        if daily_reset_at is not None:
            quota["dailyResetAtUnix"] = daily_reset_at
        rows.append(
            (
                "windsurf.settings.cachedPlanInfo",
                json.dumps({"planName": "Trial", "quotaUsage": quota}),
            )
        )
    conn.executemany("INSERT INTO ItemTable(key, value) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> dict:
    db = tmp_path / "state.vscdb"
    storage = tmp_path / "storage.json"
    profiles_dir = tmp_path / "profiles"
    storage.write_text(json.dumps({
        "telemetry.machineId": "machine-A",
        "telemetry.devDeviceId": "dev-A",
        "telemetry.sqmId": "",
        "theme": "vs-dark",
    }))
    _build_db(db, "Alice", "install-A")

    from wind_server import paths as wpaths
    monkeypatch.setattr(wpaths, "STATE_VSCDB", db)
    monkeypatch.setattr(wpaths, "STORAGE_JSON", storage)
    monkeypatch.setattr(wpaths, "PROFILES_DIR", profiles_dir)
    profiles_dir.mkdir()
    return {"db": db, "storage": storage, "profiles_dir": profiles_dir}


def test_snapshot_and_restore_round_trip(env, monkeypatch) -> None:
    # Snapshot Alice
    alice = prof.snapshot_current(label="primary")
    alice.save()
    assert (env["profiles_dir"] / alice.meta.slug).is_dir()

    # Switch DB to Bob
    env["db"].unlink()
    _build_db(env["db"], "Bob", "install-B")

    # Sanity: active is now Bob
    assert vscdb.get_active_account_name(env["db"]) == "Bob"

    # Restore Alice
    prof.apply_profile(prof.load_profile(alice.meta.slug))
    assert vscdb.get_active_account_name(env["db"]) == "Alice"
    assert vscdb.get_installation_id(env["db"]) == "install-A"


def test_find_matching_profile(env) -> None:
    alice = prof.snapshot_current()
    alice.save()
    found = prof.find_matching_profile("Alice", "install-A")
    assert found is not None and found.meta.slug == alice.meta.slug
    assert prof.find_matching_profile("Nobody", "") is None


def test_snapshot_captures_quota_into_extra(env) -> None:
    # Rebuild Alice's DB with cached plan info present.
    env["db"].unlink()
    _build_db(env["db"], "Alice", "install-A",
              daily_remaining=68, weekly_remaining=82, daily_reset_at=1_700_000_000)
    p = prof.snapshot_current()
    quota = (p.meta.extra or {}).get("quota") or {}
    assert quota.get("daily_remaining_pct") == 68
    assert quota.get("weekly_remaining_pct") == 82
    assert quota.get("daily_reset_at") == 1_700_000_000
    assert isinstance(quota.get("captured_at"), float)


def test_snapshot_skips_quota_when_cache_missing(env) -> None:
    # Default fixture DB has no cachedPlanInfo row.
    p = prof.snapshot_current()
    assert "quota" not in (p.meta.extra or {})


def test_inherit_persistent_meta_carries_last_switched(env) -> None:
    # Simulate a profile that was switched into 1234 ago.
    alice = prof.snapshot_current(label="primary")
    alice.meta.last_switched_at = 1_700_001_234.0
    alice.save()

    # Daemon-style auto-save: fresh snapshot then inherit.
    fresh = prof.snapshot_current()
    assert fresh.meta.last_switched_at == 0.0  # snapshot itself doesn't know
    match = prof.find_matching_profile(fresh.meta.account_name, fresh.meta.installation_id)
    assert match is not None
    prof.inherit_persistent_meta(fresh, match)
    fresh.save()

    reloaded = prof.load_profile(alice.meta.slug)
    assert reloaded.meta.last_switched_at == 1_700_001_234.0
    assert reloaded.meta.label == "primary"
    assert reloaded.meta.created_at == alice.meta.created_at


def test_save_current_before_switch_sets_last_switched_and_quota(env) -> None:
    # Rebuild DB with quota data so _merge_live_quota has something to read
    # (in tests the LSP is unavailable, so it falls back to cached plan info).
    env["db"].unlink()
    _build_db(env["db"], "Alice", "install-A",
              daily_remaining=42, weekly_remaining=75, daily_reset_at=1_700_000_000)

    # First, save Alice as a profile (no last_switched_at yet).
    alice = prof.snapshot_current(label="primary")
    alice.save()
    assert alice.meta.last_switched_at == 0.0

    # Now simulate "save before switch".
    saved_slug = prof.save_current_before_switch()
    assert saved_slug is not None

    # Reload and verify the from-account got a timestamp and quota.
    reloaded = prof.load_profile(saved_slug)
    assert reloaded.meta.last_switched_at > 0.0
    quota = (reloaded.meta.extra or {}).get("quota") or {}
    assert quota.get("daily_remaining_pct") == 42
    assert quota.get("weekly_remaining_pct") == 75


def test_save_current_before_switch_returns_none_when_no_match(env) -> None:
    # No profile saved yet — save_current_before_switch should return None.
    result = prof.save_current_before_switch()
    assert result is None
