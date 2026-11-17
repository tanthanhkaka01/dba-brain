from __future__ import annotations
from dbabrain.lib import errors
from dbabrain.backup_restore.shell_quoting import _BACKUP_TIMESTAMP_RE, backup_time_from_name, _build_sqlcmd_auth_args, _escape_identifier, _escape_sql_string, _ps_array, _ps_quote  # noqa: F401 - one definition
import datetime
import shlex
import subprocess
import threading
import time
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from dbabrain.backup_restore.config import (
    BackupRestoreConfig,
    DatabaseRestoreMapping,
    load_restore_config,
    assert_target_is_allowed,
    validate_restore_target_is_not_source,
)
from dbabrain.backup_restore.copy_backup import open_ssh_connection, resolve_password_ref, share_login_request, store_share_logins
from dbabrain.lib import powershell
from dbabrain.lib.shell import is_powershell_executable, powershell_executable
from dbabrain.backup_restore.events import emit_backup_restore_event
from dbabrain.backup_restore.history import BackupRestoreHistory
from dbabrain.backup_restore.sanitize import compact_log_value, sanitize_text, sanitize_value
from dbabrain.lib.config import DbaBrainConfig, load_config
from dbabrain.db.store import utc_now_text
from dbabrain.logging_ops import log_event
from dbabrain.backup_restore.restore_base import (  # noqa: F401 - re-exported: every name kept its address
    RestoreCandidate,
    _emit_restore_log,
    _format_metadata,
    _normalize_restore_path,
)
from dbabrain.backup_restore.restore_find import (  # noqa: F401 - re-exported: every name kept its address
    _backup_path_exists,
    _backup_sort_timestamp,
    _coerce_database_mapping,
    _database_full_backup_dir,
    _find_database_mapping,
    _find_latest_full_backups_linux,
    _find_linux_files_with_mtime,
    _find_log_backups_linux_with_mtime,
    _find_skipped_log_reasons,
    _linux_file_mtime,
    _missing_backup_paths,
    _path_backup_timestamp,
    _safe_file_stem,
    _skipped_full_backups,
    _target_path_str,
    find_full_backups_for_pitr,
    find_latest_full_backups,
    find_restore_diff_backup,
    find_restore_diff_backup_for_pitr,
    find_restore_log_backups,
    find_restore_log_backups_for_pitr,
    get_full_backup_for_pitr,
    get_latest_full_backup,
    get_latest_full_backup_for_database,
    target_path,
    vm_unc_to_local_path,
)
from dbabrain.backup_restore.restore_sql import (  # noqa: F401 - re-exported: every name kept its address
    build_checkdb_sql,
    build_recovery_if_restoring_sql,
    build_recovery_sql,
    build_set_recovery_model_full_sql,
    composed_restore_sql,
)
from dbabrain.backup_restore.restore_sqlcmd import (  # noqa: F401 - re-exported: every name kept its address
    AMBIGUOUS_SQL_CONNECTION_MARKERS,
    PRESTART_SQL_CONNECTION_MARKERS,
    RESTORE_FAILURE_MARKERS,
    RestoreCommandTimeoutError,
    _SqlcmdCommand,
    _assert_sql_command_target,
    _execute_sqlcmd_once,
    _is_ambiguous_sql_connection_failure,
    _is_sqlserver_msg_4305,
    _is_transient_sql_connection_failure,
    _local_request_from_argv,
    _log_restore_progress,
    _parse_restore_progress_percent,
    _remote_exec_type,
    _restore_step_command,
    _run_sqlcmd_query_command_streaming,
    _run_sqlcmd_via_ssh,
    _sql_of,
    _sqlcmd_argv,
    _sqlcmd_in_common,
    _sqlcmd_request,
    build_sqlcmd_query_command,
    restore_output_has_failure,
    run_sqlcmd_query_command,
)


@dataclass
class ChainRecord:
    """Where a share-driven SQL Server restore writes the files it applies (0.39.0, 1.367).

    The operator, 2026-11-13: *dbabrain must keep the full chain of files a restore used, so it can be
    shown which file went into which restore*. The restore from the repository has written
    ``restore_chain_files`` since 1.367; this path - full, diff and each log applied by ``sqlcmd`` from
    a share - wrote nothing. One record per source: the run row is the copy's
    (``run_copy_backup(...).restore_run_id``), and each database adds its own chain to it as it is
    reached (``append_chain``). Every file gets a row ``planned`` before its database's first RESTORE,
    then ``applied``, ``skipped`` with SQL Server's reason, or ``failed`` with the error.

    **The record is not the restore**: a store that cannot be written is said in the log, once per
    database, and the restore goes on - as the other path's ``_record_chain`` does.
    """

    catalog: object
    restore_run_id: int
    source_server_id: str
    target_server_id: str

    def plan(self, rows: list[dict[str, object]], *, logger: object | None = None) -> list[int]:
        """``rows`` added to the run's chain, each tied to its catalogued backup file when the catalog
        knows it by the source server and the file's name; their positions, or ``[]`` when not written."""
        try:
            known = self.catalog.backup_file_facts(  # type: ignore[attr-defined]
                server_id=self.source_server_id, file_names=sorted({str(row["file_name"]) for row in rows}))
            for row in rows:
                fact = known.get(str(row["file_name"])) or {}
                if fact:
                    row["file_id"] = fact["file_id"]
                    row["file_type"] = str(fact.get("file_type") or "")
                    row["backup_ended_at"] = row.get("backup_ended_at") or fact.get("backup_ended_at")
            return list(self.catalog.append_chain(int(self.restore_run_id), rows))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - the record is not the restore; it is said, never silent
            _emit_restore_log(logger, "restore-db chain not-recorded "
                              + _format_metadata(restore_run_id=self.restore_run_id,
                                                 reason=f"{type(exc).__name__}: {exc}"[:300]), level="warning")
            return []

    def mark(self, positions: list[int], result: str, error_text: str = "") -> None:
        if not positions:
            return
        try:
            self.catalog.set_chain_step(int(self.restore_run_id), list(positions),  # type: ignore[attr-defined]
                                        result=result, error_text=error_text)
        except Exception:  # noqa: BLE001 - the record is not the restore
            return


