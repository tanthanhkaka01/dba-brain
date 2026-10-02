"""A threshold override grades a number; it never overrules what the metric decided without one.

A row the metric's SQL marked CRITICAL, ERROR or NO_DATA for a reason that is not its value - the
database is offline and reports `0`, the check failed, there was nothing to read - became OK the
moment any override with thresholds existed for that metric on the target (review 0.25.0, F3.2).
Thresholds still grade OK, LOGGING and WARNING rows in both directions; lowering a verdict is said
outright with `severity_map`, which runs after.
"""

from __future__ import annotations

import pytest

from db_ops.metrics.collector import _apply_threshold_override
from db_ops.metrics.models import MetricDefinition, MetricTarget

METRIC = MetricDefinition("DATABASE_STATE", "sqlserver", "availability", 5, True)


def _target() -> MetricTarget:
    return MetricTarget(
        target_id="lab/sqlserver/lab-01", server_id="lab-01", ip="192.0.2.30", db_type="sqlserver",
        db_name="master", credential_name="fake",
        metrics_config={"metric_overrides": {
            "DATABASE_STATE": {"warning_threshold": 5, "critical_threshold": 10}}},
    )


@pytest.mark.parametrize("verdict", ["CRITICAL", "ERROR", "NO_DATA"])
def test_the_metric_s_own_verdict_stands(verdict):
    status, _ = _apply_threshold_override(metric=METRIC, target=_target(), metric_value="0",
                                          status=verdict, message="database is OFFLINE")

    assert status == verdict


@pytest.mark.parametrize("given, value, graded", [("OK", "12", "CRITICAL"), ("WARNING", "1", "OK"),
                                                  ("LOGGING", "7", "WARNING")])
def test_a_graded_row_is_still_graded_both_ways(given, value, graded):
    status, _ = _apply_threshold_override(metric=METRIC, target=_target(), metric_value=value,
                                          status=given, message="m")

    assert status == graded
