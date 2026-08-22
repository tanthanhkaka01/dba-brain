from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

# Shared scheduling convention (single source of truth for all time_window users:
# daemon app_commands, sql_tasks, metrics, reports). ``repeat_interval == 0`` means
# "run once": due only when it has never run yet (and retried on failure / recovered
# when stale), NEVER repeated after a successful run.
RUN_ONCE = 0
# ``repeat_interval == -1`` means "manual": the scheduler never starts it, not even the first
# time. This is a *third* value rather than a reuse of RUN_ONCE because run-once still runs —
# `job_due` returns True for it while `last_run is None` — so 0 cannot express "only when a
# human asks". A manual entry is still `active`, so it stays listed and can be started by a
# forced run (`sql_tasks.runner run-sql-id --force`, i.e. /spbot_run_sql_task).
MANUAL_ONLY = -1
ERROR_STATUSES = {"error", "timeout", "fail", "failed", "failure"}


@dataclass(frozen=True)
class TimeWindow:
    from_year: int | None = None
    to_year: int | None = None
    from_month: int | None = None
    to_month: int | None = None
    from_day: int | None = None
    to_day: int | None = None
    from_hour: int | None = None
    to_hour: int | None = None
    from_minute: int | None = None
    to_minute: int | None = None
    repeat_interval: int | None = None
    retry_interval: int | None = None
    timeout: int | None = None


@dataclass(frozen=True)
class ParsedTimeWindow:
    time_window: TimeWindow
    warnings: tuple[str, ...] = ()


NEW_FIELDS = (
    "from_year",
    "to_year",
    "from_month",
    "to_month",
    "from_day",
    "to_day",
    "from_hour",
    "to_hour",
    "from_minute",
    "to_minute",
    "repeat_interval",
    "retry_interval",
    "timeout",
)

LEGACY_TIME_WINDOW_FIELDS = {
    "day_from": "from_day",
    "day_to": "to_day",
    "hour_from": "from_hour",
    "hour_to": "to_hour",
}

LEGACY_TOP_LEVEL_FIELDS = {
    "interval_second": "repeat_interval",
    "interval_seconds": "repeat_interval",
    "repeat_interval_seconds": "repeat_interval",
    "timeout_seconds": "timeout",
    "default_timeout": "timeout",
    "check_error_interval_seconds": "timeout",
    "allowed_from_day": "from_day",
    "allowed_to_day": "to_day",
    "allowed_from_hour": "from_hour",
    "allowed_to_hour": "to_hour",
}


def parse_time_window_config(
    item: dict[str, Any],
    *,
    context: str,
    defaults: dict[str, int | None] | None = None,
) -> ParsedTimeWindow:
    raw_time_window = item.get("time_window") or {}
    if raw_time_window in ("", None):
        raw_time_window = {}
    if not isinstance(raw_time_window, dict):
        raise RuntimeError(f"{context}.time_window must be an object.")

    defaults = defaults or {}
    values: dict[str, int | None] = {
        field: _optional_int(defaults.get(field), f"{context}.time_window.{field}.default") if field in defaults else None
        for field in NEW_FIELDS
    }
    warnings: list[str] = []

    for legacy_name, new_name in LEGACY_TOP_LEVEL_FIELDS.items():
        if legacy_name in item:
            warnings.append(f"{context}: deprecated field {legacy_name} used; use time_window.{new_name}.")
            values[new_name] = _optional_int(item.get(legacy_name), f"{context}.{legacy_name}")

    for legacy_name, new_name in LEGACY_TIME_WINDOW_FIELDS.items():
        if legacy_name in raw_time_window:
            warnings.append(f"{context}: deprecated field time_window.{legacy_name} used; use time_window.{new_name}.")
            values[new_name] = _optional_int(raw_time_window.get(legacy_name), f"{context}.time_window.{legacy_name}")

    for field in NEW_FIELDS:
        if field in raw_time_window:
            values[field] = _optional_int(raw_time_window.get(field), f"{context}.time_window.{field}")

    _validate_non_negative(values, "year", context)
    _validate_non_negative(values, "month", context)
    _validate_non_negative(values, "day", context)
    _validate_non_negative(values, "hour", context)
    _validate_non_negative(values, "minute", context)
    # 0 is allowed and meaningful: repeat_interval=0 => run-once, timeout=0 => no
    # timeout-kill, retry_interval=0 => retry immediately. Only negatives are invalid —
    # except repeat_interval=-1, which is the manual convention (see MANUAL_ONLY).
    _validate_repeat_interval(values, context)
    _validate_non_negative_scalar(values, "retry_interval", context)
    _validate_non_negative_scalar(values, "timeout", context)

    return ParsedTimeWindow(time_window=TimeWindow(**values), warnings=tuple(warnings))