def _chain_rows(chain: ChainRecord, candidate: "RestoreCandidate", config: BackupRestoreConfig,
                diff: Path | None, logs: list, stopat_utc: datetime.datetime | None) -> list[dict[str, object]]:
    """The files one database's restore is about to apply, in order, as chain rows - each by the path
    the target's SQL Server reads it at, as the RESTORE statement names it."""
    files: list[tuple[str, str]] = [("restore-full", str(candidate.backup_file_on_vm))]
    if diff:
        files.append(("restore-diff", str(vm_unc_to_local_path(diff, config))))
    files.extend(("restore-log", str(vm_unc_to_local_path(log, config))) for log in logs)
    rows = []
    for number, (step, path) in enumerate(files, start=1):
        last_log = step == "restore-log" and number == len(files)
        rows.append({"source_server_id": chain.source_server_id, "target_server_id": chain.target_server_id,
                     "database_name": candidate.source_database_name, "restored_as": candidate.restore_database_name,
                     "step": step, "file_name": PureWindowsPath(path).name if "\\" in path else PurePosixPath(path).name,
                     "file_path": path,
                     # The last log of a point-in-time restore stops inside itself and recovers; a latest
                     # restore recovers in a step of its own, after the chain.
                     "with_recovery": 1 if last_log and stopat_utc is not None else 0,
                     "stopat": stopat_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if last_log and stopat_utc is not None else None})
    return rows


