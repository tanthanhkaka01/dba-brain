"""The combined backup -> restore workflow.

Backup and restore used to be two separately scheduled app commands with two different shapes:
backup entries had a ``backup_id``, a ``time_window`` and a run row per job, while restore
entries had none of that - the scheduler ran *every* active restore on its own fixed interval and
kept no per-entry state. That made the two halves of the same recovery story behave differently
and impossible to reason about together.

They are one command now. A restore entry is scheduled exactly like a backup job: it has a
``time_window``, its due check is :func:`dbabrain.backup_restore.schedule.is_due`, and every run is
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

from dbabrain.backup_restore import restore_messages, schedule
from dbabrain.backup_restore.backup import run_backup
from dbabrain.backup_restore.config import BackupRestoreConfig, load_restore_configs
from dbabrain.backup_restore.restore_script import (
    load_script_restores,
    run_script_restore,
    select_due_script_restores,
    unusable_reason,
)
from dbabrain.backup_restore.events import emit_backup_restore_event, stdout_excerpt
from dbabrain.backup_restore.server_metadata import replay_phase
from dbabrain.lib.integrity import (
    ACTION_COPY_ONLY, IntegrityConfigError, require_overwrite_stated, require_restore_action)
from dbabrain.lib.offload import OffloadConfigError, require_restore_from
from dbabrain.lib.restore import landing as restore_landing
from dbabrain.lib.restore import stated as restore_stated
from dbabrain.lib import instance_bundle
from dbabrain.db.store import DbaBrainStore, RunAlreadyClaimed, utc_now_text


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


def restore_mode_metadata(point_in_time: str) -> dict[str, Any]:
    """``restore_mode`` and the moment, carried on every message of a script-driven run.

    The engine path has always set them. This one did not, and the formatter's default filled in
    ``LATEST``: every message of a point-in-time drill read ``restore_mode=LATEST``, and only the
    RESTORE_START text named the moment (the .251 -> .252 drill, 2026-09-25). The UTC form is the
    moment as the restore reads it - UTC unless the text states another offset.
    """
    if not str(point_in_time or "").strip():
        return {"restore_mode": "LATEST"}
    from dbabrain.lib.restore.moment import MomentError, parse_moment

    meta: dict[str, Any] = {"restore_mode": "POINT_IN_TIME", "point_in_time_original": point_in_time}
    try:
        meta["point_in_time_utc"] = parse_moment(point_in_time).isoformat() + "+00:00"
    except MomentError:
        pass
    return meta


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
        "target_id": job.target_server_id,
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


def _dry_run_plan(job: Any, *, copy_only: bool, point_in_time: str | None, key: Any, key_base64: Any,
                  app_config: Any, logger: Any) -> dict[str, Any]:
    """One script restore's dry run: its plan, or the sentence that refuses it (0.39.0, 1.356)."""
    head = {"restore_id": job.restore_id, "db_type": job.db_type, "status": "dry-run"}
    action, action_error = _entry_action(job, copy_only=copy_only)
    if action_error is not None:
        return {**head, "would_refuse": [str(action_error)], "said": f"refused - {action_error}"}
    try:
        from dbabrain.backup_restore.cli import _open_catalog
        from dbabrain.backup_restore.restore_by_id import restore_by_id

        plan = restore_by_id({"restore_id": job.restore_id, "dry_run": True,
                              **({"point_in_time": point_in_time} if point_in_time else {}),
                              **({"copy_only": True} if action == ACTION_COPY_ONLY else {})},
                             key=key, key_base64=key_base64, catalog=_open_catalog(app_config, logger))
    except Exception as exc:  # noqa: BLE001 - a dry run says what stops the restore; it does not stop.
        return {**head, "would_refuse": [f"{type(exc).__name__}: {exc}"], "said": f"refused - {exc}"}
    answer = {**head, "action": action, **{k: v for k, v in plan.items() if k not in head}}
    answer["said"] = dry_run_said(answer)
    return answer


