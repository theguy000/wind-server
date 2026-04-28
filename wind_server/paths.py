"""Filesystem paths used by wind-server."""
from __future__ import annotations

from pathlib import Path

HOME = Path.home()

# Maximum number of backups to keep per file
MAX_BACKUPS = 10


def prune_old_backups(parent_dir: Path, stem: str, max_backups: int = MAX_BACKUPS) -> None:
    """Keep only the most recent max_backups matching .wind-server.*.bak files."""
    try:
        backups = sorted(
            p for p in parent_dir.iterdir()
            if p.is_file() and p.name.startswith(stem) and ".wind-server." in p.name and p.suffix == ".bak"
        )
        backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[max_backups:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass

# Windsurf-managed paths (Linux)
WINDSURF_USER_DIR = HOME / ".config" / "Windsurf" / "User"
GLOBAL_STORAGE = WINDSURF_USER_DIR / "globalStorage"
STATE_VSCDB = GLOBAL_STORAGE / "state.vscdb"
STATE_VSCDB_BACKUP = GLOBAL_STORAGE / "state.vscdb.backup"
STORAGE_JSON = GLOBAL_STORAGE / "storage.json"
WINDSURF_LOG_ROOT = HOME / ".config" / "Windsurf" / "logs"
WINDSURF_ARGV = HOME / ".windsurf" / "argv.json"

# Codeium per-user data (memories, conversation history, etc.)
CODEIUM_WINDSURF_DIR = HOME / ".codeium" / "windsurf"

# wind-server own state
CONFIG_DIR = HOME / ".config" / "wind-server"
PROFILES_DIR = CONFIG_DIR / "profiles"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LOG_FILE = CONFIG_DIR / "wind-server.log"
DAEMON_PID = CONFIG_DIR / "daemon.pid"


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