def _where_to_stop(candidate: "RestoreCandidate", logs: list, heads: list[dict[str, object]] | None,
                   point_in_time_utc: datetime.datetime | None, *, logger: object | None = None,
                   restore_id: str | None = None) -> tuple[str | None, dict[str, object]]:
    """``(stopat_source, expect)`` for one database of a share-driven restore (0.39.0, 1.363 / 1.367).

    **The clock.** A SQL Server backup's header times and its log records are in the SOURCE
    instance's clock, and STOPAT is read in that clock. This path handed STOPAT in UTC - the defect
    1.363 found and fixed on the restore from the repository (a +07 source stopped seven hours early
    and said done). The offset is read the way that path reads it
    (``restore_by_id.source_offset_minutes``): a file named by its UTC start (dbabrain's own
    ``_YYYYMMDD_HHMMSSZ``) against its header's ``BackupFinishDate``. A name without the ``Z`` - a
    backup another tool wrote - or headers that could not be read leave the moment as it was handed.

    **What it is held to** (``expect``, the shape ``restore_by_id.where_it_stopped`` reads): with a
    STOPAT, the target's ``stop_at`` is that moment; a moment at or past the newest log, or a latest
    restore, the last backup the target applied ended when the chain's last file did. Unknown - no
    headers - is ``not read`` there, never a difference.
    """
    from dbabrain.backup_restore.restore_by_id import in_source_clock, past_the_newest, source_offset_minutes

    files = []
    for head in heads or []:
        files.append({"path": str(head.get("path") or ""), "finished_at": head.get("finished_at")})
    chain_ends = str(files[-1]["finished_at"] or "") if files and files[-1].get("finished_at") else ""
    expect: dict[str, object] = {"database_name": candidate.restore_database_name,
                                 "source_database": candidate.source_database_name,
                                 "moment": "", "stopat": False, "chain_ends": chain_ends}
    if point_in_time_utc is None or not logs:
        return None, expect
    moment = point_in_time_utc.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    offset = source_offset_minutes(files) if files else None
    in_source = in_source_clock(moment, offset) if offset is not None else ""
    log_files = [item for item, head in zip(files, heads or []) if str(head.get("backup_type")) == "2"]
    reached = past_the_newest(log_files, in_source) if in_source and log_files else ""
    if reached:
        # The STOPAT would lie past the last log: SQL Server leaves the database RESTORING (Msg 4326)
        # and the recovery step after the chain opens it at the log's end - held to that end.
        return None, expect
    _emit_restore_log(logger, "restore-db point-in-time clock "
                      + _format_metadata(restore_id=restore_id or None, database=candidate.source_database_name,
                                         asked_utc=moment, source_offset_minutes=offset if offset is not None else "unknown",
                                         stopat=in_source or moment))
    expect.update(moment=in_source or point_in_time_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"), stopat=True)
    return (in_source or None), expect


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


def _restore_database_label(candidate: RestoreCandidate | None, database: DatabaseRestoreMapping | None = None) -> str:
    if candidate is not None:
        return candidate.source_database_name
    if database is not None:
        return database.source_database
    return "unknown"


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


def _restore_step(level: str, candidate: RestoreCandidate, config: BackupRestoreConfig, *,
                  path: str | Path, recovery: bool = False,
                  stopat_utc: datetime.datetime | None = None,
                  stopat_source: str | None = None) -> dict[str, object]:
    """What ``common.cli restore-<level>`` is asked for one step of this restore.

    The RESTORE statement itself is written in one place, ``common/restorestep/sqlserver.py``
    (rules R43, the operator's choice, 0.24.0): this module composed its own until then, beside the
    one the drills used. What stays here is what only this app knows - which files, as the target
    sees them, and where the restored database's files go. The text that comes back is the text
    this module wrote before, byte for byte (``tests/test_one_sqlserver_restore_statement.py``).
    """
    fields: dict[str, object] = {
        "db_type": "sqlserver",
        "database_name": candidate.restore_database_name,
        "backup_path": _target_path_str(Path(str(path)), config),
        "with_recovery": bool(recovery),
    }
    if level == "full":
        # The entry's word, never assumed: without it a database ONLINE on the target is refused.
        # ... and never while a lockdown is on (1.220).
        from dbabrain.db.lockdown_store import lockdown_on

        fields["overwrite_existing"] = bool(config.overwrite_existing) and not lockdown_on()
        # The logical names are read on the server (RESTORE FILELISTONLY), as they always were;
        # only where the two files go is this app's to say.
        fields["move_files"] = {
            "data": _target_path_str(candidate.restore_data_file_on_vm, config),
            "log": _target_path_str(candidate.restore_log_file_on_vm, config),
        }
    if stopat_source:
        # The moment in the source's clock, with no offset: STOPAT is read against the log's own
        # times, which are the source server's (1.363) - handed in UTC, a +07 source stopped 7 h early.
        fields["stopat"] = stopat_source
    elif stopat_utc is not None:
        fields["stopat"] = stopat_utc.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return fields


@dataclass
class DatabasePlan:
    """One database of a share-driven restore: the chain it applies and where it stops - read before
    anything is restored, and what each of its steps reads (0.40.0, 1.375)."""

    restore_config: Any
    app_config: Any
    candidate: Any
    selected_diff_backup: Any
    selected_log_backups: list[Any]
    skipped_backups: list[Any]
    restore_mode: str
    point_in_time_utc: Any
    stopat_source: Any
    expect: Any
    logger: Any = None
    certificate_result: Any = None
    # Filled by begin_database():
    history: Any = None
    restore_id: Any = None
    start_monotonic: float = 0.0
    chain: Any = None
    at_full: list[int] = field(default_factory=list)
    at_diff: list[int] = field(default_factory=list)
    at_logs: list[int] = field(default_factory=list)
    restore_room: Any = None
    steps: list[dict[str, object]] = field(default_factory=list)


def plan_database(
    *,
    config: BackupRestoreConfig | None = None,
    dbabrain_config: DbaBrainConfig | None = None,
    database: DatabaseRestoreMapping | str | None = None,
    backup_file: str | Path | None = None,
    logger: object | None = None,
    point_in_time_utc: datetime.datetime | None = None,
) -> DatabasePlan:
    """The chain one database applies - full, diff, logs - held to its LSNs and to where it is to stop,
    before anything is restored. Answers; says nothing."""
    restore_config = config or load_restore_config()
    app_config = dbabrain_config or load_config()
    validate_restore_target_is_not_source(restore_config)
    # Never into a production instance the entry does not allow (1.223).
    assert_target_is_allowed(restore_id=getattr(restore_config, "restore_id", ""),
                             target_server_id=getattr(restore_config, "target_id", ""),
                             allowed=bool(getattr(restore_config, "allow_production_target", False)))
    start_monotonic = time.monotonic()
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
    missing = _missing_backup_paths(restore_config, selected_chain)
    for selected_path in selected_chain:
        if _normalize_restore_path(selected_path) in missing:
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
    # The chain is held to its LSNs before anything is restored (1.250, gap G10), as restore-by-id's
    # is: a missing log found at RESTORE LOG leaves the target half-restored, and found here it is
    # named instead.
    heads = _check_backup_chain(restore_config, candidate, selected_diff_backup, selected_log_backups,
                                logger=logger, restore_mode=restore_mode)
    # Where this database is to stop, in the SOURCE's clock (0.39.0, 1.363 / 1.367), from the headers
    # the check just read: the moment for STOPAT, and what the target's own record is held to after
    # the check (``expect``, read by the workflow with ``restore_by_id.where_it_stopped``).
    stopat_source, expect = _where_to_stop(candidate, selected_log_backups, heads, point_in_time_utc,
                                           logger=logger, restore_id=restore_config.restore_id)

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

    return DatabasePlan(restore_config=restore_config, app_config=app_config, candidate=candidate,
                        selected_diff_backup=selected_diff_backup, selected_log_backups=selected_log_backups,
                        skipped_backups=skipped_backups, restore_mode=restore_mode,
                        point_in_time_utc=point_in_time_utc, stopat_source=stopat_source, expect=expect,
                        logger=logger)


def dry_run_answer(plan: DatabasePlan) -> dict[str, object]:
    """What a dry run says of one database: its chain and the statements it would run - nothing touched."""
    restore_config, candidate, logger = plan.restore_config, plan.candidate, plan.logger
    selected_diff_backup, selected_log_backups = plan.selected_diff_backup, plan.selected_log_backups
    restore_mode, point_in_time_utc = plan.restore_mode, plan.point_in_time_utc
    skipped_backups, certificate_result = plan.skipped_backups, plan.certificate_result
    full_sql = composed_restore_sql("full", _restore_step(
        "full", candidate, restore_config, path=candidate.backup_file_on_vm))
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
            ({"step": "dbcc-checkdb", "status": "DRY_RUN", "sql": build_checkdb_sql(candidate)}
             if restore_config.checkdb else
             {"step": "dbcc-checkdb", "status": "SKIPPED", "reason": CHECKDB_OFF_REASON}),
        ],
        "certificate": certificate_result,
    }



