"""The combined backup -> restore workflow.

Backup and restore used to be two separately scheduled app commands with two different shapes:
backup entries had a ``backup_id``, a ``time_window`` and a run row per job, while restore
entries had none of that - the scheduler ran *every* active restore on its own fixed interval and
kept no per-entry state. That made the two halves of the same recovery story behave differently
and impossible to reason about together.

They are one command now. A restore entry is scheduled exactly like a backup job: it has a
``time_window``, its due check is :func:`db_ops.backup_restore.schedule.is_due`, and every run is
written to ``job_runs`` under its own ``restore_id`` - so a restore that fails backs off on
``retry_interval`` and one that is still running is never started twice.

Order matters: backup runs first. A restore drill validates the backups, so validating the set
taken moments ago is the point; running them the other way round would test yesterday's.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from db_ops.backup_restore import schedule
from db_ops.backup_restore.backup import run_backup
from db_ops.backup_restore.config import BackupRestoreConfig, load_restore_configs
from db_ops.backup_restore.restore_script import (
    load_script_restores,
    run_script_restore,
    select_due_script_restores,
)
from db_ops.backup_restore.events import emit_backup_restore_event, stdout_excerpt
from db_ops.backup_restore.server_metadata import replay_phase
from db_ops.lib import instance_bundle
from db_ops.db.store import DbOpsStore, utc_now_text


def select_due_restores(
    *,
    configs: list[BackupRestoreConfig],
    latest_runs: dict[str, Any],
    now: datetime | None = None,
    local_now: datetime | None = None,
) -> list[BackupRestoreConfig]:
    """The due subset, by the same rule backup jobs use."""
    return [
        config for config in configs
        if config.active and config.restore_id and schedule.is_due(
            job_code=schedule.restore_job_code(config.restore_id),
            time_window=config.time_window,
            latest_runs=latest_runs,
            now=now,
            local_now=local_now,
        )
    ]


def script_restore_metadata(job: Any) -> dict[str, Any]:
    """Event metadata for a script-driven restore, in the keys the shared formatter reads.

    ``source_id`` / ``target_id`` / ``target_host`` are what ``events.py`` renders as the
    ``target=`` line, and the SQL Server engine branch has always published them. This branch
    published only ``server_id`` and ``target_container``, so every script-driven restore —
    Oracle, PostgreSQL, and the container-to-container SQL Server drill — arrived in Telegram
    with no target named at all, while an engine restore of the same database type showed one.
    The engine-specific keys are kept as well: they are what ``job_runs.metadata_json`` is read
    by, and a container name is not a host name.

    An in-place drill declares no ``target_server_id``; there the target host *is* the source.
    """
    return {
        "restore_id": job.restore_id,
        "db_type": job.db_type,
        "server_id": job.server_id,
        "target_container": job.target_container,
        "source_id": job.server_id,
        "target_id": job.target_server_id or job.server_id,
        "target_host": job.target_container,
    }


def replay_engine_phase(
    config: BackupRestoreConfig, *, phase: str, announce: Any = None,
) -> tuple[str, dict[str, Any]]:
    """One metadata phase for an engine-path entry. Returns (``PHASE=`` lines, reports by phase).

    The engine path restores through an SMB share and a ``sqlcmd`` connection, so unlike the
    script path it has no ``target_container`` to identify the instance with - what it has is the
    target host's ip, in ``vm_credential_target``. That is the whole adaptation; the decision
    itself, the ordering rule and the failure policy are shared.
    """
    plan = getattr(config, "server_metadata", None)
    if plan is None or not getattr(plan, "enabled", False):
        return "", {}
    line, report = replay_phase(
        plan,
        phase=phase,
        label=config.restore_id or config.source_id,
        source_server_id=config.source_id,
        target_server_id=config.target_id,
        target_host=config.vm_credential_target,
        announce=announce,
    )
    return line, ({phase: report} if report else {})


def run_scheduled_restores(
    *,
    app_config: Any,
    config_path: str,
    logger: Any = None,
    restore_id: str | None = None,
    copy_hours: int = 24,
    delete_hours: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    key: str | None = None,
    key_base64: str | None = None,
) -> dict[str, Any]:
    """Run every restore entry that is due, recording each under its own restore_id."""
    # Imported here: run_restore_workflow lives in the CLI module, which imports this one.
    from db_ops.backup_restore.cli import run_restore_workflow

    configs = load_restore_configs(config_path)
    # Both kinds are loaded before --restore-id is judged: a script-driven entry (Oracle,
    # PostgreSQL) is absent from the SQL Server list, so filtering that list alone would report
    # a perfectly valid restore_id as missing.
    script_jobs = load_script_restores(config_path)
    if restore_id:
        configs = [item for item in configs if item.restore_id == restore_id]
        script_jobs = [item for item in script_jobs if item.restore_id == restore_id]
        if not configs and not script_jobs:
            raise ValueError(f"No backup_restore entry found with restore_id={restore_id}.")

    store = DbOpsStore.from_config(app_config)
    # Close and report any run abandoned at RUNNING past its timeout before choosing what to
    # run now: a run that died without raising reports nothing on its own, and the operator
    # should hear about it in the same cycle that notices, not the next time someone reads
    # the table. Every entry is considered, not just the due ones.
    stale = schedule.reap_stale_runs(
        store=store, app_config=app_config, logger=logger,
        timeouts={
            **{schedule.restore_job_code(c.restore_id): c.time_window.timeout
               for c in configs if c.restore_id},
            **{job.job_code: job.time_window.timeout for job in script_jobs},
        },
        notify={
            **{schedule.restore_job_code(c.restore_id): c.notify for c in configs if c.restore_id},
            **{job.job_code: job.notify for job in script_jobs},
        },
    )
    latest_runs = store.fetch_latest_job_runs_by_job_code()
    # --force skips the schedule, never a restore already in flight - two restores into one
    # database is exactly what the RUNNING row is there to stop.
    due = ([c for c in configs
            if c.active and not schedule.is_running(schedule.restore_job_code(c.restore_id), latest_runs)]
           if force else select_due_restores(configs=configs, latest_runs=latest_runs))

    summary: dict[str, Any] = {
        "configured": len(configs),
        "due": len(due),
        "ran": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": len(configs) - len(due),
        "dry_run": bool(dry_run),
        "timed_out": stale,
        "restores": [],
    }

    for config in due:
        if dry_run:
            summary["restores"].append({"restore_id": config.restore_id, "status": "dry-run"})
            continue
        summary["ran"] += 1
        job_code = schedule.restore_job_code(config.restore_id)
        metadata = {"restore_id": config.restore_id, "source_id": config.source_id, "target_id": config.target_id}
        log_id, _started_at = schedule.start_run(
            store=store, job_code=job_code,
            message=f"Restore {config.restore_id} started.", metadata=metadata,
        )
        started = time.monotonic()
        started_at = utc_now_text()
        # This layer emits the run's events. It used to assume run_restore_workflow did - it does
        # not: those START/END events live in the CLI's main(), and the ``workflow`` subcommand
        # returns before reaching them, so nothing reported a scheduled restore at all. The one
        # message that did arrive came from the certificate sub-step deep inside
        # restore_database.py, which is why a restore that finished in 16 minutes looked to the
        # operator like it had hung at "certificate import finished" for hours.
        emit_backup_restore_event(
            app_config=app_config, command="restore-workflow", phase="START", level="logging",
            message=f"Restore {config.restore_id} started.", logger=logger,
            started_at=started_at, metadata=metadata, notify=config.notify,
        )

        def announce(event: str, message: str, extra: dict[str, Any] | None = None) -> None:
            emit_backup_restore_event(
                app_config=app_config, command="restore-workflow", phase=event, level="logging",
                message=message, logger=logger, started_at=utc_now_text(),
                metadata={**metadata, **(extra or {})}, notify=config.notify,
            )

        # The same two phases the script path runs, from the same shared function - see
        # server_metadata.replay_phase. This path names its target by ip rather than by
        # container, which is the only difference and the reason the arguments differ here.
        metadata_lines, phase_reports = replay_engine_phase(
            config, phase=instance_bundle.PRE_DATABASE, announce=announce,
        )
        try:
            output = run_restore_workflow(
                restore_configs=[config],
                app_config=app_config,
                copy_hours=copy_hours,
                delete_hours=delete_hours,
                dry_run=False,
                force=force,
                logger=logger,
                # The engine path copies inside run_restore_workflow, so this is where its copy
                # boundaries come from. The script path announces them from restore_by_id. Both
                # now emit the same two events, which is the point.
                on_phase=announce,
            )
            # Returning without raising is NOT the same as the restore having worked. A database
            # that fails is caught per-database on purpose, so the remaining ones still run - which
            # means the only signal that something was lost is the engine's own verdict. Trusting
            # "it did not raise" is what sent a ✅ "status=done" for the 2026-08-08 02:00 drill that
            # never restored APPDB_Prod and left it unreachable in RESTORING.
            failed_databases = sorted(
                name for name, db_status in (output.get("per_database_restore_status") or {}).items()
                if str(db_status) not in {"SUCCESS", "SUCCESS_RESUMED", "DRY_RUN"}
            )
            if str(output.get("status", "")) in {"SUCCESS", "DRY_RUN"} and not failed_databases:
                status = "done"
                error_text = None
            else:
                status = "error"
                error_text = (
                    f"restore reported {output.get('status')!s}"
                    + (f"; databases not restored: {', '.join(failed_databases)}"
                       if failed_databases else "")
                )[-2000:]
        except Exception as exc:  # noqa: BLE001 - one restore must not stop the others.
            output = {"status": "ERROR", "error": str(exc)}
            status = "error"
            error_text = str(exc)[-2000:]

        # Only after a restore that worked: Agent job steps name databases that have to exist.
        if status == "done":
            post_lines, post_reports = replay_engine_phase(
                config, phase=instance_bundle.POST_DATABASE, announce=announce,
            )
            metadata_lines += post_lines
            phase_reports.update(post_reports)

        duration_ms = int((time.monotonic() - started) * 1000)
        # "status" is the key the event formatter renders as `status=...`. Publishing only
        # "output_status" is why every "Restore workflow finished." message ever sent ended in a
        # bare `status=` - the formatter looked for output.status, then status, and this branch
        # supplied neither. output_status stays: it is the engine's own verdict, which is not
        # always the same thing as whether the run completed.
        end_metadata = {**metadata, "duration_ms": duration_ms,
                        "status": status,
                        "output_status": str(output.get("status", ""))}
        if phase_reports:
            end_metadata["server_metadata"] = phase_reports
            end_metadata["stdout_tail"] = metadata_lines.strip()
        end_message = f"Restore {config.restore_id} finished: {status}."
        schedule.finish_run(
            store=store, log_id=log_id, status=status,
            message=end_message,
            duration_ms=duration_ms, error_text=error_text,
            metadata=end_metadata,
        )
        emit_backup_restore_event(
            app_config=app_config, command="restore-workflow",
            phase="END" if status == "done" else "ERROR",
            level="logging" if status == "done" else "error",
            message=end_message, logger=logger, started_at=started_at,
            finished_at=utc_now_text(), duration_ms=duration_ms, error_text=error_text,
            metadata=end_metadata, notify=config.notify,
        )
        summary["succeeded" if status == "done" else "failed"] += 1
        summary["restores"].append({
            "restore_id": config.restore_id, "status": status,
            "duration_ms": duration_ms, "output_status": str(output.get("status", "")),
        })

    # Script-driven engines (Oracle, PostgreSQL) share this scheduler and these run rows; only
    # the execution differs, so they are appended to the same summary.
    summary["configured"] += len(script_jobs)
    script_due = ([j for j in script_jobs
                   if j.active and not schedule.is_running(j.job_code, latest_runs)]
                  if force else select_due_script_restores(jobs=script_jobs, latest_runs=latest_runs))
    summary["due"] += len(script_due)
    summary["skipped"] += len(script_jobs) - len(script_due)

    for job in script_due:
        if dry_run:
            summary["restores"].append({"restore_id": job.restore_id, "db_type": job.db_type, "status": "dry-run"})
            continue
        summary["ran"] += 1
        metadata = script_restore_metadata(job)
        log_id, _started = schedule.start_run(
            store=store, job_code=job.job_code,
            message=f"Restore {job.label} started.", metadata=metadata,
        )
        started = time.monotonic()
        started_at = utc_now_text()
        emit_backup_restore_event(
            app_config=app_config, command="restore-workflow", phase="START", level="logging",
            message=f"Restore {job.label} started.", logger=logger,
            started_at=started_at, metadata=metadata, notify=job.notify,
        )
        # The copy is the long half of a remote drill - 17 GB over two internet hops took hours
        # here - and between START and END the run said nothing at all, so "still copying" and
        # "hung" looked identical from Telegram until the timeout reaper spoke two hours later.
        # These two events bound that silence; they are the same event machinery, so an entry
        # that has silenced logging_on_run stays silent.
        def on_phase(phase: str, message: str, extra: dict[str, Any] | None = None) -> None:
            emit_backup_restore_event(
                app_config=app_config, command="restore-workflow", phase=phase, level="logging",
                message=message, logger=logger, started_at=utc_now_text(),
                metadata={**metadata, **(extra or {})}, notify=job.notify,
            )

        try:
            # Since 2.69.52 the scheduled restore drives db_ops.common's primitives instead of the
            # shell script. The script remains on disk and still works; what changed is that the
            # nightly run now exercises the same code an operator reaches through the CLI, so a
            # bug in one is a bug in both rather than a surprise in whichever was not tested.
            from db_ops.backup_restore.restore_by_id import restore_by_id

            outcome = restore_by_id(
                {"restore_id": job.restore_id},
                key=key, key_base64=key_base64,
                # The copy boundaries, so the long silence in the middle of a remote drill is
                # bounded by two events instead of looking like a hang.
                on_phase=on_phase,
            )
            status, exit_code = "done", 0
            out = json.dumps(outcome, default=str)
            err = ""
            error_text = None
        except Exception as exc:  # noqa: BLE001 - one restore must not stop the others.
            status, exit_code, out, err = "error", None, "", str(exc)
            error_text = str(exc)[-2000:]
        duration_ms = int((time.monotonic() - started) * 1000)
        end_metadata = {**metadata, "exit_code": exit_code, "duration_ms": duration_ms,
                        "status": status,
                        "stdout_tail": stdout_excerpt(out)}
        end_message = f"Restore {job.label} finished: {status}."
        schedule.finish_run(
            store=store, log_id=log_id, status=status,
            message=end_message,
            duration_ms=duration_ms, error_text=error_text,
            metadata=end_metadata,
        )
        emit_backup_restore_event(
            app_config=app_config, command="restore-workflow",
            phase="END" if status == "done" else "ERROR",
            level="logging" if status == "done" else "error",
            message=end_message, logger=logger, started_at=started_at,
            finished_at=utc_now_text(), duration_ms=duration_ms, error_text=error_text,
            metadata=end_metadata, notify=job.notify,
        )
        summary["succeeded" if status == "done" else "failed"] += 1
        summary["restores"].append({"restore_id": job.restore_id, "db_type": job.db_type,
                                    "status": status, "exit_code": exit_code, "duration_ms": duration_ms})

    return summary


def run_workflow(
    *,
    app_config: Any,
    config_path: str,
    logger: Any = None,
    backup_id: str | None = None,
    job_name: str | None = None,
    restore_id: str | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    env_overrides: dict[str, str] | None = None,
    backup_type: str | None = None,
    copy_hours: int = 24,
    delete_hours: int | None = None,
    skip_backup: bool = False,
    skip_restore: bool = False,
) -> dict[str, Any]:
    """Run the due backup jobs, then the due restore entries."""
    result: dict[str, Any] = {"backup": None, "restore": None}

    if not skip_backup:
        result["backup"] = run_backup(
            app_config=app_config, config_path=config_path, logger=logger,
            backup_id=backup_id, job_name=job_name, key=key, key_base64=key_base64,
            dry_run=dry_run, force=force, env_overrides=env_overrides,
            backup_type=backup_type,
        )
    if not skip_restore:
        result["restore"] = run_scheduled_restores(
            app_config=app_config, config_path=config_path, logger=logger,
            restore_id=restore_id, copy_hours=copy_hours, delete_hours=delete_hours,
            dry_run=dry_run, force=force, key=key, key_base64=key_base64,
        )

    result["failed"] = sum(int((part or {}).get("failed") or 0) for part in (result["backup"], result["restore"]))
    return result
