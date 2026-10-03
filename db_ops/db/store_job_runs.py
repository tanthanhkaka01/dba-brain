"""``DbOpsStore``'s methods for job runs, the runtime nodes, and their archive.

Split out of ``db/store.py`` on 2026-10-03 (see ``db/store_base.py``). A mixin, not a store:
``DbOpsStore`` composes it, and its methods use the store's own ``connect`` and ``initialize``.
"""

from __future__ import annotations

import json
import sqlite3
import socket
from typing import Any
from datetime import datetime, timedelta, timezone

from db_ops.db.job_runs import JobRun
from db_ops.lib import node_identity, run_claim
from db_ops.db import backend as backend_mod
from db_ops.db.store_base import RunAlreadyClaimed, _JOB_RUN_ARCHIVE_COLUMNS, _archive_requests_for_runs, _claim_pid, _claim_started, utc_now_text


class JobRunsMixin:

    def insert_job_run(self, item: JobRun) -> int:
        """Append a run row. A ``RUNNING`` row is a **claim** and may be refused.

        Raises :class:`RunAlreadyClaimed` when ``ux_job_runs_claim`` says this job_code is already
        running on this host. That is an answer, not a failure: a second restore started 47 minutes
        into the first on 2026-09-14 because the only thing standing between them was a SELECT.
        """
        self.initialize()
        created_at = utc_now_text()
        # A row claims its key only when it says which key, and only while it is running. An
        # `async` app command deliberately passes none: the daemon is meant to start it again
        # while the first is still going, and what must not run twice is the work inside it.
        claim_key = (item.claim_key or "").strip() or None
        running = str(item.status or "").strip().lower() == "running" and claim_key is not None
        metadata = dict(item.metadata or {})
        host_name = item.host_name or socket.gethostname()
        if running:
            metadata.update(run_claim.claim_fields(pid=_claim_pid(metadata), host=host_name,
                                                   node=node_identity.current(),
                                                   started=_claim_started(metadata)))
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO job_runs
                    (
                        created_at,
                        started_at,
                        finished_at,
                        job_code,
                        level,
                        status,
                        message,
                        duration_ms,
                        error_text,
                        host_name,
                        claim_key,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        created_at,
                        item.started_at,
                        item.finished_at,
                        item.job_code,
                        item.level,
                        item.status,
                        item.message,
                        item.duration_ms,
                        item.error_text,
                        host_name,
                        claim_key,
                        metadata_json,
                    ),
                )
                return int(cursor.lastrowid)
        except Exception as exc:  # noqa: BLE001 - re-raised unless the claim index refused it.
            if running and backend_mod.is_unique_violation(exc):
                raise RunAlreadyClaimed(
                    f"{item.job_code} is already running on {host_name}") from None
            raise

    def fetch_recent_job_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        log_id,
                        created_at,
                        job_code,
                        level,
                        status,
                        message,
                        duration_ms
                    FROM job_runs
                    ORDER BY log_id DESC
                    LIMIT ?;
                    """,
                    (limit,),
                )
            )

    def fetch_terminal_job_runs(
        self, *, job_codes: list[str], since_created_at: str, limit: int = 50
    ) -> list[sqlite3.Row]:
        """Job-run rows whose ``job_code`` is one of ``job_codes`` and were created at or
        after ``since_created_at`` (ISO string), newest first. Used to detect a background
        command's authoritative completion from SQLite (e.g. the
        ``backup_restore.restore-workflow.end`` / ``.error`` record) instead of relying on
        the detached process staying alive and its stdout marker."""
        self.initialize()
        codes = [c for c in job_codes if c]
        if not codes:
            return []
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT log_id, created_at, job_code, status, message, error_text, metadata_json
                    FROM job_runs
                    WHERE job_code IN ({placeholders})
                      AND created_at >= ?
                    ORDER BY log_id DESC
                    LIMIT ?;
                    """,
                    (*codes, since_created_at, limit),
                )
            )

    def job_run_started_since(self, job_code: str, since: str) -> bool:
        """Whether a run of ``job_code`` started at or after ``since``.

        One key, one indexed probe (``ix_job_runs_job_code_created_at``): asked before every claim
        of a scheduled backup or restore, so it must not read every job's latest run the way
        :meth:`fetch_latest_job_runs_by_job_code` does. See ``backup_restore.schedule.taken_since``.
        """
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM job_runs WHERE job_code = ? AND started_at >= ? LIMIT 1",
                (job_code, since),
            ).fetchone()
        return row is not None

    def fetch_latest_job_runs_by_job_code(self) -> dict[str, sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    log_id,
                    created_at,
                    started_at,
                    finished_at,
                    job_code,
                    level,
                    status,
                    message,
                    duration_ms,
                    error_text,
                    host_name,
                    metadata_json
                FROM job_runs
                WHERE log_id IN (
                    SELECT max(log_id)
                    FROM job_runs
                    GROUP BY job_code
                );
                """
            ).fetchall()
        return {str(row["job_code"]): row for row in rows}

    def fetch_running_job_runs(self, job_code_prefix: str = "") -> list[sqlite3.Row]:
        """Every row still marked RUNNING, optionally limited to one job_code prefix.

        Unlike :meth:`fetch_latest_job_runs_by_job_code` this returns *all* of them: once a
        stale run has been overtaken by a newer one it is no longer the latest row for its
        job_code, but it is still open and still needs closing.
        """
        self.initialize()
        sql = """
            SELECT
                log_id,
                created_at,
                started_at,
                finished_at,
                job_code,
                level,
                status,
                message,
                duration_ms,
                error_text,
                host_name,
                metadata_json
            FROM job_runs
            WHERE lower(status) = 'running'
        """
        params: tuple = ()
        if job_code_prefix:
            sql += " AND job_code LIKE ?"
            params = (f"{job_code_prefix}%",)
        sql += " ORDER BY log_id;"
        with self.connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    def update_job_run(
        self,
        *,
        log_id: int,
        level: str,
        status: str,
        message: str,
        finished_at: str | None = None,
        duration_ms: int | None = None,
        error_text: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.initialize()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE job_runs
                SET
                    finished_at = ?,
                    level = ?,
                    status = ?,
                    message = ?,
                    duration_ms = ?,
                    error_text = ?,
                    metadata_json = ?
                WHERE log_id = ?;
                """,
                (
                    finished_at,
                    level,
                    status,
                    message,
                    duration_ms,
                    error_text,
                    metadata_json,
                    log_id,
                ),
            )

    def record_runtime_node(
        self,
        *,
        node_id: str,
        node_role: str,
        hostname: str,
        timezone_name: str,
        utc_offset_minutes: int,
        tz_abbreviation: str = "",
        app_version: str = "",
    ) -> None:
        """Record which clock this node runs on. One row per node, upserted forever.

        Written by ``python -m db_ops.common.cli timezone '{"record": true}'`` — never implicitly
        by an app, because a store write that happens as a side effect of rendering a timestamp is
        a store write nobody can find.

        The reason it exists: master and worker share one PostgreSQL store and each reads its own
        ``config.json``. Nothing in the store could say whether the two agreed about the hour, so a
        ``time_window`` that fired at the wrong time on one of them looked exactly like a schedule
        that had never been due.

        ``utc_offset_minutes`` is a **snapshot true as of ``updated_at``** and ``timezone`` is the
        setting. Both are kept because under daylight saving the first changes twice a year, and
        only the second can be written back into a config file — neither derives from the other.

        ``first_seen_at`` survives the upsert: when a node first reported is history, and an
        upsert that reset it would erase the only record of it.
        """
        self.initialize()
        now = utc_now_text()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_nodes
                    (node_id, node_role, hostname, timezone, utc_offset_minutes,
                     tz_abbreviation, app_version, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    node_role = excluded.node_role,
                    hostname = excluded.hostname,
                    timezone = excluded.timezone,
                    utc_offset_minutes = excluded.utc_offset_minutes,
                    tz_abbreviation = excluded.tz_abbreviation,
                    app_version = excluded.app_version,
                    updated_at = excluded.updated_at;
                """,
                (
                    str(node_id),
                    str(node_role),
                    str(hostname),
                    str(timezone_name),
                    int(utc_offset_minutes),
                    str(tz_abbreviation or ""),
                    str(app_version or ""),
                    now,
                    now,
                ),
            )

    def list_runtime_nodes(self) -> list[dict[str, Any]]:
        """Every node that has ever reported, newest report first.

        The answer to "is the estate on one clock?" — which is a question, not a fact, the moment
        there is more than one node.
        """
        self.initialize()
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                """
                SELECT node_id, node_role, hostname, timezone, utc_offset_minutes,
                       tz_abbreviation, app_version, first_seen_at, updated_at
                FROM runtime_nodes
                ORDER BY updated_at DESC, node_id ASC;
                """
            ).fetchall()]

    def archive_old_job_runs(
        self,
        *,
        retention_days: int = 15,
        batch_size: int = 20000,
        max_batches: int | None = None,
    ) -> int:
        """Move ``job_runs`` rows older than ``retention_days`` into ``job_runs_history``.

        Rows are kept, not deleted — the same trade ``metric_results`` makes. Returns how many
        moved.

        Batched, because the first sweep on an unpruned store has months to clear (~1M rows /
        965 MB here) and one transaction that size holds locks on the table the daemon appends
        to on every single app-command start and finish. Each batch is its own transaction, so
        an interrupted sweep leaves a consistent store and the next pass carries on.

        ``max_batches`` caps one call to bounded work. The daemon needs that: it sweeps from the
        same loop that starts due app commands, so an uncapped first sweep would stall scheduling
        for as long as it took to move eight hundred thousand rows. Capped, the backlog drains
        over successive passes, and steady state (~13k rows/day age out here) clears inside the
        first batch every time.

        **The console's requests move with the runs they name**, in the same transaction and
        before them. ``app_command_requests.job_run_id`` is a foreign key with no ``ON DELETE``,
        so one finished request pointing into the batch made the delete fail — and the daemon
        swallows a failed sweep by design, so the busiest table in the store stopped pruning and
        said so only in one log line per interval. Found on 2026-09-04 on a live daemon:
        ``23503 … Key (log_id)=(1601189) is still referenced from table "app_command_requests"``.
        """
        self.initialize()
        days = max(int(retention_days), 0)
        if days <= 0:
            return 0
        # Before the first batch, never inside one. Creating the table here costs one catalogue
        # read per sweep; creating it inside the copy/delete transaction would COMMIT that batch's
        # INSERT halfway through - `executescript` commits on both backends - and copy+delete
        # being one transaction is what stops a row being archived twice.
        self._ensure_request_history_table()
        # The cutoff is computed here and bound as a parameter. Inlining SQLite's three-argument
        # strftime('%Y-...','now','-N days') is what took down every metrics and SLA run seconds
        # after the store was switched to PostgreSQL, where that function does not exist; the
        # translator only rewrites the two-argument UTC-now form. A guard test enforces this.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        moved = 0
        batches = 0
        while True:
            if max_batches is not None and batches >= int(max_batches):
                return moved
            batches += 1
            archived_at = utc_now_text()
            with self.connect() as conn:
                ids = [
                    int(row["log_id"])
                    for row in conn.execute(
                        "SELECT log_id FROM job_runs WHERE created_at < ? ORDER BY log_id LIMIT ?",
                        (cutoff, int(batch_size)),
                    ).fetchall()
                ]
                if not ids:
                    return moved
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"INSERT INTO job_runs_history ({_JOB_RUN_ARCHIVE_COLUMNS}, archived_at) "
                    f"SELECT {_JOB_RUN_ARCHIVE_COLUMNS}, ? FROM job_runs "
                    f"WHERE log_id IN ({placeholders})",
                    (archived_at, *ids),
                )
                _archive_requests_for_runs(conn, ids, placeholders, archived_at)
                conn.execute(f"DELETE FROM job_runs WHERE log_id IN ({placeholders})", tuple(ids))
            moved += len(ids)
            if len(ids) < int(batch_size):
                return moved

    def _ensure_request_history_table(self) -> None:
        """Create ``app_command_requests_history`` when a store has requests but no archive.

        ``RunRequestStore`` owns both tables, but it only builds them when the console runs, and
        the daemon's sweep does not run the console. So every store upgraded from a build that
        predates the archive has the requests table, the foreign key, and **no** archive — which
        is precisely the state in which :func:`_archive_requests_for_runs` reported "nothing to
        move" and the delete went on failing. Measured 2026-09-04 on both live stores:
        ``app_command_requests`` present, ``app_command_requests_history`` absent, on the
        production PostgreSQL one and on a brand-new SQLite one alike.

        Only when the requests table exists: a store whose console was never used has neither
        table and needs neither. Nothing here is backend-specific — ``executescript`` translates
        the DDL per statement on PostgreSQL.
        """
        from db_ops.db.run_requests import APP_COMMAND_REQUESTS_HISTORY_SQL

        with self.connect() as conn:
            if not backend_mod.table_exists(conn, "app_command_requests"):
                return
            if backend_mod.table_exists(conn, "app_command_requests_history"):
                return
            conn.executescript(APP_COMMAND_REQUESTS_HISTORY_SQL)
