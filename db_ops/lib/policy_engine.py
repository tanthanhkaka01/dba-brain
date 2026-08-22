from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from db_ops.lib.coerce import as_float


STATUS_ORDER = {"SUPPRESS": -1, "OK": 0, "LOGGING": 0, "NO_DATA": 1, "WARNING": 2, "ERROR": 2, "UNKNOWN": 2, "CRITICAL": 3}


def status_rank(status: str) -> int:
    """How alarming a status is, for sorting worst-first. One ranking for the whole tool.

    Metrics and Reports each carried a private copy of this table until 2026-08-06, and
    they had drifted into disagreeing about the top of the scale: the metrics CLI sorted
    ``ERROR`` *above* ``CRITICAL``, Reports sorted it below. The same rows then came out
    in a different order depending on which app rendered them, so an operator comparing a
    Telegram report against a manual ``metrics latest`` saw a different "worst" row and
    had no way to tell which one to believe. ``CRITICAL`` is the top of the scale.

    Unknown statuses rank 0 (alongside ``OK``) rather than raising: a status string that
    reaches a report is already persisted data, and a sort is not the place to fail on it.
    """
    return STATUS_ORDER.get(status.upper(), 0)


@dataclass
class PolicyEvent:
    title_key: str
    event_code: str
    severity: str
    rows: list[dict[str, Any]]
    fields: dict[str, Any]
    render_fields: list[str] = field(default_factory=list)


@dataclass
class PolicyResult:
    rows: list[dict[str, Any]]
    effective_rows: list[dict[str, Any]]
    events: list[PolicyEvent]
    consumed_result_ids: set[str]
    suppressed_result_ids: set[str]


def apply_report_policy(
    rows: list[Any],
    *,
    metric_policies: dict[str, dict[str, Any]],
    instance_policies: dict[str, dict[str, Any]],
) -> PolicyResult:
    effective_rows = [_effective_row(row, metric_policies, instance_policies) for row in rows]

    # Future hook: technical/noise/policy grouping is intentionally disabled for
    # warning/critical reports so original metric rows and messages remain visible.
    events: list[PolicyEvent] = []
    consumed: set[str] = set()
    events.extend(_technical_error_events(effective_rows, metric_policies, consumed))
    events.extend(_condition_group_events(effective_rows, metric_policies, consumed))

    return PolicyResult(
        rows=effective_rows,
        effective_rows=effective_rows,
        events=events,
        consumed_result_ids=consumed,
        suppressed_result_ids=set(),
    )


def render_policy_event(event: PolicyEvent) -> str:
    fields = event.render_fields or [key for key in event.fields if key not in {"instance_key", "target"}]
    parts = []
    for field_name in fields:
        if field_name == "error_type":
            continue
        value = event.fields.get(field_name)
        if value not in (None, ""):
            parts.append(f"{field_name}={_format_value(value)}")
    suffix = "; " + "; ".join(parts) if parts else ""
    error_type = event.fields.get("error_type")
    name = f"{event.event_code} / {error_type}" if error_type else event.event_code
    return f"{name}: count={len(event.rows)}{suffix}"


def normalize_status(value: Any) -> str:
    status = str(value or "OK").strip().upper()
    if status == "WARN":
        return "WARNING"
    # A cmd collector emits UNKNOWN when its script hit an error (docs/04 output
    # contract). Treat it as a WARNING-level problem in reports — never silently as
    # OK, which would hide a failed OS collector from the warning/critical report.
    if status == "UNKNOWN":
        return "WARNING"
    return status if status in STATUS_ORDER else "OK"


def row_status(row: Any) -> str:
    return normalize_status(_get(row, "_policy_status") or _get(row, "status"))


def row_context(row: Any) -> dict[str, Any]:
    context = dict(_get(row, "_policy_context") or {})
    if context:
        return context
    return _build_context(_row_dict(row), {}, {})


def extract_fields(row: Any) -> dict[str, Any]:
    return _extract_fields(_row_dict(row))


def _effective_row(row: Any, metric_policies: dict[str, dict[str, Any]], instance_policies: dict[str, dict[str, Any]]) -> dict[str, Any]:
    data = _row_dict(row)
    metric_code = str(data.get("metric_code") or "").upper()
    target_id = str(data.get("target_id") or "")
    metric_policy = metric_policies.get(metric_code, {})
    instance_policy = instance_policies.get(target_id, {})
    context = _build_context(data, metric_policy, instance_policy)
    status = normalize_status(data.get("status"))
    # Future hook: severity/report policies are currently pass-through. Keep the
    # normalized status annotation for existing callers without changing rows.
    data["_policy_status"] = status
    data["_policy_context"] = context
    return data