def dry_run_said(plan: dict[str, Any]) -> str:
    """A restore's dry run in one sentence - what the chat that asked for it reads (0.39.0, 1.356)."""
    refused = list(plan.get("would_refuse") or [])
    copy = plan.get("transfer_plan") or {}
    parts = []
    if copy:
        room = copy.get("space_check") or {}
        parts.append(
            f"{copy.get('file_count', 0)} file(s), {copy.get('bytes', 0) / 1024 ** 3:.2f} GB from {copy.get('reads_from')} "
            f"({copy.get('source_server_id')}) to {copy.get('target_server_id')}:{copy.get('target_dir')}"
            + (f", decrypted through {copy.get('decrypted_through')}" if copy.get("encrypted") else "")
            + (f"; {copy.get('already_on_target')} already there" if copy.get("already_on_target") else "")
            + (f"; room: {'fits' if room.get('ok') else 'DOES NOT FIT'}" if room.get("checked") else "")
            + (f"; staging: {copy.get('staging')}" if copy.get("staging") else ""))
    lands = [item for item in plan.get("lands") or [] if item.get("will")]
    if lands:
        # Where it lands (0.39.0, 1.362): each database, the name it is restored as, and what the
        # target holds under that name now.
        parts.append("lands: " + "; ".join(
            (f"{item['source_database']}" + ("" if str(item["source_database"]).lower() == str(item["lands_as"]).lower()
                                             else f" as {item['lands_as']}") + f" - {item['will']}")
            if item.get("lands_as") else f"{item.get('lands')} - {item['will']}" for item in lands))
    if plan.get("steps"):
        parts.append("then " + ", ".join(str(step).split(" - ")[0] for step in plan["steps"]))
    head = ("WOULD BE REFUSED - " + "; ".join(refused) + (" | " if parts else "")) if refused else "would run: "
    return head + " | ".join(parts) + " - nothing changed"


def _entry_action(entry: Any, *, copy_only: bool) -> tuple[str | None, Exception | None]:
    """What this run of ``entry`` does: copy only when the run says so, else the entry's stated
    ``action`` - or the refusal for an entry that states none, raised inside its own run (R50)."""
    label = f"restore_id={entry.restore_id}"
    try:
        # Stated on every entry (R50). `repository` reads the offload's proven copies: built for the
        # script-driven restores of the engines whose chain the folder's layout states; refused by
        # name elsewhere rather than read as `server`.
        source = require_restore_from(getattr(entry, "restore_from", None), label=label)
        if source == "repository":
            from dbabrain.backup_restore.restore_script import REPOSITORY_ENGINES, ScriptRestore

            engine = str(getattr(entry, "db_type", "") or "").lower()
            if not isinstance(entry, ScriptRestore) or engine not in REPOSITORY_ENGINES:
                raise OffloadConfigError(
                    f"{label}: restore_from repository is built for the script-driven {' and '.join(REPOSITORY_ENGINES)} "
                    "restores (an SSH copy from the repository); this entry is "
                    f"{'share-driven SQL Server' if not isinstance(entry, ScriptRestore) else engine} - "
                    "state restore_from: server.")
            if not entry.is_remote:
                # In place nothing is copied: the script reads the server's own folder, which is not
                # the repository - read so, the entry would say one thing and do another.
                raise OffloadConfigError(f"{label}: restore_from repository copies the repository's files to the "
                                         "target's staging; an in-place entry copies nothing - give it a target "
                                         "server, or state restore_from: server.")
        # Stated on every entry (0.39.0, 1.362): whether a restore may replace a database that exists.
        require_overwrite_stated(getattr(entry, "overwrite_existing", False), label=label)
        # And, on a script-driven SQL Server entry, which databases it restores and the name each lands
        # under (1.362): with none named it restored whatever FULL it found. A share-driven entry has
        # stated the same since 0.27.0 (lib.stated_facts); a whole-instance engine has nothing to name.
        from dbabrain.backup_restore.restore_script import ScriptRestore as _Script

        if isinstance(entry, _Script) and str(entry.db_type or "").lower() in restore_landing.BY_DATABASE_ENGINES:
            restore_landing.require_landing(entry.landing, label=label)
        # And the four that decided a run from a default (1.366): whether production may be the
        # target, whether the consistency check runs, what the copy takes, whether the room is checked.
        restore_stated.require_stated(getattr(entry, "unstated", ()), label=label)
        if copy_only:
            return ACTION_COPY_ONLY, None
        return require_restore_action(entry.action, label=label), None
    except (IntegrityConfigError, OffloadConfigError, restore_landing.LandingError,
            restore_stated.StatedError) as exc:
        return None, exc


