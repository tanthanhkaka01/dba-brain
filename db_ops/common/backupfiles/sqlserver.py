"""SQL Server: ask the instance, because the files carry their own headers.

A ``.bak``'s name says nothing reliable — it is whatever the job that wrote it chose, and a file
copied from another database keeps its old name. ``RESTORE HEADERONLY`` says what the file *is*,
which database it came from, when it finished and where it sits in the LSN sequence, so that is
what is asked.

The directory is listed through the instance too (``sys.dm_os_enumerate_filesystem``) rather than
locally: on a container target the backup path does not exist on the machine running this code at
all, and listing locally would work on the one topology where the two coincide.
"""

from __future__ import annotations

import re
from typing import Any

from db_ops.common.backupfiles import DIFF, FULL, LOG, BackupListError, row

#: RESTORE HEADERONLY's BackupType codes, and the letters some tools report instead.
_KIND = {"1": FULL, "5": DIFF, "2": LOG, "D": FULL, "I": DIFF, "L": LOG}

#: SQL Server's own numbers for "this file is not a backup": the media family is incorrectly formed
#: (3241), or not Microsoft Tape Format (3242, 3243). Measured on a certificate file, 2026-09-24.
_NOT_A_BACKUP = frozenset({"3241", "3242", "3243"})
_NUMBERED = re.compile(r"\((\d{3,5})\)")
#: What this tool's SQL Server jobs name their pieces; anything else in the folder is not ours.
_BACKUP_SUFFIXES = (".bak", ".trn")

_LIST = ("SELECT full_filesystem_path AS path, size_in_bytes AS size "
         "FROM sys.dm_os_enumerate_filesystem(N'{directory}', N'*') "
         "WHERE is_directory = 0")


def _rows(cursor) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, r)) for r in cursor.fetchall()]


def list_files(request: dict[str, Any], skipped: list[str] | None = None) -> list[dict[str, Any]]:
    target = request.get("target") or {}
    directory = str(request.get("path") or "").strip()
    if not directory:
        raise BackupListError("path is required: the directory the instance should look in.")
    if not str(target.get("host") or "").strip():
        raise BackupListError("target.host is required for sqlserver.")

    from db_ops.common.db_connect import connect_engine

    connection = connect_engine(
        db_type="sqlserver", host=str(target["host"]), port=int(target.get("port") or 1433),
        database="master", username=str(target.get("username") or ""),
        password=str(target.get("password") or ""), autocommit=True,
        statement_timeout_seconds=0,
    )
    try:
        cursor = connection.cursor()
        # A literal, not a `?` placeholder: SQL Server is reached through pyodbc when the local
        # ODBC stack can negotiate TLS with it and through pymssql when it cannot, and the
        # pymssql adapter takes the statement alone - `execute() takes 2 positional arguments
        # but 3 were given` on the first target that fell back. The value is ours, and escaped.
        cursor.execute(_LIST.format(directory=directory.replace(chr(39), chr(39) * 2)))
        found = _rows(cursor)

        rows: list[dict[str, Any]] = []
        for item in sorted(found, key=lambda i: str(i.get("path") or "")):
            path = str(item.get("path") or "")
            escaped = path.replace("'", "''")
            try:
                cursor.execute(f"RESTORE HEADERONLY FROM DISK = N'{escaped}'")
                heads = _rows(cursor)
            except Exception as exc:  # noqa: BLE001 - sorted below: stray file, or a real failure.
                # A file that is not a backup (the exported certificate beside the backups, a
                # README) is skipped: SQL Server says so with 3241-3243. Anything else is a backup
                # that could not be read, and skipping it too is how "Access is denied" (3201) and
                # a missing certificate (33111) both came out as "no databases found" on
                # 2026-09-24 - or, worse, how a restore quietly stops at an older piece.
                if _NOT_A_BACKUP.intersection(_NUMBERED.findall(str(exc))):
                    # Named like a backup and still not one: a truncated or damaged piece. Passed
                    # over - the chain is built from what can be read - but reported, because the
                    # newest piece silently missing is how a restore quietly goes back in time.
                    if skipped is not None and path.lower().endswith(_BACKUP_SUFFIXES):
                        skipped.append(path)
                    continue
                raise BackupListError(f"cannot read {path}: {exc}") from exc
            for head in heads:
                kind = _KIND.get(str(head.get("BackupType") or "").strip().upper())
                if not kind:
                    # An unrecognised type is dropped rather than guessed: a file-copy-only or
                    # partial backup offered as a full would be picked by a restore and be wrong.
                    continue
                finished = head.get("BackupFinishDate")
                rows.append(row(
                    path=path, kind=kind,
                    database=str(head.get("DatabaseName") or "") or None,
                    size=int(item.get("size") or 0) or None,
                    finished_at=finished.isoformat() if hasattr(finished, "isoformat") else None,
                    first_lsn=int(float(head.get("FirstLSN") or 0)),
                    last_lsn=int(float(head.get("LastLSN") or 0)),
                ))
        return rows
    finally:
        connection.close()
