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
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dbabrain.backup_restore import restore_messages, schedule
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
        # Every step said here, as the workflow calls it (0.40.0, 1.375): 1 now, 2-10 as each runs, 11 below.
        say = _RestoreSay(app_config=app_config, logger=logger, notify=config.notify, metadata=metadata,
                          restore_id=str(config.restore_id), engine="sqlserver", target=str(config.target_id),
                          source=str(config.source_id))
        say.started(source=str(config.source_id), moment="", copy_only=is_copy, from_repository=False, remote=True)
        phase_reports: dict[str, Any] = {}
        try:
            if action_error is not None:
                raise action_error
            # Through the CLI module's door, the one a hand run takes too: the steps are said here (say).
            from dbabrain.backup_restore.cli import run_restore_workflow

            output = run_restore_workflow(
                restore_configs=[config],
                app_config=app_config,
                copy_hours=copy_hours,
                delete_retention=delete_retention,
                dry_run=False,
                force=force,
                logger=logger,
                copy_only=is_copy,
                # The catalog's run row points at this one (1.214).
                job_run_id=log_id,
                say=say,
                # A copy-only run touches nothing on the target instance: no metadata replayed.
                replay_metadata=not is_copy,
            )
            phase_reports = dict(output.get("server_metadata") or {})
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
            output = {"status": "ERROR", "error": str(exc), **dict(getattr(exc, "metadata", None) or {})}
            phase_reports = dict(output.get("server_metadata") or {})
            status = "error"
            error_text = str(exc)[-2000:]

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
        end_message = f"{noun} {config.restore_id} finished: {status}." + (
            " Nothing restored - the files are staged and proven on the target." if is_copy and status == "done" else "")
        schedule.finish_run(
            store=store, log_id=log_id, status=status,
            message=end_message,
            duration_ms=duration_ms, error_text=error_text,
            metadata=end_metadata,
        )
        say.finished(status=status, copy_only=is_copy, restored=_share_restored(output), error=error_text or "",
                     seconds=duration_ms // 1000, run_row=end_metadata)
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
        # Every step said here, as the workflow calls it (0.40.0, 1.375): 1 now, 2-10 as each runs, 11 below.
        say = _RestoreSay(app_config=app_config, logger=logger, notify=job.notify, metadata=metadata,
                          restore_id=job.restore_id, engine=str(job.db_type or "").lower(),
                          target=str(job.target_server_id or job.server_id), source=str(job.server_id))
        say.started(source=str(job.server_id), moment=point_in_time, copy_only=is_copy,
                    from_repository=getattr(job, "restore_from", None) == "repository",
                    remote=bool(getattr(job, "is_remote", False)))
        try:
            # Since 2.69.52 the scheduled restore drives dbabrain.common's primitives instead of the
            # shell script. The script remains on disk and still works; what changed is that the
            # nightly run now exercises the same code an operator reaches through the CLI, so a
            # bug in one is a bug in both rather than a surprise in whichever was not tested.
            from dbabrain.backup_restore.cli import _open_catalog

            if action_error is not None:
                raise action_error
            outcome = run_restore_by_id(
                {"restore_id": job.restore_id,
                 **({"point_in_time": point_in_time} if point_in_time else {}),
                 **({"copy_only": True} if is_copy else {})},
                key=key, key_base64=key_base64,
                # The copy's checksum gate records into it (0.34.0, 1.213b); None still gates.
                catalog=_open_catalog(app_config, logger),
                job_run_id=log_id, say=say,
            )
            status, exit_code = "done", 0
            out = json.dumps(outcome, default=str)
            err = ""
            error_text = None
            warnings = [str(w) for w in (outcome.get("warnings") or [])]
            restored = restored_said(outcome)
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
        say.finished(status=status, copy_only=is_copy, restored=restored, warnings=warnings, error=error_text or "",
                     seconds=duration_ms // 1000, run_row=end_metadata)
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
            backup_type=backup_type, offload=not skip_offload,
        )
    if not skip_restore:
        result["restore"] = run_scheduled_restores(
            app_config=app_config, config_path=config_path, logger=logger,
            restore_id=restore_id, copy_hours=copy_hours, delete_retention=delete_retention,
            dry_run=dry_run, force=force, key=key, key_base64=key_base64, copy_only=copy_only,
        )

    if not skip_backup and not skip_offload:
        # What each backup's own process left (1.374): retries, files past the pass's limit, an
        # encryption still running, the upkeep.
        result["offload"] = run_offloads(
            app_config=app_config, config_path=config_path, logger=logger, backup_id=backup_id,
            key=key, key_base64=key_base64, dry_run=dry_run)

    result["failed"] = sum(int((part or {}).get("failed") or 0)
                           for part in (result["backup"], result["restore"], result["offload"]))
    return result


# --------------------------------------------------------------------------- #
# A backup's ten steps - said here, and only here (0.40.0, 1.374)
# --------------------------------------------------------------------------- #
# The operator, 2026-11-16: *the messages are handled in the outermost code, not inside each internal
# proc* - and, asked which: run_workflow orchestrates the whole process. Per due job it says 1 and 2,
# runs the backup, says 3, copies that backup's files right there and says 4-10. The procs under it
# (backup.run_claimed_backup_job, the offload's copy, encryption, deletes) answer what they did and say
# nothing. What is left over - a copy to try again, files past offload_files_per_pass, an encryption
# still running, the upkeep - is run_offloads' pass, and its messages are said here too.
@dataclass
class _Pass:
    """What every step of a pass needs: where it records, how it reaches its hosts, who it tells."""

    app_config: Any
    store: Any
    catalog: Any
    logger: Any
    data_dir: Any
    key: str | None
    key_base64: str | None
    sleep: Any
    locked: bool


def _said_text(said: dict[str, Any]) -> str:
    """A step's message as the log file and the event row read it: its title, then its lines - each line's
    own leading "- " (a step with nothing to do) dropped, so the row does not read "- - "."""
    lines = [line[2:] if str(line).startswith("- ") else str(line) for line in said.get("flow_lines") or []]
    return said["flow_title"].split(" · ", 1)[-1] + (f" - {'; '.join(lines)}" if lines else "")


def _say(ctx: _Pass, *, command: str, phase: str, said: dict[str, Any], notify: Any, metadata: dict[str, Any],
         level: str | None = None, message: str = "", **times: Any) -> None:
    """One step's message: its level from what it says (a step that failed is an error), its text the
    flow's; the log file and the event row read the step and its lines."""
    bad = any(str(line).startswith("❌") for line in said.get("flow_lines") or []) or "FAILED" in said["flow_title"]
    if bad and phase.endswith("_DONE"):
        phase = phase[:-len("_DONE")] + "_ERROR"
    emit_backup_restore_event(
        app_config=ctx.app_config, command=command, phase=phase, level=level or ("error" if bad else "logging"),
        message=message or _said_text(said),
        logger=ctx.logger, metadata={**metadata, **said}, notify=notify, **times)


def _record_rman_chain(item: Any, *, run_id: Any, ctx: _Pass) -> dict[str, Any] | None:
    """Ask the Oracle source's RMAN which pieces form the restore chain now, and keep the answer on the
    backup run (0.40.0, 1.380) - ``{"pieces": n}``, ``{"error": why}``, or ``None`` when the entry does
    not record its chain.

    The same question a restore from the server asks at copy time (``prepare-staging`` with its
    ``chain``, plan only: nothing is read or changed at the source but RMAN's catalog), asked while the
    source answers: a restore from the repository may be the one that runs when it no longer does.
    """
    if str(item.db_type).lower() != "oracle" or not getattr(item, "record_chain", False) or run_id is None:
        return None
    from dbabrain.backup_restore.backup import resolve_ssh_target
    from dbabrain.backup_restore.restore_script import _ssh_login
    from dbabrain.transport import common_cli

    try:
        rows = ctx.catalog.backup_run_rows(int(run_id))
        # Where the pieces lie on the server itself: a container names them by its own mount.
        folders = {str(row.get("host_path") or "").replace("\\", "/").rsplit("/", 1)[0]
                   for row in rows if row.get("host_path")}
        source_dir = sorted(folders)[0] if len(folders) == 1 else item.backup_dir
        source = resolve_ssh_target(item.server_id, label=item.label, data_dir=ctx.data_dir)
        answer = common_cli.run("prepare-staging", {
            "source": _ssh_login(source, data_dir=ctx.data_dir, key=ctx.key, key_base64=ctx.key_base64),
            "source_dir": source_dir, "make_readable": False, "plan_only": True,
            "chain": {"db_type": "oracle", "backup_dir": item.backup_dir, "container": source.container_name}},
            stream_stderr=True)
        pieces = [str(piece) for piece in answer.get("include") or []]
        if not pieces:
            raise ValueError("RMAN named no piece of the chain")
        # The source's clock beside it (1.385): a point in time restored from the repository is read
        # in it, and the source may be gone by then.
        from dbabrain.backup_restore import source_clock

        clock, _why = source_clock.ask(item.server_id, db_type="oracle", data_dir=ctx.data_dir)
        ctx.catalog.record_rman_chain(int(run_id), pieces=pieces, clock_offset_minutes=clock)
        return {"pieces": len(pieces), **({"clock_offset_minutes": clock} if clock is not None else {})}
    except Exception as exc:  # noqa: BLE001 - the backup stands; its step 3 says why the chain was not kept
        error = " ".join(f"{type(exc).__name__}: {exc}".split())[:500]
        try:
            ctx.catalog.record_rman_chain(int(run_id), error=error)
        except Exception:  # noqa: BLE001 - the catalog itself failing is said by the same line
            pass
        return {"error": error}


def run_backup(
    *,
    app_config: Any,
    config_path: str,
    logger: Any = None,
    backup_id: str | None = None,
    job_name: str | None = None,
    data_dir: Any = None,
    key: str | None = None,
    key_base64: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    env_overrides: dict[str, str] | None = None,
    backup_type: str | None = None,
    offload: bool = True,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Run the due backup jobs, each through its whole process, and say every step of it.

    ``offload`` false (``--skip-offload``) stops each process at its backup: steps 4-9 are left to a
    later pass, which says them and the process's end.
    """
    from dbabrain.backup_restore import backup as B
    from dbabrain.backup_restore import backup_messages as BM
    from dbabrain.backup_restore import offload as O
    from dbabrain.db.lockdown_store import lockdown_on

    plan = B.plan_backup_pass(app_config=app_config, config_path=config_path, logger=logger, backup_id=backup_id,
                              job_name=job_name, force=force, dry_run=dry_run)
    summary = plan.summary
    ctx = _Pass(app_config=app_config, store=plan.store, catalog=plan.catalog, logger=logger, data_dir=data_dir,
                key=key, key_base64=key_base64, sleep=sleep, locked=False)
    entries = {entry.backup_id: entry for entry in O.entries_from_jobs(plan.jobs)} if offload and plan.due else {}
    for item in plan.due:
        if dry_run:
            summary["jobs"].append(B.dry_run_line(item))
            continue
        log_id, started_at, skipped = B.claim_backup_job(item, store=plan.store, listed_at=plan.listed_at, force=force)
        if log_id is None:
            summary["skipped"] += 1
            summary.setdefault(skipped.pop("skipped"), []).append(skipped)
            continue
        summary["ran"] += 1
        said_by = {"backup_id": item.backup_id, "job": item.job, "server_id": item.server_id}
        # Steps 1 and 2 - every level alike (the operator, 2026-11-16: full, diff and log share one path);
        # what reaches a chat is the job's notify.
        _say(ctx, command="backup", phase="START", said=BM.process_started(item, started_at=started_at),
             notify=item.notify, metadata=said_by, message=f"{item.label} started.", started_at=started_at)
        _say(ctx, command="backup", phase="BACKUP_START", said=BM.backup_started(item, started_at=started_at),
             notify=item.notify, metadata=said_by, started_at=started_at)
        outcome = B.run_claimed_backup_job(
            item, log_id=log_id, started_at=started_at, app_config=app_config, config_path=config_path,
            store=plan.store, catalog=plan.catalog, data_dir=data_dir, key=key, key_base64=key_base64,
            env_overrides=env_overrides, backup_type=backup_type, dry_run=dry_run)
        if outcome.stopped:
            B.count_backup(summary, outcome)
            continue
        result = outcome.result
        times = {"started_at": started_at, "finished_at": outcome.finished_at}
        run_id = (outcome.catalog_summary or {}).get("backup_run_id")
        # Oracle (0.40.0, 1.380): the restore chain asked of the source's RMAN now, while it answers, and
        # kept on the run - a restore from the repository copies by it. A failure leaves the backup standing.
        chain = _record_rman_chain(item, run_id=run_id, ctx=ctx) if result.status == "done" else None
        level = outcome.level
        if chain and chain.get("error") and level in (None, "logging", "info"):
            level = "warning"
        # Step 3, said BEFORE the run's row closes: a pass of its own takes a backup's files only once
        # that row is no longer RUNNING, so no copy can be told ahead of it - on 2026-11-15 "Copy +
        # encrypt started" came four minutes before "Backup diff done".
        _say(ctx, command="backup", phase="END" if result.status == "done" else "ERROR",
             said=BM.backup_done(item, result=result, started_at=started_at, finished_at=outcome.finished_at,
                                 catalog_summary=outcome.catalog_summary, scrub=outcome.scrub,
                                 anomalies=outcome.anomalies, markers=outcome.markers,
                                 server_metadata=outcome.metadata.get("server_metadata"), chain=chain),
             notify=item.notify, metadata=outcome.metadata, level=level, message=outcome.message,
             duration_ms=result.duration_ms, error_text=result.error_text, **times)
        entry = entries.get(item.backup_id)
        claimed = None
        takes = B.repository_takes_it(plan.catalog, item, outcome.catalog_summary)
        if takes and entry is not None and run_id is not None:
            # The offload claimed BEFORE the backup's row closes: no other pass takes these files first.
            try:
                claimed = O.start_offload_run(entry, store=plan.store, metadata={
                    "backup_id": entry.backup_id, "server_id": entry.server_id,
                    "repository_server_id": entry.offload.server_id, "backup_run_id": int(run_id)})
            except RunAlreadyClaimed:
                claimed = None  # another pass holds this entry's offload: a later pass says 4-10
        elif not takes:
            # Nothing of this backup goes to the repository: its process ends here, in its own message.
            _say(ctx, command="backup", phase="PROCESS_DONE",
                 said=BM.process_done(item.backup_id, started_at=started_at, finished_at=outcome.finished_at,
                                      failed_step="" if result.status == "done" else "3",
                                      where=[B.where_without_a_repository(item, outcome.catalog_summary)],
                                      error="" if result.status == "done"
                                      else " ".join(str(result.error_text or "failed").split())[:300]),
                 notify=item.notify, metadata=outcome.metadata, level=outcome.level, **times)
        B.close_backup_job(outcome, store=plan.store)
        B.count_backup(summary, outcome)
        if claimed is not None:
            ctx.locked = lockdown_on(app_config)
            run = plan.catalog.backup_run(int(run_id))
            per_pass = int(entry.offload.files_per_pass)
            summary.setdefault("offload", []).append(_offload(
                entry, ctx=ctx, run=run, rows=O.run_rows(entry, catalog=plan.catalog, backup_run_id=int(run_id)),
                releasable=[], expirable=O._expirable(entry, plan.catalog), last=True, claimed=claimed,
                bundle_of=_bundle_to_carry(item, outcome),
                refresh_release=O.release_refresher(entry, catalog=plan.catalog, locked=ctx.locked,
                                                    per_pass=per_pass),
                summary={"ran": 0, "succeeded": 0, "failed": 0}))
    return summary


def run_offloads(
    *,
    app_config: Any,
    config_path: str,
    logger: Any = None,
    backup_id: str | None = None,
    data_dir: Any = None,
    key: str | None = None,
    key_base64: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """The pass for what each backup's own process left: a copy to try again, files past
    ``offload_files_per_pass``, an encryption an earlier pass left running, the upkeep - said as the
    steps they are (1.374). An entry with nothing to do writes nothing: no run row, no message.
    ``limit`` overrides the entry's ``offload_files_per_pass`` for this pass only."""
    from dbabrain.backup_restore import offload as O
    from dbabrain.backup_restore import schedule
    from dbabrain.db.backup_catalog import BackupCatalogStore
    from dbabrain.db.lockdown_store import lockdown_on
    from dbabrain.db.store import DbaBrainStore

    entries = [e for e in O.entries_to_offload(config_path) if not backup_id or e.backup_id == backup_id]
    summary: dict[str, Any] = {"entries": len(entries), "ran": 0, "succeeded": 0, "failed": 0, "jobs": []}
    if not entries:
        summary["said"] = O.said(summary, backup_id=backup_id, dry_run=dry_run)
        return summary
    store = DbaBrainStore.from_config(app_config)
    catalog = BackupCatalogStore.from_config(app_config)
    schedule.reap_stale_runs(
        store=store, app_config=app_config, logger=logger,
        timeouts={schedule.offload_job_code(e.backup_id): e.timeout_seconds for e in entries},
        notify={schedule.offload_job_code(e.backup_id): e.first.notify for e in entries})
    ctx = _Pass(app_config=app_config, store=store, catalog=catalog, logger=logger, data_dir=data_dir, key=key,
                key_base64=key_base64, sleep=sleep, locked=lockdown_on(app_config))
    running = O.backup_runs_still_running(store)
    for entry in entries:
        plan = O.plan_entry(entry, catalog=catalog, app_config=app_config, running=running, locked=ctx.locked,
                            limit=limit)
        if plan.empty:
            continue
        if dry_run:
            summary["jobs"].append({"backup_id": entry.backup_id, "status": "dry-run", "to_copy": len(plan.rows),
                                    "to_release": len(plan.releasable), "to_expire": len(plan.expirable),
                                    "to_encrypt": len(plan.encryptable)})
            continue
        refresh = O.release_refresher(entry, catalog=catalog, locked=ctx.locked, per_pass=plan.per_pass)
        # One backup after another: each backup's files are one offload run with its own steps, so one
        # backup's messages are never interleaved with another's. The retention and the folder check go
        # with the last; a pass with no file to copy is the entry's upkeep, its step 9 said alone.
        groups = O.by_backup_run(plan.rows) or [(None, [])]
        for index, (run_id, group) in enumerate(groups):
            last = index == len(groups) - 1
            summary["jobs"].append(_offload(
                entry, ctx=ctx, run=catalog.backup_run(run_id) if run_id is not None else None, rows=group,
                releasable=plan.releasable if index == 0 else [], expirable=plan.expirable if last else [],
                last=last, refresh_release=refresh, summary=summary))
    summary["said"] = O.said(summary, backup_id=backup_id, dry_run=dry_run)
    return summary


def _bundle_to_carry(item: Any, outcome: Any) -> Any:
    """The backup job whose metadata bundle this run exported and its entry carries to the repository
    (0.40.0, 1.376) - ``None`` when there is none to carry."""
    plan = getattr(item, "server_metadata", None)
    exported = (outcome.metadata.get("server_metadata") or {}) if outcome is not None else {}
    if plan is None or not getattr(plan, "enabled", False) or not getattr(plan, "repository_path", ""):
        return None
    return item if exported.get("ok") else None


def _offload(entry: Any, *, ctx: _Pass, run: dict[str, Any] | None, rows: list[dict[str, Any]],
             releasable: list[dict[str, Any]], expirable: list[dict[str, Any]], last: bool, summary: dict[str, Any],
             refresh_release: Any = None, claimed: tuple[int, str] | None = None,
             bundle_of: Any = None) -> dict[str, Any]:
    """One offload run - one backup's files, or the entry's upkeep - with its steps 4-10 said in order.

    Steps 8-10 wait for the backup's last file: a backup cut by ``offload_files_per_pass`` says its copy
    "done in part" and ends in a later pass. A step that fails ends the process at that step; the next
    pass starts again from its copy. An encryption an earlier pass left running is not a failure: this
    pass says 7 and 8 waiting, 9, and 10, and the pass that finishes it says that backup's 7, 8 and 10
    (the operator, 2026-11-16).
    """
    from dbabrain.backup_restore import backup_messages as BM
    from dbabrain.backup_restore import offload as O

    run_id = int(run["backup_run_id"]) if run else None
    metadata: dict[str, Any] = {
        "backup_id": entry.backup_id, "server_id": entry.server_id, "repository_server_id": entry.offload.server_id,
        "to_copy": len(rows), "to_release": len(releasable), "to_expire": len(expirable),
        **({"backup_run_id": run_id} if run_id is not None else {})}
    if claimed is None:
        try:
            claimed = O.start_offload_run(entry, store=ctx.store, metadata=metadata)
        except RunAlreadyClaimed:
            return {"backup_id": entry.backup_id, "status": "already_running"}
    log_id, started_at = claimed
    summary["ran"] += 1
    started = time.monotonic()
    repository_id = str(entry.offload.server_id)
    result: dict[str, Any] = {}
    error = ""
    step = "4"   # the step a failure is charged to - where the process stopped, said by step 10
    left = 0
    encrypted: set[int] = set()
    on_the_way: dict[str, Any] | None = None
    after: dict[str, Any] = {}
    waiting_before = O.waiting_backups(ctx.store, entry) if entry.offload.encrypt else {}

    def tell(phase: str, said: dict[str, Any], *, backup_run_id: int | None = run_id) -> None:
        _say(ctx, command="offload", phase=phase, said=said, notify=entry.first.notify, started_at=started_at,
             metadata={**metadata, **({"backup_run_id": backup_run_id} if backup_run_id is not None else {})})

    try:
        hosts, secrets = O.open_hosts(entry, data_dir=ctx.data_dir, key=ctx.key, key_base64=ctx.key_base64)
        sealing = O.sealing_of(entry, app_config=ctx.app_config, secrets=secrets, key=ctx.key,
                               key_base64=ctx.key_base64) if rows else None
        if rows and run_id is not None:
            again = any(str(r.get("offload_state") or "") == "failed" for r in rows) or any(
                str(f.get("offload_state") or "") == "verified" for f in ctx.catalog.backup_run_rows(run_id))
            tell("COPY_START", BM.copy_started(
                entry.backup_id, files=len(rows), size_bytes=sum(int(r.get("size_bytes") or 0) for r in rows),
                repository=repository_id, again=again,
                directory=any(O.I.is_base_directory(r.get("file_type"), r.get("file_name")) for r in rows)))
            if entry.offload.encrypt:
                tell("ENCRYPT_START", BM.encrypt_started(entry.backup_id, on_the_way=sealing is not None))
        if rows:
            result["copy"] = O.offload_entry(entry, catalog=ctx.catalog, hosts=hosts, rows=rows, sleep=ctx.sleep,
                                             sealing=sealing)
            if bundle_of is not None:
                result["bundle"] = _carry_bundle(entry, bundle_of, ctx=ctx, hosts=hosts, secrets=secrets)
            if run_id is not None:
                left = O.files_left(entry, catalog=ctx.catalog, backup_run_id=run_id)
                tell("COPY_DONE", BM.copy_done(entry.backup_id, result["copy"], repository=repository_id, left=left,
                                               bundle=result.get("bundle")))
        step = "6"
        if entry.offload.encrypt:
            after = O.encrypt_copies(entry, app_config=ctx.app_config, catalog=ctx.catalog, hosts=hosts,
                                     secrets=secrets, key=ctx.key, key_base64=ctx.key_base64)
            on_the_way = (result.get("copy") or {}).pop("sealed", None)
            result["encrypt"] = O._merge_encrypt(on_the_way, after)
        step = "8"
        if refresh_release is not None and (rows or result.get("encrypt")):
            # This pass's own copies, verified (and sealed) just now, are released in this pass too.
            releasable = refresh_release()
        sealed: dict[int, list[dict[str, Any]]] = {}
        if entry.offload.encrypt or releasable or expirable:
            encrypted, sealed = O.encryption_state(entry, app_config=ctx.app_config)
        if releasable:
            result["release"] = O.release_sources(entry, catalog=ctx.catalog, hosts=hosts, rows=releasable,
                                                  encrypted=encrypted, sealed=sealed)
        step = "9"
        if expirable and last:
            result["expire"] = O.prune_repository(entry, catalog=ctx.catalog, hosts=hosts, encrypted=encrypted,
                                                  sealed=sealed)
        if entry.offload.repository_check and last:
            result["repository_check"] = O.check_folder(entry, store=ctx.store, catalog=ctx.catalog,
                                                        app_config=ctx.app_config, hosts=hosts)
        step = "10"
    except Exception as exc:  # noqa: BLE001 - one entry's failure is its own run's, never the pass's
        error = f"{type(exc).__name__}: {exc}"[:2000]
    # Whose rows: this backup's (steps 7, 8), an earlier backup that was waiting (its 7, 8, 10), or the
    # rest - sealed late (step 9).
    own = ({int(f["file_id"]) for f in ctx.catalog.backup_run_rows(run_id)} if run_id is not None and not error
           else set())
    sealed_now = set(encrypted) | set(after.get("file_ids") or [])
    finished_waiting = {b: ids for b, ids in waiting_before.items() if b != run_id and ids and set(ids) <= sealed_now}
    late = O.rows_of(after, exclude=own | {i for ids in finished_waiting.values() for i in ids})
    own_waiting = [i for i in after.get("waiting_ids") or [] if i in own]
    if own_waiting:
        metadata["encrypt_waiting"] = own_waiting
    if finished_waiting:
        metadata["encrypt_finished_runs"] = sorted(finished_waiting)
    status, finished_at = O.finish_offload_run(entry, store=ctx.store, log_id=log_id, started=started,
                                               result=result, error=error, metadata=metadata)
    key_until = O._key_until(ctx.app_config, result)
    for earlier_run_id in sorted(finished_waiting):
        earlier = ctx.catalog.backup_run(earlier_run_id)
        if earlier is not None:
            _say_a_finished_wait(entry, earlier, ids=finished_waiting[earlier_run_id], after=after, ctx=ctx,
                                 sealed=sealed_now, finished_at=finished_at, tell=tell, key_until=key_until)
    released = result.get("release") or {}
    own_files = O.repository_files(ctx.catalog, run_id) if run_id is not None else []
    own_names = [str(f.get("file_name")) for f in own_files]
    upkeep = dict(server_id=entry.server_id, repository=repository_id, offload=entry.offload,
                  earlier_released=len([i for i in released.get("deleted_ids") or [] if int(i) not in own]),
                  release_refused=[r for r in released.get("refused") or []
                                   if not any(str(r).startswith(name) for name in own_names)],
                  expired=result.get("expire") or None, late=late, check=result.get("repository_check") or None)
    if run is not None:
        _say_the_end_of_a_backup(entry, run, ctx=ctx, result=result, error=error, step=step, left=left,
                                 own=own, files=own_files, encrypted=set(encrypted), on_the_way=on_the_way,
                                 after=after, own_waiting=own_waiting, upkeep=upkeep, last=last,
                                 finished_at=finished_at, tell=tell, key_until=key_until)
    elif error or released or result.get("expire") or result.get("repository_check") \
            or late.get("encrypted") or late.get("failed"):
        tell("UPKEEP_DONE", BM.repository_upkeep(entry.backup_id, **upkeep, error=error))
    summary["succeeded" if status == "done" else "failed"] += 1
    return {"backup_id": entry.backup_id, "status": status, **result, **({"error": error} if error else {})}


def _carry_bundle(entry: Any, job: Any, *, ctx: _Pass, hosts: Any, secrets: dict[str, str]) -> dict[str, Any]:
    """The instance's metadata bundle, sealed and carried beside its backups (0.40.0, 1.376). Never the
    reason a backup's copy fails: what went wrong is said on its own line."""
    from dbabrain.backup_restore import instance_bundle_store
    from dbabrain.backup_restore.server_metadata import instance_bundle_dir
    from dbabrain.lib.secret_text import resolve_cli_key, resolve_key

    try:
        return instance_bundle_store.carry_to_repository(
            entry, bundle_dir=instance_bundle_dir(entry.server_id),
            repository_path=str(job.server_metadata.repository_path), repository_host=hosts.repository_host,
            app_config=ctx.app_config, secrets=secrets,
            passphrase=resolve_cli_key(ctx.key, ctx.key_base64) or resolve_key() or "")
    except Exception as exc:  # noqa: BLE001 - said on step 5's line, the backup stands
        return {"error": f"{type(exc).__name__}: {exc}"[:400], "path": str(job.server_metadata.repository_path)}


def _say_a_finished_wait(entry: Any, run: dict[str, Any], *, ids: list[int], after: dict[str, Any], ctx: _Pass,
                         sealed: set[int], finished_at: Any, tell: Any, key_until: str) -> None:
    """An earlier backup whose encryption this pass finished: its 7 done, 8 cleared, 10 done."""
    from dbabrain.backup_restore import backup_messages as BM
    from dbabrain.backup_restore import offload as O

    run_id = int(run["backup_run_id"])
    files = O.repository_files(ctx.catalog, run_id)
    on_repository = [f for f in files if str(f.get("offload_state") or "") == "verified"]
    plain = sum(1 for f in on_repository if int(f["file_id"]) not in sealed)
    tell("ENCRYPT_DONE", BM.encrypt_done(entry.backup_id, O.rows_of(after, only=set(ids)), key_until=key_until),
         backup_run_id=run_id)
    tell("PLAIN_DONE", BM.plain_cleared(
        entry.backup_id, server_id=entry.server_id, delete_source=bool(entry.offload.delete_source), released=None,
        own_files=len(files), own_released=sum(1 for f in files if str(f.get("state") or "") != "present"),
        repository=str(entry.offload.server_id), repository_plain=plain), backup_run_id=run_id)
    tell("PROCESS_DONE", BM.process_done(
        entry.backup_id, started_at=run.get("started_at"), finished_at=finished_at, failed_step="8" if plain else "",
        where=[f"✅ on {entry.offload.server_id}: {len(on_repository)} of {len(files)} file(s) of this backup"
               + (", each .age" if on_repository and not plain else "")]), backup_run_id=run_id)


def _say_the_end_of_a_backup(entry: Any, run: dict[str, Any], *, ctx: _Pass, result: dict[str, Any], error: str,
                             step: str, left: int, own: set[int], files: list[dict[str, Any]], encrypted: set[int],
                             on_the_way: dict[str, Any] | None, after: dict[str, Any], own_waiting: list[int],
                             upkeep: dict[str, Any], last: bool, finished_at: Any, tell: Any, key_until: str) -> None:
    """Steps 7-10 of one backup - once its last file is through, or when a step failed.

    Step 8 is read, not assumed (the operator listed it as *clear the plain files from server_id and the
    repo*): at the source, how many of this backup's files are deleted and how many kept; at the
    repository, which of its copies are not sealed."""
    from dbabrain.backup_restore import backup_messages as BM
    from dbabrain.backup_restore import offload as O

    copied = result.get("copy") or {}
    released = result.get("release") or {}
    if error and step in ("4", "6", "8"):
        # Stopped before its steps were through: the process ends at the step it was in.
        tell("PROCESS_DONE", BM.process_done(entry.backup_id, started_at=run.get("started_at"),
                                             finished_at=finished_at, failed_step=step, error=error))
        return
    own_encrypt = O.rows_of(after, only=own)
    if on_the_way:
        own_encrypt = {**own_encrypt, "rows": own_encrypt["rows"] + int(on_the_way.get("rows") or 0),
                       "encrypted": own_encrypt["encrypted"] + int(on_the_way.get("encrypted") or 0),
                       "failed": list(on_the_way.get("failed") or []) + own_encrypt["failed"],
                       "key_id": on_the_way.get("key_id") or own_encrypt.get("key_id")}
    if copied and entry.offload.encrypt:
        tell("ENCRYPT_DONE", BM.encrypt_done(entry.backup_id, own_encrypt, key_until=key_until,
                                             waiting=len(own_waiting), repository=str(entry.offload.server_id)))
    failed_step = "5" if copied.get("failed") else "7" if own_encrypt.get("failed") else ""
    if not failed_step and left:
        return  # copied in part: the rest, and this backup's end, in a later pass
    names = [str(f.get("file_name")) for f in files]
    own_refused = [r for r in released.get("refused") or [] if any(str(r).startswith(name) for name in names)]
    on_repository = [f for f in files if str(f.get("offload_state") or "") == "verified"]
    repository_plain = (sum(1 for f in on_repository if int(f["file_id"]) not in encrypted)
                        if entry.offload.encrypt else None)
    if not failed_step:
        tell("PLAIN_DONE", BM.plain_cleared(
            entry.backup_id, server_id=entry.server_id, delete_source=bool(entry.offload.delete_source),
            released={"refused": own_refused}, own_files=len(files),
            own_released=sum(1 for f in files if str(f.get("state") or "") != "present"),
            repository=str(entry.offload.server_id), repository_plain=repository_plain, locked=ctx.locked,
            waiting=bool(own_waiting)))
    said = BM.repository_upkeep(entry.backup_id, **upkeep, error=error)
    if not last and entry.offload.repository_check:
        # The folder is checked once a pass, after its last backup: said there, not "not run" here.
        said["flow_lines"] = [line if not line.startswith("- repository check") else
                              "- repository check: after the last backup of this pass" for line in said["flow_lines"]]
    tell("UPKEEP_DONE", said)
    if not failed_step and (own_refused or (repository_plain and not own_waiting)):
        failed_step = "8"
    if not failed_step and str(run.get("status") or "").upper() == "ERROR":
        # The backup itself failed on the server; its good files still went to the repository.
        failed_step = "3"
    sealed_all = bool(on_repository) and all(int(f["file_id"]) in encrypted for f in on_repository)
    tell("PROCESS_DONE", BM.process_done(
        entry.backup_id, started_at=run.get("started_at"), finished_at=finished_at, failed_step=failed_step,
        where=[f"✅ on {entry.offload.server_id}: {len(on_repository)} of {len(files)} file(s) of this backup"
               + (", each .age" if entry.offload.encrypt and sealed_all else "")],
        later=(f"{len(own_waiting)} file(s) still being encrypted - steps 7 and 8 are said again when a later pass "
               "finishes them" if own_waiting else ""),
        error=error))


# --------------------------------------------------------------------------- #
# A restore's eleven steps - called and said here (0.40.0, 1.375)
# --------------------------------------------------------------------------- #
# The operator, 2026-11-16: the workflow calls each restore step itself and says it, as it does a
# backup's; restore_by_id only answers. Confirmed steps: 1 started, 2 copy, 3 metadata before,
# 4 certificate, 5 full (5.1, 5.2 ... per database, a start and a done), 6 diff (the same), 7 log (one
# message per file, 7.1, 7.2 ...), 8 check, 9 metadata after, 10 plain files cleared, 11 done - the
# same numbers for every engine, a step that does not apply still said, with why.
_NOT_APPLIED = {
    2: "the target reads the source's own folder - nothing is copied",
    10: "nothing was staged on the target for this restore",
}
_NOT_APPLIED_BY_ENGINE = {
    ("sqlserver", 6): "no differential after the full",
    ("sqlserver", 7): "no log after it",
    ("postgresql", 6): "the incrementals are combined into the base backup",
    ("postgresql", 7): "the WAL is replayed with the base backup",
    ("oracle", 6): "inside the RMAN duplicate",
    ("oracle", 7): "the archived logs are applied inside the RMAN duplicate",
}


def restored_said(outcome: dict[str, Any]) -> dict[str, str]:
    """What a restore put in, as its last message and its run row say it."""
    from dbabrain.backup_restore.restore_by_id import continued_said, landed_said

    restored = {"restored": outcome.get("plan") or ""}
    if outcome.get("verify"):
        restored["verified"] = (f"{outcome['verify'].get('checked')} database(s) checked, "
                                f"{outcome['verify'].get('failed')} unusable")
    if outcome.get("landed"):
        # Where each database stopped, as the target itself recorded it (0.39.0, 1.362).
        restored["landed"] = landed_said(outcome["landed"])
    if outcome.get("continued"):
        # Taken up from this entry's own unfinished restore (1.372).
        restored["continued"] = continued_said(outcome["continued"])
    if outcome.get("plain_deleted"):
        # The copies this entry staged - decrypted or plain - deleted after it worked (1.371).
        gone = outcome["plain_deleted"]
        # The size in the one form step 10 says it in (a lab drill, 2026-11-16: step 10 said 2.6 GB
        # and step 11 the same files as 2746138112 bytes).
        restored["plain_deleted"] = (f"{gone.get('deleted', 0)} of {gone.get('plain_files', 0)} staged "
                                     f"copy(ies) ({restore_messages.size_said(gone.get('bytes_deleted', 0))})")
    return restored


class _RestoreSay:
    """Says one restore's steps in order: numbers each, says a step that does not apply, and knows the
    step a failure stopped at."""

    def __init__(self, *, app_config: Any, logger: Any, notify: Any, metadata: dict[str, Any], restore_id: str,
                 engine: str, target: str, source: str = "", command: str = "restore-workflow") -> None:
        self.app_config, self.logger, self.notify = app_config, logger, notify
        self.metadata, self.restore_id, self.command = metadata, restore_id, command
        self.engine = "postgresql" if engine == "postgres" else engine
        self.target = target
        self.source = source
        self.remote = False  # set by started(): whether anything is copied onto the target
        self.at = 1          # the last step said
        self.current = "1"   # the step running now - where a failure stopped
        self.counts = {5: 0, 6: 0, 7: 0}
        self.started_at = utc_now_text()

    def _emit(self, phase: str, said: dict[str, Any], *, level: str | None = None,
              event: dict[str, Any] | None = None, **extra: Any) -> None:
        bad = any(str(line).startswith("❌") for line in said.get("flow_lines") or []) or "FAILED" in said["flow_title"]
        if bad and phase.endswith("_DONE"):
            phase = phase[:-len("_DONE")] + "_ERROR"
        emit_backup_restore_event(
            app_config=self.app_config, command=self.command, phase=phase,
            level=level or ("error" if bad else "logging"),
            message=_said_text(said),
            logger=self.logger, started_at=self.started_at, metadata={**self.metadata, **extra, **said},
            notify=self.notify, **(event or {}))

    def _reach(self, number: int) -> None:
        """Every step between the last said and ``number`` that did not run, said with why."""
        for step in range(self.at + 1, number):
            # Nothing copied and nothing staged only in place: a restore onto another machine says 2 and 10 itself.
            reason = ((_NOT_APPLIED.get(step) if not self.remote else None)
                      or _NOT_APPLIED_BY_ENGINE.get((self.engine, step)))
            if reason:
                self._emit("STEP_SKIP", restore_messages.none(self.restore_id, step, reason))
        self.at = max(self.at, number)
        self.current = str(number)

    def started(self, *, source: str, moment: str, copy_only: bool, from_repository: bool, remote: bool) -> None:
        self.remote = remote
        plan = restore_messages.plan_line(target=self.target, remote=remote, from_repository=from_repository,
                                          copy_only=copy_only)
        self._emit("START", restore_messages.process_started(
            self.restore_id, source=source, target=self.target, moment=moment, plan=plan, copy_only=copy_only,
            from_repository=from_repository))

    def copy(self, event: str, *, transferred: dict[str, Any] | None = None, from_repository: bool = False) -> None:
        self._reach(2)
        self._emit("COPY_START" if event == "start" else "COPY_DONE", restore_messages.copy_step(
            self.restore_id, target=self.target, event=event, transferred=transferred,
            from_repository=from_repository, source=self.source))

    def metadata_said(self, number: int, report: dict[str, Any]) -> None:
        self._reach(number)
        self._emit("METADATA_DONE", restore_messages.metadata_step(self.restore_id, number=number, report=report),
                   level="warning" if report.get("status") != "OFF" and not report.get("ok", True) else None)

    def entering(self, number: int) -> None:
        """A step about to run that says nothing until it is done (the certificate, a log): a failure in it
        is that step's, said by 11."""
        self._reach(number)

    def certificate(self, result: dict[str, Any]) -> None:
        self._reach(4)
        self._emit("CERT_DONE", restore_messages.certificate_step(self.restore_id, result=result))

    def chain(self, kind: str, event: str, facts: dict[str, Any], *, seconds: int | None = None) -> None:
        step = 5 if kind == "full" else 6
        self._reach(step)
        if event == "start":
            self.counts[step] += 1
        number = f"{step}.{max(self.counts[step], 1)}"
        self._emit(f"{kind.upper()}_{'START' if event == 'start' else 'DONE'}", restore_messages.chain_step(
            self.restore_id, number=number, kind=kind, event=event, facts=facts, seconds=seconds))

    def log(self, facts: dict[str, Any], *, seconds: int | None = None, result: str = "applied",
            reason: str = "") -> None:
        """One log file - applied, passed over (a share's Msg 4326 / 4305 file out of the chain), or failed."""
        self._reach(7)
        self.counts[7] += 1
        self._emit("LOG_DONE", restore_messages.log_step(self.restore_id, number=f"7.{self.counts[7]}",
                                                          facts=facts, seconds=seconds, result=result, reason=reason))

    def check(self, event: str, *, checked: Any = None, failed: Any = None) -> None:
        self._reach(8)
        self._emit("CHECK_START" if event == "start" else "CHECK_DONE",
                   restore_messages.check_step(self.restore_id, event=event, checked=checked, failed=failed))

    def plain(self, cleaned: dict[str, Any]) -> None:
        self._reach(10)
        self._emit("PLAIN_DONE", restore_messages.plain_step(self.restore_id, target=self.target, cleaned=cleaned))

    def finished(self, *, status: str, copy_only: bool = False, restored: dict[str, Any] | None = None,
                 warnings: list[str] | None = None, error: str = "", seconds: int | None = None,
                 run_row: dict[str, Any] | None = None) -> None:
        """Step 11. ``run_row``: what the run's row records (its status, duration, output tail), carried on the
        event as before."""
        if status == "done" and not copy_only:
            self._reach(11)
        self._emit("END" if status == "done" else "ERROR", restore_messages.process_done(
            self.restore_id, status=status, failed_step="" if status == "done" else self.current,
            copy_only=copy_only, restored=restored, warnings=warnings, error=error, seconds=seconds),
            level=("warning" if warnings else "logging") if status == "done" else "error",
            # The run's end as its event row records it: when, how long, and why it failed.
            event={"finished_at": utc_now_text(), "duration_ms": None if seconds is None else int(seconds) * 1000,
                   "error_text": error or None},
            **(run_row or {}))


def run_restore_by_id(request: dict[str, Any], *, data_dir: Any = None, key: str | None = None,
                      key_base64: str | None = None, catalog: Any = None, job_run_id: int | None = None,
                      say: _RestoreSay | None = None) -> dict[str, Any]:
    """Restore one configured entry, calling each of restore_by_id's steps in order and saying each
    (``say``; ``None`` says nothing). Its run rows in the catalog are closed with the outcome."""
    from dbabrain.backup_restore.copy_gate import close_restore_runs

    runs: list[tuple[int, bool]] = []
    try:
        result = _restore_by_id_steps(request, data_dir=data_dir, key=key, key_base64=key_base64, catalog=catalog,
                                      job_run_id=job_run_id, say=say, runs=runs)
    except Exception as exc:
        close_restore_runs(catalog, runs, phase="error", error_text=f"{type(exc).__name__}: {exc}"[:2000])
        raise
    close_restore_runs(catalog, runs, phase="copied" if result.get("copy_only") else "done", error_text="")
    return result


def _restore_by_id_steps(request: dict[str, Any], *, data_dir: Any, key: str | None, key_base64: str | None,
                         catalog: Any, job_run_id: int | None, say: _RestoreSay | None,
                         runs: list[tuple[int, bool]]) -> dict[str, Any]:
    from dbabrain.backup_restore import restore_by_id as R
    from dbabrain.backup_restore.copy_gate import record_run

    prep = R.prepare(request, data_dir=data_dir, key=key, key_base64=key_base64, catalog=catalog)
    if prep.dry_run:
        return R.dry_run(prep, catalog=catalog)
    job, restore_id, engine = prep.job, prep.restore_id, prep.engine
    # A name the target already holds, and a replace under a lockdown, refused BEFORE anything is copied.
    resume = {} if prep.copy_only else R.refuse_before_copy(prep, catalog=catalog)

    transferred, target_host = None, None
    if prep.remote:
        if say:
            say.copy("start", from_repository=prep.from_repository)
        transferred, target_host = R.copy(prep, catalog=catalog)
        if say:
            say.copy("done", transferred=transferred, from_repository=prep.from_repository)
    run_id = R.open_run(prep, transferred=transferred, catalog=catalog, job_run_id=job_run_id, runs=runs)
    if prep.copy_only:
        # 1.216: copied from server_id to the target and proven - nothing restored, no metadata replayed,
        # the staging kept (it is the product).
        return {"restore_id": restore_id, "db_type": engine, "copy_only": True, "dry_run": False,
                "transfer": transferred, "steps": [], "warnings": []}

    warnings: list[str] = []
    pre = R.metadata(prep, instance_bundle.PRE_DATABASE)
    if say:
        say.metadata_said(3, pre)
    warnings += [w for w in [R.metadata_warning(pre, instance_bundle.PRE_DATABASE)] if w]
    if say:
        say.entering(4)
    cert = R.certificate(prep)
    if say:
        say.certificate(cert)
    steps = R.plan_chain(prep, resume=resume)
    if cert.get("step"):
        # The certificate was the first step of the plan before it became one of its own: its place in
        # the answer and the plan's summary stays.
        steps.insert(0, cert["step"])

    plan = R._plan_summary(steps)
    restoring = [s for s in steps if s.get("request") and s["op"] != "verify-restore" and not s.get("done")]
    record_run(catalog, run_id, phase="restoring")
    # Every file this restore is about to apply, in order, written before the first one goes in (0.39.0).
    chain_of_step, chain_warning = R._record_chain(catalog, run_id, job, steps)
    if chain_warning:
        warnings.append(chain_warning)
    continued = [s["continued"] for s in steps if s.get("op") == "continued"]
    chain_number = {id(s): number for number, s in
                    enumerate([s for s in restoring if s["op"] in R._CHAIN_STEPS], start=1)}
    results: list[dict[str, Any]] = []
    verify: dict[str, Any] | None = None
    expects: list[dict[str, Any]] = []
    landed: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if step.get("warning"):
            warnings.append(step["warning"])
            continue
        if step["op"] == "expect":
            # Not a step: what the plan says this database stops at, held to the target after the check.
            expects.append(step["expect"])
            continue
        if step["op"] == "continued":
            continue
        if step.get("done"):
            # Run before planning (the certificate); recorded, not repeated.
            results.append({"step": step["op"], "ok": True})
            continue
        op = step["op"]
        facts = R.step_facts(step) if op in R._CHAIN_STEPS else {}
        if op == "verify-restore":
            record_run(catalog, run_id, phase="verifying_db")
            if say:
                say.check("start")
        elif op in ("restore-full", "restore-diff") and say:
            say.chain("full" if op == "restore-full" else "diff", "start", facts)
        elif op == "restore-log" and say:
            say.entering(7)
        started = time.monotonic()
        outcome = R.apply(step, catalog=catalog, run_id=run_id, positions=chain_of_step.get(index),
                          number=chain_number.get(id(step)), total=len(chain_number), restore_id=restore_id)
        seconds = int(time.monotonic() - started)
        if say and op in ("restore-full", "restore-diff"):
            say.chain("full" if op == "restore-full" else "diff", "done", facts, seconds=seconds)
        elif say and op == "restore-log":
            say.log(facts, seconds=seconds)
        results.append({"step": op, "ok": bool(outcome.get("ok", True))})
        if op == "verify-restore":
            verify = {"checked": outcome.get("checked"), "failed": outcome.get("failed")}
            # Where each database stopped, as the TARGET recorded it, against what the plan asked (1.362).
            landed, off_target = R.where_it_stopped(expects, outcome.get("databases") or [])
            if landed:
                record_run(catalog, run_id, landed_json=json.dumps(landed, default=str))
            record_run(catalog, run_id, databases_failed=int(outcome.get("failed") or 0),
                       databases_ok=max(int(outcome.get("checked") or 0) - int(outcome.get("failed") or 0), 0))
            if say:
                say.check("done", checked=outcome.get("checked"), failed=outcome.get("failed"))
            if not outcome.get("ok"):
                # A restore that finished and left a database nobody can query is the failure the whole
                # check exists to catch; it must not be reported as success.
                raise R.RestoreByIdError(
                    f"{restore_id}: restored, but verification failed - "
                    f"{outcome.get('failed')} of {outcome.get('checked')} database(s) unusable.")
            if off_target:
                # Open and consistent, and not where it was asked to be (1.363 said done seven hours early).
                raise R.RestoreByIdError(
                    f"{restore_id}: restored and checked, but not to what was asked - " + "; ".join(off_target))
    # Only after a restore that worked: Agent job steps name databases that have to exist.
    post = R.metadata(prep, instance_bundle.POST_DATABASE)
    if say:
        say.metadata_said(9, post)
    warnings += [w for w in [R.metadata_warning(post, instance_bundle.POST_DATABASE)] if w]

    cleaned: dict[str, Any] | None = None
    if transferred is not None and target_host is not None:
        cleaned = R.cleanup(prep, transferred=transferred, target_host=target_host, run_id=run_id, catalog=catalog)
        warnings += cleaned["warnings"]
        if say:
            say.plain(cleaned)
    pruned = (cleaned or {}).get("pruned")
    plain = (cleaned or {}).get("plain")
    return {"restore_id": restore_id, "db_type": engine, "steps": results,
            "transferred": transferred, "point_in_time": prep.point_in_time or None,
            "warnings": warnings, "plan": plan, "verify": verify, "pruned": pruned,
            "landed": landed, "restore_run_id": run_id,
            "continued": continued, "plain_deleted": plain}


# --------------------------------------------------------------------------- #
# The share-driven restore's steps - called and said here too (0.40.0, 1.375)
# --------------------------------------------------------------------------- #
def _share_facts(plan: Any, path: Any, *, last: bool = False, log_number: int = 0) -> dict[str, Any]:
    """What one file of a share-driven database applies, as facts the step words say."""
    from pathlib import PureWindowsPath

    from dbabrain.lib.timezone import format_display

    pit = plan.point_in_time_utc
    return {"database": str(plan.candidate.source_database_name), "landing": str(plan.candidate.restore_database_name),
            "files": [PureWindowsPath(str(path)).name] if path else [],
            "stopat": (str(plan.stopat_source or format_display(pit)) if last and pit is not None else ""),
            "recovered": bool(last and pit is not None),
            **({"log_number": log_number, "log_total": len(plan.selected_log_backups)} if log_number else {})}


def restore_share_database(*, config: Any, app_config: Any, database: Any = None, backup_file: Any = None,
                           dry_run: bool = False, logger: Any = None, point_in_time_utc: Any = None,
                           chain: Any = None, certificate_result: Any = None,
                           say: _RestoreSay | None = None) -> dict[str, Any]:
    """One database of a share-driven restore: its chain planned, then its full, its diff and each of its
    logs applied - each called and said here - then its recovery and its check."""
    from dbabrain.backup_restore import restore_database as D

    plan = D.plan_database(config=config, dbabrain_config=app_config, database=database, backup_file=backup_file,
                           logger=logger, point_in_time_utc=point_in_time_utc)
    plan.certificate_result = certificate_result
    if dry_run:
        return D.dry_run_answer(plan)
    try:
        D.begin_database(plan, chain=chain)
        facts = _share_facts(plan, plan.candidate.backup_file_on_vm)
        if say:
            say.chain("full", "start", facts)
        started = time.monotonic()
        outcome = D.apply_full(plan)
        if say and outcome.get("status") != "SKIPPED":
            say.chain("full", "done", facts, seconds=int(time.monotonic() - started))
        if plan.selected_diff_backup:
            facts = _share_facts(plan, plan.selected_diff_backup)
            if say:
                say.chain("diff", "start", facts)
            started = time.monotonic()
        outcome = D.apply_diff(plan)
        if say and plan.selected_diff_backup and outcome.get("status") != "SKIPPED":
            say.chain("diff", "done", facts, seconds=int(time.monotonic() - started))
        started = time.monotonic()
        total = len(plan.selected_log_backups)
        if say and total:
            say.entering(7)
        for index, path, result, reason in D.LogChain(plan).each():
            if say:
                say.log(_share_facts(plan, path, last=index == total, log_number=index),
                        seconds=int(time.monotonic() - started), result=result, reason=reason)
            started = time.monotonic()
        return D.finish_database(plan)
    except Exception as exc:
        D.fail_database(plan, exc)
        raise


def restore_share_server(*, config: Any, dbabrain_config: Any, dry_run: bool = False, logger: Any = None,
                         point_in_time_utc: Any = None, chain: Any = None,
                         say: _RestoreSay | None = None) -> dict[str, Any]:
    """Every database of one share-driven entry: the certificate first (step 4), then each database in
    turn; one database's failure does not stop the others (unless a point in time is asked)."""
    from dbabrain.backup_restore import restore_database as D
    from dbabrain.backup_restore.config import assert_target_is_allowed

    D.validate_restore_target_is_not_source(config)
    # Never into a production instance the entry does not allow (1.223).
    assert_target_is_allowed(restore_id=getattr(config, "restore_id", ""), target_server_id=getattr(config, "target_id", ""),
                             allowed=bool(getattr(config, "allow_production_target", False)))
    start_monotonic = time.monotonic()
    D.ensure_vm_share_credential(config)
    if say:
        say.entering(4)
    certificate = D.ensure_source_certificate_quietly(config=config, dry_run=dry_run, logger=logger)
    if say:
        say.certificate({"status": "none"} if (certificate or {}).get("none")
                        else {"status": "imported", "name": (certificate or {}).get("certificate_name")})
    results: list[dict[str, Any]] = []
    for database, backup_file, label in D.server_databases(config, point_in_time_utc):
        try:
            results.append(restore_share_database(
                config=config, app_config=dbabrain_config, database=database, backup_file=backup_file, dry_run=dry_run,
                logger=logger, point_in_time_utc=point_in_time_utc, chain=chain, certificate_result=certificate,
                say=say))
        except Exception as exc:  # noqa: BLE001 - one database's failure must not abort the others
            D._emit_restore_log(logger, f"restore-database FAILED database={label} "
                                        f"error={str(exc).replace(chr(10), ' ')[:300]}", level="error")
            if point_in_time_utc is not None:
                raise
            results.append({"database_name": label, "status": D._failed_status(exc), "error": str(exc)[:500]})
    return D.server_result(config, results, certificate_result=certificate, dry_run=dry_run,
                           start_monotonic=start_monotonic, logger=logger)


def _share_metadata(config: Any, phase: str, say: "_RestoreSay | None") -> dict[str, Any]:
    """One metadata phase of a share-driven entry, said as its step (3 before, 9 after)."""
    plan = getattr(config, "server_metadata", None)
    if plan is None or not getattr(plan, "enabled", False):
        report: dict[str, Any] = {"ok": True, "status": "OFF", "reason": "server_metadata is off for this entry"}
        reports: dict[str, Any] = {}
    else:
        _line, reports = replay_engine_phase(config, phase=phase, announce=None)
        report = reports.get(phase) or {"ok": True, "status": "OFF",
                                        "reason": f"the entry's server_metadata has no {phase} phase"}
    if say:
        say.metadata_said(3 if phase == instance_bundle.PRE_DATABASE else 9, report)
    return reports


def run_share_restore(
    *,
    restore_configs: list[BackupRestoreConfig],
    app_config: object,
    copy_hours: int | None = None,
    delete_retention: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    logger: object | None = None,
    point_in_time_utc: object = None,
    copy_only: bool = False,
    job_run_id: int | None = None,
    say: "_RestoreSay | None" = None,
    replay_metadata: bool = False,
) -> dict[str, object]:
    """A share-driven restore - each entry's copy, every database's full, diff and logs, the check, the
    cleanup - its steps called and said here (0.40.0, 1.375; ``say``: ``None`` says nothing).

    ``copy_only`` (0.34.0, 1.216): the same preflight, copy and checksum gate from ``server_id`` to the
    target, then stop - no RESTORE, no verify, no cleanup of the staging, which is the product. The
    catalog's run rows say ``copy_only`` and end ``copied``.
    """
    from dbabrain.backup_restore.cli import (
        ChainRecord, RestoreWorkflowError, _build_per_restore_results, _build_restore_mapping, _close_catalog_runs,
        _count_restore_statuses, _failed_restore_databases, _format_restore_workflow_stdout, _held_to_the_plan,
        _load_secrets, _open_catalog, _restore_failure_text, _restored_databases, _verify_plan, _workflow_phase,
        common_cli, dataclasses, errors, log_event, newest_backup_hint, point_in_time_window_start,
        run_copy_backup, run_delete_backup, run_target_preflight, sanitize_text, summarize_error_tail)
    from dbabrain.backup_restore.copy_gate import record_run

    restore_mode = "POINT_IN_TIME" if point_in_time_utc is not None else "LATEST"
    if copy_only:
        from dbabrain.lib.integrity import ACTION_COPY_ONLY

        restore_configs = [dataclasses.replace(c, action=ACTION_COPY_ONLY) for c in restore_configs]
    mappings = [_build_restore_mapping(c) for c in restore_configs]
    # The backup catalog's run rows this workflow opens - one per source copied - closed at the end
    # with the restore's own outcome (0.34.0). A copy the gate refused keeps its `refused`.
    catalog = None if dry_run else _open_catalog(app_config, logger)
    gated_runs: list[tuple[int, bool]] = []
    # Each entry's run row, in the configs' order, moved through its phases below (1.214).
    run_ids: list[int | None] = []
    summary: dict[str, object] = {
        "status": "RUNNING",
        "overall_workflow_status": "RUNNING",
        "restore_mode": restore_mode,
        "point_in_time_utc": point_in_time_utc.isoformat() if point_in_time_utc is not None and hasattr(point_in_time_utc, "isoformat") else None,
        "restore_count": len(restore_configs),
        "mappings": mappings,
        "per_restore_results": None,
        "copy-backup": None,
        "restore-latest": None,
        "verify-restore": None,
        "delete-backup": None,
    }
    _rw_restore_id = restore_configs[0].restore_id if len(restore_configs) == 1 else ""
    _rid = f"restore_id={_rw_restore_id} " if _rw_restore_id else ""
    workflow_start = time.monotonic()
    if logger:
        log_event(logger, level="logging", message=f"{_rid}restore-workflow start restore_mode={restore_mode}")
    try:
        # Preflight: validate (and if possible prepare) each target before touching any files.
        # Windows: checks UNC share, tries WinRM create, then admin-share fallback.
        # Linux: no-op — SSH/SFTP handles dir creation lazily.
        # run_target_preflight returns a patched config when admin-share fallback is used
        # (vm_import_unc overridden to admin share path). Scoped per restore_id.
        _preflighted: list[BackupRestoreConfig] = []
        with _workflow_phase(logger, f"{_rid}restore-workflow preflight", summary=summary, current_phase="preflight", restore_count=len(restore_configs)):
            for config in restore_configs:
                # The space check in here counts the chain the copy will take, and a point in time
                # has a chain of its own (0.26.0 §1.74). It leaves out what is already staged
                # unless the copy is forced, which writes those files again (§1.76).
                measured = (dataclasses.replace(config, copy_window_end_utc=point_in_time_utc)
                            if point_in_time_utc is not None else config)
                override = run_target_preflight(measured, logger=logger, recopy=force,
                                                dry_run=dry_run)
                _preflighted.append(override if override is not None else config)
        restore_configs = _preflighted

        # How far back each entry copies: the run's --copy-hours when one was given, else the entry's
        # own copy_recent_hours. Until 0.25.0 the flag defaulted to 24 and always won, so an entry's
        # copy_recent_hours - set, say, to reach a weekly FULL - was read, parsed and never used
        # (the 100.250 restore on the 0.25.0 soak copied 24 h of LOGs and no FULL).
        def _hours(config: BackupRestoreConfig) -> int:
            return int(copy_hours) if copy_hours is not None else int(config.copy_recent_hours)

        def _window_start(config: BackupRestoreConfig):
            return point_in_time_window_start(point_in_time_utc, _hours(config))

        copy_window_end = point_in_time_utc if point_in_time_utc is not None else None
        copy_window_start = _window_start(restore_configs[0]) if restore_configs else None
        with _workflow_phase(
            logger,
            f"{_rid}restore-workflow calculating-copy-window",
            summary=summary,
            current_phase="calculating-copy-window",
            restore_mode=restore_mode,
            copy_hours=_hours(restore_configs[0]) if restore_configs else copy_hours,
            window_start_utc=copy_window_start.isoformat() if copy_window_start is not None else None,
            window_end_utc=copy_window_end.isoformat() if copy_window_end is not None else None,
        ):
            pass
        copy_outputs = []
        if dry_run:
            # **A dry run copies nothing** (0.27.0 item 1.93). It ran the whole copy until then -
            # tens of GB over SMB onto the target - and rehearsed only the restore. The space check
            # in the preflight has already said what the copy would write; the restore below is
            # rehearsed on what is staged now.
            if logger:
                log_event(logger, level="logging",
                          message=f"{_rid}restore-workflow dry run: the copy is not run")
            summary["copy-backup"] = {"status": "DRY_RUN", "sources": []}
        # Every step in the flow format a backup's messages have (0.39.0, 1.370): the id and the step on
        # the first line, a line per fact - the share-driven path sent the older key=value shape.
        _flow_id = str(restore_configs[0].restore_id or restore_configs[0].source_id) if restore_configs else ""
        if not dry_run and say:
            say.copy("start")
        with _workflow_phase(logger, f"{_rid}restore-workflow copy-backup", summary=summary, current_phase="copy-backup", source_count=len(restore_configs)):
            for config in ([] if dry_run else restore_configs):
                copy_window_start = _window_start(config)
                step_config = dataclasses.replace(
                    config,
                    copy_recent_hours=_hours(config),
                    copy_window_start_utc=copy_window_start,
                    copy_window_end_utc=copy_window_end,
                )
                result = run_copy_backup(step_config, logger=logger, force=force, catalog=catalog)
                if result.restore_run_id is not None:
                    gated_runs.append((result.restore_run_id, bool((result.integrity or {}).get("proven"))))
                run_ids.append(result.restore_run_id)
                # What the row cannot know at the gate: the moment asked for, and the scheduler's row.
                record_run(catalog, result.restore_run_id, job_run_id=job_run_id,
                           point_in_time=point_in_time_utc.isoformat() if point_in_time_utc is not None
                           and hasattr(point_in_time_utc, "isoformat") else "LATEST")
                copy_outputs.append(
                    {
                        "source_id": step_config.source_id,
                        "returncode": result.returncode,
                        "restore_run_id": result.restore_run_id,
                        "integrity": result.integrity,
                        "copy_recent_hours": step_config.copy_recent_hours,
                        "copy_window_start_utc": copy_window_start.isoformat() if copy_window_start is not None else None,
                        "copy_window_end_utc": copy_window_end.isoformat() if copy_window_end is not None else None,
                        "files_considered": result.files_considered,
                        "copied": result.copied,
                        "skipped": result.skipped,
                    }
                )
                if result.returncode != 0:
                    raise errors.OperationFailed(f"copy-backup failed for source_id={step_config.source_id} returncode={result.returncode}")
                if result.files_considered == 0:
                    raise errors.OperationFailed(
                        "copy-backup selected no files "
                        f"for source_id={step_config.source_id} restore_id={step_config.restore_id or 'unknown'} "
                        f"window_start_utc={copy_window_start.isoformat() if copy_window_start is not None else 'now-minus-hours'} "
                        f"window_end_utc={copy_window_end.isoformat() if copy_window_end is not None else 'unbounded'}"
                        + newest_backup_hint(step_config)
                    )
        if not dry_run:
            summary["copy-backup"] = {"status": "SUCCESS", "sources": copy_outputs}
            _copied = sum(int(o.get("copied") or 0) for o in copy_outputs)
            _skipped = sum(int(o.get("skipped") or 0) for o in copy_outputs)
            if say:
                say.copy("done", transferred={"copied": _copied, "skipped": _skipped})

        if copy_only:
            # Copied and proven, and nothing restored: the staged chain is what this run is for.
            # Its cleanup is left alone - a copy-only target keeps every chain it was given until
            # the keep-the-newest-chain rule exists (plan D8).
            summary["status"] = "DRY_RUN" if dry_run else "SUCCESS"
            summary["overall_workflow_status"] = summary["status"]
            summary["copy_only"] = True
            summary["duration_seconds"] = round(time.monotonic() - workflow_start, 3)
            _close_catalog_runs(catalog, gated_runs, phase="copied", error_text="")
            if logger:
                log_event(logger, level="logging",
                          message=f"{_rid}restore-workflow copy only - nothing restored status={summary['status']} "
                                  f"elapsed_seconds={summary['duration_seconds']}")
            return summary

        # Instance metadata before the databases (step 3), after the copy and before the certificate:
        # the order the operator confirmed (2026-11-16). Never fatal - the databases are the deliverable.
        if replay_metadata and not dry_run:
            summary["server_metadata"] = {}
            for config in restore_configs:
                summary["server_metadata"].update(_share_metadata(config, instance_bundle.PRE_DATABASE, say))
        with _workflow_phase(logger, f"{_rid}restore-workflow restore-preparation", summary=summary, current_phase="restore-preparation", source_count=len(restore_configs)):
            restore_inputs = list(restore_configs)
        for run_id in run_ids:
            record_run(catalog, run_id, phase="restoring")
        _restore_started = time.monotonic()
        with _workflow_phase(logger, f"{_rid}restore-workflow restore-execution", summary=summary, current_phase="restore-execution", source_count=len(restore_inputs)):
            # The chain each source's restore applies, on that source's copy run (1.367): run_ids is in
            # the order of restore_configs, one per source; a dry run copied nothing and records nothing.
            chains = [ChainRecord(catalog, run_id, str(config.source_id), str(config.target_id))
                      if catalog is not None and run_id is not None and not dry_run else None
                      for config, run_id in zip(restore_inputs, run_ids + [None] * (len(restore_inputs) - len(run_ids)))]
            restore_outputs = [
                restore_share_server(config=config, dbabrain_config=app_config, dry_run=dry_run, logger=logger,
                                     point_in_time_utc=point_in_time_utc, **({"chain": chain} if chain else {}),
                                     **({"say": say} if say is not None and not dry_run else {}))
                for config, chain in zip(restore_inputs, chains)
            ]
        restore_counts = _count_restore_statuses(restore_outputs)
        failed_databases = _failed_restore_databases(restore_outputs)
        summary["restore-latest"] = {
            # Computed, not asserted. This said `SUCCESS` whenever no exception escaped, while the
            # `failed` count directly below it was already right - so on 2026-09-15 a database left
            # mid-restore by `Msg 5149` was recorded FAILED in `backup_restore_history` and
            # announced to the group as `status=done`. The count was never the missing part.
            "status": "DRY_RUN" if dry_run else ("FAILED" if failed_databases else "SUCCESS"),
            "sources": restore_outputs,
        }
        summary.update(restore_counts)
        per_restore_results = _build_per_restore_results(restore_configs, restore_outputs)
        summary["per_restore_results"] = per_restore_results
        if failed_databases:
            # Raised rather than returned, so the one `except` below sets the status, the error tail
            # and the alert - the shape `delete-backup` already uses for a non-zero return code.
            #
            # **And this deliberately stops before the retention cleanup.** A restore drill is what
            # proves the backups are restorable; pruning them in the same run that failed to restore
            # one is exactly backwards. Do not "fix" the early return.
            raise errors.OperationFailed(f"{_restore_failure_text(restore_outputs, failed_databases)} "
                               "Retention cleanup was not run.")

        # **The last question, and the only one that matters to whoever asked for the restore:
        # can the database be opened?** Every step above reports on the command it ran, and a
        # command can return having left a database mid-restore - which is exactly what happened on
        # 2026-09-15, when `Msg 5149` stopped a restore and the run still announced `status=done`.
        # `verify-restore` runs a real query rather than reading a state column, because a database
        # can read ONLINE and still refuse one while it finishes an upgrade step.
        #
        # `restore_by_id` has planned this as its last step for all three engines since it was
        # written; this path never called it. So the check existed and the nightly job skipped it.
        verify_outputs: list[dict[str, object]] = []
        if not dry_run:
            for run_id in run_ids:
                record_run(catalog, run_id, phase="verifying_db")
            if say:
                say.check("start")
            with _workflow_phase(logger, f"{_rid}restore-workflow verify-restore", summary=summary,
                                 current_phase="verify-restore", source_count=len(restore_configs)):
                try:
                    verify_secrets = _load_secrets(data_dir=None, key=None, key_base64=None)
                except Exception as exc:  # noqa: BLE001 - no key is a state, not a broken restore
                    # The restore itself got its credentials some other way (a generated batch, an
                    # ssh session). Failing the whole run because *this* check could not read the
                    # store would make the check more fragile than the work it is checking.
                    verify_secrets = {}
                    _verify_unavailable = sanitize_text(str(exc))
                else:
                    _verify_unavailable = ""
                stopped_short: list[str] = []
                # Each entry with its own restore's answer: one that states restore_all_databases is
                # checked on every database its run restored, by the backup's name (1.101).
                for position, (config, restore_output) in enumerate(zip(restore_inputs, restore_outputs)):
                    request, why_not = _verify_plan(config, verify_secrets,
                                                    _restored_databases(restore_output))
                    if request is None:
                        verify_outputs.append({
                            "restore_id": config.restore_id, "target_id": config.target_id,
                            "status": "SKIPPED",
                            "reason": _verify_unavailable or why_not,
                        })
                        continue
                    outcome = common_cli.run("verify-restore", request)
                    # The entry's own row says how many of its databases opened (1.214).
                    _checked_here = int(outcome.get("checked") or 0)
                    _failed_here = int(outcome.get("failed") or 0)
                    record_run(catalog, run_ids[position] if position < len(run_ids) else None,
                               databases_ok=max(_checked_here - _failed_here, 0), databases_failed=_failed_here)
                    # Where each database stopped, as the TARGET recorded it, against what its restore
                    # asked (0.39.0, 1.367) - the repository path's rule on this path too: a database
                    # that opens and checks clean can still be hours short of what was asked.
                    landed, off_target = _held_to_the_plan(restore_output, outcome)
                    if landed:
                        record_run(catalog, run_ids[position] if position < len(run_ids) else None,
                                   landed_json=json.dumps(landed, default=str))
                    stopped_short.extend(f"{config.restore_id}: {line}" for line in off_target)
                    verify_outputs.append({
                        "restore_id": config.restore_id, "target_id": config.target_id,
                        "status": "SUCCESS" if outcome.get("ok") and not off_target else "FAILED",
                        "checked": outcome.get("checked"), "failed": outcome.get("failed"),
                        "databases": outcome.get("databases"), "landed": landed,
                    })
            unusable = [
                str(db.get("database_name"))
                for item in verify_outputs
                for db in (item.get("databases") or [])
                if isinstance(db, dict) and not db.get("ok")
            ]
            _checked = sum(int(item.get("checked") or 0) for item in verify_outputs)
            _skipped_entries = sum(1 for item in verify_outputs if item.get("status") == "SKIPPED")
            summary["verify-restore"] = {
                "status": "FAILED" if unusable else "SUCCESS",
                "checked": _checked, "failed": len(unusable),
                "skipped_entries": _skipped_entries, "targets": verify_outputs,
            }
            if say:
                say.check("done", checked=_checked, failed=len(unusable))
            if unusable:
                # Same reason the failed-restore count raises: the retention cleanup must not prune
                # the backups when the drill that proves them restorable has just failed.
                raise errors.OperationFailed(
                    f"restore finished but {len(unusable)} database(s) cannot be opened: "
                    f"{', '.join(sorted(unusable))}. Retention cleanup was not run.")
            if stopped_short:
                # Open and consistent, and not where it was asked to be: a success that is not one.
                raise errors.OperationFailed(
                    "restored and checked, but not to what was asked - " + "; ".join(stopped_short)
                    + ". Retention cleanup was not run.")

        # Instance metadata after the databases (step 9) - only after a restore that worked and was
        # checked: Agent job steps name databases that have to exist.
        if replay_metadata and not dry_run:
            for config in restore_configs:
                summary.setdefault("server_metadata", {}).update(
                    _share_metadata(config, instance_bundle.POST_DATABASE, say))
        delete_outputs = []
        # Announced like every other phase. Until 2026-09-11 the retention cleanup was the one
        # phase that wrote nothing to the store: it logged to a file inside the container and
        # nowhere else, so across 254 recorded workflow runs there was not a single row saying
        # whether it had ever pruned anything. "Is retention running?" had no answer short of
        # shelling into the worker and grepping.
        with _workflow_phase(logger, f"{_rid}restore-workflow delete-backup", summary=summary, current_phase="delete-backup", source_count=len(restore_configs), cleanup_retention=delete_retention):
            for config in restore_configs:
                # Seconds the whole way down now. This used to convert the configured seconds into
                # hours with `max(1, seconds // 3600)` before handing them over, which turned any
                # retention under an hour into an hour without saying so.
                step_config = (config if delete_retention is None
                               else dataclasses.replace(config, cleanup_retention=delete_retention))
                result = run_delete_backup(step_config, logger=logger, dry_run=dry_run)
                delete_outputs.append(
                    {
                        "source_id": step_config.source_id,
                        "returncode": result.returncode,
                        "cleanup_retention": result.cleanup_retention,
                        "files_considered": result.files_considered,
                        "deleted": result.deleted,
                        "skipped": getattr(result, "skipped", 0),
                    }
                )
                if result.returncode != 0:
                    raise errors.OperationFailed(f"delete-backup failed for source_id={step_config.source_id} returncode={result.returncode}")
        _deleted = sum(int(item.get("deleted") or 0) for item in delete_outputs)
        _considered = sum(int(item.get("files_considered") or 0) for item in delete_outputs)
        _skipped = sum(int(item.get("skipped") or 0) for item in delete_outputs)
        # The counts are the point: "SUCCESS" on its own cannot distinguish a cleanup that pruned
        # thirteen files from one that scanned nothing, and both had been reported identically.
        if say:
            say.plain({"plain": None, "pruned": {"pruned": _deleted, "skipped": f"{_considered} considered, "
                                                                          f"{_skipped} kept"}, "kept": 0})
        summary["delete-backup"] = {"status": "SUCCESS", "sources": delete_outputs,
                                    "files_considered": _considered, "deleted": _deleted,
                                    "skipped": _skipped}
        summary["status"] = "SUCCESS"
        summary["overall_workflow_status"] = "SUCCESS"
        summary["duration_seconds"] = round(time.monotonic() - workflow_start, 3)
        _close_catalog_runs(catalog, gated_runs, phase="done", error_text="")
        if logger:
            log_event(logger, level="logging", message=f"{_rid}restore-workflow completion status=SUCCESS elapsed_seconds={summary['duration_seconds']}")
            log_event(logger, level="logging", message=f"{_rid}{_format_restore_workflow_stdout(summary)}")
        return summary
    except Exception as exc:
        summary["status"] = "FAILED"
        summary["overall_workflow_status"] = "FAILED"
        summary["duration_seconds"] = round(time.monotonic() - workflow_start, 3)
        summary["exception_type"] = type(exc).__name__
        summary["exception_message"] = sanitize_text(str(exc))
        summary["error_tail"] = summarize_error_tail(str(exc))
        _close_catalog_runs(catalog, gated_runs, phase="error",
                            error_text=sanitize_text(f"{type(exc).__name__}: {exc}")[:2000])
        if logger:
            log_event(logger, level="critical", message=sanitize_text(f"{_rid}restore-workflow failed error={type(exc).__name__}: {exc}"))
        raise RestoreWorkflowError(str(exc), metadata=summary) from exc
