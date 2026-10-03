"""A SQL Server restore: which backup files a restore needs: the newest FULL, its DIFF, the LOGs after it - for the latest point or a moment - on a share or on a Linux host.

Split out of ``backup_restore/restore_database.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``restore_database``
re-exports every name, so no import changes.
"""

from __future__ import annotations
from db_ops.backup_restore.shell_quoting import _BACKUP_TIMESTAMP_RE, backup_time_from_name, _build_sqlcmd_auth_args, _escape_identifier, _escape_sql_string, _ps_array, _ps_quote  # noqa: F401 - one definition
import datetime
import shlex
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from db_ops.backup_restore.config import (
    BackupRestoreConfig,
    DatabaseRestoreMapping,
    load_restore_config,
)
from db_ops.backup_restore.copy_backup import open_ssh_connection
from db_ops.backup_restore.restore_base import RestoreCandidate, _normalize_restore_path


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
    stamped = backup_time_from_name(path.name)
    return fallback_mtime if stamped is None else stamped


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
        answer = ssh.run(
            f'find {shlex.quote(linux_import)} -type f -name "*.bak" -printf "%T@ %p\\n" 2>/dev/null || true'
        )
        lines = answer.stdout.splitlines()

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
        answer = ssh.run(
            f'find {shlex.quote(linux_dir)} -maxdepth 1 -type f -name {shlex.quote(pattern)} '
            f'-printf "%T@ %p\\n" 2>/dev/null || true'
        )
        lines = answer.stdout.splitlines()
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
        answer = ssh.run(
            f'stat -c "%Y" {shlex.quote(linux_path)} 2>/dev/null || true'
        )
        value = answer.stdout.strip()
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _missing_backup_paths(config: BackupRestoreConfig, paths: list[Path] | list[str]) -> set[str]:
    """The chain's files that are NOT on the target, asked in ONE session.

    Asked one file at a time until 0.24.1: a FULL and its logs are a session each, every session a
    fresh SSH connection, and on 2026-09-27 a lab VM dropped one of them (``WinError 10054``) while
    a 97-log chain was being checked - which failed the database, and cost 2 min 46 s before it
    did. The answer is compared by :func:`_normalize_restore_path`.
    """
    if not paths:
        return set()
    if not config.is_linux:
        return {_normalize_restore_path(path) for path in paths if not Path(path).is_file()}
    posix = [str(path).replace("\\", "/") for path in paths]
    script = "\n".join(f"[ -f {shlex.quote(path)} ] || printf '%s\\n' {shlex.quote(path)}" for path in posix)
    with open_ssh_connection(config) as ssh:
        answer = ssh.run_script(script)
    return {_normalize_restore_path(line.strip()) for line in answer.stdout.splitlines() if line.strip()}


def _backup_path_exists(config: BackupRestoreConfig, path: str | Path) -> bool:
    if not config.is_linux:
        return Path(path).is_file()
    linux_path = str(path).replace("\\", "/")
    with open_ssh_connection(config) as ssh:
        answer = ssh.run(
            f'test -f {shlex.quote(linux_path)} && printf yes || true'
        )
        return answer.stdout.strip() == "yes"


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
            answer = ssh.run(
                f'find {shlex.quote(linux_import)} -type f -name "*.bak" -printf "%T@ %p\\n" 2>/dev/null || true'
            )
            lines = answer.stdout.splitlines()
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
            stat_answer = ssh.run(f'stat -c "%Y" {shlex.quote(full_linux)} 2>/dev/null || echo 0')
            full_stat_mtime = float(stat_answer.stdout.strip() or "0")
            full_mtime = _backup_sort_timestamp(Path(full_linux), fallback_mtime=full_stat_mtime)
            diff_answer = ssh.run(
                f'find {shlex.quote(diff_dir)} -maxdepth 1 -type f -name "*.bak" -printf "%T@ %p\\n" 2>/dev/null || true'
            )
            lines = diff_answer.stdout.splitlines()
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
            stat_answer = ssh.run(f'stat -c "%Y" {shlex.quote(baseline_linux)} 2>/dev/null || echo 0')
            baseline_stat_mtime = float(stat_answer.stdout.strip() or "0")
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
