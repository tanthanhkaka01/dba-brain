from __future__ import annotations
from db_ops.db.store import ensure_sqlite_column  # noqa: F401 - one definition, see that module

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db_ops.lib import event_policy, health_model
from db_ops.config import StoreConfig
from db_ops.db import backend as backend_mod
from db_ops.db.backend import StoreTarget
from db_ops.db.store import utc_now_text
from db_ops.db.metric_results import MetricResult


#: Bumped when this store's tables or additive migrations change, so
#: db_ops.db.backend.remote_schema_is_current can skip the DDL when nothing has.
METRIC_SCHEMA_VERSION = 1


class MetricStore:
    """Metric runs/results/target-health store.

    Runs on SQLite or PostgreSQL per ``data/store_config.json``. A bare path means SQLite;
    :meth:`from_config` follows the declared backend.
    """

    def __init__(
        self,
        source: "str | Path | StoreTarget | StoreConfig",
        *,
        key: str | None = None,
        password: str | None = None,
    ) -> None:
        self.target = StoreTarget.coerce(source, key=key, password=password)
        self.sqlite_path = self.target.sqlite_path

    @classmethod
    def from_config(cls, config, *, key: str | None = None, password: str | None = None) -> "MetricStore":
        return cls(StoreTarget.from_config(config, key=key, password=password))

    @property
    def backend(self) -> str:
        return self.target.store.backend

    def connect(self):
        return self.target.connect()

    def initialize(self, *, force: bool = False) -> None:
        """Create/upgrade this store's schema.

        ``force`` skips both the in-process memo and the recorded schema version, so the DDL and the
        additive migrations run even when everything looks current. That is what
        ``db_ops.db.cli init`` uses: an explicit "build the schema" request should do the work, and
        it is the only way to re-run a repair (an identity-sequence resync, say) on a store whose
        recorded version has not changed.
        """
        if not force and backend_mod.schema_is_ready("MetricStore", self.target):
            return
        # Cross-process check: the daemon's app commands are new processes every run, so the
        # in-process memo above never helps them. schema_meta answers "already built?" with one
        # indexed SELECT instead of ~55 DDL statements per process.
        if not force and backend_mod.remote_schema_is_current(self.target, "MetricStore", METRIC_SCHEMA_VERSION):
            backend_mod.mark_schema_ready("MetricStore", self.target)
            return
        self.target.prepare()
        with self.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(SCHEMA_SQL)
            ensure_sqlite_column(
                conn,
                table_name="metric_results",
                column_name="daily_report_created",
                column_sql="daily_report_created INTEGER NOT NULL DEFAULT 0 CHECK (daily_report_created IN (0, 1))",
            )
            ensure_sqlite_column(conn, table_name="metric_results", column_name="raw_stdout", column_sql="raw_stdout TEXT")
            ensure_sqlite_column(conn, table_name="metric_results", column_name="raw_stderr", column_sql="raw_stderr TEXT")
            ensure_sqlite_column(conn, table_name="metric_results", column_name="exit_code", column_sql="exit_code INTEGER")
            ensure_sqlite_column(conn, table_name="metric_results", column_name="execution_time", column_sql="execution_time REAL")
            ensure_sqlite_column(conn, table_name="metric_results", column_name="collector_type", column_sql="collector_type TEXT")
            ensure_sqlite_column(conn, table_name="metric_results", column_name="category", column_sql="category TEXT")
            ensure_sqlite_column(conn, table_name="metric_results", column_name="error_type", column_sql="error_type TEXT")
            ensure_sqlite_column(
                conn,
                table_name="metric_results",
                column_name="normalized_error_signature",
                column_sql="normalized_error_signature TEXT",
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_metric_results_daily_report_created
                    ON metric_results(daily_report_created);
                """
            )
            backend_mod.record_schema_version(conn, "MetricStore", METRIC_SCHEMA_VERSION)
        backend_mod.mark_schema_ready("MetricStore", self.target)

    # ------------------------------------------------------------------ #
    # Reads that used to be done by opening SQLite directly
    #
    # jobs/status.py, reports/inventory_health.py and reports/server_report.py each opened the
    # store file themselves with sqlite3.connect(). That worked only because the store happened to
    # be SQLite, and it meant the same "read metric_results" logic existed in four places. They call
    # these methods now, so the backend is decided in exactly one layer.
    #
    # Every window here is bounded by a cutoff computed in Python rather than by SQLite's
    # strftime('...','now','-N days'). The old form was untranslatable in one case anyway - the
    # server-report query passed the '-N days' modifier as a *bound parameter*, so no textual
    # rewrite could have reached it - and a plain timestamp comparison is portable and easier to read.
    # ------------------------------------------------------------------ #
    def fetch_freshness(self) -> dict | None:
        """Metric recency overall and per target, for ``jobs status``.

        Returns ``None`` when the store cannot be read at all, which is what the caller renders as
        "no metric data" - a status command must not fail because the store is missing.
        """
        try:
            with self.connect() as conn:
                overall = conn.execute(
                    "SELECT max(collected_at) AS last, count(*) AS n FROM metric_results"
                ).fetchone()
                per_target = conn.execute(
                    "SELECT ip, server_id, max(collected_at) AS last, count(*) AS n "
                    "FROM metric_results GROUP BY ip, server_id ORDER BY last DESC"
                ).fetchall()
        except Exception:  # noqa: BLE001 - missing/locked store is a valid "unknown" answer.
            return None
        return {
            "overall_last": overall["last"] if overall else None,
            "overall_rows": overall["n"] if overall else 0,
            "per_target": [
                {"ip": row["ip"], "server_id": row["server_id"], "last": row["last"], "rows": row["n"]}
                for row in per_target
            ],
        }

    def fetch_health_metrics(self, *, codes: list[str], days: int,
                             as_of: str | None = None) -> list[dict]:
        """Rows behind the inventory health overlay: selected metric codes over a day window.

        ``as_of`` closes the window at a past moment, so a report can be rebuilt for the day it
        describes instead of always ending at "now".
        """
        if not codes:
            return []
        placeholders = ",".join("?" for _ in codes)
        ceiling = as_of_text(as_of)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT ip, server_id, db_name, metric_code, metric_item, metric_value, "
                "metric_unit, status, message, collected_at "
                "FROM metric_results "
                f"WHERE metric_code IN ({placeholders}) AND collected_at >= ? "
                + ("AND collected_at <= ? " if ceiling else "")
                + "ORDER BY collected_at ASC",
                (*codes, cutoff_text(days, as_of=as_of), *( (ceiling,) if ceiling else () )),
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_results_since(self, *, metric_codes: list[str], since: str,
                            db_type: str = "") -> list[sqlite3.Row]:
        """Full result rows for the given metric codes collected at or after ``since``.

        The whole row, not the summary shape :meth:`fetch_health_metrics` returns: the caller here
        is the backup-health report, which re-derives per-database evidence and needs
        ``importance``, ``run_id`` and ``result_id`` as well. Ordered so one server's databases
        arrive together, newest first within each — the order the report renders in, decided here
        rather than re-sorted by every caller.

        ``since`` is an ISO-8601 UTC string, the same shape ``collected_at`` is stored in.
        """
        if not metric_codes:
            return []
        placeholders = ",".join("?" for _ in metric_codes)
        db_type_filter = "AND db_type = ? " if db_type else ""
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT result_id, run_id, collected_at, target_id, server_id, ip, db_type, "
                    "db_name, metric_code, metric_item, metric_value, metric_unit, status, "
                    "importance, message "
                    "FROM metric_results "
                    f"WHERE metric_code IN ({placeholders}) AND collected_at >= ? "
                    + db_type_filter
                    + "ORDER BY server_id, ip, db_name, collected_at DESC, result_id DESC",
                    (*metric_codes, since, *((db_type,) if db_type else ())),
                )
            )

    def fetch_severity_by_server(self, *, days: int, as_of: str | None = None) -> dict[str, dict]:
        """Worst collector-assigned status per server, with the metric codes responsible.

        The fleet report used to re-derive severity from a hand-picked set of signals, while the
        per-server detail page reports the status **the collector computed**. The two disagreed:
        a server with 1533 stack dumps in ``LOG_RECENT_CRITICAL`` read CRITICAL on its own page and
        healthy on the fleet page, because that metric was not one of the signals the fleet rule
        looked at - and was not even loaded.

        Deliberately not restricted to ``HEALTH_CODES``: the point is to see every metric the
        collectors judged, which is exactly what the detail page sees. One aggregate query.

        ``as_of`` closes the window at a past moment, the same way the two sibling queries take it.
        It was the only one of the three without it, so its callers could ask for "the last seven
        days" and nothing else - which is fine for a live report and is why the tests covering it
        expired on a clock rather than on a defect, twice in one day.
        """
        cutoff = cutoff_text(days, as_of=as_of)
        ceiling = as_of_text(as_of)
        with self.connect() as conn:
            # Only the rows of each metric's newest collection - see
            # db_ops.lib.health_model for why the newest row *per item* is not current state.
            # Partitioning by item kept a CRITICAL lock row "current" for the whole window,
            # because the collection that cleared it wrote its OK under metric_item = NULL and
            # therefore landed in a different partition. 192.0.2.250 showed 17 critical lock
            # results on the fleet page while its own page showed blocking at zero.
            rows = conn.execute(
                "SELECT server_id, status, metric_code, count(*) AS n, max(collected_at) AS newest "
                "FROM ("
                "  SELECT server_id, metric_code, status, collected_at,"
                "         MAX(collected_at) OVER ("
                "           PARTITION BY server_id, metric_code"
                "         ) AS snapshot_at"
                "  FROM metric_results"
                "  WHERE collected_at >= ? AND server_id IS NOT NULL AND server_id <> ''"
                + (" AND collected_at <= ?" if ceiling else "")
                + ") latest "
                "WHERE collected_at = snapshot_at "
                "GROUP BY server_id, status, metric_code",
                (cutoff, *((ceiling,) if ceiling else ())),
            ).fetchall()

        severity: dict[str, dict] = {}
        for row in rows:
            server_id = str(row["server_id"])
            state = str(row["status"] or "").upper()
            entry = severity.setdefault(
                server_id,
                {"worst": "OK", "critical_codes": [], "warning_codes": [],
                 "critical_rows": 0, "warning_rows": 0, "as_of": ""},
            )
            entry["as_of"] = max(entry["as_of"], str(row["newest"] or ""))
            if state in ("CRITICAL", "ERROR"):
                entry["worst"] = "CRITICAL"
                entry["critical_codes"].append(str(row["metric_code"]))
                entry["critical_rows"] += int(row["n"])
            elif state == "WARNING":
                if entry["worst"] != "CRITICAL":
                    entry["worst"] = "WARNING"
                entry["warning_codes"].append(str(row["metric_code"]))
                entry["warning_rows"] += int(row["n"])
        for entry in severity.values():
            entry["critical_codes"] = sorted(set(entry["critical_codes"]))
            entry["warning_codes"] = sorted(set(entry["warning_codes"]))
        return severity

    def fetch_current_problems(self, *, days: int, limit: int = 4000) -> dict[str, list[dict]]:
        """Every not-OK row of each metric's newest collection, per server.

        This is the fleet page's half of "one classifier, two pages". The fleet page used to
        re-derive its own findings from a hand-picked set of signals and then bury the rest in a
        single informational paragraph, so 42 current blocked sessions had no critical card and a
        95 ms latency finding existed only on the server page.

        The two logging-only metrics are pulled in whatever their status says, because their SQL
        returns OK on every branch and :func:`health_model.report_judged_severity` is what decides
        them. Everything else is filtered in SQL: not-OK rows are a small fraction of a snapshot,
        and a metric like MAINTENANCE_INDEX_USAGE emits ~29k rows per collection that must never
        be pulled into a report process.
        """
        judged = ", ".join("?" for _ in health_model.REPORT_JUDGED_CODES)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT server_id, metric_code, metric_item, metric_value, metric_unit, status, "
                "       message, collected_at "
                "FROM ("
                "  SELECT server_id, metric_code, metric_item, metric_value, metric_unit, status,"
                "         message, collected_at,"
                "         MAX(collected_at) OVER ("
                "           PARTITION BY server_id, metric_code"
                "         ) AS snapshot_at"
                "  FROM metric_results"
                "  WHERE collected_at >= ? AND server_id IS NOT NULL AND server_id <> ''"
                ") latest "
                "WHERE collected_at = snapshot_at "
                "  AND (upper(COALESCE(status, '')) NOT IN ('OK', 'LOGGING') "
                f"       OR metric_code IN ({judged})) "
                "ORDER BY server_id, metric_code, metric_item "
                "LIMIT ?",
                (cutoff_text(days), *health_model.REPORT_JUDGED_CODES, int(limit)),
            ).fetchall()
        by_server: dict[str, list[dict]] = {}
        for row in rows:
            by_server.setdefault(str(row["server_id"]), []).append(dict(row))
        return by_server

    def fetch_metric_freshness(self, *, days: int,
                               as_of: str | None = None) -> dict[str, list[dict]]:
        """Per ``(server, metric_code)``: when it was last attempted, last succeeded, and what it
        says now — the answer to "is this page describing the present?" for each metric separately.

        A server-wide "data age: 3 minutes" is not that answer. On 192.0.2.250 it read three
        minutes old while ``LOG_RECENT_CRITICAL`` was 48 hours stale and
        ``QUERY_LONG_WAITING_OR_ROLLBACK_REQUESTS`` 37 hours, both against a five-minute cadence:
        the freshest metric hid every late one behind it.

        A success is a row that is not ERROR and whose ``error_type`` is not a collector failure
        (:data:`event_policy.COLLECTOR_FAILURE_ERROR_TYPES`). Status alone cannot decide this: a
        target's severity map downgrades connect and auth failures to WARNING, so on
        192.0.2.250 four metrics had been returning ``AUTH_FAILED`` for a day while reading as
        ordinary warnings — and the report went on showing the values from before the credential
        broke, as current.
        """
        cutoff = cutoff_text(days, as_of=as_of)
        ceiling = as_of_text(as_of)
        failures = ", ".join(f"'{name}'" for name in event_policy.COLLECTOR_FAILURE_ERROR_TYPES)
        # One pass. The aggregates are windowed rather than grouped so the newest row's own
        # status and message can ride along: "this metric last succeeded 37 hours ago" is only
        # half an answer without "and what it says now is Login failed for user 'db_ops'".
        success = ("upper(COALESCE(status, '')) <> 'ERROR' "
                   f"AND upper(COALESCE(error_type, '')) NOT IN ({failures})")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT server_id, metric_code, db_type, collector_type, status, message, "
                "       last_attempt, last_success, rows_in_window "
                "FROM ("
                "  SELECT server_id, metric_code, db_type, collector_type, status, message,"
                "         MAX(collected_at) OVER (PARTITION BY server_id, metric_code)"
                "             AS last_attempt,"
                f"         MAX(CASE WHEN {success} THEN collected_at END)"
                "             OVER (PARTITION BY server_id, metric_code) AS last_success,"
                "         COUNT(*) OVER (PARTITION BY server_id, metric_code) AS rows_in_window,"
                "         ROW_NUMBER() OVER ("
                "           PARTITION BY server_id, metric_code"
                "           ORDER BY collected_at DESC, result_id DESC"
                "         ) AS recency"
                "  FROM metric_results"
                "  WHERE collected_at >= ? AND server_id IS NOT NULL AND server_id <> ''"
                + ("  AND collected_at <= ? " if ceiling else "")
                + ") latest "
                "WHERE recency = 1",
                (cutoff, *((ceiling,) if ceiling else ())),
            ).fetchall()
        by_server: dict[str, list[dict]] = {}
        for row in rows:
            by_server.setdefault(str(row["server_id"]), []).append(dict(row))
        return by_server

    def fetch_server_series(self, *, server_id: str, days: int,
                            as_of: str | None = None) -> list[dict]:
        """Chartable metric history for one server, oldest first.

        NULL-item rows are relabelled rather than dropped. The old ``metric_item IS NOT NULL``
        filter discarded exactly the rows that say a metric is currently failing or currently
        empty, so the page kept charting the last values the metric produced *before* it broke and
        presented them as the present.

        **All** of them come through, including the OK "SQL returned no rows" ones, even though
        those are never charted. They are what makes a metric's newest-collection timestamp
        correct, and :func:`server_report.load_server_series` drops them once it has used them.
        Filtering them out here instead looked equivalent and was not: with the OK rows gone, a
        *cleared* collector failure became the newest thing the metric had, so a target that
        recovered hours ago still reported the failure as current.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT metric_code, "
                "       COALESCE(metric_item, ?) AS metric_item, "
                "       metric_value, metric_unit, status, message, collected_at "
                "FROM metric_results "
                "WHERE server_id = ? AND collected_at >= ? "
                + ("AND collected_at <= ? " if as_of_text(as_of) else "")
                + "ORDER BY collected_at ASC",
                (health_model.COLLECTOR_ITEM, server_id, cutoff_text(days, as_of=as_of),
                 *((as_of_text(as_of),) if as_of_text(as_of) else ())),
            ).fetchall()
        return [dict(row) for row in rows]

    def archive_old_results(self, *, retention_days: int = 30) -> int:
        """Move metric_results rows older than ``retention_days`` into
        ``metric_results_archive`` (kept, not deleted) and remove them from the live
        table. Returns the number of rows archived. The copy + delete run in one
        transaction so a row is never lost or duplicated."""
        self.initialize()
        # The cutoff and the archive stamp are computed in Python and bound as parameters.
        #
        # This used to inline SQLite's three-argument strftime('%Y-...','now','-N days'). PostgreSQL
        # has no such function, and the dialect translator only rewrites the two-argument UTC-now
        # form, so on a PostgreSQL store every metrics run died with
        # "function strftime(unknown, unknown, unknown) does not exist" - which is what broke the
        # metrics and SLA app commands immediately after the cutover. Teaching the translator a
        # second strftime shape would work; not generating engine-specific SQL in the first place is
        # better, and reads more plainly besides.
        days = max(int(retention_days), 0)
        cutoff = cutoff_text(days)
        archived_at = utc_now_text()
        with self.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM metric_results WHERE collected_at < ?", (cutoff,)
            ).fetchone()[0]
            if count:
                conn.execute(
                    f"INSERT INTO metric_results_archive ({_ARCHIVE_COLUMNS}, archived_at) "
                    f"SELECT {_ARCHIVE_COLUMNS}, ? "
                    f"FROM metric_results WHERE collected_at < ?",
                    (archived_at, cutoff),
                )
                conn.execute("DELETE FROM metric_results WHERE collected_at < ?", (cutoff,))
        return int(count)

    def start_run(self, *, started_at: str, status: str = "RUNNING", message: str = "") -> int:
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO metric_runs (started_at, status, message) VALUES (?, ?, ?);",
                (started_at, status, message),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        *,
        run_id: int,
        finished_at: str,
        status: str,
        target_count: int,
        metric_count: int,
        result_count: int,
        error_count: int,
        warning_count: int,
        critical_count: int,
        message: str,
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE metric_runs
                SET finished_at = ?, status = ?, target_count = ?, metric_count = ?,
                    result_count = ?, error_count = ?, warning_count = ?, critical_count = ?, message = ?
                WHERE run_id = ?;
                """,
                (
                    finished_at,
                    status,
                    target_count,
                    metric_count,
                    result_count,
                    error_count,
                    warning_count,
                    critical_count,
                    message,
                    run_id,
                ),
            )

    def insert_results(self, *, run_id: int, results: list[MetricResult]) -> int:
        self.initialize()
        if not results:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO metric_results
                (
                    run_id, target_id, server_id, ip, db_type, db_name, metric_code,
                    metric_item, metric_value, metric_unit, status, importance, message, collected_at,
                    raw_stdout, raw_stderr, exit_code, execution_time,
                    collector_type, category, error_type, normalized_error_signature
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                [
                    (
                        run_id,
                        item.target_id,
                        item.server_id,
                        item.ip,
                        item.db_type,
                        item.db_name,
                        item.metric_code,
                        # Every free-text column the collected SQL can influence goes through the
                        # NUL scrub, not just `message`: metric_item and metric_value are built
                        # from server-side values too, and one NUL anywhere fails the whole batch.
                        _sqlite_text(item.metric_item),
                        _sqlite_text(item.metric_value),
                        item.metric_unit,
                        item.status,
                        item.importance,
                        _sqlite_text(item.message),
                        item.collected_at,
                        _sqlite_text(item.raw_stdout),
                        _sqlite_text(item.raw_stderr),
                        _sqlite_int(item.exit_code),
                        _sqlite_float(item.execution_time),
                        _sqlite_text(item.collector_type),
                        _sqlite_text(item.category),
                        _sqlite_text(item.error_type),
                        _sqlite_text(item.normalized_error_signature),
                    )
                    for item in results
                ],
            )
        return len(results)

    def mark_daily_report_created(self, *, result_ids: list[int]) -> int:
        self.initialize()
        clean_ids = sorted({int(result_id) for result_id in result_ids if int(result_id) > 0})
        if not clean_ids:
            return 0
        placeholders = ", ".join("?" for _ in clean_ids)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE metric_results
                SET daily_report_created = 1
                WHERE result_id IN ({placeholders});
                """,
                clean_ids,
            )
            return int(cursor.rowcount)

    # `mark_daily_report_created_for_scope` was deleted on 2026-08-16, for the same reason as its
    # twin in `store.py`: byte-identical to `DbOpsStore.mark_metric_daily_report_created_for_scope`
    # and called by nothing. The reports app holds a `DbOpsStore`, so that is where the scoped
    # mark stayed.

    def rebuild_target_health(self, *, run_id: int) -> int:
        self.initialize()
        with self.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT run_id, target_id, server_id, ip, db_type, db_name, status, importance, message, collected_at
                    FROM metric_results
                    WHERE run_id = ?;
                    """,
                    (run_id,),
                )
            )
            conn.execute("DELETE FROM target_health WHERE run_id = ?;", (run_id,))
            if not rows:
                return 0

            grouped: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                grouped.setdefault(str(row["target_id"] or ""), []).append(row)

            health_rows = []
            for target_id, target_rows in grouped.items():
                status = _target_health_status(target_rows)
                counts = _status_counts(target_rows)
                score = sum(
                    int(row["importance"] or 0)
                    for row in target_rows
                    if str(row["status"] or "").upper() in {"ERROR", "CRITICAL", "WARNING", "NO_DATA"}
                )
                messages = [
                    str(row["message"] or "").strip()
                    for row in target_rows
                    if str(row["message"] or "").strip()
                    and str(row["status"] or "").upper() in {"ERROR", "CRITICAL", "WARNING", "NO_DATA"}
                ]
                first = target_rows[0]
                health_rows.append(
                    (
                        run_id,
                        target_id,
                        first["server_id"],
                        first["ip"],
                        first["db_type"],
                        first["db_name"],
                        status,
                        score,
                        counts["ERROR"],
                        counts["WARNING"],
                        counts["CRITICAL"],
                        counts["NO_DATA"],
                        counts["OK"],
                        "\n".join(messages[:5]),
                        max(str(row["collected_at"] or "") for row in target_rows),
                    )
                )

            conn.executemany(
                """
                INSERT INTO target_health
                (
                    run_id, target_id, server_id, ip, db_type, db_name, status, score,
                    error_count, warning_count, critical_count, no_data_count, ok_count,
                    message, collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                health_rows,
            )
            return len(health_rows)

    def latest_result_time(self, *, target_id: str, metric_code: str) -> str | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT collected_at
                FROM metric_results
                WHERE target_id = ? AND metric_code = ?
                ORDER BY collected_at DESC, result_id DESC
                LIMIT 1;
                """,
                (target_id, metric_code),
            ).fetchone()
        return str(row["collected_at"]) if row else None

    def latest_successful_result_time(self, *, target_id: str, metric_code: str) -> str | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT collected_at
                FROM metric_results
                WHERE target_id = ?
                  AND metric_code = ?
                  AND upper(COALESCE(status, '')) <> 'ERROR'
                  AND NOT (
                      upper(COALESCE(status, '')) = 'WARNING'
                      AND lower(COALESCE(message, '')) LIKE 'sql execution failed:%'
                  )
                ORDER BY collected_at DESC, result_id DESC
                LIMIT 1;
                """,
                (target_id, metric_code),
            ).fetchone()
        return str(row["collected_at"]) if row else None

    def fetch_latest_results(
        self,
        *,
        limit: int,
        status: str | None = None,
        importance_min: int | None = None,
        target_id: str | None = None,
        metric_code: str | None = None,
    ) -> list[sqlite3.Row]:
        self.initialize()
        where, params = _filters(status=status, importance_min=importance_min, target_id=target_id, metric_code=metric_code)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT result_id, run_id, collected_at, target_id, server_id, ip, db_type, db_name,
                           metric_code, metric_item, metric_value, metric_unit, status, importance,
                           message, daily_report_created, collector_type, category,
                           error_type, normalized_error_signature
                    FROM metric_results
                    {where}
                    ORDER BY collected_at DESC, result_id DESC
                    LIMIT ?;
                    """,
                    (*params, limit),
                )
            )

    def fetch_alert_results(
        self,
        *,
        run_id: int | None,
        importance_min: int,
        include_warning: bool,
        include_ok: bool,
    ) -> list[sqlite3.Row]:
        self.initialize()
        statuses = ["ERROR", "CRITICAL"]
        if include_warning:
            statuses.append("WARNING")
        if include_ok:
            statuses.append("OK")
        params: list[object] = [importance_min, *statuses]
        run_filter = ""
        if run_id is not None:
            run_filter = "AND run_id = ?"
            params.append(run_id)
        placeholders = ", ".join("?" for _ in statuses)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT run_id, collected_at, target_id, server_id, ip, db_name, metric_code,
                           metric_item, metric_value, metric_unit, status, importance, message,
                           daily_report_created
                    FROM metric_results
                    WHERE (importance >= ? OR upper(status) IN ({placeholders}))
                      {run_filter}
                    ORDER BY importance DESC, collected_at DESC, result_id DESC;
                    """,
                    params,
                )
            )

    def latest_run_id(self) -> int | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT run_id
                FROM metric_runs
                ORDER BY run_id DESC
                LIMIT 1;
                """
            ).fetchone()
        return int(row["run_id"]) if row else None

    def fetch_run_results(
        self,
        *,
        run_id: int,
        db_type: str | None = None,
        target_id: str | None = None,
    ) -> list[sqlite3.Row]:
        self.initialize()
        clauses = ["run_id = ?"]
        params: list[object] = [run_id]
        if db_type:
            clauses.append("db_type = ?")
            params.append(db_type.lower())
        if target_id:
            clauses.append("target_id = ?")
            params.append(target_id)
        where = " AND ".join(clauses)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT result_id, run_id, collected_at, target_id, server_id, ip, db_type, db_name,
                           metric_code, metric_item, metric_value, metric_unit, status, importance,
                           message, daily_report_created, collector_type, category,
                           error_type, normalized_error_signature
                    FROM metric_results
                    WHERE {where}
                    ORDER BY target_id, metric_code, result_id;
                    """,
                    params,
                )
            )

    def fetch_latest_report_results(
        self,
        *,
        db_type: str | None = None,
        target_id: str | None = None,
        metric_code: str | None = None,
        metric_codes: set[str] | None = None,
    ) -> list[sqlite3.Row]:
        self.initialize()
        clauses: list[str] = []
        params: list[object] = []
        if db_type:
            clauses.append("r.db_type = ?")
            params.append(db_type.lower())
        if target_id:
            clauses.append("r.target_id = ?")
            params.append(target_id)
        if metric_code:
            clauses.append("r.metric_code = ?")
            params.append(metric_code)
        elif metric_codes is not None:
            if not metric_codes:
                return []
            placeholders = ", ".join("?" for _ in metric_codes)
            clauses.append(f"r.metric_code IN ({placeholders})")
            params.extend(sorted(metric_codes))
        extra_where = " AND " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT r.result_id, r.run_id, r.collected_at, r.target_id, r.server_id, r.ip,
                           r.db_type, r.db_name, r.metric_code, r.metric_item, r.metric_value,
                           r.metric_unit, r.status, r.importance, r.message, r.daily_report_created
                    FROM metric_results AS r
                    WHERE r.collected_at = (
                        SELECT MAX(inner_r.collected_at)
                        FROM metric_results AS inner_r
                        WHERE inner_r.target_id = r.target_id
                          AND inner_r.metric_code = r.metric_code
                    )
                    {extra_where}
                    ORDER BY r.metric_code, r.target_id, r.result_id;
                    """,
                    params,
                )
            )


def _filters(
    *,
    status: str | None,
    importance_min: int | None,
    target_id: str | None,
    metric_code: str | None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("upper(status) = ?")
        params.append(status.upper())
    if importance_min is not None:
        clauses.append("importance >= ?")
        params.append(importance_min)
    if target_id:
        clauses.append("target_id = ?")
        params.append(target_id)
    if metric_code:
        clauses.append("metric_code = ?")
        params.append(metric_code)
    return ("WHERE " + " AND ".join(clauses), params) if clauses else ("", params)


def cutoff_text(days: int, *, as_of: str | None = None) -> str:
    """UTC timestamp ``days`` before now (or before ``as_of``), in the store's text format.

    The store keeps timestamps as ISO-8601 UTC text, so a window can be expressed as a plain string
    comparison that means the same thing on both backends - no ``strftime`` modifier, no
    ``interval`` arithmetic, and nothing for the dialect translator to get wrong.
    """
    end = _parse_as_of(as_of) or datetime.now(timezone.utc)
    return (end - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_as_of(as_of: str | None) -> datetime | None:
    """``as_of`` as a UTC datetime, or None for "now"."""
    text = str(as_of or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def as_of_text(as_of: str | None) -> str | None:
    """The upper bound of a window, in the store's text format, or None for "up to now".

    Every report window is open-ended at the top because the normal question is "what is true
    now". Rebuilding a report for a past date is the same query with a ceiling on it: without
    one, a report built for 1 August would happily read rows collected on the 3rd and claim to
    be the 1st.
    """
    end = _parse_as_of(as_of)
    return end.strftime("%Y-%m-%dT%H:%M:%SZ") if end else None




def _sqlite_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return _without_nul(value.decode("utf-8", errors="replace"))
    return _without_nul(str(value))


def _without_nul(text: str) -> str:
    """Drop NUL characters, which PostgreSQL refuses in a text column.

    SQL Server's nvarchar happily holds ``0x0000``, and ``sys.dm_exec_sql_text`` hands it straight
    back — so a metric that quotes the running statement can carry one. Postgres then rejects the
    INSERT with ``invalid byte sequence for encoding "UTF8": 0x00`` and, because the whole batch
    is one executemany, **the entire collection run dies** — every metric for every remaining
    target, over one stray byte in one session's SQL text. Found when LOCK_TRANSACTION_HOLDERS
    started succeeding on 192.0.2.115 and immediately killed the run.

    Dropped rather than escaped: NUL carries no meaning in a message a human reads, and the
    alternative (refusing the row) loses a real finding to a formatting artifact.
    """
    return text.replace("\x00", "") if "\x00" in text else text


def _sqlite_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _sqlite_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


# Columns shared by metric_results and metric_results_archive (archive adds archived_at).
_ARCHIVE_COLUMNS = (
    "result_id, run_id, target_id, server_id, ip, db_type, db_name, metric_code, "
    "metric_item, metric_value, metric_unit, status, importance, message, collected_at, "
    "daily_report_created, raw_stdout, raw_stderr, exit_code, execution_time, "
    "collector_type, category, error_type, normalized_error_signature"
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metric_runs
(
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    target_count INTEGER,
    metric_count INTEGER,
    result_count INTEGER,
    error_count INTEGER,
    warning_count INTEGER,
    critical_count INTEGER,
    message TEXT
);

CREATE TABLE IF NOT EXISTS metric_results
(
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    target_id TEXT,
    server_id TEXT,
    ip TEXT,
    db_type TEXT,
    db_name TEXT,
    metric_code TEXT NOT NULL,
    metric_item TEXT,
    metric_value TEXT,
    metric_unit TEXT,
    status TEXT,
    importance INTEGER,
    message TEXT,
    collected_at TEXT NOT NULL,
    daily_report_created INTEGER NOT NULL DEFAULT 0 CHECK (daily_report_created IN (0, 1)),
    raw_stdout TEXT,
    raw_stderr TEXT,
    exit_code INTEGER,
    execution_time REAL,
    collector_type TEXT,
    category TEXT,
    error_type TEXT,
    normalized_error_signature TEXT
);

CREATE INDEX IF NOT EXISTS ix_metric_results_collected_at ON metric_results(collected_at);
CREATE INDEX IF NOT EXISTS ix_metric_results_target_id ON metric_results(target_id);
CREATE INDEX IF NOT EXISTS ix_metric_results_metric_code ON metric_results(metric_code);
CREATE INDEX IF NOT EXISTS ix_metric_results_server_metric_time
    ON metric_results(server_id, metric_code, collected_at);
CREATE INDEX IF NOT EXISTS ix_metric_results_status ON metric_results(status);
CREATE INDEX IF NOT EXISTS ix_metric_results_importance ON metric_results(importance);

CREATE TABLE IF NOT EXISTS metric_results_archive
(
    result_id INTEGER,
    run_id INTEGER,
    target_id TEXT,
    server_id TEXT,
    ip TEXT,
    db_type TEXT,
    db_name TEXT,
    metric_code TEXT,
    metric_item TEXT,
    metric_value TEXT,
    metric_unit TEXT,
    status TEXT,
    importance INTEGER,
    message TEXT,
    collected_at TEXT,
    daily_report_created INTEGER,
    raw_stdout TEXT,
    raw_stderr TEXT,
    exit_code INTEGER,
    execution_time REAL,
    collector_type TEXT,
    category TEXT,
    error_type TEXT,
    normalized_error_signature TEXT,
    archived_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_metric_results_archive_collected_at ON metric_results_archive(collected_at);
CREATE TABLE IF NOT EXISTS target_health
(
    target_health_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    target_id TEXT,
    server_id TEXT,
    ip TEXT,
    db_type TEXT,
    db_name TEXT,
    status TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    warning_count INTEGER NOT NULL DEFAULT 0,
    critical_count INTEGER NOT NULL DEFAULT 0,
    no_data_count INTEGER NOT NULL DEFAULT 0,
    ok_count INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    collected_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_target_health_run_target ON target_health(run_id, target_id);
CREATE INDEX IF NOT EXISTS ix_target_health_status ON target_health(status);
CREATE INDEX IF NOT EXISTS ix_target_health_collected_at ON target_health(collected_at);
"""


def _target_health_status(rows: list[sqlite3.Row]) -> str:
    statuses = {str(row["status"] or "").upper() for row in rows}
    if "ERROR" in statuses:
        return "ERROR"
    if "CRITICAL" in statuses:
        return "CRITICAL"
    if "WARNING" in statuses or "WARN" in statuses or "UNKNOWN" in statuses:
        return "WARNING"
    if "NO_DATA" in statuses:
        return "NO_DATA"
    return "OK"


def _status_counts(rows: list[sqlite3.Row]) -> dict[str, int]:
    counts = {"ERROR": 0, "WARNING": 0, "CRITICAL": 0, "NO_DATA": 0, "OK": 0}
    for row in rows:
        status = str(row["status"] or "").upper()
        if status == "LOGGING":
            status = "OK"
        if status in {"WARN", "UNKNOWN"}:
            status = "WARNING"
        if status in counts:
            counts[status] += 1
    return counts
