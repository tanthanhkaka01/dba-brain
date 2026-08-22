"""The shapes the SLA app persists — shared between that app and the store that writes them.

They live here rather than in ``sla/models.py`` because the store moved to ``common/sla_store.py``
(2026-08-11) and ``common`` must never import an app. A shape used on both sides of that boundary
belongs below both — the same move ``common/metric_results.py`` records for the metrics app.
(Both were in ``db_ops/contracts/`` until that package was folded into ``common`` on 2026-08-15;
like the rest, this module still imports nothing but stdlib.)

``SlaPolicy`` deliberately stayed behind in ``sla/models.py``: a policy is *config the app parses*,
not a row anything writes, so the store has no business knowing its shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SlaPolicyResult:
    policy_id: str
    name: str
    target_id: str
    status: str
    objective_percent: float
    actual_percent: float
    error_budget_percent: float
    budget_consumed_percent: float
    budget_remaining_percent: float
    total_count: int
    good_count: int
    bad_count: int
    no_data: bool
    window_hours: int
    window_start: str
    window_end: str
    scope: str = ""
    category: str = ""
    failures_by_status: dict[str, int] = field(default_factory=dict)
    sli_code: str = ""
    domain: str = ""
    aggregation: str = "success_ratio"
    policy_model: str = "time_slo"
    #: The verdict from the NEWEST cycle alone: "OK", "BAD", or "" when nothing was collected.
    #:
    #: A rolling window and the present tense answer different questions, and the report was only
    #: showing the window. On ACME-192-0-2-250 `OS_REBOOT_PENDING` was warning from 2026-07-29
    #: to 08-02 and OK on 08-03 and 08-04: the seven-day arithmetic said 28.57% and was right,
    #: while the host was not actually pending a reboot. Presenting only the window makes a
    #: historical breach read as an active incident.
    current_status: str = ""
    #: For finding_inventory: distinct affected objects in the newest cycle. The number an
    #: operator actually works through, as opposed to how many times it was observed.
    affected_objects: int = 0
    comparison_operator: str = ">="
    actual_value: float | None = None
    objective_value: float | None = None
    unit: str = "percentage"
    compliant: bool | None = None
    expected_sample_count: int = 0
    coverage_percent: float = 0.0
    data_quality_status: str = "OK"
    data_freshness_seconds: float | None = None
    reason: str = ""
    required: bool = True
    error_budget_total: float = 0.0
    error_budget_consumed: float = 0.0
    error_budget_remaining: float = 0.0
    burn_rate: float | None = None


@dataclass(frozen=True)
class SlaValidationSummary:
    status: str
    policy_count: int
    result_count: int
    passed_count: int
    at_risk_count: int
    failed_count: int
    no_data_count: int
    window_end: str
    results: tuple[SlaPolicyResult, ...]


def state_key(policy_id: object, target_id: object) -> str:
    """The identity of one evaluated thing, stable across runs.

    A run-to-run comparison needs a key that survives reordering and re-numbering, so it is built
    from what the result *is* (policy plus target) rather than its row id. This is the same string
    the reports and Telegram messages label a result with, so a key in a diff can be pasted into a
    search of the web report and find its row.
    """
    target = str(target_id or "").strip()
    policy = str(policy_id or "").strip()
    return f"{policy} @ {target}" if target and target != "*" else policy
