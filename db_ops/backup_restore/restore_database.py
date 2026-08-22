from __future__ import annotations
from db_ops.backup_restore.shell_quoting import _BACKUP_TIMESTAMP_RE, _build_sqlcmd_auth_args, _escape_identifier, _escape_sql_string, _ps_array, _ps_quote  # noqa: F401 - one definition

import datetime
import shlex
import subprocess
import threading
import time
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from db_ops.backup_restore.config import (
    BackupRestoreConfig,
    DatabaseRestoreMapping,
    load_restore_config,
    validate_restore_target_is_not_source,
)
from db_ops.backup_restore.copy_backup import build_cmdkey_command, open_ssh_connection, resolve_password_ref
from db_ops.lib import powershell
from db_ops.lib.shell import is_powershell_executable, powershell_executable
from db_ops.backup_restore.events import emit_backup_restore_event
from db_ops.backup_restore.history import BackupRestoreHistory
from db_ops.backup_restore.sanitize import compact_log_value, sanitize_text, sanitize_value
from db_ops.config import DbOpsConfig, load_config
from db_ops.db.store import utc_now_text
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


RESTORE_FAILURE_MARKERS = (
    "msg 3013",
    "terminating abnormally",
    "incorrectly formed",
    "can not be read",
    "cannot be read",
    "the media family",
)

PRESTART_SQL_CONNECTION_MARKERS = (
    "login timeout expired",
    "tcp provider: error code 0x102",
    "network-related or instance-specific error",
)

AMBIGUOUS_SQL_CONNECTION_MARKERS = (
    "connection reset",
    "broken pipe",
)


class RestoreCommandTimeoutError(RuntimeError):
    def __init__(self, message: str, *, command_started: bool) -> None:
        super().__init__(message)
        self.command_started = command_started


def _emit_restore_log(logger: object | None, message: str, *, level: str = "logging") -> None:
    if logger:
        log_event(logger, level=level, message=sanitize_text(message))


def parse_point_in_time(value: str) -> datetime.datetime:
    """Parse a timezone-aware datetime string into a UTC datetime.

    Accepts: 'YYYY-MM-DD HH:MM:SS +HH:MM' or 'YYYY-MM-DDTHH:MM:SS+HH:MM'.
    The timezone offset is required and is used to convert the result to UTC.
    The returned STOPAT value is in UTC; SQL Server STOPAT comparison depends
    on whether the server is configured in UTC or local time.
    """
    m = re.match(r'^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})\s*([+-]\d{2}:\d{2})?$', value.strip())
    if not m:
        raise ValueError(f"Cannot parse point-in-time: {value!r}. Use format: 'YYYY-MM-DD HH:MM:SS +HH:MM'")
    date_part, time_part, tz_part = m.group(1), m.group(2), m.group(3)
    if not tz_part:
        raise ValueError(f"point-in-time must include a timezone offset, e.g. +07:00. Got: {value!r}")
    tz_sign = 1 if tz_part[0] == '+' else -1
    tz_hours, tz_mins = int(tz_part[1:3]), int(tz_part[4:6])
    tz_offset = datetime.timezone(datetime.timedelta(hours=tz_sign * tz_hours, minutes=tz_sign * tz_mins))
    dt = datetime.datetime(
        int(date_part[:4]), int(date_part[5:7]), int(date_part[8:10]),
        int(time_part[:2]), int(time_part[3:5]), int(time_part[6:8]),
        tzinfo=tz_offset,
    )
    return dt.astimezone(datetime.timezone.utc)


def _format_metadata(**metadata: object) -> str:
    return " ".join(f"{key}={compact_log_value(value)}" for key, value in metadata.items() if value is not None)


def _restore_database_label(candidate: RestoreCandidate | None, database: DatabaseRestoreMapping | None = None) -> str:
    if candidate is not None:
        return candidate.source_database_name
    if database is not None:
        return database.source_database
    return "unknown"


