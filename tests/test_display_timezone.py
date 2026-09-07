"""The one clock db_ops shows its operator, and what happens when it is declared badly.

Every timestamp the store holds is UTC. That is what makes a rendered wall-clock time dangerous:
`Snapshot 2026-09-07 07:32:56` cannot be lined up against `metric_results.collected_at` unless it
says which clock it is on, and before this module the answer came from the machine's TZ on one node
and a hardcoded +07 in the reports app on another. So the offset is printed every single time, and
the declaration that produces it lives in exactly one place.

The failure these tests really guard is the quiet one: a timezone that is wrong but plausible. A
config typo must stop the process where the config is read, not produce a whole estate of reports
in a clock nobody notices for a month.
"""

from datetime import datetime, timedelta, timezone

import pytest

from db_ops.lib import timezone as tz


MOMENT = datetime(2026, 9, 7, 0, 32, 56, tzinfo=timezone.utc)

#: Berlin in January and in July - the same declaration, two different offsets. A fixed offset
#: cannot express this, which is the whole reason IANA names are accepted at all.
WINTER = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
SUMMER = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _utc_by_default():
    """Leave the process-wide zone as it was found.

    `bind_display_timezone` is module state on purpose - forty producers must not each be handed a
    timezone - and module state that a test leaves behind is a test that changes another one.
    """
    before = tz.display_declaration()
    yield
    tz.bind_display_timezone(before)


# --------------------------------------------------------------------------------------------- #
# Declaring it
# --------------------------------------------------------------------------------------------- #

def test_an_absent_declaration_is_utc_rather_than_the_machines_clock():
    """A config written before the field existed is an upgrade, not a mistake - but the default
    cannot be "whatever this host is set to", because that is the behaviour the field replaces."""
    assert tz.parse_declaration(None) == "UTC"
    assert tz.parse_declaration("") == "UTC"
    assert tz.parse_declaration("   ") == "UTC"


def test_the_offsets_an_operator_actually_types_all_mean_the_same_thing():
    """Normalised to one spelling so the stored declaration and the config file cannot disagree
    about a value they agree on."""
    for written in ("+07", "+07:00", "+0700", "UTC+07:00", "utc+7"):
        assert tz.parse_declaration(written) == "+07:00", written


def test_utc_is_accepted_by_every_name_it_is_written_under():
    for written in ("UTC", "utc", "GMT", "Z", "+00:00", "+00"):
        assert tz.parse_declaration(written) == "UTC", written


def test_an_iana_name_is_passed_through_for_the_zone_database_to_judge():
    """`parse_declaration` is the grammar check. Resolving is separate because a node that only
    ever uses fixed offsets has no reason to need a zone database installed."""
    assert tz.parse_declaration("Asia/Ho_Chi_Minh") == "Asia/Ho_Chi_Minh"
    assert tz.parse_declaration("  America/New_York  ") == "America/New_York"


def test_a_value_that_is_neither_shape_names_both_shapes_it_could_have_been():
    with pytest.raises(tz.TimezoneError) as caught:
        tz.parse_declaration("GMT plus seven", context="config.json:timezone")
    message = str(caught.value)
    assert "config.json:timezone" in message
    assert "GMT plus seven" in message
    assert "IANA" in message and "+07:00" in message


def test_an_offset_larger_than_any_real_one_is_a_typo_and_is_refused():
    """420 is +07:00 written in minutes - the exact mistake, and applying it would move the
    reported time into another day rather than fail."""
    for written in ("+20:00", "-15:00"):
        with pytest.raises(tz.TimezoneError):
            tz.parse_declaration(written)


def test_a_typo_in_a_zone_name_is_told_apart_from_a_missing_zone_database():
    """Two different errors that both arrive as ZoneInfoNotFoundError. One is `pip install
    tzdata`, the other is a spelling mistake, and a reader given the wrong one chases the wrong
    fix."""
    with pytest.raises(tz.TimezoneError) as caught:
        tz.resolve("Asia/Ho_Chi_Min")
    assert "not a known IANA timezone name" in str(caught.value)
    assert "tzdata" not in str(caught.value)


# --------------------------------------------------------------------------------------------- #
# Rendering it
# --------------------------------------------------------------------------------------------- #

def test_a_displayed_time_always_carries_its_offset():
    assert tz.format_display(MOMENT, "UTC") == "2026-09-07 00:32:56 +00"
    assert tz.format_display(MOMENT, "+07:00") == "2026-09-07 07:32:56 +07"


