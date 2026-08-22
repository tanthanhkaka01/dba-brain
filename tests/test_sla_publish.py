from db_ops.sla.models import SlaPolicyResult, SlaValidationSummary
from db_ops.sla.publish import (
    chat_id_for_level,
    publish_html,
    publish_index,
    render_html,
    render_index_html,
    summary_notify_level,
)


def _result(policy_id: str, status: str, actual: float, objective: float, budget_left: float, target_id: str = "srv/sqlserver/db") -> SlaPolicyResult:
    return SlaPolicyResult(
        policy_id=policy_id,
        name=policy_id,
        target_id=target_id,
        status=status,
        objective_percent=objective,
        actual_percent=actual,
        error_budget_percent=round(100.0 - objective, 4),
        budget_consumed_percent=round(100.0 - budget_left, 2),
        budget_remaining_percent=budget_left,
        total_count=10,
        good_count=9,
        bad_count=1,
        no_data=status == "NO_DATA",
        window_hours=24,
        window_start="2026-05-27T04:00:00Z",
        window_end="2026-05-28T04:00:00Z",
        scope="db_types:sqlserver",
        category="availability",
    )


def _summary(results, *, status, passed, at_risk, failed, no_data) -> SlaValidationSummary:
    return SlaValidationSummary(
        status=status,
        policy_count=len(results),
        result_count=len(results),
        passed_count=passed,
        at_risk_count=at_risk,
        failed_count=failed,
        no_data_count=no_data,
        window_end="2026-05-28T04:00:00Z",
        results=tuple(results),
    )


def test_notify_level_routes_by_severity_without_an_sla_group():
    # No "sla" level configured: the original severity routing stands.
    groups = {"logging": "-1", "warning": "-2", "critical": "-3"}
    failed = _summary([_result("A", "FAILED", 50.0, 99.0, 0.0)], status="FAILED", passed=0, at_risk=0, failed=1, no_data=0)
    at_risk = _summary([_result("A", "AT_RISK", 96.0, 95.0, 20.0)], status="PASSED", passed=0, at_risk=1, failed=0, no_data=0)
    clean = _summary([_result("A", "PASSED", 100.0, 99.0, 100.0)], status="PASSED", passed=1, at_risk=0, failed=0, no_data=0)
    assert summary_notify_level(failed, groups) == "critical"
    assert summary_notify_level(at_risk, groups) == "warning"
    assert summary_notify_level(clean, groups) == "logging"


def test_an_sla_group_takes_every_run_regardless_of_severity():
    """A per-domain group only works if the whole story lands in it — a FAILED run in
    Criticals and a PASSED run in Logging is the split the group exists to end."""
    groups = {"logging": "-1", "warning": "-2", "critical": "-3", "sla": "-9"}
    failed = _summary([_result("A", "FAILED", 50.0, 99.0, 0.0)], status="FAILED", passed=0, at_risk=0, failed=1, no_data=0)
    clean = _summary([_result("A", "PASSED", 100.0, 99.0, 100.0)], status="PASSED", passed=1, at_risk=0, failed=0, no_data=0)
    assert summary_notify_level(failed, groups) == "sla"
    assert summary_notify_level(clean, groups) == "sla"
    # A level configured but left blank is not a route.
    assert summary_notify_level(failed, {**groups, "sla": ""}) == "critical"


def test_chat_id_for_level_falls_back_error_group_for_critical():
    groups = {"logging": "-1", "warning": "-2", "error": "-3"}
    assert chat_id_for_level(groups, "critical") == "-3"
    assert chat_id_for_level(groups, "warning") == "-2"


def test_render_and_publish_html(tmp_path):
    results = [_result("SS_AVAIL", "PASSED", 100.0, 99.9, 100.0)]
    summary = _summary(results, status="PASSED", passed=1, at_risk=0, failed=0, no_data=0)
    page = render_html(summary, recent_runs=[{"sla_run_id": 1, "started_at": "2026-05-28T04:00:00Z",
                                              "finished_at": "2026-05-28T04:00:01Z", "status": "PASSED",
                                              "passed_count": 1, "at_risk_count": 0, "failed_count": 0, "no_data_count": 0}])
    assert "SLA / SLO compliance" in page
    assert "SS_AVAIL" in page
    path = publish_html(summary, recent_runs=[], out_dir=tmp_path / "reports")
    assert path.name == "sla.html"
    assert path.exists()
    assert "SS_AVAIL" in path.read_text(encoding="utf-8")
    # A dated archive copy is written alongside the stable page — one per DAY, overwritten.
    # Stamping every hourly run instead left 696 files and 422 MB in the serving directory in
    # four weeks, growing without bound, so the name carries the day only.
    archives = list((tmp_path / "reports").glob("[0-9]" * 8 + "_sla.html"))
    assert len(archives) == 1
    assert not list((tmp_path / "reports").glob("*_??????_sla*.html")), "no per-run stamp"
    # publish_html also refreshes the index hub linking to sla.html
    index = (tmp_path / "reports" / "index.html")
    assert index.exists()
    assert 'href="sla.html"' in index.read_text(encoding="utf-8")


def test_index_marks_inventory_disabled_when_absent(tmp_path):
    results = [_result("SS_AVAIL", "FAILED", 50.0, 99.0, 0.0)]
    summary = _summary(results, status="FAILED", passed=0, at_risk=0, failed=1, no_data=0)
    reports = tmp_path / "reports"
    reports.mkdir()
    # no inventory report present -> inventory card is disabled (not a link)
    html_no_inv = render_index_html(summary, directory=reports)
    assert 'href="sla.html"' in html_no_inv
    assert 'href="database-inventory.html"' not in html_no_inv
    # once an inventory report exists, the card becomes a link
    (reports / "database-inventory.html").write_text("x", encoding="utf-8")
    html_with_inv = render_index_html(summary, directory=reports)
    assert 'href="database-inventory.html"' in html_with_inv
    path = publish_index(summary, out_dir=reports)
    assert path.name == "index.html"
