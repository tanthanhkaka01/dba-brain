"""``DbOpsStore``'s methods for reports, their send state, and the metric results they read.

Split out of ``db/store.py`` on 2026-10-03 (see ``db/store_base.py``). A mixin, not a store:
``DbOpsStore`` composes it, and its methods use the store's own ``connect`` and ``initialize``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from datetime import datetime, timedelta, timezone

from db_ops.db.store_base import utc_now_text


class ReportsMixin:

    def insert_report(
        self,
        *,
        report_code: str,
        report_name: str,
        report_type: str,
        report_level: str,
        report_text: str,
        status: str = "created",
        source_type: str | None = None,
        source_id: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        self.initialize()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reports
                (
                    report_code,
                    report_name,
                    report_type,
                    report_level,
                    status,
                    report_text,
                    source_type,
                    source_id,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    report_code,
                    report_name,
                    report_type,
                    report_level,
                    status,
                    report_text,
                    source_type,
                    source_id,
                    metadata_json,
                ),
            )
            return int(cursor.lastrowid)

    def fetch_reports_for_push(
        self,
        *,
        limit: int = 50,
        report_type: str | None = None,
        report_level: str | None = None,
        report_ids: list[int] | None = None,
        target_id: str | None = None,
    ) -> list[sqlite3.Row]:
        self.initialize()
        clauses = ["status = 'created'"]
        params: list[object] = []
        if report_ids is not None:
            if not report_ids:
                return []
            placeholders = ", ".join("?" for _ in report_ids)
            clauses.append(f"report_id IN ({placeholders})")
            params.extend(report_ids)
        if report_type:
            clauses.append("report_type = ?")
            params.append(report_type)
        if report_level:
            clauses.append("report_level = ?")
            params.append(report_level)
        if target_id:
            clauses.append("json_extract(metadata_json, '$.target_id') = ?")
            params.append(target_id)
        params.append(limit)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT
                        report_id,
                        report_code,
                        report_name,
                        report_type,
                        report_level,
                        status,
                        report_text,
                        source_type,
                        source_id,
                        created_at,
                        pushed_at,
                        telegram_send_message_id,
                        metadata_json
                    FROM reports
                    WHERE {" AND ".join(clauses)}
                    ORDER BY created_at ASC, report_id ASC
                    LIMIT ?;
                    """,
                    params,
                )
            )

    def mark_report_pushed(self, *, report_id: int, telegram_send_message_id: int) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reports
                SET
                    status = 'pushed',
                    pushed_at = ?,
                    telegram_send_message_id = ?
                WHERE report_id = ?;
                """,
                (utc_now_text(), telegram_send_message_id, report_id),
            )

    def mark_report_skipped(self, *, report_id: int, reason: str) -> None:
        self.initialize()
        metadata_json = json.dumps({"skip_reason": reason}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reports
                SET
                    status = 'skipped',
                    metadata_json = ?
                WHERE report_id = ?;
                """,
                (metadata_json, report_id),
            )

    def report_exists_on_local_date(self, *, report_code: str, local_date: str,
                                    utc_offset_minutes: int) -> bool:
        """Has this report already been produced on the given **local** calendar day?

        The daily guard behind a once-a-day report. Local, not UTC, because "today" for the
        operator reading it ends at local midnight — a report generated at 07:30 local is
        yesterday in UTC, and a UTC-day guard lets the same report out twice.

        Only ``created``/``pushed`` count: a row that failed to generate is not a report that
        happened, and treating it as one silences the retry.

        **The window is computed here and bound as two plain strings.** This used to ask SQLite
        for ``substr(datetime(created_at, '+7 hours'), 1, 10)``. ``datetime(text, text)`` is not a
        PostgreSQL function, the translator does not rewrite it, and the compatibility layer
        supplies only the two JSON functions - so the same call answered ``False`` on SQLite and
        raised ``42883 function datetime(text, unknown) does not exist`` on PostgreSQL, measured on
        the two live stores on 2026-09-04. It stayed hidden because the scheduled path passes
        ``force=True`` and short-circuits this before the query is built; a manual
        ``db-ops reports create-backup-health-report`` on a PostgreSQL store is what reaches it.

        A half-open range over the stored text is the same answer on both engines - the format
        sorts lexicographically, which is why it is the store's format - and unlike a wrapped
        column it can use an index on ``created_at``.

        A ``local_date`` that is not a date raises rather than answering ``False``: "no report
        today" is the answer that lets a duplicate out.

        ``utc_offset_minutes`` has **no default**, on purpose. It used to default to 7 hours — a
        Vietnam business calendar, decided in the store layer, applying to every operator of a
        published tool. Whose midnight this is is the caller's decision, and the caller takes it
        from the configured timezone (``db_ops.lib.timezone``). Minutes rather than hours because
        half-hour zones exist and an int of hours cannot express +05:30.
        """
        self.initialize()
        try:
            midnight = datetime.strptime(str(local_date), "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"local_date must be YYYY-MM-DD, got {local_date!r}") from exc
        offset = timedelta(minutes=int(utc_offset_minutes))
        window_start = (midnight - offset).strftime("%Y-%m-%dT%H:%M:%SZ")
        window_end = (midnight + timedelta(days=1) - offset).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM reports
                WHERE report_code = ?
                  AND created_at >= ?
                  AND created_at < ?
                  AND status IN ('created', 'pushed')
                LIMIT 1;
                """,
                (report_code, window_start, window_end),
            ).fetchone()
        return row is not None

    def recent_alert_exists(self, *, source_id: str, within_seconds: int,
                            source_types: tuple[str, ...] = ("metrics", "reports")) -> bool:
        """Was an alert for this source queued recently — the dedupe check before queueing another.

        Counts a message in any non-terminal or delivered state (``send_status`` 0/1/2): one still
        waiting in the queue is exactly as much a duplicate as one already sent, and skipping
        queued rows is how a stuck queue turns into a burst of identical messages when it drains.

        A message too long for one Telegram body is queued as ``<source_id>:part:<n>``, and the
        exact match never found those - so a long alert was never a duplicate of itself (review
        0.25.0, F7.2). The parts match too, by an exact prefix rather than ``LIKE``, whose ``_``
        would match any character in an id like ``metrics_latest:critical:LAB_01``.
        """
        self.initialize()
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=int(within_seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")
        placeholders = ",".join("?" for _ in source_types)
        parts_prefix = f"{source_id}:part:"
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT 1
                FROM telegram_send_messages
                WHERE source_type IN ({placeholders})
                  AND (source_id = ? OR substr(source_id, 1, ?) = ?)
                  AND row_ins_date >= ?
                  AND send_status IN (0, 1, 2)
                LIMIT 1;
                """,
                (*source_types, source_id, len(parts_prefix), parts_prefix, cutoff),
            ).fetchone()
        return row is not None

    def fetch_report_send_state(self, *, report_code: str, channel: str) -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    report_code,
                    channel,
                    last_sent_at,
                    last_run_at,
                    last_status,
                    last_skipped_reason,
                    updated_at
                FROM report_send_state
                WHERE report_code = ?
                  AND channel = ?;
                """,
                (report_code, channel),
            ).fetchone()

    def upsert_report_send_state(
        self,
        *,
        report_code: str,
        channel: str,
        last_run_at: str | None = None,
        last_status: str,
        last_skipped_reason: str = "",
        last_sent_at: str | None = None,
    ) -> None:
        """Record what the last evaluation of one report+channel did.

        ``last_run_at`` is **when the run that last produced a report started**, and it is the
        anchor the schedule counts ``repeat_interval`` from — so it is written only by a run that
        actually sent, and ``None`` (the default) leaves the stored one alone. It used to be
        written on every evaluation, skips included, which made it "when the scheduler last
        looked": a number that moves every sweep and can anchor nothing. The skip still records
        itself in ``last_status`` / ``last_skipped_reason``.
        """
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO report_send_state
                (
                    report_code,
                    channel,
                    last_sent_at,
                    last_run_at,
                    last_status,
                    last_skipped_reason,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_code, channel) DO UPDATE SET
                    last_sent_at = COALESCE(excluded.last_sent_at, report_send_state.last_sent_at),
                    last_run_at = COALESCE(excluded.last_run_at, report_send_state.last_run_at),
                    last_status = excluded.last_status,
                    last_skipped_reason = excluded.last_skipped_reason,
                    updated_at = excluded.updated_at;
                """,
                (
                    report_code,
                    channel,
                    last_sent_at,
                    last_run_at,
                    last_status,
                    last_skipped_reason,
                    utc_now_text(),
                ),
            )

    def fetch_latest_metric_report_results(
        self,
        *,
        db_type: str | None = None,
        target_id: str | None = None,
        target_ip: str | None = None,
        metric_code: str | None = None,
        metric_codes: set[str] | None = None,
        collected_at_gte: str | None = None,
        unreported_only: bool = False,
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
        if target_ip:
            clauses.append("r.ip = ?")
            params.append(target_ip)
        if metric_code:
            clauses.append("r.metric_code = ?")
            params.append(metric_code)
        elif metric_codes is not None:
            if not metric_codes:
                return []
            placeholders = ", ".join("?" for _ in metric_codes)
            clauses.append(f"r.metric_code IN ({placeholders})")
            params.extend(sorted(metric_codes))
        if collected_at_gte:
            clauses.append("r.collected_at >= ?")
            params.append(collected_at_gte)
        if unreported_only:
            clauses.append("r.daily_report_created = 0")
        extra_where = " AND " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT r.result_id, r.run_id, r.collected_at, r.target_id, r.server_id, r.ip,
                           r.db_type, r.db_name, r.metric_code, r.metric_item, r.metric_value,
                           r.metric_unit, r.status, r.importance, r.message, r.daily_report_created,
                           r.collector_type, r.category, r.error_type, r.normalized_error_signature
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

    def fetch_metric_report_results(
        self,
        *,
        db_type: str | None = None,
        server_id: str | None = None,
        target_id: str | None = None,
        target_ip: str | None = None,
        metric_code: str | None = None,
        metric_codes: set[str] | None = None,
        collected_at_gte: str | None = None,
        collected_at_lte: str | None = None,
        unreported_only: bool = False,
    ) -> list[sqlite3.Row]:
        self.initialize()
        clauses: list[str] = []
        params: list[object] = []
        if db_type:
            clauses.append("r.db_type = ?")
            params.append(db_type.lower())
        if server_id:
            clauses.append("r.server_id = ?")
            params.append(server_id)
        if target_id:
            clauses.append("r.target_id = ?")
            params.append(target_id)
        if target_ip:
            clauses.append("r.ip = ?")
            params.append(target_ip)
        if metric_code:
            clauses.append("r.metric_code = ?")
            params.append(metric_code)
        elif metric_codes is not None:
            if not metric_codes:
                return []
            placeholders = ", ".join("?" for _ in metric_codes)
            clauses.append(f"r.metric_code IN ({placeholders})")
            params.extend(sorted(metric_codes))
        if collected_at_gte:
            clauses.append("r.collected_at >= ?")
            params.append(collected_at_gte)
        if collected_at_lte:
            clauses.append("r.collected_at <= ?")
            params.append(collected_at_lte)
        if unreported_only:
            clauses.append("r.daily_report_created = 0")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT r.result_id, r.run_id, r.collected_at, r.target_id, r.server_id, r.ip,
                           r.db_type, r.db_name, r.metric_code, r.metric_item, r.metric_value,
                           r.metric_unit, r.status, r.importance, r.message, r.daily_report_created,
                           r.collector_type, r.category, r.error_type, r.normalized_error_signature
                    FROM metric_results AS r
                    {where}
                    ORDER BY r.collected_at, r.metric_code, r.target_id, r.result_id;
                    """,
                    params,
                )
            )

    def mark_metric_daily_report_created_for_scope(
        self,
        *,
        metric_codes: set[str],
        target_ids: set[str],
        collected_at_lte: str,
    ) -> int:
        self.initialize()
        clean_metric_codes = sorted({str(metric_code).strip() for metric_code in metric_codes if str(metric_code).strip()})
        clean_target_ids = sorted({str(target_id).strip() for target_id in target_ids if str(target_id).strip()})
        cutoff = str(collected_at_lte or "").strip()
        if not clean_metric_codes or not clean_target_ids or not cutoff:
            return 0

        metric_placeholders = ", ".join("?" for _ in clean_metric_codes)
        target_placeholders = ", ".join("?" for _ in clean_target_ids)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE metric_results
                SET daily_report_created = 1
                WHERE daily_report_created = 0
                  AND metric_code IN ({metric_placeholders})
                  AND target_id IN ({target_placeholders})
                  AND collected_at <= ?;
                """,
                [*clean_metric_codes, *clean_target_ids, cutoff],
            )
            return int(cursor.rowcount)

    # `mark_metric_daily_report_created` was deleted on 2026-08-16: it was byte-identical to
    # `MetricStore.mark_daily_report_created` and **nothing called it**. Two methods writing
    # `metric_results.daily_report_created` is a rule with two versions, and the second one is
    # found by whoever is debugging why the first did not apply. The scoped variant next to it
    # is the live one; the row-id form lives on `MetricStore`, which owns that table.

    def fetch_latest_metric_run_meta(self) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, started_at, finished_at, status, target_count, metric_count,
                       result_count, error_count, warning_count, critical_count, message
                FROM metric_runs
                ORDER BY run_id DESC
                LIMIT 1;
                """
            ).fetchone()
        return dict(row) if row else None
