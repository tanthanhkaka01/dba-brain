"""Query Store: how deep the scan reads and how recent a finding must be are two windows.

A query that ran once at 14:28 was reported as CRITICAL at 14:32, 14:47, 15:02, 15:17 ... and on
until 20:28, because the metric reported everything its 6-hour scan could see and it runs every 15
minutes. Twenty-four identical alerts for one finished statement, nothing about it changing in
between; the two lines filled the critical stream for a whole afternoon.

The scan still reads 6 hours - it has to, because the cheapest plan a query used recently is the
baseline that makes "this plan regressed" mean anything, and a 30-minute scan re-elects the bad
plan as its own best (ratio 1.00, regression gone). What narrowed is which findings are *reported*:
only a plan whose newest execution falls inside the alert window is news.

These tests pin the two windows apart, and pin the alert window to a length that cannot skip a
finding between two collections.
"""

import json
import re
from pathlib import Path

import pytest
from db_ops.lib.paths import resolve_tool_path
from conftest import shipped_config

DEFINITIONS = shipped_config("metric_definitions.json")
ISSUES_SQL = resolve_tool_path("assets/metrics/sqlserver/023_sqlserver_query_store_query_issues.sql")


@pytest.fixture(scope="module")
def sql():
    return ISSUES_SQL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def metrics():
    doc = json.loads(DEFINITIONS.read_bytes().decode("utf-8-sig"))
    return {m["metric_code"]: m for m in doc["metrics"]}


def _alert_window_minutes(sql: str) -> int:
    match = re.search(r"@p_AlertFromLocal\s+datetime\s*=\s*DATEADD\(MINUTE,\s*-(\d+),", sql)
    assert match, "the alert window must stay a MINUTE offset that this test can read"
    return int(match.group(1))


def test_the_scan_still_reads_six_hours(sql):
    """The baseline's window, not the alert's. Shorten this and every regression stops being one:
    with only the current plan in view, best_logical_reads is the bad plan's own number."""
    assert "@p_FromLocal      datetime = DATEADD(HOUR, -6, GETDATE())" in sql


def test_a_finding_is_reported_only_if_it_ran_inside_the_alert_window(sql):
    """The filter belongs to the row selection, not to the scan: `detail` and its baseline are
    computed over the full six hours and only then narrowed to what is new."""
    assert "@p_AlertFromLocal datetime = DATEADD(MINUTE, -30, GETDATE())" in sql
    assert "AND d.last_execution_time_local >= @p_AlertFromLocal" in sql


def test_the_alert_window_covers_the_gap_between_two_collections(sql, metrics):
    """A window shorter than the cadence has a blind spot: a query that finishes just after one
    run and more than `alert_window` before the next is never inside anyone's window, and the
    metric goes quiet about a real regression. 30 minutes against a 900s cadence leaves none."""
    cadence_seconds = metrics["QUERY_STORE_QUERY_ISSUES"]["time_window"]["repeat_interval"]

    assert _alert_window_minutes(sql) * 60 >= cadence_seconds


def test_the_message_names_both_windows(sql):
    """Read alone, `checked_window=last_6_hours` next to a 2-hour-old `last_execution_time` looks
    like the alert is late. Both windows in the message is what makes the row self-explaining."""
    assert "alert_window=last_30_minutes" in sql
    assert "alert_from=" in sql
    assert "checked_window=last_6_hours" in sql


def test_a_finding_is_judged_on_what_the_plan_did_recently(sql):
    """The alert filter alone does not stop a repeat. A plan with one heavy execution at 09:00 that
    keeps running normally has a newest execution inside every alert window until 15:00, so judged
    on its six-hour maximum it was re-reported all morning. The maxima a finding is judged on come
    from the rows touched inside the alert window; the six hours stay the baseline only."""
    assert "recent_agg AS" in sql
    recent = sql.split("recent_agg AS", 1)[1].split("query_best AS", 1)[0]
    assert "WHERE last_execution_time_local >= @p_AlertFromLocal" in recent
    assert "FROM recent_agg p" in sql
    assert "JOIN plan_agg h" in sql


def test_a_small_query_on_a_worse_plan_run_often_is_a_finding(sql):
    """2026-10-02, a production engine: four statements went from ~25 ms to ~1,300 ms after a recompile
    and ran ~1,300 times each in half an hour - every engine run took ten times longer and this
    metric said nothing, because each of its thresholds is about ONE execution being huge. The
    finding needs all three legs; drop any one and it is either blind again or fires on noise."""
    assert "'QUERY_PLAN_REGRESSED_FREQUENT'" in sql
    assert "'query_store_plan_regressed_frequent'" in sql
    leg = sql.split("THEN 'QUERY_PLAN_REGRESSED_FREQUENT'", 1)[0].rsplit("WHEN", 1)[1]
    assert "p.recent_executions >= @p_FreqMinExecutions" in leg
    assert "p.recent_total_cpu_sec >= @p_FreqWarnTotalCpuSec" in leg
    assert "NULLIF(b.best_avg_cpu_sec, 0) >= @p_FreqWarnCpuRatio" in leg


def test_the_frequency_baseline_is_another_plan_over_a_longer_window(sql):
    """Two ways the baseline goes blind. Compared with its own history a plan has ratio ~1; and
    inside the six-hour scan a flip that survived the night has no good plan left to compare with.
    So the baseline is the cheapest OTHER plan, read over seven days, for candidate queries only."""
    assert "@p_BaselineFromLocal datetime = DATEADD(DAY, -7, GETDATE())" in sql
    assert "AND bb.plan_id <> p.plan_id" in sql
    assert "FROM #qs_cand c WHERE c.database_name = @db_name" in sql
    assert "cpu_baseline_window=last_7_days" in sql


def test_the_windows_follow_the_servers_own_clock(sql):
    """Query Store stores UTC. A named time zone in the SQL is right on one estate and silently
    shifts every window on a server that lives anywhere else."""
    assert "AT TIME ZONE" not in sql
    assert "DATEDIFF(MINUTE, GETUTCDATE(), GETDATE())" in sql


def test_the_scan_does_not_read_query_text(sql):
    """It was copied into #qs_raw once per runtime-stats row and never reached the message."""
    assert "query_store_query_text" not in sql
    assert "query_sql_text" not in sql


def test_averages_are_weighted_by_executions(sql):
    """A plain AVG over interval rows lets an hour with one execution outvote one with ten thousand."""
    assert "AVG(avg_cpu_sec)" not in sql
    assert "SUM(avg_cpu_sec * ISNULL(count_executions, 0)) / NULLIF(SUM(ISNULL(count_executions, 0)), 0) AS avg_cpu_sec" in sql
