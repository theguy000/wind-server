"""Tests for LSP quota extraction."""
from wind_server import lsp_client

def test_extract_quota_info_success() -> None:
    status = {
        "userStatus": {
            "planStatus": {
                "dailyQuotaRemainingPercent": 27,
                "weeklyQuotaRemainingPercent": 71,
                "dailyQuotaResetAtUnix": "1777881600",
                "weeklyQuotaResetAtUnix": "1778400000"
            }
        }
    }
    daily, weekly, d_reset, w_reset = lsp_client.extract_quota_info(status)
    assert daily == 27
    assert weekly == 71
    assert d_reset == 1777881600
    assert w_reset == 1778400000

def test_extract_quota_info_old_format() -> None:
    status = {
        "dailyQuotaRemainingPercent": 10,
        "weeklyQuotaRemainingPercent": 20,
        "dailyQuotaResetAtUnix": 123456,
        "weeklyQuotaResetAtUnix": 789012
    }
    daily, weekly, d_reset, w_reset = lsp_client.extract_quota_info(status)
    assert daily == 10
    assert weekly == 20
    assert d_reset == 123456
    assert w_reset == 789012

def test_extract_quota_info_empty() -> None:
    daily, weekly, d_reset, w_reset = lsp_client.extract_quota_info({})
    assert daily is None
    assert weekly is None
    assert d_reset is None
    assert w_reset is None