def test_a_whole_hour_offset_does_not_print_its_zero_minutes():
    """Beside a 19-character timestamp, `+07:00` is four characters of noise on most of the
    planet. Minutes appear when they carry information and not before."""
    assert tz.format_offset(420) == "+07"
    assert tz.format_offset(0) == "+00"
    assert tz.format_offset(330) == "+05:30"
    assert tz.format_offset(-210) == "-03:30"


def test_the_stored_declaration_keeps_its_minutes_so_it_compares_as_a_string():
    assert tz.format_offset(420, always_minutes=True) == "+07:00"
    assert tz.format_offset(0, always_minutes=True) == "+00:00"


def test_a_half_hour_zone_is_rendered_in_minutes_not_as_a_fraction():
    assert tz.format_display(MOMENT, "Asia/Kolkata") == "2026-09-07 06:02:56 +05:30"


def test_a_negative_offset_can_move_the_displayed_date_backwards():
    """The point of showing the offset: 2026-09-07 00:32 UTC is still the 6th in New York, and a
    reader comparing it against a UTC row needs to be able to see why."""
    assert tz.format_display(MOMENT, "America/New_York") == "2026-09-06 20:32:56 -04"


def test_a_naive_timestamp_is_read_as_utc_because_that_is_what_the_columns_hold():
    """Reading a naive value as machine-local would shift it by the offset and then render the
    shifted value with the offset - wrong twice, and plausible-looking both times."""
    assert tz.format_display(MOMENT.replace(tzinfo=None), "+07:00") == "2026-09-07 07:32:56 +07"


def test_the_stored_format_is_untouched_by_any_of_this():
    """One value, two audiences: a column that has to sort and compare, and a sentence someone
    acts on. Nothing here may change the first."""
    tz.bind_display_timezone("Asia/Ho_Chi_Minh")
    assert tz.format_stored(MOMENT) == "2026-09-07T00:32:56Z"


# --------------------------------------------------------------------------------------------- #
# Daylight saving
# --------------------------------------------------------------------------------------------- #

def test_an_iana_zone_follows_daylight_saving_and_a_fixed_offset_does_not():
    assert tz.format_display(WINTER, "Europe/Berlin") == "2026-01-15 13:00:00 +01"
    assert tz.format_display(SUMMER, "Europe/Berlin") == "2026-07-15 14:00:00 +02"
    assert tz.format_display(WINTER, "+01:00") == "2026-01-15 13:00:00 +01"
    assert tz.format_display(SUMMER, "+01:00") == "2026-07-15 13:00:00 +01"


def test_a_recorded_offset_is_only_true_for_the_moment_it_was_recorded_at():
    """Why the store keeps the declaration *and* the resolved offset instead of deriving one from
    the other: this pair differs by an hour twice a year, and only the declaration can be written
    back into a config file."""
    assert tz.offset_minutes("Europe/Berlin", at=WINTER) == 60
    assert tz.offset_minutes("Europe/Berlin", at=SUMMER) == 120


# --------------------------------------------------------------------------------------------- #
# Binding it, once
# --------------------------------------------------------------------------------------------- #

def test_binding_the_zone_changes_every_later_render():
    tz.bind_display_timezone("+07:00")
    assert tz.display_declaration() == "+07:00"
    assert tz.format_display(MOMENT) == "2026-09-07 07:32:56 +07"
    tz.bind_display_timezone("UTC")
    assert tz.format_display(MOMENT) == "2026-09-07 00:32:56 +00"


def test_binding_a_bad_declaration_raises_where_the_config_is_read():
    """Config errors belong at config-load time. The alternative is forty timestamps in the wrong
    clock and a defect report written from a report header a month later."""
    with pytest.raises(tz.TimezoneError):
        tz.bind_display_timezone("Mars/Olympus_Mons")


def test_a_file_stamp_and_the_header_inside_the_file_agree():
    """They did not before: the stamp came from the machine clock and the row describing it was
    UTC, so a report built on the master at 14:48 +07 was `20260905_144812_*.html` beside a store
    row saying 07:48:12Z."""
    tz.bind_display_timezone("+07:00")
    assert tz.file_stamp(MOMENT) == "20260907_073256"
    assert tz.format_display(MOMENT).startswith("2026-09-07 07:32:56")


