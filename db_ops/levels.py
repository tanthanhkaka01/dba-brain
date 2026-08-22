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
