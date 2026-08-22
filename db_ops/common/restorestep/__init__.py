"""Restoring one named backup file: full, then diff, then log — the caller drives the chain.

`restore-database` decides a whole chain for you. These three are the level below it: the caller
already knows which file it wants applied, having chosen it with `list-backup-files`, and asks for
exactly that. That is the API a recovery actually needs — an operator recovering to a moment wants
to see each step land before deciding the next, not hand over a directory and hope.

**The three engines do not mean the same thing by "restore one file", and the difference is not
smoothed over here.** Pretending they match would produce a command that looks portable and is
wrong on two of them:

* **SQL Server** genuinely restores one file at a time. `RESTORE DATABASE ... FROM DISK` with
  `NORECOVERY` between steps and `RECOVERY` on the last is the native shape, and `STOPAT` on a log
  is how a point in time is reached.
* **Oracle** does not take a path. RMAN restores from its catalogue, so a piece is **cataloged**
  first (`CATALOG BACKUPPIECE`) and then RMAN is asked to `RESTORE`/`RECOVER` — it decides which
  pieces to read. Handing it a path is how you *offer* a backup, not how you apply one.
* **PostgreSQL** has no per-file restore at all: a base backup is a directory that becomes the data
  directory, incrementals are combined into it (`pg_combinebackup`), and logs are replayed by the
  server at startup. The "file" is a directory, and "log" is a configuration, not a step.

Each response says which of those happened, so a caller can never mistake one for another.
"""

from __future__ import annotations

from typing import Any
# Re-exported: the vocabulary itself lives in db_ops/lib/backup_kinds.py, once.
from db_ops.lib.backup_kinds import DIFF, FULL, LOG  # noqa: F401

LEVELS = (FULL, DIFF, LOG)


class RestoreStepError(ValueError):
    """The step cannot be run as asked."""


def restore_step(level: str, request: dict[str, Any]) -> dict[str, Any]:
    """Apply one backup at ``level``. Dispatches on ``db_type``."""
    if level not in LEVELS:
        raise RestoreStepError(f"level must be one of {', '.join(LEVELS)}; got {level!r}.")
    if not isinstance(request, dict):
        raise RestoreStepError("request must be a JSON object.")

    db_type = str(request.get("db_type") or "").strip().lower()
    if db_type == "sqlserver":
        from db_ops.common.restorestep import sqlserver as engine
    elif db_type == "oracle":
        from db_ops.common.restorestep import oracle as engine
    elif db_type in {"postgresql", "postgres"}:
        from db_ops.common.restorestep import postgresql as engine
    else:
        raise RestoreStepError(
            f"db_type must be sqlserver, oracle or postgresql; got {db_type!r}.")

    # PostgreSQL's log step applies no file at all - it writes recovery configuration pointing at
    # a WAL directory, and the server replays from there at startup. Demanding a backup_path for
    # it asks the caller to name something that does not exist.
    if db_type in {"postgresql", "postgres"} and level == LOG:
        wal = str(request.get("wal_dir") or "").strip()
        paths = [wal] if wal else backup_paths(request)
    else:
        paths = backup_paths(request)
    return engine.apply(level, request, paths)


def backup_paths(request: dict[str, Any]) -> list[str]:
    """The file(s) this step applies, from ``backup_path`` or ``backup_paths``.

    Both spellings because a log step usually applies several in order while a full applies one,
    and forcing a one-item array on the common case reads like a mistake. Naming both is refused:
    a request that says two things is a request nobody has checked.
    """
    single = str(request.get("backup_path") or "").strip()
    many = request.get("backup_paths") or []
    if isinstance(many, str):
        raise RestoreStepError('backup_paths must be an array; use backup_path for one file.')
    if single and many:
        raise RestoreStepError("give either backup_path or backup_paths, not both.")
    paths = [single] if single else [str(p).strip() for p in many if str(p).strip()]
    if not paths:
        raise RestoreStepError("backup_path (or backup_paths) is required.")
    return paths
