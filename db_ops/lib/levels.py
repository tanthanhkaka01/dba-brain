"""The severity vocabulary - the four levels every event, route and alert speaks in.

A root module (``db_ops/levels.py``) until 0.24.0, when the root package stopped holding anything
of its own (rules R41): a vocabulary is a value, so it is ``lib``'s.
"""

from __future__ import annotations

import logging


LOGGING = "logging"
WARNING = "warning"
ERROR = "error"
CRITICAL = "critical"

LEVEL_TO_PYTHON = {
    LOGGING: logging.INFO,
    WARNING: logging.WARNING,
    ERROR: logging.ERROR,
    CRITICAL: logging.CRITICAL,
}


def normalize_level(level: str) -> str:
    value = level.strip().lower()
    if value in ("log", "info", "logging"):
        return LOGGING
    if value in ("warn", "warning"):
        return WARNING
    if value in ("err", "error"):
        return ERROR
    if value in ("crit", "critical"):
        return CRITICAL
    raise ValueError(f"Unsupported level: {level}. Use logging, warning, error, or critical.")
