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
