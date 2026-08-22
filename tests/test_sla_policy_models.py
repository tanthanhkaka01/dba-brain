"""One row-weighted ratio cannot answer three different questions.

Every policy defaulted to `success_ratio`: good rows divided by all rows. That is a meaningful
number only when the rows are time samples. Applied to objects and to configuration settings it
produced verdicts that were arithmetically correct and operationally false:

* `POSTGRESQL_SECURITY_24H` reported **50% security compliance** from one warning about
  `postgres@cluster` holding superuser plus one good row. Half of what, over what period?
* `SQLSERVER_MAINTENANCE_7D` on 192.0.2.115 reported **4.09%** from 144 good and 3,377 bad
  rows — 1,631 distinct objects seen repeatedly. The backlog is real; the percentage measures how
  often the collector looked at it.
* `SQLSERVER_RECOVERY_CHECKDB_7D` averaged 125 snapshots of 39 databases, so a CHECKDB run this
  morning was diluted by a week of the old state.

So a policy now declares which question it answers. The dangerous direction is a policy silently
falling back to the ratio, which is why an unknown `policy_model` fails the config and a
finding inventory with a `>=` objective is refused outright.
"""

from __future__ import annotations

import pytest

from db_ops.sla import compliance
from db_ops.sla.models import SlaPolicy
from db_ops.sla.policies import parse_sla_policy

WINDOW = ("2026-08-04T00:00:00Z", "2026-08-05T00:00:00Z")


class _Row(dict):
    def __init__(self, status="OK", item="", collected_at="2026-08-04T23:00:00Z", code="M"):
        super().__init__(status=status, error_type="", collected_at=collected_at,
                         metric_code=code, metric_value="1", metric_item=item,
                         target_id="srv/sqlserver/svc")


def _policy(model, **kwargs):
    defaults = dict(policy_id="P", name="p", metric_codes=("M",), objective_percent=99.0,
                    policy_model=model, minimum_sample_count=1)
    defaults.update(kwargs)
    return SlaPolicy(**defaults)


def _build(policy, rows):
    return compliance._build_result(policy, "srv/sqlserver/svc", rows, *WINDOW)


# --------------------------------------------------------------------------- current_state


def test_current_state_reads_only_the_newest_collection():
    """A configuration that was wrong all week and is right now is compliant now. Averaging the
    week answers a question nobody asked."""
    rows = [_Row(status="CRITICAL", collected_at="2026-08-04T01:00:00Z"),
            _Row(status="CRITICAL", collected_at="2026-08-04T02:00:00Z"),
            _Row(status="OK", collected_at="2026-08-04T23:00:00Z")]

    result = _build(_policy("current_state"), rows)

    assert result.actual_value == 100.0
    assert result.status == "PASSED"


def test_current_state_is_binary_not_a_share_of_objects():
    """One host of thirty pending a reboot is not "96.7% compliant with the reboot policy" — it is
    non-compliant, with one host to fix."""
    rows = [_Row(status="OK", item=f"db{index}") for index in range(29)]
    rows.append(_Row(status="WARNING", item="db29"))

    result = _build(_policy("current_state"), rows)

    assert result.actual_value == 0.0
    assert result.status == "FAILED"
    assert result.affected_objects == 1, "the count is what tells the operator how much work it is"


def test_the_postgres_superuser_case_no_longer_reads_as_fifty_percent():
    """The finding that motivated the model: one risky role and one good row read as 50%."""
    rows = [_Row(status="OK", item="app@cluster"), _Row(status="WARNING", item="postgres@cluster")]

    ratio = _build(_policy("time_slo"), rows)
    assert ratio.actual_percent == 50.0, "this is the old behaviour being replaced"

    current = _build(_policy("current_state"), rows)
    assert current.actual_value == 0.0 and current.affected_objects == 1


# ----------------------------------------------------------------------- finding_inventory


def test_a_finding_inventory_counts_distinct_objects_not_observations():
    """1,503 stale statistics observed across repeated collections were reported as 3,377 bad
    rows. An operator has to fix the objects, not the observations."""
    rows = []
    for cycle in ("2026-08-04T01:00:00Z", "2026-08-04T12:00:00Z", "2026-08-04T23:00:00Z"):
        rows += [_Row(status="WARNING", item=f"stat{index}", collected_at=cycle) for index in range(50)]

    result = _build(_policy("finding_inventory", comparison_operator="<=", target_value=0.0), rows)

    assert result.affected_objects == 50
    assert result.actual_value == 50.0, "the reported value is the backlog, not a percentage"
    assert result.status == "FAILED"