def begin_database(plan: DatabasePlan, *, chain: ChainRecord | None = None) -> None:
    """The database's history row, its chain written before its first RESTORE (1.367), and the room its
    files need on the target (§1.76)."""
    plan.history = BackupRestoreHistory.from_config(plan.app_config)
    plan.start_monotonic = time.monotonic()
    plan.restore_id = plan.history.start_restore(
        database_name=plan.candidate.restore_database_name,
        backup_file=str(plan.candidate.backup_file_on_vm),
        restore_start=utc_now_text(),
    )
    plan.chain = chain
    positions = (chain.plan(_chain_rows(chain, plan.candidate, plan.restore_config, plan.selected_diff_backup,
                                        plan.selected_log_backups, plan.point_in_time_utc), logger=plan.logger)
                 if chain is not None else [])
    plan.at_full = positions[:1]
    plan.at_diff = positions[1:2] if plan.selected_diff_backup else []
    plan.at_logs = positions[(2 if plan.selected_diff_backup else 1):]
    plan.restore_room = _check_restore_room(plan.restore_config, plan.candidate, plan.selected_diff_backup,
                                            logger=plan.logger)


def _kept(plan: DatabasePlan, step: Any, at: list[int]) -> dict[str, object]:
    try:
        outcome = step()
    except Exception as exc:
        if plan.chain is not None:
            plan.chain.mark(at, "failed", f"{type(exc).__name__}: {exc}"[:2000])
        raise
    if plan.chain is not None:
        plan.chain.mark(at, "applied")
    plan.steps.append(outcome)
    return outcome


def apply_full(plan: DatabasePlan) -> dict[str, object]:
    return _kept(plan, lambda: run_restore_full(config=plan.restore_config, candidate=plan.candidate,
                                                logger=plan.logger), plan.at_full)


def apply_diff(plan: DatabasePlan) -> dict[str, object]:
    return _kept(plan, lambda: run_restore_diff(config=plan.restore_config, candidate=plan.candidate,
                                                selected_backup=plan.selected_diff_backup, logger=plan.logger),
                 plan.at_diff)


class LogChain:
    """A database's logs, each applied as the caller asks for the next: ``each()`` yields what became of
    every file - ``(index, file, result, reason)``, ``applied``, ``skipped`` or ``failed`` - as it is
    known. A Msg 4305 refusal is decided only by what follows it (skipped when a later log goes in, failed
    when none does), so it is yielded when that is known; a failure raises after its file is yielded."""

    def __init__(self, plan: DatabasePlan) -> None:
        self.plan = plan
        self.outcome: dict[str, object] | None = None

    def each(self):
        plan = self.plan
        steps = restore_log_steps(config=plan.restore_config, candidate=plan.candidate,
                                  selected_backups=plan.selected_log_backups, logger=plan.logger,
                                  stopat_utc=plan.point_in_time_utc, stopat_source=plan.stopat_source)
        while True:
            try:
                index, result, reason = next(steps)
            except StopIteration as done:
                self.outcome = done.value
                plan.steps.append(done.value)
                return
            if plan.chain is not None and 0 < index <= len(plan.at_logs):
                plan.chain.mark([plan.at_logs[index - 1]], result, reason)
            yield index, plan.selected_log_backups[index - 1], result, reason


