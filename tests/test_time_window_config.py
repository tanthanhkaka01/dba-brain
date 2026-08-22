from datetime import datetime, timedelta, timezone

import pytest

from db_ops.lib.time_window import (
    MANUAL_ONLY,
    RUN_ONCE,
    is_time_window_open,
    job_due,
    parse_time_window_config,
    repeat_due,
)


def test_new_time_window_config_parses_schedule_and_timeout():
    parsed = parse_time_window_config(
        {
            "time_window": {
                "from_year": 2026,
                "to_year": 2026,
                "from_month": 6,
                "to_month": 6,
                "from_day": 7,
                "to_day": 7,
                "from_hour": 19,
                "to_hour": 19,
                "from_minute": 30,
                "to_minute": 45,
                "repeat_interval": 300,
                "retry_interval": 60,
                "timeout": 600,
            }
        },
        context="test.item",
    )

    assert parsed.warnings == ()
    assert parsed.time_window.repeat_interval == 300
    assert parsed.time_window.retry_interval == 60
    assert parsed.time_window.timeout == 600
    assert is_time_window_open(parsed.time_window, datetime(2026, 6, 7, 19, 30))


def test_old_time_window_config_is_supported_with_warnings():
    parsed = parse_time_window_config(
        {
            "interval_second": 3600,
            "timeout_seconds": 300,
            "time_window": {"day_from": 7, "day_to": 7, "hour_from": 19, "hour_to": 19},
        },
        context="test.legacy",
    )

    assert parsed.time_window.repeat_interval == 3600
    assert parsed.time_window.timeout == 300
    assert parsed.time_window.from_day == 7
    assert parsed.time_window.to_hour == 19
    assert len(parsed.warnings) == 6


def test_new_time_window_fields_take_priority_over_old_fields():
    parsed = parse_time_window_config(
        {
            "repeat_interval_seconds": 60,
            "timeout_seconds": 60,
            "time_window": {
                "day_from": 1,
                "from_day": 7,
                "repeat_interval": 300,
                "timeout": 900,
            },
        },
        context="test.conflict",
    )

    assert parsed.time_window.from_day == 7
    assert parsed.time_window.repeat_interval == 300
    assert parsed.time_window.timeout == 900


def test_time_window_bounds_allow_loose_positive_values():
    parsed = parse_time_window_config(
        {
            "time_window": {
                "from_year": 2026,
                "to_year": None,
                "from_month": 6,
                "to_month": 100,
                "from_day": 7,
                "to_day": 200,
                "from_hour": 19,
                "to_hour": 100,
                "from_minute": 45,
                "to_minute": 200,
                "repeat_interval": 3600,
                "timeout": 1800,
            }
        },
        context="test.loose",
    )

    assert parsed.time_window.to_month == 100
    assert parsed.time_window.to_day == 200
    assert parsed.time_window.to_hour == 100
    assert parsed.time_window.to_minute == 200


def test_time_window_bounds_allow_from_greater_than_to():
    parsed = parse_time_window_config(
        {"time_window": {"from_day": 7, "to_day": 1, "from_hour": 19, "to_hour": 13}},
        context="test.loose",
    )

    assert parsed.time_window.from_day == 7
    assert parsed.time_window.to_day == 1
    assert parsed.time_window.from_hour == 19
    assert parsed.time_window.to_hour == 13


def test_time_window_checks_only_from_when_to_is_missing():
    parsed = parse_time_window_config({"time_window": {"from_hour": 13}}, context="test.from_only")

    assert not is_time_window_open(parsed.time_window, datetime(2026, 6, 7, 12, 59))
    assert is_time_window_open(parsed.time_window, datetime(2026, 6, 7, 13, 0))
    assert is_time_window_open(parsed.time_window, datetime(2026, 6, 7, 23, 59))


def test_time_window_checks_only_to_when_from_is_missing():
    parsed = parse_time_window_config({"time_window": {"to_hour": 13}}, context="test.to_only")

    assert is_time_window_open(parsed.time_window, datetime(2026, 6, 7, 0, 0))
    assert is_time_window_open(parsed.time_window, datetime(2026, 6, 7, 13, 59))
    assert not is_time_window_open(parsed.time_window, datetime(2026, 6, 7, 14, 0))


