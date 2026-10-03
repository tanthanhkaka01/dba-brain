"""What every part of the runtime store shares: run claims, the archive columns, the clock text.

Split out of ``db/store.py`` on 2026-10-03 (Q11, `audits/20261002_audit_typed_requests_and_errors.md`
section 5): the store was one 3,400-line module, and the table families now live beside it -
``store_job_runs``, ``store_telegram``, ``store_reports``, ``store_sql_runs`` - with the schema in
``store_schema``. ``db/store.py`` re-exports every name, so no import changes.
"""

from __future__ import annotations

from db_ops.lib import errors
import os
from typing import Any
from datetime import datetime, timezone

from db_ops.lib import process_liveness
from db_ops.db import backend as backend_mod


#: 2 — added telegram_send_messages.message_type and the job_runs_history archive table.
#: 3 — added runtime_nodes: which clock each node in the cluster is actually running on. A master
#:     and a worker share one store and can hold different config.json timezones, so "what time is
#:     this row in" had no answer the store could give.
#: 4 — added telegram_workflow_steps: what the operator was asked, what came back, and which step
#:     of a conversation is live. The conversation state row says only what a run is waiting for,
#:     and Back needs the history it was throwing away.
def _claim_pid(metadata: dict) -> int:
    """Whose process owns this run.

    ``metadata["pid"]`` when the row already names one — the daemon records the **child** it just
    started, and the child is what is doing the work, so a daemon that is restarted while its child
    survives must not be able to take the claim back and start a second one. Otherwise this
    process, which is the one running the work.
    """
    raw = (metadata or {}).get("pid")
    try:
        return int(raw) if raw is not None else os.getpid()
    except (TypeError, ValueError):
        return os.getpid()


def _claim_started(metadata: dict) -> str:
    """When the claiming process started, or ``""`` when that cannot be read.

    Written beside the pid so that whoever later has to stop this run can tell its process from a
    newer one holding the same number (``run_claim.STARTED_FIELD``). Never a reason to fail a claim.
    """
    try:
        return process_liveness.process_start_marker(_claim_pid(metadata)) or ""
    except Exception:  # noqa: BLE001 - a marker that cannot be read is simply not recorded.
        return ""


class RunAlreadyClaimed(errors.Refused):
    """Someone else already holds the ``running`` row for this key.

    Raised where a claim is refused by ``ux_sql_runs_claim`` / ``ux_job_runs_claim``. It is not a
    failure and must never be reported as one: it is the answer "that task is already running",
    arriving from the only place that can answer it without a race.
    """


SCHEMA_VERSION = 5

#: Columns copied verbatim when a job_runs row ages into job_runs_history. Listed rather than
#: `SELECT *` so a future column added to job_runs fails loudly here instead of silently
#: dropping out of the archive.
_JOB_RUN_ARCHIVE_COLUMNS = (
    "log_id, created_at, started_at, finished_at, job_code, level, status, message, "
    "duration_ms, error_text, host_name, metadata_json"
)

#: The same, for the console's "run this now" requests. They carry a real foreign key to
#: ``job_runs (log_id)``, so they have to move in the same transaction as the run they name — see
#: :meth:`DbOpsStore.archive_old_job_runs`.
_APP_COMMAND_REQUEST_ARCHIVE_COLUMNS = (
    "request_id, app_command_id, status, requested_by, request_source, requested_at, "
    "claimed_at, started_at, finished_at, job_run_id, note"
)


def _archive_requests_for_runs(conn: Any, ids: list[int], placeholders: str,
                               archived_at: str) -> int:
    """Move the ``app_command_requests`` rows that point at *ids* into history. Returns how many.

    Called from inside :meth:`DbOpsStore.archive_old_job_runs`'s transaction, before the runs are
    deleted, because the foreign key is what makes the order matter.

    A store whose console has never been used has neither table — ``RunRequestStore`` creates
    them, not ``DbOpsStore`` — so their absence is normal and means there is nothing to move. It
    is checked rather than caught: an exception here would be indistinguishable from a real
    failure, and this runs inside the sweep whose failures are deliberately swallowed.
    """
    if not backend_mod.table_exists(conn, "app_command_requests"):
        return 0
    if not backend_mod.table_exists(conn, "app_command_requests_history"):
        return 0
    moved = conn.execute(
        f"SELECT COUNT(*) AS n FROM app_command_requests WHERE job_run_id IN ({placeholders})",
        tuple(ids),
    ).fetchone()
    count = int(moved["n"]) if moved is not None else 0
    if not count:
        return 0
    conn.execute(
        f"INSERT INTO app_command_requests_history "
        f"({_APP_COMMAND_REQUEST_ARCHIVE_COLUMNS}, archived_at) "
        f"SELECT {_APP_COMMAND_REQUEST_ARCHIVE_COLUMNS}, ? FROM app_command_requests "
        f"WHERE job_run_id IN ({placeholders})",
        (archived_at, *ids),
    )
    conn.execute(
        f"DELETE FROM app_command_requests WHERE job_run_id IN ({placeholders})", tuple(ids)
    )
    return count


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
