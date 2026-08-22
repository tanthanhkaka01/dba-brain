from __future__ import annotations

from typing import Any


def is_target_enabled(target: Any) -> bool:
    return _flag(target, "enabled", default=True)


def is_metrics_enabled(target: Any) -> bool:
    return is_target_enabled(target) and _nested_enabled(target, "metrics", default=is_target_enabled(target))


def is_reports_enabled(target: Any) -> bool:
    return is_target_enabled(target) and _nested_enabled(target, "reports", default=is_metrics_enabled(target))


def is_alerts_enabled(target: Any) -> bool:
    return is_target_enabled(target) and _nested_enabled(target, "alerts", default=is_reports_enabled(target))


def _nested_enabled(target: Any, key: str, *, default: bool) -> bool:
    value = _get(target, key, None)
    if value is None:
        return bool(default)
    if isinstance(value, dict):
        return bool(value.get("enabled", default))
    return bool(value)


def _flag(target: Any, key: str, *, default: bool) -> bool:
    value = _get(target, key, default)
    return bool(value)


def _get(target: Any, key: str, default: Any) -> Any:
    if isinstance(target, dict):
        return target.get(key, default)
    return getattr(target, key, default)
