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

from dbabrain.common.restorestep import DIFF, FULL, LOG, RestoreStepError


#: The rows of ``RESTORE FILELISTONLY`` (SQL Server 2016+ adds ``SnapshotUrl``; the form both
#: FILELISTONLY readers insert into).
_FILELIST_TABLE = """DECLARE @filelist TABLE
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
);"""


def _quote_name(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def _quote_literal(value: str) -> str:
    return "N'" + str(value).replace("'", "''") + "'"


def _server_moment(text: str) -> str:
    """``text`` in the target's clock (UTC unless it says otherwise), ISO with the ``T``.

    Never with an offset: STOPAT refuses one outright - ``Invalid value specified for STOPAT
    parameter`` (Msg 3217) for every point-in-time restore from the bot, whose moments carry
    ``+HH:MM`` (the point-in-time drill, 2026-09-25)."""
    from dbabrain.lib.restore.moment import MomentError, parse_moment

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

{_FILELIST_TABLE}

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

def _full_relocated(*, database: str, path: str, data_dir: str, log_dir: str, recovery: bool,
                    replace: bool, stats: int) -> str:
    """A full restored onto ANOTHER server: every file the backup names moved to the target's folders.

    0.39.0 (1.354). A restore from the repository puts a Windows server's backup onto a Linux SQL
    Server, and a RESTORE that keeps the source's paths (``D:\\SQLDATA\\APP.mdf``) fails there. Where
    the files go is read on the target, not stated by a caller who cannot know it: every row of
    ``RESTORE FILELISTONLY`` (not only the first data and log file, as ``move_files`` takes - a
    database with an ``.ndf`` was refused by that form) goes to ``data_dir`` (a log file to
    ``log_dir``), each as ``<database>_<its own file name>``; an empty folder means the instance's own
    default (``SERVERPROPERTY('InstanceDefaultDataPath')`` / ``'InstanceDefaultLogPath'``, SQL Server
    2012+).
    """
    literal = str(database).replace("'", "''")
    name = str(database).replace("]", "]]")
    nested_name = name.replace("'", "''")
    bak = str(path).replace("'", "''''")

    def folder(value: str, prop: str) -> str:
        text = str(value or "").strip()
        if text:
            return "N'" + text.replace("'", "''") + "'"
        return f"CAST(SERVERPROPERTY('{prop}') AS nvarchar(512))"

    tail = ["REPLACE"] if replace else []
    tail += ["RECOVERY" if recovery else "NORECOVERY", "CHECKSUM", f"STATS = {int(stats)}"]
    options = ", ".join(tail)
    base = "REPLACE(PhysicalName, N'/', N'\\')"
    text = f"""
USE master;

IF DB_ID(N'{literal}') IS NOT NULL
    AND DATABASEPROPERTYEX(N'{literal}', N'Status') != N'RESTORING'
BEGIN
    ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
END;

{_FILELIST_TABLE}

INSERT INTO @filelist
EXEC('RESTORE FILELISTONLY FROM DISK = N''{bak}''');

DECLARE @dataDir nvarchar(512) = {folder(data_dir, 'InstanceDefaultDataPath')};
DECLARE @logDir nvarchar(512) = {folder(log_dir, 'InstanceDefaultLogPath')};
IF @dataDir IS NULL OR @logDir IS NULL
BEGIN
    THROW 51001, 'The target names no default data or log folder: state relocate data_dir and log_dir.', 1;
END;
IF RIGHT(@dataDir, 1) NOT IN (N'/', N'\\')
    SET @dataDir = @dataDir + CASE WHEN CHARINDEX(N'\\', @dataDir) > 0 THEN N'\\' ELSE N'/' END;
IF RIGHT(@logDir, 1) NOT IN (N'/', N'\\')
    SET @logDir = @logDir + CASE WHEN CHARINDEX(N'\\', @logDir) > 0 THEN N'\\' ELSE N'/' END;

DECLARE @moves nvarchar(max) = N'';
SELECT @moves = @moves + N', MOVE N''' + REPLACE(LogicalName, N'''', N'''''') + N''' TO N'''
    + REPLACE(CASE WHEN [Type] = 'L' THEN @logDir ELSE @dataDir END + N'{literal}_'
              + CASE WHEN CHARINDEX(N'\\', REVERSE({base})) > 0
                     THEN RIGHT({base}, CHARINDEX(N'\\', REVERSE({base})) - 1)
                     ELSE PhysicalName END, N'''', N'''''') + N''''
FROM @filelist
ORDER BY FileId;

IF @moves = N''
BEGIN
    THROW 51000, 'The backup file names no database file (RESTORE FILELISTONLY returned nothing).', 1;
END;

DECLARE @restoreSql nvarchar(max) = N'RESTORE DATABASE [{nested_name}] FROM DISK = N''{bak}'' WITH '
    + STUFF(@moves, 1, 2, N'') + N', {options};';

EXEC sys.sp_executesql @restoreSql;
"""
    if recovery:
        text += f"\nALTER DATABASE [{name}] SET MULTI_USER;\n"
    return text.strip()



def existing_guard(database: str) -> str:
    """The batch's first lines on a full restore that may not replace: stop if the name is taken.

    ``REPLACE`` overwrites whatever holds that name, and the full template first sets it
    ``SINGLE_USER WITH ROLLBACK IMMEDIATE`` - an ONLINE database on the wrong target lost its users'
    work and then its data, and nothing asked (owner decision G2.10, 2026-10-01). That guard stopped
    for an ONLINE database only: one OFFLINE, a log-shipping standby left RESTORING, or the half of
    an unfinished restore was replaced without a word. The operator, 2026-11-13 (translated):
    *overwriting a DB must be set explicitly, off by default* - so without ``overwrite_existing:
    true`` a restore lands on a name the target does not hold, **in any state**, or it stops here
    and names the state it found. A repeated drill, whose point is to replace its own last restore,
    states ``overwrite_existing: true``. ``RAISERROR`` + ``RETURN`` rather than ``THROW``: the guard
    runs on 2008 R2 too.
    """
    literal = str(database).replace("'", "''")
    # The one %s is RAISERROR's own - the state found; any other % in the name is doubled for it.
    name = str(database).replace("%", "%%")
    message = (f"[{name}] exists on this server (state %s); a full restore would replace it. "
               "Nothing was restored. State overwrite_existing: true on the restore entry when "
               "replacing it is the intent, or restore under another name (database_mappings).")
    message = message.replace("'", "''")
    return (f"IF DB_ID(N'{literal}') IS NOT NULL\n"
            f"BEGIN\n"
            f"    DECLARE @dbabrainFoundState nvarchar(60) = "
            f"CAST(DATABASEPROPERTYEX(N'{literal}', N'Status') AS nvarchar(60));\n"
            f"    RAISERROR(N'{message}', 16, 1, @dbabrainFoundState);\n    RETURN;\nEND;\n")


def statement(level: str, *, database: str, path: str, recovery: bool = False, stopat: str = "",
              replace: bool = True, move: dict[str, str] | None = None,
              move_files: dict[str, str] | None = None, relocate: dict[str, str] | None = None,
              stats: int = 10) -> str:
    """The one RESTORE statement for one file. Pure - nothing is executed here.

    ``stopat`` is already in the server's clock (see :func:`_server_moment`); ``replace``, ``move``
    and ``move_files`` belong to a full and are ignored on the other levels by the caller's rules.
    """
    if level == FULL and relocate is not None:
        return _full_relocated(
            database=database, path=path, data_dir=str(relocate.get("data_dir") or ""),
            log_dir=str(relocate.get("log_dir") or ""), recovery=recovery, replace=replace, stats=stats)
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
    relocate = _relocate(request.get("relocate"))
    if (move or move_files or relocate is not None) and level != FULL:
        raise RestoreStepError("move, move_files and relocate apply to the full restore only.")
    if sum(bool(x) for x in (move, move_files, relocate is not None)) > 1:
        raise RestoreStepError("give one of move (logical name -> path), move_files (data and log paths, "
                               "names read on the server) or relocate (every file to the target's folders) - one of them, not both.")
    if move_files and not (str(move_files.get("data") or "").strip()
                           and str(move_files.get("log") or "").strip()):
        raise RestoreStepError('move_files needs both "data" and "log": the two paths on the target.')

    # Whether this restore may replace a database the target holds - the entry's word, true or
    # nothing (0.39.0, 1.362). Without it: the guard below, and no REPLACE in the statement either,
    # so SQL Server itself refuses a name that came into being between the guard and the RESTORE.
    overwrite = request.get("overwrite_existing") is True
    replace = bool(request.get("replace", True)) and (overwrite or level != FULL)
    guarded = level == FULL and not overwrite
    statements: list[str] = []
    for index, path in enumerate(paths, start=1):
        last = index == len(paths)
        first = index == 1
        text = statement(
            level, database=database, path=path, recovery=last and with_recovery,
            stopat=stopat if last else "",
            replace=first and replace,
            move=move if first else None, move_files=move_files if first else None,
            relocate=relocate if first else None,
            stats=int(request.get("stats") or 10))
        if first and guarded:
            # In the same batch, before anything the statement does - the full template's first act
            # is to throw the database's users out.
            text = "USE master;\n" + existing_guard(database) + text
        statements.append(text)
    return statements


def _relocate(value: Any) -> dict[str, str] | None:
    """``relocate``: absent - not asked; an object - ``data_dir`` and ``log_dir``, either absent or
    empty for the target's own default (``{}``: both). An object, never ``true``: the request is held to
    its reference before it reaches here, and a boolean there was refused by it (the first restore from
    the repository, 2026-11-13)."""
    if value is None:
        return None
    if isinstance(value, dict) and set(value) <= {"data_dir", "log_dir"}:
        return {"data_dir": str(value.get("data_dir") or ""), "log_dir": str(value.get("log_dir") or "")}
    raise RestoreStepError('relocate is {} (the target\'s default folders) or {"data_dir": ..., "log_dir": ...}.')


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

    from dbabrain.common.db_connect import connect_engine

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
    from dbabrain.common.sqlcmd_run import run_sqlcmd

    block = {key: value for key, value in dict(request["sqlcmd"]).items() if key != "sql"}
    ran = run_sqlcmd({**block, "sql": statements[0]})
    return {"db_type": "sqlserver", "level": level, "applied": [], "ran": paths,
            "statements": statements, **ran,
            "recovered": bool(request.get("with_recovery", False)),
            "stopat": str(request.get("stopat") or "") or None}
