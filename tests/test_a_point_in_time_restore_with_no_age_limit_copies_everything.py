"""`copy_recent_hours <= 0` means "every matching file", for a point-in-time restore too.

Before 0.25.0 the entry's value was never used (`--copy-hours` defaulted to 24 and always won), so
this could not happen from a config. Once the entry's own value was honoured, a point-in-time
restore of an entry set to 0 built a window from `point - 0 h` to `point` - zero wide - and the copy
found nothing to take; a negative value inverted it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from db_ops.backup_restore.cli import point_in_time_window_start

POINT = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("hours", [0, -1, -24])
def test_no_age_limit_leaves_the_window_open_below(hours):
    assert point_in_time_window_start(POINT, hours) is None


def test_a_positive_limit_reaches_back_that_far():
    assert point_in_time_window_start(POINT, 36) == POINT - dt.timedelta(hours=36)


def test_no_point_in_time_means_no_window_here():
    assert point_in_time_window_start(None, 24) is None
