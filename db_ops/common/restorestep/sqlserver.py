"""SQL Server: one `RESTORE` per file, and the recovery flag is the caller's decision.

**The one place a SQL Server RESTORE is written** (rules R43, the operator's choice, 0.24.0). The
nightly SMB restore (`backup_restore restore-latest`) composed its own statements and ran them
through `run-sqlcmd`, while the drills (`restore-by-id`) came here: two texts for one job. The
nightly's shapes live here now, and it asks for its steps like every other caller.

The two rules that make a chain work, and that this enforces rather than trusts:

* **`NORECOVERY` between steps, `RECOVERY` only on the last.** A database recovered early cannot
  have the remaining logs applied at all — the only fix is to start the whole restore again. So
  ``with_recovery`` defaults to **false**: a caller stepping through a chain gets the safe answer
  without saying so, and the one step that finishes it says it does.
* **`STOPAT` only on a log.** SQL Server accepts it on a full or a differential and *silently
  ignores it there*, which reads as a point-in-time restore that never happened. Asked for on any
  other level, it is refused. It is written ``YYYY-MM-DDTHH:MM:SS``: the ISO form is the one SQL
  Server reads the same under every login language - ``YYYY-MM-DD HH:MM:SS`` is read year-day-month
  under a British or French one.

Where the data and log files go on the target is said one of two ways. ``move`` maps each logical
name to a path, for a caller that knows them. ``move_files`` names only the two paths - ``data``
and ``log`` - and the logical names are read on the server from the backup's own file list
(``RESTORE FILELISTONLY``), which is how the nightly restore has always done it: nobody reliably
knows a source database's logical names in advance. That form also takes the database to
``SINGLE_USER`` first, so connected sessions cannot hold the restore off.

**Two ways to run it.** Over a driver connection from this machine (``target``), or through
``sqlcmd`` where the SQL Server is (``sqlcmd`` - the same block ``run-sqlcmd`` takes, less the SQL),
which is how a restore reaches files only the server can see. Through ``sqlcmd`` a call applies one
file and answers like ``run-sqlcmd`` does - exit code, stdout, stderr, timed out - because the
caller decides what an answer means: a Msg 4305 log to skip, a connection lost mid-``RESTORE LOG``
to inspect rather than retry.
"""

from __future__ import annotations

from typing import Any

from db_ops.common.restorestep import DIFF, FULL, LOG, RestoreStepError