def is_time_window_open(time_window: TimeWindow | None, current: datetime) -> bool:
    return not time_window_closed_reason(time_window, current)


def time_window_closed_reason(time_window: TimeWindow | None, current: datetime) -> str:
    """Name the first dimension that closes the window at ``current`` ("" = open).

    This is the only sanctioned way to evaluate or explain a time window — apps
    must not re-implement the from_*/to_* comparisons (they would drift on
    wrapping ranges and new fields).
    """
    if time_window is None:
        return ""
    checks = (
        ("year", current.year),
        ("month", current.month),
        ("day", current.day),
        ("hour", current.hour),
        ("minute", current.minute),
    )
    for name, value in checks:
        from_value = getattr(time_window, f"from_{name}")
        to_value = getattr(time_window, f"to_{name}")
        if from_value is not None and to_value is not None and from_value > to_value:
            # Wrapping range (e.g. from_hour=22, to_hour=6 spans midnight): the window is
            # open when the current value is >= from OR <= to. Without this, 22->6 would
            # never be open (22 <= h <= 6 is impossible).
            if not (value >= from_value or value <= to_value):
                return f"outside allowed {name} window: {value}"
        else:
            if from_value is not None and value < from_value:
                return f"outside allowed {name} window: {value}"
            if to_value is not None and value > to_value:
                return f"outside allowed {name} window: {value}"
    return ""


