"""Shared scheduling + run recording for the backup_restore workflow.

Backup jobs and restore entries are scheduled by exactly the same rules, so those rules live
here once instead of being written twice and drifting. A unit of work has a ``time_window`` and
a ``job_code``; the *last run of any status* decides when it runs again, which is why every run
is written to ``job_runs`` before and after it executes - the row is not just a log, it is the
state the next due check reads.

Same contract as sql_tasks, the daemon and metrics (``db_ops.lib.time_window``): window open,
not already running, ``repeat_interval`` after a success, ``retry_interval`` after a failure, and
a stale ``running`` recovered after its timeout.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from typing import Any

from db_ops.lib.coerce import as_utc_datetime
from db_ops.lib.time_window import TimeWindow, is_time_window_open, job_due
from db_ops.db.job_runs import JobRun
from db_ops.db.store import DbOpsStore, utc_now_text

# One namespace per kind of work, distinct from the ``backup_restore.<command>.<phase>`` codes
# emit_backup_restore_event writes: these rows carry schedule state and must not be mixed in.
BACKUP_JOB_PREFIX = "backup_restore.backup_job"
RESTORE_JOB_PREFIX = "backup_restore.restore_job"

# Matches the sql_tasks default: work with no repeat_interval is reconsidered every 5 minutes.
DEFAULT_REPEAT_SECONDS = 300


def backup_job_code(backup_id: str, job: str) -> str:
    return f"{BACKUP_JOB_PREFIX}.{backup_id}.{job}"


def restore_job_code(restore_id: str) -> str:
    return f"{RESTORE_JOB_PREFIX}.{restore_id}"


def is_due(
    *,
    job_code: str,
    time_window: TimeWindow,
    latest_runs: dict[str, Any],
    now: datetime | None = None,
    local_now: datetime | None = None,
) -> bool:
    """Whether one unit of work should run now."""
    now = now or datetime.now(timezone.utc)
    local_now = local_now or datetime.now().astimezone()
    if not is_time_window_open(time_window, local_now):
        return False
    latest = latest_runs.get(job_code)
    # A run still marked RUNNING is either in flight or died mid-way; job_due's stale handling
    # recovers it after the timeout rather than starting a second copy against the same database.
    retry_interval = (
        time_window.retry_interval if time_window.retry_interval is not None else time_window.repeat_interval
    )
    return job_due(
        last_run=run_time(latest),
        last_status=str(latest["status"]).lower() if latest else None,
        repeat_interval=time_window.repeat_interval,
        retry_interval=retry_interval,
        now=now,
        timeout=time_window.timeout,
        default_repeat=DEFAULT_REPEAT_SECONDS,
    )


def is_running(job_code: str, latest_runs: dict[str, Any]) -> bool:
    """Whether this unit of work has a run still open.

    ``--force`` means "ignore the schedule", not "ignore the run in flight". Those are
    different things and conflating them started two copies of the same restore against one
    database, each waiting on the other. A stale row is not covered here: closing it is
    :func:`reap_stale_runs`'s job, and until it does, a second run must not start.
    """
    latest = latest_runs.get(job_code)
    if latest is None:
        return False
    try:
        return str(latest["status"] or "").strip().lower() == "running"
    except (KeyError, IndexError, TypeError):
        return False


def run_time(row: Any) -> datetime | None:
    """When a ``job_runs`` row started, as an aware UTC datetime."""
    if row is None:
        return None
    for column in ("started_at", "created_at"):
        try:
            value = row[column]
        except (KeyError, IndexError, TypeError):
            continue
        parsed = as_utc_datetime(value)
        if parsed is not None:
            return parsed
    return None


# `_parse_utc` is `db_ops.lib.coerce.as_utc_datetime` since 2026-08-16 — the same nine lines
# also lived in `common/restore_drill.py`, one deciding whether a backup is due and the other
# whether a drill counts, which is not a rule that should have had two copies.


def start_run(*, store: DbOpsStore, job_code: str, message: str, metadata: dict[str, Any]) -> tuple[int, str]:
    """Record the RUNNING row that both the operator and the next due check read."""
    started_at = utc_now_text()
    log_id = store.insert_job_run(
        JobRun(
            job_code=job_code,
            level="logging",
            status="RUNNING",
            message=message,
            started_at=started_at,
            host_name=socket.gethostname(),
            metadata=metadata,
        )
    )
    return log_id, started_at


def reap_stale_runs(
    *,
    store: DbOpsStore,
    timeouts: dict[str, int | None],
    notify: dict[str, Any] | None = None,
    app_config: Any = None,
    logger: Any = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Close runs stuck at RUNNING past their ``time_window.timeout``, and report each one.

    Until now ``timeout`` only answered "may the next run start?" (:func:`is_due` feeds it to
    ``job_due`` as the stale grace). Nothing ever *closed* the abandoned row or told anyone:
    when a run died without raising — the daemon killing the process at its own timeout, a
    container restart, an OOM — the row stayed RUNNING forever and the operator got no
    message at all, because the code that emits the failure event never ran.

    This is that missing half. It only writes the run row and emits the event; it does not
    touch any process. Killing in-flight work stays with the daemon, which owns the
    processes — stopping a restore mid-``RESTORE DATABASE`` would leave the database
    RESTORING and is not something a bookkeeping pass should decide.

    ``timeouts`` maps ``job_code`` -> configured timeout in seconds. A job_code absent from
    it is left alone (its config is gone, so no timeout applies), and a timeout of 0 means
    "never time out" — the same convention ``time_window`` uses everywhere else.
    ``notify`` maps ``job_code`` -> that job's Telegram routing block, so a timeout alert
    follows the same per-job routing as the job's own events.
    """
    now = now or datetime.now(timezone.utc)
    reaped: list[dict[str, Any]] = []

    for prefix in (BACKUP_JOB_PREFIX, RESTORE_JOB_PREFIX):
        for row in store.fetch_running_job_runs(prefix):
            job_code = str(row["job_code"])
            timeout = timeouts.get(job_code)
            if not timeout:  # unknown job_code, or timeout=0 = never time out
                continue
            started = run_time(row)
            if started is None:
                continue
            elapsed = int((now - started).total_seconds())
            if elapsed < int(timeout):
                continue

            metadata = _row_metadata(row)
            message = (
                f"{_run_label(job_code)} timed out: no completion recorded after {elapsed}s "
                f"(timeout={int(timeout)}s). The run died without reporting — process killed, "
                f"container restarted, or host rebooted."
            )
            store.update_job_run(
                log_id=int(row["log_id"]),
                level="error",
                status="TIMEOUT",
                message=message,
                finished_at=utc_now_text(),
                duration_ms=elapsed * 1000,
                error_text=message,
                metadata={**metadata, "timeout_seconds": int(timeout), "elapsed_seconds": elapsed,
                          "reaped_by": socket.gethostname()},
            )
            if app_config is not None:
                _emit_timeout_event(
                    app_config=app_config, job_code=job_code, message=message,
                    metadata=metadata, elapsed=elapsed, timeout=int(timeout),
                    started_at=str(row["started_at"] or row["created_at"] or ""), logger=logger,
                    notify=(notify or {}).get(job_code),
                )
            reaped.append({"job_code": job_code, "elapsed_seconds": elapsed,
                           "timeout_seconds": int(timeout), **_run_ids(job_code, metadata)})
    return reaped


