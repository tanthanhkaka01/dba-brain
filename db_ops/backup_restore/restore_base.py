"""A SQL Server restore: what every part of it shares - the candidate a restore is planned as, and how a step is logged.

Split out of ``backup_restore/restore_database.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``restore_database``
re-exports every name, so no import changes.
"""

from __future__ import annotations
from db_ops.backup_restore.shell_quoting import _BACKUP_TIMESTAMP_RE, backup_time_from_name, _build_sqlcmd_auth_args, _escape_identifier, _escape_sql_string, _ps_array, _ps_quote  # noqa: F401 - one definition
from dataclasses import dataclass
from pathlib import Path
from db_ops.backup_restore.sanitize import compact_log_value, sanitize_text
from db_ops.logging_ops import log_event


@dataclass(frozen=True)
class RestoreCandidate:
    source_key: str
    source_database_name: str
    restore_database_name: str
    backup_file_unc: Path
    backup_file_on_vm: Path
    restore_data_file_on_vm: Path
    restore_log_file_on_vm: Path


def _emit_restore_log(logger: object | None, message: str, *, level: str = "logging") -> None:
    if logger:
        log_event(logger, level=level, message=sanitize_text(message))


def _format_metadata(**metadata: object) -> str:
    return " ".join(f"{key}={compact_log_value(value)}" for key, value in metadata.items() if value is not None)


def _normalize_restore_path(path: str | Path) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()
