"""The page must separate four questions that were all being shown as one red number.

"37 failed" answered none of: is anything broken *now*, did something breach its objective at some
point this week, how much maintenance debt is queued, and how much of this we simply could not
measure. An operator reading that number cannot tell which of the four they are looking at, and
three of them are not incidents.

The case that forced this: on `ACME-192-0-2-250`, `OS_REBOOT_PENDING` was warning from 07-29 to
08-02 and OK on 08-03 and 08-04. `ALL_OS_PATCH_STATE_7D` computed 2/7 = 28.57% and was arithmetically
right, while the host was not pending a reboot. The seven-day breach was presented as a live
incident because the page never showed the present tense beside the window.
"""

from __future__ import annotations

from db_ops.sla.models import SlaPolicyResult, SlaValidationSummary
from db_ops.sla.publish import publish_html, render_html


def _result(policy_id="P", target_id="srv/sqlserver/svc", status="FAILED", *,
            current_status="OK", policy_model="time_slo", affected_objects=0,
            data_quality_status="OK") -> SlaPolicyResult:
    return SlaPolicyResult(
        policy_id=policy_id, name=policy_id, target_id=target_id, scope="instance",
        category="availability", status=status, objective_percent=99.0, actual_percent=28.57,
        error_budget_percent=1.0, budget_consumed_percent=100.0, budget_remaining_percent=0.0,
        total_count=7, good_count=2, bad_count=5, no_data=False,
        window_hours=168, window_start="2026-07-29T00:00:00Z", window_end="2026-08-05T00:00:00Z",
        current_status=current_status, policy_model=policy_model,
        affected_objects=affected_objects, data_quality_status=data_quality_status,
    )


def _summary(results) -> SlaValidationSummary:
    return SlaValidationSummary(
        status="FAILED", policy_count=len(results), result_count=len(results),
        passed_count=sum(1 for r in results if r.status == "PASSED"), at_risk_count=0,
        failed_count=sum(1 for r in results if r.status == "FAILED"), no_data_count=0,
        window_end="2026-08-05T00:00:00Z", results=tuple(results),
    )


def test_a_window_breach_that_has_already_recovered_is_marked_ok_now():
    """The reboot-pending case. 28.57% over seven days, healthy in the newest collection."""
    page = render_html(_summary([_result(current_status="OK")]), recent_runs=[])
    assert "OK now" in page
    assert "28.57" in page, "the window figure is still shown; it is not wrong, only incomplete"


def test_something_actually_broken_right_now_is_marked_as_such():
    page = render_html(_summary([_result(current_status="BAD")]), recent_runs=[])
    assert "bad now" in page


def test_the_headline_counts_the_four_questions_separately():
    results = [
        _result("LIVE", current_status="BAD"),                                   # broken now
        _result("HISTORIC", current_status="OK"),                                 # window only
        _result("DEBT", policy_model="finding_inventory", affected_objects=1485),  # debt
        _result("BLIND", data_quality_status="COLLECTION_FAILED"),                # unmeasurable
    ]
    page = render_html(_summary(results), recent_runs=[])

    assert ">1<" in page and "Bad right now" in page
    assert "1485" in page and "Objects in backlog" in page
    assert "Cannot measure" in page
    assert "Window breach" in page


def test_operational_debt_is_reported_as_objects_not_as_a_percentage():
    """4.09% was a percentage of snapshots. 1,485 is the number of things to fix."""
    page = render_html(
        _summary([_result(policy_model="finding_inventory", affected_objects=1485)]), recent_runs=[])
    assert "1485 objects affected" in page


def test_the_page_states_how_this_run_differs_from_the_last():
    previous = {"P @ srv/sqlserver/svc": "PASSED", "GONE @ srv/sqlserver/svc": "FAILED"}
    page = render_html(_summary([_result()]), recent_runs=[], previous_state=previous)

    assert "1 newly failing" in page
    assert "no longer evaluated" in page, "a retired policy is not a recovery"


def test_without_a_previous_run_the_page_says_so_rather_than_implying_no_change():
    page = render_html(_summary([_result()]), recent_runs=[], previous_state=None)
    assert "No previous run stored" in page


def test_the_history_table_states_its_range():
    """It shows the newest 15 runs and said so nowhere, so on an hourly schedule a reader saw
    about 15 hours while reasonably assuming it was everything."""
    runs = [{"sla_run_id": index, "started_at": f"2026-08-05T{index:02d}:00:00Z",
             "finished_at": f"2026-08-05T{index:02d}:00:05Z", "status": "FAILED",
             "passed_count": 1, "at_risk_count": 0, "failed_count": 1, "no_data_count": 0}
            for index in (5, 4, 3)]

    page = render_html(_summary([_result()]), recent_runs=runs, history_limit=15)

    assert "Showing the newest 3 runs" in page
    assert "(capped)" not in page, "3 of a 15 limit is not capped; saying so would be misleading"
    assert "2026-08-05T03:00:05Z to 2026-08-05T05:00:05Z" in page
    assert "The store keeps them all" in page


def test_the_archive_keeps_one_file_per_day_not_one_per_run(tmp_path):
    """696 hourly files and 422 MB accumulated in the serving directory in four weeks."""
    summary = _summary([_result()])
    for _ in range(3):
        publish_html(summary, recent_runs=[], out_dir=tmp_path)

    assert len(list(tmp_path.glob("[0-9]" * 8 + "_sla.html"))) == 1
    assert (tmp_path / "sla.html").exists(), "the stable name every link points at must survive"