def get_latest_full_backup(config: BackupRestoreConfig | None = None) -> Path:
    restore_config = config or load_restore_config()
    files = sorted(
        find_latest_full_backups(restore_config),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError(f"No .bak file found in {restore_config.vm_import_unc}.")
    return files[0]


def _target_path_str(path: Path, config: BackupRestoreConfig) -> str:
    """Return path string in the correct format for the target OS SQL context."""
    s = str(path)
    return s.replace("\\", "/") if config.is_linux else s


def target_path(*parts, config: BackupRestoreConfig) -> Path:
    """Join *parts* the way the **target host** writes a path, not the way this machine does.

    Three separators are in play and only one is right: the orchestrator's (an Ubuntu worker), the
    target's, and whichever one `pathlib.Path` picks from the platform it was imported on. Joining
    with `Path` silently chose the third, so a restore path for a Windows SQL Server came off the
    Linux worker with a forward slash in the middle of it.

    Forcing Windows is not the fix either — SQL Server runs on Linux, and `config.is_linux` is what
    says which this target is. The result is converted through `str` because
    `Path(PureWindowsPath(...))` copies the *parts* and re-joins them locally, which is the same
    bug wearing a different hat.
    """
    flavour = PurePosixPath if config.is_linux else PureWindowsPath
    joined = flavour(parts[0])
    for part in parts[1:]:
        joined = joined / flavour(part)
    return Path(str(joined))


def vm_unc_to_local_path(path: str | Path, config: BackupRestoreConfig) -> Path:
    """Where a file copied to the target's import share is, as the *target* sees it."""
    backup_path = Path(path)
    relative_path = backup_path.relative_to(config.vm_import_unc)
    return target_path(config.vm_import_local, relative_path, config=config)




def _backup_sort_timestamp(path: Path, *, fallback_mtime: float) -> float:
    match = _BACKUP_TIMESTAMP_RE.search(path.name)
    if not match:
        return fallback_mtime
    try:
        return datetime.datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return fallback_mtime


def find_latest_full_backups(config: BackupRestoreConfig | None = None, *, now: float | None = None) -> list[Path]:
    restore_config = config or load_restore_config()
    if restore_config.is_linux:
        return _find_latest_full_backups_linux(restore_config, now=now)
    grouped: dict[str, Path] = {}
    database_filter = {item.source_database.lower() for item in restore_config.databases}
    cutoff = (time.time() if now is None else now) - (restore_config.copy_recent_hours * 60 * 60)
    for path in restore_config.vm_import_unc.rglob("*.bak"):
        if not path.is_file() or path.parent.name.lower() != restore_config.full_backup_subdir.lower():
            continue
        if database_filter and path.parent.parent.name.lower() not in database_filter:
            continue
        if restore_config.copy_recent_hours > 0 and path.stat().st_mtime < cutoff:
            continue
        source_key = str(path.parent.parent.relative_to(restore_config.vm_import_unc)).lower()
        current = grouped.get(source_key)
        if current is None or path.stat().st_mtime > current.stat().st_mtime:
            grouped[source_key] = path
    return sorted(grouped.values(), key=lambda path: str(path.parent.parent.relative_to(restore_config.vm_import_unc)).lower())


def _find_latest_full_backups_linux(config: BackupRestoreConfig, *, now: float | None = None) -> list[Path]:
    linux_import = str(config.vm_import_unc).replace("\\", "/")
    full_subdir = config.full_backup_subdir
    database_filter = {item.source_database.lower() for item in config.databases}
    cutoff = (time.time() if now is None else now) - (config.copy_recent_hours * 60 * 60) if config.copy_recent_hours > 0 else None

    with open_ssh_connection(config) as ssh:
        _, stdout, _ = ssh.exec_command(
            f'find {shlex.quote(linux_import)} -type f -name "*.bak" -printf "%T@ %p\\n" 2>/dev/null || true'
        )
        lines = stdout.read().decode("utf-8", errors="replace").splitlines()

    grouped: dict[str, tuple[float, Path]] = {}
    for line in lines:
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        try:
            mtime = float(parts[0])
        except ValueError:
            continue
        fpath = parts[1].strip()
        p = PurePosixPath(fpath)
        # Expected layout: {import_root}/{DB_NAME}/{FULL}/{file.bak}
        if p.parent.name.lower() != full_subdir.lower():
            continue
        db_name = p.parent.parent.name
        if database_filter and db_name.lower() not in database_filter:
            continue
        if cutoff is not None and mtime < cutoff:
            continue
        source_key = db_name.lower()
        existing_mtime, _ = grouped.get(source_key, (0.0, None))
        if mtime > existing_mtime:
            grouped[source_key] = (mtime, Path(fpath))

    return sorted((v for _, v in grouped.values()), key=lambda p: str(p).lower())


def _find_log_backups_linux_with_mtime(config: BackupRestoreConfig, linux_db_dir: str) -> list[tuple[float, str]]:
    return _find_linux_files_with_mtime(config, f"{linux_db_dir}/LOG", "*.trn")


def _find_linux_files_with_mtime(
    config: BackupRestoreConfig,
    directory: str | Path,
    pattern: str,
) -> list[tuple[float, str]]:
    linux_dir = str(directory).replace("\\", "/")
    with open_ssh_connection(config) as ssh:
        _, stdout, _ = ssh.exec_command(
            f'find {shlex.quote(linux_dir)} -maxdepth 1 -type f -name {shlex.quote(pattern)} '
            f'-printf "%T@ %p\\n" 2>/dev/null || true'
        )
        lines = stdout.read().decode("utf-8", errors="replace").splitlines()
    result: list[tuple[float, str]] = []
    for line in lines:
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        try:
            mtime = float(parts[0])
        except ValueError:
            continue
        result.append((mtime, parts[1].strip()))
    return result


def _linux_file_mtime(config: BackupRestoreConfig, path: str | Path) -> float | None:
    linux_path = str(path).replace("\\", "/")
    with open_ssh_connection(config) as ssh:
        _, stdout, _ = ssh.exec_command(
            f'stat -c "%Y" {shlex.quote(linux_path)} 2>/dev/null || true'
        )
        value = stdout.read().decode("utf-8", errors="replace").strip()
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _backup_path_exists(config: BackupRestoreConfig, path: str | Path) -> bool:
    if not config.is_linux:
        return Path(path).is_file()
    linux_path = str(path).replace("\\", "/")
    with open_ssh_connection(config) as ssh:
        _, stdout, _ = ssh.exec_command(
            f'test -f {shlex.quote(linux_path)} && printf yes || true'
        )
        return stdout.read().decode("utf-8", errors="replace").strip() == "yes"


def build_restore_candidate(
    backup_file_unc: str | Path,
    config: BackupRestoreConfig,
    *,
    database: DatabaseRestoreMapping | None = None,
) -> RestoreCandidate:
    backup_unc = Path(backup_file_unc)
    backup_on_vm = vm_unc_to_local_path(backup_unc, config)
    source_root = backup_unc.parent.parent
    # Names a folder on the VM, so it is written the way the VM writes it, whatever this machine
    # would have used as a separator.
    source_key = str(target_path(source_root.relative_to(config.vm_import_unc), config=config))
    source_database = source_root.name
    mapping = database or _find_database_mapping(config, source_database)
    restore_database = mapping.target_database if mapping and mapping.target_database else config.restore_database_name or source_database
    safe_restore_name = _safe_file_stem(restore_database)
    return RestoreCandidate(
        source_key=source_key,
        source_database_name=source_database,
        restore_database_name=restore_database,
        backup_file_unc=backup_unc,
        backup_file_on_vm=backup_on_vm,
        restore_data_file_on_vm=target_path(
            config.restore_data_dir_on_vm, f"{safe_restore_name}.mdf", config=config),
        restore_log_file_on_vm=target_path(
            config.restore_data_dir_on_vm, f"{safe_restore_name}_log.ldf", config=config),
    )


def build_restore_sql(
    backup_file: str | Path,
    config: BackupRestoreConfig | None = None,
    *,
    candidate: RestoreCandidate | None = None,
) -> str:
    restore_config = config or load_restore_config()
    restore_candidate = candidate or _candidate_from_backup_argument(backup_file, restore_config)
    bak_path = _escape_sql_string(_target_path_str(restore_candidate.backup_file_on_vm, restore_config))
    data_path = _escape_sql_string(_target_path_str(restore_candidate.restore_data_file_on_vm, restore_config))
    log_path = _escape_sql_string(_target_path_str(restore_candidate.restore_log_file_on_vm, restore_config))
    return f"""
USE master;

IF DB_ID(N'{_escape_sql_string(restore_candidate.restore_database_name)}') IS NOT NULL
    AND DATABASEPROPERTYEX(N'{_escape_sql_string(restore_candidate.restore_database_name)}', N'Status') != N'RESTORING'
BEGIN
    ALTER DATABASE [{_escape_identifier(restore_candidate.restore_database_name)}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
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
EXEC('RESTORE FILELISTONLY FROM DISK = N''{bak_path}''');

DECLARE @dataLogicalName nvarchar(128) = (SELECT TOP (1) LogicalName FROM @filelist WHERE [Type] = 'D' ORDER BY FileId);
DECLARE @logLogicalName nvarchar(128) = (SELECT TOP (1) LogicalName FROM @filelist WHERE [Type] = 'L' ORDER BY FileId);

IF @dataLogicalName IS NULL OR @logLogicalName IS NULL
BEGIN
    THROW 51000, 'Backup file does not contain one data file and one log file.', 1;
END;

DECLARE @restoreSql nvarchar(max) = N'
RESTORE DATABASE [{_escape_identifier(restore_candidate.restore_database_name)}]
FROM DISK = N''{bak_path}''
WITH
    MOVE N''' + REPLACE(@dataLogicalName, '''', '''''') + N''' TO N''{data_path}'',
    MOVE N''' + REPLACE(@logLogicalName, '''', '''''') + N''' TO N''{log_path}'',
    REPLACE,
    RECOVERY,
    CHECKSUM,
    STATS = 10;';

EXEC sys.sp_executesql @restoreSql;

ALTER DATABASE [{_escape_identifier(restore_candidate.restore_database_name)}] SET MULTI_USER;
""".strip()


def build_restore_full_sql(
    backup_file: str | Path,
    config: BackupRestoreConfig | None = None,
    *,
    candidate: RestoreCandidate | None = None,
) -> str:
    sql = build_restore_sql(backup_file, config, candidate=candidate)
    sql = sql.replace("\n    RECOVERY,\n", "\n    NORECOVERY,\n")
    multi_user = f"\n\nALTER DATABASE [{_escape_identifier((candidate or _candidate_from_backup_argument(backup_file, config or load_restore_config())).restore_database_name)}] SET MULTI_USER;"
    if sql.endswith(multi_user):
        sql = sql[: -len(multi_user)]
    return sql


def build_restore_diff_sql(backup_file: str | Path, candidate: RestoreCandidate, config: BackupRestoreConfig | None = None) -> str:
    path_str = _target_path_str(Path(str(backup_file)), config) if config else str(backup_file)
    return f"""
USE master;
RESTORE DATABASE [{_escape_identifier(candidate.restore_database_name)}]
FROM DISK = N'{_escape_sql_string(path_str)}'
WITH NORECOVERY, CHECKSUM, STATS = 10;
""".strip()


def build_restore_log_sql(backup_file: str | Path, candidate: RestoreCandidate, config: BackupRestoreConfig | None = None) -> str:
    path_str = _target_path_str(Path(str(backup_file)), config) if config else str(backup_file)
    return f"""
USE master;
RESTORE LOG [{_escape_identifier(candidate.restore_database_name)}]
FROM DISK = N'{_escape_sql_string(path_str)}'
WITH NORECOVERY, CHECKSUM, STATS = 10;
""".strip()


def build_restore_log_stopat_sql(
    backup_file: str | Path,
    candidate: RestoreCandidate,
    stopat_utc: datetime.datetime,
    config: BackupRestoreConfig | None = None,
) -> str:
    path_str = _target_path_str(Path(str(backup_file)), config) if config else str(backup_file)
    stopat_str = stopat_utc.strftime('%Y-%m-%dT%H:%M:%S')
    return f"""
USE master;
RESTORE LOG [{_escape_identifier(candidate.restore_database_name)}]
FROM DISK = N'{_escape_sql_string(path_str)}'
WITH RECOVERY, CHECKSUM, STATS = 10, STOPAT = N'{stopat_str}';
""".strip()


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


def build_sqlcmd_query_command(*, sql: str, config: BackupRestoreConfig) -> list[str]:
    sql_auth_args = _build_sqlcmd_auth_args(config)
    timeout_args = [
        "-l",
        str(config.sql_login_timeout_seconds),
        "-t",
        str(config.sql_query_timeout_seconds),
    ]
    if config.vm_credential_target and config.is_linux:
        # Linux: returned as a sentinel — actual execution is SSH-based via run_sqlcmd_query_command.
        return ["__ssh_sqlcmd__", config.vm_credential_target, config.restore_sql_instance_on_vm, sql, *timeout_args]
    if config.vm_credential_target:
        open_timeout_ms = max(config.remote_command_timeout_seconds, 1) * 1000
        operation_timeout_ms = (
            config.restore_command_timeout_seconds * 1000
            if config.restore_command_timeout_seconds > 0
            else 2_147_483_647
        )
        password = ""
        if config.vm_username and config.vm_password_env:
            password = resolve_password_ref(config.vm_password_env)
            if not password:
                raise RuntimeError(f"Password ref not found in environment or secret_text.json: {config.vm_password_env}")
        # The Invoke-Command wrapper (credential, session option, script block) is shared —
        # see db_ops.lib.powershell. Only the remote body below is restore-specific.
        return powershell.build_invoke_command_argv(
            host=config.vm_credential_target,
            username=config.vm_username if password else "",
            password=password,
            open_timeout_ms=open_timeout_ms,
            operation_timeout_ms=operation_timeout_ms,
            arguments=[config.sqlcmd_path, config.restore_sql_instance_on_vm, sql],
            script_body=[
                "    param($SqlcmdPath, $SqlInstance, $Sql)",
                f"    $sqlAuthArgs = @({_ps_array(sql_auth_args)})",
                f"    $timeoutArgs = @({_ps_array(timeout_args)})",
                "    & $SqlcmdPath -S $SqlInstance -C @sqlAuthArgs @timeoutArgs -b -Q $Sql",
                "    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
            ],
        )
    return [
        config.sqlcmd_path,
        "-S",
        config.restore_sql_instance_on_vm,
        "-C",
        *sql_auth_args,
        *timeout_args,
        "-b",
        "-Q",
        sql,
    ]


def _remote_exec_type(config: BackupRestoreConfig) -> str:
    if config.vm_credential_target:
        return "ssh" if config.is_linux else "powershell"
    return "local"


def _assert_sql_command_target(cmd: list[str], config: BackupRestoreConfig) -> str:
    exec_type = _remote_exec_type(config)
    first = str(cmd[0]).lower() if cmd else ""
    is_ssh = first == "__ssh_sqlcmd__"
    is_powershell = is_powershell_executable(cmd[0]) if cmd else False

    if config.is_linux and not is_ssh:
        raise RuntimeError(
            f"Target context mismatch: restore_id={config.restore_id} target_host={config.vm_credential_target} "
            f"target_os_type=linux cannot execute remote_exec_type={'powershell' if is_powershell else 'local'}."
        )
    if not config.is_linux and config.vm_credential_target and not is_powershell:
        raise RuntimeError(
            f"Target context mismatch: restore_id={config.restore_id} target_host={config.vm_credential_target} "
            f"target_os_type=windows cannot execute remote_exec_type={'ssh' if is_ssh else 'local'}."
        )
    if is_ssh:
        if len(cmd) < 4 or cmd[1] != config.vm_credential_target or cmd[2] != config.restore_sql_instance_on_vm:
            raise RuntimeError(
                f"Target context mismatch: restore_id={config.restore_id} SSH command target does not match "
                f"target_host={config.vm_credential_target} sql_instance={config.restore_sql_instance_on_vm}."
            )
    if is_powershell:
        expected = f"Invoke-Command -ComputerName {_ps_quote(config.vm_credential_target)}"
        script = cmd[-1] if cmd else ""
        if expected not in script:
            raise RuntimeError(
                f"Target context mismatch: restore_id={config.restore_id} PowerShell command does not match "
                f"target_host={config.vm_credential_target}."
            )
    return exec_type


def run_restore_database(
    *,
    config: BackupRestoreConfig | None = None,
    db_ops_config: DbOpsConfig | None = None,
    database: DatabaseRestoreMapping | str | None = None,
    backup_file: str | Path | None = None,
    dry_run: bool = False,
    ensure_certificate: bool = True,
    ensure_credential: bool = True,
    logger: object | None = None,
    point_in_time_utc: datetime.datetime | None = None,
) -> dict[str, object]:
    restore_config = config or load_restore_config()
    app_config = db_ops_config or load_config()
    validate_restore_target_is_not_source(restore_config)
    start_monotonic = time.monotonic()
    if ensure_credential:
        ensure_vm_share_credential(restore_config)
    certificate_result = None
    if ensure_certificate:
        certificate_result = ensure_source_certificate_with_events(
            config=restore_config,
            app_config=app_config,
            dry_run=dry_run,
            logger=logger,
        )
    database_mapping = _coerce_database_mapping(database)
    database_label = _restore_database_label(None, database_mapping)
    _emit_restore_log(
        logger,
        "restore-db start "
        + _format_metadata(restore_id=restore_config.restore_id or None, source_id=restore_config.source_id, target_id=restore_config.target_id, database=database_label),
    )
    restore_mode = "POINT_IN_TIME" if point_in_time_utc is not None else "LATEST"
    try:
        if backup_file:
            selected_backup_unc = Path(backup_file)
        elif point_in_time_utc is not None:
            selected_backup_unc = get_full_backup_for_pitr(restore_config, database_mapping, point_in_time_utc)
        else:
            selected_backup_unc = get_latest_full_backup_for_database(restore_config, database_mapping)
    except Exception as exc:
        _emit_restore_log(
            logger,
            "restore-db backup-chain skipped "
            + _format_metadata(restore_id=restore_config.restore_id or None, database=database_label, reason=str(exc)),
            level="critical",
        )
        _emit_restore_log(
            logger,
            "restore-db failed "
            + _format_metadata(restore_id=restore_config.restore_id or None, database=database_label, step="backup-chain", error=str(exc)),
            level="critical",
        )
        raise
    candidate = build_restore_candidate(selected_backup_unc, restore_config, database=database_mapping)
    skipped_backups = _skipped_full_backups(restore_config, selected_backup_unc, database_mapping)
    if point_in_time_utc is not None:
        selected_full_ts = _path_backup_timestamp(restore_config, selected_backup_unc)
        if selected_full_ts is None:
            raise FileNotFoundError(f"Selected PITR FULL backup is missing on target: {selected_backup_unc}")
        if selected_full_ts > point_in_time_utc.timestamp():
            raise ValueError(
                f"Selected FULL backup is newer than requested PITR time. "
                f"database={candidate.source_database_name} full={selected_backup_unc} "
                f"full_time_utc={datetime.datetime.fromtimestamp(selected_full_ts, tz=datetime.timezone.utc).isoformat()} "
                f"point_in_time_utc={point_in_time_utc.isoformat()}"
            )
    if point_in_time_utc is not None:
        selected_diff_backup = find_restore_diff_backup_for_pitr(restore_config, candidate, point_in_time_utc)
        selected_log_backups = find_restore_log_backups_for_pitr(restore_config, candidate, selected_diff_backup, point_in_time_utc)
    else:
        selected_diff_backup = find_restore_diff_backup(restore_config, candidate)
        selected_log_backups = find_restore_log_backups(restore_config, candidate, selected_diff_backup)
    _emit_restore_log(
        logger,
        "restore-db backup-chain selected "
        + _format_metadata(
            restore_id=restore_config.restore_id or None,
            database=candidate.source_database_name,
            target_id=restore_config.target_id,
            target_os_type=restore_config.vm_platform,
            restore_mode=restore_mode,
            selected_full=candidate.backup_file_unc,
            selected_diff=selected_diff_backup or "null",
            selected_log_count=len(selected_log_backups),
            first_log=selected_log_backups[0] if selected_log_backups else "null",
            last_log=selected_log_backups[-1] if selected_log_backups else "null",
            recovery_mode="NORECOVERY until final RECOVERY",
            skipped_full=len(skipped_backups),
        ),
    )
    skipped_log_reasons = _find_skipped_log_reasons(
        restore_config,
        candidate,
        selected_diff_backup,
        selected_log_backups,
    )
    for skipped_log, reason in skipped_log_reasons:
        _emit_restore_log(
            logger,
            "restore-db restore-log skipped "
            + _format_metadata(
                restore_id=restore_config.restore_id or None,
                target_id=restore_config.target_id,
                target_os_type=restore_config.vm_platform,
                database=candidate.source_database_name,
                restore_mode=restore_mode,
                file=skipped_log,
                reason=reason,
            ),
        )

    selected_chain = [candidate.backup_file_unc]
    if selected_diff_backup:
        selected_chain.append(selected_diff_backup)
    selected_chain.extend(selected_log_backups)
    for selected_path in selected_chain:
        if not _backup_path_exists(restore_config, selected_path):
            reason = "file_not_copied" if Path(selected_path).suffix.lower() in {".bak", ".trn"} else "path_missing"
            _emit_restore_log(
                logger,
                "restore-db backup-chain skipped "
                + _format_metadata(
                    restore_id=restore_config.restore_id or None,
                    target_id=restore_config.target_id,
                    target_os_type=restore_config.vm_platform,
                    database=candidate.source_database_name,
                    restore_mode=restore_mode,
                    file=selected_path,
                    reason=reason,
                ),
                level="critical",
            )
            raise FileNotFoundError(f"Selected restore file is missing on target: {selected_path}")

    # Print the restore plan before executing so the exact FULL -> DIFF -> LOG chain and the
    # STOPAT point are auditable up front (and visible in a dry run). Only the final LOG uses
    # STOPAT + RECOVERY; every earlier restore stays NORECOVERY.
    stopat_log = selected_log_backups[-1] if (point_in_time_utc is not None and selected_log_backups) else None
    _emit_restore_log(
        logger,
        "restore-db restore-plan "
        + _format_metadata(
            restore_id=restore_config.restore_id or None,
            database=candidate.source_database_name,
            target_database=candidate.restore_database_name,
            restore_mode=restore_mode,
            selected_full=candidate.backup_file_unc,
            selected_diff=selected_diff_backup or "null",
            selected_log_count=len(selected_log_backups),
            first_log=selected_log_backups[0] if selected_log_backups else "null",
            last_log=selected_log_backups[-1] if selected_log_backups else "null",
            stopat_log=stopat_log or "null",
            stopat_value=point_in_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if point_in_time_utc is not None else "null",
        ),
    )
    print(
        "\n".join(
            [
                f"Restore plan for restore_id={restore_config.restore_id or '?'} database={candidate.source_database_name} mode={restore_mode}",
                f"  Selected FULL    : {candidate.backup_file_unc}",
                f"  Selected DIFF    : {selected_diff_backup or 'none'}",
                f"  Selected LOG count: {len(selected_log_backups)}",
                f"  First LOG        : {selected_log_backups[0] if selected_log_backups else 'none'}",
                f"  Last LOG         : {selected_log_backups[-1] if selected_log_backups else 'none'}",
                f"  STOPAT log       : {stopat_log or 'none'}",
                f"  STOPAT value     : {point_in_time_utc.strftime('%Y-%m-%dT%H:%M:%SZ') if point_in_time_utc is not None else 'none'}",
            ]
        ),
        flush=True,
    )

    if dry_run:
        full_sql = build_restore_full_sql(candidate.backup_file_on_vm, restore_config, candidate=candidate)
        _emit_restore_log(
            logger,
            "restore-db completed "
            + _format_metadata(restore_id=restore_config.restore_id or None, database=candidate.source_database_name, status="DRY_RUN", target_database=candidate.restore_database_name),
        )
        return {
            "status": "DRY_RUN",
            "overall_status": "DRY_RUN",
            "restore_mode": restore_mode,
            "point_in_time_utc": point_in_time_utc.isoformat() if point_in_time_utc else None,
            "source_id": restore_config.source_id,
            "target_id": restore_config.target_id,
            "source_key": candidate.source_key,
            "source_database": candidate.source_database_name,
            "database_name": candidate.restore_database_name,
            "selected_full_backup": str(candidate.backup_file_unc),
            "selected_diff_backup": str(selected_diff_backup) if selected_diff_backup else None,
            "selected_log_backups": [str(path) for path in selected_log_backups],
            "skipped_backups": skipped_backups,
            "per_database_restore_status": {candidate.restore_database_name: "DRY_RUN"},
            "final_recovery_status": "DRY_RUN",
            "final_recovery_model_status": "DRY_RUN",
            "backup_file_unc": str(candidate.backup_file_unc),
            "backup_file_on_vm": str(candidate.backup_file_on_vm),
            "sql": full_sql,
            "command": build_sqlcmd_query_command(sql=full_sql, config=restore_config),
            "steps": [
                {"step": "restore-full", "status": "DRY_RUN", "sql": full_sql},
                {"step": "restore-diff", "status": "DRY_RUN" if selected_diff_backup else "SKIPPED", "selected_backup": str(selected_diff_backup) if selected_diff_backup else None},
                {"step": "restore-log", "status": "DRY_RUN" if selected_log_backups else "SKIPPED", "selected_backups": [str(path) for path in selected_log_backups]},
                {"step": "recovery", "status": "DRY_RUN", "sql": build_recovery_if_restoring_sql(candidate) if point_in_time_utc is not None else build_recovery_sql(candidate)},
                {"step": "set-recovery-model", "status": "DRY_RUN", "sql": build_set_recovery_model_full_sql(candidate)},
                {"step": "dbcc-checkdb", "status": "DRY_RUN", "sql": build_checkdb_sql(candidate)},
            ],
            "certificate": certificate_result,
        }

    history = BackupRestoreHistory.from_config(app_config)
    started_at = utc_now_text()
    start_monotonic = time.monotonic()
    restore_id = history.start_restore(
        database_name=candidate.restore_database_name,
        backup_file=str(candidate.backup_file_on_vm),
        restore_start=started_at,
    )
    try:
        steps = [
            run_restore_full(config=restore_config, candidate=candidate, logger=logger),
            run_restore_diff(config=restore_config, candidate=candidate, selected_backup=selected_diff_backup, logger=logger),
            run_restore_log(config=restore_config, candidate=candidate, selected_backups=selected_log_backups, logger=logger, stopat_utc=point_in_time_utc),
        ]
        if point_in_time_utc is not None:
            # The final LOG restore normally recovers the DB (WITH RECOVERY + STOPAT). If that log
            # was skipped (Msg 4305 stray/non-contiguous log) or the chain ended exactly at the
            # target, the DB is still RESTORING -- finalize it so SET RECOVERY FULL does not fail
            # with Msg 5052 ("ALTER DATABASE is not permitted while ... Restoring"). No-op if online.
            recovery_result = run_restore_recovery_if_restoring(config=restore_config, candidate=candidate, logger=logger)
            steps.append(recovery_result)
        else:
            recovery_result = run_restore_recovery(config=restore_config, candidate=candidate, logger=logger)
            steps.append(recovery_result)
        recovery_model_result = run_set_recovery_model_full(config=restore_config, candidate=candidate, logger=logger)
        steps.append(recovery_model_result)
        checkdb_result = run_restore_checkdb(config=restore_config, candidate=candidate, logger=logger)
        steps.append(checkdb_result)
    except Exception as exc:
        history.finish_restore(
            restore_id=restore_id,
            status="FAILED",
            duration_seconds=round(time.monotonic() - start_monotonic, 3),
            error_message=sanitize_text(str(exc)),
        )
        _emit_restore_log(
            logger,
            "restore-db failed "
            + _format_metadata(restore_id=restore_config.restore_id or None, database=candidate.source_database_name, target_database=candidate.restore_database_name, error=str(exc)),
            level="critical",
        )
        raise

    duration_seconds = round(time.monotonic() - start_monotonic, 3)
    history.finish_restore(
        restore_id=restore_id,
        status="SUCCESS",
        duration_seconds=duration_seconds,
    )
    _emit_restore_log(
        logger,
        "restore-db completed "
        + _format_metadata(
            restore_id=restore_config.restore_id or None,
            database=candidate.source_database_name,
            target_database=candidate.restore_database_name,
            status="SUCCESS",
            duration_seconds=duration_seconds,
        ),
    )
    return {
        "restore_id": restore_id,
        "status": "SUCCESS",
        "overall_status": "SUCCESS",
        "restore_mode": restore_mode,
        "point_in_time_utc": point_in_time_utc.isoformat() if point_in_time_utc else None,
        "source_id": restore_config.source_id,
        "target_id": restore_config.target_id,
        "source_key": candidate.source_key,
        "source_database": candidate.source_database_name,
        "database_name": candidate.restore_database_name,
        "selected_full_backup": str(candidate.backup_file_unc),
        "selected_diff_backup": str(selected_diff_backup) if selected_diff_backup else None,
        "selected_log_backups": [str(path) for path in selected_log_backups],
        "skipped_backups": skipped_backups,
        "per_database_restore_status": {candidate.restore_database_name: "SUCCESS"},
        "final_recovery_status": recovery_result["status"],
        "final_recovery_model_status": recovery_model_result["status"],
        "checkdb_status": checkdb_result["status"],
        "backup_file_unc": str(candidate.backup_file_unc),
        "backup_file_on_vm": str(candidate.backup_file_on_vm),
        "duration_seconds": duration_seconds,
        "stdout": "\n".join(str(step.get("stdout", "")).strip() for step in steps if step.get("stdout")).strip(),
        "stderr": "\n".join(str(step.get("stderr", "")).strip() for step in steps if step.get("stderr")).strip(),
        "steps": steps,
        "certificate": certificate_result,
    }


def run_restore_full(*, config: BackupRestoreConfig, candidate: RestoreCandidate, logger: object | None = None) -> dict[str, object]:
    return _run_restore_step(
        step_name="restore-full",
        config=config,
        sql=build_restore_full_sql(candidate.backup_file_on_vm, config, candidate=candidate),
        logger=logger,
        candidate=candidate,
        metadata={"file": str(candidate.backup_file_on_vm)},
    )


def run_restore_diff(
    *,
    config: BackupRestoreConfig,
    candidate: RestoreCandidate,
    selected_backup: Path | None = None,
    logger: object | None = None,
) -> dict[str, object]:
    if selected_backup:
        return _run_restore_step(
            step_name="restore-diff",
            config=config,
            sql=build_restore_diff_sql(vm_unc_to_local_path(selected_backup, config), candidate, config=config),
            logger=logger,
            candidate=candidate,
            metadata={"file": str(vm_unc_to_local_path(selected_backup, config))},
        )
    _emit_restore_log(
        logger,
        "restore-db restore-diff skipped "
        + _format_metadata(restore_id=config.restore_id or None, database=candidate.source_database_name, target_database=candidate.restore_database_name, reason="no_diff_backup"),
    )
    result = {"step": "restore-diff", "status": "SKIPPED", "selected_backup": None, "stdout": "", "stderr": ""}
    return result


def run_restore_log(
    *,
    config: BackupRestoreConfig,
    candidate: RestoreCandidate,
    selected_backups: list[Path] | None = None,
    logger: object | None = None,
    stopat_utc: datetime.datetime | None = None,
) -> dict[str, object]:
    selected_backups = selected_backups or []
    if selected_backups:
        results = []
        skipped_4305: list[str] = []
        total = len(selected_backups)
        for index, backup in enumerate(selected_backups, start=1):
            backup_on_vm = vm_unc_to_local_path(backup, config)
            is_last_with_stopat = index == total and stopat_utc is not None
            sql = (
                build_restore_log_stopat_sql(backup_on_vm, candidate, stopat_utc, config=config)
                if is_last_with_stopat
                else build_restore_log_sql(backup_on_vm, candidate, config=config)
            )
            metadata: dict[str, object] = {"sequence": index, "total": total, "file": str(backup_on_vm)}
            if is_last_with_stopat:
                metadata["stopat_utc"] = stopat_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
            try:
                result = _run_restore_step(
                    step_name="restore-log",
                    config=config,
                    sql=sql,
                    logger=logger,
                    candidate=candidate,
                    metadata=metadata,
                )
            except RestoreCommandTimeoutError as exc:
                resume_result = _inspect_log_restore_resume_state(
                    config=config,
                    candidate=candidate,
                    current_backup=backup_on_vm,
                    next_backup=(
                        vm_unc_to_local_path(selected_backups[index], config)
                        if index < total
                        else None
                    ),
                    logger=logger,
                )
                if resume_result["resume_decision"] == "confirmed_last_log_restored":
                    result = {
                        "step": "restore-log",
                        "status": "SUCCESS_RESUMED",
                        "selected_backup": str(backup),
                        "stdout": "",
                        "stderr": "",
                    }
                else:
                    raise RuntimeError(
                        f"reason=restore_timeout_resume_unsafe database={candidate.restore_database_name} "
                        f"backup={backup_on_vm} last_confirmed_log={resume_result['last_confirmed_log']}: {exc}"
                    ) from exc
            except Exception as exc:  # noqa: BLE001 - inspect the SQL error to decide skip vs. fail.
                if _is_sqlserver_msg_4305(str(exc)):
                    skipped_4305.append(str(backup_on_vm))
                    _emit_restore_log(
                        logger,
                        "restore-db restore-log skipped "
                        + _format_metadata(
                            restore_id=config.restore_id or None,
                            database=candidate.source_database_name,
                            target_database=candidate.restore_database_name,
                            reason="msg_4305_too_recent_try_next_log",
                            file=str(backup_on_vm),
                            sequence=index,
                            total=total,
                        ),
                    )
                    continue
                # Msg 4326 ("...terminates at LSN X, which is too early to apply...") means this
                # log backup ends before the database's current restore LSN — typically a log
                # taken the same minute as, but just before, the FULL backup. Such a log is
                # redundant for the chain, so skip it and continue rather than failing the DB.
                # (A real gap raises Msg 4305 "too recent to apply", which is NOT skipped.)
                if "too early to apply" in str(exc).lower():
                    _emit_restore_log(
                        logger,
                        "restore-db restore-log skipped "
                        + _format_metadata(
                            restore_id=config.restore_id or None,
                            database=candidate.source_database_name,
                            target_database=candidate.restore_database_name,
                            reason="log_too_early_lsn",
                            file=str(backup_on_vm),
                            sequence=index,
                            total=total,
                        ),
                    )
                    continue
                raise
            results.append(result)
        if not results:
            skipped_text = ", ".join(skipped_4305) if skipped_4305 else "none"
            raise RuntimeError(
                "missing required earlier LOG backup / LSN gap: no selected LOG backup could be applied "
                f"database={candidate.restore_database_name} skipped_msg_4305={skipped_text}"
            )
        return {
            "step": "restore-log",
            "status": "SUCCESS",
            "selected_backups": [str(path) for path in selected_backups],
            "stdout": "\n".join(str(result.get("stdout", "")) for result in results).strip(),
            "stderr": "\n".join(str(result.get("stderr", "")) for result in results).strip(),
            "results": results,
        }
    _emit_restore_log(
        logger,
        "restore-db restore-log skipped "
        + _format_metadata(restore_id=config.restore_id or None, database=candidate.source_database_name, target_database=candidate.restore_database_name, reason="no_log_files_found"),
    )
    return {"step": "restore-log", "status": "SKIPPED", "selected_backups": [], "stdout": "", "stderr": ""}


def run_restore_recovery(*, config: BackupRestoreConfig, candidate: RestoreCandidate, logger: object | None = None) -> dict[str, object]:
    return _run_restore_step(
        step_name="recovery",
        config=config,
        sql=build_recovery_sql(candidate),
        logger=logger,
        candidate=candidate,
        metadata={},
    )


def run_restore_recovery_if_restoring(*, config: BackupRestoreConfig, candidate: RestoreCandidate, logger: object | None = None) -> dict[str, object]:
    return _run_restore_step(
        step_name="recovery",
        config=config,
        sql=build_recovery_if_restoring_sql(candidate),
        logger=logger,
        candidate=candidate,
        metadata={"mode": "pitr_finalize_if_restoring"},
    )


def run_set_recovery_model_full(*, config: BackupRestoreConfig, candidate: RestoreCandidate, logger: object | None = None) -> dict[str, object]:
    return _run_restore_step(
        step_name="set-recovery-model",
        config=config,
        sql=build_set_recovery_model_full_sql(candidate),
        logger=logger,
        candidate=candidate,
        metadata={"recovery_model": "FULL"},
    )


def run_restore_checkdb(*, config: BackupRestoreConfig, candidate: RestoreCandidate, logger: object | None = None) -> dict[str, object]:
    return _run_restore_step(
        step_name="dbcc-checkdb",
        config=config,
        sql=build_checkdb_sql(candidate),
        logger=logger,
        candidate=candidate,
        metadata={},
    )


def _run_restore_step(
    *,
    step_name: str,
    config: BackupRestoreConfig,
    sql: str,
    logger: object | None,
    candidate: RestoreCandidate,
    metadata: dict[str, object],
) -> dict[str, object]:
    _emit_restore_log(
        logger,
        f"restore-db {step_name} start "
        + _format_metadata(restore_id=config.restore_id or None, database=candidate.source_database_name, target_database=candidate.restore_database_name, **metadata),
    )
    cmd = build_sqlcmd_query_command(sql=sql, config=config)
    remote_exec_type = _assert_sql_command_target(cmd, config)
    _emit_restore_log(
        logger,
        f"restore-db {step_name} command_start "
        + _format_metadata(
            restore_id=config.restore_id or None,
            target_id=config.target_id,
            target_host=config.vm_credential_target or "local",
            target_os_type=config.vm_platform,
            remote_exec_type=remote_exec_type,
            sql_instance=config.restore_sql_instance_on_vm,
            command_phase=step_name,
            sql_login_timeout_seconds=config.sql_login_timeout_seconds,
            sql_query_timeout_seconds=config.sql_query_timeout_seconds,
            remote_command_timeout_seconds=config.remote_command_timeout_seconds,
            restore_command_timeout_seconds=config.restore_command_timeout_seconds,
            database=candidate.source_database_name,
        ),
    )
    start_monotonic = time.monotonic()
    try:
        result = run_sqlcmd_query_command(
            cmd,
            config=config,
            logger=logger,
            progress_step=step_name,
            progress_database=candidate.source_database_name,
            restore_id=config.restore_id,
            command_file=str(metadata.get("file") or ""),
        )
        wall_duration_seconds = round(time.monotonic() - start_monotonic, 3)
        stdout_summary = summarize_restore_stdout(result.stdout)
        output = {
            "step": step_name,
            "status": "SUCCESS",
            "sql": sql,
            "command": cmd,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
        _emit_restore_log(
            logger,
            f"restore-db {step_name} command_success "
            + _format_metadata(
                restore_id=config.restore_id or None,
                database=candidate.source_database_name,
                exit_code=result.returncode,
                duration_seconds=wall_duration_seconds,
            ),
        )
        success_metadata = {
            "restore_id": config.restore_id or None,
            "database": candidate.source_database_name,
            "target_database": candidate.restore_database_name,
            **metadata,
            **stdout_summary,
            "wall_duration_seconds": wall_duration_seconds,
        }
        _emit_restore_log(
            logger,
            f"restore-db {step_name} success " + _format_metadata(**success_metadata),
        )
        return output
    except Exception as exc:
        wall_duration_seconds = round(time.monotonic() - start_monotonic, 3)
        error_text = str(exc).lower()
        reason = "lsn_gap" if any(
            marker in error_text
            for marker in ("lsn", "log in this backup set begins at", "too recent to apply", "no files are ready to rollforward")
        ) else "restore_command_failed"
        _emit_restore_log(
            logger,
            f"restore-db {step_name} failed "
            + _format_metadata(
                restore_id=config.restore_id or None,
                database=candidate.source_database_name,
                target_database=candidate.restore_database_name,
                reason=reason,
                error=summarize_error_tail(str(exc)),
                wall_duration_seconds=wall_duration_seconds,
                **metadata,
            ),
            level="critical",
        )
        raise


def _log_restore_step(logger: object | None, step_name: str, phase: str, **metadata: object) -> None:
    message = f"{step_name} {phase}"
    if metadata:
        message = f"{message}: {metadata}"
    if logger:
        log_event(logger, level="logging", message=message)


def summarize_restore_stdout(stdout: str) -> dict[str, object]:
    summary: dict[str, object] = {}
    pages = 0
    sql_duration_seconds: float | None = None
    throughput_mb_sec: float | None = None
    for line in stdout.splitlines():
        file_match = re.search(r"^\s*Processed\s+(\d+)\s+pages\s+for\s+database\b", line, re.IGNORECASE)
        if file_match:
            pages += int(file_match.group(1))
            continue
        summary_match = re.search(
            r"successfully\s+processed\s+(\d+)\s+pages\s+in\s+([0-9.]+)\s+seconds\s+\(([0-9.]+)\s+MB/sec\)",
            line,
            re.IGNORECASE,
        )
        if summary_match:
            if not pages:
                pages = int(summary_match.group(1))
            sql_duration_seconds = float(summary_match.group(2))
            throughput_mb_sec = float(summary_match.group(3))
    if pages:
        summary["pages"] = pages
    if sql_duration_seconds is not None:
        summary["sql_duration_seconds"] = round(sql_duration_seconds, 3)
    if throughput_mb_sec is not None:
        summary["throughput_mb_sec"] = throughput_mb_sec
    return summary


def summarize_error_tail(text: str, *, max_lines: int = 20) -> str:
    lines = sanitize_text(text).splitlines()
    return "\n".join(lines[-max_lines:])


def run_restore_server(
    *,
    config: BackupRestoreConfig | None = None,
    db_ops_config: DbOpsConfig | None = None,
    dry_run: bool = False,
    logger: object | None = None,
    point_in_time_utc: datetime.datetime | None = None,
) -> dict[str, object]:
    restore_config = config or load_restore_config()
    app_config = db_ops_config or load_config()
    validate_restore_target_is_not_source(restore_config)
    start_monotonic = time.monotonic()
    _emit_restore_log(
        logger,
        "restore-instance start " + _format_metadata(restore_id=restore_config.restore_id or None, source_id=restore_config.source_id, target_id=restore_config.target_id),
    )
    ensure_vm_share_credential(restore_config)
    certificate_result = ensure_source_certificate_with_events(
        config=restore_config,
        app_config=app_config,
        dry_run=dry_run,
        logger=logger,
    )
    results: list[dict[str, object]] = []
    if restore_config.databases:
        for database in restore_config.databases:
            db_label = getattr(database, "target_database", None) or getattr(database, "source_database", None) or str(database)
            try:
                results.append(
                    run_restore_database(
                        config=restore_config,
                        db_ops_config=app_config,
                        database=database,
                        dry_run=dry_run,
                        ensure_certificate=False,
                        ensure_credential=False,
                        logger=logger,
                        point_in_time_utc=point_in_time_utc,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one DB's failure must not abort the remaining databases.
                _emit_restore_log(logger, f"restore-database FAILED database={db_label} error={str(exc).replace(chr(10), ' ')[:300]}", level="error")
                if point_in_time_utc is not None:
                    raise
                results.append({"database_name": db_label, "status": "FAILED", "error": str(exc)[:500]})
    else:
        backups = (
            find_full_backups_for_pitr(restore_config, point_in_time_utc)
            if point_in_time_utc is not None
            else find_latest_full_backups(restore_config)
        )
        if not backups:
            if point_in_time_utc is not None:
                raise FileNotFoundError(
                    f"No FULL .bak files found under {restore_config.vm_import_unc} at_or_before={point_in_time_utc.isoformat()}."
                )
            raise FileNotFoundError(f"No latest FULL .bak files found under {restore_config.vm_import_unc}.")
        for backup in backups:
            db_label = backup.parent.parent.name
            try:
                results.append(
                    run_restore_database(
                        config=restore_config,
                        db_ops_config=app_config,
                        backup_file=backup,
                        dry_run=dry_run,
                        ensure_certificate=False,
                        ensure_credential=False,
                        logger=logger,
                        point_in_time_utc=point_in_time_utc,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - restore-all must continue past one DB's failure (e.g. broken log chain).
                _emit_restore_log(logger, f"restore-database FAILED database={db_label} error={str(exc).replace(chr(10), ' ')[:300]}", level="error")
                if point_in_time_utc is not None:
                    raise
                results.append({"database_name": db_label, "status": "FAILED", "error": str(exc)[:500]})
    if not results:
        raise FileNotFoundError(f"No latest FULL .bak files found under {restore_config.vm_import_unc}.")
    per_database_status = {
        str(result.get("database_name")): str(result.get("status"))
        for result in results
        if isinstance(result, dict) and result.get("database_name")
    }
    success_count = sum(1 for status in per_database_status.values() if status in {"SUCCESS", "DRY_RUN"})
    failed_count = sum(1 for status in per_database_status.values() if status not in {"SUCCESS", "DRY_RUN"})
    skipped_count = sum(1 for result in results if isinstance(result, dict) and str(result.get("status")) == "SKIPPED")
    _emit_restore_log(
        logger,
        "restore-instance completed "
        + _format_metadata(
            restore_id=restore_config.restore_id or None,
            source_id=restore_config.source_id,
            target_id=restore_config.target_id,
            databases_considered=len(results),
            success=success_count,
            failed=failed_count,
            skipped=skipped_count,
            duration_seconds=round(time.monotonic() - start_monotonic, 3),
        ),
    )
    # The verdict follows the count above. It used to be hardcoded to SUCCESS, so a run that lost
    # one database out of six still reported SUCCESS - failed_count was computed, logged, and then
    # ignored by the only field anybody reads. On 2026-08-08 that sent a green "Restore workflow
    # finished. status=done" for a drill that never restored APPDB_Prod and left it in RESTORING,
    # unreachable; the failure existed only inside per_database_restore_status. A restore that
    # silently drops a database is precisely the failure this app exists to catch, so it is now
    # impossible for the instance verdict to be greener than its databases.
    overall_status = "DRY_RUN" if dry_run else ("SUCCESS" if failed_count == 0 else "FAILED")
    return {
        "status": overall_status,
        "overall_status": overall_status,
        "source_id": restore_config.source_id,
        "target_id": restore_config.target_id,
        "certificate": certificate_result,
        "databases_considered": len(results),
        "selected_full_backup": [
            result.get("selected_full_backup")
            for result in results
            if isinstance(result, dict) and result.get("selected_full_backup")
        ],
        "selected_diff_backup": [
            result.get("selected_diff_backup")
            for result in results
            if isinstance(result, dict) and result.get("selected_diff_backup")
        ],
        "selected_log_backups": [
            backup
            for result in results
            if isinstance(result, dict)
            for backup in result.get("selected_log_backups", [])
        ],
        "skipped_backups": [
            skipped
            for result in results
            if isinstance(result, dict)
            for skipped in result.get("skipped_backups", [])
        ],
        "per_database_restore_status": per_database_status,
        "final_recovery_status": {
            str(result.get("database_name")): result.get("final_recovery_status")
            for result in results
            if isinstance(result, dict) and result.get("database_name")
        },
        "final_recovery_model_status": {
            str(result.get("database_name")): result.get("final_recovery_model_status")
            for result in results
            if isinstance(result, dict) and result.get("database_name")
        },
        "checkdb_status": {
            str(result.get("database_name")): result.get("checkdb_status")
            for result in results
            if isinstance(result, dict) and result.get("database_name")
        },
        "results": results,
    }


def run_restore_all_latest(
    *,
    config: BackupRestoreConfig | None = None,
    db_ops_config: DbOpsConfig | None = None,
    dry_run: bool = False,
    logger: object | None = None,
    point_in_time_utc: datetime.datetime | None = None,
) -> dict[str, object]:
    return run_restore_server(config=config, db_ops_config=db_ops_config, dry_run=dry_run, logger=logger, point_in_time_utc=point_in_time_utc)


def ensure_source_certificate_with_events(
    *,
    config: BackupRestoreConfig,
    app_config: DbOpsConfig,
    dry_run: bool = False,
    logger: object | None = None,
) -> dict[str, object]:
    from db_ops.backup_restore.certificate import ensure_source_certificate

    if not config.certificate_api_url:
        result = ensure_source_certificate(config=config, dry_run=dry_run, logger=logger)
        _emit_restore_log(
            logger,
            "restore-instance certificate skipped "
            + _format_metadata(restore_id=config.restore_id or None, source_id=config.source_id, target_id=config.target_id, reason="no_certificate_api"),
        )
        return result

    started_at = utc_now_text()
    start_monotonic = time.monotonic()
    metadata = {
        "restore_id": config.restore_id,
        "source_id": config.source_id,
        "target_id": config.target_id,
        "certificate_api_configured": bool(config.certificate_api_url),
        "dry_run": dry_run,
    }
    _emit_restore_log(
        logger,
        "restore-instance certificate start "
        + _format_metadata(restore_id=config.restore_id or None, source_id=config.source_id, target_id=config.target_id, certificate_name="unknown"),
    )
    emit_backup_restore_event(
        app_config=app_config,
        command="restore-latest",
        phase="CERT_START",
        level="logging",
        message=f"certificate import started source_id={config.source_id}",
        started_at=started_at,
        metadata=metadata,
        notify=config.notify,
    )
    try:
        result = ensure_source_certificate(config=config, dry_run=dry_run, logger=logger)
    except Exception as exc:
        _emit_restore_log(
            logger,
            "restore-instance certificate failed "
            + _format_metadata(restore_id=config.restore_id or None, source_id=config.source_id, target_id=config.target_id, certificate_name="unknown", error=str(exc)),
            level="critical",
        )
        emit_backup_restore_event(
            app_config=app_config,
            command="restore-latest",
            phase="CERT_ERROR",
            level="critical",
            message=sanitize_text(f"certificate import failed source_id={config.source_id}: {exc}"),
            started_at=started_at,
            finished_at=utc_now_text(),
            duration_ms=int((time.monotonic() - start_monotonic) * 1000),
            error_text=sanitize_text(str(exc)),
            metadata=metadata,
            notify=config.notify,
        )
        raise

    _emit_restore_log(
        logger,
        "restore-instance certificate success "
        + _format_metadata(
            restore_id=config.restore_id or None,
            source_id=config.source_id,
            target_id=config.target_id,
            certificate_name=result.get("certificate_name") or "unknown",
            thumbprint=result.get("thumbprint"),
            status=result.get("status"),
        ),
    )
    emit_backup_restore_event(
        app_config=app_config,
        command="restore-latest",
        phase="CERT_DONE",
        level="logging",
        message=f"certificate import finished source_id={config.source_id} status={result.get('status')}",
        started_at=started_at,
        finished_at=utc_now_text(),
        duration_ms=int((time.monotonic() - start_monotonic) * 1000),
        metadata={**metadata, "result": sanitize_value(result)},
        notify=config.notify,
    )
    return result


def ensure_vm_share_credential(config: BackupRestoreConfig) -> None:
    if config.is_linux:
        return
    cmd = build_cmdkey_command(
        credential_target=config.vm_credential_target,
        username=config.vm_username,
        password_env=config.vm_password_env,
    )
    if cmd:
        subprocess.run(cmd, check=True, text=True)


def _run_sqlcmd_via_ssh(cmd: list[str], config: BackupRestoreConfig) -> subprocess.CompletedProcess[str]:
    """Execute sqlcmd on a Linux target via SSH (paramiko). cmd is the sentinel list from build_sqlcmd_query_command."""
    _assert_sql_command_target(cmd, config)
    sql = cmd[3]
    sql_auth_args = _build_sqlcmd_auth_args(config)
    auth_str = " ".join(shlex.quote(a) for a in sql_auth_args)
    remote_cmd = (
        "export PATH=$PATH:/opt/mssql-tools/bin:/opt/mssql-tools18/bin; "
        f"{shlex.quote(config.sqlcmd_path)} "
        f"-S {shlex.quote(config.restore_sql_instance_on_vm)} "
        f"-C {auth_str} "
        f"-l {config.sql_login_timeout_seconds} "
        f"-t {config.sql_query_timeout_seconds} -b "
        f"-Q {shlex.quote(sql)}"
    )
    with open_ssh_connection(config) as ssh:
        chan = ssh.get_transport().open_session(
            timeout=config.remote_command_timeout_seconds or None
        )
        chan.exec_command(remote_cmd)
        deadline = (
            time.monotonic() + config.restore_command_timeout_seconds
            if config.restore_command_timeout_seconds > 0
            else None
        )
        stdout_data = b""
        stderr_data = b""
        while not chan.exit_status_ready():
            if chan.recv_ready():
                stdout_data += chan.recv(4096)
            if chan.recv_stderr_ready():
                stderr_data += chan.recv_stderr(4096)
            if deadline is not None and time.monotonic() >= deadline:
                chan.close()
                raise RestoreCommandTimeoutError(
                    f"Restore command timed out after {config.restore_command_timeout_seconds} seconds.",
                    command_started=True,
                )
            time.sleep(0.05)
        # drain remaining
        while chan.recv_ready():
            stdout_data += chan.recv(4096)
        while chan.recv_stderr_ready():
            stderr_data += chan.recv_stderr(4096)
        returncode = chan.recv_exit_status()
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=returncode,
        stdout=stdout_data.decode("utf-8", errors="replace"),
        stderr=stderr_data.decode("utf-8", errors="replace"),
    )


def run_sqlcmd_query_command(
    cmd: list[str],
    *,
    config: BackupRestoreConfig | None = None,
    logger: object | None = None,
    progress_step: str | None = None,
    progress_database: str | None = None,
    restore_id: str = "",
    allow_transient_retry: bool = True,
    command_file: str = "",
) -> subprocess.CompletedProcess[str]:
    if config is not None:
        remote_exec_type = _assert_sql_command_target(cmd, config)
        _emit_restore_log(
            logger,
            "restore-db remote-command dispatch "
            + _format_metadata(
                restore_id=config.restore_id or None,
                target_id=config.target_id,
                target_host=config.vm_credential_target or "local",
                target_os_type=config.vm_platform,
                remote_exec_type=remote_exec_type,
                sql_instance=config.restore_sql_instance_on_vm,
                command_phase=progress_step or "sql",
                sql_login_timeout_seconds=config.sql_login_timeout_seconds,
                sql_query_timeout_seconds=config.sql_query_timeout_seconds,
                remote_command_timeout_seconds=config.remote_command_timeout_seconds,
                restore_command_timeout_seconds=config.restore_command_timeout_seconds,
            ),
        )
    attempts = 3 if allow_transient_retry else 1
    for attempt in range(1, attempts + 1):
        result = _execute_sqlcmd_once(
            cmd,
            config=config,
            logger=logger,
            progress_step=progress_step,
            progress_database=progress_database,
            restore_id=restore_id,
        )
        if result.returncode == 0 and not restore_output_has_failure(result.stdout, result.stderr):
            return result
        combined_output = f"{result.stdout}\n{result.stderr}"
        if attempt < attempts and _is_transient_sql_connection_failure(combined_output):
            _emit_restore_log(
                logger,
                "restore-db remote-command retry "
                + _format_metadata(
                    restore_id=restore_id or (config.restore_id if config else None),
                    target_id=config.target_id if config else None,
                    target_host=config.vm_credential_target if config else None,
                    command_phase=progress_step or "sql",
                    reason="transient_connection_before_restore",
                    resume_decision="retry_safe_not_started",
                    last_confirmed_log="unknown",
                    next_log=command_file or "unknown",
                    attempt=attempt + 1,
                ),
            )
            time.sleep(min(attempt, 2))
            continue
        if progress_step == "restore-log" and _is_ambiguous_sql_connection_failure(combined_output):
            raise RestoreCommandTimeoutError(
                "Connection was lost after RESTORE LOG command dispatch; execution status is ambiguous.",
                command_started=True,
            )
        break
    details = [f"sqlcmd command failed with exit code {result.returncode}."]
    stdout = sanitize_text(result.stdout.strip())
    stderr = sanitize_text(result.stderr.strip())
    if stdout:
        details.append(f"stdout:\n{stdout}")
    if stderr:
        details.append(f"stderr:\n{stderr}")
    if not stdout and not stderr:
        details.append("No stdout/stderr was returned by PowerShell/sqlcmd.")
    raise RuntimeError("\n".join(details))


def _execute_sqlcmd_once(
    cmd: list[str],
    *,
    config: BackupRestoreConfig | None,
    logger: object | None,
    progress_step: str | None,
    progress_database: str | None,
    restore_id: str,
) -> subprocess.CompletedProcess[str]:
    timeout_seconds = config.restore_command_timeout_seconds if config else 0
    if cmd and cmd[0] == "__ssh_sqlcmd__":
        if config is None:
            raise RuntimeError("config is required for SSH sqlcmd execution.")
        return _run_sqlcmd_via_ssh(cmd, config)
    if logger and progress_step:
        return _run_sqlcmd_query_command_streaming(
            cmd,
            logger=logger,
            progress_step=progress_step,
            progress_database=progress_database or "unknown",
            restore_id=restore_id,
            timeout_seconds=timeout_seconds,
        )
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds or None,
        )
    except subprocess.TimeoutExpired as exc:
        raise RestoreCommandTimeoutError(
            f"Restore command timed out after {timeout_seconds} seconds.",
            command_started=True,
        ) from exc


def _run_sqlcmd_query_command_streaming(
    cmd: list[str],
    *,
    logger: object,
    progress_step: str,
    progress_database: str,
    restore_id: str = "",
    timeout_seconds: int = 0,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    stdout_lines: list[str] = []
    assert process.stdout is not None
    def read_stdout() -> None:
        for line in process.stdout:
            stdout_lines.append(line)
            progress = _parse_restore_progress_percent(line)
            if progress is not None:
                _emit_restore_log(
                    logger,
                    f"restore-db {progress_step} progress "
                    + _format_metadata(restore_id=restore_id or None, database=progress_database, percent=progress),
                )

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=timeout_seconds) if timeout_seconds > 0 else process.wait()
    except subprocess.TimeoutExpired as exc:
        process.kill()
        reader.join(timeout=5)
        raise RestoreCommandTimeoutError(
            f"Restore command timed out after {timeout_seconds} seconds.",
            command_started=True,
        ) from exc
    reader.join()
    return subprocess.CompletedProcess(cmd, returncode, "".join(stdout_lines), "")


def _is_transient_sql_connection_failure(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in PRESTART_SQL_CONNECTION_MARKERS)


def _is_ambiguous_sql_connection_failure(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in AMBIGUOUS_SQL_CONNECTION_MARKERS)


def _inspect_log_restore_resume_state(
    *,
    config: BackupRestoreConfig,
    candidate: RestoreCandidate,
    current_backup: Path,
    logger: object | None,
    next_backup: Path | None = None,
) -> dict[str, str]:
    database = _escape_sql_string(candidate.restore_database_name)
    sql = f"""
SET NOCOUNT ON;
DECLARE @database sysname = N'{database}';
DECLARE @active bit =
(
    SELECT CASE WHEN EXISTS
    (
        SELECT 1
        FROM sys.dm_exec_requests
        WHERE command LIKE N'RESTORE%'
          AND (database_id = DB_ID(@database) OR database_id = 0)
    ) THEN 1 ELSE 0 END
);
DECLARE @backup_set_id int;
DECLARE @first_lsn numeric(25, 0);
DECLARE @last_lsn numeric(25, 0);
DECLARE @last_file nvarchar(4000);
DECLARE @restore_file_count int;

SELECT TOP (1)
    @backup_set_id = rh.backup_set_id,
    @first_lsn = bs.first_lsn,
    @last_lsn = bs.last_lsn,
    @last_file = bmf.physical_device_name,
    @restore_file_count =
    (
        SELECT COUNT(*)
        FROM msdb.dbo.restorefile AS rf
        WHERE rf.restore_history_id = rh.restore_history_id
    )
FROM msdb.dbo.restorehistory AS rh
INNER JOIN msdb.dbo.backupset AS bs ON bs.backup_set_id = rh.backup_set_id
INNER JOIN msdb.dbo.backupmediafamily AS bmf ON bmf.media_set_id = bs.media_set_id
WHERE rh.destination_database_name = @database
  AND bs.[type] = 'L'
ORDER BY rh.restore_date DESC, rh.restore_history_id DESC;

SELECT
    N'DBOPS_RESUME|'
    + CONVERT(nvarchar(1), @active) + N'|'
    + COALESCE(CONVERT(nvarchar(20), @backup_set_id), N'') + N'|'
    + COALESCE(CONVERT(nvarchar(40), @first_lsn), N'') + N'|'
    + COALESCE(CONVERT(nvarchar(40), @last_lsn), N'') + N'|'
    + COALESCE(CONVERT(nvarchar(20), @restore_file_count), N'') + N'|'
    + COALESCE(@last_file, N'');
""".strip()
    cmd = build_sqlcmd_query_command(sql=sql, config=config)
    try:
        result = run_sqlcmd_query_command(
            cmd,
            config=config,
            logger=logger,
            progress_step="restore-resume-check",
            progress_database=candidate.source_database_name,
            restore_id=config.restore_id,
            allow_transient_retry=True,
        )
    except Exception as exc:
        _emit_restore_log(
            logger,
            "restore-db restore-log resume-check failed "
            + _format_metadata(
                restore_id=config.restore_id or None,
                target_id=config.target_id,
                target_host=config.vm_credential_target,
                database=candidate.source_database_name,
                reason="restore_timeout_resume_unsafe",
                resume_decision="unsafe",
                last_confirmed_log="unknown",
                next_log=current_backup,
                error=str(exc),
            ),
            level="critical",
        )
        return {"resume_decision": "unsafe", "last_confirmed_log": "unknown"}

    marker = next(
        (line.strip() for line in result.stdout.splitlines() if "DBOPS_RESUME|" in line),
        "",
    )
    parts = marker[marker.find("DBOPS_RESUME|"):].split("|", 6) if marker else []
    if len(parts) != 7:
        _emit_restore_log(
            logger,
            "restore-db restore-log resume-decision "
            + _format_metadata(
                restore_id=config.restore_id or None,
                target_id=config.target_id,
                target_host=config.vm_credential_target,
                database=candidate.source_database_name,
                reason="restore_timeout_resume_unsafe",
                resume_decision="unsafe",
                last_confirmed_log="unknown",
                next_log=current_backup,
            ),
            level="critical",
        )
        return {"resume_decision": "unsafe", "last_confirmed_log": "unknown"}
    active = parts[1].strip() == "1"
    backup_set_id = parts[2].strip()
    first_lsn = parts[3].strip()
    last_lsn = parts[4].strip()
    restore_file_count = parts[5].strip()
    last_file = parts[6].strip()
    current_key = _normalize_restore_path(current_backup)
    last_key = _normalize_restore_path(last_file) if last_file else ""
    try:
        restore_file_count_value = int(restore_file_count)
    except ValueError:
        restore_file_count_value = 0
    exact_identity = bool(
        backup_set_id
        and first_lsn
        and last_lsn
        and restore_file_count_value > 0
        and last_key == current_key
    )
    resume_decision = (
        "confirmed_last_log_restored"
        if exact_identity and not active
        else "unsafe"
    )
    _emit_restore_log(
        logger,
        "restore-db restore-log resume-decision "
        + _format_metadata(
            restore_id=config.restore_id or None,
            target_id=config.target_id,
            target_host=config.vm_credential_target,
            database=candidate.source_database_name,
            active_restore=active,
            backup_set_id=backup_set_id or "null",
            first_lsn=first_lsn or "null",
            last_lsn=last_lsn or "null",
            restore_file_count=restore_file_count or "null",
            resume_decision=resume_decision,
            last_confirmed_log=last_file or "null",
            next_log=(
                next_backup
                if resume_decision == "confirmed_last_log_restored" and next_backup
                else current_backup if resume_decision == "unsafe"
                else "null"
            ),
        ),
        level="logging" if resume_decision != "unsafe" else "critical",
    )
    return {
        "resume_decision": resume_decision,
        "last_confirmed_log": last_file or "null",
    }


def _normalize_restore_path(path: str | Path) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def _parse_restore_progress_percent(line: str) -> int | None:
    lowered = line.lower()
    if "percent" not in lowered:
        return None
    parts = lowered.replace(".", " ").split()
    for index, part in enumerate(parts[:-1]):
        if part.isdigit() and parts[index + 1].startswith("percent"):
            return int(part)
    return None


def restore_output_has_failure(stdout: str | None, stderr: str | None) -> bool:
    text = f"{stdout or ''}\n{stderr or ''}".lower()
    return any(marker in text for marker in RESTORE_FAILURE_MARKERS)


def _is_sqlserver_msg_4305(text: str) -> bool:
    lowered = text.lower()
    return "msg 4305" in lowered and "too recent to apply" in lowered


def get_latest_full_backup_for_database(config: BackupRestoreConfig, database: DatabaseRestoreMapping | None) -> Path:
    if database is None:
        return get_latest_full_backup(config)
    backup_dir = _database_full_backup_dir(config, database.source_database)
    if config.is_linux:
        cutoff = time.time() - (config.copy_recent_hours * 60 * 60) if config.copy_recent_hours > 0 else None
        files = [
            (mtime, Path(path))
            for mtime, path in _find_linux_files_with_mtime(config, backup_dir, "*.bak")
            if cutoff is None or mtime >= cutoff
        ]
        if not files:
            raise FileNotFoundError(f"No recent .bak file found for database {database.source_database} in {backup_dir}.")
        return sorted(files, key=lambda item: (item[0], str(item[1]).lower()), reverse=True)[0][1]
    files = sorted(
        [
            path
            for path in backup_dir.glob("*.bak")
            if path.is_file()
            and (config.copy_recent_hours <= 0 or path.stat().st_mtime >= time.time() - (config.copy_recent_hours * 60 * 60))
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError(f"No recent .bak file found for database {database.source_database} in {backup_dir}.")
    return files[0]


def get_full_backup_for_pitr(
    config: BackupRestoreConfig,
    database: DatabaseRestoreMapping | None,
    point_in_time_utc: datetime.datetime,
) -> Path:
    pit_ts = point_in_time_utc.timestamp()
    database_label = database.source_database if database is not None else config.source_database_name or "all"
    if database is None:
        candidates = [
            (backup_timestamp, path)
            for path in find_full_backups_for_pitr(config, point_in_time_utc)
            for backup_timestamp in [_path_backup_timestamp(config, path)]
            if backup_timestamp is not None and backup_timestamp <= pit_ts
        ]
    else:
        backup_dir = _database_full_backup_dir(config, database.source_database)
        if config.is_linux:
            candidates = [
                (_backup_sort_timestamp(Path(path), fallback_mtime=mtime), Path(path))
                for mtime, path in _find_linux_files_with_mtime(config, backup_dir, "*.bak")
                if _backup_sort_timestamp(Path(path), fallback_mtime=mtime) <= pit_ts
            ]
        else:
            candidates = [
                (_backup_sort_timestamp(path, fallback_mtime=path.stat().st_mtime), path)
                for path in backup_dir.glob("*.bak")
                if path.is_file() and _backup_sort_timestamp(path, fallback_mtime=path.stat().st_mtime) <= pit_ts
            ]
    if not candidates:
        raise FileNotFoundError(
            f"No FULL backup found for PITR database={database_label} at_or_before={point_in_time_utc.isoformat()}."
        )
    return sorted(candidates, key=lambda item: (item[0], str(item[1]).lower()), reverse=True)[0][1]


def find_full_backups_for_pitr(config: BackupRestoreConfig, point_in_time_utc: datetime.datetime) -> list[Path]:
    pit_ts = point_in_time_utc.timestamp()
    database_filter = {item.source_database.lower() for item in config.databases}
    grouped: dict[str, tuple[float, Path]] = {}
    if config.is_linux:
        linux_import = str(config.vm_import_unc).replace("\\", "/")
        with open_ssh_connection(config) as ssh:
            _, stdout, _ = ssh.exec_command(
                f'find {shlex.quote(linux_import)} -type f -name "*.bak" -printf "%T@ %p\\n" 2>/dev/null || true'
            )
            lines = stdout.read().decode("utf-8", errors="replace").splitlines()
        for line in lines:
            parts = line.split(" ", 1)
            if len(parts) < 2:
                continue
            try:
                mtime = float(parts[0])
            except ValueError:
                continue
            path = Path(parts[1].strip())
            posix = PurePosixPath(str(path))
            if posix.parent.name.lower() != config.full_backup_subdir.lower():
                continue
            db_name = posix.parent.parent.name
            if database_filter and db_name.lower() not in database_filter:
                continue
            backup_ts = _backup_sort_timestamp(path, fallback_mtime=mtime)
            if backup_ts > pit_ts:
                continue
            current = grouped.get(db_name.lower())
            if current is None or backup_ts > current[0]:
                grouped[db_name.lower()] = (backup_ts, path)
    else:
        for path in config.vm_import_unc.rglob("*.bak"):
            if not path.is_file() or path.parent.name.lower() != config.full_backup_subdir.lower():
                continue
            db_name = path.parent.parent.name
            if database_filter and db_name.lower() not in database_filter:
                continue
            backup_ts = _backup_sort_timestamp(path, fallback_mtime=path.stat().st_mtime)
            if backup_ts > pit_ts:
                continue
            source_key = str(path.parent.parent.relative_to(config.vm_import_unc)).lower()
            current = grouped.get(source_key)
            if current is None or backup_ts > current[0]:
                grouped[source_key] = (backup_ts, path)
    return sorted((path for _, path in grouped.values()), key=lambda path: str(path).lower())


def _path_backup_timestamp(config: BackupRestoreConfig, path: Path) -> float | None:
    if config.is_linux:
        mtime = _linux_file_mtime(config, path)
        return _backup_sort_timestamp(path, fallback_mtime=mtime) if mtime is not None else None
    if not path.exists():
        return None
    return _backup_sort_timestamp(path, fallback_mtime=path.stat().st_mtime)


def _skipped_full_backups(
    config: BackupRestoreConfig,
    selected_backup: Path,
    database: DatabaseRestoreMapping | None,
) -> list[dict[str, str]]:
    if database is None:
        candidates = find_latest_full_backups(config)
    elif config.is_linux:
        candidates = [
            Path(path)
            for _, path in _find_linux_files_with_mtime(
                config,
                _database_full_backup_dir(config, database.source_database),
                "*.bak",
            )
        ]
    else:
        candidates = [path for path in _database_full_backup_dir(config, database.source_database).glob("*.bak") if path.is_file()]
    return [
        {"backup_file": str(path), "reason": "not_latest_full"}
        for path in sorted(candidates, key=lambda item: str(item).lower())
        if path != selected_backup
    ]












def _safe_file_stem(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("_", "-") else "_" for char in value)


def _find_database_mapping(config: BackupRestoreConfig, source_database: str) -> DatabaseRestoreMapping | None:
    for database in config.databases:
        if database.source_database.lower() == source_database.lower():
            return database
    return None


def _coerce_database_mapping(database: DatabaseRestoreMapping | str | None) -> DatabaseRestoreMapping | None:
    if database is None:
        return None
    if isinstance(database, DatabaseRestoreMapping):
        return database
    return DatabaseRestoreMapping(source_database=str(database))


def _database_full_backup_dir(config: BackupRestoreConfig, source_database: str) -> Path:
    if config.is_linux:
        return config.vm_import_unc / source_database / config.full_backup_subdir
    candidates = [
        path
        for path in config.vm_import_unc.rglob(config.full_backup_subdir)
        if path.is_dir() and path.parent.name.lower() == source_database.lower()
    ]
    if candidates:
        return sorted(candidates, key=lambda path: str(path).lower())[0]
    return config.vm_import_unc / source_database / config.full_backup_subdir


def find_restore_diff_backup(config: BackupRestoreConfig, candidate: RestoreCandidate) -> Path | None:
    diff_dir = candidate.backup_file_unc.parent.parent / "DIFF"
    if config.is_linux:
        full_mtime = _linux_file_mtime(config, candidate.backup_file_unc)
        if full_mtime is None:
            return None
        files = [
            (mtime, Path(path))
            for mtime, path in _find_linux_files_with_mtime(config, diff_dir, "*.bak")
            if mtime >= full_mtime
        ]
        return sorted(files, key=lambda item: (item[0], str(item[1]).lower()))[-1][1] if files else None
    if not diff_dir.exists():
        return None
    full_mtime = candidate.backup_file_unc.stat().st_mtime if candidate.backup_file_unc.exists() else 0
    files = [
        path
        for path in diff_dir.glob("*.bak")
        if path.is_file() and (not candidate.backup_file_unc.exists() or path.stat().st_mtime >= full_mtime)
    ]
    if not files:
        return None
    return sorted(files, key=lambda path: (path.stat().st_mtime, str(path).lower()))[-1]


def find_restore_log_backups(config: BackupRestoreConfig, candidate: RestoreCandidate, diff_backup: Path | None = None) -> list[Path]:
    log_dir = candidate.backup_file_unc.parent.parent / "LOG"
    if config.is_linux:
        baseline = diff_backup or candidate.backup_file_unc
        baseline_mtime = _linux_file_mtime(config, baseline)
        if baseline_mtime is None:
            return []
        return [
            Path(path)
            for mtime, path in sorted(
                _find_linux_files_with_mtime(config, log_dir, "*.trn"),
                key=lambda item: (item[0], item[1].lower()),
            )
            if mtime >= baseline_mtime
        ]
    if not log_dir.exists():
        return []
    baseline = diff_backup or candidate.backup_file_unc
    baseline_mtime = baseline.stat().st_mtime if baseline.exists() else 0
    return sorted(
        [
            path
            for path in log_dir.glob("*.trn")
            if path.is_file() and (not baseline.exists() or path.stat().st_mtime >= baseline_mtime)
        ],
        key=lambda path: (path.stat().st_mtime, str(path).lower()),
    )


def _find_skipped_log_reasons(
    config: BackupRestoreConfig,
    candidate: RestoreCandidate,
    diff_backup: Path | None,
    selected_logs: list[Path],
) -> list[tuple[str, str]]:
    log_dir = candidate.backup_file_unc.parent.parent / "LOG"
    if config.is_linux:
        entries = _find_linux_files_with_mtime(config, log_dir, "*.trn")
        baseline_mtime = _linux_file_mtime(config, diff_backup or candidate.backup_file_unc)
    else:
        entries = (
            [(path.stat().st_mtime, str(path)) for path in log_dir.glob("*.trn") if path.is_file()]
            if log_dir.exists()
            else []
        )
        baseline = diff_backup or candidate.backup_file_unc
        baseline_mtime = baseline.stat().st_mtime if baseline.exists() else None

    if not entries:
        return [("null", "no_log_files_found")] if not selected_logs else []
    if baseline_mtime is None:
        return [(str(diff_backup or candidate.backup_file_unc), "path_missing")]

    selected = {str(path).replace("\\", "/").lower() for path in selected_logs}
    before_reason = "log_before_diff" if diff_backup else "log_before_full"
    return [
        (path, before_reason)
        for mtime, path in sorted(entries, key=lambda item: (item[0], item[1].lower()))
        if mtime < baseline_mtime and path.replace("\\", "/").lower() not in selected
    ]


def find_restore_diff_backup_for_pitr(
    config: BackupRestoreConfig,
    candidate: RestoreCandidate,
    point_in_time_utc: datetime.datetime,
) -> Path | None:
    pit_ts = point_in_time_utc.timestamp()
    if config.is_linux:
        full_linux = str(candidate.backup_file_unc).replace("\\", "/")
        # Navigate up two levels: .../DB_NAME/FULL/file.bak -> .../DB_NAME
        db_linux_dir = "/".join(full_linux.split("/")[:-2])
        diff_dir = f"{db_linux_dir}/DIFF"
        with open_ssh_connection(config) as ssh:
            _, st_out, _ = ssh.exec_command(f'stat -c "%Y" {shlex.quote(full_linux)} 2>/dev/null || echo 0')
            full_stat_mtime = float(st_out.read().decode("utf-8", errors="replace").strip() or "0")
            full_mtime = _backup_sort_timestamp(Path(full_linux), fallback_mtime=full_stat_mtime)
            _, df_out, _ = ssh.exec_command(
                f'find {shlex.quote(diff_dir)} -maxdepth 1 -type f -name "*.bak" -printf "%T@ %p\\n" 2>/dev/null || true'
            )
            lines = df_out.read().decode("utf-8", errors="replace").splitlines()
        entries = []
        for line in lines:
            parts = line.split(" ", 1)
            if len(parts) < 2:
                continue
            try:
                mtime = float(parts[0])
            except ValueError:
                continue
            fpath = parts[1].strip()
            entries.append((_backup_sort_timestamp(Path(fpath), fallback_mtime=mtime), fpath))
        valid = [(mtime, fpath) for mtime, fpath in entries if mtime >= full_mtime and mtime <= pit_ts]
        if not valid:
            return None
        return Path(sorted(valid)[-1][1])
    else:
        diff_dir = candidate.backup_file_unc.parent.parent / "DIFF"
        if not diff_dir.exists():
            return None
        full_mtime = (
            _backup_sort_timestamp(candidate.backup_file_unc, fallback_mtime=candidate.backup_file_unc.stat().st_mtime)
            if candidate.backup_file_unc.exists()
            else 0
        )
        files = [
            p for p in diff_dir.glob("*.bak")
            if p.is_file()
            and _backup_sort_timestamp(p, fallback_mtime=p.stat().st_mtime) >= full_mtime
            and _backup_sort_timestamp(p, fallback_mtime=p.stat().st_mtime) <= pit_ts
        ]
        if not files:
            return None
        return sorted(files, key=lambda p: (_backup_sort_timestamp(p, fallback_mtime=p.stat().st_mtime), str(p).lower()))[-1]


def find_restore_log_backups_for_pitr(
    config: BackupRestoreConfig,
    candidate: RestoreCandidate,
    diff_backup: Path | None,
    point_in_time_utc: datetime.datetime,
) -> list[Path]:
    pit_ts = point_in_time_utc.timestamp()
    if config.is_linux:
        full_linux = str(candidate.backup_file_unc).replace("\\", "/")
        db_linux_dir = "/".join(full_linux.split("/")[:-2])
        baseline_linux = str(diff_backup).replace("\\", "/") if diff_backup else full_linux
        with open_ssh_connection(config) as ssh:
            _, st_out, _ = ssh.exec_command(f'stat -c "%Y" {shlex.quote(baseline_linux)} 2>/dev/null || echo 0')
            baseline_stat_mtime = float(st_out.read().decode("utf-8", errors="replace").strip() or "0")
            baseline_mtime = _backup_sort_timestamp(Path(baseline_linux), fallback_mtime=baseline_stat_mtime)
        entries = _find_log_backups_linux_with_mtime(config, db_linux_dir)
        entries_after = [
            (_backup_sort_timestamp(Path(fpath), fallback_mtime=mtime), fpath)
            for mtime, fpath in entries
            if _backup_sort_timestamp(Path(fpath), fallback_mtime=mtime) >= baseline_mtime
        ]
        entries_sorted = sorted(entries_after, key=lambda x: (x[0], x[1]))
    else:
        log_dir = candidate.backup_file_unc.parent.parent / "LOG"
        if not log_dir.exists():
            entries_sorted = []
        else:
            baseline = diff_backup or candidate.backup_file_unc
            baseline_mtime = _backup_sort_timestamp(baseline, fallback_mtime=baseline.stat().st_mtime) if baseline.exists() else 0
            all_logs = sorted(
                [
                    p for p in log_dir.glob("*.trn")
                    if p.is_file() and _backup_sort_timestamp(p, fallback_mtime=p.stat().st_mtime) >= baseline_mtime
                ],
                key=lambda p: (_backup_sort_timestamp(p, fallback_mtime=p.stat().st_mtime), str(p).lower()),
            )
            entries_sorted = [(_backup_sort_timestamp(p, fallback_mtime=p.stat().st_mtime), str(p)) for p in all_logs]

    if not entries_sorted:
        raise ValueError(
            f"No transaction log backups found for PITR to {point_in_time_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}."
        )

    # Collect logs up to and including the first one that reaches the target time. A log whose
    # backup timestamp is >= the target spans (or ends exactly at) the target, so it is the last
    # one we need. Using >= (not >) stops the chain when the target lands exactly on a log
    # boundary -- otherwise we would wrongly pull in the next log, which may be a far-future,
    # non-contiguous backup (LSN gap) that SQL Server rejects with Msg 4305 ("too recent to apply").
    result_paths: list[Path] = []
    for mtime, fpath in entries_sorted:
        result_paths.append(Path(fpath))
        if mtime >= pit_ts:
            break

    last_mtime = entries_sorted[len(result_paths) - 1][0]
    if last_mtime < pit_ts:
        latest_log_utc = datetime.datetime.fromtimestamp(last_mtime, tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        raise ValueError(
            f"Target point-in-time {point_in_time_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} is beyond the available log chain. "
            f"Latest log backup ends approximately {latest_log_utc}."
        )

    return result_paths


def _candidate_from_backup_argument(backup_file: str | Path, config: BackupRestoreConfig) -> RestoreCandidate:
    backup_path = Path(backup_file)
    try:
        return build_restore_candidate(backup_path, config)
    except ValueError:
        source_database = config.source_database_name or backup_path.parent.parent.name or backup_path.stem
        restore_database = config.restore_database_name or source_database
        safe_restore_name = _safe_file_stem(restore_database)
        return RestoreCandidate(
            source_key=source_database,
            source_database_name=source_database,
            restore_database_name=restore_database,
            backup_file_unc=backup_path,
            backup_file_on_vm=backup_path,
            restore_data_file_on_vm=config.restore_data_file_on_vm or config.restore_data_dir_on_vm / f"{safe_restore_name}.mdf",
            restore_log_file_on_vm=config.restore_log_file_on_vm or target_path(
                config.restore_data_dir_on_vm, f"{safe_restore_name}_log.ldf", config=config),
        )
