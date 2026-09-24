from __future__ import annotations

from typing import Any

from db_ops.lib import field_names


def is_target_enabled(target: Any) -> bool:
    # `active` is the standard name for a record's switch (0.22.0 section 1.0); `enabled` is what
    # db_instances.json still says until every node reads both. A dict is asked for either.
    if isinstance(target, dict):
        return bool(field_names.read(target, "db_instance", "active", True))
    return _flag(target, "enabled", default=True)


def is_record_active(record: Any) -> bool:
    """A Telegram group or user is switched on: ``active: true``, or the older ``status: "active"``.

    Absent means active, as everywhere in this tree. The four readers of the older field disagreed
    about that: the permission checks read a record with no ``status`` as active, the level routing
    as inactive - so one hand-written group could be allowed to run commands and never be sent an
    alert. One rule now, for both spellings.
    """
    if not isinstance(record, dict):
        return False
    if "active" in record:
        value = record["active"]
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "active"}
        return bool(value)
    if "status" in record:
        return str(record["status"]).strip().lower() == "active"
    return True


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
