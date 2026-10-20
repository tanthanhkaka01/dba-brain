"""Latency percentile arithmetic across collections for Query Store, Oracle, and Postgres.

Metrics collectors record raw numbers and never grade: Query Store intervals are pre-aggregated
runtime stats on SQL Server, V$EVENT_HISTOGRAM records cumulative wait counts by power-of-two
buckets on Oracle, and pg_stat_statements records execution totals per statement on PostgreSQL.
The arithmetic across collections lives in dbabrain/lib and is pure functions with no I/O.

These tests verify that:
- duplicate Query Store intervals collapse to the newest collection, rendering collection
  overlaps harmless;
- Oracle wait event histogram pairs across an instance restart or with negative counter deltas
  are refused, and valid pairs tile time without gaps or overlaps;
- bucket percentiles for wait histograms land on discrete power-of-two bucket upper bounds
  without unrecorded interpolations;
- PostgreSQL statement intervals weight per-statement means by calls, correctly reflecting the
  dominant query workload;
- uninstalled extensions or counter resets return None;
- weighted percentiles correctly handle edge cases including single pairs, concentrated weights,
  and empty inputs.
"""

from __future__ import annotations

from dbabrain.lib.latency_percentiles import (
    histogram_interval,
    histogram_series,
    query_store_intervals,
    statement_interval,
    weighted_percentiles,
)


def test_duplicate_query_store_intervals_collapse_to_the_newest():
    """Two collections overlapping on an interval collapse to the row from the newest collection."""
    rows = [
        {
            "target_id": "LAB-192-0-2-10",
            "metric_item": "app_db @ 2026-10-15T10:00Z",
            "collected_at": "2026-10-15T10:15:00Z",
            "message": (
                "database=app_db, interval_start_utc=2026-10-15T10:00Z, "
                "interval_end_utc=2026-10-15T10:15Z, executions=50, plans=1, "
                "p50_ms=10.0, p95_ms=25.0, p99_ms=50.0, avg_ms=12.0, max_ms=80.0, "
                "measured_as=duration"
            ),
        },
        {
            "target_id": "LAB-192-0-2-10",
            "metric_item": "app_db @ 2026-10-15T10:00Z",
            # A later collection carries the same interval with updated final counters
            "collected_at": "2026-10-15T10:30:00Z",
            "message": (
                "database=app_db, interval_start_utc=2026-10-15T10:00Z, "
                "interval_end_utc=2026-10-15T10:15Z, executions=120, plans=2, "
                "p50_ms=12.0, p95_ms=30.0, p99_ms=60.0, avg_ms=15.0, max_ms=90.0, "
                "measured_as=duration"
            ),
        },
        {
            "target_id": "LAB-192-0-2-10",
            "metric_item": "app_db @ 2026-10-15T10:15Z",
            "collected_at": "2026-10-15T10:30:00Z",
            "message": (
                "database=app_db, interval_start_utc=2026-10-15T10:15Z, "
                "interval_end_utc=2026-10-15T10:30Z, executions=80, plans=1, "
                "p50_ms=8.0, p95_ms=20.0, p99_ms=40.0, avg_ms=10.0, max_ms=70.0, "
                "measured_as=duration"
            ),
        },
    ]

    results = query_store_intervals(rows)

    assert len(results) == 2
    first, second = results[0], results[1]

    # The 10:00Z interval took the row from 10:30Z, discarding the earlier 10:15Z reading
    assert first["interval_start_utc"] == "2026-10-15T10:00Z"
    assert first["executions"] == 120.0
    assert isinstance(first["executions"], float)
    assert first["plans"] == 2.0
    assert first["p50_ms"] == 12.0
    assert first["p95_ms"] == 30.0
    assert first["p99_ms"] == 60.0
    assert first["avg_ms"] == 15.0
    assert first["max_ms"] == 90.0
    assert first["measured_as"] == "duration"

    # Sorted by interval_start
    assert second["interval_start_utc"] == "2026-10-15T10:15Z"
    assert second["executions"] == 80.0


