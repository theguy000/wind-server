# wind-server

Multi-account manager for the Windsurf IDE.

- **Switch / swap accounts** with one command (or one keystroke in the TUI).
- **Auto-save** the active account into the matching profile whenever it changes.
- **Auto-fail-over** to another profile when your daily quota runs out or the
  server returns 429.

Built around the reverse-engineered identity model documented in
[`docs/windsurf-internals.md`](docs/windsurf-internals.md): a Windsurf account
is identified by a small set of rows in `~/.config/Windsurf/User/globalStorage/state.vscdb`
plus a few telemetry IDs in `storage.json`. The libsecret-stored master
encryption key is shared across all accounts on the same machine, so we only
need to swap the SQLite rows.

## Install

```bash
pipx install /home/istiak/git/wind-server
# or for development:
pip install -e /home/istiak/git/wind-server
```

This installs two console entrypoints: `wind-server` and the short alias `ws`.

## Quick start

```bash
# 1. While logged into account A, capture it.
wind-server add --label personal

# 2. Sign out and into account B inside Windsurf, then capture it too.
wind-server add --label trial-1

# 3. List what you have.
wind-server list

# 4. Swap. (Closes Windsurf, swaps auth, relaunches.)
wind-server switch personal

# 5. Open the TUI.
wind-server ui
```

## Auto-save daemon

```bash
mkdir -p ~/.config/systemd/user
cp systemd/wind-server.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wind-server.service
journalctl --user -fu wind-server.service
```

The daemon watches `state.vscdb`; whenever the active account name or
installation id changes, it snapshots the new state into the matching
profile (or creates a new one).

## Auto-fail-over on rate limit

```bash
wind-server auto --interval 60 --threshold 5 --workspace ~/git/auto-ds
```

Polls `GetUserStatus` every 60 seconds and tails Windsurf's logs for `429` /
`RESOURCE_EXHAUSTED`. When daily remaining drops below the threshold (or a
log hit is detected), it rotates to the next saved profile and restarts
Windsurf at the given workspace.

## Commands

| Command | What it does |
|---|---|
| `wind-server list` | Show profiles and which one is active. |
| `wind-server add [--label X]` | Capture the current account as a new profile. |
| `wind-server save [SLUG]` | Update an existing profile from current state. |
| `wind-server switch SLUG` | Stop Windsurf, swap auth, relaunch. |
| `wind-server status [--json]` | Print active account, quota, and rate-limit signals. |
| `wind-server auto` | Watchdog loop that auto-rotates on quota exhaustion. |
| `wind-server daemon` | Auto-save daemon (intended for systemd --user). |
| `wind-server ui` | Launch the Textual TUI. |

## Profile layout

Each profile lives in `~/.config/wind-server/profiles/<slug>/`:

- `auth_rows.json` — every auth-shaped row from `state.vscdb`.
- `identity.json` — the telemetry id triple from `storage.json`.
- `meta.json` — display name, label, timestamps, JWT preview.

Whenever wind-server modifies `state.vscdb` or `storage.json`, it first
copies the file to `<file>.wind-server.<unix>.bak`. Nothing is ever deleted
in place.

## Caveats

- Windsurf **must be closed** while we rewrite `state.vscdb`. The `switch`
  command does this for you.
- The encrypted `secret://` blobs in `state.vscdb` are decrypted with a
  per-machine key in libsecret. Profiles captured on machine A will not
  decrypt on machine B.
- Rate-limit detection parses the `GetUserStatus` response heuristically
  (no proto schema). If Codeium changes the wire format, fall back to
  `--threshold 0` plus log-tail-only detection.
- Telemetry (`RecordAnalyticsEvent`) is **not** blocked. Per
  `docs/windsurf-internals.md`, blocking it breaks the IDE.

## License

Private — for personal use.
