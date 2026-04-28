"""Textual-based TUI for switching profiles."""
from __future__ import annotations

import json
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Input, Static

from . import profile as prof
from . import ratelimit, vscdb, windsurf_proc


def _active_email() -> str:
    return vscdb.get_active_email() or "—"


def _email_from_profile(p: prof.Profile) -> str:
    val = p.auth_rows.get("codeium.windsurf", "")
    if not val:
        return "—"
    try:
        data = json.loads(val)
    except json.JSONDecodeError:
        return "—"
    return data.get("lastLoginEmail") or "—"


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%I:%M %p %m-%d") if ts else "—"


class WindServerTUI(App):
    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }

    #header {
        dock: top;
        height: 3;
        background: $primary;
        color: $text;
        padding: 0 2;
        border-bottom: solid $primary-darken-2;
    }

    #header-title {
        width: 1fr;
        content-align: left middle;
    }

    #header-clock {
        width: auto;
        content-align: right middle;
        color: $text-muted;
    }

    #status {
        dock: top;
        height: 3;
        padding: 0 2;
        background: $surface;
        color: $text;
        border-bottom: solid $primary-darken-2;
    }

    #table {
        height: 1fr;
        background: $surface;
    }

    DataTable {
        background: $surface;
        border: none;
    }

    DataTable .datatable--header {
        background: $primary-darken-1;
        color: $text;
        border-bottom: solid $primary-darken-2;
    }

    DataTable .datatable--header-row {
        background: $primary-darken-1;
    }

    DataTable .datatable--row {
        background: $surface;
        color: $text;
    }

    DataTable .datatable--row-highlight {
        background: $primary-darken-2;
        color: $text;
    }

    DataTable .datatable--cursor-row {
        background: $primary-darken-2;
        color: $text;
    }

    Footer {
        dock: bottom;
        background: $primary-darken-1;
        color: $text;
        border-top: none;
    }

    Input {
        dock: bottom;
        margin: 0 2 1 2;
    }
    """
    BINDINGS = [
        Binding("enter", "switch_selected", "Switch"),
        Binding("s", "save_current", "Save current"),
        Binding("r", "refresh", "Refresh"),
        Binding("l", "edit_label", "Label"),
        Binding("q", "quit", "Quit"),
    ]

    # How often to re-poll live quota for the active account (seconds).
    AUTO_REFRESH_INTERVAL = 15.0

    def __init__(self) -> None:
        super().__init__()
        self.editing_slug: str | None = None
        # Populated in on_mount; needed for in-place cell updates on auto-refresh.
        self._col_daily_key = None
        self._col_switched_key = None

    def compose(self) -> ComposeResult:
        now = datetime.now().strftime("%H:%M")
        yield Horizontal(
            Static("[b]Wind Server[/b]  [dim]Profile Manager[/dim]", id="header-title"),
            Static(now, id="header-clock"),
            id="header",
        )
        yield Static("", id="status")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        keys = table.add_columns("", "email", "account", "label", "daily used", "last active")
        # `add_columns` returns the list of ColumnKey objects in order.
        self._col_daily_key = keys[4]
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

        warn = "  ⚠ LOW" if q.is_low else ""
        status_color = "yellow" if q.is_low else "green"
        self.query_one("#status", Static).update(
            f"  [b]Account[/b] {active}  │  [b]Email[/b] {email}  │  [b]Windsurf[/b] {running}  │  "
            f"[b]Daily[/b] [{status_color}]{_used(q.daily_remaining_pct)}[/{status_color}]  │  "
            f"[b]Weekly[/b] [{status_color}]{_used(q.weekly_remaining_pct)}[/{status_color}]{warn}"
        )

    def _refresh_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        active_name = vscdb.get_active_account_name()
        active_install = vscdb.get_installation_id()
        # Live quota for the active row — overrides the stale per-profile
        # `extra.quota` snapshot when available.
        live_q = ratelimit.read_quota()
        live_daily = live_q.daily_remaining_pct if live_q.source != "unknown" else None
        for p in prof.list_profiles():
            is_active = (
                p.meta.account_name == active_name
                and (not active_install or p.meta.installation_id == active_install)
            )
            stashed = (p.meta.extra or {}).get("quota") or {}
            if is_active and live_daily is not None:
                daily_rem = live_daily
                stale = False
            else:
                daily_rem = stashed.get("daily_remaining_pct")
                # Mark as stale if the cache predates the most recent daily reset.
                reset = stashed.get("daily_reset_at") or 0
                captured = stashed.get("captured_at") or 0
                stale = bool(reset and captured and captured < reset)
            if isinstance(daily_rem, int):
                daily_cell = f"{100 - daily_rem}%{'?' if stale else ''}"
            else:
                daily_cell = "—"

            if is_active:
                switched_cell = "active now"
            elif p.meta.last_active_at:
                switched_cell = _fmt(p.meta.last_active_at)
            else:
                switched_cell = "—"

            table.add_row(
                "●" if is_active else "○",
                _email_from_profile(p),
                p.meta.account_name,
                p.meta.label or "-",
                daily_cell,
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
            now = datetime.now().strftime("%H:%M")
            self.query_one("#header-clock", Static).update(now)
        except Exception:
            return
        if self._col_daily_key is None:
            return
        active_name = vscdb.get_active_account_name()
        active_install = vscdb.get_installation_id()
        if not active_name:
            return
        live_q = ratelimit.read_quota()
        live_daily = live_q.daily_remaining_pct if live_q.source != "unknown" else None
        # Find the matching profile slug to address its row.
        match = prof.find_matching_profile(active_name, active_install or "")
        if match is None:
            return
        table = self.query_one("#table", DataTable)
        try:
            if live_daily is not None:
                table.update_cell(match.meta.slug, self._col_daily_key, f"{100 - live_daily}%")
            if self._col_switched_key:
                table.update_cell(
                    match.meta.slug, self._col_switched_key,
                    "active now",
                )
        except Exception:
            # Row may have been removed mid-tick; full refresh will fix it.
            pass

    # --- actions ---------------------------------------------------------

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
            match = prof.find_matching_profile(current.meta.account_name, current.meta.installation_id)
            if match:
                prof.inherit_persistent_meta(current, match)
                current.save()
                self.notify(f"Updated: {current.meta.slug}")
            else:
                current.save()
                self.notify(f"Added: {current.meta.slug}")
        except Exception as e:
            self.notify(f"Save failed: {e}", severity="error")
        self.action_refresh()

    def action_edit_label(self) -> None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        slug = row_key.value
        if not slug:
            return
        self.editing_slug = slug
        inp = Input(placeholder=f"New label for {slug}")
        # Add explicit Esc binding
        inp.bind("escape", "cancel_edit", description="Cancel")
        self.mount(inp)
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.editing_slug:
            try:
                p = prof.load_profile(self.editing_slug)
                p.meta.label = event.value
                p.save()
                self.notify(f"Labeled {self.editing_slug} → {event.value!r}")
            except Exception as e:
                self.notify(f"Failed: {e}", severity="error")
        self.editing_slug = None
        event.input.remove()
        self.action_refresh()

    def action_cancel_edit(self) -> None:
        """Cancel label editing (bound to Esc in edit mode)."""
        if self.editing_slug:
            self.editing_slug = None
            # Find and remove the Input widget
            for inp in self.query(Input):
                inp.remove()
                break


def run_tui() -> None:
    WindServerTUI().run()
