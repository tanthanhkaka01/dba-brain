"""Performing a SQL Server restore from a spec: discover, choose, build, execute.

I/O lives here, and only I/O. What to restore is :mod:`.chain`'s answer and what to send is
:mod:`.sql`'s; this module connects, asks the instance what is on disk, and runs the statements in
order. The split matters because the two halves fail differently: a bug here raises at the
instance and is obvious, a bug there restores the wrong data and is not.

**It reads no config, and it still needs none to work.** Everything comes from the spec - host,
port, login, the directory to look in, where the data files go. Even the *file listing* is asked
of the instance rather than of a filesystem this process can see, through
``sys.dm_os_enumerate_filesystem``: the backups sit where the SQL Server can read them, which on a
container target is a path that does not exist on the machine running this code at all. Listing
them locally would work on the one topology where the two happen to coincide and fail everywhere
else - and that is the topology this API exists to stop being special.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from db_ops.common.restore.sqlserver.chain import RestoreChainError, parse_headers, select_chain
from db_ops.common.restore.sqlserver.sql import build_restore_statements

#: SQL 2017+ and every Linux build. Asked of the instance so the path is resolved where the
#: backups actually live, not where this process happens to be running.
_LIST_FILES = """
SELECT full_filesystem_path AS path
FROM sys.dm_os_enumerate_filesystem(N'{directory}', N'*')
WHERE is_directory = 0
"""

_HEADERONLY = "RESTORE HEADERONLY FROM DISK = N'{path}'"
_FILELISTONLY = "RESTORE FILELISTONLY FROM DISK = N'{path}'"


def _rows(cursor) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _escape(path: str) -> str:
    return str(path).replace("'", "''")


def connect_target(spec) -> Any:
    """Connect to the target instance's ``master``.

    ``autocommit=True`` because ``RESTORE`` is refused inside a user transaction, and the default
    connection wraps every batch in one. A restore is a sequence of statements that each either
    happened or did not; there is no transaction to want.

    ``statement_timeout_seconds=0`` means **no statement timeout**, and it has to be stated: the
    connection layer reads ``None`` as "reuse the connect timeout", so leaving it unset caps every
    statement at 30 seconds. A restore of any real database runs for minutes, so it died at 30s
    with ``HYT00 Query timeout expired`` - and it died *mid-restore*, leaving the database
    RESTORING, which reads like a failure of the restore rather than of a default.
    """
    from db_ops.common.db_connect import connect_engine

    return connect_engine(
        db_type="sqlserver",
        host=spec.target.host,
        port=spec.target.port or 1433,
        database="master",
        username=spec.target.username,
        password=spec.target.password,
        autocommit=True,
        statement_timeout_seconds=0,
    )


def discover_headers(cursor, directory: str, *, patterns=(".bak", ".trn")) -> list[Any]:
    """Every backup header under ``directory``, as the instance sees it.

    A file that cannot be read as a backup is skipped rather than fatal: a shared import directory
    collects stray files - a half-copied piece, someone's notes - and one of them must not stop a
    recovery that has everything it needs.
    """
    # A literal, not a `?` placeholder: SQL Server is reached through pyodbc when the
    # local ODBC stack can negotiate TLS with it and through pymssql when it cannot, and
    # the pymssql adapter takes the statement alone - `execute() takes 2 positional
    # arguments but 3 were given` on the first target that fell back. The value is ours,
    # and it is escaped.
    cursor.execute(_LIST_FILES.format(directory=_escape(directory)))
    paths = [str(row["path"]) for row in _rows(cursor)]
    wanted = [p for p in paths if not patterns or p.lower().endswith(tuple(patterns))]

    rows: list[dict[str, Any]] = []
    for path in sorted(wanted):
        try:
            cursor.execute(_HEADERONLY.format(path=_escape(path)))
            for row in _rows(cursor):
                rows.append({**row, "path": path})
        except Exception:  # noqa: BLE001 - an unreadable file is not the recovery's problem.
            continue
    return parse_headers(rows)


def move_map(cursor, backup_path: str, *, data_dir: str, log_dir: str = "") -> dict[str, str]:
    """Where each logical file goes on the target, from the full backup's own file list.

    Derived rather than configured: the logical names belong to the source database and nobody
    reliably knows them in advance, while getting one wrong puts a data file in a directory that
    may not exist on the target - which is every cross-machine restore.
    """
    cursor.execute(_FILELISTONLY.format(path=_escape(backup_path)))
    moves: dict[str, str] = {}
    separator = "\\" if ":" in str(data_dir)[:3] else "/"
    for row in _rows(cursor):
        logical = str(row.get("LogicalName") or "")
        if not logical:
            continue
        physical = str(row.get("PhysicalName") or "")
        suffix = physical.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] or logical
        root = (log_dir or data_dir) if str(row.get("Type") or "").upper() == "L" else data_dir
        # Hoisted out of the f-string: a backslash inside an f-string expression is Python 3.12+
        # syntax, and this package declares `requires-python = ">=3.11"`. It parsed here because
        # development runs on a newer interpreter, so the tree did not run at all on the floor it
        # promised — found by the first lint pass, which is exactly what `G-08` is for.
        base = str(root).rstrip("/\\")
        moves[logical] = f"{base}{separator}{suffix}"
    return moves


def _point_in_time(spec) -> datetime | None:
    """The moment as a naive datetime in the target server's own clock.

    ``STOPAT`` is read in server-local time and an offset is rejected outright, so the conversion
    happens once, here, where the offset is still visible - not at the statement builder, where a
    rejected statement would look like a formatting bug rather than the timezone decision it is.
    """
    if not spec.point_in_time:
        return None
    from db_ops.common.restore.sqlserver.timeparse import parse_moment

    return parse_moment(spec.point_in_time)


def execute_restore_spec(spec, plan: dict[str, Any]) -> dict[str, Any]:
    """Restore every database the spec names. Returns the JSON result the CLI prints."""
    moment = _point_in_time(spec)
    directory = spec.target.import_dir or spec.source.path
    results: list[dict[str, Any]] = []

    connection = connect_target(spec)
    try:
        cursor = connection.cursor()
        headers = discover_headers(cursor, directory)
        if not headers:
            raise RestoreChainError(
                f"No readable backups under {directory} as the target instance sees it. The path "
                "is resolved on the SQL Server, not on this machine - check it exists there."
            )

        databases = list(spec.databases) or sorted({h.database_name for h in headers})
        for database in databases:
            chain = select_chain(headers, database=database, point_in_time=moment)
            moves = move_map(cursor, chain.full.path,
                             data_dir=spec.target.data_dir, log_dir=spec.target.log_dir)
            statements = build_restore_statements(chain, database=database, move=moves)
            for statement in statements:
                cursor.execute(statement)
                # RESTORE emits its progress as info messages and can leave result sets behind;
                # draining them here keeps the next statement from reading the previous one's.
                while cursor.nextset():
                    pass
            results.append({
                "database": database,
                "restored": [h.path for h in chain.ordered],
                "stopat": chain.stopat.isoformat() if chain.stopat else None,
            })
    finally:
        connection.close()

    return {"method": plan.get("method"), "restore_mode": plan.get("restore_mode"),
            "target": plan.get("target"), "databases": results}
