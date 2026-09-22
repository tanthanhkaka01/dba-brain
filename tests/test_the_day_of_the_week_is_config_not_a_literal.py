"""Which days a thing may run on is configuration, and it is read on one clock.

Until 2026-09-21 ``TimeWindow`` had no day-of-week dimension, on purpose: expressing "Sunday"
would have meant adding a field to the object four apps share. So the two engine backup scripts
carried the weekday themselves, as a shell literal — and that is how one scheduled backup came to
make its two time decisions on two different clocks. The hour window was evaluated by the daemon on
the node's configured timezone; the FULL/INCR choice was made inside the script against
``date +%u`` on the container host, which is UTC. On 2026-09-19 those disagreed, and five
incrementals ran and failed on the night the weekly full was due.

``weekdays`` is that dimension. It is the only field of the fourteen that is a set rather than a
``from_``/``to_`` pair, because a pair cannot say "Monday and Thursday", and a wrapping pair over
seven values is exactly the hand-written comparison this module exists to stop apps writing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db_ops.common import config_admin
from db_ops.lib import time_window
from db_ops.lib.time_window import (
    INT_FIELDS, NEW_FIELDS, TimeWindow, WEEKDAYS_FIELD,
    parse_time_window_config, time_window_closed_reason)

PLUS_8 = timezone(timedelta(hours=8))
SUNDAY = datetime(2026, 9, 20, 2, 0, tzinfo=PLUS_8)
MONDAY = datetime(2026, 9, 21, 2, 0, tzinfo=PLUS_8)


def window(**fields) -> TimeWindow:
    return parse_time_window_config({"time_window": fields}, context="t").time_window


def test_a_window_that_says_nothing_about_weekdays_runs_on_any_day():
    """Absent means no restriction, the same as every other bound — so a config written before the
    field existed keeps the behaviour it had."""
    assert window(from_hour=1, to_hour=5).weekdays is None
    assert time_window_closed_reason(window(from_hour=1, to_hour=5), MONDAY) == ""


def test_sunday_is_seven_because_that_is_what_the_rest_of_the_estate_calls_it():
    """ISO, not cron. `isoweekday()`, `date +%u` and `DB_OPS_WEEKDAY` all say 7 for Sunday, and a
    second numbering would put back the disagreement this field was added to remove."""
    weekly = window(weekdays=[7], from_hour=1, to_hour=5)

    assert time_window_closed_reason(weekly, SUNDAY) == ""
    assert time_window_closed_reason(weekly, MONDAY) == "outside allowed weekday window: 1"


def test_a_set_can_name_days_that_are_not_next_to_each_other():
    """The reason it is a set: `from_`/`to_` could not express this at all."""
    assert time_window_closed_reason(window(weekdays=[1, 4]), MONDAY) == ""
    assert time_window_closed_reason(window(weekdays=[2, 4]), MONDAY) != ""


def test_an_empty_set_means_never_and_says_so_in_those_words():
    """`[]` is the field's own reading — only on the days in the set, and there are none — not a
    mistake. The wording matters: "outside allowed weekday window: 1" would read like a range
    problem and send its reader looking for a bound that was never written."""
    assert window(weekdays=[]).weekdays == ()
    assert time_window_closed_reason(window(weekdays=[]), MONDAY) == "no weekday is allowed"
    assert time_window_closed_reason(window(weekdays=[]), SUNDAY) == "no weekday is allowed"


def test_the_weekday_is_named_before_the_hour_when_both_are_closed():
    """A day that is not permitted excludes every hour in it, so naming the hour would send the
    reader to a field that is not the reason."""
    closed_on_both = window(weekdays=[7], from_hour=9, to_hour=10)

    assert time_window_closed_reason(closed_on_both, MONDAY) == "outside allowed weekday window: 1"


def test_zero_is_refused_rather_than_dropped_because_dropping_it_would_mean_never():
    """0 is the cron spelling of Sunday and somebody will write it. A parser that ignored what it
    did not recognise would turn `[0]` into `[]` — which now means *never runs* — and a weekly full
    would stop for good while its config still read like a schedule."""
    with pytest.raises(RuntimeError) as refused:
        window(weekdays=[0])

    assert "1-7" in str(refused.value)
    assert "cron" in str(refused.value)


def test_a_day_listed_twice_is_refused_because_it_is_a_set():
    with pytest.raises(RuntimeError) as refused:
        window(weekdays=[7, 7])

    assert "twice" in str(refused.value)


@pytest.mark.parametrize("bad", [8, -1, "monday", 1.5])
def test_anything_that_is_not_an_iso_weekday_is_refused(bad):
    with pytest.raises(RuntimeError):
        window(weekdays=[bad])


def test_a_weekday_set_that_is_not_an_array_is_refused():
    """A bare `7` is a plausible hand-edit, and accepting it would make the field mean two shapes."""
    with pytest.raises(RuntimeError):
        window(weekdays=7)
    with pytest.raises(RuntimeError):
        window(weekdays="7")


def test_true_is_not_monday():
    """`True == 1` in Python, and a checker that accepted it would read `[true]` as Monday."""
    with pytest.raises(RuntimeError):
        window(weekdays=[True])


def test_the_field_is_in_the_contract_but_not_in_the_integer_loops():
    """One list is the contract — what a config may carry and what the reference must describe —
    and the other is the subset that may be coerced with `int()`. Deriving the second from the
    first is what stops a field being added to one and forgotten in the other."""
    assert WEEKDAYS_FIELD in NEW_FIELDS
    assert WEEKDAYS_FIELD not in INT_FIELDS
    assert set(NEW_FIELDS) - set(INT_FIELDS) == {WEEKDAYS_FIELD}
    assert len(NEW_FIELDS) == 14


def test_the_registrar_reads_and_writes_the_same_weekday_set_the_scheduler_does():
    """`sql-target-add` and the runtime must not be able to disagree: a registrar that accepted a
    weekday set the scheduler refused would only be found when someone moved work between nodes,
    which is how `retry_interval` was lost once already."""
    written = config_admin.normalize_time_window({"weekdays": [7], "repeat_interval": 72000})

    assert written["weekdays"] == [7]
    assert config_admin.normalize_time_window(written) == written
    assert parse_time_window_config({"time_window": written}, context="t").time_window.weekdays == (7,)


def test_the_registrar_refuses_what_the_scheduler_refuses():
    with pytest.raises(config_admin.ConfigAdminError):
        config_admin.normalize_time_window({"weekdays": [0]})
    with pytest.raises(config_admin.ConfigAdminError):
        config_admin.normalize_time_window({"weekdays": [3, 3]})


def test_an_unset_weekday_set_is_not_the_same_as_an_empty_one():
    """The distinction the whole field rests on, and the one a `None`/`[]` conflation would lose."""
    assert config_admin.normalize_time_window({})["weekdays"] is None
    assert config_admin.normalize_time_window({"weekdays": []})["weekdays"] == []
    assert window().weekdays is None
    assert window(weekdays=[]).weekdays == ()


def test_the_module_still_refuses_to_let_an_app_write_its_own_comparison():
    """`is_time_window_open` is the boolean face of the one evaluator, so the weekday reaches every
    app — the daemon, metrics, sql_tasks, reports and backup_restore — through one `if`."""
    assert time_window.is_time_window_open(window(weekdays=[7]), SUNDAY) is True
    assert time_window.is_time_window_open(window(weekdays=[7]), MONDAY) is False
