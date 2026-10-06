"""``DbOpsStore``'s methods for SQL task runs.

Split out of ``db/store.py`` on 2026-10-03 (see ``db/store_base.py``). A mixin, not a store:
``DbOpsStore`` composes it, and its methods use the store's own ``connect`` and ``initialize``.
"""

from __future__ import annotations

import json
import sqlite3
import socket

from db_ops.lib import node_identity, run_claim
from db_ops.db import backend as backend_mod
from db_ops.db.store_base import RunAlreadyClaimed, _claim_pid, _claim_started


class SqlRunsMixin:

    def insert_sql_run(
        self,
        *,
        run_key: str,
        sql_id: int,
        sql_code: str,
        target_no: int,
        server_id: str,
        db_type: str,
        service_name: str,
        instance_name: str,
        database_name: str | None,
        credential_name: str,
        status: str,
        level: str,
        message: str,
        started_at: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        self.initialize()
        running = str(status or "").strip().lower() == "running"
        metadata = dict(metadata or {})
        host_name = str(metadata.get("host_name") or socket.gethostname())
        if running:
            # The claim is the row: whoever this INSERT accepts owns the run. The pid and host go
            # in beside it so the next scan can ask whether the owner is still alive instead of
            # reaping the row on age and starting a second copy on top of it.
            metadata.update(run_claim.claim_fields(pid=_claim_pid(metadata), host=host_name,
                                                   node=node_identity.current(),
                                                   started=_claim_started(metadata)))
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO sql_runs
                    (
                        run_key,
                        sql_id,
                        sql_code,
                        target_no,
                        server_id,
                        db_type,
                        service_name,
                        instance_name,
                        database_name,
                        credential_name,
                        status,
                        level,
                        message,
                        started_at,
                        host_name,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        run_key,
                        sql_id,
                        sql_code,
                        target_no,
                        server_id,
                        db_type,
                        service_name,
                        instance_name,
                        database_name,
                        credential_name,
                        status,
                        level,
                        message,
                        started_at,
                        host_name,
                        metadata_json,
                    ),
                )
                return int(cursor.lastrowid)
        except Exception as exc:  # noqa: BLE001 - re-raised unless the claim index refused it.
            if running and backend_mod.is_unique_violation(exc):
                raise RunAlreadyClaimed(
                    f"{run_key} is already running on {host_name}") from None
            raise


    def update_sql_run(
        self,
        *,
        sql_run_id: int,
        status: str,
        level: str,
        message: str,
        finished_at: str | None = None,
        duration_ms: int | None = None,
        row_count: int | None = None,
        result: dict | list | None = None,
        error_text: str | None = None,
        metadata: dict | None = None,
        only_if_status: str | None = None,
    ) -> bool:
        """Write a run's outcome; True when the row was changed.

        ``only_if_status`` makes the write a claim: it lands only while the row still has that
        status, so of two processes closing the same run exactly one is told it did. The stale-run
        reaper needs that - ``APP-SQL_TASKS`` runs ten scans at once, each reads the same ``running``
        row, and each closed it and sent its alert, so one dead run was reported twice (2026-09-26).
        """
        self.initialize()
        result_json = json.dumps(result if result is not None else {}, ensure_ascii=False, sort_keys=True)
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        values = [status, level, message, finished_at, duration_ms, row_count, result_json,
                  error_text, metadata_json, sql_run_id]
        guard = ""
        if only_if_status is not None:
            guard = " AND status = ?"
            values.append(only_if_status)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sql_runs
                SET
                    status = ?,
                    level = ?,
                    message = ?,
                    finished_at = ?,
                    duration_ms = ?,
                    row_count = ?,
                    result_json = ?,
                    error_text = ?,
                    metadata_json = ?
                WHERE sql_run_id = ?""" + guard + ";",
                tuple(values),
            )
            return int(cursor.rowcount or 0) > 0

    def fetch_latest_done_or_running_sql_runs_by_run_key(self) -> dict[str, sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    sql_run_id,
                    run_key,
                    sql_id,
                    sql_code,
                    target_no,
                    server_id,
                    db_type,
                    service_name,
                    instance_name,
                    database_name,
                    credential_name,
                    status,
                    level,
                    message,
                    started_at,
                    finished_at,
                    duration_ms,
                    row_count,
                    error_text,
                    metadata_json
                FROM sql_runs
                WHERE status IN ('done', 'running')
                  AND sql_run_id IN (
                    SELECT max(sql_run_id)
                    FROM sql_runs
                    WHERE status IN ('done', 'running')
                    GROUP BY run_key
                );
                """
            ).fetchall()
        return {str(row["run_key"]): row for row in rows}

    def sql_run_started_since(self, run_key: str, since: str) -> bool:
        """Whether a run of this task-and-target started at or after ``since``.

        The SQL task scan's counterpart of :meth:`job_run_started_since`: one indexed probe
        (``ix_sql_runs_run_key_created_at``) before each due task, so an overlapping async scan's
        finished work is never run a second time.
        """
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sql_runs WHERE run_key = ? AND started_at >= ? LIMIT 1",
                (run_key, since),
            ).fetchone()
        return row is not None

    def fetch_latest_sql_runs_by_run_key(self) -> dict[str, sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    sql_run_id,
                    run_key,
                    sql_id,
                    sql_code,
                    target_no,
                    server_id,
                    db_type,
                    service_name,
                    instance_name,
                    database_name,
                    credential_name,
                    status,
                    level,
                    message,
                    started_at,
                    finished_at,
                    duration_ms,
                    row_count,
                    error_text,
                    metadata_json
                FROM sql_runs
                WHERE sql_run_id IN (
                    SELECT max(sql_run_id)
                    FROM sql_runs
                    GROUP BY run_key
                );
                """
            ).fetchall()
        return {str(row["run_key"]): row for row in rows}

    def fetch_running_sql_runs(self) -> list:
        """**Every** row still marked ``running``, oldest first — not one per run_key.

        The `fetch_latest_*` pair answer "where does each task stand", and the stale-run reaper
        used to read the first of them. That made a run invisible the moment a newer run of the
        same task existed: on 2026-09-04 sql_id 28 was killed twice by a worker restart, a fresh
        run started minutes later while the killed row was still short of its timeout, and the
        two abandoned rows were never latest again — so they sat in ``running`` for the rest of
        the day, past their timeout, with no error row and no alert. An abandoned run is a
        failure whether or not anything ran after it, so the reaper has to see all of them.
        """
        self.initialize()
        with self.connect() as conn:
            return list(conn.execute(
                """
                SELECT
                    sql_run_id,
                    run_key,
                    sql_id,
                    sql_code,
                    target_no,
                    server_id,
                    db_type,
                    service_name,
                    instance_name,
                    database_name,
                    credential_name,
                    status,
                    level,
                    message,
                    started_at,
                    finished_at,
                    duration_ms,
                    row_count,
                    error_text,
                    host_name,
                    metadata_json
                FROM sql_runs
                WHERE status = 'running'
                ORDER BY sql_run_id;
                """
            ).fetchall())

    def fetch_latest_sql_run_for_sql_id(self, *, sql_id: int, status: str = "done") -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    sql_run_id,
                    run_key,
                    sql_id,
                    sql_code,
                    target_no,
                    server_id,
                    db_type,
                    service_name,
                    instance_name,
                    database_name,
                    credential_name,
                    status,
                    level,
                    message,
                    started_at,
                    finished_at,
                    duration_ms,
                    row_count,
                    result_json,
                    error_text,
                    metadata_json
                FROM sql_runs
                WHERE sql_id = ?
                  AND status = ?
                ORDER BY sql_run_id DESC
                LIMIT 1;
                """,
                (sql_id, status),
            ).fetchone()


    def fetch_recent_sql_runs(self, *, limit: int = 10, sql_id: int | None = None) -> list:
        """The most recent SQL task runs, newest first — the history, not the schedule.

        `fetch_latest_*_by_run_key` answer "where does each task stand"; this answers "what has
        been happening", which is the question someone asks after an alert. Ordered by
        ``sql_run_id`` rather than ``started_at`` because two runs can start in the same second
        and the id is the only total order the table has.
        """
        self.initialize()
        sql = """
            SELECT sql_run_id, sql_id, sql_code, target_no, server_id, service_name,
                   status, level, message, started_at, finished_at, duration_ms, row_count,
                   error_text
            FROM sql_runs
        """
        params: list = []
        if sql_id is not None:
            sql += " WHERE sql_id = ?"
            params.append(int(sql_id))
        sql += " ORDER BY sql_run_id DESC LIMIT ?;"
        params.append(int(limit))
        with self.connect() as conn:
            return list(conn.execute(sql, tuple(params)).fetchall())