def _quote_name(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def _quote_literal(value: str) -> str:
    return "N'" + str(value).replace("'", "''") + "'"


def _server_moment(text: str) -> str:
    """``text`` in the target's clock (UTC unless it says otherwise), ISO with the ``T``.

    Never with an offset: STOPAT refuses one outright - ``Invalid value specified for STOPAT
    parameter`` (Msg 3217) for every point-in-time restore from the bot, whose moments carry
    ``+HH:MM`` (the point-in-time drill, 2026-09-25)."""
    from db_ops.lib.restore.moment import MomentError, parse_moment

    try:
        return parse_moment(text).strftime("%Y-%m-%dT%H:%M:%S")
    except MomentError as exc:
        raise RestoreStepError(str(exc)) from exc


def _full_with_the_files_the_server_names(*, database: str, path: str, data_path: str,
                                          log_path: str, recovery: bool, replace: bool,
                                          stats: int) -> str:
    """The nightly's full restore: logical names from ``RESTORE FILELISTONLY``, moved to two paths.

    Written as it has run every night, with one correction: a value inside the ``EXEC`` string and
    inside ``@restoreSql`` sits two literals deep, so its quote is doubled twice. Doubled once, as
    it was, a ``'`` in a path or a database name ended the outer literal - no such name exists on
    this estate, and for every other the text is the same byte for byte.
    """
    literal = str(database).replace("'", "''")
    name = str(database).replace("]", "]]")
    nested_name = name.replace("'", "''")
    bak = str(path).replace("'", "''''")
    data = str(data_path).replace("'", "''''")
    log = str(log_path).replace("'", "''''")
    options = [f"    MOVE N''' + REPLACE(@dataLogicalName, '''', '''''') + N''' TO N''{data}'',",
               f"    MOVE N''' + REPLACE(@logLogicalName, '''', '''''') + N''' TO N''{log}'',"]
    if replace:
        options.append("    REPLACE,")
    options += ["    RECOVERY," if recovery else "    NORECOVERY,", "    CHECKSUM,",
                f"    STATS = {int(stats)};';"]
    text = f"""
USE master;

IF DB_ID(N'{literal}') IS NOT NULL
    AND DATABASEPROPERTYEX(N'{literal}', N'Status') != N'RESTORING'
BEGIN
    ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
END;

DECLARE @filelist TABLE
(
    LogicalName nvarchar(128),
    PhysicalName nvarchar(260),
    [Type] char(1),
    FileGroupName nvarchar(128) NULL,
    Size numeric(20, 0),
    MaxSize numeric(20, 0),
    FileId bigint,
    CreateLSN numeric(25, 0) NULL,
    DropLSN numeric(25, 0) NULL,
    UniqueId uniqueidentifier,
    ReadOnlyLSN numeric(25, 0) NULL,
    ReadWriteLSN numeric(25, 0) NULL,
    BackupSizeInBytes bigint,
    SourceBlockSize int,
    FileGroupId int,
    LogGroupGUID uniqueidentifier NULL,
    DifferentialBaseLSN numeric(25, 0) NULL,
    DifferentialBaseGUID uniqueidentifier,
    IsReadOnly bit,
    IsPresent bit,
    TDEThumbprint varbinary(32) NULL,
    SnapshotUrl nvarchar(360) NULL
);

INSERT INTO @filelist
EXEC('RESTORE FILELISTONLY FROM DISK = N''{bak}''');

DECLARE @dataLogicalName nvarchar(128) = (SELECT TOP (1) LogicalName FROM @filelist WHERE [Type] = 'D' ORDER BY FileId);
DECLARE @logLogicalName nvarchar(128) = (SELECT TOP (1) LogicalName FROM @filelist WHERE [Type] = 'L' ORDER BY FileId);

IF @dataLogicalName IS NULL OR @logLogicalName IS NULL
BEGIN
    THROW 51000, 'Backup file does not contain one data file and one log file.', 1;
END;

DECLARE @restoreSql nvarchar(max) = N'
RESTORE DATABASE [{nested_name}]
FROM DISK = N''{bak}''
WITH
""" + "\n".join(options) + """

EXEC sys.sp_executesql @restoreSql;
"""
    if recovery:
        text += f"\nALTER DATABASE [{name}] SET MULTI_USER;\n"
    return text.strip()


def statement(level: str, *, database: str, path: str, recovery: bool = False, stopat: str = "",
              replace: bool = True, move: dict[str, str] | None = None,
              move_files: dict[str, str] | None = None, stats: int = 10) -> str:
    """The one RESTORE statement for one file. Pure - nothing is executed here.

    ``stopat`` is already in the server's clock (see :func:`_server_moment`); ``replace``, ``move``
    and ``move_files`` belong to a full and are ignored on the other levels by the caller's rules.
    """
    if level == FULL and move_files:
        return _full_with_the_files_the_server_names(
            database=database, path=path, data_path=str(move_files["data"]),
            log_path=str(move_files["log"]), recovery=recovery, replace=replace, stats=stats)
    options = ["RECOVERY" if recovery else "NORECOVERY", "CHECKSUM", f"STATS = {int(stats)}"]
    # REPLACE only on a full: it means "overwrite the existing database", a statement about
    # starting a chain rather than continuing one.
    if level == FULL and replace:
        options.append("REPLACE")
    if stopat:
        options.append(f"STOPAT = {_quote_literal(stopat)}")
    moves = "".join(f", MOVE {_quote_literal(logical)} TO {_quote_literal(target)}"
                    for logical, target in sorted((move or {}).items())) if level == FULL else ""
    verb = "RESTORE LOG" if level == LOG else "RESTORE DATABASE"
    return (f"USE master;\n{verb} {_quote_name(database)}\nFROM DISK = {_quote_literal(path)}\n"
            f"WITH {', '.join(options)}{moves};")


def build_statements(level: str, request: dict[str, Any], paths: list[str]) -> list[str]:
    """The RESTORE statements this step will send, one per file, in order. Pure."""
    database = str(request.get("database_name") or request.get("database") or "").strip()
    if not database:
        raise RestoreStepError("database is required for sqlserver.")

    with_recovery = bool(request.get("with_recovery", False))
    stopat = str(request.get("stopat") or "").strip()
    if stopat and level != LOG:
        raise RestoreStepError(
            f"stopat applies to a log restore only; SQL Server accepts it on a {level} and "
            "silently ignores it, which reads as a point-in-time restore that never happened."
        )
    if stopat:
        stopat = _server_moment(stopat)

    move = request.get("move") or {}
    move_files = request.get("move_files") or {}
    if (move or move_files) and level != FULL:
        raise RestoreStepError("move and move_files apply to the full restore only.")
    if move and move_files:
        raise RestoreStepError("give either move (logical name -> path) or move_files "
                               "(data and log paths, names read on the server), not both.")
    if move_files and not (str(move_files.get("data") or "").strip()
                           and str(move_files.get("log") or "").strip()):
        raise RestoreStepError('move_files needs both "data" and "log": the two paths on the target.')

    statements: list[str] = []
    for index, path in enumerate(paths, start=1):
        last = index == len(paths)
        first = index == 1
        statements.append(statement(
            level, database=database, path=path, recovery=last and with_recovery,
            stopat=stopat if last else "",
            replace=first and bool(request.get("replace", True)),
            move=move if first else None, move_files=move_files if first else None,
            stats=int(request.get("stats") or 10)))
    return statements


def apply(level: str, request: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    statements = build_statements(level, request, paths)
    if request.get("dry_run"):
        return {"db_type": "sqlserver", "level": level, "applied": [], "statements": statements,
                "dry_run": True}
    if request.get("sqlcmd"):
        return _apply_through_sqlcmd(level, request, paths, statements)

    target = request.get("target") or {}
    if not str(target.get("host") or "").strip():
        raise RestoreStepError('target.host is required for sqlserver (or "sqlcmd", to run it '
                               "where the SQL Server is).")

    from db_ops.common.db_connect import connect_engine

    connection = connect_engine(
        db_type="sqlserver", host=str(target["host"]), port=int(target.get("port") or 1433),
        database="master", username=str(target.get("username") or ""),
        password=str(target.get("password") or ""), autocommit=True,
        # No statement timeout: a restore runs for minutes, and the connection layer reads None as
        # "reuse the connect timeout" - which cut the first real restore off at 30s, mid-chain.
        statement_timeout_seconds=0,
    )
    try:
        cursor = connection.cursor()
        for text in statements:
            cursor.execute(text)
            # RESTORE reports progress as info messages and can leave result sets behind; draining
            # them keeps the next statement from reading the previous one's.
            while cursor.nextset():
                pass
    finally:
        connection.close()

    return {"db_type": "sqlserver", "level": level, "applied": paths,
            "statements": statements,
            "recovered": bool(request.get("with_recovery", False)),
            "stopat": str(request.get("stopat") or "") or None}


def _apply_through_sqlcmd(level: str, request: dict[str, Any], paths: list[str],
                          statements: list[str]) -> dict[str, Any]:
    """One file, run by ``sqlcmd`` where the SQL Server is, answered as ``run-sqlcmd`` answers.

    One per call because the caller decides after each file whether to go on - a log too recent to
    apply (Msg 4305) is skipped, a connection lost after the command went out is inspected - and
    ``sqlcmd`` running several would decide that for it.
    """
    if len(statements) != 1:
        raise RestoreStepError(
            "through sqlcmd a step applies one file per call: the caller decides after each "
            "whether to go on (a Msg 4305 log is skipped, a lost connection is inspected).")
    from db_ops.common.sqlcmd_run import run_sqlcmd

    block = {key: value for key, value in dict(request["sqlcmd"]).items() if key != "sql"}
    ran = run_sqlcmd({**block, "sql": statements[0]})
    return {"db_type": "sqlserver", "level": level, "applied": [], "ran": paths,
            "statements": statements, **ran,
            "recovered": bool(request.get("with_recovery", False)),
            "stopat": str(request.get("stopat") or "") or None}
