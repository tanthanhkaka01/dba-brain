"""`PERFORMANCE_WAIT_STATS` measures an interval, and a single DMV read cannot.

`sys.dm_os_wait_stats` is cumulative since the engine started. Read once, it answers "what has
this instance waited on since it booted", and on 192.0.2.115 with 22 days of uptime that answer was
`SOS_WORK_DISPATCHER` at 361,631,036 seconds on all 350 stored samples — status OK on every one,
while users were blocked on `LCK_M_U` for up to 30 minutes. An idle-scheduler counter accrues a
second per scheduler per second, so it out-ranks every real wait forever and no threshold on it can
move.

The fix takes both samples inside one execution. These tests pin the properties that make that
work, each of which was a real failure found by running the collector against the estate on
2026-08-25 rather than by reading it:

* the second read has to happen, and the reported value has to be the difference;
* the idle waits have to be excluded, because on an interval an idle counter *is* the interval;
* each sample has to be materialised before it is aggregated, or a major-version-10 build fails
  with a duplicate-key violation on a DMV that has no duplicates;
* and the grading has to be gated on the instance being busy, or a box doing nothing warns forever.
"""

import pytest
from db_ops.lib.paths import resolve_tool_path

MODERN = resolve_tool_path("assets/metrics/sqlserver/014_sqlserver_wait_stats.sql")
LEGACY = resolve_tool_path("assets/metrics/sqlserver/legacy_2008r2/014_sqlserver_wait_stats.sql")

BOTH = pytest.mark.parametrize("path", [MODERN, LEGACY], ids=["modern", "legacy_2008r2"])


def _sql(path):
    return path.read_text(encoding="utf-8")


@BOTH
def test_the_collector_reads_the_dmv_twice_with_a_wait_between(path):
    """One read cannot produce an interval, however it is graded afterwards."""
    sql = _sql(path)

    assert sql.count("FROM sys.dm_os_wait_stats") == 2
    assert "WAITFOR DELAY @sample_delay" in sql


@BOTH
def test_the_reported_value_is_the_difference_not_the_total(path):
    sql = _sql(path)

    assert "SUM(r.wait_time_ms) - MIN(f.wait_time_ms)" in sql
    # The old note claimed the opposite and is what a reader would have believed.
    assert "cumulative_since_sqlserver_start" not in sql
    assert "note=interval_sample_not_cumulative" in sql


@BOTH
def test_a_counter_reset_between_the_reads_is_dropped_rather_than_reported_negative(path):
    """A failover or DBCC SQLPERF between the samples is not negative work."""
    assert "HAVING SUM(r.wait_time_ms) - MIN(f.wait_time_ms) > 0" in _sql(path)


@BOTH
def test_the_idle_waits_that_topped_the_chart_are_excluded(path):
    """Each of these was measured as the top wait on some instance before it was added.

    `SOS_WORK_DISPATCHER` is the one from the audit; the other two were found on 2026-08-25 by
    running the collector against a 2012 instance, and both were already in the modern list and
    missing from the legacy one. The two lists are the same list now.
    """
    sql = _sql(path)

    for idle_wait in ("SOS_WORK_DISPATCHER",
                      "SQLTRACE_INCREMENTAL_FLUSH_SLEEP",
                      "HADR_FILESTREAM_IOMGR_IOCOMPLETION",
                      "QDS_PERSIST_TASK_MAIN_LOOP_SLEEP"):
        assert f"('{idle_wait}')" in sql


@BOTH
def test_each_sample_is_materialised_before_it_is_aggregated(path):
    """Aggregating straight out of the DMV returns split groups on some builds.

    Measured on a major-version-10 instance: 490 rows, 490 distinct wait types under either
    collation, and the grouped insert still raised "Cannot insert duplicate key in object
    '@first'". Copying the rows into a table variable first gives the aggregate a real input.
    """
    sql = _sql(path)

    assert "@raw_before" in sql and "@raw_after" in sql
    assert "FROM @raw_before AS r" in sql
    assert "FROM @raw_after AS r" in sql


@BOTH
def test_an_instance_that_is_doing_nothing_cannot_raise_a_warning(path):
    """Both gates, because the second one is what stops the metric crying wolf.

    4.79 s of total wait over a 5 s sample on a 60-core instance is idle, and its 49% signal-wait
    ratio would otherwise have raised WARNING every 150 seconds forever.
    """
    sql = _sql(path)

    assert "WHEN d.wait_ms / 1000.0 < @idle_floor_seconds THEN 'OK'" in sql
    assert "WHEN @avg_waiting_tasks < @busy_floor_tasks THEN 'OK'" in sql
    # The busy floor is relative to the instance: the same wait count means different things on a
    # 60-core box and a 2-core VM.
    assert "SELECT cpu_count FROM sys.dm_os_sys_info" in sql


@BOTH
def test_worker_starvation_is_graded_before_either_gate(path):
    """THREADPOOL on an idle-looking instance is still THREADPOOL: the next symptom is refused logins."""
    sql = _sql(path)

    threadpool = sql.index("WHEN d.wait_type = 'THREADPOOL' THEN 'CRITICAL'")
    assert threadpool < sql.index("@idle_floor_seconds THEN 'OK'")
    assert threadpool < sql.index("@avg_waiting_tasks < @busy_floor_tasks")


@BOTH
def test_lock_waits_do_not_raise_a_second_alarm(path):
    """LOCK_BLOCKING_SESSIONS already alerts on blocking, with the chain and the SQL text.

    This metric carries lock waits as evidence — they can be the reported top wait — but they are
    not in the list that turns a top wait into a WARNING.
    """
    sql = _sql(path)
    pressure = sql[sql.index("DECLARE @pressure TABLE"):sql.index("DECLARE @raw_before")]

    assert "LCK_M_" not in pressure


def test_the_sample_fits_inside_the_metric_timeout():
    """A five-second sample is only safe while the metric is allowed sixty."""
    from conftest import shipped_config
    import json

    doc = json.loads(shipped_config("metric_definitions.json").read_bytes().decode("utf-8-sig"))
    metric = next(m for m in doc["metrics"] if m["metric_code"] == "PERFORMANCE_WAIT_STATS")

    assert metric["time_window"]["timeout"] >= 30
    # And the hold on the session has to stay a small fraction of the collection interval.
    assert metric["time_window"]["repeat_interval"] >= 60