def _row_metadata(row: Any) -> dict[str, Any]:
    """The stored metadata of a run row — it carries the restore_id/backup_id we must report."""
    try:
        raw = row["metadata_json"]
    except (KeyError, IndexError, TypeError):
        return {}
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _run_ids(job_code: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """The id of the run, from its metadata or — if that is missing — from the job_code,
    which embeds it by construction (``backup_restore.restore_job.<restore_id>``)."""
    if job_code.startswith(BACKUP_JOB_PREFIX):
        suffix = job_code[len(BACKUP_JOB_PREFIX) + 1:]
        backup_id = str(metadata.get("backup_id") or suffix.rsplit(".", 1)[0] or "")
        return {"backup_id": backup_id}
    suffix = job_code[len(RESTORE_JOB_PREFIX) + 1:]
    return {"restore_id": str(metadata.get("restore_id") or suffix or "")}


def _run_label(job_code: str) -> str:
    ids = _run_ids(job_code, {})
    key, value = next(iter(ids.items()))
    return f"{key}={value}"


def _emit_timeout_event(
    *, app_config: Any, job_code: str, message: str, metadata: dict[str, Any],
    elapsed: int, timeout: int, started_at: str, logger: Any,
    notify: Any = None,
) -> None:
    # Imported here: events imports the config/store stack, and only this path needs it.
    from db_ops.backup_restore.events import emit_backup_restore_event

    is_backup = job_code.startswith(BACKUP_JOB_PREFIX)
    emit_backup_restore_event(
        app_config=app_config,
        # The run's own command, with TIMEOUT as the phase: a timeout is something that happened
        # *to* this run, not a different kind of run. Encoding it in the command was the other
        # half of the two-conventions problem - a step is a phase, never a command suffix.
        command="backup" if is_backup else "restore-workflow",
        phase="TIMEOUT",
        level="critical",
        message=message,
        logger=logger,
        started_at=started_at or None,
        finished_at=utc_now_text(),
        duration_ms=elapsed * 1000,
        error_text=message,
        metadata={**metadata, **_run_ids(job_code, metadata), "job_code": job_code,
                  "timeout_seconds": timeout, "elapsed_seconds": elapsed},
        notify=notify,
    )


def finish_run(
    *,
    store: DbOpsStore,
    log_id: int,
    status: str,
    message: str,
    duration_ms: int,
    error_text: str | None,
    metadata: dict[str, Any],
) -> str:
    finished_at = utc_now_text()
    store.update_job_run(
        log_id=log_id,
        level="logging" if status == "done" else "error",
        status=status.upper(),
        message=message,
        finished_at=finished_at,
        duration_ms=duration_ms,
        error_text=error_text,
        metadata=metadata,
    )
    return finished_at
