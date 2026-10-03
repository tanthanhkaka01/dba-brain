from __future__ import annotations
from db_ops.lib import errors
from db_ops.backup_restore.shell_quoting import _BACKUP_TIMESTAMP_RE, backup_time_from_name, _build_sqlcmd_auth_args, _escape_identifier, _escape_sql_string, _ps_array, _ps_quote  # noqa: F401 - one definition
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
from db_ops.backup_restore.copy_backup import open_ssh_connection, resolve_password_ref, share_login_request, store_share_logins
from db_ops.lib import powershell
from db_ops.lib.shell import is_powershell_executable, powershell_executable
from db_ops.backup_restore.events import emit_backup_restore_event
from db_ops.backup_restore.history import BackupRestoreHistory
from db_ops.backup_restore.sanitize import compact_log_value, sanitize_text, sanitize_value
from db_ops.lib.config import DbOpsConfig, load_config
from db_ops.db.store import utc_now_text
from db_ops.logging_ops import log_event
from db_ops.backup_restore.restore_base import (  # noqa: F401 - re-exported: every name kept its address
    RestoreCandidate,
    _emit_restore_log,
    _format_metadata,
    _normalize_restore_path,
)
from db_ops.backup_restore.restore_find import (  # noqa: F401 - re-exported: every name kept its address
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
from db_ops.backup_restore.restore_sql import (  # noqa: F401 - re-exported: every name kept its address
    build_checkdb_sql,
    build_recovery_if_restoring_sql,
    build_recovery_sql,
    build_set_recovery_model_full_sql,
    composed_restore_sql,
)
from db_ops.backup_restore.restore_sqlcmd import (  # noqa: F401 - re-exported: every name kept its address
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
                  stopat_utc: datetime.datetime | None = None) -> dict[str, object]:
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
        fields["overwrite_existing"] = bool(config.overwrite_existing)
        # The logical names are read on the server (RESTORE FILELISTONLY), as they always were;
        # only where the two files go is this app's to say.
        fields["move_files"] = {
            "data": _target_path_str(candidate.restore_data_file_on_vm, config),
            "log": _target_path_str(candidate.restore_log_file_on_vm, config),
        }
    if stopat_utc is not None:
        fields["stopat"] = stopat_utc.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return fields


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

    history = BackupRestoreHistory.from_config(app_config)
    started_at = utc_now_text()
    start_monotonic = time.monotonic()
    restore_id = history.start_restore(
        database_name=candidate.restore_database_name,
        backup_file=str(candidate.backup_file_on_vm),
        restore_start=started_at,
    )
    try:
        # Before the first RESTORE: do the files this backup holds fit where they go (§1.76).
        restore_room = _check_restore_room(restore_config, candidate, selected_diff_backup, logger=logger)
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
        "restore_room": restore_room,
        "duration_seconds": duration_seconds,
        "stdout": "\n".join(str(step.get("stdout", "")).strip() for step in steps if step.get("stdout")).strip(),
        "stderr": "\n".join(str(step.get("stderr", "")).strip() for step in steps if step.get("stderr")).strip(),
        "steps": steps,
        "certificate": certificate_result,
    }


def _check_restore_room(config: BackupRestoreConfig, candidate: RestoreCandidate,
                        selected_diff_backup: Path | None, *,
                        logger: object | None = None) -> dict[str, object]:
    """The room this database's restore needs, asked of the target before its first RESTORE.

    The rule and the batch are :mod:`db_ops.backup_restore.space`'s; what is this module's is the
    channel - the ``sqlcmd`` the restore itself is about to use - and the paths as the target
    spells them. A measured shortfall raises, and is this database's failure like any other step's.
    """
    from db_ops.backup_restore import space

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
            # The last log of a point-in-time restore stops inside itself and recovers; every other
            # stays NORECOVERY, and a latest restore recovers in its own step after the chain.
            step = ("log", _restore_step("log", candidate, config, path=backup_on_vm,
                                         recovery=is_last_with_stopat,
                                         stopat_utc=stopat_utc if is_last_with_stopat else None))
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
                    raise errors.Refused(
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
            raise errors.OperationFailed(
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
                results.append({"database_name": db_label, "status": _failed_status(exc),
                                "error": str(exc)[:500]})
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
                results.append({"database_name": db_label, "status": _failed_status(exc),
                                "error": str(exc)[:500]})
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
    from db_ops.backup_restore.certificate import ensure_source_certificate, has_certificate_source

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
