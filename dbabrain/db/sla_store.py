from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from dbabrain.lib.config import StoreConfig
from dbabrain.db.sla_results import SlaPolicyResult, SlaValidationSummary, state_key
from dbabrain.db import backend as backend_mod
from dbabrain.db.backend import StoreTarget


#: Bumped when this store's tables or additive migrations change, so
#: dbabrain.db.backend.remote_schema_is_current can skip the DDL when nothing has.
SLA_SCHEMA_VERSION = 1


class SlaStore:
    """Persistence for the SLA app (SQLite or PostgreSQL, per ``data/store_config.json``).

    The SLA app never connects to a monitored database: its only input is the
    ``metric_results`` table already populated by the metrics app (read via
    :meth:`fetch_metric_samples`), and its only output is the ``sla_runs`` /
    ``sla_results`` tables (written via :meth:`save_summary`). All three live in the
    shared ``dbabrain.sqlite`` file, so the app stays independent of the metrics app code.
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
    def from_config(cls, config, *, key: str | None = None, password: str | None = None) -> "SlaStore":
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
        ``dbabrain.db.cli init`` uses: an explicit "build the schema" request should do the work, and
        it is the only way to re-run a repair (an identity-sequence resync, say) on a store whose
        recorded version has not changed.
        """
        if not force and backend_mod.schema_is_ready("SlaStore", self.target):
            return
        # Cross-process check: the daemon's app commands are new processes every run, so the
        # in-process memo above never helps them. schema_meta answers "already built?" with one
        # indexed SELECT instead of ~55 DDL statements per process.
        if not force and backend_mod.remote_schema_is_current(self.target, "SlaStore", SLA_SCHEMA_VERSION):
            backend_mod.mark_schema_ready("SlaStore", self.target)
            return
        self.target.prepare()
        with self.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(SCHEMA_SQL)
            # Additive migrations for stores created before per-instance results existed.
            _ensure_column(conn, "sla_runs", "result_count", "result_count INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "sla_results", "target_id", "target_id TEXT NOT NULL DEFAULT '*'")
            _ensure_column(conn, "sla_results", "details_json", "details_json TEXT NOT NULL DEFAULT '{}'")
            backend_mod.record_schema_version(conn, "SlaStore", SLA_SCHEMA_VERSION)
        backend_mod.mark_schema_ready("SlaStore", self.target)
        backend_mod.mark_schema_ready("SlaStore", self.target)

    def fetch_metric_samples(
        self,
        *,
        target_ids: tuple[str, ...],
        db_types: tuple[str, ...],
        metric_codes: tuple[str, ...],
        window_start: str,
        window_end: str,
        server_ids: tuple[str, ...] = (),
        with_message: bool = False,
    ) -> list[sqlite3.Row]:
        """Read the metric samples in scope for one SLI.

        ``with_message`` adds each row's ``message`` - the backup RPO policy reads a backup's
        recovery model and finish time there. Off by default: every other policy reads the status
        and the value, and the message is the widest column of the table.

        Scope is an explicit set of ``target_ids``, or the ``server_ids`` the caller resolved
        from config for a per-DBMS SLO. **``db_types`` is only the fallback**, and it is the
        weakest of the three: it matches on the ``db_type`` column *stored on each row*, which is
        a snapshot of what the target was called when the sample was collected.

        That snapshot is why it must not be the primary scope. On 2026-08-06 two host records
        were given ``db_type: "host"`` so the OS SLOs would cover them; their 14,093 existing
        rows still carried the empty string they were collected under, so every one of those
        SLOs answered NO_DATA for machines with three weeks of history sitting in the table. The
        rows were never wrong — the query was asking a frozen copy of a config value instead of
        asking which machines are in scope.

        ``server_id`` is the identity that does not move: one machine, one id, everywhere in
        dbabrain. Grouping into per-database verdicts still happens on ``target_id`` upstream, so a
        server running several databases is not collapsed into one answer.
        ``metric_results`` is created lazily by the metrics/db layer; if it does not exist yet
        there is simply no data.
        """
        if not metric_codes or (not target_ids and not server_ids and not db_types):
            return []
        if not self._metric_results_exists():
            return []
        metric_placeholders = ", ".join("?" for _ in metric_codes)
        scope_clause, scope_params = _build_scope_clause(target_ids, server_ids, db_types)
        params: list[object] = [*metric_codes, *scope_params, window_start, window_end]
        message_column = ", message" if with_message else ""
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    -- error_type separates "the service is bad" from "we could not look".
                    -- Without it every authentication failure counted as a backup/CHECKDB/job
                    -- breach, which multiplied one collector incident across every policy that
                    -- touched that target.
                    SELECT target_id, server_id, db_type, db_name, metric_code, metric_item,
                           metric_value, metric_unit, status, error_type, collected_at{message_column}
                    FROM metric_results
                    WHERE metric_code IN ({metric_placeholders})
                      AND {scope_clause}
                      AND collected_at >= ?
                      AND collected_at <= ?
                    ORDER BY collected_at ASC, result_id ASC;
                    """,
                    params,
                )
            )

    def minute_counts(
        self,
        *,
        target_ids: tuple[str, ...] = (),
        server_ids: tuple[str, ...] = (),
        db_types: tuple[str, ...] = (),
        metric_codes: tuple[str, ...],
        good_statuses: tuple[str, ...],
        excluded_error_types: tuple[str, ...] | frozenset[str] = (),
        window_end: str,
        window_starts: dict[str, str],
        maintenance_windows: tuple[tuple[str, str], ...] = (),
        include_archive: bool = False,
    ) -> dict[str, dict[str, tuple[int, int]]]:
        """For each target, for each named window [window_starts[name], window_end),
        the number of observed minutes and of good minutes."""
        if not metric_codes or (not target_ids and not server_ids and not db_types) or not window_starts:
            return {}
        if not self._metric_results_exists():
            return {}

        earliest_start = min(window_starts.values())
        scope_clause, scope_params = _build_scope_clause(target_ids, server_ids, db_types)

        metric_placeholders = ", ".join("?" for _ in metric_codes)
        good_statuses_upper = tuple(s.upper() for s in good_statuses) or ("OK", "LOGGING")
        good_placeholders = ", ".join("?" for _ in good_statuses_upper)

        excluded_clause = ""
        excluded_params: list[object] = []
        if excluded_error_types:
            excluded_upper = tuple(e.upper() for e in excluded_error_types)
            excluded_clause = f"AND upper(coalesce(error_type, '')) NOT IN ({', '.join('?' for _ in excluded_upper)})"
            excluded_params = list(excluded_upper)

        maint_clause = ""
        maint_params: list[object] = []
        for start, end in maintenance_windows:
            maint_clause += " AND NOT (collected_at >= ? AND collected_at <= ?)"
            maint_params.extend([start, end])

        window_items = list(window_starts.items())
        outer_selects = []
        outer_params: list[object] = []
        for i, (name, start_time) in enumerate(window_items):
            start_min = start_time[:16]
            outer_selects.append(f"SUM(CASE WHEN minute >= ? THEN 1 ELSE 0 END) AS obs_{i}")
            outer_selects.append(f"SUM(CASE WHEN minute >= ? AND up = 1 THEN 1 ELSE 0 END) AS good_{i}")
            outer_params.extend([start_min, start_min])

        with self.connect() as conn:
            table_source = _resolve_metric_source(conn, include_archive)
            sql = f"""
            WITH minutes AS (
                SELECT target_id,
                       substr(collected_at, 1, 16) AS minute,
                       max(CASE WHEN upper(coalesce(status, '')) IN ({good_placeholders}) THEN 1 ELSE 0 END) AS up
                FROM {table_source}
                WHERE metric_code IN ({metric_placeholders})
                  AND {scope_clause}
                  AND collected_at >= ?
                  AND collected_at < ?
                  {excluded_clause}
                  {maint_clause}
                GROUP BY target_id, substr(collected_at, 1, 16)
            )
            SELECT target_id,
                   {', '.join(outer_selects)}
            FROM minutes
            GROUP BY target_id;
            """
            params = [
                *good_statuses_upper,
                *metric_codes,
                *scope_params,
                earliest_start,
                window_end,
                *excluded_params,
                *maint_params,
                *outer_params,
            ]
            rows = conn.execute(sql, params).fetchall()

        result: dict[str, dict[str, tuple[int, int]]] = {}
        for row in rows:
            target_id = str(row["target_id"])
            result[target_id] = {}
            for i, (name, _) in enumerate(window_items):
                obs = int(row[f"obs_{i}"] or 0)
                good = int(row[f"good_{i}"] or 0)
                result[target_id][name] = (obs, good)
        return result

    def minute_details(
        self,
        *,
        target_ids: tuple[str, ...] = (),
        server_ids: tuple[str, ...] = (),
        db_types: tuple[str, ...] = (),
        metric_codes: tuple[str, ...],
        good_statuses: tuple[str, ...],
        excluded_error_types: tuple[str, ...] | frozenset[str] = (),
        window_start: str,
        window_end: str,
        maintenance_windows: tuple[tuple[str, str], ...] = (),
        include_archive: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Detailed minute counts and diagnostics for one window."""
        if not metric_codes or (not target_ids and not server_ids and not db_types):
            return {}
        if not self._metric_results_exists():
            return {}

        scope_clause, scope_params = _build_scope_clause(target_ids, server_ids, db_types)

        metric_placeholders = ", ".join("?" for _ in metric_codes)
        good_statuses_upper = tuple(s.upper() for s in good_statuses) or ("OK", "LOGGING")
        good_placeholders = ", ".join("?" for _ in good_statuses_upper)

        maint_clause = ""
        maint_params: list[object] = []
        for start, end in maintenance_windows:
            maint_clause += " AND NOT (collected_at >= ? AND collected_at <= ?)"
            maint_params.extend([start, end])

        excluded_upper = tuple(e.upper() for e in excluded_error_types) if excluded_error_types else ()
        if excluded_upper:
            ex_placeholders = ", ".join("?" for _ in excluded_upper)
            excluded_clause_minutes = f"AND upper(coalesce(error_type, '')) NOT IN ({ex_placeholders})"
            collection_rows_expr = f"sum(CASE WHEN upper(coalesce(error_type, '')) IN ({ex_placeholders}) THEN 1 ELSE 0 END)"
            bad_rows_excluded = f"AND upper(coalesce(r.error_type, '')) NOT IN ({ex_placeholders})"
        else:
            excluded_clause_minutes = ""
            collection_rows_expr = "0"
            bad_rows_excluded = ""

        with self.connect() as conn:
            table_source = _resolve_metric_source(conn, include_archive)
            sql = f"""
            WITH rows AS (
                SELECT target_id, status, error_type, collected_at,
                       substr(collected_at, 1, 16) AS minute
                FROM {table_source}
                WHERE metric_code IN ({metric_placeholders})
                  AND {scope_clause}
                  AND collected_at >= ?
                  AND collected_at < ?
                  {maint_clause}
            ),
            minutes AS (
                SELECT target_id, minute,
                       max(CASE WHEN upper(coalesce(status, '')) IN ({good_placeholders}) THEN 1 ELSE 0 END) AS up
                FROM rows
                WHERE 1=1 {excluded_clause_minutes}
                GROUP BY target_id, minute
            ),
            target_summary AS (
                SELECT target_id,
                       max(collected_at) AS newest,
                       {collection_rows_expr} AS collection_rows
                FROM rows
                GROUP BY target_id
            ),
            newest_min AS (
                SELECT target_id, up AS newest_minute_up
                FROM (
                    SELECT target_id, up,
                           ROW_NUMBER() OVER (PARTITION BY target_id ORDER BY minute DESC) AS rn
                    FROM minutes
                ) ranked
                WHERE rn = 1
            ),
            bad_rows AS (
                SELECT r.target_id,
                       upper(coalesce(r.status, '')) AS status,
                       count(*) AS status_count
                FROM rows r
                JOIN minutes m ON r.target_id = m.target_id AND r.minute = m.minute
                WHERE m.up = 0
                  {bad_rows_excluded}
                GROUP BY r.target_id, upper(coalesce(r.status, ''))
            )
            SELECT s.target_id,
                   s.newest,
                   s.collection_rows,
                   nm.newest_minute_up,
                   b.status AS bad_status,
                   b.status_count AS bad_status_count
            FROM target_summary s
            LEFT JOIN newest_min nm ON s.target_id = nm.target_id
            LEFT JOIN bad_rows b ON s.target_id = b.target_id;
            """
            params: list[object] = [
                *metric_codes,
                *scope_params,
                window_start,
                window_end,
                *maint_params,
                *good_statuses_upper,
            ]
            if excluded_upper:
                params.extend(excluded_upper)
                params.extend(excluded_upper)
                params.extend(excluded_upper)
            rows = conn.execute(sql, params).fetchall()

        details: dict[str, dict[str, Any]] = {}
        for row in rows:
            target_id = str(row["target_id"])
            if target_id not in details:
                up_val = row["newest_minute_up"]
                details[target_id] = {
                    "failures_by_status": {},
                    "collection_rows": int(row["collection_rows"] or 0),
                    "newest": str(row["newest"]) if row["newest"] is not None else None,
                    "newest_minute_up": bool(up_val) if up_val is not None else None,
                }
            bad_status = row["bad_status"]
            if bad_status is not None:
                details[target_id]["failures_by_status"][str(bad_status)] = int(row["bad_status_count"])
        return details

    def save_summary(
        self,
        *,
        summary: SlaValidationSummary,
        started_at: str,
        finished_at: str,
        message: str = "",
    ) -> int:
        """Persist one validation run: a header row in ``sla_runs`` plus one ``sla_results``
        row per policy. Returns the new ``sla_run_id``."""
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO sla_runs
                (started_at, finished_at, status, policy_count, result_count, passed_count,
                 at_risk_count, failed_count, no_data_count, window_end, message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    started_at,
                    finished_at,
                    summary.status,
                    summary.policy_count,
                    summary.result_count,
                    summary.passed_count,
                    summary.at_risk_count,
                    summary.failed_count,
                    summary.no_data_count,
                    summary.window_end,
                    message,
                ),
            )
            sla_run_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO sla_results
                (sla_run_id, policy_id, name, target_id, scope, category, status,
                 objective_percent, actual_percent, error_budget_percent,
                 budget_consumed_percent, budget_remaining_percent,
                 total_count, good_count, bad_count, no_data,
                 window_hours, window_start, window_end,
                 failures_by_status_json, details_json, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                [
                    (
                        sla_run_id,
                        result.policy_id,
                        result.name,
                        result.target_id,
                        result.scope,
                        result.category,
                        result.status,
                        result.objective_percent,
                        result.actual_percent,
                        result.error_budget_percent,
                        result.budget_consumed_percent,
                        result.budget_remaining_percent,
                        result.total_count,
                        result.good_count,
                        result.bad_count,
                        1 if result.no_data else 0,
                        result.window_hours,
                        result.window_start,
                        result.window_end,
                        json.dumps(result.failures_by_status, ensure_ascii=False, sort_keys=True),
                        json.dumps({
                            "sli_code": result.sli_code, "domain": result.domain,
                            "policy_model": result.policy_model,
                            "current_status": result.current_status,
                            "affected_objects": result.affected_objects,
                            "aggregation": result.aggregation, "operator": result.comparison_operator,
                            "actual_value": result.actual_value, "objective_value": result.objective_value,
                            "unit": result.unit, "compliant": result.compliant,
                            "expected_sample_count": result.expected_sample_count,
                            "coverage_percent": result.coverage_percent,
                            "data_quality_status": result.data_quality_status,
                            "data_freshness_seconds": result.data_freshness_seconds,
                            "reason": result.reason, "required": result.required,
                            "error_budget_total": result.error_budget_total,
                            "error_budget_consumed": result.error_budget_consumed,
                            "error_budget_remaining": result.error_budget_remaining,
                            "burn_rate": result.burn_rate,
                            "burn_rates": result.burn_rates,
                            "burn_alert": result.burn_alert,
                        }, ensure_ascii=False, sort_keys=True),
                        finished_at,
                    )
                    for result in summary.results
                ],
            )
            return sla_run_id

    def fetch_recent_runs(self, *, limit: int = 20) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT sla_run_id, started_at, finished_at, status, policy_count, result_count,
                           passed_count, at_risk_count, failed_count, no_data_count,
                           window_end, message
                    FROM sla_runs
                    ORDER BY sla_run_id DESC
                    LIMIT ?;
                    """,
                    (limit,),
                )
            )

    def fetch_previous_state(self, *, before_run_id: int | None = None) -> dict[str, str] | None:
        """The ``{policy@target: status}`` map of the run before ``before_run_id``.

        Returns ``None`` — not an empty dict — when there is no earlier run. The two mean very
        different things to a state comparison: an empty map says "the previous run measured
        nothing", which would report every current failure as newly broken, while ``None`` says
        "there is nothing to compare against" and lets the caller establish a baseline instead of
        announcing a fleet-wide outage the first time it runs.

        ``before_run_id`` is the run being evaluated, which by this point has usually already been
        stored; without excluding it the comparison would be against itself and never see a change.
        """
        self.initialize()
        clause = "WHERE sla_run_id < ?" if before_run_id is not None else ""
        params: list[object] = [before_run_id] if before_run_id is not None else []
        with self.connect() as conn:
            previous = list(
                conn.execute(
                    f"SELECT sla_run_id FROM sla_runs {clause} ORDER BY sla_run_id DESC LIMIT 1;",
                    params,
                )
            )
            if not previous:
                return None
            rows = conn.execute(
                "SELECT policy_id, target_id, status FROM sla_results WHERE sla_run_id = ?;",
                (int(previous[0]["sla_run_id"]),),
            )
            return {state_key(row["policy_id"], row["target_id"]): str(row["status"]) for row in rows}

    def fetch_run_results(self, *, sla_run_id: int) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT policy_id, name, target_id, scope, category, status,
                           objective_percent, actual_percent, error_budget_percent,
                           budget_consumed_percent, budget_remaining_percent,
                           total_count, good_count, bad_count, no_data,
                           window_hours, window_start, window_end,
                           failures_by_status_json, details_json, collected_at
                    FROM sla_results
                    WHERE sla_run_id = ?
                    ORDER BY status, policy_id, target_id;
                    """,
                    (sla_run_id,),
                )
            )

    def _metric_results_exists(self) -> bool:
        """Is metric_results there yet? It is created by the metrics app, which may not have run."""
        with self.connect() as conn:
            return backend_mod.table_exists(conn, "metric_results")


