"""The server page lists every Query Store finding of the last day, one row per query plan.

On 2026-10-02 four statements in a production database ran on a worse plan from about 03:00 to
13:28, and metric 23 - fixed that day - reported them. Simulated through the page builders, the
server page showed almost none of it: the metric's ``metric_item`` is the *kind* of finding, so
three regressed queries in one run were one series carrying one query's message; the first run
of a new kind had fewer than ``MIN_POINTS`` samples and was not drawn at all while the fleet page
already carried a WARNING card for it; and once the plan flip cleared, nothing on the page said it
had happened. The operator asked for the findings to be kept for a day so that track is not lost.
"""

from __future__ import annotations

from db_ops.reports.server_report import QUERY_STORE_FINDINGS_HOURS, build_query_store_findings

CODE = "QUERY_STORE_QUERY_ISSUES"


def _finding(collected_at, query_id, plan_id, *, status="WARNING", cpu=400.0, ratio=1400.0,
             kind="QUERY_PLAN_REGRESSED_FREQUENT", query_bad=""):
    return {
        "metric_code": CODE, "metric_item": "query_store_plan_regressed_frequent",
        "metric_value": str(ratio), "metric_unit": "cpu_ratio", "status": status,
        "collected_at": collected_at,
        "message": (f"db_name=SALESDB, query_id={query_id}, plan_id={plan_id}, old_plan_id=9, "
                    f"issue_type={kind}, max_duration_sec=2.5, max_logical_reads=16000, "
                    f"recent_executions=600, recent_total_cpu_sec={cpu}, recent_avg_cpu_sec=1.3, "
                    f"best_avg_cpu_sec=0.001, best_cpu_plan_id=7, cpu_ratio={ratio}, "
                    f"last_execution_time=2026-10-02 09:00:00{query_bad}"),
    }


def _nothing(collected_at):
    return {"metric_code": CODE, "metric_item": None, "metric_value": None, "metric_unit": None,
            "status": "OK", "collected_at": collected_at, "message": "SQL returned no rows."}


def test_a_finding_seen_once_is_listed_at_once():
    """The chart pipeline needs three samples; a table needs one."""
    section = build_query_store_findings([_finding("2026-10-02T00:05:00Z", 305494, 171479)])
    assert [f["queryId"] for f in section["findings"]] == ["305494"]
    assert section["findings"][0]["now"] is True


def test_three_queries_reported_under_one_kind_are_three_rows():
    stamp = "2026-10-02T02:05:00Z"
    section = build_query_store_findings([_finding(stamp, 305294, 1), _finding(stamp, 305486, 2),
                                          _finding(stamp, 305494, 3)])
    assert sorted(f["queryId"] for f in section["findings"]) == ["305294", "305486", "305494"]
    assert section["summary"]["now"] == 3


def test_a_regression_that_cleared_stays_listed_as_cleared_with_its_history():
    rows = [
        _finding("2026-10-02T00:05:00Z", 305494, 171479, cpu=325.0, ratio=4274.0),
        _finding("2026-10-02T03:05:00Z", 305494, 171479, status="CRITICAL", cpu=3822.0, ratio=4239.0),
        _nothing("2026-10-02T07:05:00Z"),
    ]
    section = build_query_store_findings(rows)
    [finding] = section["findings"]
    assert finding["now"] is False
    assert finding["worst"] == "CRITICAL"
    assert finding["reports"] == 2
    assert (finding["firstSeen"], finding["lastSeen"]) == ("2026-10-02T00:05:00Z", "2026-10-02T03:05:00Z")
    assert finding["peakRecentCpuSec"] == 3822.0 and finding["peakCpuRatio"] == 4274.0
    assert section["summary"]["cleared"] == 1 and section["summary"]["now"] == 0


def test_what_is_happening_now_is_listed_before_what_cleared():
    rows = [_finding("2026-10-02T00:05:00Z", 1, 1, status="CRITICAL"),
            _finding("2026-10-02T05:05:00Z", 2, 2, status="WARNING")]
    section = build_query_store_findings(rows)
    assert [(f["queryId"], f["now"]) for f in section["findings"]] == [("2", True), ("1", False)]


def test_a_run_that_found_nothing_is_a_collection_and_not_a_missing_section():
    section = build_query_store_findings([_nothing("2026-10-02T07:05:00Z")])
    assert section["summary"]["collected"] is True
    assert section["findings"] == []


def test_a_server_without_the_metric_has_no_section():
    assert build_query_store_findings([])["summary"]["collected"] is False


def test_the_window_is_a_day():
    assert QUERY_STORE_FINDINGS_HOURS == 24


def test_a_finding_judged_on_its_querys_bad_plans_carries_their_sum():
    """Since 2026-10-03 the frequency finding sums a query's bad plans - replayed at 13:05 that
    morning, query 374468 burned 307 s on two (210 + 97) and was missed plan by plan. The row shows
    the query's total beside the plan's own, and keeps its peak over the day like the other numbers."""
    stamp = "2026-10-02T05:05:00Z"
    section = build_query_store_findings([
        _finding("2026-10-02T04:05:00Z", 374468, 382516, cpu=150.0,
                 query_bad=", query_bad_plan_count=2, query_bad_cpu_sec=301.5"),
        _finding(stamp, 374468, 382516, cpu=210.42, query_bad=", query_bad_plan_count=2, query_bad_cpu_sec=306.97"),
    ])

    (row,) = section["findings"]
    assert row["queryBadPlans"] == 2
    assert row["queryBadCpuSec"] == 306.97
    assert row["peakQueryBadCpuSec"] == 306.97
    assert row["peakRecentCpuSec"] == 210.42


def test_a_message_from_before_the_sum_still_reads():
    """Rows the metric wrote before 2026-10-03 carry no query total; the page shows the plan's own."""
    (row,) = build_query_store_findings([_finding("2026-10-02T00:05:00Z", 305494, 171479)])["findings"]

    assert row["queryBadPlans"] is None and row["queryBadCpuSec"] is None
