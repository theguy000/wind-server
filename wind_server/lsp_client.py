"""Client for Windsurf's local language-server RPC.

Why this exists:
    The `windsurf.settings.cachedPlanInfo` row in `state.vscdb` is updated
    only periodically by Windsurf (we've seen 30+ minute lag). For *live*
    daily/weekly quota numbers we have to ask the language_server itself,
    which proxies to `server.self-serve.windsurf.com` with the right
    Authorization header that we can't reconstruct from the JWT alone.

How it works:
    Windsurf's extension host spawns `language_server_linux_x64` on a random
    127.0.0.1 port and shares a CSRF token via the `WINDSURF_CSRF_TOKEN`
    environment variable. The renderer talks to that local server via
    Connect-RPC, with two pieces of auth in every request:

      Header `x-codeium-csrf-token: <token>`   (matches CsrfInterceptor)
      Body  `metadata.api_key: <devin-session-token$JWT>`

    We scrape both at runtime:
      * CSRF: read `/proc/<pid>/environ` for any process owned by the
        current uid that has `WINDSURF_CSRF_TOKEN=`.
      * Port: parse the most recent Windsurf log line
        `Language server listening on random port at <PORT>`.

Limitations:
    * Only works while Windsurf is running.
    * Only works on Linux (uses `/proc`).
    * The wire format is private — fields could rename/move on Windsurf
      upgrades. We tolerate that by returning `None` instead of crashing.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests

from . import paths, vscdb

LSP_PORT_LOG_RE = re.compile(
    r"Language server listening on random port at (\d+)"
)
USER_STATUS_PATH = (
    "/exa.language_server_pb.LanguageServerService/GetUserStatus"
)
CSRF_HEADER = "x-codeium-csrf-token"

# Match what the extension sends. Versions don't have to be exact — the
# server validates `api_key` and `csrf_token`, not the IDE version strings.
_DEFAULT_IDE_NAME = "windsurf"
_DEFAULT_EXTENSION = "windsurf"
_FALLBACK_VERSION = "2.0.50"


def _read_product_version() -> str:
    """Best-effort read of Windsurf's ide/extension version."""
    candidates = [
        Path("/usr/share/windsurf/resources/app/product.json"),
        Path("/opt/Windsurf/resources/app/product.json"),
        Path("/opt/windsurf/resources/app/product.json"),
    ]
    for p in candidates:
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        v = data.get("windsurfVersion") or data.get("version")
        if v:
            return str(v)
    return _FALLBACK_VERSION


def discover_csrf_token() -> str | None:
    """Scan /proc/*/environ for WINDSURF_CSRF_TOKEN=<uuid>.

    Only inspects processes owned by the current uid; everything else is
    silently skipped (read-protected by the kernel anyway).
    """
    my_uid = os.getuid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != my_uid:
                continue
            data = (entry / "environ").read_bytes()
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
        for kv in data.split(b"\x00"):
            if kv.startswith(b"WINDSURF_CSRF_TOKEN="):
                return kv.split(b"=", 1)[1].decode("utf-8", "replace").strip()
    return None


def discover_lsp_port(log_root: Path | None = None) -> int | None:
    """Parse the most-recent Windsurf log for the language-server port."""
    root = log_root or paths.WINDSURF_LOG_ROOT
    if not root.exists():
        return None
    # Newest session dir wins (`YYYYMMDDTHHMMSS` sort lexicographically).
    sessions = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        reverse=True,
    )
    for session in sessions:
        for log in session.rglob("codeium.windsurf/Windsurf.log"):
            try:
                # Read only tail of file to keep it cheap on long-running sessions.
                size = log.stat().st_size
                tail_size = min(65536, size)  # Read last 64KB max
                with log.open("rb") as f:
                    f.seek(max(0, size - tail_size))
                    text = f.read().decode("utf-8", errors="ignore")
            except OSError:
                continue
            # Use last match — language_server can restart within a session.
            matches = LSP_PORT_LOG_RE.findall(text)
            if matches:
                return int(matches[-1])
    return None


def get_user_status(
    *,
    api_key: str | None = None,
    csrf_token: str | None = None,
    port: int | None = None,
    timeout: float = 4.0,
) -> dict | None:
    """Call GetUserStatus on the local language_server. Return parsed JSON
    or None if any prerequisite (running Windsurf, port, CSRF, JWT) is
    missing or the request fails.
    """
    api_key = api_key or vscdb.get_session_jwt()
    csrf_token = csrf_token or discover_csrf_token()
    port = port or discover_lsp_port()
    if not (api_key and csrf_token and port):
        return None

    version = _read_product_version()
    body = {
        "metadata": {
            "ide_name": _DEFAULT_IDE_NAME,
            "ide_version": version,
            "extension_name": _DEFAULT_EXTENSION,
            "extension_version": version,
            "api_key": api_key,
        }
    }
    url = f"http://127.0.0.1:{port}{USER_STATUS_PATH}"
    try:
        resp = requests.post(
            url,
            json=body,
            headers={CSRF_HEADER: csrf_token, "Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def extract_quota_percents(status: dict) -> tuple[int | None, int | None]:
    """Return (daily_remaining_pct, weekly_remaining_pct) from a status dict.

    The server has been observed to use either field naming style:
      * `userStatus.planStatus.{dailyQuotaRemainingPercent, weeklyQuotaRemainingPercent}`
      * top-level fallbacks (older builds)

    Returns (None, None) when neither path is present.
    """
    plan = (status.get("userStatus") or {}).get("planStatus") or {}
    daily = plan.get("dailyQuotaRemainingPercent")
    weekly = plan.get("weeklyQuotaRemainingPercent")
    if daily is None and weekly is None:
        # Older shape, just in case.
        daily = status.get("dailyQuotaRemainingPercent")
        weekly = status.get("weeklyQuotaRemainingPercent")
    return (
        int(daily) if isinstance(daily, (int, float)) else None,
        int(weekly) if isinstance(weekly, (int, float)) else None,
    )
