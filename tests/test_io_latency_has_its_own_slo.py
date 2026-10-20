"""I/O latency has an SLO of its own, stated as a share of good samples (1.155).

`PERFORMANCE_IO_LATENCY_INTERVAL` (104) grades each 10-second sample by file type - log writes and data
reads have different thresholds. An SLA policy cannot filter items, so a p95 over its values would
put a log write and a data read in one percentile against one number. The policy counts the samples
the metric graded good instead - Google SRE's form of a latency SLO, "99 % of samples within the
threshold" - and stands apart from SQLSERVER_PERFORMANCE_24H, where up to two rows per database every
five minutes would outweigh the metrics already pooled there.
"""

from __future__ import annotations

import json
from pathlib import Path

from dbabrain.sla.policies import load_sla_policies

POLICIES = Path(__file__).resolve().parents[1] / "data" / "sla_policies.json"


def _policy(policy_id: str):
    return next(p for p in load_sla_policies(POLICIES) if p.policy_id == policy_id)


def test_the_latency_slo_counts_the_samples_the_metric_graded_good():
    policy = _policy("SQLSERVER_IO_LATENCY_24H")
    assert policy.metric_codes == ("PERFORMANCE_IO_LATENCY_INTERVAL",)
    assert policy.aggregation == "success_ratio"
    assert policy.objective_percent == 99.0
    assert set(policy.good_statuses) == {"OK", "LOGGING"}
    assert policy.expected_collection_interval_seconds == 300


def test_a_window_too_young_to_hold_one_percent_of_a_sample_is_insufficient_not_a_breach():
    """99 % of fewer than 100 samples leaves no room for a single bad one: 1 of 12 is 91.7 %, a
    breach on the first slow sample of a new window. Below 100 the answer is INSUFFICIENT_DATA."""
    policy = _policy("SQLSERVER_IO_LATENCY_24H")
    assert policy.minimum_sample_count >= round(100 / (100 - policy.objective_percent))


def test_the_interval_metric_is_not_pooled_into_the_performance_slo():
    assert "PERFORMANCE_IO_LATENCY_INTERVAL" not in _policy("SQLSERVER_PERFORMANCE_24H").metric_codes


def test_the_policy_reads_a_metric_the_catalogue_defines():
    catalogue = json.loads((POLICIES.parent / "metric_definitions.json").read_text(encoding="utf-8"))
    assert any(m["metric_code"] == "PERFORMANCE_IO_LATENCY_INTERVAL" for m in catalogue["metrics"])
