"""wind-server CLI."""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime

import click

from . import paths, profile as prof
from . import ratelimit, vscdb, windsurf_proc
from .log import get as get_logger
from .paths import ensure_dirs
from . import settings as _settings

log = get_logger("cli")


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %I:%M %p")


@click.group(help="Multi-account manager for Windsurf IDE.")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def main(verbose: bool) -> None:
    from .log import setup
    setup(verbose=verbose)
    ensure_dirs()


@main.command("list", help="List all saved profiles and the active account.")
def cmd_list() -> None:
    active_email = vscdb.get_active_email()
    active_name = vscdb.get_active_account_name()
    profiles = prof.list_profiles()
    if not profiles:
        click.echo("No saved profiles. Run `wind-server add` to capture the current account.")
    click.echo(f"{'Active':6} {'Slug':20} {'Account':28} {'Label':14} {'Last active':20}")
    click.echo("-" * 92)
    seen_active = False
    for p in profiles:
        is_active = prof._profile_matches_identity(
            p, active_email or "", active_name or ""
        )
        if is_active:
            seen_active = True
        marker = "  *  " if is_active else "     "
        switched = (
            "active now" if is_active
            else _fmt_ts(p.meta.last_active_at) if p.meta.last_active_at
            else "—"
        )
        click.echo(
            f"{marker:6} {p.meta.slug:20} {p.meta.account_name:28} "
            f"{p.meta.label or '-':14} {switched:16}"
        )
    if (active_email or active_name) and not seen_active:
        click.echo()
        click.echo(
            f"  ! Active account '{active_name}' ({active_email or 'no email'}) is NOT in your saved profiles. "
            "Run `wind-server add` to capture it."
        )


@main.command("add", help="Capture the currently active Windsurf account as a new profile.")
@click.option("--label", default="", help="Optional human-friendly alias (e.g. 'personal').")
@click.option("--name", default="", help="Override the slug. Defaults to the account name.")
def cmd_add(label: str, name: str) -> None:
    # label is passed separately; name only affects slug if provided
    p = prof.snapshot_current(label=label)
    email = prof.email_from_profile(p)
    if name:
        p.meta.slug = prof._ensure_unique_slug(prof._slug(name), email, p.meta.account_name)
    target_dir = paths.PROFILES_DIR / p.meta.slug
    if target_dir.exists():
        click.confirm(f"Profile '{p.meta.slug}' already exists. Overwrite?", abort=True)
    p.save()
    click.echo(f"Saved profile: {p.meta.slug}  ({p.meta.account_name})")


@main.command("save", help="Update an existing profile from the current state, or auto-detect.")
@click.argument("slug", required=False)
def cmd_save(slug: str | None) -> None:
    fresh = prof.snapshot_current()
    email = prof.email_from_profile(fresh)
    account = fresh.meta.account_name
    if slug is None:
        existing = prof.find_matching_profile(email, account)
        if existing:
            slug = existing.meta.slug
        else:
            slug = prof._ensure_unique_slug(fresh.meta.slug, email, account)
            click.echo(f"No matching profile found; creating new one: {slug}")
    else:
        # Explicit slug: still ensure we don't overwrite a different identity
        slug = prof._ensure_unique_slug(slug, email, account)
    fresh.meta.slug = slug
    # Preserve persistent meta (created_at, label, last_active_at) if the
    # profile already exists.
    target_dir = paths.PROFILES_DIR / slug
    if (target_dir / "meta.json").exists():
        try:
            old = prof.load_profile(slug)
            prof.inherit_persistent_meta(fresh, old)
        except Exception:
            pass
    fresh.save()
    click.echo(f"Updated profile: {slug}")