def finish_database(plan: DatabasePlan) -> dict[str, object]:
    """Recovery, the recovery model, the check - then the database's history row closed and its answer."""
    restore_config, candidate, logger = plan.restore_config, plan.candidate, plan.logger
    point_in_time_utc, steps = plan.point_in_time_utc, plan.steps
    if point_in_time_utc is not None:
        # The final LOG restore normally recovers the DB (WITH RECOVERY + STOPAT). If that log was
        # skipped (Msg 4305 stray/non-contiguous log) or the chain ended exactly at the target, the DB
        # is still RESTORING -- finalize it so SET RECOVERY FULL does not fail with Msg 5052. No-op if online.
        recovery_result = run_restore_recovery_if_restoring(config=restore_config, candidate=candidate, logger=logger)
    else:
        recovery_result = run_restore_recovery(config=restore_config, candidate=candidate, logger=logger)
    steps.append(recovery_result)
    recovery_model_result = run_set_recovery_model_full(config=restore_config, candidate=candidate, logger=logger)
    steps.append(recovery_model_result)
    if restore_config.checkdb:
        try:
            checkdb_result = run_restore_checkdb(config=restore_config, candidate=candidate, logger=logger)
        except Exception as exc:
            # Its own type, so the caller can say "restored and recovered, check failed" rather
            # than "restore failed": the data IS there. The restore still counts as failed.
            raise IntegrityCheckFailed(str(exc)) from exc
    else:
        checkdb_result = {"step": "dbcc-checkdb", "status": "SKIPPED", "reason": CHECKDB_OFF_REASON}
        _emit_restore_log(
            logger,
            "restore-db dbcc-checkdb skipped "
            + _format_metadata(restore_id=restore_config.restore_id or None,
                               database=candidate.source_database_name,
                               target_database=candidate.restore_database_name,
                               reason="checkdb_false_on_the_entry"),
        )
    steps.append(checkdb_result)
    history, restore_id, start_monotonic = plan.history, plan.restore_id, plan.start_monotonic
    selected_diff_backup, selected_log_backups = plan.selected_diff_backup, plan.selected_log_backups
    restore_mode, skipped_backups, expect = plan.restore_mode, plan.skipped_backups, plan.expect
    restore_room, certificate_result = plan.restore_room, plan.certificate_result
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
        "expect": expect,
        "final_recovery_status": recovery_result["status"],
        "final_recovery_model_status": recovery_model_result["status"],
        "checkdb_status": checkdb_result["status"],
        "backup_file_unc": str(candidate.backup_file_unc),
        "backup_file_on_vm": str(candidate.backup_file_on_vm),
        "restore_room": restore_room,
        "duration_seconds": duration_seconds,
        "stdout": "\n".join(str(step.get("stdout", "")).strip() for step in steps if step.get("stdout")).strip(),
        "stderr": "\n".join(str(step.get("stderr", "")).strip() for step in steps if step.get("stderr")).strip(),
        "steps": steps,
        "certificate": certificate_result,
    }



def fail_database(plan: DatabasePlan, exc: BaseException) -> None:
    """A database that failed: its history row closed FAILED, said in the restore log."""
    if plan.history is not None and plan.restore_id is not None:
        plan.history.finish_restore(
            restore_id=plan.restore_id,
            status="FAILED",
            duration_seconds=round(time.monotonic() - plan.start_monotonic, 3),
            error_message=sanitize_text(str(exc)),
        )
    _emit_restore_log(
        plan.logger,
        "restore-db failed "
        + _format_metadata(restore_id=plan.restore_config.restore_id or None,
                           database=plan.candidate.source_database_name,
                           target_database=plan.candidate.restore_database_name, error=str(exc)),
        level="critical",
    )


def run_restore_database(
    *,
    config: BackupRestoreConfig | None = None,
    dbabrain_config: DbaBrainConfig | None = None,
    database: DatabaseRestoreMapping | str | None = None,
    backup_file: str | Path | None = None,
    dry_run: bool = False,
    ensure_certificate: bool = True,
    ensure_credential: bool = True,
    logger: object | None = None,
    point_in_time_utc: datetime.datetime | None = None,
    chain: ChainRecord | None = None,
    on_phase: "Callable[..., None] | None" = None,
) -> dict[str, object]:
    """One database restored through its steps - the workflow's run of them with no one told
    (``workflow.restore_share_database``). ``on_phase`` is accepted and not called: nothing under the
    workflow says a step (0.40.0, 1.375)."""
    from dbabrain.backup_restore.workflow import restore_share_database

    restore_config = config or load_restore_config()
    app_config = dbabrain_config or load_config()
    if ensure_credential:
        ensure_vm_share_credential(restore_config)
    certificate_result = None
    if ensure_certificate:
        certificate_result = ensure_source_certificate_quietly(config=restore_config, dry_run=dry_run, logger=logger)
    return restore_share_database(config=restore_config, app_config=app_config, database=database,
                                  backup_file=backup_file, dry_run=dry_run, logger=logger,
                                  point_in_time_utc=point_in_time_utc, chain=chain,
                                  certificate_result=certificate_result, say=None)

def _read_chain_headers(config: BackupRestoreConfig, candidate: RestoreCandidate, paths: list[str],
                        *, logger: object | None) -> str:
    """The target's ``sqlcmd`` output for one batch reading every chain file's header."""
    from dbabrain.backup_restore import restore_chain_check

    result = run_sqlcmd_query_command(
        build_sqlcmd_query_command(sql=restore_chain_check.build_header_sql(paths), config=config),
        config=config,
        logger=logger,
        progress_step="backup-chain-check",
        progress_database=candidate.source_database_name,
        restore_id=config.restore_id,
        allow_transient_retry=True,
    )
    return result.stdout or ""