def _share_restored(output: Any) -> dict[str, str]:
    """What a share-driven restore's END message says it did (0.39.0, 1.370): the databases restored,
    the check, and where each stopped - read off the workflow's own summary."""
    said: dict[str, str] = {}
    if not isinstance(output, dict):
        return said
    latest = output.get("restore-latest") if isinstance(output.get("restore-latest"), dict) else {}
    restored = [str(name) for source in latest.get("sources") or [] if isinstance(source, dict)
                for name, status in (source.get("per_database_restore_status") or {}).items()
                if str(status) == "SUCCESS"]
    if restored:
        said["restored"] = ", ".join(restored)
    verify = output.get("verify-restore") or {}
    if isinstance(verify, dict) and verify.get("checked") is not None:
        said["verified"] = f"{verify.get('checked')} database(s) checked, {verify.get('failed') or 0} unusable"
        landed = [item for target in verify.get("targets") or [] if isinstance(target, dict)
                  for item in target.get("landed") or []]
        if landed:
            from dbabrain.backup_restore.restore_by_id import landed_said

            said["landed"] = landed_said(landed)
    return said


def run_scheduled_restores(
    *,
    app_config: Any,
    config_path: str,
    logger: Any = None,
    restore_id: str | None = None,
    copy_hours: int | None = None,
    delete_retention: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    key: str | None = None,
    key_base64: str | None = None,
    on_demand: bool = False,
    point_in_time: str = "",
    copy_only: bool = False,
) -> dict[str, Any]:
    """Run every restore entry that is due, recording each under its own restore_id.

    Each entry does what its ``action`` states (0.34.0, 1.216): ``restore``, or ``copy_only`` - the
    copy from ``server_id`` to the target through the checksum gate, and nothing restored. An entry
    that states none is that entry's error (R50). ``copy_only=True`` makes this one run copy only,
    whatever the entries state (``restore-workflow --copy-only``).

    ``on_demand`` is an operator naming one entry (``restore-workflow --restore-id``, which is
    what ``/spbot_restore`` runs): it runs whether or not the entry is active, as the SQL Server
    path always has. ``point_in_time`` reaches a script-driven restore's plan.
    """
    # Imported here: run_restore_workflow lives in the CLI module, which imports this one.
    from dbabrain.backup_restore.cli import run_restore_workflow

    configs = load_restore_configs(config_path)
    # Both kinds are loaded before --restore-id is judged: a script-driven entry (Oracle,
    # PostgreSQL) is absent from the SQL Server list, so filtering that list alone would report
    # a perfectly valid restore_id as missing.
    script_jobs = load_script_restores(config_path)
    if restore_id:
        # An inactive entry the loader could not read is not "missing": it says what it lacks.
        why_not = unusable_reason(configs, restore_id) or unusable_reason(script_jobs, restore_id)
        configs = [item for item in configs if item.restore_id == restore_id]
        script_jobs = [item for item in script_jobs if item.restore_id == restore_id]
        if why_not:
            raise ValueError(why_not)
        if not configs and not script_jobs:
            raise ValueError(f"No backup_restore entry found with restore_id={restore_id}.")

    store = DbaBrainStore.from_config(app_config)
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
    listed_at = utc_now_text()
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
        job_code = schedule.restore_job_code(config.restore_id)
        # Taken and done by an overlapping async run since this one listed it (schedule.taken_since).
        if not force and schedule.taken_since(store, job_code, listed_at):
            summary["skipped"] += 1
            summary["restores"].append({"restore_id": config.restore_id, "status": "taken-by-another-run"})
            continue
        action, action_error = _entry_action(config, copy_only=copy_only)
        is_copy = action == ACTION_COPY_ONLY
        noun = "Copy only" if is_copy else "Restore"
        metadata = {"restore_id": config.restore_id, "source_id": config.source_id, "target_id": config.target_id,
                    "action": action or ""}
        try:
            log_id, _started_at = schedule.start_run(
                store=store, job_code=job_code,
                message=f"{noun} {config.restore_id} started.", metadata=metadata,
            )
        except RunAlreadyClaimed as exc:
            summary["skipped"] += 1
            summary["restores"].append({"restore_id": config.restore_id,
                                        "status": "already-running", "detail": str(exc)})
            continue
        summary["ran"] += 1
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
            message=f"{noun} {config.restore_id} started.", logger=logger,
            started_at=started_at,
            # The flow format (1.306 / 1.370), as the script-driven restore's START.
            metadata={**metadata, **restore_messages.started(
                str(config.restore_id), source=str(config.source_id), target=str(config.target_id),
                moment="", copy_only=is_copy)},
            notify=config.notify,
        )

        def announce(event: str, message: str, extra: dict[str, Any] | None = None) -> None:
            fields = dict(extra or {})
            # A short step's start is not a message of its own (1.306): the run row and the log keep it.
            on_telegram = fields.pop("telegram", True) is not False
            emit_backup_restore_event(
                app_config=app_config, command="restore-workflow", phase=event, level="logging",
                message=message, logger=logger, started_at=utc_now_text(),
                metadata={**metadata, **fields}, notify=config.notify, telegram=on_telegram,
            )

        # The same two phases the script path runs, from the same shared function - see
        # server_metadata.replay_phase. This path names its target by ip rather than by
        # container, which is the only difference and the reason the arguments differ here.
        # A copy-only run touches nothing on the target instance: no metadata replayed, before or after.
        metadata_lines, phase_reports = ("", {}) if (is_copy or action_error) else replay_engine_phase(
            config, phase=instance_bundle.PRE_DATABASE, announce=announce,
        )
        try:
            if action_error is not None:
                raise action_error
            output = run_restore_workflow(
                restore_configs=[config],
                app_config=app_config,
                copy_hours=copy_hours,
                delete_retention=delete_retention,
                dry_run=False,
                force=force,
                logger=logger,
                # The engine path copies inside run_restore_workflow, so this is where its copy
                # boundaries come from. The script path announces them from restore_by_id. Both
                # now emit the same two events, which is the point.
                on_phase=announce,
                copy_only=is_copy,
                # The catalog's run row points at this one (1.214).
                job_run_id=log_id,
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
                per_database = output.get("per_database_restore_status") or {}
                checked = [name for name in failed_databases if str(per_database.get(name)) == "CHECK_FAILED"]
                not_restored = [name for name in failed_databases if name not in checked]
                error_text = (
                    f"restore reported {output.get('status')!s}"
                    + (f"; databases not restored: {', '.join(not_restored)}" if not_restored else "")
                    # Restored and recovered - only the check after it failed.
                    + (f"; integrity check (DBCC CHECKDB) failed: {', '.join(checked)}" if checked else "")
                )[-2000:]
        except Exception as exc:  # noqa: BLE001 - one restore must not stop the others.
            output = {"status": "ERROR", "error": str(exc)}
            status = "error"
            error_text = str(exc)[-2000:]

        # Only after a restore that worked: Agent job steps name databases that have to exist.
        if status == "done" and not is_copy:
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
        end_message = f"{noun} {config.restore_id} finished: {status}." + (
            " Nothing restored - the files are staged and proven on the target." if is_copy and status == "done" else "")
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
            metadata={**end_metadata, **restore_messages.finished(
                str(config.restore_id), status=status, copy_only=is_copy, restored=_share_restored(output),
                error=error_text or "", seconds=duration_ms // 1000)},
            notify=config.notify,
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
                   if (j.active or on_demand) and not schedule.is_running(j.job_code, latest_runs)]
                  if force else select_due_script_restores(jobs=script_jobs, latest_runs=latest_runs))
    summary["due"] += len(script_due)
    summary["skipped"] += len(script_jobs) - len(script_due)

    for job in script_due:
        if dry_run:
            # 0.39.0 (1.356): the dry run answered "dry-run" and nothing else. It is now the restore's
            # own plan, read only: what the entry's action is or why it is refused, the chain where the
            # copy would read it, whether it fits on the target, and the steps after it. No run is
            # claimed, nothing is announced.
            summary["restores"].append(_dry_run_plan(job, copy_only=copy_only, point_in_time=point_in_time,
                                                     key=key, key_base64=key_base64, app_config=app_config,
                                                     logger=logger))
            continue
        if not force and schedule.taken_since(store, job.job_code, listed_at):
            summary["skipped"] += 1
            summary["restores"].append({"restore_id": job.restore_id, "db_type": job.db_type,
                                        "status": "taken-by-another-run"})
            continue
        action, action_error = _entry_action(job, copy_only=copy_only)
        is_copy = action == ACTION_COPY_ONLY
        noun = "Copy only" if is_copy else "Restore"
        metadata = {**script_restore_metadata(job), **restore_mode_metadata(point_in_time), "action": action or ""}
        try:
            log_id, _started = schedule.start_run(
                store=store, job_code=job.job_code,
                message=f"{noun} {job.label} started.", metadata=metadata,
            )
        except RunAlreadyClaimed as exc:
            # Another run of this same restore is in flight. Skipped, not failed, and not counted
            # as a run: a restore is not idempotent, and two of them into one database is the
            # failure of 2026-09-14 rather than a busy scheduler.
            summary["skipped"] += 1
            summary["restores"].append({"restore_id": job.restore_id, "db_type": job.db_type,
                                        "status": "already-running", "detail": str(exc)})
            continue
        summary["ran"] += 1
        started = time.monotonic()
        started_at = utc_now_text()
        emit_backup_restore_event(
            app_config=app_config, command="restore-workflow", phase="START", level="logging",
            message=f"{noun} {job.label} started.", logger=logger,
            started_at=started_at,
            # In the flow format a backup's messages have (1.306), each step of a restore after it (1.370).
            metadata={**metadata, **restore_messages.started(
                job.restore_id, source=str(job.server_id), target=str(job.target_server_id or job.server_id),
                moment=point_in_time, copy_only=is_copy,
                from_repository=getattr(job, "restore_from", None) == "repository")},
            notify=job.notify,
        )
        # The copy is the long half of a remote drill - 17 GB over two internet hops took hours
        # here - and between START and END the run said nothing at all, so "still copying" and
        # "hung" looked identical from Telegram until the timeout reaper spoke two hours later.
        # These two events bound that silence; they are the same event machinery, so an entry
        # that has silenced logging_on_run stays silent.
        def on_phase(phase: str, message: str, extra: dict[str, Any] | None = None) -> None:
            fields = dict(extra or {})
            # A short step's start is not a message of its own (1.306): the run row and the log keep it.
            on_telegram = fields.pop("telegram", True) is not False
            emit_backup_restore_event(
                app_config=app_config, command="restore-workflow", phase=phase, level="logging",
                message=message, logger=logger, started_at=utc_now_text(),
                metadata={**metadata, **fields}, notify=job.notify, telegram=on_telegram,
            )

        try:
            # Since 2.69.52 the scheduled restore drives dbabrain.common's primitives instead of the
            # shell script. The script remains on disk and still works; what changed is that the
            # nightly run now exercises the same code an operator reaches through the CLI, so a
            # bug in one is a bug in both rather than a surprise in whichever was not tested.
            from dbabrain.backup_restore.cli import _open_catalog
            from dbabrain.backup_restore.restore_by_id import restore_by_id

            if action_error is not None:
                raise action_error
            outcome = restore_by_id(
                {"restore_id": job.restore_id,
                 **({"point_in_time": point_in_time} if point_in_time else {}),
                 **({"copy_only": True} if is_copy else {})},
                key=key, key_base64=key_base64,
                # The copy boundaries, so the long silence in the middle of a remote drill is
                # bounded by two events instead of looking like a hang.
                on_phase=on_phase,
                # The copy's checksum gate records into it (0.34.0, 1.213b); None still gates.
                catalog=_open_catalog(app_config, logger),
                job_run_id=log_id,
            )
            status, exit_code = "done", 0
            out = json.dumps(outcome, default=str)
            err = ""
            error_text = None
            warnings = [str(w) for w in (outcome.get("warnings") or [])]
            restored = {"restored": outcome.get("plan") or ""}
            if outcome.get("verify"):
                restored["verified"] = (f"{outcome['verify'].get('checked')} database(s) checked, "
                                        f"{outcome['verify'].get('failed')} unusable")
            if outcome.get("landed"):
                from dbabrain.backup_restore.restore_by_id import landed_said

                # Where each database stopped, as the target itself recorded it (0.39.0, 1.362).
                restored["landed"] = landed_said(outcome["landed"])
            if outcome.get("continued"):
                from dbabrain.backup_restore.restore_by_id import continued_said

                # Taken up from this entry's own unfinished restore (1.372).
                restored["continued"] = continued_said(outcome["continued"])
            if outcome.get("plain_deleted"):
                # The copies this entry staged - decrypted or plain - deleted after it worked (1.371).
                gone = outcome["plain_deleted"]
                restored["plain_deleted"] = (f"{gone.get('deleted', 0)} of {gone.get('plain_files', 0)} staged "
                                             f"copy(ies), {gone.get('bytes_deleted', 0)} bytes")
        except Exception as exc:  # noqa: BLE001 - one restore must not stop the others.
            status, exit_code, out, err = "error", None, "", str(exc)
            error_text = str(exc)[-2000:]
            warnings = []
            restored = {}
        duration_ms = int((time.monotonic() - started) * 1000)
        end_metadata = {**metadata, "exit_code": exit_code, "duration_ms": duration_ms,
                        "status": status, **restored,
                        "stdout_tail": stdout_excerpt(out)}
        end_message = f"{noun} {job.label} finished: {status}." + (
            " Nothing restored - the files are staged and proven on the target." if is_copy and status == "done" else "")
        if warnings:
            # Done, and something the operator must hear about - a piece passed over (1.23). Said
            # in the message and raised to `warning`, so the alert is not a quiet success.
            end_message = f"Restore {job.label} finished: done with a warning - {'; '.join(warnings)}"
            end_metadata["warnings"] = warnings
        schedule.finish_run(
            store=store, log_id=log_id, status=status,
            message=end_message,
            duration_ms=duration_ms, error_text=error_text,
            metadata=end_metadata,
        )
        emit_backup_restore_event(
            app_config=app_config, command="restore-workflow",
            phase="END" if status == "done" else "ERROR",
            level=("warning" if warnings else "logging") if status == "done" else "error",
            message=end_message, logger=logger, started_at=started_at,
            finished_at=utc_now_text(), duration_ms=duration_ms, error_text=error_text,
            metadata={**end_metadata, **restore_messages.finished(
                job.restore_id, status=status, copy_only=is_copy, restored=restored, warnings=warnings,
                error=error_text or "", seconds=duration_ms // 1000)},
            notify=job.notify,
        )
        summary["succeeded" if status == "done" else "failed"] += 1
        summary["restores"].append({"restore_id": job.restore_id, "db_type": job.db_type,
                                    "status": status, "exit_code": exit_code, "duration_ms": duration_ms,
                                    # What a person at the CLI reads without opening the store: why it
                                    # failed, what it warned of, and where each database stopped.
                                    **({"error": error_text} if error_text else {}),
                                    **({"warnings": warnings} if warnings else {}),
                                    **({"landed": restored["landed"]} if restored.get("landed") else {})})

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
    copy_hours: int | None = None,
    delete_retention: int | None = None,
    skip_backup: bool = False,
    skip_restore: bool = False,
    copy_only: bool = False,
    skip_offload: bool = False,
) -> dict[str, Any]:
    """Run the due backup jobs, then the due restore entries, then the repository copies.

    The repository copy (0.34.0, 1.240) is the backup half's last step and runs with it: an entry
    with ``offload_enabled`` has its written files copied off the server, proven there, and - with
    ``offload_delete_source`` - released. Last, so a long copy never delays a due restore in this
    pass; and it writes nothing at all for an entry with nothing to copy.
    """
    result: dict[str, Any] = {"backup": None, "restore": None, "offload": None}

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
            restore_id=restore_id, copy_hours=copy_hours, delete_retention=delete_retention,
            dry_run=dry_run, force=force, key=key, key_base64=key_base64, copy_only=copy_only,
        )

    if not skip_backup and not skip_offload:
        from dbabrain.backup_restore.offload import run_offloads

        result["offload"] = run_offloads(
            app_config=app_config, config_path=config_path, logger=logger, backup_id=backup_id,
            key=key, key_base64=key_base64, dry_run=dry_run)

    result["failed"] = sum(int((part or {}).get("failed") or 0)
                           for part in (result["backup"], result["restore"], result["offload"]))
    return result
