"""A SQL Server restore: the SQL text of the steps around a restore: recovery, setting the recovery model, CHECKDB, and the composed RESTORE a dry run shows.

Split out of ``backup_restore/restore_database.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``restore_database``
re-exports every name, so no import changes.
"""

from __future__ import annotations
from db_ops.backup_restore.shell_quoting import _BACKUP_TIMESTAMP_RE, backup_time_from_name, _build_sqlcmd_auth_args, _escape_identifier, _escape_sql_string, _ps_array, _ps_quote  # noqa: F401 - one definition
from db_ops.backup_restore.restore_base import RestoreCandidate


def composed_restore_sql(level: str, fields: dict[str, object]) -> str:
    """The statement ``common`` will run for a step, without running it - for a dry run's plan."""
    from typing import cast

    from db_ops.lib.cli_types import RestoreStepAnswer
    from db_ops.transport import common_cli

    # Typed by hand: the command name is built at run time, so the transport stub cannot narrow it.
    # A restore step's answer read under a key it no longer carries is the 0.22.0 defect that left
    # every script-driven restore RESTORING; under this type, mypy names such a key.
    answer = cast(RestoreStepAnswer, common_cli.run(f"restore-{level}", {**fields, "dry_run": True}))
    return "\n".join(str(text) for text in answer.get("statements") or [])


def build_recovery_sql(candidate: RestoreCandidate) -> str:
    return f"""
USE master;
RESTORE DATABASE [{_escape_identifier(candidate.restore_database_name)}] WITH RECOVERY;
ALTER DATABASE [{_escape_identifier(candidate.restore_database_name)}] SET MULTI_USER;
""".strip()


def build_recovery_if_restoring_sql(candidate: RestoreCandidate) -> str:
    # PITR finalize: only recover if the DB is still RESTORING. During a point-in-time restore the
    # final LOG restore normally recovers the DB (WITH RECOVERY + STOPAT); but if that log was
    # skipped (e.g. Msg 4305 from a non-contiguous/stray log) or the chain ended exactly at the
    # target, the DB is left RESTORING. Recovering here brings it online at the last applied point
    # -- which is never past the target, since every NORECOVERY log ends at or before it. If the DB
    # is already ONLINE the RESTORE is skipped and this is a no-op that just normalizes user access.
    db = _escape_identifier(candidate.restore_database_name)
    db_literal = _escape_sql_string(candidate.restore_database_name)
    return f"""
USE master;
IF DATABASEPROPERTYEX(N'{db_literal}', N'Status') = N'RESTORING'
    RESTORE DATABASE [{db}] WITH RECOVERY;
ALTER DATABASE [{db}] SET MULTI_USER;
""".strip()


def build_set_recovery_model_full_sql(candidate: RestoreCandidate) -> str:
    return f"ALTER DATABASE [{_escape_identifier(candidate.restore_database_name)}] SET RECOVERY FULL;"


def build_checkdb_sql(candidate: RestoreCandidate) -> str:
    return f"DBCC CHECKDB ([{_escape_identifier(candidate.restore_database_name)}]) WITH NO_INFOMSGS;"
