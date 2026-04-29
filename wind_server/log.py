"""Shared logging configuration for wind-server."""
from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s [wind-server] %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"


def get(name: str) -> logging.Logger:
    """Return a logger with the wind-server namespace.

    Call ``setup()`` once at entry points (CLI, daemon) to configure
    formatting and verbosity.  If ``setup()`` was never called the
    library default (warnings+) applies.
    """
    return logging.getLogger(f"wind_server.{name}")


def setup(*, verbose: bool = False) -> None:
    """Configure the root wind-server logger.

    Must be called once from the CLI / daemon entry point before any
    module logs.  Safe to call multiple times (idempotent).
    """
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root = logging.getLogger("wind_server")
    root.setLevel(level)
    # Avoid duplicate handlers on repeated calls.
    if not root.handlers:
        root.addHandler(handler)