def test_a_histogram_pair_across_an_instance_restart_is_refused():
    """When counters_since differ, the instance restarted and the pair cannot be differenced."""
    older = {
        "metric_item": "db file sequential read",
        "collected_at": "2026-10-15T10:00:00Z",
        "message": (
            "event=db file sequential read, "
            "counters_since=2026-10-01T00:00:00Z, "
            "buckets=le_1ms:100|le_2ms:50"
        ),
    }
    newer = {
        "metric_item": "db file sequential read",
        "collected_at": "2026-10-15T10:15:00Z",
        "message": (
            "event=db file sequential read, "
            "counters_since=2026-10-15T10:05:00Z, "
            "buckets=le_1ms:10|le_2ms:5"
        ),
    }

    assert histogram_interval(older, newer) is None


def test_a_histogram_pair_with_any_negative_delta_is_refused():
    """A counter that moved backwards means the baseline reset and the pair is invalid."""
    older = {
        "metric_item": "db file sequential read",
        "collected_at": "2026-10-15T10:00:00Z",
        "message": (
            "event=db file sequential read, "
            "counters_since=2026-10-01T00:00:00Z, "
            "buckets=le_1ms:100|le_2ms:50"
        ),
    }
    newer = {
        "metric_item": "db file sequential read",
        "collected_at": "2026-10-15T10:15:00Z",
        # le_2ms dropped from 50 to 40
        "message": (
            "event=db file sequential read, "
            "counters_since=2026-10-01T00:00:00Z, "
            "buckets=le_1ms:200|le_2ms:40"
        ),
    }

    assert histogram_interval(older, newer) is None


def test_bucket_percentiles_land_on_the_right_bound_for_a_hand_built_histogram():
    """900 waits <=1ms, 90 <=8ms, 10 <=64ms: p50 1, p95 8 and p99 8 - 990 of the 1000 waits are
    at or below 8 ms, so the 99th percentile is reached there (PERCENTILE_DISC), not at 64."""
    older = {
        "metric_item": "db file sequential read",
        "collected_at": "2026-10-15T10:00:00Z",
        "message": (
            "event=db file sequential read, "
            "counters_since=2026-10-01T00:00:00Z, "
            "buckets=le_1ms:100|le_8ms:10|le_64ms:5"
        ),
    }
    newer = {
        "metric_item": "db file sequential read",
        "collected_at": "2026-10-15T10:15:00Z",
        "message": (
            "event=db file sequential read, "
            "counters_since=2026-10-01T00:00:00Z, "
            "buckets=le_1ms:1000|le_8ms:100|le_64ms:15"
        ),
    }

    result = histogram_interval(older, newer)

    assert result is not None
    assert result["event"] == "db file sequential read"
    assert result["seconds"] == 900.0
    assert result["waits"] == 1000
    assert result["p50_ms_le"] == 1
    assert result["p95_ms_le"] == 8
    assert result["p99_ms_le"] == 8
    assert result["buckets_delta"] == {"le_1ms": 900, "le_8ms": 90, "le_64ms": 10}


def test_a_histogram_interval_with_zero_waits_returns_none_for_all_percentiles():
    """An interval where no waits occurred reports zero waits and None for percentiles."""
    older = {
        "metric_item": "db file sequential read",
        "collected_at": "2026-10-15T10:00:00Z",
        "message": "event=db file sequential read, counters_since=2026-10-01T00:00:00Z, buckets=le_1ms:100",
    }
    newer = {
        "metric_item": "db file sequential read",
        "collected_at": "2026-10-15T10:15:00Z",
        "message": "event=db file sequential read, counters_since=2026-10-01T00:00:00Z, buckets=le_1ms:100",
    }

    result = histogram_interval(older, newer)

    assert result is not None
    assert result["waits"] == 0
    assert result["p50_ms_le"] is None
    assert result["p95_ms_le"] is None
    assert result["p99_ms_le"] is None


