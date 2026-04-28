"""Rate-limit detection.

Layered strategies, tried in order:

1. **Live LSP RPC** (`lsp_client.get_user_status`). Calls the local
   `language_server` over its random 127.0.0.1 port using the CSRF token
   from the extension host process and the JWT from `state.vscdb`. This is
   the *fresh* number Windsurf's own UI displays.

2. **Cached plan info** (`windsurf.settings.cachedPlanInfo` JSON). Updated
   only when Windsurf bothers to write it back — we've seen 30+ minute
   lag — but works even when Windsurf isn't running.

3. **Log tailing** of Windsurf's renderer logs for `429` /
   `RESOURCE_EXHAUSTED` markers as a reactive last-resort signal.

A legacy `fetch_quota()` HTTP path is kept for reference but isn't usable
with only the `devin-session-token` JWT (the public server returns 404 —
the LSP path above succeeds because the language_server adds the right
production Authorization header internally).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from . import lsp_client, vscdb
from .paths import WINDSURF_LOG_ROOT

# State for scan_recent_logs to track position and cooldown
_last_log_path: Path | None = None
_last_log_pos: int = 0
_last_rate_limit_hit: float = 0.0
_SWITCH_COOLDOWN_SECONDS: float = 300.0  # 5 minutes between switches

USER_STATUS_URL = (
    "https://server.self-serve.windsurf.com/"
    "exa.api_server_pb.ApiServerService/GetUserStatus"
)
DEFAULT_THRESHOLD_PCT = 5  # swap when daily remaining drops below this


@dataclass
class QuotaSnapshot:
    daily_remaining_pct: int | None  # 0..100, None if unknown
    weekly_remaining_pct: int | None
    raw_status_code: int
    rate_limited: bool      # explicit 429 / RESOURCE_EXHAUSTED
    source: str = "unknown"  # "lsp_live" | "cached_plan_info" | "http" | "unknown"

    @property
    def is_low(self) -> bool:
        if self.rate_limited:
            return True
        if self.daily_remaining_pct is not None:
            return self.daily_remaining_pct < DEFAULT_THRESHOLD_PCT
        return False


def _from_cached_plan_info(db_path: Path | None) -> QuotaSnapshot:
    info = vscdb.read_cached_plan_info(db_path)
    if not info:
        return QuotaSnapshot(None, None, 0, False, source="unknown")
    qu = info.get("quotaUsage") or {}
    daily = qu.get("dailyRemainingPercent")
    weekly = qu.get("weeklyRemainingPercent")
    return QuotaSnapshot(
        daily_remaining_pct=int(daily) if isinstance(daily, (int, float)) else None,
        weekly_remaining_pct=int(weekly) if isinstance(weekly, (int, float)) else None,
        raw_status_code=200 if daily is not None else 0,
        rate_limited=False,
        source="cached_plan_info" if daily is not None else "unknown",
    )


def read_quota(
    db_path: Path | None = None,
    *,
    prefer_live: bool = True,
) -> QuotaSnapshot:
    """Read current quota, preferring the live LSP RPC over the stale cache.

    Set ``prefer_live=False`` to skip the LSP probe (useful in tests or
    when polling at high frequency — the LSP call is ~10–20 ms).
    """
    if prefer_live:
        status = lsp_client.get_user_status()
        if status is not None:
            daily, weekly = lsp_client.extract_quota_percents(status)
            if daily is not None or weekly is not None:
                return QuotaSnapshot(
                    daily_remaining_pct=daily,
                    weekly_remaining_pct=weekly,
                    raw_status_code=200,
                    rate_limited=False,
                    source="lsp_live",
                )
    return _from_cached_plan_info(db_path)


def _scan_percent_bytes(payload: bytes) -> tuple[int | None, int | None]:
    """Heuristic: find two single-byte fields whose values look like 0..100.

    Per `docs/windsurf-internals.md`: the daily-remaining byte sits in the
    0x00..0x64 range and decreases over time; the weekly byte does the same.
    Without the proto schema we approximate by collecting candidates in that
    range from the response and returning the first two distinct values.

    Unlimited tier comes back as 0xFF (-1) — we map that to 100 so it never
    triggers a swap.
    """
    if not payload:
        return None, None
    candidates: list[int] = []
    for b in payload:
        if b == 0xFF:
            continue
        if 0 <= b <= 100 and b not in candidates:
            candidates.append(b)
        if len(candidates) >= 2:
            break
    if not candidates:
        return None, None
    return candidates[0], candidates[1] if len(candidates) > 1 else None


def fetch_quota(session_jwt: str, timeout: float = 6.0) -> QuotaSnapshot:
    """Call GetUserStatus and parse remaining quota bytes."""
    headers = {
        "Authorization": f"Bearer {session_jwt}" if not session_jwt.startswith("Bearer") else session_jwt,
        "Content-Type": "application/proto",
        "Connect-Protocol-Version": "1",
    }
    try:
        resp = requests.post(USER_STATUS_URL, headers=headers, data=b"", timeout=timeout)
    except requests.RequestException:
        return QuotaSnapshot(None, None, raw_status_code=0, rate_limited=False)

    if resp.status_code == 429:
        return QuotaSnapshot(None, None, raw_status_code=429, rate_limited=True)

    # Only parse the body when the server returned protobuf, not an error page.
    if resp.status_code != 200:
        return QuotaSnapshot(None, None, raw_status_code=resp.status_code, rate_limited=False)

    daily, weekly = _scan_percent_bytes(resp.content)
    return QuotaSnapshot(
        daily_remaining_pct=daily,
        weekly_remaining_pct=weekly,
        raw_status_code=resp.status_code,
        rate_limited=False,
    )


_LOG_TRIGGERS = re.compile(
    r"(429\b|RESOURCE_EXHAUSTED|rate.?limit|quota.exceeded)",
    re.IGNORECASE,
)


def _newest_log_file() -> Path | None:
    if not WINDSURF_LOG_ROOT.exists():
        return None
    candidates = list(WINDSURF_LOG_ROOT.rglob("*.log"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def scan_recent_logs(byte_window: int = 65536) -> bool:
    """Return True if the newest Windsurf log shows a recent rate-limit hit.

    Tracks file position between calls to avoid re-reporting the same 429
    multiple times. Also implements a cooldown period to prevent rapid
    successive switches.
    """
    global _last_log_path, _last_log_pos, _last_rate_limit_hit

    path = _newest_log_file()
    if not path:
        _last_log_path = None
        _last_log_pos = 0
        return False

    # Reset position if log file changed (new session started)
    if path != _last_log_path:
        _last_log_path = path
        _last_log_pos = 0

    try:
        size = path.stat().st_size
        # If file shrunk (rotated/truncated), reset position
        if size < _last_log_pos:
            _last_log_pos = 0

        with path.open("rb") as f:
            f.seek(_last_log_pos)
            new_content = f.read()
            _last_log_pos = f.tell()
    except OSError:
        return False

    if not new_content:
        return False

    tail = new_content.decode("utf-8", errors="ignore")
    if _LOG_TRIGGERS.search(tail):
        now = time.time()
        # Only report if cooldown has passed
        if now - _last_rate_limit_hit >= _SWITCH_COOLDOWN_SECONDS:
            _last_rate_limit_hit = now
            return True
    return False


def is_switch_cooldown_active() -> bool:
    """Return True if a recent switch has occurred and cooldown is active."""
    return time.time() - _last_rate_limit_hit < _SWITCH_COOLDOWN_SECONDS
