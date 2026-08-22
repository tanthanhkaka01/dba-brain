from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

from db_ops.metrics.models import MetricDefinition, MetricTarget
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module


DEFAULT_OVERRIDES_PATH = TOOL_ROOT / "data" / "metric_importance_overrides.json"
MATCH_FIELDS = ("ip", "db_type", "db_name", "service_name", "server_id", "target_id")


def load_metric_importance_overrides(
    path: str | Path = DEFAULT_OVERRIDES_PATH,
    *,
    known_metric_codes: set[str] | None = None,
) -> dict[str, Any]:
    overrides_path = Path(path)
    if not overrides_path.exists():
        return {"scale": _default_scale(), "instance_overrides": []}
    with overrides_path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise RuntimeError(f"metric_importance_overrides.json root must be an object: {overrides_path}")

    scale = data.get("scale") or {}
    if int(scale.get("min", -1)) != 0 or int(scale.get("max", -1)) != 5:
        raise RuntimeError("metric_importance_overrides.json scale must have min=0 and max=5.")

    for item in data.get("instance_overrides", []) or []:
        if not isinstance(item, dict):
            continue
        importance = item.get("importance") or {}
        if not isinstance(importance, dict):
            raise RuntimeError("instance_overrides[].importance must be an object keyed by metric_code.")
        for metric_code, value in importance.items():
            if known_metric_codes is not None and str(metric_code) not in known_metric_codes:
                warnings.warn(f"metric_importance_overrides references unknown metric_code: {metric_code}", RuntimeWarning)
            int_value = int(value)
            if not 0 <= int_value <= 5:
                raise RuntimeError(f"Override importance for {metric_code} must be from 0 to 5.")
    return data


def resolve_metric_importance(
    metric: MetricDefinition,
    target: MetricTarget,
    overrides: dict[str, Any] | None = None,
) -> int:
    data = overrides or load_metric_importance_overrides()
    for item in data.get("instance_overrides", []) or []:
        if not _match_target(item.get("match") or {}, target):
            continue
        importance = item.get("importance") or {}
        if metric.metric_code in importance:
            return int(importance[metric.metric_code])
    return int(metric.default_importance)


def should_run_metric(metric: MetricDefinition, target: MetricTarget, overrides: dict[str, Any] | None = None) -> bool:
    return resolve_metric_importance(metric, target, overrides) != 0


def _match_target(match: dict[str, Any], target: MetricTarget) -> bool:
    for field in MATCH_FIELDS:
        if field not in match:
            continue
        expected = str(match.get(field, "")).strip().lower()
        actual = str(getattr(target, field, "")).strip().lower()
        if expected != actual:
            return False
    return True


def _default_scale() -> dict[str, Any]:
    return {
        "min": 0,
        "max": 5,
        "labels": {
            "0": "disabled",
            "1": "informational",
            "2": "low",
            "3": "medium",
            "4": "high",
            "5": "critical",
        },
    }
