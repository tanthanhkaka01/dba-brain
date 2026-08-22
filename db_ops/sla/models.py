from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SlaPolicy:
    """A Service Level Agreement: a scope (which instances) + a Service Level Objective
    (``objective_percent`` over ``window_hours``) evaluated against a Service Level
    Indicator computed purely from collected ``metric_results`` rows.

    Scope selects the *population of instances* the policy applies to — either explicit
    ``target_ids`` or, more commonly, every target of a ``db_types``. By default the SLO is
    evaluated **per instance** (one result per ``target_id``), because an SLA belongs to a
    specific service, not a lumped fleet. Set ``aggregate=True`` only for a deliberate
    fleet-wide number.
    """

    policy_id: str
    name: str
    metric_codes: tuple[str, ...]
    objective_percent: float
    target_ids: tuple[str, ...] = ()
    db_types: tuple[str, ...] = ()
    window_hours: int = 24
    good_statuses: tuple[str, ...] = ("OK", "LOGGING")
    # Below this much error budget remaining, a still-passing SLO is flagged AT_RISK.
    at_risk_budget_percent: float = 25.0
    aggregate: bool = False
    category: str = ""
    description: str = ""
    sli_code: str = ""
    aggregation: str = "success_ratio"
    #: Which question this policy answers. One row-weighted ratio cannot answer all three, and
    #: using it for all of them is how "one superuser role warning plus one good row" became
    #: "50% security compliance", and how 3,377 stale-statistics rows became "4.09% maintenance".
    #:
    #: * ``time_slo`` — the fraction of the window the service was good. Availability,
    #:   connectivity, saturation, backup RPO, latency. The only model where a percentage of
    #:   samples means anything, because the samples are time.
    #: * ``current_state`` — is it compliant *now*? Only the newest cycle is read. Reboot pending,
    #:   security configuration, service state, latest CHECKDB. A config that was wrong for six
    #:   days and is right now is compliant; averaging it over the window answers no useful
    #:   question.
    #: * ``finding_inventory`` — how many distinct things are affected? Stale statistics,
    #:   fragmented indexes, risky roles, failed jobs. Counts distinct ``metric_item`` values in
    #:   the newest cycle, so repeated snapshots of the same 1,503 objects count once, not
    #:   3,377 times.
    policy_model: str = "time_slo"
    comparison_operator: str = ">="
    target_value: float | None = None
    unit: str = "percentage"
    minimum_sample_count: int = 1
    expected_collection_interval_seconds: int | None = None
    freshness_threshold_seconds: int | None = None
    missing_data_policy: str = "unknown"
    required: bool = True
    maintenance_windows: tuple[tuple[str, str], ...] = ()
    percentile: float = 95.0


# SlaPolicyResult, SlaValidationSummary and state_key moved to db_ops/common/sla_results.py when
# the SLA store moved to common (common may not import an app). Re-exported here so the app's own
# imports, and the tests that import them from this module, keep working.
from db_ops.db.sla_results import (  # noqa: E402,F401 - re-exported for compatibility
    SlaPolicyResult,
    SlaValidationSummary,
    state_key,
)
