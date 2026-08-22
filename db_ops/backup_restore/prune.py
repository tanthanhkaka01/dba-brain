"""Deleting a backup entry's obsolete files, as its own scheduled step.

Separate from ``backup`` on purpose, and the separation is the point. Taking a backup and deleting
one are different risks with different failure modes: a backup that fails costs a run, a prune that
is wrong costs the thing the run produced. They also want different cadences — a full runs nightly
and needs pruning weekly at most — and folding the second into the first would tie them together
forever.

The split against ``common`` is the same one the backup half uses: this reads
``restore_config.json`` for the entry, ``db_instances.json`` for the SSH host behind a
``server_id``, and the secret store for the credential; :mod:`db_ops.common.backupfiles` is handed
a request and answers it. What comes back is recorded in ``job_runs`` and reported the same way a
backup run is, because an operator asking "did the cleanup run" is asking the same question about
the same estate.

**The backup scripts already prune their own directories** (``RETENTION_DAYS`` inside each
``assets/backup/**`` script). This exists for the directories nothing prunes — a Windows share, a
copy on a second host — and to make the decision inspectable before it happens: ``--dry-run`` lists
every file with the reason it is going, which the in-script cleanup cannot be asked for.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from db_ops.lib import common_cli
from db_ops.backup_restore.backup import _load_secrets, load_backup_jobs, resolve_backup_target
from db_ops.backup_restore.events import emit_backup_restore_event
from db_ops.lib.backupfiles_retention import AGE, DEFAULT_RETENTION_DAYS
from db_ops.db.job_runs import JobRun
from db_ops.db.store import DbOpsStore, utc_now_text

#: Entries whose engine this can list. SQL Server is asked through the instance rather than the
#: filesystem, which needs a login the prune request does not carry, so it is refused by name
#: rather than half-attempted.
LISTABLE = {"oracle", "postgresql", "postgres"}


def prune_job_request(job: Any, *, target: Any, secrets: dict[str, str] | None = None,
                      retention_days: int | None = None, mode: str = AGE, delete: bool = False,
                      dry_run: bool = False, data_dir: str | Path | None = None) -> dict[str, Any]:
    """The ``prune-backup-files`` request for one configured backup job.

    Built here and not in ``common`` because every value in it is a lookup: the directory comes
    from the entry, the host from the inventory, the credential from the secret store, and the
    retention from whatever the job already declares — the same number its own script prunes with,
    so the two cannot drift apart.
    """
    # The same two helpers the backup spec uses, so a prune reaches a host exactly the way the
    # backup that filled it did. Resolved here rather than left to `common`, which holds no
    # credentials and would otherwise be handed a key_file it cannot find.
    from db_ops.backup_restore.spec_builder import _resolved_key_file, _ssh_password

    return {
        "db_type": job.db_type,
        "path": job.backup_dir,
        "retention_days": int(retention_days if retention_days is not None
                              else (job.retention_days or DEFAULT_RETENTION_DAYS)),
        "mode": mode,
        "delete": bool(delete),
        "dry_run": bool(dry_run),
        "host": {
            "runtime": "docker" if target.container_name else "linux",
            "host": target.host,
            "port": target.port,
            "username": target.username,
            "container": target.container_name,
            "key_file": _resolved_key_file(target, data_dir),
            "password": _ssh_password(target, secrets or {}),
            # The db_ops user is not in the docker group on these hosts; the metrics collectors
            # and the backup scripts both reach docker through sudo for the same reason.
            "sudo": bool(target.container_name),
        },
    }


def run_prune(
    *,
    app_config: Any,
    config_path: str | Path,
    logger: Any = None,
    backup_id: str | None = None,
    job_name: str | None = None,
    retention_days: int | None = None,
    mode: str = AGE,
    data_dir: str | Path | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Prune the obsolete files of every selected backup entry.

    ``apply`` is what makes it delete. Without it the command answers and removes nothing — the
    opposite default from ``backup``, because reporting a backup that did not happen is a wasted
    run and reporting a deletion that did not happen is not.
    """
    jobs = [item for item in load_backup_jobs(config_path) if item.active]
    if backup_id:
        jobs = [item for item in jobs if item.backup_id == backup_id]
        if not jobs:
            raise ValueError(f"No active backup entry found with backup_id={backup_id}.")
    if job_name:
        jobs = [item for item in jobs if item.job == job_name]
        if not jobs:
            raise ValueError(f"No active backup job named {job_name}.")

    store = DbOpsStore.from_config(app_config)
    summary: dict[str, Any] = {
        "configured": len(jobs), "pruned": 0, "obsolete": 0, "deleted": 0, "failed": 0,
        "skipped": 0, "apply": bool(apply), "mode": mode, "jobs": [],
    }
    secrets = _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64)

    for item in jobs:
        engine = str(item.db_type or "").strip().lower()
        if engine not in LISTABLE:
            # Named rather than skipped quietly: an operator who configured a SQL Server entry and
            # sees nothing happen would reasonably conclude the prune ran and found nothing.
            summary["skipped"] += 1
            summary["jobs"].append({
                "job": item.label, "status": "skipped",
                "reason": f"{engine or 'unknown'} backups are listed through the instance, which "
                          "needs a login this command does not carry; prune it with "
                          "common.cli prune-backup-files and a target block.",
            })
            continue

        started_at = utc_now_text()
        # Every run brackets itself: exactly one START and exactly one terminal event. Prune only
        # ever emitted the END, so "how many prunes ran" was unanswerable from the messages, and a
        # prune that hung looked identical to one that was never due.
        emit_backup_restore_event(
            app_config=app_config, command="prune", phase="START", level="logging",
            message=f"Prune {item.label} started.", logger=logger, started_at=started_at,
            metadata={"backup_id": item.backup_id, "job": item.job}, notify=item.notify,
        )
        try:
            target = resolve_backup_target(item, data_dir=data_dir)
            request = prune_job_request(item, target=target, secrets=secrets,
                                        retention_days=retention_days, mode=mode,
                                        delete=bool(apply), dry_run=False, data_dir=data_dir)
            result = _prune_one(request, secrets=secrets)
        except Exception as exc:  # noqa: BLE001 - one entry must not stop the rest.
            summary["failed"] += 1
            summary["jobs"].append({"job": item.label, "status": "error", "error": str(exc)})
            _record(store, item, status="ERROR", started_at=started_at, message=str(exc),
                    app_config=app_config, logger=logger, level="error")
            continue

        obsolete = result["counts"]["obsolete"]
        deleted = int(((result.get("deleted") or {}).get("counts") or {}).get("deleted", 0))
        summary["pruned"] += 1
        summary["obsolete"] += obsolete
        summary["deleted"] += deleted
        summary["jobs"].append({
            "job": item.label, "status": "done", "path": item.backup_dir,
            "retention_days": result["retention_days"], "obsolete": obsolete, "deleted": deleted,
            "kept": result["counts"]["keep"],
        })
        message = (f"{item.label}: {obsolete} obsolete of {result['counts']['total']} at "
                   f"{result['retention_days']} days"
                   + (f", {deleted} deleted." if apply else ", nothing deleted (report only)."))
        _record(store, item, status="DONE", started_at=started_at, message=message,
                app_config=app_config, logger=logger,
                metadata={"obsolete": obsolete, "deleted": deleted})

    return summary