def test_consecutive_histogram_pairs_cover_each_interval_once():
    """Consecutive histogram pairs tile time exactly with no gap and no overlap."""
    rows = [
        {
            "metric_item": "db file sequential read",
            "collected_at": "2026-10-15T10:00:00Z",
            "message": "event=db file sequential read, counters_since=2026-10-01T00:00:00Z, buckets=le_1ms:100|le_2ms:50",
        },
        {
            "metric_item": "db file sequential read",
            "collected_at": "2026-10-15T10:15:00Z",
            "message": "event=db file sequential read, counters_since=2026-10-01T00:00:00Z, buckets=le_1ms:200|le_2ms:80",
        },
        {
            "metric_item": "db file sequential read",
            "collected_at": "2026-10-15T10:30:00Z",
            "message": "event=db file sequential read, counters_since=2026-10-01T00:00:00Z, buckets=le_1ms:350|le_2ms:120",
        },
    ]

    series = histogram_series(rows)

    assert len(series) == 2
    # Pair 0 covers 10:00 to 10:15
    assert series[0]["seconds"] == 900.0
    assert series[0]["waits"] == 130
    assert series[0]["buckets_delta"] == {"le_1ms": 100, "le_2ms": 30}
    # Pair 1 covers 10:15 to 10:30
    assert series[1]["seconds"] == 900.0
    assert series[1]["waits"] == 190
    assert series[1]["buckets_delta"] == {"le_1ms": 150, "le_2ms": 40}


def test_histogram_series_skips_refused_pairs_across_a_restart():
    """A series with an intervening restart drops the broken pair while keeping valid ones."""
    rows = [
        {
            "metric_item": "db file sequential read",
            "collected_at": "2026-10-15T10:00:00Z",
            "message": "event=db file sequential read, counters_since=2026-10-01T00:00:00Z, buckets=le_1ms:100",
        },
        {
            "metric_item": "db file sequential read",
            "collected_at": "2026-10-15T10:15:00Z",
            "message": "event=db file sequential read, counters_since=2026-10-01T00:00:00Z, buckets=le_1ms:200",
        },
        {
            "metric_item": "db file sequential read",
            # Restart occurred before this collection
            "collected_at": "2026-10-15T10:30:00Z",
            "message": "event=db file sequential read, counters_since=2026-10-15T10:20:00Z, buckets=le_1ms:30",
        },
        {
            "metric_item": "db file sequential read",
            "collected_at": "2026-10-15T10:45:00Z",
            "message": "event=db file sequential read, counters_since=2026-10-15T10:20:00Z, buckets=le_1ms:80",
        },
    ]

    series = histogram_series(rows)

    # Pair (10:00->10:15) succeeds; pair (10:15->10:30) is refused; pair (10:30->10:45) succeeds
    assert len(series) == 2
    assert series[0]["waits"] == 100
    assert series[1]["waits"] == 50


def test_statement_interval_weights_by_calls_so_a_frequent_fast_query_dominates_p50():
    """A fast statement called 1000 times and a slow one called once makes p50 the fast mean."""
    older_rows = [
        {
            "metric_item": "all_statements",
            "collected_at": "2026-10-15T10:00:00Z",
            "message": (
                "queryid=all_statements, calls=10000, total_exec_time_ms=50000.0, "
                "counters_since=2026-10-01T00:00:00Z"
            ),
        },
        {
            "metric_item": "queryid:101",
            "collected_at": "2026-10-15T10:00:00Z",
            "message": "queryid=101, calls=1000, total_exec_time_ms=1000.0",
        },
        {
            "metric_item": "queryid:202",
            "collected_at": "2026-10-15T10:00:00Z",
            "message": "queryid=202, calls=10, total_exec_time_ms=500.0",
        },
    ]
    newer_rows = [
        {
            "metric_item": "all_statements",
            "collected_at": "2026-10-15T10:15:00Z",
            "message": (
                "queryid=all_statements, calls=11001, total_exec_time_ms=51100.0, "
                "counters_since=2026-10-01T00:00:00Z"
            ),
        },
        {
            "metric_item": "queryid:101",
            "collected_at": "2026-10-15T10:15:00Z",
            # delta calls: 1000, delta time: 1000.0 ms -> mean: 1.0 ms
            "message": "queryid=101, calls=2000, total_exec_time_ms=2000.0",
        },
        {
            "metric_item": "queryid:202",
            "collected_at": "2026-10-15T10:15:00Z",
            # delta calls: 1, delta time: 100.0 ms -> mean: 100.0 ms
            "message": "queryid=202, calls=11, total_exec_time_ms=600.0",
        },
    ]

    result = statement_interval(older_rows, newer_rows)

    assert result is not None
    assert result["seconds"] == 900.0
    assert result["calls"] == 1001
    assert result["statements"] == 2
    # Fast query (101) mean is 1.0 ms, called 1000 times; slow query (202) mean is 100.0 ms, called 1 time.
    # p50 is dominated by the fast query's mean.
    assert result["p50_ms"] == 1.0
    # Mean of all_statements: delta total time 1100.0 ms / delta calls 1001
    assert round(result["mean_ms"], 4) == round(1100.0 / 1001, 4)


