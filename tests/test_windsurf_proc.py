"""Tests for the Windsurf process detector.

Regression: on most Linux builds Windsurf runs as a generic ``electron``
binary; only the cmdline mentions ``windsurf``. The detector must match
those, otherwise ``stop()`` becomes a no-op and switching profiles ends
up launching a second Windsurf on top of the live one.
"""
from __future__ import annotations

from wind_server.windsurf_proc import _is_windsurf_proc


def test_matches_electron_with_windsurf_app():
    """Real-world example from Manjaro/Arch packaging."""
    info = {
        "name": "electron",
        "exe": "/usr/lib/electron39/electron",
        "cmdline": [
            "/usr/lib/electron39/electron",
            "--app=/usr/share/windsurf/resources/app",
        ],
    }
    assert _is_windsurf_proc(info) is True


def test_matches_electron_helper_via_resources_path():
    info = {
        "name": "electron",
        "exe": "/usr/lib/electron39/electron",
        "cmdline": [
            "/usr/lib/electron39/electron",
            "/usr/share/windsurf/resources/app/extensions/markdown-language-features/dist/serverWorkerMain",
            "--node-ipc",
        ],
    }
    assert _is_windsurf_proc(info) is True


def test_matches_when_name_contains_windsurf():
    info = {
        "name": "windsurf",
        "exe": "/usr/bin/windsurf",
        "cmdline": ["/usr/bin/windsurf"],
    }
    assert _is_windsurf_proc(info) is True


def test_matches_capitalised_windsurf_in_path():
    """Crashpad handler logs into ``~/.config/Windsurf/Crashpad``."""
    info = {
        "name": "chrome_crashpad_handler",
        "exe": "/usr/lib/electron39/chrome_crashpad_handler",
        "cmdline": [
            "/usr/lib/electron39/chrome_crashpad_handler",
            "--database=/home/me/.config/Windsurf/Crashpad",
        ],
    }
    assert _is_windsurf_proc(info) is True


def test_does_not_match_wind_server_itself():
    """The CLI/TUI lives at .../wind-server (hyphen) and must not be
    picked up — otherwise stop() would SIGTERM the very process trying
    to perform the switch."""
    info = {
        "name": "python3",
        "exe": "/usr/bin/python3",
        "cmdline": [
            "/home/me/.local/bin/wind-server",
            "switch",
            "personal",
        ],
    }
    assert _is_windsurf_proc(info) is False


def test_does_not_match_unrelated_process_mentioning_windsurf_as_string():
    """A python -c snippet that contains the literal word 'windsurf'
    inside source code (no path separator) must not be matched."""
    info = {
        "name": "python3",
        "exe": "/usr/bin/python3",
        "cmdline": [
            "python3",
            "-c",
            "print('searching for windsurf in /proc')",
        ],
    }
    assert _is_windsurf_proc(info) is False


def test_handles_missing_fields_gracefully():
    assert _is_windsurf_proc({}) is False
    assert _is_windsurf_proc({"name": None, "exe": None, "cmdline": None}) is False