@pytest.mark.parametrize(
    "field",
    [
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
    ],
)
def test_time_window_bounds_must_not_be_negative(field):
    with pytest.raises(RuntimeError, match=f"{field} must be >= 0"):
        parse_time_window_config({"time_window": {field: -1}}, context="test.invalid")


@pytest.mark.parametrize("field", ["retry_interval", "timeout"])
def test_intervals_and_timeout_must_not_be_negative(field):
    with pytest.raises(RuntimeError, match=f"{field} must be >= 0"):
        parse_time_window_config({"time_window": {field: -1}}, context="test.invalid")


def test_repeat_interval_accepts_minus_one_as_manual_but_nothing_lower():
    """-1 is the manual convention (never scheduled). It has to be accepted where every other
    negative is rejected — and only -1, so a typo like -300 still fails loudly instead of
    silently turning a five-minute task into one that never runs."""
    parsed = parse_time_window_config(
        {"time_window": {"repeat_interval": MANUAL_ONLY}}, context="test.manual")
    assert parsed.time_window.repeat_interval == MANUAL_ONLY

    with pytest.raises(RuntimeError, match="repeat_interval must be >= 0, or -1 for manual"):
        parse_time_window_config({"time_window": {"repeat_interval": -300}}, context="test.typo")


def test_a_manual_interval_is_never_due_not_even_the_first_time():
    """This is what separates manual from run-once. RUN_ONCE (0) still runs: job_due returns
    True while it has never run. A manual entry must return False even then, and must not be
    revived by the retry-on-error or stale-running paths either — nothing the scheduler does
    starts it."""
    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    long_ago = now - timedelta(days=30)

    assert repeat_due(None, MANUAL_ONLY, now) is False
    assert repeat_due(long_ago, MANUAL_ONLY, now) is False
    for status in (None, "done", "error", "running"):
        assert job_due(last_run=long_ago, last_status=status, repeat_interval=MANUAL_ONLY,
                       retry_interval=1, now=now, timeout=1) is False
    assert job_due(last_run=None, last_status=None, repeat_interval=MANUAL_ONLY,
                   retry_interval=1, now=now) is False

    # Contrast: run-once with no run yet IS due — which is why 0 cannot mean "manual".
    assert job_due(last_run=None, last_status=None, repeat_interval=RUN_ONCE,
                   retry_interval=1, now=now) is True


@pytest.mark.parametrize("field", ["repeat_interval", "retry_interval", "timeout"])
def test_intervals_and_timeout_allow_zero(field):
    # 0 is meaningful: repeat_interval=0 => run-once, timeout=0 => no kill, retry_interval=0 => retry now.
    parsed = parse_time_window_config({"time_window": {field: 0}}, context="test.zero")
    assert getattr(parsed.time_window, field) == 0


def test_time_window_hour_wraps_across_midnight():
    """A night window 22:00->06:00 spans midnight: open late night / early morning, closed
    during the day. Regression guard for the wrap-around support."""
    from db_ops.lib.time_window import TimeWindow

    tw = parse_time_window_config(
        {"time_window": {"from_hour": 22, "to_hour": 6}}, context="test.night"
    ).time_window
    assert isinstance(tw, TimeWindow)
    for hour in (22, 23, 0, 3, 6):
        assert is_time_window_open(tw, datetime(2026, 7, 17, hour, 0)), f"expected open at {hour}"
    for hour in (7, 12, 18, 21):
        assert not is_time_window_open(tw, datetime(2026, 7, 17, hour, 0)), f"expected closed at {hour}"


