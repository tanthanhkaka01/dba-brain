"""What ``full``/``diff``/``log`` is called on each engine.

Pure engine knowledge, split out of ``common/backup/spec.py`` on 2026-08-15. ``backup_restore``
needs it while *deciding* which backup is due — before there is a request to hand over — so it
could not sit behind the CLI. Oracle counts levels (``0``/``1``) and has no separate log backup at
all; PostgreSQL calls a differential ``incr``; SQL Server uses the words. Getting this wrong is
silent: the script runs, and takes the wrong kind of backup.
"""

from __future__ import annotations


#: What each engine calls the level, keyed by the word a caller uses. One vocabulary across four
#: engines so a Telegram command or a runbook can say "full" without knowing which engine answers.
#:
#: Oracle and PostgreSQL have no ``log`` here on purpose: their archive/WAL backups are a *separate
#: script* with its own schedule, so asking for one at this level is a mistake worth naming rather
#: than passing an unknown value into a shell script that will interpret it as something else.
class BackupSpecError(ValueError):
    """The spec cannot be honoured as written."""


BACKUP_LEVEL_BY_ENGINE: dict[str, dict[str, str]] = {
    "oracle": {"full": "0", "diff": "1"},
    "postgresql": {"full": "full", "diff": "incr"},
    "sqlserver": {"full": "full", "diff": "diff", "log": "log"},
    "mysql": {"full": "full", "diff": "incr"},
}


def backup_level_for(db_type: str, backup_type: str) -> str:
    """The engine's own name for ``backup_type``. Raises when that engine has no such level."""
    engine = str(db_type or "").strip().lower()
    wanted = str(backup_type or "").strip().lower()
    levels = BACKUP_LEVEL_BY_ENGINE.get(engine)
    if levels is None:
        raise BackupSpecError(f"No backup levels defined for engine '{db_type}'.")
    if wanted not in levels:
        raise BackupSpecError(
            f"{engine} has no '{wanted}' level (it has: {', '.join(sorted(levels))}). "
            f"For {engine}, log/archive backups are a separate job."
        )
    return levels[wanted]
