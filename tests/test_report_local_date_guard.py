"""The once-a-day guard behind a daily report, and the calendar it counts on.

"Already produced today" is a question about the *operator's* day, not about UTC: a report
generated at 07:30 local is still yesterday in UTC, so a UTC-day guard lets the same report out
twice. The window therefore runs from local midnight to local midnight, expressed as a range over
the stored UTC text.

It is also the one store method that used to answer differently on the two backends. It compared
with SQLite's `datetime(created_at, '+7 hours')`; PostgreSQL has no `datetime(text, text)`, the
dialect translator does not rewrite it, and the compatibility layer supplies only the two JSON
functions — so on 2026-09-04 the same call answered False on SQLite and raised
`42883 function datetime(text, unknown) does not exist` on the production PostgreSQL store. The
boundaries below are what a range comparison has to get right in exchange.

*Whose* day it is used to be a default argument on this method — seven hours, a Vietnam business
calendar decided in the store layer and applied to every operator of a published tool. The offset
now has no default and comes from the configured timezone, so a test that means +07 has to say so.
That is the point: the boundary cases below are only interesting relative to a stated offset.
"""

from datetime import datetime, timedelta, timezone

import pytest

from db_ops.db import DbOpsStore


#: The offset these boundary cases are written against. Named rather than repeated, because half
#: the assertions below turn on one second either side of it.
SEVEN_HOURS = 7 * 60


def _store_with_report(tmp_path, created_at, *, status="created", report_code="rp_daily"):
    # BACKUP_HEALTH because `reports.report_type` is a foreign key into the seeded `report_types`,
    # and this guard is about the day a report landed on, not about its type.
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    report_id = store.insert_report(
        report_code=report_code,
        report_name="Daily",
        report_type="BACKUP_HEALTH",
        report_level="logging",
        report_text="body",
        status=status,
    )
    with store.connect() as conn:
        conn.execute("UPDATE reports SET created_at = ? WHERE report_id = ?", (created_at, report_id))
    return store


def _exists(store, local_date, *, report_code="rp_daily", offset=SEVEN_HOURS):
    return store.report_exists_on_local_date(
        report_code=report_code, local_date=local_date, utc_offset_minutes=offset)


def test_the_last_second_of_the_local_day_still_counts_as_that_day(tmp_path):
    """16:59:59Z is 23:59:59 on the same day at +07 — inside the window, by one second."""
    store = _store_with_report(tmp_path, "2026-09-04T16:59:59Z")

    assert _exists(store, "2026-09-04")


def test_the_first_second_of_the_next_local_day_belongs_to_that_next_day(tmp_path):
    """17:00:00Z is 00:00:00 the next day at +07. Off by one here and the daily report either
    fires twice or never — the two failures this guard sits between."""
    store = _store_with_report(tmp_path, "2026-09-04T17:00:00Z")

    assert not _exists(store, "2026-09-04")
    assert _exists(store, "2026-09-05")


def test_the_window_moves_with_the_offset_it_is_given(tmp_path):
    """The same row, read on two clocks: at UTC it is the 4th, at +07 it is already the 5th."""
    store = _store_with_report(tmp_path, "2026-09-04T18:30:00Z")

    assert _exists(store, "2026-09-04", offset=0)
    assert _exists(store, "2026-09-05", offset=SEVEN_HOURS)


def test_an_offset_in_minutes_places_a_half_hour_zone_correctly(tmp_path):
    """Minutes, not hours, is why the parameter changed shape: +05:30 and +05:45 are real zones
    and an int of hours cannot say either. 18:35Z is 00:05 on the 5th in Kolkata and still the
    4th at +05:00 — an hours-only offset would have put this row on the wrong day."""
    store = _store_with_report(tmp_path, "2026-09-04T18:35:00Z")

    assert _exists(store, "2026-09-05", offset=330)
    assert _exists(store, "2026-09-04", offset=300)


def test_a_report_that_failed_to_generate_is_not_a_report_that_happened(tmp_path):
    """Counting it would silence the retry, which is the opposite of what the guard is for."""
    store = _store_with_report(tmp_path, "2026-09-04T02:00:00Z", status="failed")

    assert not _exists(store, "2026-09-04")


def test_a_pushed_report_counts_as_much_as_a_created_one(tmp_path):
    store = _store_with_report(tmp_path, "2026-09-04T02:00:00Z", status="pushed")

    assert _exists(store, "2026-09-04")


def test_another_report_code_on_the_same_day_is_not_this_one(tmp_path):
    store = _store_with_report(tmp_path, "2026-09-04T02:00:00Z", report_code="rp_other")

    assert not _exists(store, "2026-09-04")


def test_a_local_date_that_is_not_a_date_is_refused_rather_than_answered(tmp_path):
    """"No report today" is the answer that lets a duplicate out, so a caller that cannot say
    which day it means is told, not guessed at."""
    store = _store_with_report(tmp_path, "2026-09-04T02:00:00Z")

    with pytest.raises(ValueError):
        _exists(store, "04/09/2026")


def test_the_guard_answers_across_a_month_boundary(tmp_path):
    """The range is built with real date arithmetic, not by string surgery on the day number."""
    store = _store_with_report(tmp_path, "2026-08-31T17:30:00Z")

    assert _exists(store, "2026-09-01")


def test_todays_report_is_found_for_todays_local_date(tmp_path):
    """The way the caller actually asks: `local_now.date().isoformat()` for a row written now."""
    now = datetime.now(timezone.utc)
    store = _store_with_report(tmp_path, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    local_today = (now + timedelta(minutes=SEVEN_HOURS)).date().isoformat()

    assert _exists(store, local_today)