def test_time_window_closed_reason_names_the_failing_dimension():
    """time_window_closed_reason is the shared skip-reason text: "" when open, and the
    first closed dimension otherwise — including on wrapping ranges."""
    from db_ops.lib.time_window import TimeWindow, time_window_closed_reason

    plain = TimeWindow(from_hour=10, to_hour=11)
    assert time_window_closed_reason(plain, datetime(2026, 7, 17, 10, 30)) == ""
    assert time_window_closed_reason(plain, datetime(2026, 7, 17, 9, 0)) == "outside allowed hour window: 9"
    assert time_window_closed_reason(None, datetime(2026, 7, 17, 9, 0)) == ""

    night = TimeWindow(from_hour=22, to_hour=6)
    assert time_window_closed_reason(night, datetime(2026, 7, 17, 23, 0)) == ""
    assert time_window_closed_reason(night, datetime(2026, 7, 17, 3, 0)) == ""
    assert time_window_closed_reason(night, datetime(2026, 7, 17, 12, 0)) == "outside allowed hour window: 12"

    days = TimeWindow(from_day=1, to_day=15, from_hour=10, to_hour=11)
    assert time_window_closed_reason(days, datetime(2026, 7, 17, 10, 30)) == "outside allowed day window: 17"


def test_time_window_wrap_applies_to_every_dimension():
    """from > to wraps on month, day, and minute exactly like the hour example
    (22->06): open when value >= from OR value <= to."""
    from db_ops.lib.time_window import TimeWindow

    nov_to_feb = TimeWindow(from_month=11, to_month=2)
    for month in (11, 12, 1, 2):
        assert is_time_window_open(nov_to_feb, datetime(2026, month, 17, 12, 0)), f"expected open in month {month}"
    for month in (3, 7, 10):
        assert not is_time_window_open(nov_to_feb, datetime(2026, month, 17, 12, 0)), f"expected closed in month {month}"

    day_25_to_5 = TimeWindow(from_day=25, to_day=5)
    for day in (25, 31, 1, 5):
        assert is_time_window_open(day_25_to_5, datetime(2026, 7, day, 12, 0)), f"expected open on day {day}"
    for day in (6, 15, 24):
        assert not is_time_window_open(day_25_to_5, datetime(2026, 7, day, 12, 0)), f"expected closed on day {day}"

    minute_50_to_10 = TimeWindow(from_minute=50, to_minute=10)
    for minute in (50, 59, 0, 10):
        assert is_time_window_open(minute_50_to_10, datetime(2026, 7, 17, 12, minute)), f"expected open at minute {minute}"
    for minute in (11, 30, 49):
        assert not is_time_window_open(minute_50_to_10, datetime(2026, 7, 17, 12, minute)), f"expected closed at minute {minute}"


def test_metric_not_collected_outside_its_schedule_window():
    """The metrics collector honors a per-metric schedule_window: a metric restricted to a
    22:00-06:00 window is not collected at midday even when it has never run.

    Checked through _metric_window_open, not _metric_due: the window is a statement about when
    the metric may touch the database at all, and keeping it inside the due check made it
    collateral damage of --force. See tests/test_metric_windows.py."""
    from db_ops.lib.time_window import TimeWindow
    from db_ops.metrics.collector import _metric_window_open
    from db_ops.metrics.models import MetricDefinition, MetricTarget

    class _Store:

        @classmethod
        def from_config(cls, config, **kwargs):
            """Store doubles must offer the same constructor contract as the real classes."""
            return cls(getattr(config, 'sqlite_path', None))
        def latest_result_time(self, **_):
            return None  # never collected

        def latest_successful_result_time(self, **_):
            return None

    metric = MetricDefinition(
        metric_code="MAINTENANCE_INDEX_FRAGMENTATION",
        db_type="sqlserver",
        category="maintenance",
        default_importance=1,
        active=True,
        interval_seconds=86400,
        schedule_window=TimeWindow(from_hour=22, to_hour=6),
    )
    target = MetricTarget(
        target_id="t1", server_id="s1", ip="10.0.0.1", db_type="sqlserver", db_name="master",
        credential_name="c", port=1433, service_name="svc", instance_name="i",
        connection_info={}, credential={"username": "u", "password_ref": "P"},
    )
    # Naive datetime -> astimezone() keeps the wall-clock hour, so this is tz-independent.
    assert not _metric_window_open(metric=metric, now=datetime(2026, 7, 17, 12, 0))
    assert _metric_window_open(metric=metric, now=datetime(2026, 7, 17, 23, 0))
