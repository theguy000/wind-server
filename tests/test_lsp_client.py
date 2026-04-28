"""Tests for the local language-server RPC helpers."""
from __future__ import annotations

from pathlib import Path

from wind_server import lsp_client


def test_extract_quota_percents_nominal() -> None:
    daily, weekly = lsp_client.extract_quota_percents({
        "userStatus": {
            "planStatus": {
                "dailyQuotaRemainingPercent": 72,
                "weeklyQuotaRemainingPercent": 84,
            }
        }
    })
    assert daily == 72 and weekly == 84


def test_extract_quota_percents_missing_planStatus() -> None:
    assert lsp_client.extract_quota_percents({"userStatus": {}}) == (None, None)


def test_extract_quota_percents_top_level_fallback() -> None:
    daily, weekly = lsp_client.extract_quota_percents({
        "dailyQuotaRemainingPercent": 5,
        "weeklyQuotaRemainingPercent": 10,
    })
    assert daily == 5 and weekly == 10


def test_extract_quota_percents_unrelated_payload() -> None:
    assert lsp_client.extract_quota_percents({"foo": "bar"}) == (None, None)


def test_discover_lsp_port_parses_latest_session(tmp_path: Path) -> None:
    # Two session dirs; the lexicographically-greatest one wins.
    older = tmp_path / "20260101T000000" / "window1" / "exthost" / "codeium.windsurf"
    newer = tmp_path / "20260202T000000" / "window1" / "exthost" / "codeium.windsurf"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "Windsurf.log").write_text("Language server listening on random port at 11111\n")
    (newer / "Windsurf.log").write_text(
        "Language server listening on random port at 33333\n"
        "Language server listening on random port at 44444\n"  # restart in same session
    )
    assert lsp_client.discover_lsp_port(tmp_path) == 44444


def test_discover_lsp_port_no_logs(tmp_path: Path) -> None:
    assert lsp_client.discover_lsp_port(tmp_path) is None