def _build_scope_clause(
    target_ids: tuple[str, ...],
    server_ids: tuple[str, ...],
    db_types: tuple[str, ...],
) -> tuple[str, list[object]]:
    """Build the scope WHERE clause matching fetch_metric_samples' resolution order.

    target_ids takes precedence, then server_ids, with db_types as fallback.
    """
    if target_ids:
        placeholders = ", ".join("?" for _ in target_ids)
        return f"target_id IN ({placeholders})", list(target_ids)
    if server_ids:
        placeholders = ", ".join("?" for _ in server_ids)
        return f"server_id IN ({placeholders})", list(server_ids)
    if db_types:
        placeholders = ", ".join("?" for _ in db_types)
        return f"lower(db_type) IN ({placeholders})", [db_type.lower() for db_type in db_types]
    return "1=1", []


def _resolve_metric_source(conn: Any, include_archive: bool) -> str:
    """Return the table or subquery to read metric samples from.

    When include_archive is True and metric_results_archive exists, reads both tables
    via UNION ALL. Otherwise reads metric_results alone. The columns are named, not `*`: the
    archive carries `archived_at` besides, and a UNION of unequal column lists does not run.
    """
    if include_archive and backend_mod.table_exists(conn, "metric_results_archive"):
        columns = "target_id, server_id, db_type, metric_code, status, error_type, collected_at"
        return (f"(SELECT {columns} FROM metric_results UNION ALL "
                f"SELECT {columns} FROM metric_results_archive) mr")
    return "metric_results"


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_sql: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table});")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql};")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sla_runs
(
    sla_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    policy_count INTEGER NOT NULL DEFAULT 0,
    result_count INTEGER NOT NULL DEFAULT 0,
    passed_count INTEGER NOT NULL DEFAULT 0,
    at_risk_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    no_data_count INTEGER NOT NULL DEFAULT 0,
    window_end TEXT,
    message TEXT
);

