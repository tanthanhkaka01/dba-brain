"""The wall-clock time an operator reads off a message.

A message that names no time reads as "this is happening now", which a reaped SQL run never is:
an abandoned row is only revisited on the next scan, so the alert can be written hours after the
run it is about died. The offset is printed every time because every timestamp the tool *stores*
is UTC — a bare local time cannot be compared with `sql_runs.started_at` without knowing which
clock it came from, and the reader has no way to ask.
"""

from datetime import datetime, timezone

from db_ops.lib.text_format import (MESSAGE_UTC_OFFSET_ENV, format_message_time,
                                    message_utc_offset_hours)


MOMENT = datetime(2026, 9, 4, 6, 13, 11, tzinfo=timezone.utc)


def test_a_time_is_utc_and_says_so_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv(MESSAGE_UTC_OFFSET_ENV, raising=False)
    assert format_message_time(MOMENT) == "2026-09-04 06:13:11 UTC+00:00"


def test_a_configured_offset_moves_the_clock_and_the_label_together(monkeypatch):
    monkeypatch.setenv(MESSAGE_UTC_OFFSET_ENV, "7")
    assert format_message_time(MOMENT) == "2026-09-04 13:13:11 UTC+07:00"


def test_a_half_hour_offset_is_rendered_in_minutes_not_as_a_fraction(monkeypatch):
    monkeypatch.setenv(MESSAGE_UTC_OFFSET_ENV, "5.5")
    assert format_message_time(MOMENT) == "2026-09-04 11:43:11 UTC+05:30"


def test_a_negative_offset_keeps_its_sign(monkeypatch):
    monkeypatch.setenv(MESSAGE_UTC_OFFSET_ENV, "-3")
    assert format_message_time(MOMENT) == "2026-09-04 03:13:11 UTC-03:00"


def test_a_naive_timestamp_is_read_as_utc(monkeypatch):
    """Every timestamp column here holds UTC, so a value parsed out of one and handed over
    without a tzinfo is UTC — guessing the local zone would silently shift it."""
    monkeypatch.delenv(MESSAGE_UTC_OFFSET_ENV, raising=False)
    assert format_message_time(MOMENT.replace(tzinfo=None)) == "2026-09-04 06:13:11 UTC+00:00"


def test_an_unusable_offset_falls_back_to_utc_rather_than_failing(monkeypatch):
    """The message exists because something already went wrong; a typo in a display setting must
    not be what stops it from being sent. 420 is the same offset written in minutes, which is the
    typo this rejects — applying it would move the reported time into another day."""
    for value in ("", "   ", "seven", "420"):
        monkeypatch.setenv(MESSAGE_UTC_OFFSET_ENV, value)
        assert message_utc_offset_hours() == 0.0
        assert format_message_time(MOMENT) == "2026-09-04 06:13:11 UTC+00:00"