def _optional_int(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc


def _validate_non_negative(values: dict[str, int | None], name: str, context: str) -> None:
    for prefix in ("from", "to"):
        key = f"{prefix}_{name}"
        value = values.get(key)
        if value is not None and value < 0:
            raise RuntimeError(f"{context}.time_window.{key} must be >= 0: {value}")


def _validate_non_negative_scalar(values: dict[str, int | None], name: str, context: str) -> None:
    value = values.get(name)
    if value is not None and value < 0:
        raise RuntimeError(f"{context}.time_window.{name} must be >= 0: {value}")


def _validate_repeat_interval(values: dict[str, int | None], context: str) -> None:
    """``repeat_interval`` accepts one negative — -1 (MANUAL_ONLY). Everything below that is
    still a typo, and saying so names the two special values instead of just "must be >= 0"."""
    value = values.get("repeat_interval")
    if value is not None and value < 0 and value != MANUAL_ONLY:
        raise RuntimeError(
            f"{context}.time_window.repeat_interval must be >= 0, or {MANUAL_ONLY} for manual "
            f"(never scheduled; forced runs only): {value}"
        )


#: How much of an interval a run may be early, so scheduler drift does not cost a whole cycle.
#: Proportional, because 5 seconds of slack means something different to a 60-second metric and a
#: 20-hour one; capped, because a long interval does not need minutes of slack.
DUE_GRACE_FRACTION = 0.05
DUE_GRACE_MAX_SECONDS = 30


def _due_grace_seconds(interval: int) -> float:
    """Slack allowed on ``interval``. Never larger than the interval itself."""
    if interval <= 0:
        return 0.0
    return min(interval * DUE_GRACE_FRACTION, DUE_GRACE_MAX_SECONDS, float(interval))


def repeat_due(
    last_run: datetime | None,
    repeat_interval: int | None,
    now: datetime,
    *,
    default: int | None = None,
) -> bool:
    """Shared repeat-interval due check with the run-once convention.

    * ``repeat_interval == -1``  -> False always (MANUAL: never scheduled, not even once).
    * ``last_run is None``       -> True  (never ran yet).
    * ``repeat_interval == 0``   -> False (RUN-ONCE: already ran, never repeat).
    * ``repeat_interval is None``-> use ``default`` (None default -> never due again).
    * otherwise                  -> due when ``last_run + interval - grace <= now``.

    The **grace** is what keeps a declared interval achievable. Nothing runs continuously: the
    metric collector is itself started by the daemon on a sweep, so a metric is only ever tested
    for due-ness at sweep boundaries. Without slack, a 300-second metric checked 299.4 seconds
    after its last run is "not due" and waits for the *next* sweep — so a five-minute metric
    actually runs every ten. Measured on both audited targets: DATABASE_STATUS declared 300s ran
    at a median of 600s, LOCK_BLOCKING_SESSIONS declared 900s ran at 1,202s.

    The grace is proportional and small (:data:`DUE_GRACE_FRACTION`, capped by
    :data:`DUE_GRACE_MAX_SECONDS`), so it absorbs sweep drift without meaningfully shortening the
    interval: a metric can now be up to a few seconds early, never a whole cycle late.
    """
    # Checked before the last_run test on purpose: a manual entry that has never run must not
    # be due either, which is exactly where RUN_ONCE differs.
    if repeat_interval == MANUAL_ONLY:
        return False
    if last_run is None:
        return True
    if repeat_interval == RUN_ONCE:
        return False
    interval = repeat_interval if repeat_interval is not None else default
    if interval is None:
        return False
    return last_run + timedelta(seconds=interval - _due_grace_seconds(interval)) <= now


def job_due(
    *,
    last_run: datetime | None,
    last_status: str | None,
    repeat_interval: int | None,
    retry_interval: int | None,
    now: datetime,
    timeout: int | None = None,
    timeout_disabled: bool = False,
    default_repeat: int | None = None,
) -> bool:
    """Status-aware due check shared by the daemon and metrics collector.

    Builds on :func:`repeat_due` and adds retry-on-failure and stale-running recovery:

    * MANUAL (``repeat_interval == -1``) -> False, always. No first run, no retry-on-failure,
      no stale recovery: nothing the scheduler does starts a manual entry. (A stale ``running``
      row left by a forced run is still cleaned up — that is `mark_stale_running_sql_runs`,
      which does not go through this check.)
    * ``last_run is None`` -> True.
    * RUN-ONCE (``repeat_interval == 0``):
        - failed last run (status in :data:`ERROR_STATUSES`) -> retry after ``retry_interval``;
        - stale ``running`` (no in-memory tracking) -> recover after ``retry_interval`` when
          ``timeout_disabled`` else after ``timeout``;
        - otherwise (succeeded) -> never repeat.
    * Repeating (``repeat_interval`` > 0 / None): due when the interval elapsed, or on the
      same retry/stale rules above.
    """
    if repeat_interval == MANUAL_ONLY:
        return False
    if last_run is None:
        return True
    status = str(last_status or "").strip().lower()
    retry = retry_interval if retry_interval is not None else 60
    stale_grace = retry if timeout_disabled else (timeout if timeout is not None else 300)

    if repeat_interval == RUN_ONCE:
        if status in ERROR_STATUSES:
            return last_run + timedelta(seconds=retry) <= now
        if status == "running":
            return last_run + timedelta(seconds=stale_grace) <= now
        return False

    if repeat_due(last_run, repeat_interval, now, default=default_repeat):
        return True
    if status == "running":
        return last_run + timedelta(seconds=stale_grace) <= now
    if status in ERROR_STATUSES:
        return last_run + timedelta(seconds=retry) <= now
    return False
