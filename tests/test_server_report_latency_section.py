"""Per-server report latency percentiles section contract and payload builder.

Operators reading server-metrics.html need honest performance distributions across engines
without synthetic grading, arbitrary thresholds, or false failures.

Query Store (SQL Server) aggregates completed execution intervals per database server-side;
v$event_histogram (Oracle) records cumulative wait counts into power-of-two duration buckets;
and pg_stat_statements (PostgreSQL) records cumulative query execution totals without intra-query
variance.

The section displays what the collectors actually record:
- SQL Server and PostgreSQL percentiles are weighted by executions over per-plan or per-statement
  means, where variance inside a single plan/statement is invisible;
- Oracle percentiles represent real waits at discrete power-of-two bucket upper bounds (<= N ms);
- Configuration and baseline states (such as uninstalled extensions, single collections without a
  baseline, or instance restarts) are presented as operational states rather than monitoring
  failures.

These tests hold the payload shapes, engine-specific nature statements, and state translations
offline without database connectivity or I/O.
"""

from __future__ import annotations

import pytest

from typing import Any

from dbabrain.reports.server_latency import (
    LATENCY_CODES,
    ORACLE_NATURE,
    ORACLE_WAIT_HISTOGRAM_CODE,
    POSTGRES_STATEMENT_TOTALS_CODE,
    POSTGRESQL_NATURE,
    QUERY_LATENCY_PERCENTILES_CODE,
    SQLSERVER_NATURE,
    build_latency,
)
from dbabrain.reports.server_report import build_payload


def _make_row(
    metric_code: str,
    metric_item: str,
    metric_value: str,
    metric_unit: str,
    message: str,
    *,
    collected_at: str = "2026-10-15T10:00:00Z",
    target_id: str = "LAB-TARGET",
    server_id: str = "LAB-TARGET",
    status: str = "LOGGING",
) -> dict[str, Any]:
    """Build a metric row matching the exact store envelope produced by collectors."""
    return {
        "run_id": 1,
        "target_id": target_id,
        "server_id": server_id,
        "metric_code": metric_code,
        "metric_item": metric_item,
        "metric_value": metric_value,
        "metric_unit": metric_unit,
        "status": status,
        "importance": 2,
        "message": message,
        "collected_at": collected_at,
    }


