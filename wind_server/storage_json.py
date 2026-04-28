"""Read/write the small subset of storage.json that contributes to identity.

Only the telemetry IDs are part of a profile. Everything else (theme,
windowSplash, profileAssociations, ...) is left untouched.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import paths

IDENTITY_KEYS: tuple[str, ...] = (
    "telemetry.machineId",
    "telemetry.devDeviceId",
    "telemetry.sqmId",
)


def read_identity(path: Path | None = None) -> dict[str, str]:
    path = path if path is not None else paths.STORAGE_JSON
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return {k: data[k] for k in IDENTITY_KEYS if k in data}


def write_identity(values: dict[str, str], path: Path | None = None) -> None:
    path = path if path is not None else paths.STORAGE_JSON
    if not path.exists():
        raise FileNotFoundError(path)
    backup = path.with_suffix(path.suffix + f".wind-server.{int(time.time())}.bak")
    shutil.copy2(path, backup)
    paths.prune_old_backups(path.parent, path.stem)

    data = json.loads(path.read_text())
    for k in IDENTITY_KEYS:
        if k in values:
            data[k] = values[k]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=4))
    tmp.replace(path)