@main.command("switch", help="Switch to a saved profile (will close & relaunch Windsurf).")
@click.argument("slug")
@click.option("--no-restart", is_flag=True, help="Don't relaunch Windsurf after switching.")
@click.option("--workspace", default=None, help="Workspace folder to reopen on relaunch.")
def cmd_switch(slug: str, no_restart: bool, workspace: str | None) -> None:
    try:
        target = prof.load_profile(slug)
    except FileNotFoundError:
        log.error("Profile not found: %s", slug)
        click.echo(f"Profile '{slug}' not found.", err=True)
        sys.exit(1)

    # Auto-save current state into its matching profile before switching.
    saved = prof.save_current_before_switch()
    if saved:
        click.echo(f"  ↳ auto-saved current state into '{saved}'")

    if windsurf_proc.is_running():
        click.echo("Stopping Windsurf...")
        if not windsurf_proc.stop():
            log.error("Failed to stop Windsurf cleanly")
            click.echo("Failed to stop Windsurf cleanly.", err=True)
            sys.exit(2)
    # Wait until SQLite confirms the DB lock has been released.
    if not windsurf_proc.wait_until_db_unlocked():
        log.error("DB still locked after Windsurf exit")
        click.echo("DB still locked after Windsurf exit.", err=True)
        sys.exit(2)

    prof.apply_profile(target)
    click.echo(f"Switched to: {target.meta.slug}  ({target.meta.account_name})")

    if not no_restart:
        if windsurf_proc.launch(workspace):
            click.echo("Relaunched Windsurf.")
        else:
            click.echo("Could not find Windsurf binary on PATH; relaunch it manually.")


