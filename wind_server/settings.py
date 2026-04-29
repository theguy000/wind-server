"""Persistent user settings stored in ~/.config/wind-server/settings.json.

Settings are simple key-value pairs.  Known keys are listed in DEFAULTS;
unknown keys are preserved as-is so forward-compatible configs survive
downgrades.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .log import get as get_logger
from .paths import SETTINGS_FILE, ensure_dirs

log = get_logger("settings")

# Default values for recognized settings.  Anything not listed here is
# still accepted and round-tripped, but has no effect on built-in
# behaviour.
DEFAULTS: dict[str, Any] = {
    "auto_threshold_pct": 5,
    "auto_cooldown_seconds": 300.0,
    "auto_interval_seconds": 60,
    "default_workspace": None,
}


def _read_raw() -> dict[str, Any]:
    """Return the raw settings dict from disk, or an empty dict."""
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read settings: %s", exc)
        return {}


def _write_raw(data: dict[str, Any]) -> None:
    ensure_dirs()
    SETTINGS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load() -> dict[str, Any]:
    """Return the effective settings dict (defaults overlaid with user overrides)."""
    merged = dict(DEFAULTS)
    merged.update(_read_raw())
    return merged


def get(key: str) -> Any:
    """Return a single setting value, falling back to the default."""
    return load().get(key, DEFAULTS.get(key))


def set(key: str, value: Any) -> None:
    """Persist a single key=value pair to the settings file."""
    data = _read_raw()

    # Coerce numeric strings to the type of the default so that
    # ``wind-server set auto_threshold_pct 10`` stores an int, not a string.
    if key in DEFAULTS and DEFAULTS[key] is not None:
        default_type = type(DEFAULTS[key])
        if isinstance(value, str):
            try:
                value = default_type(value)
            except (ValueError, TypeError):
                pass

    data[key] = value
    _write_raw(data)
    log.debug("set %s = %r", key, value)


def unset(key: str) -> bool:
    """Remove a key from user settings.  Returns True if the key existed."""
    data = _read_raw()
    if key not in data:
        return False
    del data[key]
    _write_raw(data)
    log.debug("unset %s", key)
    return True


def list_all() -> dict[str, Any]:
    """Return the full effective settings dict (for display)."""
    return load()