def _build_context(row: dict[str, Any], metric_policy: dict[str, Any], instance_policy: dict[str, Any]) -> dict[str, Any]:
    fields = _extract_fields(row)
    collector_type = str(row.get("collector_type") or metric_policy.get("collector_type") or "").strip().upper()
    category = str(row.get("category") or metric_policy.get("category") or "").strip()
    target = str(row.get("target_id") or "")
    context = {
        **fields,
        "result_id": row.get("result_id"),
        "target_id": target,
        "instance_key": target or row.get("server_id") or row.get("ip"),
        "target": target,
        "server_id": row.get("server_id"),
        "ip": row.get("ip"),
        "db_name": row.get("db_name"),
        "metric_code": str(row.get("metric_code") or "").upper(),
        "metric_item": row.get("metric_item"),
        "metric_value": _number_or_text(row.get("metric_value")),
        "metric_unit": row.get("metric_unit"),
        "message": row.get("message"),
        "collector_type": collector_type,
        "category": category,
        "error_type": str(row.get("error_type") or fields.get("error_type") or "").strip().upper(),
        "normalized_error_signature": row.get("normalized_error_signature") or fields.get("normalized_error_signature"),
        "env": str(instance_policy.get("env") or "").strip().lower(),
    }
    if "database" not in context and context.get("db_name"):
        context["database"] = context["db_name"]
    return context


def _extract_fields(row: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for source in (row.get("metric_item"), row.get("message")):
        text = str(source or "")
        for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=([^,;|]+)", text):
            fields[key.strip().lower()] = _number_bool_or_text(value.strip())
        for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_ ]{1,40}?)\s+(-?\d+(?:\.\d+)?)\b", text):
            normalized_key = "_".join(key.strip().lower().split())
            fields.setdefault(normalized_key, _number_bool_or_text(value.strip()))
    return fields


def _apply_severity_policy(status: str, context: dict[str, Any], policy: dict[str, Any]) -> str:
    threshold_status = status
    for threshold in policy.get("thresholds") or []:
        if not isinstance(threshold, dict) or _unless_matches(context, threshold.get("unless") or []):
            continue
        if _condition_matches(context, threshold):
            matched_status = normalize_status(threshold.get("severity"))
            if matched_status == "SUPPRESS":
                return "SUPPRESS"
            if STATUS_ORDER.get(matched_status, 0) >= STATUS_ORDER.get(threshold_status, 0):
                threshold_status = matched_status
    status = threshold_status
    action = str((policy.get("by_env") or {}).get(str(context.get("env") or "").lower()) or policy.get("default") or "KEEP").upper()
    return _apply_severity_action(status, action)


def _apply_instance_policy(status: str, context: dict[str, Any], policy: dict[str, Any]) -> str:
    overrides = policy.get("severity_overrides") or {}
    if isinstance(overrides, dict):
        event_code = str(context.get("event_code") or "")
        metric_code = str(context.get("metric_code") or "")
        action = overrides.get(event_code) or overrides.get(metric_code)
        if action:
            return _apply_severity_action(status, str(action).upper())
    return status


def _apply_severity_action(status: str, action: str) -> str:
    if action in {"", "KEEP"}:
        return status
    if action == "SUPPRESS":
        return "SUPPRESS"
    if action == "DOWNGRADE_TO_WARNING" and STATUS_ORDER.get(status, 0) > STATUS_ORDER["WARNING"]:
        return "WARNING"
    if action == "DOWNGRADE_TO_LOGGING" and STATUS_ORDER.get(status, 0) > STATUS_ORDER["LOGGING"]:
        return "LOGGING"
    if action in STATUS_ORDER:
        return action
    return status


def _technical_error_events(rows: list[dict[str, Any]], metric_policies: dict[str, dict[str, Any]], consumed: set[str]) -> list[PolicyEvent]:
    # Future hook: technical grouping disabled. Do not consume raw rows.
    return []
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    configs: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        policy = (metric_policies.get(str(row.get("metric_code") or "").upper(), {}).get("technical_error") or {})
        if not bool(policy.get("collapse_enabled", False)):
            continue
        context = row_context(row)
        if not context.get("error_type"):
            continue
        key = tuple(context.get(field) for field in policy.get("key_fields") or ["instance_key", "error_type", "normalized_error_signature"])
        grouped.setdefault(key, []).append(row)
        configs[key] = policy

    events = []
    for key, group_rows in sorted(grouped.items(), key=lambda item: str(item[0])):
        policy = configs[key]
        first_context = row_context(group_rows[0])
        event_code = _render_template(str(policy.get("event_code_template") or policy.get("event_code") or "MONITORING_EVENT"), first_context)
        fields = {
            **first_context,
            "sample_metrics": sorted({str(row.get("metric_code") or "").upper() for row in group_rows})[: int(policy.get("max_sample_metrics") or 3)],
        }
        events.append(PolicyEvent(str(first_context.get("instance_key") or ""), event_code, row_status(group_rows[0]), group_rows, fields, ["error_type", "sample_metrics"]))
        if bool(policy.get("replace_raw_rows", True)):
            consumed.update(_row_id(row) for row in group_rows)
    return events


