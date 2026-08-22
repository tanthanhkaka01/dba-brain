from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db_ops.sla.models import SlaPolicy
from db_ops.lib.paths import DEFAULT_DATA_DIR


DEFAULT_POLICIES_PATH = DEFAULT_DATA_DIR / "sla_policies.json"


#: The three questions an SLA policy can answer. See SlaPolicy.policy_model for why one
#: row-weighted ratio cannot answer all three.
SUPPORTED_POLICY_MODELS = frozenset({"time_slo", "current_state", "finding_inventory"})


def load_sla_policies(path: str | Path | None = None) -> list[SlaPolicy]:
    policy_path = Path(path) if path else DEFAULT_POLICIES_PATH
    with policy_path.open("r", encoding="utf-8-sig") as file:
        raw = json.load(file)
    items = raw.get("sla_policies", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("SLA policy file must contain an array or an object with sla_policies array.")
    return [parse_sla_policy(item) for item in items if bool(item.get("active", True))]


def parse_sla_policy(raw: dict[str, Any]) -> SlaPolicy:
    policy_id = str(raw.get("policy_id") or "").strip()
    if not policy_id:
        raise ValueError("SLA policy requires policy_id.")
    target_ids = _parse_string_tuple(raw.get("target_ids"))
    db_types = tuple(db_type.lower() for db_type in _parse_string_tuple(raw.get("db_types")))
    metric_codes = _parse_string_tuple(raw.get("metric_codes"))
    if not target_ids and not db_types:
        raise ValueError(f"SLA policy {policy_id} requires target_ids or db_types.")
    if not metric_codes:
        raise ValueError(f"SLA policy {policy_id} requires metric_codes.")
    objective = float(raw.get("objective_percent", raw.get("slo_target", 99.0)))
    aggregation = str(raw.get("aggregation") or raw.get("aggregation_method") or "success_ratio").lower()
    supported_aggregations = {"success_ratio", "average", "minimum", "maximum", "sum", "count", "latest", "percentile"}
    if aggregation not in supported_aggregations:
        raise ValueError(f"SLA policy {policy_id} has invalid aggregation: {aggregation}.")
    # An unrecognised model must fail the config rather than fall back to time_slo: a typo that
    # silently reverts a finding inventory to a row-weighted percentage restores exactly the
    # misreporting this field exists to end, and it would do so without a word in the logs.
    policy_model = str(raw.get("policy_model") or "time_slo").strip().lower()
    if policy_model not in SUPPORTED_POLICY_MODELS:
        raise ValueError(
            f"SLA policy {policy_id} has invalid policy_model: {policy_model}. "
            f"Expected one of {', '.join(sorted(SUPPORTED_POLICY_MODELS))}."
        )
    if policy_model == "finding_inventory":
        # The actual value is a COUNT of affected objects, so the objective must be an upper
        # bound. Left at the default ">= 99" a backlog of 1,503 stale statistics would compare as
        # 1503 >= 99 and report PASSED — a silent inversion that reads as healthy precisely when
        # the backlog is worst. Refuse the config instead of guessing which way it meant.
        if str(raw.get("comparison_operator") or raw.get("operator") or "").strip() not in ("<=", "<"):
            raise ValueError(
                f"SLA policy {policy_id} is a finding_inventory and must set comparison_operator "
                f'to "<=" or "<": its actual value is a count of affected objects, and a ">=" '
                f"objective would report the largest backlogs as compliant."
            )
        if raw.get("target_value") is None:
            raise ValueError(
                f"SLA policy {policy_id} is a finding_inventory and must set target_value: the "
                f"maximum number of affected objects tolerated (0 for none)."
            )
    percentile = float(raw.get("percentile", 95.0))
    if aggregation == "percentile" and not 0 < percentile <= 100:
        raise ValueError(f"SLA policy {policy_id} percentile must be > 0 and <= 100.")
    operator = str(raw.get("comparison_operator") or raw.get("operator") or ">=")
    if operator not in {">=", ">", "<=", "<", "==", "!="}:
        raise ValueError(f"SLA policy {policy_id} has invalid comparison operator: {operator}.")
    minimum_sample_count = int(raw.get("minimum_sample_count", 1))
    if minimum_sample_count < 1:
        raise ValueError(f"SLA policy {policy_id} minimum_sample_count must be >= 1.")
    missing_policy = str(raw.get("missing_data_policy") or "unknown").lower()
    if missing_policy not in {"unknown", "bad", "good", "ignore"}:
        raise ValueError(f"SLA policy {policy_id} has invalid missing_data_policy: {missing_policy}.")
    maintenance = _parse_maintenance_windows(raw.get("maintenance_windows"), policy_id)
    window_hours = int(raw.get("window_hours", 24))
    if window_hours < 1:
        raise ValueError(f"SLA policy {policy_id} window_hours must be >= 1.")
    return SlaPolicy(
        policy_id=policy_id,
        name=str(raw.get("name") or policy_id),
        target_ids=target_ids,
        db_types=db_types,
        metric_codes=metric_codes,
        objective_percent=objective,
        window_hours=window_hours,
        good_statuses=tuple(status.upper() for status in _parse_string_tuple(raw.get("good_statuses")) or ("OK", "LOGGING")),
        at_risk_budget_percent=float(raw.get("at_risk_budget_percent", 25.0)),
        aggregate=bool(raw.get("aggregate", False)),
        category=str(raw.get("category") or ""),
        description=str(raw.get("description") or ""),
        sli_code=str(raw.get("sli_code") or policy_id),
        aggregation=aggregation,
        policy_model=policy_model,
        comparison_operator=operator,
        target_value=float(raw["target_value"]) if raw.get("target_value") is not None else None,
        unit=str(raw.get("unit") or _default_unit(policy_model)),
        minimum_sample_count=minimum_sample_count,
        expected_collection_interval_seconds=(int(raw["expected_collection_interval_seconds"]) if raw.get("expected_collection_interval_seconds") else None),
        freshness_threshold_seconds=(int(raw["freshness_threshold_seconds"]) if raw.get("freshness_threshold_seconds") else None),
        missing_data_policy=missing_policy,
        required=bool(raw.get("required", True)),
        maintenance_windows=maintenance,
        percentile=percentile,
    )


def _parse_string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        raise ValueError("Expected string or array.")
    return tuple(item for item in items if item)


def _parse_maintenance_windows(value: object, policy_id: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"SLA policy {policy_id} maintenance_windows must be an array.")
    windows = []
    for item in value:
        if not isinstance(item, dict) or not item.get("start") or not item.get("end"):
            raise ValueError(f"SLA policy {policy_id} maintenance window requires start and end.")
        start, end = str(item["start"]), str(item["end"])
        if start >= end:
            raise ValueError(f"SLA policy {policy_id} maintenance window start must precede end.")
        windows.append((start, end))
    return tuple(windows)


def _default_unit(policy_model: str) -> str:
    """What the actual value is measured in. A count rendered as "1503 percentage" tells the
    reader the report does not know what it is showing."""
    if policy_model == "finding_inventory":
        return "objects"
    if policy_model == "current_state":
        return "compliant"
    return "percentage"