def test_statement_interval_returns_none_when_pg_stat_statements_is_not_installed():
    """When pg_stat_statements is not installed, no interval can be computed."""
    older_rows = [
        {
            "metric_item": "pg_stat_statements",
            "metric_value": "NOT_INSTALLED",
            "collected_at": "2026-10-15T10:00:00Z",
            "message": "pg_stat_statements is not installed on this database",
        }
    ]
    newer_rows = [
        {
            "metric_item": "all_statements",
            "collected_at": "2026-10-15T10:15:00Z",
            "message": (
                "queryid=all_statements, calls=500, total_exec_time_ms=1000.0, "
                "counters_since=2026-10-01T00:00:00Z"
            ),
        }
    ]

    assert statement_interval(older_rows, newer_rows) is None
    assert statement_interval(newer_rows, older_rows) is None


def test_statement_interval_returns_none_when_all_statements_counters_since_differ():
    """A reset of pg_stat_statements or server restart makes counters_since differ and returns None."""
    older_rows = [
        {
            "metric_item": "all_statements",
            "collected_at": "2026-10-15T10:00:00Z",
            "message": "queryid=all_statements, calls=1000, total_exec_time_ms=5000.0, counters_since=2026-10-01T00:00:00Z",
        }
    ]
    newer_rows = [
        {
            "metric_item": "all_statements",
            "collected_at": "2026-10-15T10:15:00Z",
            "message": "queryid=all_statements, calls=200, total_exec_time_ms=1000.0, counters_since=2026-10-15T10:05:00Z",
        }
    ]

    assert statement_interval(older_rows, newer_rows) is None


def test_statement_interval_returns_none_when_all_statements_calls_delta_is_negative():
    """Negative calls delta indicates statistics reset and is refused."""
    older_rows = [
        {
            "metric_item": "all_statements",
            "collected_at": "2026-10-15T10:00:00Z",
            "message": "queryid=all_statements, calls=1000, total_exec_time_ms=5000.0, counters_since=2026-10-01T00:00:00Z",
        }
    ]
    newer_rows = [
        {
            "metric_item": "all_statements",
            "collected_at": "2026-10-15T10:15:00Z",
            # Calls dropped from 1000 to 500
            "message": "queryid=all_statements, calls=500, total_exec_time_ms=2500.0, counters_since=2026-10-01T00:00:00Z",
        }
    ]

    assert statement_interval(older_rows, newer_rows) is None


def test_weighted_percentiles_handles_edge_cases_of_single_pair_concentrated_weight_and_empty():
    """Single pairs, all weight on one value, and empty inputs return consistent discrete percentiles."""
    quantiles = (50, 95, 99)

    # Single pair: every quantile lands on the single value
    single = weighted_percentiles([(42.0, 10.0)], quantiles)
    assert single == {50: 42.0, 95: 42.0, 99: 42.0}

    # All weight on one value across multiple items
    concentrated = weighted_percentiles([(7.5, 3.0), (7.5, 9.0)], quantiles)
    assert concentrated == {50: 7.5, 95: 7.5, 99: 7.5}

    # Empty pairs returns None for all requested quantiles
    empty = weighted_percentiles([], quantiles)
    assert empty == {50: None, 95: None, 99: None}

    # Zero or negative weights are ignored; if no positive weight remains, all return None
    non_positive = weighted_percentiles([(10.0, 0.0), (20.0, -5.0)], quantiles)
    assert non_positive == {50: None, 95: None, 99: None}


def test_a_percentile_is_reached_not_exceeded():
    """PERCENTILE_DISC, the definition the SQL Server collector computes server-side: the smallest
    value whose cumulative weight reaches the quantile. Half the weight at 10 ms and half at 20 ms
    puts the median at 10, and a strictly-exceeds rule would have said 20."""
    result = weighted_percentiles([(10.0, 1.0), (20.0, 1.0)], (50, 100))

    assert result[50] == 10.0
    assert result[100] == 20.0
