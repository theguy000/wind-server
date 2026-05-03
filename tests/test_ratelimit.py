"""Tests for rate-limit byte parsing."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from wind_server import ratelimit


def test_scan_percent_bytes_finds_two_distinct_values() -> None:
    # 0x5b = 91, 0x08 = 8. Per docs, those map to daily/weekly remaining.
    payload = b"\x00\x10\x5b\x99\x08\xff\xff\x10"
    daily, weekly = ratelimit._scan_percent_bytes(payload)
    assert daily == 0
    # First two distinct in-range values: 0x00 and 0x10
    assert weekly == 16


def test_scan_percent_bytes_skips_unlimited_marker() -> None:
    # 0xFF must be ignored (unlimited tier per docs).
    payload = b"\xff\xff\xff"
    daily, weekly = ratelimit._scan_percent_bytes(payload)
    assert daily is None and weekly is None


def test_scan_percent_bytes_empty() -> None:
    assert ratelimit._scan_percent_bytes(b"") == (None, None)


def test_quota_snapshot_is_low_uses_threshold(monkeypatch) -> None:
    monkeypatch.setattr(ratelimit, "DEFAULT_THRESHOLD_PCT", 5)
    # Low daily
    q = ratelimit.QuotaSnapshot(daily_remaining_pct=4, weekly_remaining_pct=50,
                                raw_status_code=200, rate_limited=False)
    assert q.is_low

    # Low weekly
    q2 = ratelimit.QuotaSnapshot(daily_remaining_pct=50, weekly_remaining_pct=3,
                                 raw_status_code=200, rate_limited=False)
    assert q2.is_low

    # Neither low
    q3 = ratelimit.QuotaSnapshot(daily_remaining_pct=20, weekly_remaining_pct=50,
                                 raw_status_code=200, rate_limited=False)
    assert not q3.is_low

    # Rate limited
    q4 = ratelimit.QuotaSnapshot(daily_remaining_pct=None, weekly_remaining_pct=None,
                                 raw_status_code=429, rate_limited=True)
    assert q4.is_low


def _make_db(tmp_path: Path, plan_info: dict | None) -> Path:
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    if plan_info is not None:
        conn.execute(
            "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
            ("windsurf.settings.cachedPlanInfo", json.dumps(plan_info)),
        )
    conn.commit()
    conn.close()
    return db


def test_read_quota_from_cached_plan_info(tmp_path: Path) -> None:
    db = _make_db(tmp_path, {
        "planName": "Free",
        "quotaUsage": {
            "dailyRemainingPercent": 42,
            "weeklyRemainingPercent": 77,
            "dailyResetAtUnix": 12345,
            "weeklyResetAtUnix": 67890
        },
    })
    q = ratelimit.read_quota(db, prefer_live=False)
    assert q.daily_remaining_pct == 42
    assert q.weekly_remaining_pct == 77
    assert q.daily_reset_at == 12345
    assert q.weekly_reset_at == 67890
    assert q.source == "cached_plan_info"
    assert q.raw_status_code == 200
    assert not q.rate_limited


def test_read_quota_low_triggers_is_low(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ratelimit, "DEFAULT_THRESHOLD_PCT", 5)
    db = _make_db(tmp_path, {"quotaUsage": {"dailyRemainingPercent": 3, "weeklyRemainingPercent": 50}})
    assert ratelimit.read_quota(db, prefer_live=False).is_low


def test_read_quota_missing_row_returns_unknown(tmp_path: Path) -> None:
    db = _make_db(tmp_path, None)
    q = ratelimit.read_quota(db, prefer_live=False)
    assert q.daily_remaining_pct is None
    assert q.weekly_remaining_pct is None
    assert q.source == "unknown"


def test_read_quota_missing_quotaUsage(tmp_path: Path) -> None:
    # Paid/enterprise tier without quotaUsage block.
    db = _make_db(tmp_path, {"planName": "Pro"})
    q = ratelimit.read_quota(db, prefer_live=False)
    assert q.daily_remaining_pct is None
    assert q.source == "unknown"


def test_read_quota_uses_lsp_when_available(tmp_path: Path, monkeypatch) -> None:
    # Cache says 50/50 but the live RPC says 12/30 — live must win.
    db = _make_db(tmp_path, {"quotaUsage": {"dailyRemainingPercent": 50, "weeklyRemainingPercent": 50}})
    monkeypatch.setattr(
        ratelimit.lsp_client,
        "get_user_status",
        lambda **kwargs: {
            "userStatus": {
                "planStatus": {
                    "dailyQuotaRemainingPercent": 12,
                    "weeklyQuotaRemainingPercent": 30,
                }
            }
        },
    )
    q = ratelimit.read_quota(db)
    assert q.daily_remaining_pct == 12
    assert q.weekly_remaining_pct == 30
    assert q.source == "lsp_live"


def test_read_quota_falls_back_when_lsp_unavailable(tmp_path: Path, monkeypatch) -> None:
    db = _make_db(tmp_path, {"quotaUsage": {"dailyRemainingPercent": 88, "weeklyRemainingPercent": 91}})
    monkeypatch.setattr(ratelimit.lsp_client, "get_user_status", lambda **kwargs: None)
    q = ratelimit.read_quota(db)
    assert q.daily_remaining_pct == 88
    assert q.source == "cached_plan_info"


def test_read_quota_exhausted_lsp_response(monkeypatch) -> None:
    # LSP returns valid JSON but with nulls for quota percents -> should be 0/0
    monkeypatch.setattr(
        ratelimit.lsp_client,
        "get_user_status",
        lambda **kwargs: {
            "userStatus": {
                "planStatus": {
                    "dailyQuotaRemainingPercent": None,
                    "weeklyQuotaRemainingPercent": None,
                    "dailyQuotaResetAtUnix": 1000,
                    "weeklyQuotaResetAtUnix": 2000,
                }
            }
        },
    )
    q = ratelimit.read_quota(None)
    assert q.daily_remaining_pct == 0
    assert q.weekly_remaining_pct == 0
    assert q.daily_reset_at == 1000
    assert q.weekly_reset_at == 2000
    assert q.source == "lsp_live"


def test_read_quota_partial_exhausted_lsp_response(monkeypatch) -> None:
    # Daily is null (0), weekly is 15
    monkeypatch.setattr(
        ratelimit.lsp_client,
        "get_user_status",
        lambda **kwargs: {
            "userStatus": {
                "planStatus": {
                    "dailyQuotaRemainingPercent": None,
                    "weeklyQuotaRemainingPercent": 15,
                }
            }
        },
    )
    q = ratelimit.read_quota(None)
    assert q.daily_remaining_pct == 0
    assert q.weekly_remaining_pct == 15

    # Daily is 20, weekly is null (0)
    monkeypatch.setattr(
        ratelimit.lsp_client,
        "get_user_status",
        lambda **kwargs: {
            "userStatus": {
                "planStatus": {
                    "dailyQuotaRemainingPercent": 20,
                    "weeklyQuotaRemainingPercent": None,
                }
            }
        },
    )
    q = ratelimit.read_quota(None)
    assert q.daily_remaining_pct == 20
    assert q.weekly_remaining_pct == 0