def _check_backup_chain(config: BackupRestoreConfig, candidate: RestoreCandidate, diff: Path | str | None,
                        logs: list, *, logger: object | None, restore_mode: str) -> list[dict[str, object]] | None:
    """Refuse a chain whose LSNs do not join, naming the piece; a chain that cannot be read is
    logged as not judged and restored as before (``restore_chain_check``). Answers the headers it
    judged by, full first (each with ``finished_at``, the source's clock), or ``None``."""
    from dbabrain.backup_restore import restore_chain_check

    def on_target(path) -> str:
        return _target_path_str(vm_unc_to_local_path(Path(str(path)), config), config)

    full = _target_path_str(Path(str(candidate.backup_file_on_vm)), config)
    diff_path = on_target(diff) if diff else None
    log_paths = [on_target(item) for item in logs]
    paths = [full, *([diff_path] if diff_path else []), *log_paths]
    meta = {"restore_id": config.restore_id or None, "target_id": config.target_id,
            "database": candidate.source_database_name, "restore_mode": restore_mode, "files": len(paths)}
    try:
        stdout = _read_chain_headers(config, candidate, paths, logger=logger)
        broken, unjudged, heads = restore_chain_check.read_chain(full, diff_path, log_paths, stdout)
    except errors.NotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001 - a check that cannot run is not judged, never a refusal
        broken, unjudged, heads = "", f"the headers could not be read: {exc}", None
    if unjudged:
        _emit_restore_log(logger, "restore-db backup-chain not-judged "
                          + _format_metadata(**meta, reason=unjudged), level="warning")
        return None
    if broken:
        _emit_restore_log(logger, "restore-db backup-chain refused "
                          + _format_metadata(**meta, reason=broken), level="critical")
        raise errors.Refused(f"{candidate.source_database_name}: the backup chain is broken - {broken}. "
                             "Nothing was restored.")
    _emit_restore_log(logger, "restore-db backup-chain checked " + _format_metadata(**meta))
    return heads


def _check_restore_room(config: BackupRestoreConfig, candidate: RestoreCandidate,
                        selected_diff_backup: Path | None, *,
                        logger: object | None = None) -> dict[str, object]:
    """The room this database's restore needs, asked of the target before its first RESTORE.

    The rule and the batch are :mod:`dbabrain.backup_restore.space`'s; what is this module's is the
    channel - the ``sqlcmd`` the restore itself is about to use - and the paths as the target
    spells them. A measured shortfall raises, and is this database's failure like any other step's.
    """
    from dbabrain.backup_restore import space

    backups = [_target_path_str(Path(str(candidate.backup_file_on_vm)), config)]
    if selected_diff_backup:
        backups.append(_target_path_str(vm_unc_to_local_path(selected_diff_backup, config), config))

    def run_sql(sql: str) -> str:
        # One attempt: a measurement that did not answer is reported as not made, and the RESTORE
        # that follows is the step that retries a connection.
        return run_sqlcmd_query_command(
            build_sqlcmd_query_command(sql=sql, config=config),
            config=config, logger=logger, progress_step="restore-room",
            progress_database=candidate.source_database_name, restore_id=config.restore_id,
            allow_transient_retry=False,
        ).stdout

    return space.check_restore_room(
        config,
        database=candidate.restore_database_name,
        backups=backups,
        data_path=_target_path_str(candidate.restore_data_file_on_vm, config),
        log_path=_target_path_str(candidate.restore_log_file_on_vm, config),
        run_sql=run_sql,
        log=lambda message: _emit_restore_log(logger, "restore-db " + message),
    )


