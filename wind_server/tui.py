"""Textual-based TUI for switching profiles."""
from __future__ import annotations

import json
import time
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Input, Static
from textual.widgets._header import HeaderIcon

from . import profile as prof
from . import ratelimit, vscdb, windsurf_proc


def _active_email() -> str:
    return vscdb.get_active_email() or "—"


def _email_from_profile(p: prof.Profile) -> str:
    email = prof.email_from_profile(p)
    return email or "—"


def _fmt(ts: float) -> str:
    if not ts:
        return "—"
    diff = time.time() - ts
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    if diff < 86400 * 7:
        return f"{int(diff // 86400)}d ago"
    return datetime.fromtimestamp(ts).strftime("%m-%d")


class WindServerTUI(App):
    TITLE = "🚀 Wind Server"
    SUB_TITLE = "Profile Manager"

    CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }

    #status {
        height: auto;
        padding: 1 2;
        margin: 1 2 1 2;
        background: $surface;
        color: $text;
        border: solid $primary;
        content-align: center middle;
    }

    #table {
        height: 1fr;
        margin: 0 2 1 2;
        background: $surface;
        border: solid $primary;
    }

    DataTable {
        background: $surface;
        border: none;
    }

    DataTable > .datatable--header {
        background: $panel;
        color: $text;
        text-style: bold;
    }

    DataTable > .datatable--hover {
        background: $boost;
        color: $text;
    }

    DataTable > .datatable--cursor {
        background: $accent;
        color: auto;
        text-style: bold;
    }

    Footer {
        dock: bottom;
        background: $panel;
        color: $text;
        border-top: solid $background;
    }

    HeaderIcon {
        width: auto;
        padding: 0 1;
    }


    Input {
        dock: bottom;
        margin: 0 2 1 2;
    }

    Screen.-fullscreen Header { display: none; }
    Screen.-fullscreen Footer { display: none; }
    Screen.-fullscreen #status { margin: 0; }
    Screen.-fullscreen #table {
        margin: 0;
        height: 1fr;
        border: none;
    }
    """
    BINDINGS = [
        Binding("enter", "switch_selected", "Switch"),
        Binding("s", "save_current", "Save current"),
        Binding("r", "refresh", "Refresh"),
        Binding("f", "toggle_fullscreen", "Fullscreen"),
        Binding("c", "command_palette", "[u]Commands[/u]"),
        Binding("q", "quit", "[u]Q[/u]uit"),
    ]

    # How often to re-poll live quota for the active account (seconds).
    AUTO_REFRESH_INTERVAL = 15.0

    def __init__(self) -> None:
        super().__init__()
        self.editing_slug: str | None = None
        # Populated in on_mount; needed for in-place cell updates on auto-refresh.
        self._col_daily_key = None
        self._col_weekly_key = None
        self._col_switched_key = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, time_format="%I:%M %p")
        yield Static("", id="status")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        # Replace the default header icon with Commands + Quit.
        header_icon = self.query_one(HeaderIcon)
        header_icon.icon = "[u]C[/u]ommands │ [u]Q[/u]uit"
        header_icon.tooltip = "Open command palette (c) │ Quit (q)"

        table = self.query_one("#table", DataTable)
        keys = table.add_columns("", "Email", "Account", "Daily Used", "Weekly Used", "Last Active")
        # `add_columns` returns the list of ColumnKey objects in order.
        self._col_daily_key = keys[3]
        self._col_weekly_key = keys[4]
        self._col_switched_key = keys[5]
        self.action_refresh()
        # Auto-refresh: only the live status bar + the active row's daily cell.
        # We deliberately do NOT rebuild the whole table on a timer — that
        # would reset cursor position mid-navigation.
        self.set_interval(self.AUTO_REFRESH_INTERVAL, self._tick)

    # --- helpers ---------------------------------------------------------

    def _refresh_status(self) -> None:
        active = vscdb.get_active_account_name() or "(none)"
        email = _active_email()
        running = "running" if windsurf_proc.is_running() else "stopped"
        q = ratelimit.read_quota()

        def _used(rem: int | None) -> str:
            if rem is None:
                return "?% [?% rem]"
            return f"{100 - rem}% [{rem}% rem]"

        warn = "  ⚠️ LOW" if q.is_low else ""
        status_color = "red" if q.is_low else "green"
        self.query_one("#status", Static).update(
            f"[b]Account[/b] [cyan]{active}[/cyan]  •  "
            f"[b]Email[/b] [cyan]{email}[/cyan]  •  "
            f"[b]Windsurf[/b] [magenta]{running}[/magenta]  •  "
            f"[b]Daily[/b] [{status_color}]{_used(q.daily_remaining_pct)}[/{status_color}]  •  "
            f"[b]Weekly[/b] [{status_color}]{_used(q.weekly_remaining_pct)}[/{status_color}]{warn}"
        )

    def _refresh_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        active_email = vscdb.get_active_email()
        active_name = vscdb.get_active_account_name()
        # Live quota for the active row — overrides the stale per-profile
        # `extra.quota` snapshot when available.
        live_q = ratelimit.read_quota()
        live_daily = live_q.daily_remaining_pct if live_q.source != "unknown" else None
        live_weekly = live_q.weekly_remaining_pct if live_q.source != "unknown" else None
        for p in prof.list_profiles():
            is_active = prof._profile_matches_identity(
                p, active_email or "", active_name or ""
            )
            stashed = (p.meta.extra or {}).get("quota") or {}
            # Daily quota
            stale_daily = False
            if is_active and live_daily is not None:
                daily_rem = live_daily
            else:
                daily_rem = stashed.get("daily_remaining_pct")
                reset_at = stashed.get("daily_reset_at") or 0
                captured_at = stashed.get("captured_at") or 0
                if reset_at and time.time() >= reset_at:
                    daily_rem = 100
                else:
                    stale_daily = bool(reset_at and captured_at and captured_at < reset_at)

            # Weekly quota
            stale_weekly = False
            if is_active and live_weekly is not None:
                weekly_rem = live_weekly
            else:
                weekly_rem = stashed.get("weekly_remaining_pct")
                w_reset_at = stashed.get("weekly_reset_at") or 0
                captured_at = stashed.get("captured_at") or 0
                if w_reset_at and time.time() >= w_reset_at:
                    weekly_rem = 100
                else:
                    stale_weekly = bool(w_reset_at and captured_at and captured_at < w_reset_at)

            if is_active:
                marker = "[bold green]▶[/bold green]"
                email_cell = f"[bold green]{_email_from_profile(p)}[/]"
                account_cell = f"[bold green]{p.meta.account_name}[/]"
                switched_cell = "[bold green]active now[/]"

                if isinstance(daily_rem, int):
                    used_daily = 100 - daily_rem
                    d_color = "#ff5555" if used_daily == 100 else "green"
                    daily_cell = f"[bold {d_color}]{used_daily}%{'?' if stale_daily else ''}[/]"
                else:
                    daily_cell = "[bold green]—[/]"

                if isinstance(weekly_rem, int):
                    used_weekly = 100 - weekly_rem
                    w_color = "#ff5555" if used_weekly == 100 else "green"
                    weekly_cell = f"[bold {w_color}]{used_weekly}%{'?' if stale_weekly else ''}[/]"
                else:
                    weekly_cell = "[bold green]—[/]"
            else:
                marker = "[dim]○[/dim]"
                email_cell = _email_from_profile(p)
                account_cell = p.meta.account_name

                if p.meta.last_active_at:
                    switched_cell = _fmt(p.meta.last_active_at)
                else:
                    switched_cell = "—"

                if isinstance(daily_rem, int):
                    used_daily = 100 - daily_rem
                    daily_cell = f"{used_daily}%{'?' if stale_daily else ''}"
                    if used_daily == 100:
                        daily_cell = f"[#ff5555]{daily_cell}[/]"
                else:
                    daily_cell = "—"

                if isinstance(weekly_rem, int):
                    used_weekly = 100 - weekly_rem
                    weekly_cell = f"{used_weekly}%{'?' if stale_weekly else ''}"
                    if used_weekly == 100:
                        weekly_cell = f"[#ff5555]{weekly_cell}[/]"
                else:
                    weekly_cell = "—"

            table.add_row(
                marker,
                email_cell,
                account_cell,
                daily_cell,
                weekly_cell,
                switched_cell,
                key=p.meta.slug,
            )

    def _tick(self) -> None:
        """Lightweight periodic refresh: status bar + active row's daily cell only.

        Skips the full `_refresh_table()` rebuild so the user's cursor /
        selection isn't reset every interval. Non-active rows reflect data
        as of the last time that profile was active and cannot be refreshed
        without switching into them — pressing `r` won't change that either.
        """
        try:
            self._refresh_status()
        except Exception:
            return
        if self._col_daily_key is None:
            return
        active_email = vscdb.get_active_email()
        if not active_email:
            return
        live_q = ratelimit.read_quota()
        live_daily = live_q.daily_remaining_pct if live_q.source != "unknown" else None
        if live_daily is None:
            return
        # Find the matching profile slug to address its row.
        match = prof.find_matching_profile(active_email, vscdb.get_active_account_name() or "")
        if match is None:
            return
        table = self.query_one("#table", DataTable)
        try:
            used_daily = 100 - live_daily
            d_color = "#ff5555" if used_daily == 100 else "green"
            table.update_cell(match.meta.slug, self._col_daily_key, f"[bold {d_color}]{used_daily}%[/]")
            if live_q.weekly_remaining_pct is not None:
                used_weekly = 100 - live_q.weekly_remaining_pct
                w_color = "#ff5555" if used_weekly == 100 else "green"
                table.update_cell(match.meta.slug, self._col_weekly_key, f"[bold {w_color}]{used_weekly}%[/]")
        except Exception:
            # Row may have been removed mid-tick; full refresh will fix it.
            pass

    # --- actions ---------------------------------------------------------

    def action_toggle_fullscreen(self) -> None:
        self.screen.toggle_class("-fullscreen")

    def action_refresh(self) -> None:
        self._refresh_status()
        self._refresh_table()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Switch to the clicked row's profile."""
        self.action_switch_selected()

    def action_switch_selected(self) -> None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        slug = row_key.value
        if not slug:
            return
        try:
            target = prof.load_profile(slug)
        except FileNotFoundError:
            self.notify(f"Profile {slug!r} not found", severity="error")
            return
        # Auto-save current first (captures live quota)
        prof.save_current_before_switch()
        if windsurf_proc.is_running():
            self.notify("Stopping Windsurf...", timeout=2)
            if not windsurf_proc.stop():
                self.notify("Failed to stop Windsurf cleanly", severity="error", timeout=6)
                return
        # Wait until SQLite confirms the DB lock has been released.
        if not windsurf_proc.wait_until_db_unlocked():
            self.notify("DB still locked after Windsurf exit", severity="error", timeout=6)
            return
        try:
            prof.apply_profile(target)
        except Exception as e:
            self.notify(f"Switch failed: {e}", severity="error", timeout=6)
            windsurf_proc.launch()
            return
        windsurf_proc.launch()
        self.notify(f"Switched to {target.meta.slug}")
        self.action_refresh()

    def action_save_current(self) -> None:
        try:
            current = prof.snapshot_current()
            email = prof.email_from_profile(current)
            account = current.meta.account_name
            match = prof.find_matching_profile(email, account)
            if match:
                prof.inherit_persistent_meta(current, match)
                current.save()
                self.notify(f"Updated: {current.meta.slug}")
            else:
                current.meta.slug = prof._ensure_unique_slug(
                    current.meta.slug, email, account
                )
                current.save()
                self.notify(f"Added: {current.meta.slug}")
        except Exception as e:
            self.notify(f"Save failed: {e}", severity="error")
        self.action_refresh()


def run_tui() -> None:
    WindServerTUI().run()
