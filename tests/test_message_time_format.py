"""The wall-clock time an operator reads off a message.

A message that names no time reads as "this is happening now", which a reaped SQL run never is:
an abandoned row is only revisited on the next scan, so the alert can be written hours after the
run it is about died. The offset is printed every time because every timestamp the tool *stores*
is UTC — a bare local time cannot be compared with `sql_runs.started_at` without knowing which
clock it came from, and the reader has no way to ask.

`format_message_time` used to resolve its own offset from an env var and render `UTC+07:00`, which
made two spellings of one value — the drift `db_ops.lib.text_format`'s own docstring is about. It
is now one call deep into `db_ops.lib.timezone`, so a Telegram alert and the report header about
the same failure are stamped identically. These tests are what holds it there: the assertion that
matters is not the string, it is that the string is the *same* string.
"""

from datetime import datetime, timezone

import pytest

from db_ops.lib import timezone as tz
from db_ops.lib.text_format import MESSAGE_UTC_OFFSET_ENV, format_message_time, format_utc


MOMENT = datetime(2026, 9, 4, 6, 13, 11, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _restore_zone():
    before = tz.display_declaration()
    yield
    tz.bind_display_timezone(before)


def test_a_time_is_utc_and_says_so_when_nothing_is_configured():
    tz.bind_display_timezone("UTC")
    assert format_message_time(MOMENT) == "2026-09-04 06:13:11 +00"


def test_the_configured_zone_moves_the_clock_and_the_label_together():
    tz.bind_display_timezone("+07:00")
    assert format_message_time(MOMENT) == "2026-09-04 13:13:11 +07"


def test_a_half_hour_zone_is_rendered_in_minutes_not_as_a_fraction():
    tz.bind_display_timezone("+05:30")
    assert format_message_time(MOMENT) == "2026-09-04 11:43:11 +05:30"


def test_a_negative_offset_keeps_its_sign():
    tz.bind_display_timezone("-03:00")
    assert format_message_time(MOMENT) == "2026-09-04 03:13:11 -03"


def test_a_naive_timestamp_is_read_as_utc():
    """Every timestamp column here holds UTC, so a value parsed out of one and handed over
    without a tzinfo is UTC — guessing the local zone would silently shift it."""
    tz.bind_display_timezone("UTC")
    assert format_message_time(MOMENT.replace(tzinfo=None)) == "2026-09-04 06:13:11 +00"


def test_a_message_and_a_report_header_render_the_same_instant_identically():
    """The reason this function is a delegation and not an implementation. Two producers, one
    failure, two stamps that used to be `2026-09-04 13:13:11 UTC+07:00` and
    `2026-09-04 13:13:11 +07` — the same moment, and no reader could be sure of that."""
    tz.bind_display_timezone("Asia/Ho_Chi_Minh")
    assert format_message_time(MOMENT) == tz.format_display(MOMENT)


def test_the_displayed_time_never_becomes_the_stored_time():
    """The one thing that must not follow the display zone. `format_utc` is what goes into a
    column that sorts, compares and is range-queried on both backends."""
    tz.bind_display_timezone("Asia/Ho_Chi_Minh")
    assert format_utc(MOMENT) == "2026-09-04T06:13:11Z"
    assert format_message_time(MOMENT) != format_utc(MOMENT)


def test_the_env_var_this_replaces_is_still_the_name_the_fallback_reads(monkeypatch):
    """`DB_OPS_MESSAGE_UTC_OFFSET_HOURS` was the only mechanism there used to be. It still works —
    an operator who set it does not silently land on UTC after upgrading — but it now feeds the
    same resolution as everything else instead of being read here."""
    monkeypatch.delenv(tz.TIMEZONE_ENV_VAR, raising=False)
    monkeypatch.setenv(MESSAGE_UTC_OFFSET_ENV, "7")
    assert tz.declaration_from_env() == "+07:00"