def _prune_one(request: dict[str, Any], *, secrets: dict[str, str]) -> dict[str, Any]:
    """List, judge, and (when the request says so) delete.

    The two halves are deliberately not the same kind of thing. Listing what is on the share and
    deleting from it are **operations on another machine**, so since 2026-08-15 they go through the
    ``common`` CLI like every other one; a failure there still raises, because
    :mod:`db_ops.lib.common_cli` turns ``success: false`` into an exception rather than
    letting it look like "nothing to delete".

    Deciding *which* files the retention window no longer covers is a pure function of the listing,
    and it stays an import: it is a rule about values, and a subprocess to apply arithmetic to a
    list would be the wrong shape at any speed.
    """
    from db_ops.lib.backupfiles_retention import plan_retention

    listed = common_cli.run("list-backup-files", request)
    plan = plan_retention(listed["files"], retention_days=request["retention_days"],
                          mode=request["mode"])
    if not request.get("delete") or not plan["obsolete_paths"]:
        return {**plan, "deleted": None}
    deleted = common_cli.run("delete-files", {
        "paths": plan["obsolete_paths"],
        "host": request["host"],
        # The entry's own directory is the only place this may remove anything from.
        "must_be_under": request["path"],
    })
    return {**plan, "deleted": deleted}


def _record(store: DbOpsStore, job: Any, *, status: str, started_at: str, message: str,
            app_config: Any, logger: Any, level: str = "logging",
            metadata: dict[str, Any] | None = None) -> None:
    """One ``job_runs`` row per entry, under its own job code.

    Its own code and not the backup's: a prune that failed must not look like a backup that failed,
    and the scheduler's "when did this last run" is a different question for each.
    """
    code = f"backup_restore.prune_job.{job.backup_id}.{job.job}"
    store.insert_job_run(JobRun(
        job_code=code, level=level, status=status, message=message,
        started_at=started_at, finished_at=utc_now_text(), host_name=socket.gethostname(),
        metadata={"backup_id": job.backup_id, "job": job.job, "server_id": job.server_id,
                  **(metadata or {})},
    ))
    emit_backup_restore_event(
        app_config=app_config, command="prune",
        # The terminal event says which way the run ended. Reporting a failure as END at error
        # level worked only because the level overrides the phase in message_type_for; saying
        # ERROR outright means the phase alone is readable.
        phase="END" if status == "DONE" else "ERROR",
        level=level,
        message=message, logger=logger, started_at=started_at,
        metadata={"backup_id": job.backup_id, "job": job.job}, notify=job.notify,
    )
