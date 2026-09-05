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
"""

from datetime import datetime, timedelta, timezone

import pytest

from db_ops.db import DbOpsStore


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


def test_the_last_second_of_the_local_day_still_counts_as_that_day(tmp_path):
    """16:59:59Z is 23:59:59 on the same day at +07 — inside the window, by one second."""
    store = _store_with_report(tmp_path, "2026-09-04T16:59:59Z")

    assert store.report_exists_on_local_date(report_code="rp_daily", local_date="2026-09-04")


def test_the_first_second_of_the_next_local_day_belongs_to_that_next_day(tmp_path):
    """17:00:00Z is 00:00:00 the next day at +07. Off by one here and the daily report either
    fires twice or never — the two failures this guard sits between."""
    store = _store_with_report(tmp_path, "2026-09-04T17:00:00Z")

    assert not store.report_exists_on_local_date(report_code="rp_daily", local_date="2026-09-04")
    assert store.report_exists_on_local_date(report_code="rp_daily", local_date="2026-09-05")


def test_the_window_moves_with_the_offset_it_is_given(tmp_path):
    """The same row, read on two clocks: at UTC it is the 4th, at +07 it is already the 5th."""
    store = _store_with_report(tmp_path, "2026-09-04T18:30:00Z")

    assert store.report_exists_on_local_date(
        report_code="rp_daily", local_date="2026-09-04", utc_offset_hours=0)
    assert store.report_exists_on_local_date(
        report_code="rp_daily", local_date="2026-09-05", utc_offset_hours=7)


def test_a_report_that_failed_to_generate_is_not_a_report_that_happened(tmp_path):
    """Counting it would silence the retry, which is the opposite of what the guard is for."""
    store = _store_with_report(tmp_path, "2026-09-04T02:00:00Z", status="failed")

    assert not store.report_exists_on_local_date(report_code="rp_daily", local_date="2026-09-04")


def test_a_pushed_report_counts_as_much_as_a_created_one(tmp_path):
    store = _store_with_report(tmp_path, "2026-09-04T02:00:00Z", status="pushed")

    assert store.report_exists_on_local_date(report_code="rp_daily", local_date="2026-09-04")


def test_another_report_code_on_the_same_day_is_not_this_one(tmp_path):
    store = _store_with_report(tmp_path, "2026-09-04T02:00:00Z", report_code="rp_other")

    assert not store.report_exists_on_local_date(report_code="rp_daily", local_date="2026-09-04")


def test_a_local_date_that_is_not_a_date_is_refused_rather_than_answered(tmp_path):
    """"No report today" is the answer that lets a duplicate out, so a caller that cannot say
    which day it means is told, not guessed at."""
    store = _store_with_report(tmp_path, "2026-09-04T02:00:00Z")

    with pytest.raises(ValueError):
        store.report_exists_on_local_date(report_code="rp_daily", local_date="04/09/2026")


def test_the_guard_answers_across_a_month_boundary(tmp_path):
    """The range is built with real date arithmetic, not by string surgery on the day number."""
    store = _store_with_report(tmp_path, "2026-08-31T17:30:00Z")

    assert store.report_exists_on_local_date(report_code="rp_daily", local_date="2026-09-01")


def test_todays_report_is_found_for_todays_local_date(tmp_path):
    """The way the caller actually asks: `local_now.date().isoformat()` for a row written now."""
    now = datetime.now(timezone.utc)
    store = _store_with_report(tmp_path, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    local_today = (now + timedelta(hours=7)).date().isoformat()

    assert store.report_exists_on_local_date(report_code="rp_daily", local_date=local_today)