def test_sql_server_query_store_rows_produce_per_database_intervals_with_percentiles():
    """Query Store interval rows group by database and carry execution-weighted plan percentiles."""
    rows = [
        # An earlier collection sample for the 10:00Z interval:
        _make_row(
            metric_code=QUERY_LATENCY_PERCENTILES_CODE,
            metric_item="sales_db @ 2026-10-15T10:00Z",
            metric_value="40",
            metric_unit="executions",
            message=(
                "database=sales_db, interval_start_utc=2026-10-15T10:00Z, "
                "interval_end_utc=2026-10-15T10:15Z, executions=40, plans=1, "
                "p50_ms=10.0, p95_ms=25.0, p99_ms=50.0, avg_ms=12.0, max_ms=80.0, "
                "measured_as=exec_weighted_plan_average"
            ),
            collected_at="2026-10-15T10:15:00Z",
        ),
        # A later collection carrying updated final counters for the same 10:00Z interval:
        _make_row(
            metric_code=QUERY_LATENCY_PERCENTILES_CODE,
            metric_item="sales_db @ 2026-10-15T10:00Z",
            metric_value="120",
            metric_unit="executions",
            message=(
                "database=sales_db, interval_start_utc=2026-10-15T10:00Z, "
                "interval_end_utc=2026-10-15T10:15Z, executions=120, plans=2, "
                "p50_ms=12.0, p95_ms=30.0, p99_ms=60.0, avg_ms=15.0, max_ms=90.0, "
                "measured_as=exec_weighted_plan_average"
            ),
            collected_at="2026-10-15T10:30:00Z",
        ),
        # Next completed interval:
        _make_row(
            metric_code=QUERY_LATENCY_PERCENTILES_CODE,
            metric_item="sales_db @ 2026-10-15T10:15Z",
            metric_value="80",
            metric_unit="executions",
            message=(
                "database=sales_db, interval_start_utc=2026-10-15T10:15Z, "
                "interval_end_utc=2026-10-15T10:30Z, executions=80, plans=1, "
                "p50_ms=8.0, p95_ms=20.0, p99_ms=40.0, avg_ms=10.0, max_ms=70.0, "
                "measured_as=exec_weighted_plan_average"
            ),
            collected_at="2026-10-15T10:30:00Z",
        ),
    ]

    payload = build_latency(rows)

    assert payload["collected"] is True
    assert payload["available"] is True
    assert payload["sqlserver"]["state"] == "ok"
    assert payload["sqlserver"]["nature"] == SQLSERVER_NATURE

    sql = payload["sqlserver"]
    assert sql["collected"] is True
    assert sql["available"] is True
    assert sql["state"] == "ok"
    assert sql["nature"] == SQLSERVER_NATURE

    databases = sql["databases"]
    assert len(databases) == 1
    db_entry = databases[0]
    assert db_entry["database"] == "sales_db"
    assert db_entry["name"] == "sales_db"

    intervals = db_entry["intervals"]
    assert len(intervals) == 2

    # Newest interval leads so the most recent completed window appears first
    newest = intervals[0]
    assert newest["interval_start_utc"] == "2026-10-15T10:15Z"
    assert newest["executions"] == 80.0
    assert newest["plans"] == 1.0
    assert newest["p50_ms"] == 8.0
    assert newest["p95_ms"] == 20.0
    assert newest["p99_ms"] == 40.0
    assert newest["avg_ms"] == 10.0
    assert newest["max_ms"] == 70.0

    # Overlapping 10:00Z interval collapsed to the row from 10:30Z with final execution count 120
    older = intervals[1]
    assert older["interval_start_utc"] == "2026-10-15T10:00Z"
    assert older["executions"] == 120.0
    assert older["plans"] == 2.0
    assert older["p50_ms"] == 12.0
    assert older["p95_ms"] == 30.0
    assert older["p99_ms"] == 60.0
    assert older["avg_ms"] == 15.0
    assert older["max_ms"] == 90.0