def run_restore_full(*, config: BackupRestoreConfig, candidate: RestoreCandidate, logger: object | None = None) -> dict[str, object]:
    return _run_restore_step(
        step_name="restore-full",
        config=config,
        restore=("full", _restore_step("full", candidate, config, path=candidate.backup_file_on_vm)),
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
            restore=("diff", _restore_step("diff", candidate, config,
                                           path=vm_unc_to_local_path(selected_backup, config))),
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


def restore_log_steps(
    *,
    config: BackupRestoreConfig,
    candidate: RestoreCandidate,
    selected_backups: list[Path] | None = None,
    logger: object | None = None,
    stopat_utc: datetime.datetime | None = None,
    stopat_source: str | None = None,
):
    """Each selected log, in order - a generator: each ``next()`` applies as far as the next file whose
    fate is known and yields ``(index, result, reason)`` - ``applied``, ``skipped`` or ``failed`` (the
    chain record of 1.367). Its return value is the step's answer. A generator so the workflow calls each
    log and says it as it goes in (0.40.0, 1.375); :func:`run_restore_log` runs it to its end.

    Two refusals are passed over. **Msg 4326** - the log ends before the database's restore point, a
    log taken the same minute as, just before, the full: it holds nothing the database needs, and is
    ``skipped`` at once. **Msg 4305** - the log begins AFTER the restore point: either a file out of
    the chain (a later log then goes in, and the file is ``skipped``) or a log is MISSING before it.
    Until 0.39.0 every 4305 was passed over and the database still came out SUCCESS when one log had
    gone in, so behind a real gap every later log was refused, passed over, and the database was
    recovered at the gap - hours short of what was asked, and said done (read 2026-11-14 for 1.367;
    the chain check before the restore catches a gap only when it can read the headers). Now a 4305
    after which no later log of the database goes in **fails the database**, naming the log: it did
    not reach what it was asked to.
    """
    selected_backups = selected_backups or []

    if selected_backups:
        results = []
        skipped_4305: list[str] = []
        # 4305 refusals no later log has gone in after yet: a file out of the chain, or the gap.
        pending_4305: list[tuple[int, str]] = []
        total = len(selected_backups)
        for index, backup in enumerate(selected_backups, start=1):
            backup_on_vm = vm_unc_to_local_path(backup, config)
            is_last_with_stopat = index == total and stopat_utc is not None
            # The last log of a point-in-time restore stops inside itself and recovers; every other
            # stays NORECOVERY, and a latest restore recovers in its own step after the chain.
            step = ("log", _restore_step("log", candidate, config, path=backup_on_vm,
                                         recovery=is_last_with_stopat,
                                         stopat_utc=stopat_utc if is_last_with_stopat else None,
                                         stopat_source=stopat_source if is_last_with_stopat else None))
            metadata: dict[str, object] = {"sequence": index, "total": total, "file": str(backup_on_vm)}
            if is_last_with_stopat:
                metadata["stopat_utc"] = stopat_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
            try:
                result = _run_restore_step(
                    step_name="restore-log",
                    config=config,
                    restore=step,
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
                    yield (index, "failed", f"restore timed out and the target could not confirm the log went in: {exc}"[:2000])
                    raise errors.Refused(
                        f"reason=restore_timeout_resume_unsafe database={candidate.restore_database_name} "
                        f"backup={backup_on_vm} last_confirmed_log={resume_result['last_confirmed_log']}: {exc}"
                    ) from exc
            except Exception as exc:  # noqa: BLE001 - inspect the SQL error to decide skip vs. fail.
                if _is_sqlserver_msg_4305(str(exc)):
                    skipped_4305.append(str(backup_on_vm))
                    pending_4305.append((index, str(backup_on_vm)))
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
                    yield (index, "skipped", "Msg 4326: the log ends before the database's restore point - "
                                            "it holds nothing the database needs")
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
                yield (index, "failed", f"{type(exc).__name__}: {exc}"[:2000])
                raise
            results.append(result)
            yield (index, "applied", "")
            # A later log went in: the 4305 refusals before it were files out of the chain, not a gap.
            for refused_index, _refused in pending_4305:
                yield (refused_index, "skipped", "Msg 4305: the log begins after the database's restore point, "
                                                "and a later log of the chain went in - a file out of the chain")
            pending_4305.clear()
        if pending_4305:
            # Refused as too recent, and nothing went in after: a log is missing before the first of them.
            first_index, first = pending_4305[0]
            for refused_index, _refused in pending_4305:
                yield (refused_index, "failed",
                      "Msg 4305: the log begins after the database's restore point and no later log went in - "
                      f"a log is missing before {PureWindowsPath(first).name if chr(92) in first else PurePosixPath(first).name}")
            applied_text = f"{len(results)} log(s) applied before it" if results else "no log applied"
            raise errors.OperationFailed(
                "missing required earlier LOG backup / LSN gap: "
                f"database={candidate.restore_database_name} - {first} and the {len(pending_4305) - 1} log(s) after it "
                f"were refused as too recent to apply (Msg 4305) and no later log went in; {applied_text}. "
                "The database would stop at the gap, short of what was asked - it is not recovered as restored. "
                f"skipped_msg_4305={', '.join(skipped_4305)}"
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



def run_restore_log(
    *,
    config: BackupRestoreConfig,
    candidate: RestoreCandidate,
    selected_backups: list[Path] | None = None,
    logger: object | None = None,
    stopat_utc: datetime.datetime | None = None,
    on_file: "Callable[[int, str, str], None] | None" = None,
    stopat_source: str | None = None,
) -> dict[str, object]:
    """Every selected log, run to its end; ``on_file(index, result, reason)`` is told what became of each."""
    steps = restore_log_steps(config=config, candidate=candidate, selected_backups=selected_backups, logger=logger,
                              stopat_utc=stopat_utc, stopat_source=stopat_source)
    while True:
        try:
            index, result, reason = next(steps)
        except StopIteration as done:
            return done.value
        if on_file is not None:
            try:
                on_file(index, result, reason)
            except Exception:  # noqa: BLE001 - the record is not the restore
                pass

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


#: Why the check did not run, where a step result or a log line says so.
CHECKDB_OFF_REASON = "switched off on this restore entry (checkdb: false)"

#: A database restored and recovered whose `DBCC CHECKDB` failed. Not `FAILED`: the data is there,
#: and "restore failed" sent the reader to the backups and the copy, which were fine (2026-09-24,
#: Msg 1823 / 7928 on a container target). It still fails the run.
CHECK_FAILED = "CHECK_FAILED"


class IntegrityCheckFailed(errors.OperationFailed):
    """`DBCC CHECKDB` failed on a database that was restored and recovered."""


def _failed_status(exc: BaseException) -> str:
    return CHECK_FAILED if isinstance(exc, IntegrityCheckFailed) else "FAILED"


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
    sql: str = "",
    restore: tuple[str, dict[str, object]] | None = None,
    logger: object | None,
    candidate: RestoreCandidate,
    metadata: dict[str, object],
) -> dict[str, object]:
    """One statement of the restore, through ``common.cli``: a RESTORE of a file as
    ``restore-<level>`` (``restore``), the recovery, recovery model and CHECKDB as ``run-sqlcmd``
    (``sql``). Either way the answer is read here, the same way - retries, a Msg 4305 log, a lost
    connection mid-``RESTORE LOG`` are this app's decisions."""
    _emit_restore_log(
        logger,
        f"restore-db {step_name} start "
        + _format_metadata(restore_id=config.restore_id or None, database=candidate.source_database_name, target_database=candidate.restore_database_name, **metadata),
    )
    cmd = (_restore_step_command(restore, config=config) if restore is not None
           else build_sqlcmd_query_command(sql=sql, config=config))
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
            "sql": "\n".join(getattr(result, "statements", None) or []) or sql,
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
    dbabrain_config: DbaBrainConfig | None = None,
    dry_run: bool = False,
    logger: object | None = None,
    point_in_time_utc: datetime.datetime | None = None,
    chain: ChainRecord | None = None,
    on_phase: "Callable[..., None] | None" = None,
) -> dict[str, object]:
    """Every database of one entry restored through its steps - the workflow's run of them with no one
    told (``workflow.restore_share_server``). ``on_phase`` is accepted and not called (0.40.0, 1.375)."""
    from dbabrain.backup_restore.workflow import restore_share_server

    return restore_share_server(config=config or load_restore_config(), dbabrain_config=dbabrain_config or load_config(),
                                dry_run=dry_run, logger=logger, point_in_time_utc=point_in_time_utc, chain=chain,
                                say=None)


def server_databases(restore_config: BackupRestoreConfig,
                     point_in_time_utc: datetime.datetime | None) -> list[tuple[Any, Any, str]]:
    """The databases one entry restores - ``(mapping, None, label)`` for each it names, or ``(None, full
    backup, label)`` for each FULL its share holds when it names none."""
    if restore_config.databases:
        return [(database, None, str(getattr(database, "target_database", None)
                                     or getattr(database, "source_database", None) or database))
                for database in restore_config.databases]
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
    return [(None, backup, backup.parent.parent.name) for backup in backups]


def server_result(restore_config: BackupRestoreConfig, results: list[dict[str, object]], *,
                  certificate_result: Any, dry_run: bool, start_monotonic: float,
                  logger: object | None = None) -> dict[str, object]:
    """One entry's answer from its databases' - its verdict never greener than they are."""
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
        # What each failed database said, so a message can name the step and the SQL Server
        # message number instead of "restore failed" for a database that was restored.
        "per_database_error": {
            str(result.get("database_name")): str(result.get("error") or "")
            for result in results
            if isinstance(result, dict) and result.get("database_name") and result.get("error")
        },
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
    dbabrain_config: DbaBrainConfig | None = None,
    dry_run: bool = False,
    logger: object | None = None,
    point_in_time_utc: datetime.datetime | None = None,
    chain: ChainRecord | None = None,
    on_phase: "Callable[..., None] | None" = None,
) -> dict[str, object]:
    return run_restore_server(config=config, dbabrain_config=dbabrain_config, dry_run=dry_run, logger=logger,
                              point_in_time_utc=point_in_time_utc, chain=chain, on_phase=on_phase)