CREATE INDEX IF NOT EXISTS ix_sla_runs_started_at ON sla_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS ix_sla_runs_status ON sla_runs(status);

CREATE TABLE IF NOT EXISTS sla_results
(
    sla_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sla_run_id INTEGER NOT NULL,
    policy_id TEXT NOT NULL,
    name TEXT,
    target_id TEXT NOT NULL DEFAULT '*',
    scope TEXT,
    category TEXT,
    status TEXT NOT NULL,
    objective_percent REAL NOT NULL,
    actual_percent REAL NOT NULL,
    error_budget_percent REAL NOT NULL DEFAULT 0,
    budget_consumed_percent REAL NOT NULL DEFAULT 0,
    budget_remaining_percent REAL NOT NULL DEFAULT 0,
    total_count INTEGER NOT NULL DEFAULT 0,
    good_count INTEGER NOT NULL DEFAULT 0,
    bad_count INTEGER NOT NULL DEFAULT 0,
    no_data INTEGER NOT NULL DEFAULT 0 CHECK (no_data IN (0, 1)),
    window_hours INTEGER NOT NULL DEFAULT 24,
    window_start TEXT,
    window_end TEXT,
    failures_by_status_json TEXT NOT NULL DEFAULT '{}',
    details_json TEXT NOT NULL DEFAULT '{}',
    collected_at TEXT NOT NULL,
    FOREIGN KEY (sla_run_id) REFERENCES sla_runs (sla_run_id)
);

CREATE INDEX IF NOT EXISTS ix_sla_results_run_id ON sla_results(sla_run_id);
CREATE INDEX IF NOT EXISTS ix_sla_results_policy_id ON sla_results(policy_id);
CREATE INDEX IF NOT EXISTS ix_sla_results_status ON sla_results(status);
"""