@main.command("status", help="Show active account, quota, and rate-limit signals.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def cmd_status(as_json: bool) -> None:
    active = vscdb.get_active_account_name()
    install = vscdb.get_installation_id()
    jwt = vscdb.get_session_jwt() or ""
    running = windsurf_proc.is_running()

    quota = ratelimit.read_quota()

    log_hit = ratelimit.scan_recent_logs()

    info = {
        "active_account": active,
        "installation_id": install,
        "windsurf_running": running,
        "quota": {
            "daily_remaining_pct": quota.daily_remaining_pct,
            "weekly_remaining_pct": quota.weekly_remaining_pct,
            "source": quota.source,
            "rate_limited": quota.rate_limited,
            "is_low": quota.is_low,
        },
        "log_rate_limit_hit": log_hit,
    }
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return

    def _used(rem: int | None) -> str:
        if rem is None:
            return "?% [?% remaining]"
        return f"{100 - rem}% [{rem}% remaining]"

    click.echo(f"Active account     : {active or '(none)'}")
    click.echo(f"Installation id    : {install or '(none)'}")
    click.echo(f"Windsurf running   : {'yes' if running else 'no'}")
    click.echo(f"Daily usage        : {_used(quota.daily_remaining_pct)}")
    click.echo(f"Weekly usage       : {_used(quota.weekly_remaining_pct)}")
    click.echo(f"Quota source       : {quota.source}")
    click.echo(f"Recent log 429s    : {'yes' if log_hit else 'no'}")
    click.echo(f"Should auto-switch : {'YES' if (quota.is_low or log_hit) else 'no'}")


@main.command("auto", help="Run the rate-limit watchdog: auto-switch profiles when quota runs out.")
@click.option("--interval", default=None, type=int, help="Polling interval (seconds).")
@click.option("--threshold", default=None, type=int,
              help="Daily remaining %% under which to switch.")
@click.option("--workspace", default=None, help="Workspace to reopen on relaunch.")
def cmd_auto(interval: int | None, threshold: int | None, workspace: str | None) -> None:
    s = _settings.load()
    interval = interval if interval is not None else s.get("auto_interval_seconds", 60)
    threshold = threshold if threshold is not None else s.get("auto_threshold_pct", ratelimit.DEFAULT_THRESHOLD_PCT)
    workspace = workspace or s.get("default_workspace")
    click.echo(f"wind-server watchdog: interval={interval}s threshold={threshold}%")
    while True:
        try:
            # Skip check if cooldown from recent switch is active
            if ratelimit.is_switch_cooldown_active():
                click.echo(f"[{datetime.now():%H:%M:%S}] cooldown active — skipping check")
                time.sleep(interval)
                continue

            q = ratelimit.read_quota()
            # Check threshold manually since we don't mutate DEFAULT_THRESHOLD_PCT
            is_low = q.rate_limited or (
                q.daily_remaining_pct is not None and q.daily_remaining_pct < threshold
            )
            log_hit = ratelimit.scan_recent_logs()
            if is_low or log_hit:
                click.echo(f"[{datetime.now():%H:%M:%S}] quota exhausted — switching")
                _auto_switch(workspace)
            else:
                click.echo(
                    f"[{datetime.now():%H:%M:%S}] ok  "
                    f"daily={q.daily_remaining_pct} weekly={q.weekly_remaining_pct} log429={log_hit}"
                )
            time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("stopped")
            return
        except Exception as e:
            log.warning("watchdog error: %s", e)
            click.echo(f"[{datetime.now():%H:%M:%S}] watchdog error: {e}")
            time.sleep(interval)


def _auto_switch(workspace: str | None) -> None:
    """Pick the next profile in round-robin order and switch to it."""
    profiles = prof.list_profiles()
    if len(profiles) < 2:
        click.echo("  ! need at least 2 saved profiles to auto-switch.")
        return
    active_email = vscdb.get_active_email()
    active_name = vscdb.get_active_account_name() or ""
    # Find current index
    current_idx = -1
    for i, p in enumerate(profiles):
        if prof._profile_matches_identity(p, active_email or "", active_name):
            current_idx = i
            break
    next_idx = (current_idx + 1) % len(profiles)
    target = profiles[next_idx]
    if prof._profile_matches_identity(target, active_email or "", active_name):
        click.echo("  ! only one usable profile; cannot rotate.")
        return
    click.echo(f"  → switching to '{target.meta.slug}'")
    # Auto-save current before stopping Windsurf (so live quota is captured)
    prof.save_current_before_switch()
    if windsurf_proc.is_running():
        if not windsurf_proc.stop():
            click.echo("  ! failed to stop Windsurf cleanly.", err=True)
            return
    # Wait until SQLite confirms the DB lock has been released.
    if not windsurf_proc.wait_until_db_unlocked():
        click.echo("  ! DB still locked after Windsurf exit.", err=True)
        return
    prof.apply_profile(target)
    windsurf_proc.launch(workspace)


@main.command("ui", help="Launch the interactive TUI for switching profiles.")
def cmd_ui() -> None:
    from .tui import run_tui  # lazy import; textual is heavy
    run_tui()


@main.command("daemon", help="Run the auto-save daemon (watches state.vscdb for account changes).")
def cmd_daemon() -> None:
    from .daemon import run_daemon  # lazy import
    run_daemon()


@main.group("settings", help="View or change persistent settings.")
def cmd_settings() -> None:
    pass


@cmd_settings.command("list", help="Show all settings and their current values.")
def cmd_settings_list() -> None:
    all_settings = _settings.list_all()
    defaults = _settings.DEFAULTS
    for key in sorted(all_settings):
        val = all_settings[key]
        is_default = key not in defaults or val == defaults.get(key)
        marker = "" if is_default else " *"
        click.echo(f"  {key:30} {val!r}{marker}")
    click.echo()
    click.echo("  * = overridden from default")


@cmd_settings.command("get", help="Print the value of a single setting.")
@click.argument("key")
def cmd_settings_get(key: str) -> None:
    val = _settings.get(key)
    if val is None:
        click.echo(f"{key}: (not set)")
    else:
        click.echo(f"{key}: {val!r}")


@cmd_settings.command("set", help="Set a setting value. Persisted to ~/.config/wind-server/settings.json.")
@click.argument("key")
@click.argument("value")
def cmd_settings_set(key: str, value: str) -> None:
    _settings.set(key, value)
    click.echo(f"Set {key} = {value!r}")


@cmd_settings.command("unset", help="Remove a setting override (reverts to default).")
@click.argument("key")
def cmd_settings_unset(key: str) -> None:
    if _settings.unset(key):
        click.echo(f"Unset {key}")
    else:
        click.echo(f"{key} was not overridden")


if __name__ == "__main__":
    main()