def ensure_source_certificate_quietly(*, config: BackupRestoreConfig, dry_run: bool = False,
                                     logger: object | None = None) -> dict[str, object]:
    """The source's certificate made ready on the target - its log lines written, no message sent: the
    workflow says the certificate step (0.40.0, 1.375). ``{"status": "none"}`` when the entry names no
    certificate source - the backup is not encrypted."""
    from dbabrain.backup_restore.certificate import ensure_source_certificate, has_certificate_source

    if not has_certificate_source(config):
        result = ensure_source_certificate(config=config, dry_run=dry_run, logger=logger)
        _emit_restore_log(logger, "restore-instance certificate skipped " + _format_metadata(
            restore_id=config.restore_id or None, source_id=config.source_id, target_id=config.target_id,
            reason="no_certificate_api"))
        return {**(result or {}), "none": True}
    _emit_restore_log(logger, "restore-instance certificate start " + _format_metadata(
        restore_id=config.restore_id or None, source_id=config.source_id, target_id=config.target_id,
        certificate_name="unknown"))
    try:
        result = ensure_source_certificate(config=config, dry_run=dry_run, logger=logger)
    except Exception as exc:
        _emit_restore_log(logger, "restore-instance certificate failed " + _format_metadata(
            restore_id=config.restore_id or None, source_id=config.source_id, target_id=config.target_id,
            certificate_name="unknown", error=str(exc)), level="critical")
        raise
    _emit_restore_log(logger, "restore-instance certificate success " + _format_metadata(
        restore_id=config.restore_id or None, source_id=config.source_id, target_id=config.target_id,
        certificate_name=result.get("certificate_name") or "unknown", thumbprint=result.get("thumbprint"),
        status=result.get("status")))
    return result


def ensure_source_certificate_with_events(
    *,
    config: BackupRestoreConfig,
    app_config: DbaBrainConfig,
    dry_run: bool = False,
    logger: object | None = None,
) -> dict[str, object]:
    from dbabrain.backup_restore.certificate import ensure_source_certificate, has_certificate_source

    if not has_certificate_source(config):
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
        "certificate_from_backups": config.backup_certificate is not None,
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
    request = share_login_request(
        credential_target=config.vm_credential_target,
        username=config.vm_username,
        password_env=config.vm_password_env,
    )
    if request:
        store_share_logins([request])


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
    N'DBABRAIN_RESUME|'
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
        (line.strip() for line in result.stdout.splitlines() if "DBABRAIN_RESUME|" in line),
        "",
    )
    parts = marker[marker.find("DBABRAIN_RESUME|"):].split("|", 6) if marker else []
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
