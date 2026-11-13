"""Which of the two kinds a restore entry is: script-driven, or share-driven.

``backup_restore.restores[]`` holds both in one list, and every reader has to tell them apart the
same way: the two loaders (each takes its own kind and steps over the other), the registrar, and -
since 0.38.0 - the runbook that tells a person which command restores an entry by hand
(:mod:`dbabrain.lib.backup_runbook`). ``restore-latest`` refuses a script-driven entry and
``restore-by-id`` a share-driven one, so a second copy of this rule is an answer naming the command
that refuses. It lived in ``backup_restore.config`` until then; that module still exports the names.
"""

from __future__ import annotations

from typing import Any

#: Restore entries for these engines are driven by a script, not by the SQL Server share-driven
#: flow. They live in the same ``restores`` list so an operator sees one list of restores with one
#: shape (restore_id + time_window), but they carry none of the SQL Server fields (SMB share, .bak
#: paths, sqlcmd) that flow requires - so its parser steps over them rather than fail on them.
SCRIPT_RESTORE_DB_TYPES = {"oracle", "postgresql", "mysql"}


def is_script_restore(item: dict[str, Any]) -> bool:
    """Whether this entry is driven by a shell script rather than the SQL Server engine.

    Declaring a ``script`` is what makes an entry script-driven — the engine is not the
    deciding factor. SQL Server can be either: the production flow reads an SMB share and
    drives sqlcmd through ``restore_database.py``, while a container-to-container drill is the
    same shape as the Oracle/PostgreSQL ones and reuses their machinery (host-to-host
    transfer, staging into the target container, env_secrets). Keying off ``db_type`` alone
    would force every SQL Server entry down one path or the other.
    """
    entry = item or {}
    if str(entry.get("script") or "").strip():
        return True
    return str(entry.get("db_type") or "").strip().lower() in SCRIPT_RESTORE_DB_TYPES
