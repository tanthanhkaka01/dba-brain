"""Quoting and small shared helpers for the backup/restore shell and SQL paths.

Five of these were written out twice — once in ``certificate.py`` and once in
``restore_database.py`` — and three more twice again in ``copy_backup.py`` and
``delete_backup.py``. Identical each time, which is what made it easy to keep doing and dangerous
to leave: these are the functions that decide whether a database name or a path is safely escaped
before it is pasted into T-SQL or a PowerShell command line. A rule about escaping with two copies
is a rule with two versions, and the second one is found by whoever is reading the incident.

App-side rather than in ``common`` because they are this app's own plumbing: the shapes they quote
for (``sqlcmd`` arguments, a staged ``.ps1``, a backup timestamp) belong to how backup_restore
drives its hosts, and nothing else in the tree asks these questions.
"""

from __future__ import annotations

import logging
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from db_ops.logging_ops import log_event


if TYPE_CHECKING:  # pragma: no cover - the config type is only needed for annotations
    from db_ops.backup_restore.config import BackupRestoreConfig


_BACKUP_TIMESTAMP_RE = re.compile(r"_(\d{8})_(\d{6})")


def _build_sqlcmd_auth_args(config: BackupRestoreConfig) -> list[str]:
    # Lazy: copy_backup imports this module for its own quoting helpers, so a module-level
    # import here would close the loop. One direction is enough at import time.
    from db_ops.backup_restore.copy_backup import resolve_password_ref

    if not config.restore_sql_username and not config.restore_sql_password_env:
        return ["-E"]
    if not config.restore_sql_username or not config.restore_sql_password_env:
        raise ValueError("target.sql_username and target.sql_password_env are both required when SQL auth is enabled.")
    password = resolve_password_ref(config.restore_sql_password_env)
    if not password:
        raise RuntimeError(f"Password ref not found in environment or secret_text.json: {config.restore_sql_password_env}")
    return ["-U", config.restore_sql_username, "-P", password]


def _escape_identifier(value: str) -> str:
    return value.replace("]", "]]")


def _escape_sql_string(value: str) -> str:
    return value.replace("'", "''")


def _log_progress(logger: logging.Logger | None, message: str) -> None:
    if logger and not hasattr(sys.stdout, "path"):
        log_event(logger, level="logging", message=message)
    print(message, flush=True)


def _ps_array(values: list[str]) -> str:
    return ", ".join(_ps_quote(value) for value in values)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _write_temp_powershell_script(script: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8")
    with handle:
        handle.write(script)
    return Path(handle.name)
