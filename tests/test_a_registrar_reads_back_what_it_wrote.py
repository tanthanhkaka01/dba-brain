"""`sql-target-add` must accept every `time_window` field the runtime reads.

Found on 2026-09-19, moving SQL task 29 from the container worker to a soak node. The target was
read out of `data/sql_targets.json` and handed straight back to `sql-target-add`, which refused it:

    ConfigAdminError: Unknown time_window field: retry_interval

`retry_interval` is read by `db_ops.lib.time_window`, documented in
`data/shared_config_objects.json` as one of `time_window`'s thirteen fields, and set by targets in
this estate. `common/config_admin.py` had its own twelve-key copy of that list, and the copy had
drifted. Nothing caught it: the file is valid, the runtime reads it, `check-objects` passes it — the
only way to meet the gap is to move a target from one node to another, which happens rarely and
badly.

So the list is no longer copied. These tests pin that, and the round trip that found it.
"""

from __future__ import annotations

import pytest

from db_ops.common import config_admin
from db_ops.lib import time_window


def test_the_registrar_accepts_exactly_the_fields_the_runtime_reads():
    """One list, not two. A second copy is what drifted."""
    assert tuple(config_admin._TIME_WINDOW_KEYS) == tuple(time_window.NEW_FIELDS)
    assert len(config_admin._TIME_WINDOW_KEYS) == 13


def test_every_accepted_field_has_a_default():
    """A key accepted but absent from the defaults would be dropped on the way through."""
    assert set(config_admin._DEFAULT_TIME_WINDOW) == set(config_admin._TIME_WINDOW_KEYS)


def test_the_field_that_was_missing_is_kept_through_the_round_trip():
    """The exact refusal: a window read from sql_targets.json, handed back unchanged."""
    stored = {
        "from_year": None, "to_year": None, "from_month": None, "to_month": None,
        "from_day": 1, "to_day": 31, "from_hour": None, "to_hour": None,
        "from_minute": None, "to_minute": None,
        "repeat_interval": 18000, "timeout": 1800, "retry_interval": 1800,
    }

    window = config_admin.normalize_time_window(stored)

    assert window["retry_interval"] == 1800
    assert window["repeat_interval"] == 18000
    assert window["timeout"] == 1800


def test_a_window_it_writes_can_be_read_back_by_the_same_function():
    """The property the copy broke: normalize(normalize(x)) == normalize(x)."""
    once = config_admin.normalize_time_window({"repeat_interval": 300, "retry_interval": 60})

    assert config_admin.normalize_time_window(once) == once


def test_an_unset_retry_interval_stays_unset_rather_than_taking_one_app_s_default():
    """Unset means "the app's own default", and the apps do not agree on it: the daemon falls back
    to 60 s, metrics declare 600. Writing a number here would freeze one app's answer for all."""
    assert config_admin.normalize_time_window({})["retry_interval"] is None


def test_a_field_that_is_genuinely_not_a_time_window_field_is_still_refused():
    """The fix widens the list to the real one; it does not stop checking."""
    with pytest.raises(config_admin.ConfigAdminError) as caught:
        config_admin.normalize_time_window({"retry_intervals": 60})

    assert "retry_intervals" in str(caught.value)


def test_a_negative_retry_interval_is_refused_like_every_other_interval():
    with pytest.raises(config_admin.ConfigAdminError) as caught:
        config_admin.normalize_time_window({"retry_interval": -1})

    assert "retry_interval" in str(caught.value)
    # -1 means "manual" for repeat_interval only; it is not a general escape.
    assert config_admin.normalize_time_window({"repeat_interval": -1})["repeat_interval"] == -1