def test_the_local_calendar_day_ends_at_the_operators_midnight():
    """A UTC day boundary lets a once-a-day report out twice in +07: 07:30 local is still
    yesterday in UTC."""
    early = datetime(2026, 9, 6, 22, 30, 0, tzinfo=timezone.utc)
    assert tz.to_display(early, "+07:00").date().isoformat() == "2026-09-07"
    assert tz.to_display(early, "UTC").date().isoformat() == "2026-09-06"


# --------------------------------------------------------------------------------------------- #
# The environment
# --------------------------------------------------------------------------------------------- #

def test_the_env_var_lets_one_config_file_be_copied_to_a_second_node(monkeypatch):
    """The worker gets a copy of the master's config.json. A per-node zone must not require
    editing it - the same reason DB_OPS_NODE_ROLE exists."""
    monkeypatch.setenv(tz.TIMEZONE_ENV_VAR, "Asia/Ho_Chi_Minh")
    assert tz.declaration_from_env() == "Asia/Ho_Chi_Minh"


def test_the_offset_env_var_this_replaces_still_answers_so_an_upgrade_keeps_its_clock(monkeypatch):
    monkeypatch.delenv(tz.TIMEZONE_ENV_VAR, raising=False)
    monkeypatch.setenv(tz.LEGACY_OFFSET_ENV_VAR, "7")
    assert tz.declaration_from_env() == "+07:00"
    monkeypatch.setenv(tz.LEGACY_OFFSET_ENV_VAR, "5.5")
    assert tz.declaration_from_env() == "+05:30"


def test_the_named_zone_wins_over_the_offset_it_replaces(monkeypatch):
    monkeypatch.setenv(tz.TIMEZONE_ENV_VAR, "UTC")
    monkeypatch.setenv(tz.LEGACY_OFFSET_ENV_VAR, "7")
    assert tz.declaration_from_env() == "UTC"


def test_a_silent_environment_says_so_rather_than_guessing(monkeypatch):
    """None, not 'UTC': the caller has to be able to tell "nothing was set" from "UTC was set",
    because only the first falls through to the config file."""
    monkeypatch.delenv(tz.TIMEZONE_ENV_VAR, raising=False)
    monkeypatch.delenv(tz.LEGACY_OFFSET_ENV_VAR, raising=False)
    assert tz.declaration_from_env() is None


def test_an_unusable_legacy_offset_is_ignored_rather_than_applied(monkeypatch):
    monkeypatch.delenv(tz.TIMEZONE_ENV_VAR, raising=False)
    for value in ("seven", "420", "-99"):
        monkeypatch.setenv(tz.LEGACY_OFFSET_ENV_VAR, value)
        assert tz.declaration_from_env() is None, value


# --------------------------------------------------------------------------------------------- #
# What gets recorded
# --------------------------------------------------------------------------------------------- #

def test_the_recorded_description_carries_the_setting_and_the_snapshot_both():
    tz.bind_display_timezone("Europe/Berlin")
    facts = tz.describe(at=SUMMER)
    assert facts["timezone"] == "Europe/Berlin"          # the setting, writable back to a config
    assert facts["utc_offset_minutes"] == 120            # the snapshot, true as of SUMMER only
    assert facts["utc_offset"] == "+02"
    assert facts["tz_abbreviation"] == "CEST"
    assert facts["now_display"] == "2026-07-15 14:00:00 +02"
    assert facts["now_utc"] == "2026-07-15T12:00:00Z"


def test_a_fixed_offset_records_no_abbreviation_rather_than_a_second_copy_of_the_offset():
    """Python answers `UTC+07:00` for a fixed offset, which is the offset written out again. Empty
    keeps the column meaning "the zone had a name"."""
    assert tz.zone_abbreviation("+07:00", at=MOMENT) == ""
    assert tz.describe("+07:00", at=MOMENT)["tz_abbreviation"] == ""


def test_now_in_the_display_zone_is_the_same_instant_as_now_in_utc():
    """`display_now` moves the clock, never the instant. A window comparison that shifted the
    moment as well as the rendering would fire at the wrong time in both zones."""
    tz.bind_display_timezone("+07:00")
    drift = abs(tz.display_now() - datetime.now(timezone.utc))
    assert drift < timedelta(seconds=5)
    assert tz.display_now().utcoffset() == timedelta(hours=7)
