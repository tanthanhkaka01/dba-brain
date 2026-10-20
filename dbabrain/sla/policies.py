from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dbabrain.sla.models import SlaPolicy
from dbabrain.lib import field_names
from dbabrain.lib.paths import DEFAULT_DATA_DIR


DEFAULT_POLICIES_PATH = DEFAULT_DATA_DIR / "sla_policies.json"


#: The questions an SLA policy can answer. See SlaPolicy.policy_model for why one
#: row-weighted ratio cannot answer every question.
SUPPORTED_POLICY_MODELS = frozenset({"time_slo", "current_state", "finding_inventory", "backup_rpo", "restore_drill"})


def load_sla_policies(path: str | Path | None = None) -> list[SlaPolicy]:
    """Every active policy in the document, or an empty list when the default one is not there.

    **Absent and named-but-absent are different.** A path the caller typed is a file they believe
    in, and failing to open it is the answer they need. The *default* path missing is a node that
    has not been given objectives yet, and raising there turns a configuration state into a
    traceback — while returning `[]` reaches `validate_sla_policies`, which reports it as
    `NOT_CONFIGURED` and names the file. Neither route may end at "compliant".
    """
    policy_path = Path(path) if path else DEFAULT_POLICIES_PATH
    if path is None and not policy_path.exists():
        return []
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
    # An unrecognised model must fail the config rather than fall back to time_slo: a typo that
    # silently reverts a finding inventory or restore drill to a row-weighted percentage restores
    # exactly the misreporting this field exists to end.
    policy_model = str(raw.get("policy_model") or "time_slo").strip().lower()
    if policy_model not in SUPPORTED_POLICY_MODELS:
        raise ValueError(
            f"SLA policy {policy_id} has invalid policy_model: {policy_model}. "
            f"Expected one of {', '.join(sorted(SUPPORTED_POLICY_MODELS))}."
        )
    target_ids = _parse_string_tuple(raw.get("target_ids"))
    db_types = tuple(db_type.lower() for db_type in _parse_string_tuple(raw.get("db_types")))
    metric_codes = _parse_string_tuple(raw.get("metric_codes"))
    if policy_model != "restore_drill":
        if not target_ids and not db_types:
            raise ValueError(f"SLA policy {policy_id} requires target_ids or db_types.")
        if not metric_codes:
            raise ValueError(f"SLA policy {policy_id} requires metric_codes.")
    rto_target_seconds = None
    if policy_model == "restore_drill":
        raw_rto = raw.get("rto_target_seconds")
        if raw_rto is None:
            raise ValueError(
                f"SLA policy {policy_id} is a restore_drill and requires rto_target_seconds."
            )
        try:
            rto_target_seconds = int(raw_rto)
        except (TypeError, ValueError):
            raise ValueError(
                f"SLA policy {policy_id} rto_target_seconds must be a positive integer."
            ) from None
        if rto_target_seconds <= 0:
            raise ValueError(
                f"SLA policy {policy_id} rto_target_seconds must be a positive integer."
            )
    elif raw.get("rto_target_seconds") is not None:
        try:
            rto_target_seconds = int(raw["rto_target_seconds"])
        except (TypeError, ValueError):
            pass
    # Each of these was accepted under two names; the standard ones are read first (0.22.0).
    objective = float(field_names.read(raw, "sla_policy", "objective_percent", 99.0))
    aggregation = str(field_names.read(raw, "sla_policy", "aggregation", "")
                      or "success_ratio").lower()
    supported_aggregations = {"success_ratio", "average", "minimum", "maximum", "sum", "count", "latest", "percentile", "minutes"}
    if aggregation not in supported_aggregations:
        raise ValueError(f"SLA policy {policy_id} has invalid aggregation: {aggregation}.")
    if aggregation == "minutes" and policy_model != "time_slo":
        raise ValueError(
            f"SLA policy {policy_id} aggregation 'minutes' is supported only for policy_model 'time_slo', got '{policy_model}'."
        )
    if policy_model in ("finding_inventory", "backup_rpo", "restore_drill"):
        # The actual value is a COUNT of affected objects or breaches, so the objective must be an upper
        # bound. Left at the default ">= 99" a count would compare as count >= 99 and report PASSED.
        if str(field_names.read(raw, "sla_policy", "comparison_operator", "") or "").strip() \
                not in ("<=", "<"):
            raise ValueError(
                f"SLA policy {policy_id} is a {policy_model} and must set comparison_operator "
                f'to "<=" or "<": its actual value is a count of breaches or affected objects, '
                f'and a ">=" objective would report the largest backlogs as compliant.'
            )
        if raw.get("target_value") is None:
            raise ValueError(
                f"SLA policy {policy_id} is a {policy_model} and must set target_value: the "
                f"maximum number tolerated (0 for none)."
            )
    percentile = float(raw.get("percentile", 95.0))
    if aggregation == "percentile" and not 0 < percentile <= 100:
        raise ValueError(f"SLA policy {policy_id} percentile must be > 0 and <= 100.")
    operator = str(field_names.read(raw, "sla_policy", "comparison_operator", "") or ">=")
    if operator not in {">=", ">", "<=", "<", "==", "!="}:
        raise ValueError(f"SLA policy {policy_id} has invalid comparison operator: {operator}.")
    minimum_sample_count = int(raw.get("minimum_sample_count", 1))
    if minimum_sample_count < 1:
        raise ValueError(f"SLA policy {policy_id} minimum_sample_count must be >= 1.")
    missing_policy = str(raw.get("missing_data_policy") or "unknown").lower()
    if missing_policy not in {"unknown", "bad", "good", "ignore"}:
        raise ValueError(f"SLA policy {policy_id} has invalid missing_data_policy: {missing_policy}.")
    aggregate = bool(raw.get("aggregate", False))
    if aggregation == "minutes" and aggregate:
        raise ValueError(
            f"SLA policy {policy_id} aggregate: true is not supported with aggregation 'minutes'."
        )
    burn_rate_alerts = _parse_burn_rate_alerts(raw.get("burn_rate_alerts"), policy_id, policy_model, aggregation)
    calendar_month_report = bool(raw.get("calendar_month_report", False))
    maintenance = _parse_maintenance_windows(raw.get("maintenance_windows"), policy_id)
    window_hours = int(raw.get("window_hours", 24))
    if window_hours < 1:
        raise ValueError(f"SLA policy {policy_id} window_hours must be >= 1.")
    return SlaPolicy(
        policy_id=policy_id,
        name=str(field_names.read(raw, "sla_policy", "display_name", "") or policy_id),
        target_ids=target_ids,
        db_types=db_types,
        metric_codes=metric_codes,
        objective_percent=objective,
        window_hours=window_hours,
        good_statuses=tuple(status.upper() for status in _parse_string_tuple(raw.get("good_statuses")) or ("OK", "LOGGING")),
        at_risk_budget_percent=float(raw.get("at_risk_budget_percent", 25.0)),
        aggregate=aggregate,
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
        rto_target_seconds=rto_target_seconds,
        burn_rate_alerts=burn_rate_alerts,
        calendar_month_report=calendar_month_report,
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
    if policy_model in ("backup_rpo", "restore_drill"):
        return "breaches"
    return "percentage"


def _parse_burn_rate_alerts(
    value: object,
    policy_id: str,
    policy_model: str,
    aggregation: str,
) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"SLA policy {policy_id} burn_rate_alerts must be an array.")
    if not value:
        return ()
    if policy_model != "time_slo" or aggregation != "minutes":
        raise ValueError(
            f"SLA policy {policy_id} burn_rate_alerts is supported only for policy_model 'time_slo' and aggregation 'minutes'."
        )
    alerts = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"SLA policy {policy_id} burn_rate_alerts items must be objects.")
        severity = str(item.get("severity") or "").strip().lower()
        if severity not in ("page", "ticket"):
            raise ValueError(
                f"SLA policy {policy_id} burn_rate_alerts severity must be 'page' or 'ticket', got {severity!r}."
            )
        try:
            factor = float(item["factor"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                f"SLA policy {policy_id} burn_rate_alerts factor must be a number > 0."
            ) from None
        if factor <= 0:
            raise ValueError(
                f"SLA policy {policy_id} burn_rate_alerts factor must be a number > 0."
            )
        try:
            long_w = int(item["long_window_minutes"])
            short_w = int(item["short_window_minutes"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                f"SLA policy {policy_id} burn_rate_alerts window minutes must be integers."
            ) from None
        if long_w <= 0 or short_w <= 0:
            raise ValueError(
                f"SLA policy {policy_id} burn_rate_alerts window minutes must be positive integers."
            )
        if short_w >= long_w:
            raise ValueError(
                f"SLA policy {policy_id} burn_rate_alerts short_window_minutes must be smaller than long_window_minutes."
            )
        alerts.append({
            "severity": severity,
            "factor": factor,
            "long_window_minutes": long_w,
            "short_window_minutes": short_w,
        })
    return tuple(alerts)

