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

from typing import Any

from db_ops.common.backupfiles import DIFF, FULL, LOG, BackupListError, row

#: RESTORE HEADERONLY's BackupType codes, and the letters some tools report instead.
_KIND = {"1": FULL, "5": DIFF, "2": LOG, "D": FULL, "I": DIFF, "L": LOG}

_LIST = ("SELECT full_filesystem_path AS path, size_in_bytes AS size "
         "FROM sys.dm_os_enumerate_filesystem(N'{directory}', N'*') "
         "WHERE is_directory = 0")


def _rows(cursor) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, r)) for r in cursor.fetchall()]


def list_files(request: dict[str, Any]) -> list[dict[str, Any]]:
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
            except Exception:  # noqa: BLE001 - a stray file in a shared directory is not an error.
                continue
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