def test_oracle_two_collections_difference_buckets_into_interval_percentiles():
    """Subtracting consecutive Oracle wait histograms yields discrete bucket upper bounds."""
    older_row = _make_row(
        metric_code=ORACLE_WAIT_HISTOGRAM_CODE,
        metric_item="db file sequential read",
        metric_value="115",
        metric_unit="waits",
        message=(
            "event=db file sequential read, "
            "counters_since=2026-10-01T00:00:00Z, "
            "buckets=le_1ms:100|le_8ms:10|le_64ms:5"
        ),
        collected_at="2026-10-15T10:00:00Z",
    )
    newer_row = _make_row(
        metric_code=ORACLE_WAIT_HISTOGRAM_CODE,
        metric_item="db file sequential read",
        metric_value="1115",
        metric_unit="waits",
        message=(
            "event=db file sequential read, "
            "counters_since=2026-10-01T00:00:00Z, "
            "buckets=le_1ms:1000|le_8ms:100|le_64ms:15"
        ),
        collected_at="2026-10-15T10:15:00Z",
    )

    payload = build_latency([older_row, newer_row])

    assert payload["collected"] is True
    assert payload["available"] is True
    assert payload["oracle"]["state"] == "ok"
    assert payload["oracle"]["nature"] == ORACLE_NATURE

    ora = payload["oracle"]
    assert ora["collected"] is True
    assert ora["available"] is True
    assert ora["seconds"] == 900.0
    assert ora["nature"] == ORACLE_NATURE

    events = ora["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "db file sequential read"
    # Deltas: (1000-100) + (100-10) + (15-5) = 900 + 90 + 10 = 1000 total waits
    assert ev["waits"] == 1000
    # Discrete power-of-two bucket upper bounds: 90% in <=1ms, 99% in <=8ms
    assert ev["p50_ms_le"] == 1
    assert ev["p95_ms_le"] == 8
    assert ev["p99_ms_le"] == 8


def test_postgresql_two_collections_compute_a_statement_interval():
    """Consecutive pg_stat_statements collections yield call-weighted statement mean percentiles."""
    older_rows = [
        _make_row(
            metric_code=POSTGRES_STATEMENT_TOTALS_CODE,
            metric_item="all_statements",
            metric_value="10000",
            metric_unit="calls",
            message=(
                "queryid=all_statements, calls=10000, total_exec_time_ms=50000.0, "
                "counters_since=2026-10-01T00:00:00Z"
            ),
            collected_at="2026-10-15T10:00:00Z",
        ),
        _make_row(
            metric_code=POSTGRES_STATEMENT_TOTALS_CODE,
            metric_item="queryid:101",
            metric_value="1000",
            metric_unit="calls",
            message="queryid=101, calls=1000, total_exec_time_ms=1000.0",
            collected_at="2026-10-15T10:00:00Z",
        ),
        _make_row(
            metric_code=POSTGRES_STATEMENT_TOTALS_CODE,
            metric_item="queryid:202",
            metric_value="10",
            metric_unit="calls",
            message="queryid=202, calls=10, total_exec_time_ms=500.0",
            collected_at="2026-10-15T10:00:00Z",
        ),
    ]

    newer_rows = [
        _make_row(
            metric_code=POSTGRES_STATEMENT_TOTALS_CODE,
            metric_item="all_statements",
            metric_value="11001",
            metric_unit="calls",
            message=(
                "queryid=all_statements, calls=11001, total_exec_time_ms=51100.0, "
                "counters_since=2026-10-01T00:00:00Z"
            ),
            collected_at="2026-10-15T10:15:00Z",
        ),
        _make_row(
            metric_code=POSTGRES_STATEMENT_TOTALS_CODE,
            metric_item="queryid:101",
            metric_value="2000",
            metric_unit="calls",
            message="queryid=101, calls=2000, total_exec_time_ms=2000.0",
            collected_at="2026-10-15T10:15:00Z",
        ),
        _make_row(
            metric_code=POSTGRES_STATEMENT_TOTALS_CODE,
            metric_item="queryid:202",
            metric_value="11",
            metric_unit="calls",
            message="queryid=202, calls=11, total_exec_time_ms=600.0",
            collected_at="2026-10-15T10:15:00Z",
        ),
    ]

    payload = build_latency(older_rows + newer_rows)

    assert payload["collected"] is True
    assert payload["available"] is True
    assert payload["postgresql"]["state"] == "ok"
    assert payload["postgresql"]["nature"] == POSTGRESQL_NATURE

    pg = payload["postgresql"]
    assert pg["collected"] is True
    assert pg["available"] is True
    assert pg["state"] == "ok"
    assert pg["nature"] == POSTGRESQL_NATURE

    it = pg["interval"]
    assert it is not None
    assert it["seconds"] == 900.0
    assert it["calls"] == 1001
    assert it["statements"] == 2
    # 1000 calls of query 101 at 1.0 ms mean, 1 call of query 202 at 100.0 ms mean
    assert it["p50_ms"] == 1.0
    assert it["p95_ms"] == 1.0
    assert it["p99_ms"] == 1.0  # 1 call in 1001 is under 1 %: p99 reaches 1.0 (PERCENTILE_DISC)
    assert round(it["mean_ms"], 4) == round(1100.0 / 1001, 4)


def test_postgresql_not_installed_reports_the_not_configured_line():
    """An uninstalled extension reports its operational requirement rather than a monitoring error."""
    row = _make_row(
        metric_code=POSTGRES_STATEMENT_TOTALS_CODE,
        metric_item="pg_stat_statements",
        metric_value="NOT_INSTALLED",
        metric_unit="status",
        message=(
            "pg_stat_statements is not installed on this database (not configured is a state, "
            "not a failure: requires pg_stat_statements in shared_preload_libraries and CREATE EXTENSION)"
        ),
        collected_at="2026-10-15T10:00:00Z",
    )

    payload = build_latency([row])

    assert payload["collected"] is True
    assert payload["available"] is False
    assert payload["postgresql"]["state"] == "not_installed"
    assert "pg_stat_statements in shared_preload_libraries and CREATE EXTENSION" in payload["postgresql"]["reason"]

    pg = payload["postgresql"]
    assert pg["collected"] is True
    assert pg["available"] is False
    assert pg["state"] == "not_installed"
    assert "pg_stat_statements in shared_preload_libraries and CREATE EXTENSION" in pg["reason"]
    assert pg["interval"] is None


def test_a_cluster_enabled_inside_the_window_is_judged_by_its_newest_collections():
    """The morning's NOT_INSTALLED rows stay in the window after the extension is enabled; only the
    newest collection says whether it is installed now, or the section would deny it for a day."""
    def totals(calls, total_ms, at):
        return [
            _make_row(metric_code=POSTGRES_STATEMENT_TOTALS_CODE, metric_item="all_statements",
                      metric_value=str(total_ms), metric_unit="ms",
                      message=(f"queryid=all, dbname=all, calls={calls}, total_exec_time_ms={total_ms}, "
                               "counters_since=2026-10-15T08:50:36Z"),
                      collected_at=at),
            _make_row(metric_code=POSTGRES_STATEMENT_TOTALS_CODE, metric_item="queryid:101",
                      metric_value=str(total_ms), metric_unit="ms",
                      message=f"queryid=101, calls={calls}, total_exec_time_ms={total_ms}",
                      collected_at=at),
        ]

    not_installed = _make_row(
        metric_code=POSTGRES_STATEMENT_TOTALS_CODE, metric_item="pg_stat_statements",
        metric_value="NOT_INSTALLED", metric_unit="status",
        message="pg_stat_statements is not installed on this database",
        collected_at="2026-10-15T08:00:00Z")

    payload = build_latency([not_installed, *totals(100, 200.0, "2026-10-15T09:00:00Z"),
                             *totals(300, 600.0, "2026-10-15T09:15:00Z")])

    assert payload["postgresql"]["state"] == "ok"
    assert payload["postgresql"]["interval"]["calls"] == 200
    assert payload["postgresql"]["interval"]["covered_calls"] == 200


def test_a_statement_only_in_the_newer_collection_is_left_out_and_the_coverage_says_so():
    """Its counters run back to the reset, not to the older collection: counted from zero it could
    credit weeks of calls to minutes. The percentiles leave it out and say how much they cover."""
    def row(item, calls, total_ms, at):
        return _make_row(metric_code=POSTGRES_STATEMENT_TOTALS_CODE, metric_item=item,
                         metric_value=str(total_ms), metric_unit="ms",
                         message=(f"queryid={item.split(':')[-1]}, calls={calls}, "
                                  f"total_exec_time_ms={total_ms}, counters_since=2026-10-15T08:50:36Z"),
                         collected_at=at)

    older = [row("all_statements", 10, 1.0, "2026-10-15T09:00:00Z"),
             row("queryid:101", 10, 1.0, "2026-10-15T09:00:00Z")]
    newer = [row("all_statements", 15, 101.0, "2026-10-15T09:15:00Z"),
             row("queryid:101", 13, 1.3, "2026-10-15T09:15:00Z"),
             row("queryid:303", 2, 99.7, "2026-10-15T09:15:00Z")]

    interval = build_latency([*older, *newer])["postgresql"]["interval"]

    assert interval["calls"] == 5 and interval["covered_calls"] == 3
    assert interval["p99_ms"] == pytest.approx(0.1)  # the new, slow statement is not in it
    assert interval["mean_ms"] == pytest.approx(20.0)  # all of the interval is


def test_a_single_collection_reports_the_needs_two_collections_state():
    """Cumulative metrics without a prior baseline report that two collections are needed."""
    ora_row = _make_row(
        metric_code=ORACLE_WAIT_HISTOGRAM_CODE,
        metric_item="db file sequential read",
        metric_value="100",
        metric_unit="waits",
        message=(
            "event=db file sequential read, "
            "counters_since=2026-10-01T00:00:00Z, "
            "buckets=le_1ms:100"
        ),
        collected_at="2026-10-15T10:00:00Z",
    )
    payload_ora = build_latency([ora_row])
    assert payload_ora["collected"] is True
    assert payload_ora["available"] is False
    assert payload_ora["oracle"]["state"] == "needs_two_collections"
    assert "needs two collections with no restart between them" in payload_ora["oracle"]["reason"]
    assert payload_ora["oracle"]["state"] == "needs_two_collections"

    pg_row = _make_row(
        metric_code=POSTGRES_STATEMENT_TOTALS_CODE,
        metric_item="all_statements",
        metric_value="500",
        metric_unit="calls",
        message=(
            "queryid=all_statements, calls=500, total_exec_time_ms=1000.0, "
            "counters_since=2026-10-01T00:00:00Z"
        ),
        collected_at="2026-10-15T10:00:00Z",
    )
    payload_pg = build_latency([pg_row])
    assert payload_pg["collected"] is True
    assert payload_pg["available"] is False
    assert payload_pg["postgresql"]["state"] == "needs_two_collections"
    assert "needs two collections with no restart between them" in payload_pg["postgresql"]["reason"]
    assert payload_pg["postgresql"]["state"] == "needs_two_collections"


def test_a_reset_between_collections_reports_no_interval_and_the_reason():
    """An instance restart or counter reset invalidates the interval and states why."""
    # Oracle restart: counters_since shifts between collections
    older_ora = _make_row(
        metric_code=ORACLE_WAIT_HISTOGRAM_CODE,
        metric_item="db file sequential read",
        metric_value="100",
        metric_unit="waits",
        message="event=db file sequential read, counters_since=2026-10-01T00:00:00Z, buckets=le_1ms:100",
        collected_at="2026-10-15T10:00:00Z",
    )
    newer_ora = _make_row(
        metric_code=ORACLE_WAIT_HISTOGRAM_CODE,
        metric_item="db file sequential read",
        metric_value="10",
        metric_unit="waits",
        message="event=db file sequential read, counters_since=2026-10-15T10:05:00Z, buckets=le_1ms:10",
        collected_at="2026-10-15T10:15:00Z",
    )
    payload_ora = build_latency([older_ora, newer_ora])
    assert payload_ora["collected"] is True
    assert payload_ora["available"] is False
    assert payload_ora["oracle"]["state"] == "restart"
    assert "needs two collections with no restart between them" in payload_ora["oracle"]["reason"]
    assert payload_ora["oracle"]["state"] == "restart"

    # PostgreSQL restart: stats_reset / postmaster start shifts between collections
    older_pg = _make_row(
        metric_code=POSTGRES_STATEMENT_TOTALS_CODE,
        metric_item="all_statements",
        metric_value="1000",
        metric_unit="calls",
        message="queryid=all_statements, calls=1000, total_exec_time_ms=5000.0, counters_since=2026-10-01T00:00:00Z",
        collected_at="2026-10-15T10:00:00Z",
    )
    newer_pg = _make_row(
        metric_code=POSTGRES_STATEMENT_TOTALS_CODE,
        metric_item="all_statements",
        metric_value="200",
        metric_unit="calls",
        message="queryid=all_statements, calls=200, total_exec_time_ms=1000.0, counters_since=2026-10-15T10:05:00Z",
        collected_at="2026-10-15T10:15:00Z",
    )
    payload_pg = build_latency([older_pg, newer_pg])
    assert payload_pg["collected"] is True
    assert payload_pg["available"] is False
    assert payload_pg["postgresql"]["state"] == "restart"
    assert "needs two collections with no restart between them" in payload_pg["postgresql"]["reason"]
    assert payload_pg["postgresql"]["state"] == "restart"


def test_build_payload_carries_the_latency_key():
    """The server report payload exposes the latency block under the 'latency' key."""
    row = _make_row(
        metric_code=QUERY_LATENCY_PERCENTILES_CODE,
        metric_item="app_db @ 2026-10-15T10:00Z",
        metric_value="50",
        metric_unit="executions",
        message=(
            "database=app_db, interval_start_utc=2026-10-15T10:00Z, "
            "interval_end_utc=2026-10-15T10:15Z, executions=50, plans=1, "
            "p50_ms=10.0, p95_ms=20.0, p99_ms=30.0, avg_ms=12.0, max_ms=45.0, "
            "measured_as=exec_weighted_plan_average"
        ),
        collected_at="2026-10-15T10:15:00Z",
    )

    payload_with_latency = build_payload([], [], now=1760000000, latency_rows=[row])
    assert "latency" in payload_with_latency
    assert payload_with_latency["latency"]["collected"] is True
    assert payload_with_latency["latency"]["available"] is True
    assert payload_with_latency["latency"]["sqlserver"]["available"] is True

    payload_empty = build_payload([], [], now=1760000000)
    assert "latency" in payload_empty
    assert payload_empty["latency"]["collected"] is False
    assert payload_empty["latency"]["available"] is False


def test_empty_rows_report_nothing_collected_state():
    """When no latency metric rows exist for a server, the section declares no_data cleanly."""
    payload = build_latency([])

    assert payload["collected"] is False
    assert payload["available"] is False
    assert payload["postgresql"]["state"] == "no_data"
    assert payload["sqlserver"]["collected"] is False
    assert payload["oracle"]["collected"] is False
    assert payload["postgresql"]["collected"] is False


def test_latency_codes_constant_covers_all_three_collectors():
    """LATENCY_CODES must list the three metric codes fetched for the latency section."""
    assert set(LATENCY_CODES) == {
        QUERY_LATENCY_PERCENTILES_CODE,
        ORACLE_WAIT_HISTOGRAM_CODE,
        POSTGRES_STATEMENT_TOTALS_CODE,
    }


def _probe(target: str, status: str, seconds: float | None) -> dict[str, Any]:
    return {"server_id": "srv", "target_id": target, "metric_code": "INSTANCE_STATUS",
            "status": status, "collected_at": "2026-10-16T08:00:00Z", "execution_time": seconds}


def test_the_connection_probe_reports_real_percentiles_of_successful_probes():
    """INSTANCE_STATUS every 30 s, timed by metric-batch (0.30.0), is the one latency every engine
    gives at no cost - a real distribution, one connect and one query per sample. 100 probes from
    1 to 100 ms: p50 50, p95 95, p99 99 (PERCENTILE_DISC, as the other blocks). A failed probe is
    counted, not timed - it is a minute the availability SLO reads as down."""
    rows = [_probe("srv/sqlserver/A", "OK", ms / 1000.0) for ms in range(1, 101)]
    rows.append(_probe("srv/sqlserver/A", "CRITICAL", 5.0))

    probe = build_latency([], probe_rows=rows)["probe"]

    [entry] = probe["targets"]
    assert (entry["samples"], entry["failed"]) == (100, 1)
    assert (entry["p50_ms"], entry["p95_ms"], entry["p99_ms"], entry["max_ms"]) == pytest.approx((50, 95, 99, 100))
    assert probe["available"] is True


def test_probes_from_before_the_timing_existed_say_so():
    """Rows collected before 0.30.0 carry no execution_time: the block says no timed probe yet,
    rather than showing percentiles of nothing."""
    probe = build_latency([], probe_rows=[_probe("srv/oracle/B", "OK", None)])["probe"]

    assert probe["collected"] is True
    assert probe["available"] is False
    assert probe["targets"][0]["untimed"] == 1
    assert "0.30.0" in probe["reason"]
