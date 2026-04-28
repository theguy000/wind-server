"""A 'profile' is a complete snapshot of one Windsurf account's identity.

On disk:  ~/.config/wind-server/profiles/<name>/
    auth_rows.json     # every auth-shaped state.vscdb key/value
    identity.json      # storage.json identity subset
    meta.json          # display name, account label, created/updated timestamps
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths, storage_json, vscdb
from .paths import ensure_dirs


def _profiles_dir() -> Path:
    return paths.PROFILES_DIR

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    s = SAFE_NAME.sub("-", name.strip()).strip("-")
    return s or "profile"


@dataclass
class ProfileMeta:
    slug: str
    account_name: str       # Windsurf display name (codeium.windsurf-windsurf_auth)
    label: str = ""         # user-supplied alias (e.g. "personal", "trial-3")
    installation_id: str = ""
    session_jwt_preview: str = ""   # first 24 chars, for display
    created_at: float = 0.0
    updated_at: float = 0.0
    last_active_at: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProfileMeta":
        return cls(
            slug=d["slug"],
            account_name=d.get("account_name", ""),
            label=d.get("label", ""),
            installation_id=d.get("installation_id", ""),
            session_jwt_preview=d.get("session_jwt_preview", ""),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
            last_active_at=d.get("last_active_at", d.get("last_switched_at", 0.0)),
            extra=d.get("extra") or {},
        )


@dataclass
class Profile:
    meta: ProfileMeta
    auth_rows: dict[str, str]
    identity: dict[str, str]

    @property
    def dir(self) -> Path:
        return _profiles_dir() / self.meta.slug

    def save(self) -> None:
        ensure_dirs()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "auth_rows.json").write_text(json.dumps(self.auth_rows, indent=2))
        (self.dir / "identity.json").write_text(json.dumps(self.identity, indent=2))
        (self.dir / "meta.json").write_text(json.dumps(self.meta.to_dict(), indent=2))


def load_profile(slug: str) -> Profile:
    pdir = _profiles_dir() / slug
    if not pdir.is_dir():
        raise FileNotFoundError(f"Profile not found: {slug}")
    meta = ProfileMeta.from_dict(json.loads((pdir / "meta.json").read_text()))
    auth = json.loads((pdir / "auth_rows.json").read_text())
    ident = json.loads((pdir / "identity.json").read_text())
    return Profile(meta=meta, auth_rows=auth, identity=ident)


def list_profiles() -> list[Profile]:
    ensure_dirs()
    pdir_root = _profiles_dir()
    if not pdir_root.exists():
        return []
    profiles = []
    for pdir in sorted(pdir_root.iterdir()):
        if not pdir.is_dir():
            continue
        if not (pdir / "meta.json").exists():
            continue
        try:
            profiles.append(load_profile(pdir.name))
        except Exception:
            continue
    return profiles


def _capture_quota_extra() -> dict:
    """Read the cached plan info row and return a small {daily,weekly,captured_at}
    dict suitable for stashing into ProfileMeta.extra["quota"].

    Returns an empty dict if the row is missing or doesn't expose quotaUsage
    (paid/enterprise plans without daily/weekly caps).
    """
    info = vscdb.read_cached_plan_info()
    if not info:
        return {}
    qu = info.get("quotaUsage") or {}
    daily = qu.get("dailyRemainingPercent")
    weekly = qu.get("weeklyRemainingPercent")
    if daily is None and weekly is None:
        return {}
    out: dict = {"captured_at": time.time()}
    if isinstance(daily, (int, float)):
        out["daily_remaining_pct"] = int(daily)
    if isinstance(weekly, (int, float)):
        out["weekly_remaining_pct"] = int(weekly)
    reset = qu.get("dailyResetAtUnix")
    if isinstance(reset, (int, float)):
        out["daily_reset_at"] = int(reset)
    return out


def snapshot_current(label: str = "") -> Profile:
    """Capture the currently active Windsurf account into a Profile object.

    Caller must call .save() to persist.
    """
    auth = vscdb.read_auth_rows()
    if not auth:
        raise RuntimeError("No auth rows found in state.vscdb — is Windsurf logged in?")

    account = vscdb.get_active_account_name() or "unknown"
    install_id = vscdb.get_installation_id() or ""
    jwt = vscdb.get_session_jwt() or ""
    ident = storage_json.read_identity()
    now = time.time()

    slug = _slug(label or account)
    extra: dict = {}
    quota = _capture_quota_extra()
    if quota:
        extra["quota"] = quota
    meta = ProfileMeta(
        slug=slug,
        account_name=account,
        label=label,
        installation_id=install_id,
        session_jwt_preview=jwt[:24],
        created_at=now,
        updated_at=now,
        extra=extra,
    )
    return Profile(meta=meta, auth_rows=auth, identity=ident)


def inherit_persistent_meta(fresh: "Profile", match: "Profile") -> None:
    """Copy fields that should survive an auto-save from `match` into `fresh`.

    `snapshot_current()` only knows about live state — it has no idea what
    the user-supplied label was, when the profile was first created, or when
    we last switched into it. When auto-saving (daemon, `cli save`, TUI
    save / pre-switch save) we must carry those forward, otherwise every
    auto-save zeroes `last_active_at`.
    """
    fresh.meta.slug = match.meta.slug
    fresh.meta.created_at = match.meta.created_at or fresh.meta.created_at
    fresh.meta.label = match.meta.label or fresh.meta.label
    fresh.meta.last_active_at = match.meta.last_active_at


def find_matching_profile(account_name: str, installation_id: str) -> Profile | None:
    """Locate an existing profile that matches the given identity."""
    for p in list_profiles():
        if p.meta.account_name == account_name and (
            not installation_id or p.meta.installation_id == installation_id
        ):
            return p
    return None


def _merge_live_quota(profile: Profile) -> None:
    """Merge live LSP quota into profile.meta.extra, overriding cached values.

    Falls back to cached plan info when the LSP is unavailable (e.g. Windsurf
    not running).  Skips the merge entirely when no quota source is reachable.
    Also refreshes daily_reset_at from the cached plan info if available.
    """
    from . import ratelimit, vscdb

    q = ratelimit.read_quota()
    if q.source == "unknown":
        return
    if profile.meta.extra is None:
        profile.meta.extra = {}
    quota = profile.meta.extra.setdefault("quota", {})
    if q.daily_remaining_pct is not None:
        quota["daily_remaining_pct"] = q.daily_remaining_pct
    if q.weekly_remaining_pct is not None:
        quota["weekly_remaining_pct"] = q.weekly_remaining_pct
    quota["captured_at"] = time.time()
    # Refresh reset time from cached plan info when available
    info = vscdb.read_cached_plan_info()
    if info:
        qu = info.get("quotaUsage") or {}
        reset = qu.get("dailyResetAtUnix")
        if isinstance(reset, (int, float)):
            quota["daily_reset_at"] = int(reset)


def save_current_before_switch() -> str | None:
    """Auto-save the currently active account before switching away.

    Merges live quota into ``extra.quota`` so the TUI can display usage
    for inactive profiles.

    Returns the slug of the saved profile, or *None* if the current
    account has no matching saved profile (nothing to update).
    """
    try:
        current = snapshot_current()
    except RuntimeError:
        return None
    match = find_matching_profile(current.meta.account_name, current.meta.installation_id)
    if not match:
        return None
    inherit_persistent_meta(current, match)
    # Record when this account was last active (we are switching away from it now).
    current.meta.last_active_at = time.time()
    # Merge live quota so it's visible when this profile is inactive
    _merge_live_quota(current)
    current.save()
    return current.meta.slug


def apply_profile(profile: Profile) -> None:
    """Write the profile's auth + identity into the live Windsurf store."""
    if vscdb.is_db_locked():
        raise RuntimeError(
            "state.vscdb is locked — Windsurf is still running. Close it first."
        )
    vscdb.write_auth_rows(profile.auth_rows)
    vscdb.clear_cached_plan_info()
    if profile.identity:
        storage_json.write_identity(profile.identity)
    profile.save()
