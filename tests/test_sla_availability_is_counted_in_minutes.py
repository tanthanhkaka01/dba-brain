"""SLA availability counted in minutes (cloud SLA model).

When measuring availability over high-frequency checks (e.g. INSTANCE_STATUS running every 30-60s),
counting raw observation rows distorts the SLA: a single transient probe failure within an otherwise
healthy minute would count as an outage, while 10 probe failures in an outage would count 10 times
more than 1 probe failure.

The cloud SLA standard defines availability by UTC minutes:
- A minute is UP (good) if ANY sample within that UTC minute succeeded (OK or LOGGING).
- A minute is DOWN (bad) only when EVERY sample within that UTC minute failed.
- Minutes with no observations at all are neither UP nor DOWN; they represent an observation gap
  and lower the coverage percentage without penalizing or rewarding the service.
- Error budgets and burn rates follow the observed minutes, identical to other time-based SLOs.
- The aggregation value "minutes" is restricted to policy_model "time_slo" because only time-windowed
  metrics are composed of temporal buckets.
- Minute counts are executed directly in the database store (SlaStore.minute_counts and
  SlaStore.minute_details) in standard SQL, excluding collection failures and maintenance windows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbabrain.metrics.models import MetricResult
from dbabrain.metrics.storage import MetricStore
from dbabrain.sla.compliance import validate_sla_policies
from dbabrain.sla.models import SlaPolicy, SlaPolicyResult
from dbabrain.sla.policies import parse_sla_policy


def _make_policy(**overrides) -> SlaPolicy:
    """Helper to construct a valid minutes-based SlaPolicy."""
    kwargs = {
        "policy_id": "INSTANCE_AVAILABILITY_24H",
        "name": "Instance availability (24h in minutes)",
        "target_ids": ("server/sqlserver/master",),
        "metric_codes": ("INSTANCE_STATUS",),
        "aggregation": "minutes",
        "policy_model": "time_slo",
        "objective_percent": 99.0,
        "window_hours": 1,
        "good_statuses": ("OK", "LOGGING"),
        "at_risk_budget_percent": 25.0,
        "minimum_sample_count": 1,
    }
    kwargs.update(overrides)
    return SlaPolicy(**kwargs)


def _row(
    status: str,
    collected_at: str,
    *,
    metric_code: str = "INSTANCE_STATUS",
    error_type: str | None = None,
) -> dict:
    return {
        "status": status,
        "collected_at": collected_at,
        "metric_code": metric_code,
        "metric_item": "master",
        "metric_value": "1",
        "error_type": error_type,
    }


def _evaluate(
    tmp_path: Path,
    policy: SlaPolicy,
    rows: list[dict],
    window_end: str,
) -> SlaPolicyResult:
    """Helper to write rows into a real SQLite MetricStore and validate via store-counted path."""
    sqlite_path = tmp_path / f"dbabrain_{policy.policy_id}_{len(rows)}.sqlite"
    store = MetricStore(sqlite_path)
    store.initialize()
    if rows:
        metric_results = [
            MetricResult(
                target_id=r.get("target_id", "server/sqlserver/master"),
                server_id="server",
                ip="192.0.2.1",
                db_type="sqlserver",
                db_name="master",
                metric_code=r.get("metric_code", "INSTANCE_STATUS"),
                metric_item=r.get("metric_item", "master"),
                metric_value=r.get("metric_value", "1"),
                metric_unit="",
                status=r["status"],
                importance=5,
                message=None,
                collected_at=r["collected_at"],
                error_type=r.get("error_type"),
            )
            for r in rows
        ]
        store.insert_results(run_id=1, results=metric_results)

    summary = validate_sla_policies(
        sqlite_path=sqlite_path,
        policies=[policy],
        window_end=window_end,
    )
    return summary.results[0]


def test_a_minute_with_one_bad_and_one_good_sample_is_counted_as_up(tmp_path: Path) -> None:
    """A minute is healthy if any observation within that UTC minute succeeded.

    If a probe at :10 fails but a retry at :40 succeeds, the database was reachable during that
    minute, so the cloud SLA rule counts the whole minute as up.
    """
    policy = _make_policy(window_hours=1)
    rows = [
        _row("ERROR", "2026-10-16T10:00:10Z"),
        _row("OK", "2026-10-16T10:00:40Z"),
    ]

    result = _evaluate(tmp_path, policy, rows, "2026-10-16T11:00:00Z")

    assert result.total_count == 1
    assert result.good_count == 1
    assert result.bad_count == 0
    assert result.actual_percent == 100.0
    assert result.compliant is True
    assert result.failures_by_status == {}


def test_a_minute_whose_samples_are_all_bad_is_counted_as_down(tmp_path: Path) -> None:
    """A minute is down only when every observation in it failed.

    When all probes in that minute report failure, that minute is down and counts against the SLA.
    """
    policy = _make_policy(window_hours=1)
    rows = [
        _row("ERROR", "2026-10-16T10:01:10Z"),
        _row("CRITICAL", "2026-10-16T10:01:40Z"),
    ]

    result = _evaluate(tmp_path, policy, rows, "2026-10-16T11:00:00Z")

    assert result.total_count == 1
    assert result.good_count == 0
    assert result.bad_count == 1
    assert result.actual_percent == 0.0
    assert result.compliant is False
    assert result.failures_by_status == {"ERROR": 1, "CRITICAL": 1}


def test_a_minute_with_no_sample_is_neither_up_nor_down_and_lowers_coverage_only(tmp_path: Path) -> None:
    """An unobserved minute is a coverage gap, not downtime and not uptime.

    In a 1-hour window (60 expected minutes), having 15 observed minutes (all good) results in
    100% compliance across the observed minutes and 25% coverage. Unobserved minutes are neither
    assumed up nor assumed down.
    """
    policy = _make_policy(window_hours=1)
    # 15 distinct minutes observed out of 60 expected
    rows = [
        _row("OK", f"2026-10-16T10:{minute:02d}:30Z")
        for minute in range(15)
    ]

    result = _evaluate(tmp_path, policy, rows, "2026-10-16T11:00:00Z")

    assert result.total_count == 15
    assert result.good_count == 15
    assert result.bad_count == 0
    assert result.actual_percent == 100.0
    assert result.expected_sample_count == 60
    assert result.coverage_percent == 25.0


def test_the_error_budget_and_burn_rate_follow_the_observed_minutes(tmp_path: Path) -> None:
    """Error budget consumption and burn rate are computed from observed minutes.

    With a 99.0% objective, the budget allows 1.0% bad time. If 2 minutes out of 100 observed
    minutes are bad, bad_percent is 2.0%, consuming 100% of the budget with a burn rate of 2.0.
    """
    policy = _make_policy(objective_percent=99.0, window_hours=24)
    # 98 good minutes, 2 bad minutes = 100 total minutes
    good_rows = [_row("OK", f"2026-10-16T00:{m:02d}:00Z") for m in range(50)]
    good_rows += [_row("OK", f"2026-10-16T01:{m:02d}:00Z") for m in range(48)]
    bad_rows = [
        _row("ERROR", "2026-10-16T01:48:00Z"),
        _row("ERROR", "2026-10-16T01:49:00Z"),
    ]

    result = _evaluate(tmp_path, policy, good_rows + bad_rows, "2026-10-17T00:00:00Z")

    assert result.total_count == 100
    assert result.good_count == 98
    assert result.bad_count == 2
    assert result.actual_percent == 98.0
    assert result.error_budget_percent == 1.0
    assert result.budget_consumed_percent == 100.0
    assert result.budget_remaining_percent == 0.0
    assert result.burn_rate == 2.0


def test_the_policy_parser_refuses_minutes_on_a_non_time_slo_model() -> None:
    """The 'minutes' aggregation is valid only for time_slo policies.

    Current state or inventory policies represent point-in-time snapshots or object backlogs,
    which have no concept of minute-by-minute duration.
    """
    valid_raw = {
        "policy_id": "TEST_TIME_SLO",
        "display_name": "Test Time SLO",
        "target_ids": "server/sqlserver/master",
        "metric_codes": "INSTANCE_STATUS",
        "policy_model": "time_slo",
        "aggregation": "minutes",
        "objective_percent": 99.9,
    }
    policy = parse_sla_policy(valid_raw)
    assert policy.aggregation == "minutes"
    assert policy.policy_model == "time_slo"

    for invalid_model in ("current_state", "finding_inventory"):
        invalid_raw = dict(valid_raw, policy_model=invalid_model, target_value=0, comparison_operator="<=")
        with pytest.raises(ValueError) as exc_info:
            parse_sla_policy(invalid_raw)
        assert "aggregation 'minutes' is supported only for policy_model 'time_slo'" in str(exc_info.value)


def test_failures_by_status_counts_rows_in_bad_minutes_only(tmp_path: Path) -> None:
    """Probe failures in minutes that were ultimately healthy do not appear in failures_by_status.

    If minute 10:00 had an ERROR but recovered to OK, that ERROR was part of an UP minute and is not
    a service outage. Only rows from minutes where all probes failed are counted.
    """
    policy = _make_policy(window_hours=1)
    rows = [
        # Minute 10:00 - UP: ERROR + OK -> ERROR should NOT be counted in failures_by_status
        _row("ERROR", "2026-10-16T10:00:10Z"),
        _row("OK", "2026-10-16T10:00:40Z"),
        # Minute 10:01 - DOWN: ERROR + CRITICAL -> both must be counted
        _row("ERROR", "2026-10-16T10:01:10Z"),
        _row("CRITICAL", "2026-10-16T10:01:40Z"),
        # Minute 10:02 - DOWN: WARNING + WARNING -> both must be counted
        _row("WARNING", "2026-10-16T10:02:10Z"),
        _row("WARNING", "2026-10-16T10:02:40Z"),
    ]

    result = _evaluate(tmp_path, policy, rows, "2026-10-16T11:00:00Z")

    assert result.total_count == 3
    assert result.good_count == 1
    assert result.bad_count == 2
    assert result.failures_by_status == {"ERROR": 1, "CRITICAL": 1, "WARNING": 2}


def test_a_collection_failure_row_is_not_counted_as_a_minute(tmp_path: Path) -> None:
    """A collection failure (e.g. CONNECT_FAILED) is a monitoring fault, not service downtime.

    The row is excluded from the observed minute counts and details. A minute that has only
    collection failure observations is not observed as good or bad.
    """
    policy = _make_policy(window_hours=1)
    rows = [
        # Minute 10:00 has only a collection failure
        _row("ERROR", "2026-10-16T10:00:15Z", error_type="CONNECT_FAILED"),
        # Minute 10:01 has a successful collection
        _row("OK", "2026-10-16T10:01:15Z"),
    ]

    result = _evaluate(tmp_path, policy, rows, "2026-10-16T11:00:00Z")

    # Only minute 10:01 was observed
    assert result.total_count == 1
    assert result.good_count == 1
    assert result.bad_count == 0
    assert result.actual_percent == 100.0


def test_a_maintenance_window_row_is_not_counted_as_a_minute(tmp_path: Path) -> None:
    """Rows within maintenance windows are completely excluded from minute counting.

    Even if a probe reports ERROR during maintenance, it is neither observed nor counted
    as downtime.
    """
    policy = _make_policy(
        window_hours=1,
        maintenance_windows=(("2026-10-16T10:10:00Z", "2026-10-16T10:20:00Z"),),
    )
    rows = [
        # Normal minute - UP
        _row("OK", "2026-10-16T10:05:00Z"),
        # Minute inside maintenance window - failed, but excluded from SLA evaluation
        _row("ERROR", "2026-10-16T10:15:00Z"),
        # Normal minute - DOWN
        _row("ERROR", "2026-10-16T10:30:00Z"),
    ]

    result = _evaluate(tmp_path, policy, rows, "2026-10-16T11:00:00Z")

    # Maintenance row at 10:15 is excluded, leaving 2 observed minutes: 10:05 (good), 10:30 (bad)
    assert result.total_count == 2
    assert result.good_count == 1
    assert result.bad_count == 1
    assert result.actual_percent == 50.0


def test_validate_sla_policies_end_to_end_with_metric_store(tmp_path: Path) -> None:
    """validate_sla_policies evaluates a minutes-aggregated policy end-to-end against a MetricStore."""
    store = MetricStore(tmp_path / "dbabrain.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id="server/sqlserver/master",
                server_id="server",
                ip="192.0.2.1",
                db_type="sqlserver",
                db_name="master",
                metric_code="INSTANCE_STATUS",
                metric_item="master",
                metric_value="1",
                metric_unit="",
                status="ERROR",
                importance=5,
                message=None,
                collected_at="2026-10-16T10:00:10Z",
            ),
            MetricResult(
                target_id="server/sqlserver/master",
                server_id="server",
                ip="192.0.2.1",
                db_type="sqlserver",
                db_name="master",
                metric_code="INSTANCE_STATUS",
                metric_item="master",
                metric_value="1",
                metric_unit="",
                status="OK",
                importance=5,
                message=None,
                collected_at="2026-10-16T10:00:40Z",
            ),
            MetricResult(
                target_id="server/sqlserver/master",
                server_id="server",
                ip="192.0.2.1",
                db_type="sqlserver",
                db_name="master",
                metric_code="INSTANCE_STATUS",
                metric_item="master",
                metric_value="1",
                metric_unit="",
                status="ERROR",
                importance=5,
                message=None,
                collected_at="2026-10-16T10:01:10Z",
            ),
        ],
    )

    policy = _make_policy(
        policy_id="INSTANCE_AVAILABILITY_24H",
        objective_percent=50.0,
        window_hours=1,
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "db_instances.json").write_text(
        json.dumps({
            "db_instances": [
                {
                    "server_id": "server",
                    "db_type": "sqlserver",
                    "ip": "192.0.2.1",
                    "port": 1433,
                    "service_name": "MSSQLSERVER",
                    "enabled": True,
                }
            ]
        }),
        encoding="utf-8",
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "dbabrain.sqlite",
        policies=[policy],
        window_end="2026-10-16T10:05:00Z",
        data_dir=data_dir,
    )

    assert summary.status == "PASSED"
    assert len(summary.results) == 1
    res = summary.results[0]
    # Minute 10:00 is OK (one bad, one good -> good)
    # Minute 10:01 is ERROR (only one bad -> bad)
    # Total minutes = 2, good = 1, actual = 50.0% >= objective 50.0%
    assert res.total_count == 2
    assert res.good_count == 1
    assert res.bad_count == 1
    assert res.actual_percent == 50.0
    assert res.failures_by_status == {"ERROR": 1}
