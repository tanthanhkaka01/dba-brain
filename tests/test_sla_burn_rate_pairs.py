"""Multi-window multi-burn-rate alerts for SLA compliance (Google SRE Workbook, ch. 5).

Burn rate measures the rate at which a service is consuming its error budget:
    burn_rate = error_rate / error_budget
where error_budget = 1.0 - (objective_percent / 100.0).

Alerting on single windows is prone to false positives (brief spikes) or delayed detection
(slow leaks). Google SRE Workbook chapter 5 recommends multi-window multi-burn-rate pairs:
an alert fires only when both a long window and a short window simultaneously burn at or above
the designated factor.

Key behaviors tested:
1. Fast burn (e.g. 14.4x over 1h and 5m) fires a "page" alert and escalates PASSED/AT_RISK to BURNING.
2. Historical bad minutes outside the short/long burn windows fire no alert.
3. Slow burn (e.g. 1x over 3d and 6h) fires a "ticket" alert and leaves status PASSED.
4. An objective that is already missed remains FAILED even if a page burn fires.
5. SlaStore.save_summary preserves burn_rates and burn_alert in details_json.
6. The policy parser refuses burn_rate_alerts on policies that are not time_slo with minutes aggregation.
7. State transition PASSED -> BURNING is recognized as an escalation in diff_states with SLA_SEVERITY_ORDER.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbabrain.lib.state_transition import diff_states
from dbabrain.metrics.models import MetricResult
from dbabrain.metrics.storage import MetricStore
from dbabrain.sla.cli import SLA_SEVERITY_ORDER
from dbabrain.sla.compliance import validate_sla_policies
from dbabrain.sla.models import SlaPolicy
from dbabrain.sla.policies import parse_sla_policy
from dbabrain.sla.storage import SlaStore


def _make_policy(**overrides) -> SlaPolicy:
    kwargs = {
        "policy_id": "INSTANCE_AVAILABILITY_TEST",
        "name": "Instance availability test",
        "target_ids": ("server/sqlserver/master",),
        "metric_codes": ("INSTANCE_STATUS",),
        "aggregation": "minutes",
        "policy_model": "time_slo",
        "objective_percent": 99.0,  # 1% error budget
        "window_hours": 24,
        "good_statuses": ("OK", "LOGGING"),
        "at_risk_budget_percent": 25.0,
        "minimum_sample_count": 1,
        "burn_rate_alerts": (
            {
                "severity": "page",
                "factor": 14.4,
                "short_window_minutes": 5,
                "long_window_minutes": 60,
            },
        ),
    }
    kwargs.update(overrides)
    return SlaPolicy(**kwargs)


def _row(status: str, collected_at: str) -> MetricResult:
    return MetricResult(
        target_id="server/sqlserver/master",
        server_id="server",
        ip="192.0.2.1",
        db_type="sqlserver",
        db_name="master",
        metric_code="INSTANCE_STATUS",
        metric_item="master",
        metric_value="1",
        metric_unit="",
        status=status,
        importance=5,
        message=None,
        collected_at=collected_at,
    )


def test_fast_burn_triggers_burning_status_and_page_alert(tmp_path: Path) -> None:
    """A fast burn (error budget consumed rapidly across both short and long windows) triggers BURNING.

    With a 99.0% objective (1.0% error budget), 14.4x requires an error rate of 14.4%.
    If bad minutes occur in both the last 5m (e.g. 1 bad / 5 = 20%) and the last 60m (e.g. 10 bad / 60 = 16.7%),
    both windows burn at >= 14.4x, firing a page alert and setting status to BURNING.
    """
    sqlite_path = tmp_path / "burn_fast.sqlite"
    metric_store = MetricStore(sqlite_path)
    metric_store.initialize()

    # Window end: 2026-10-16T12:00:00Z
    # 5m window: 11:55:00 to 12:00:00
    # 1h window: 11:00:00 to 12:00:00
    results = []

    # 50 good minutes from 10:00 to 10:50
    for m in range(50):
        results.append(_row("OK", f"2026-10-16T10:{m:02d}:30Z"))

    # In 1h window (11:00 - 12:00):
    # 9 bad minutes between 11:00 and 11:09
    for m in range(9):
        results.append(_row("ERROR", f"2026-10-16T11:{m:02d}:30Z"))
    # 45 good minutes between 11:10 and 11:54
    for m in range(10, 55):
        results.append(_row("OK", f"2026-10-16T11:{m:02d}:30Z"))
    # In 5m window (11:55 - 12:00): 1 bad minute at 11:57, 4 good minutes
    results.append(_row("ERROR", "2026-10-16T11:57:30Z"))
    for m in (55, 56, 58, 59):
        results.append(_row("OK", f"2026-10-16T11:{m:02d}:30Z"))

    metric_store.insert_results(run_id=1, results=results)

    policy = _make_policy(objective_percent=90.0)  # Budget = 10%
    # For budget 10%, factor 14.4 would need 144% which is impossible.
    # Set factor to 1.5 for a clean test:
    # 5m window: 1 bad / 5 = 20% error rate -> burn rate = 0.20 / 0.10 = 2.0x >= 1.5x
    # 1h window: 10 bad / 60 = 16.7% error rate -> burn rate = 0.1667 / 0.10 = 1.67x >= 1.5x
    # Overall 24h: 10 bad / 109 observed = 90.8% >= 90.0% objective (compliant)
    policy = _make_policy(
        objective_percent=90.0,
        burn_rate_alerts=(
            {
                "severity": "page",
                "factor": 1.5,
                "short_window_minutes": 5,
                "long_window_minutes": 60,
            },
        ),
    )

    summary = validate_sla_policies(
        sqlite_path=sqlite_path,
        policies=[policy],
        window_end="2026-10-16T12:00:00Z",
    )

    assert len(summary.results) == 1
    res = summary.results[0]
    assert res.burn_alert == "page"
    assert res.status == "BURNING"
    assert "5m" in res.burn_rates
    assert "1h" in res.burn_rates
    assert res.burn_rates["5m"] >= 1.5
    assert res.burn_rates["1h"] >= 1.5
    # Overall summary status should not fail
    assert summary.burning_count == 1


def test_historical_bad_minutes_outside_burn_windows_fire_nothing(tmp_path: Path) -> None:
    """Bad minutes from earlier in the rolling window that are not burning right now fire no alert.

    If 10 bad minutes happened 3 hours ago, and the last 1h and 5m are completely clean,
    burn rate in the alert windows is 0.0x and no alert is triggered.
    """
    sqlite_path = tmp_path / "burn_old.sqlite"
    metric_store = MetricStore(sqlite_path)
    metric_store.initialize()

    # Window end: 12:00:00
    # Bad minutes occurred at 08:00
    results = [
        _row("ERROR", f"2026-10-16T08:{m:02d}:30Z")
        for m in range(5)
    ]
    # Clean minutes from 11:00 to 12:00
    for m in range(60):
        results.append(_row("OK", f"2026-10-16T11:{m:02d}:30Z"))

    metric_store.insert_results(run_id=1, results=results)

    policy = _make_policy(
        objective_percent=80.0,
        burn_rate_alerts=(
            {
                "severity": "page",
                "factor": 1.5,
                "short_window_minutes": 5,
                "long_window_minutes": 60,
            },
        ),
    )

    summary = validate_sla_policies(
        sqlite_path=sqlite_path,
        policies=[policy],
        window_end="2026-10-16T12:00:00Z",
    )

    res = summary.results[0]
    assert res.burn_alert == ""
    assert res.status == "PASSED"
    assert res.burn_rates.get("5m") == 0.0
    assert res.burn_rates.get("1h") == 0.0


def test_slow_burn_fires_ticket_alert_and_leaves_status_passed(tmp_path: Path) -> None:
    """A slow burn over multiple days fires a ticket alert but does NOT change status to BURNING.

    Ticket-level alerts notify for maintenance and scheduling; they do not page on-call operators,
    so the status remains PASSED (or AT_RISK).
    """
    sqlite_path = tmp_path / "burn_ticket.sqlite"
    metric_store = MetricStore(sqlite_path)
    metric_store.initialize()

    # The policy window must be longer than the pair's long window: over one and the same window,
    # a burn of 1x is exactly the objective missed. Seven days at 95 % (a 5 % budget): four good
    # days, then three days with one minute in twelve down - 8.3 %, a 1.67x burn over both 3 d and
    # 6 h - which the week as a whole still meets (3.6 % down, 71 % of the budget used).
    from datetime import datetime, timedelta, timezone

    end = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    results = []
    for minute in range(7 * 1440):
        at = end - timedelta(minutes=minute + 1)
        down = minute < 3 * 1440 and minute % 12 == 0
        results.append(_row("ERROR" if down else "OK", at.strftime("%Y-%m-%dT%H:%M:30Z")))

    metric_store.insert_results(run_id=1, results=results)

    policy = _make_policy(
        objective_percent=95.0,
        window_hours=168,
        burn_rate_alerts=(
            {
                "severity": "ticket",
                "factor": 1.0,
                "short_window_minutes": 360,
                "long_window_minutes": 4320,
            },
        ),
    )

    summary = validate_sla_policies(
        sqlite_path=sqlite_path,
        policies=[policy],
        window_end="2026-10-16T12:00:00Z",
    )

    res = summary.results[0]
    assert res.burn_alert == "ticket"
    assert res.status == "PASSED"  # Ticket never changes status to BURNING
    assert "6h" in res.burn_rates
    assert "3d" in res.burn_rates


def test_already_failing_objective_stays_failed_despite_page_burn(tmp_path: Path) -> None:
    """When a policy has already missed its objective, its status remains FAILED even if burning.

    A missed rolling objective takes precedence over active burn escalation.
    """
    sqlite_path = tmp_path / "burn_already_failed.sqlite"
    metric_store = MetricStore(sqlite_path)
    metric_store.initialize()

    # 5 bad minutes in the last 5m (100% bad)
    results = [_row("ERROR", f"2026-10-16T11:{55+m:02d}:30Z") for m in range(5)]
    metric_store.insert_results(run_id=1, results=results)

    policy = _make_policy(
        objective_percent=99.0,
        window_hours=1,
        burn_rate_alerts=(
            {
                "severity": "page",
                "factor": 1.0,
                "short_window_minutes": 5,
                "long_window_minutes": 60,
            },
        ),
    )

    summary = validate_sla_policies(
        sqlite_path=sqlite_path,
        policies=[policy],
        window_end="2026-10-16T12:00:00Z",
    )

    res = summary.results[0]
    assert res.burn_alert == "page"
    assert res.status == "FAILED"  # Objective missed (0% < 99.0%), stays FAILED


def test_details_json_carries_burn_rates_and_burn_alert(tmp_path: Path) -> None:
    """SlaStore.save_summary serializes burn_rates and burn_alert into details_json."""
    sqlite_path = tmp_path / "store_details.sqlite"
    metric_store = MetricStore(sqlite_path)
    metric_store.initialize()

    results = [
        _row("ERROR", "2026-10-16T11:58:00Z"),
        _row("OK", "2026-10-16T11:59:00Z"),
    ]
    metric_store.insert_results(run_id=1, results=results)

    policy = _make_policy(
        objective_percent=90.0,
        burn_rate_alerts=(
            {
                "severity": "page",
                "factor": 1.0,
                "short_window_minutes": 5,
                "long_window_minutes": 60,
            },
        ),
    )

    summary = validate_sla_policies(
        sqlite_path=sqlite_path,
        policies=[policy],
        window_end="2026-10-16T12:00:00Z",
    )

    sla_store = SlaStore(sqlite_path)
    sla_store.initialize()
    run_id = sla_store.save_summary(
        summary=summary,
        started_at="2026-10-16T12:00:00Z",
        finished_at="2026-10-16T12:00:01Z",
        message="test run",
    )

    rows = [dict(r) for r in sla_store.fetch_run_results(sla_run_id=run_id)]
    assert len(rows) == 1
    details = json.loads(rows[0]["details_json"])
    assert "burn_rates" in details
    assert "burn_alert" in details
    assert isinstance(details["burn_rates"], dict)


def test_parser_refuses_burn_rate_alerts_on_non_minutes_policy() -> None:
    """burn_rate_alerts is only allowed when policy_model == 'time_slo' and aggregation == 'minutes'."""
    raw_invalid_model = {
        "policy_id": "TEST_CURRENT_STATE",
        "display_name": "Test",
        "target_ids": "server/sqlserver/master",
        "policy_model": "current_state",
        "metric_codes": "INSTANCE_STATUS",
        "burn_rate_alerts": [
            {
                "severity": "page",
                "factor": 14.4,
                "short_window_minutes": 5,
                "long_window_minutes": 60,
            }
        ],
    }
    with pytest.raises(ValueError) as exc_info:
        parse_sla_policy(raw_invalid_model)
    assert "supported only for policy_model 'time_slo' and aggregation 'minutes'" in str(exc_info.value)

    raw_invalid_agg = {
        "policy_id": "TEST_TIME_SLO_RATIO",
        "display_name": "Test",
        "target_ids": "server/sqlserver/master",
        "policy_model": "time_slo",
        "aggregation": "success_ratio",
        "metric_codes": "INSTANCE_STATUS",
        "objective_percent": 99.9,
        "burn_rate_alerts": [
            {
                "severity": "page",
                "factor": 14.4,
                "short_window_minutes": 5,
                "long_window_minutes": 60,
            }
        ],
    }
    with pytest.raises(ValueError) as exc_info:
        parse_sla_policy(raw_invalid_agg)
    assert "supported only for policy_model 'time_slo' and aggregation 'minutes'" in str(exc_info.value)


def test_transition_passed_to_burning_is_escalation_in_diff_states() -> None:
    """A transition from PASSED to BURNING is treated as an escalation in diff_states."""
    key = "INSTANCE_AVAILABILITY_30D @ server/sqlserver/master"

    # PASSED -> BURNING is new_bad
    diff_from_passed = diff_states(
        previous={key: "PASSED"},
        current={key: "BURNING"},
        severity_order=SLA_SEVERITY_ORDER,
        healthy=("PASSED",),
    )
    assert key in diff_from_passed.new_bad

    # AT_RISK -> BURNING is worsened (BURNING is ranked ahead of AT_RISK in SLA_SEVERITY_ORDER)
    diff_from_at_risk = diff_states(
        previous={key: "AT_RISK"},
        current={key: "BURNING"},
        severity_order=SLA_SEVERITY_ORDER,
        healthy=("PASSED",),
    )
    assert any(item[0] == key and item[1] == "AT_RISK" and item[2] == "BURNING" for item in diff_from_at_risk.worsened)

    # BURNING -> PASSED is recovered
    diff_recovered = diff_states(
        previous={key: "BURNING"},
        current={key: "PASSED"},
        severity_order=SLA_SEVERITY_ORDER,
        healthy=("PASSED",),
    )
    assert key in diff_recovered.recovered
