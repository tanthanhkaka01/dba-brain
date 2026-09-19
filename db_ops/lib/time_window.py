"""When a unit of work is allowed to run, and whether it is due.

The one scheduling convention four apps share — the app-command daemon, sql_tasks, metrics and
reports — so that ``from_hour: 1`` means the same thing in ``app_commands.json`` as it does in
``sql_targets.json``. No app may parse, evaluate or explain a time window with comparisons of its
own; they would drift on wrapping ranges and on any field added later.

**Which clock the bounds are on is the caller's to supply, and there is exactly one right answer:**
``db_ops.lib.timezone.display_now()`` — the timezone DBA Brain is configured to run in
(``config.json`` -> ``timezone``). Every ``from_*``/``to_*`` here is a wall-clock comparison, so a
caller that passes ``datetime.now()`` or ``datetime.now(timezone.utc)`` silently reinterprets every
schedule in the estate.

That is not hypothetical. Until 2026-09-07 every caller passed ``datetime.now().astimezone()`` —
the host's clock — which held together only because ``docker-compose.yml`` pinned
``TZ: Asia/Ho_Chi_Minh`` into the worker container. On the published image, without that line, the
container is UTC and a window written for the small hours ran during the operator's working day.

This module stays pure and takes ``current`` as an argument rather than reading the configured zone
itself: :mod:`db_ops.lib` imports nothing from ``db_ops``, and a scheduling rule that reads global
state is one a test cannot pin to a moment.

Elapsed-time arithmetic (``repeat_interval``, retry, stale-running recovery) is a different thing
and is done in UTC by every caller — subtracting two instants is offset-safe, and the stored
timestamps it compares against are UTC. Only the window open-check reads a wall clock.

**Every interval here is measured from the previous run's START, and that is not the caller's
choice.** ``repeat_interval: 300`` means "start 300 seconds after the last start", so a task that
runs for 240 seconds is due again about 60 seconds after it finishes — 45, in fact, because of the
grace below — on every app, for every object that carries a ``time_window``. The alternative (measure from the finish) makes the declared
number unreadable: the same ``300`` would mean a 5-minute cycle for a fast task and a 9-minute one
for a slow one, and nothing in the config would say which. It also drifts, because each run's
duration pushes the next one further out.

That was not true until 2026-09-19. ``sql_tasks`` measured from ``finished_at`` and the reports app
from the moment the last send completed, so one estate ran three different meanings of the same
field. What each app passes as ``last_run`` is listed in ``docs/configuration.md`` §5, and
``tests/test_repeat_interval_is_measured_from_start.py`` holds them to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from db_ops.lib.coerce import as_utc_datetime

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
    """The thirteen fields, in two groups that answer two different questions.

    **The ten bounds — "may it run at this moment?"** Five dimensions, each a ``from_``/``to_``
    pair, read off ``current`` as wall-clock values in the node's configured timezone. Every bound
    is **inclusive**, every one is **optional**, and ``null`` means *no restriction on that
    dimension* — which is why an absent object schedules a thing to run at any hour of any day.
    A pair whose ``from_`` is greater than its ``to_`` **wraps** (``from_hour: 22, to_hour: 6``
    spans midnight); that is the one case a hand-written comparison always gets wrong, and the
    reason no app may write its own. All ten are validated ``>= 0``; a value above its dimension's
    natural ceiling is not rejected but can never match, so ``to_hour: 25`` is a window that is
    open all day and ``from_month: 13`` is one that never opens.

    **The three intervals — "is it due, and how long may it take?"** Seconds, always, and every one
    of them measured from the previous run's **start** (see the module docstring). ``null`` means
    "the caller's default", which differs per app, so an interval that matters is stated rather
    than left out.

    Required: **none of the thirteen**. The object itself is optional everywhere, and each field
    falls back to a documented default. That is deliberate — a config that omits the block still
    runs — but it means an omission cannot be told from a decision, so the shipped catalogues state
    ``repeat_interval`` and ``timeout`` explicitly on everything they schedule.
    """

    #: Calendar year, inclusive. Practically only ever used to retire a one-off: ``to_year: 2026``
    #: stops a schedule at the end of that year without deleting the record that explains it.
    from_year: int | None = None
    to_year: int | None = None
    #: Month 1-12, inclusive, wrapping (``from_month: 11, to_month: 2`` is the winter).
    from_month: int | None = None
    to_month: int | None = None
    #: Day of the **month** 1-31, inclusive, wrapping. Not a weekday: there is no weekday
    #: dimension, and a "weekends only" schedule cannot be expressed here.
    from_day: int | None = None
    to_day: int | None = None
    #: Hour 0-23, inclusive, wrapping. The field the estate actually schedules by, and the one
    #: whose meaning moves with ``config.json`` -> ``timezone``: 21 metrics share
    #: ``from_hour: 1, to_hour: 6`` so heavy work lands overnight, which it does only if the node's
    #: clock is the operator's. ``to_hour: 23`` is the idiom for "all day" — ``0`` would mean
    #: midnight alone, which is how a schedule ends up firing once a day by accident.
    from_hour: int | None = None
    to_hour: int | None = None
    #: Minute 0-59, inclusive, wrapping. Narrows an hour window; it does **not** pin a run to a
    #: minute, because due-ness is still ``repeat_interval`` elapsed and the scheduler only looks
    #: on its own sweep. "Every day at 02:30" is ``from_hour/to_hour: 2`` plus
    #: ``from_minute: 30, to_minute: 59`` plus an interval longer than the window.
    from_minute: int | None = None
    to_minute: int | None = None
    #: Seconds between **starts** after a run that did not fail. Three values are special:
    #: ``0`` = run once and never again (:data:`RUN_ONCE` — how a service that stays up is
    #: expressed), ``-1`` = manual only (:data:`MANUAL_ONLY`, never scheduled, forced runs only),
    #: ``null`` = the caller's default. Anything below ``-1`` is a typo and is refused. A run may
    #: start up to :func:`_due_grace_seconds` early so a sweep boundary does not cost a whole
    #: cycle — 30 s on a 1800 s interval, which is why 1800 is observed as ~1770.
    repeat_interval: int | None = None
    #: Seconds between **starts** after a run that ended in :data:`ERROR_STATUSES`, again from the
    #: failed run's start. It exists so a broken thing backs off instead of hammering a target
    #: every sweep. ``null`` means the caller's default, which is *not* the same number
    #: everywhere: the daemon falls back to 60 s, sql_tasks and backup_restore to
    #: ``repeat_interval`` (a failed task is never retried faster than its normal schedule unless
    #: it says so), metrics to 600 s. ``0`` = retry on the next sweep.
    retry_interval: int | None = None
    #: Seconds a run may take before it is killed, measured from its start. It is also the grace
    #: on a ``running`` row: a row still open after ``timeout`` is a corpse the next sweep reaps
    #: instead of starting a second copy. ``0`` = never killed (only for something meant to stay
    #: up), ``null`` = the caller's default. Set it above the **slowest** thing inside the command,
    #: not its average — a collection pass killed at its timeout loses every metric in it, not
    #: only the slow one — and below ``repeat_interval``, or the reaper and the scheduler disagree
    #: about what a cycle is.
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

    ``current`` must be :func:`db_ops.lib.timezone.display_now` — the configured
    display timezone. Its ``.hour`` and ``.day`` are read directly, so whatever
    zone it carries *is* the meaning of every bound; see this module's docstring.
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


@dataclass(frozen=True)
class DueVerdict:
    """Whether a unit of work runs now, and the one sentence that says why not.

    The verdict and its explanation are produced together on purpose. They used to be two
    computations — the rule in :func:`job_due` and a message built beside each caller — and they
    disagreed: the daemon printed ``last_run + timeout`` as the next attempt for a ``running`` row
    while the rule was still reading the interval, which is the disagreement that let a second
    restore start 47 minutes into the first (2026-09-14).
    """

    due: bool
    #: Empty when ``due``. Otherwise the first thing that blocked it, in the reader's terms:
    #: ``outside allowed hour window: 14``, ``interval not elapsed: 120s/300s``,
    #: ``running within timeout: 40s/600s``, ``retry not elapsed: 30s/600s``,
    #: ``run-once: already ran``, ``manual only: never scheduled``.
    reason: str = ""
    #: The instant the intervals were measured from — the previous run's start, or ``None`` when
    #: nothing has run yet. Returned so a caller can print it without going back to the row.
    last_run: datetime | None = None
    #: When the next attempt becomes due, for a verdict that is not due *yet*. ``None`` when the
    #: answer is not a clock reading: it is due now, it never repeats, it is manual, or a closed
    #: wall-clock window is what is holding it (the window reopens on the calendar, not after an
    #: interval). It is computed by the branch that made the decision — the daemon used to derive
    #: it beside the rule and the two disagreed.
    next_due_at: datetime | None = None


def explain_due(
    *,
    last_run: datetime | None,
    last_status: str | None,
    repeat_interval: int | None,
    retry_interval: int | None,
    now: datetime,
    timeout: int | None = None,
    timeout_disabled: bool = False,
    default_repeat: int | None = None,
    running_blocks: bool = True,
) -> DueVerdict:
    """The scheduling rule itself: the verdict **and** its reason, in one pass.

    :func:`job_due` is this function's boolean face and :func:`due_from_row` is the one that takes
    a store row. Nothing outside this module re-implements what is here.

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

    ``running_blocks=False`` is for a caller that has said **its own run in flight is not a reason
    to wait** — an app command declared ``run_mode: async``. The interval still applies, measured
    from that run's start as always; what is skipped is the "wait for it to finish" branch. It is
    only safe for a caller whose *work* is claimed somewhere else, which is why it is off by
    default and why the daemon passes it for async commands alone: on 2026-09-19 a scan that took
    three minutes made every async command wait exactly as a sync one did, and the concurrency the
    field promised never happened.
    """
    if repeat_interval == MANUAL_ONLY:
        return DueVerdict(False, "manual only: never scheduled", last_run)
    if last_run is None:
        return DueVerdict(True, "", None)
    status = str(last_status or "").strip().lower()
    retry = retry_interval if retry_interval is not None else 60
    stale_grace = retry if timeout_disabled else (timeout if timeout is not None else 300)
    elapsed = int((now - last_run).total_seconds())

    if repeat_interval == RUN_ONCE:
        if status in ERROR_STATUSES:
            return _elapsed_verdict(last_run, elapsed, retry, now, "retry not elapsed")
        if status == "running":
            return _elapsed_verdict(last_run, elapsed, stale_grace, now, "running within timeout")
        return DueVerdict(False, "run-once: already ran", last_run)

    # `running` is checked BEFORE the interval, not after. The other order made the running check
    # unreachable for every repeating command whose run outlives its own repeat interval: the
    # interval elapses while the job is still working, `repeat_due` returns True, and a second copy
    # starts. In-process the daemon catches that with `running_commands`, so it only shows when a
    # daemon starts while a job is in flight - measured 2026-09-14, where a restart began a second
    # `backup_restore workflow` 47 minutes into a restore of the same database onto the same
    # target, having logged `startup.running_within_timeout` about that very row one second before.
    # The daemon's own not-due diagnostic already printed `last_run + timeout` as the next attempt
    # for a running row, so the message and the rule had disagreed since the rule was written.
    if status == "running" and running_blocks:
        return _elapsed_verdict(last_run, elapsed, stale_grace, now, "running within timeout")
    if repeat_due(last_run, repeat_interval, now, default=default_repeat):
        return DueVerdict(True, "", last_run)
    if status in ERROR_STATUSES:
        return _elapsed_verdict(last_run, elapsed, retry, now, "retry not elapsed")
    interval = repeat_interval if repeat_interval is not None else default_repeat
    if interval is None:
        return DueVerdict(False, "no repeat_interval and no default: never due again", last_run)
    next_due_at = last_run + timedelta(seconds=int(interval) - _due_grace_seconds(int(interval)))
    return DueVerdict(False, f"interval not elapsed: {elapsed}s/{int(interval)}s",
                      last_run, next_due_at)


def _elapsed_verdict(last_run: datetime, elapsed: int, allowed: int,
                     now: datetime, label: str) -> DueVerdict:
    """Due once ``allowed`` seconds have passed since ``last_run``, said in one shape.

    No grace here, unlike :func:`repeat_due`: a retry and a stale-row reap are recoveries from
    something that already went wrong, and being a few seconds early to either is not worth the
    sentence it would take to explain in a log.
    """
    next_due_at = last_run + timedelta(seconds=allowed)
    if next_due_at <= now:
        return DueVerdict(True, "", last_run)
    return DueVerdict(False, f"{label}: {elapsed}s/{int(allowed)}s", last_run, next_due_at)


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
    """:func:`explain_due` without the explanation, kept because a due check reads better as a
    boolean wherever nothing prints the reason."""
    return explain_due(
        last_run=last_run, last_status=last_status, repeat_interval=repeat_interval,
        retry_interval=retry_interval, now=now, timeout=timeout,
        timeout_disabled=timeout_disabled, default_repeat=default_repeat,
    ).due


#: The columns a run's **start** may be spelled in, most trusted first. One tuple, because four
#: apps had four accessors and two of them read a different column — ``sql_runs.finished_at`` and
#: the reports app's last-send instant — so ``repeat_interval: 300`` meant three different cycles
#: in one estate (fixed 2026-09-19). ``created_at`` is the fallback for a row inserted before it
#: started; ``finished_at`` the last resort for a row that somehow carries only that.
RUN_ANCHOR_COLUMNS: tuple[str, ...] = ("started_at", "created_at", "finished_at")


def run_anchor(row: Any | None) -> datetime | None:
    """The instant every interval is measured from, read off one store row.

    Takes anything mapping-like — ``sqlite3.Row``, the PostgreSQL backend's ``Row``, a plain dict —
    because ``lib`` may not import the store, and a row is the one shape all four schedulers
    already hold.
    """
    if row is None:
        return None
    for column in RUN_ANCHOR_COLUMNS:
        try:
            value = row[column]
        except (KeyError, IndexError, TypeError):
            continue
        parsed = as_utc_datetime(value)
        if parsed is not None:
            return parsed
    return None


def due_from_row(
    *,
    time_window: TimeWindow | None,
    row: Any | None,
    now: datetime,
    local_now: datetime | None = None,
    retry_default: int | None = None,
    timeout_default: int | None = None,
    default_repeat: int | None = None,
    running_blocks: bool = True,
) -> DueVerdict:
    """**The shared entry point: one row, one clock, one verdict.**

    What every app needs and what none of them may write again: read the previous run's start off
    the row (:func:`run_anchor`), check the wall-clock window when the caller supplies
    ``local_now``, then apply the rule (:func:`explain_due`). The three ``*_default`` arguments are
    the only per-app freedom left, and they exist because the apps genuinely differ: the daemon
    retries a failure after 60 s, metrics after 600, sql_tasks and backup_restore after the repeat
    interval itself. Which *column* the anchor is read from is no longer one of those freedoms.

    ``local_now`` must be :func:`db_ops.lib.timezone.display_now` — see the module docstring. Left
    out, the window is not checked at all, which is what a caller that has already checked it (or
    has no window) wants.
    """
    if local_now is not None:
        closed = time_window_closed_reason(time_window, local_now)
        if closed:
            return DueVerdict(False, closed, run_anchor(row))
    window = time_window or TimeWindow()
    retry = window.retry_interval if window.retry_interval is not None else retry_default
    timeout = window.timeout if window.timeout is not None else timeout_default
    return explain_due(
        last_run=run_anchor(row),
        last_status=_row_status(row),
        repeat_interval=window.repeat_interval,
        retry_interval=retry,
        now=now,
        timeout=timeout,
        timeout_disabled=window.timeout == 0,
        default_repeat=default_repeat,
        running_blocks=running_blocks,
    )


def _row_status(row: Any | None) -> str:
    if row is None:
        return ""
    try:
        return str(row["status"] or "").strip().lower()
    except (KeyError, IndexError, TypeError):
        return ""