def test_a_finding_inventory_reports_no_percentage_or_error_budget():
    """A count has no error budget: "how much of the window may be bad" is not a question about
    a pile of objects, and printing a burn rate for one invites reading it as availability."""
    rows = [_Row(status="WARNING", item=f"idx{index}") for index in range(7)]

    result = _build(_policy("finding_inventory", comparison_operator="<=", target_value=0.0), rows)

    assert result.error_budget_percent == 0.0
    assert result.burn_rate is None


def test_a_clean_inventory_passes():
    rows = [_Row(status="OK", item=f"idx{index}") for index in range(7)]
    result = _build(_policy("finding_inventory", comparison_operator="<=", target_value=0.0), rows)
    assert result.affected_objects == 0 and result.status == "PASSED"


# --------------------------------------------------------------- collection stays measurable


def test_narrowing_to_the_newest_cycle_does_not_make_coverage_collapse():
    """Coverage and freshness are questions about the collector, not about the verdict. Measuring
    them on the one cycle a current_state policy reads would report every such policy as ~1%
    covered and turn a perfectly healthy collector into a data-quality alarm."""
    rows = [_Row(status="OK", collected_at=f"2026-08-04T{hour:02d}:00:00Z") for hour in range(24)]
    policy = _policy("current_state", expected_collection_interval_seconds=3600, window_hours=24)

    result = _build(policy, rows)

    assert result.coverage_percent == 100.0
    assert result.data_quality_status == "OK"


def test_the_sample_minimum_is_judged_on_the_window_not_the_evaluated_cycle():
    """A current_state policy reads one cycle by design; judging that against a 20-sample minimum
    would mark every one of them INSUFFICIENT_DATA forever."""
    rows = [_Row(status="OK", collected_at=f"2026-08-04T{hour:02d}:00:00Z") for hour in range(24)]
    result = _build(_policy("current_state", minimum_sample_count=20), rows)
    assert result.data_quality_status == "OK"


def test_the_newest_cycle_is_per_metric_so_a_slower_metric_is_not_lost():
    """Two metrics on different cadences: taking one global "latest" would drop the slower one
    entirely every time the faster one ran."""
    rows = [_Row(status="OK", code="FAST", collected_at="2026-08-04T23:00:00Z"),
            _Row(status="CRITICAL", code="SLOW", collected_at="2026-08-04T06:00:00Z")]

    kept = compliance._latest_cycle(rows)

    assert {row["metric_code"] for row in kept} == {"FAST", "SLOW"}


# ------------------------------------------------------------------------------- config


def test_an_unknown_policy_model_is_refused_rather_than_defaulted():
    """A typo that silently reverts to the ratio restores the exact misreporting this replaces,
    and does it without a word in the logs."""
    with pytest.raises(ValueError, match="invalid policy_model"):
        parse_sla_policy({"policy_id": "P", "name": "p", "metric_codes": ["M"], "db_types": ["sqlserver"],
                          "objective_percent": 99.0, "policy_model": "finding-inventory"})


def test_a_finding_inventory_with_a_greater_than_objective_is_refused():
    """Left at the default ">= 99" a backlog of 1,631 objects compares as 1631 >= 99 and reports
    PASSED — healthy exactly when it is worst."""
    with pytest.raises(ValueError, match="comparison_operator"):
        parse_sla_policy({"policy_id": "P", "name": "p", "metric_codes": ["M"], "db_types": ["sqlserver"],
                          "objective_percent": 99.0, "policy_model": "finding_inventory",
                          "target_value": 0})


def test_a_finding_inventory_without_a_target_count_is_refused():
    with pytest.raises(ValueError, match="target_value"):
        parse_sla_policy({"policy_id": "P", "name": "p", "metric_codes": ["M"], "db_types": ["sqlserver"],
                          "objective_percent": 99.0, "policy_model": "finding_inventory",
                          "comparison_operator": "<="})


def test_the_unit_follows_the_model_so_a_count_is_never_labelled_a_percentage():
    policy = parse_sla_policy({"policy_id": "P", "name": "p", "metric_codes": ["M"], "db_types": ["sqlserver"],
                               "objective_percent": 99.0, "policy_model": "finding_inventory",
                               "comparison_operator": "<=", "target_value": 0})
    assert policy.unit == "objects"


def test_time_slo_remains_the_default_and_is_unchanged():
    """The change must not quietly alter availability reporting, which was correct all along."""
    rows = [_Row(status="OK") for _ in range(99)] + [_Row(status="CRITICAL")]
    policy = parse_sla_policy({"policy_id": "P", "name": "p", "metric_codes": ["M"],
                               "db_types": ["sqlserver"], "objective_percent": 99.0})

    assert policy.policy_model == "time_slo"
    assert _build(policy, rows).actual_percent == 99.0