def _condition_group_events(rows: list[dict[str, Any]], metric_policies: dict[str, dict[str, Any]], consumed: set[str]) -> list[PolicyEvent]:
    # Future hook: policy grouping disabled. Do not consume raw rows.
    return []
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    configs: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        metric_policy = metric_policies.get(str(row.get("metric_code") or "").upper(), {})
        policy = metric_policy.get("condition_grouping") or {}
        if not bool(policy.get("enabled", False)) or _row_id(row) in consumed:
            continue
        context = row_context(row)
        key = (str(row.get("metric_code") or "").upper(), *(context.get(field) for field in policy.get("key_fields") or ["instance_key", "metric_item"]))
        grouped.setdefault(key, []).append(row)
        configs[key] = metric_policy

    events = []
    for key, group_rows in sorted(grouped.items(), key=lambda item: str(item[0])):
        metric_policy = configs[key]
        group_policy = metric_policy.get("condition_grouping") or {}
        render_policy = metric_policy.get("render_policy") or {}
        fields = _aggregate_fields(group_rows, group_policy.get("aggregate_fields") or {})
        fields.update({name: row_context(group_rows[0]).get(name) for name in group_policy.get("key_fields") or []})
        event_code = str(group_policy.get("event_code") or group_rows[0].get("metric_code") or "").upper()
        events.append(
            PolicyEvent(
                str(row_context(group_rows[0]).get("instance_key") or ""),
                event_code,
                max((row_status(row) for row in group_rows), key=lambda item: STATUS_ORDER.get(item, 0)),
                group_rows,
                fields,
                list(render_policy.get("fields") or fields.keys()),
            )
        )
        if bool(group_policy.get("replace_raw_rows", True)):
            consumed.update(_row_id(row) for row in group_rows)
    return events


def _aggregate_fields(rows: list[dict[str, Any]], aggregate_fields: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    contexts = [row_context(row) for row in rows]
    for output_field, spec in aggregate_fields.items():
        if spec == "count":
            result[output_field] = len(rows)
        elif isinstance(spec, dict) and spec.get("op") == "max":
            source = str(spec.get("field") or "")
            result[output_field] = max((as_float(context.get(source)) or 0 for context in contexts), default=0)
        elif isinstance(spec, dict) and spec.get("op") == "sample":
            source = str(spec.get("field") or "")
            limit = int(spec.get("limit") or 5)
            seen = []
            for context in contexts:
                value = context.get(source)
                if value not in (None, "") and value not in seen:
                    seen.append(value)
            result[output_field] = seen[:limit]
    return result


def _condition_matches(context: dict[str, Any], condition: dict[str, Any]) -> bool:
    field_value = context.get(str(condition.get("field") or ""))
    expected = condition.get("value")
    op = str(condition.get("op") or "=")
    left_num = as_float(field_value)
    right_num = as_float(expected)
    if op in {"=", "=="}:
        return field_value == expected or str(field_value).lower() == str(expected).lower()
    if op == "!=":
        return not _condition_matches(context, {**condition, "op": "="})
    if left_num is None or right_num is None:
        return False
    if op == "<":
        return left_num < right_num
    if op == "<=":
        return left_num <= right_num
    if op == ">":
        return left_num > right_num
    if op == ">=":
        return left_num >= right_num
    return False


def _unless_matches(context: dict[str, Any], conditions: list[Any]) -> bool:
    return any(isinstance(condition, dict) and _condition_matches(context, condition) for condition in conditions)


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    keys = row.keys() if hasattr(row, "keys") else []
    return {key: row[key] for key in keys}


def _get(row: Any, field: str) -> Any:
    try:
        return row[field]
    except (KeyError, IndexError, TypeError):
        return None


def _row_id(row: Any) -> str:
    return str(_get(row, "result_id") or id(row))


def _render_template(template: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        return str(context.get(match.group(1)) or "").upper()

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, template).upper()


def _number_bool_or_text(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    return _number_or_text(value)


def _number_or_text(value: Any) -> Any:
    if value in (None, ""):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def _format_value(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
