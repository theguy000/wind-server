"""wind-server CLI."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime

import click

from . import paths, profile as prof
from . import ratelimit, vscdb, windsurf_proc
from .paths import ensure_dirs


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %I:%M %p")


@click.group(help="Multi-account manager for Windsurf IDE.")
def main() -> None:
    ensure_dirs()


@main.command("list", help="List all saved profiles and the active account.")
def cmd_list() -> None:
    active_name = vscdb.get_active_account_name()
    active_install = vscdb.get_installation_id()
    profiles = prof.list_profiles()
    if not profiles:
        click.echo("No saved profiles. Run `wind-server add` to capture the current account.")
    click.echo(f"{'Active':6} {'Slug':20} {'Account':28} {'Label':14} {'Last active':20}")
    click.echo("-" * 92)
    seen_active = False
    for p in profiles:
        is_active = (
            p.meta.account_name == active_name
            and (not active_install or p.meta.installation_id == active_install)
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
    if active_name and not seen_active:
        click.echo()
        click.echo(
            f"  ! Active account '{active_name}' is NOT in your saved profiles. "
            "Run `wind-server add` to capture it."
        )


@main.command("add", help="Capture the currently active Windsurf account as a new profile.")
@click.option("--label", default="", help="Optional human-friendly alias (e.g. 'personal').")
@click.option("--name", default="", help="Override the slug. Defaults to the account name.")
def cmd_add(label: str, name: str) -> None:
    # label is passed separately; name only affects slug if provided
    p = prof.snapshot_current(label=label)
    if name:
        p.meta.slug = prof._slug(name)
    target_dir = paths.PROFILES_DIR / p.meta.slug
    if target_dir.exists():
        click.confirm(f"Profile '{p.meta.slug}' already exists. Overwrite?", abort=True)
    p.save()
    click.echo(f"Saved profile: {p.meta.slug}  ({p.meta.account_name})")


@main.command("save", help="Update an existing profile from the current state, or auto-detect.")
@click.argument("slug", required=False)
def cmd_save(slug: str | None) -> None:
    fresh = prof.snapshot_current()
    if slug is None:
        existing = prof.find_matching_profile(fresh.meta.account_name, fresh.meta.installation_id)
        if existing:
            slug = existing.meta.slug
        else:
            slug = fresh.meta.slug
            click.echo(f"No matching profile found; creating new one: {slug}")
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
        click.echo(f"Profile '{slug}' not found.", err=True)
        sys.exit(1)

    # Auto-save current state into its matching profile before switching.
    saved = prof.save_current_before_switch()
    if saved:
        click.echo(f"  ↳ auto-saved current state into '{saved}'")

    if windsurf_proc.is_running():
        click.echo("Stopping Windsurf...")
        if not windsurf_proc.stop():
            click.echo("Failed to stop Windsurf cleanly.", err=True)
            sys.exit(2)
    # Wait until SQLite confirms the DB lock has been released.
    if not windsurf_proc.wait_until_db_unlocked():
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
@click.option("--interval", default=60, type=int, help="Polling interval (seconds).")
@click.option("--threshold", default=ratelimit.DEFAULT_THRESHOLD_PCT, type=int,
              help="Daily remaining %% under which to switch.")
@click.option("--workspace", default=None, help="Workspace to reopen on relaunch.")
def cmd_auto(interval: int, threshold: int, workspace: str | None) -> None:
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
            click.echo(f"[{datetime.now():%H:%M:%S}] watchdog error: {e}")
            time.sleep(interval)


def _auto_switch(workspace: str | None) -> None:
    """Pick the next profile in round-robin order and switch to it."""
    profiles = prof.list_profiles()
    if len(profiles) < 2:
        click.echo("  ! need at least 2 saved profiles to auto-switch.")
        return
    active_name = vscdb.get_active_account_name()
    install = vscdb.get_installation_id()
    # Find current index
    current_idx = -1
    for i, p in enumerate(profiles):
        if p.meta.account_name == active_name and (
            not install or p.meta.installation_id == install
        ):
            current_idx = i
            break
    next_idx = (current_idx + 1) % len(profiles)
    target = profiles[next_idx]
    if target.meta.account_name == active_name:
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


if __name__ == "__main__":
    main()
