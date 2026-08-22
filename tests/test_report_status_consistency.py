"""The fleet page and the per-server detail page must agree about a server.

They disagreed because they rested on different facts. ``server-metrics.html`` reports the status
**the collector computed** (each metric's SQL judged its own thresholds). ``database-inventory.html``
re-derived severity from a hand-picked set of signals - backups, disks, services, PLE - and did not
even load metrics like ``LOG_RECENT_CRITICAL``. So 192.0.2.250, with 1533 stack dumps in 24h and
several sessions pinning locks, read CRITICAL on its own page and healthy on the fleet page.
"""
from __future__ import annotations
import datetime as _dt

# Timestamps are anchored to *now* rather than written as literals. These tests ask the store for a
# 7-day window, so a fixed July date passes until the day the window moves past it and then fails
# every run afterwards with nothing having changed - which is exactly what happened on 2026-08-06
# and 2026-08-07. What the tests are about is the ordering of the rows inside the window, not the
# calendar, so the calendar is computed.
_ANCHOR = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=2)


def _ts(hours: float = 0) -> str:
    return (_ANCHOR + _dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")



import pytest

from db_ops.reports.inventory_health import build_server_overlay
from db_ops.reports.inventory_report import _merge_findings, _status


def _model(**over):
    model = {
        "backup": {"note": "", "cov": ""}, "disks": [], "os_health": {},
        "total": 3, "online": 3, "ple": None, "sessions": 5, "severity": {}, "osOnly": False,
    }
    model.update(over)
    return model


def test_a_collector_critical_makes_the_fleet_page_critical():
    """The exact case reported: metrics the fleet rule never looked at."""
    severity = {"worst": "CRITICAL", "critical_rows": 29,
                "critical_codes": ["LOCK_TRANSACTION_HOLDERS", "LOG_RECENT_CRITICAL"],
                "warning_codes": []}
    assert _status(_model(severity=severity)) == "crit"


def test_a_collector_warning_makes_the_fleet_page_warn():
    severity = {"worst": "WARNING", "warning_rows": 4,
                "critical_codes": [], "warning_codes": ["QUERY_LONG_RUNNING"]}
    assert _status(_model(severity=severity)) == "warn"


def test_a_healthy_collector_verdict_does_not_force_a_status():
    """OK from the collectors must not override the report's own disk/backup rules."""
    severity = {"worst": "OK", "critical_codes": [], "warning_codes": []}
    assert _status(_model(severity=severity)) == "ok"
    # A critical disk still wins even when no metric row is CRITICAL.
    assert _status(_model(severity=severity, disks=[{"st": "CRITICAL", "free": 2}])) == "crit"


def test_critical_outranks_warning():
    severity = {"worst": "CRITICAL", "critical_rows": 1, "critical_codes": ["BACKUP_AGE"],
                "warning_codes": ["QUERY_LONG_RUNNING"]}
    assert _status(_model(severity=severity)) == "crit"


def test_a_missing_severity_block_keeps_the_old_behaviour():
    """Servers merged from an older overlay have no severity; they must not break or change."""
    assert _status(_model(severity={})) == "ok"
    assert _status(_model(severity=None)) == "ok"


# --------------------------------------------------------------------------- #
# The page must say *why*
# --------------------------------------------------------------------------- #
def test_findings_name_the_metric_codes_responsible():
    """A status badge with no stated reason is what made the two pages look arbitrary."""
    server = {"metric_severity": {"worst": "CRITICAL", "critical_rows": 29,
                                  "critical_codes": ["LOCK_TRANSACTION_HOLDERS", "LOG_RECENT_CRITICAL"],
                                  "warning_codes": []}}
    findings = _merge_findings(server)
    assert findings
    assert "29 critical metric result(s)" in findings[0]
    assert "LOG_RECENT_CRITICAL" in findings[0]


def test_warning_findings_are_reported_when_there_is_no_critical():
    server = {"metric_severity": {"worst": "WARNING", "warning_rows": 7,
                                  "critical_codes": [], "warning_codes": ["QUERY_LONG_RUNNING"]}}
    assert "7 warning metric result(s)" in _merge_findings(server)[0]


def test_static_findings_and_config_warnings_survive():
    server = {
        "findings": ["documented finding"],
        "config_warnings": ["compatibility level 80 is below the engine native 100 (1 database: X)"],
        "metric_severity": {"worst": "CRITICAL", "critical_rows": 1,
                            "critical_codes": ["BACKUP_AGE"], "warning_codes": []},
    }
    findings = _merge_findings(server)
    assert "documented finding" in findings
    assert any("compatibility level 80" in f for f in findings)
    assert findings[0].startswith("1 critical metric result(s)")


def test_findings_are_not_duplicated():
    server = {"findings": ["same"], "config_warnings": ["same"], "metric_severity": {}}
    assert _merge_findings(server).count("same") == 1


# --------------------------------------------------------------------------- #
# The overlay has to carry it, or the merge drops it
# --------------------------------------------------------------------------- #
def test_overlay_carries_the_severity_block():
    overlay = build_server_overlay("SRV", "10.0.0.1", {}, {"worst": "CRITICAL"})
    assert overlay["metric_severity"] == {"worst": "CRITICAL"}


def test_overlay_without_severity_is_still_valid():
    assert build_server_overlay("SRV", "10.0.0.1", {})["metric_severity"] == {}


@pytest.mark.parametrize("module_name", ["db_ops.reports.inventory_summary", "db_ops.control.inventory"])
def test_both_merge_allowlists_carry_the_new_blocks(module_name):
    """HEALTH_BLOCKS gates which overlay blocks survive the merge, and there are two copies of it.
    A block missing from one is silently dropped before the report renders.

    One of the two lives in `control`, which the public distribution does not ship — so there the
    second copy does not exist and there is nothing to disagree with. Skipped rather than dropped,
    because in *this* repository both copies are real and drifting apart is the failure.
    """
    import importlib

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        pytest.skip(f"{module_name} is not in this distribution, so there is no second copy")
    blocks = module.HEALTH_BLOCKS
    assert "metric_severity" in blocks
    assert "instance_health" in blocks


# --------------------------------------------------------------------------- #
# Severity describes the state now, not everything that ever happened
# --------------------------------------------------------------------------- #
def test_severity_uses_only_the_latest_row_per_metric_item(tmp_path):
    """A cleared error must stop counting.

    Counting every row in the window kept a server red for the whole window over a transient
    failure that had already recovered, and inflated warning counts by re-counting the same
    condition once per collection cycle.
    """
    from db_ops.metrics.models import MetricResult
    from db_ops.metrics.storage import MetricStore

    store = MetricStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    run = store.start_run(started_at=_ts(0))

    def result(status, collected_at):
        return MetricResult(
            target_id="T", server_id="SRV", ip="10.0.0.1", db_type="sqlserver", db_name="db",
            metric_code="BACKUP_AGE", metric_item="db", metric_value="1", metric_unit="hours",
            status=status, message="", collected_at=collected_at, importance=3,
        )

    # Failed earlier in the window, recovered on the most recent collection.
    store.insert_results(run_id=run, results=[result("CRITICAL", _ts(1))])
    store.insert_results(run_id=run, results=[result("OK", _ts(9))])

    severity = store.fetch_severity_by_server(days=7)
    assert severity["SRV"]["worst"] == "OK"
    assert severity["SRV"]["critical_codes"] == []


def test_a_still_failing_metric_is_still_critical(tmp_path):
    from db_ops.metrics.models import MetricResult
    from db_ops.metrics.storage import MetricStore

    store = MetricStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    run = store.start_run(started_at=_ts(0))
    for stamp in (_ts(1), _ts(9)):
        store.insert_results(run_id=run, results=[MetricResult(
            target_id="T", server_id="SRV", ip="10.0.0.1", db_type="sqlserver", db_name="db",
            metric_code="JOB_FAILED", metric_item="sql_agent", metric_value="3", metric_unit="",
            status="CRITICAL", message="", collected_at=stamp, importance=3)])

    severity = store.fetch_severity_by_server(days=7)
    assert severity["SRV"]["worst"] == "CRITICAL"
    assert severity["SRV"]["critical_codes"] == ["JOB_FAILED"]
    # Counted once - it is one condition, not one per collection cycle.
    assert severity["SRV"]["critical_rows"] == 1
