"""Two retention rules, and which one answers by default.

`age` is the default and the operator-chosen one: a file older than the window is obsolete, full,
diff and log alike. It is predictable - somebody reading "retention 14 days" against a directory
listing can work out the answer themselves - and it is the same rule the backup scripts already
apply to their own directories.

`recovery_window` is the stricter cousin, kept because the difference is real: restoring to a point
ten days ago needs the FULL taken *before* that point, and under `age` that FULL goes the moment it
turns N days old while the differentials that restore onto it stay. It only bites when fulls are
taken rarely relative to the window; this estate takes one daily against 14 days, so the two agree
here. The tests below pin both rules and the boundary between them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db_ops.lib.backupfiles_retention import (
    AGE,
    DEFAULT_RETENTION_DAYS,
    OBSOLETE,
    RECOVERY_WINDOW,
    RetentionError,
    plan_retention,
)

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def _file(kind, days_ago, *, database=None, size=1000, name=None):
    finished = NOW - timedelta(days=days_ago)
    stamp = finished.strftime("%Y%m%d_%H%M%S")
    return {
        "path": f"/backup/{database or 'db'}/{kind.upper()}/{name or kind}_{stamp}.bak",
        "kind": kind, "database": database, "size": size,
        "finished_at": finished.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _paths(rows):
    return {row["path"] for row in rows}


def test_the_default_window_is_fourteen_days():
    """What restore_config.json already says for every database/full job, and what an operator
    means by "retention" without qualifying it."""
    assert DEFAULT_RETENTION_DAYS == 14


def test_a_full_older_than_the_window_is_kept_when_the_window_reaches_back_to_it():
    """THE case age-based deletion gets wrong. The 20-day-old full is the anchor: restoring to 14
    days ago starts from it, so it is required however old it is."""
    files = [_file("full", 20), _file("full", 3), _file("log", 15), _file("log", 1)]

    plan = plan_retention(files, retention_days=14, mode=RECOVERY_WINDOW, now=NOW)

    assert plan["counts"]["obsolete"] == 0
    assert "anchor" in plan["keep"][0]["reason"]


def test_everything_before_the_anchor_full_is_obsolete():
    """A second, newer full inside the window moves the anchor forward - and only then does the
    older chain stop being needed."""
    files = [_file("full", 40, name="oldest"), _file("log", 35, name="ancient_log"),
             _file("full", 20, name="anchor"), _file("log", 10, name="recent_log")]

    plan = plan_retention(files, retention_days=14, mode=RECOVERY_WINDOW, now=NOW)

    assert _paths(plan["obsolete"]) == {
        "/backup/db/FULL/oldest_20260628_120000.bak",
        "/backup/db/LOG/ancient_log_20260703_120000.bak",
    }
    assert plan["counts"]["keep"] == 2


def test_nothing_is_deleted_when_every_full_is_newer_than_the_cutoff():
    """The chain does not reach the far edge of the window yet. Deleting from it would shorten a
    window that is already too short - the opposite of what retention is for."""
    files = [_file("full", 3), _file("diff", 2), _file("log", 1)]

    plan = plan_retention(files, retention_days=14, mode=RECOVERY_WINDOW, now=NOW)

    assert plan["counts"]["obsolete"] == 0
    assert "does not reach back" in plan["keep"][0]["reason"]


def test_nothing_is_deleted_when_there_is_no_full_at_all():
    """Logs and differentials with nothing to restore onto. Deleting the oldest of them frees space
    and destroys the only thing present."""
    files = [_file("log", 40), _file("log", 30), _file("diff", 20)]

    plan = plan_retention(files, retention_days=14, mode=RECOVERY_WINDOW, now=NOW)

    assert plan["counts"]["obsolete"] == 0
    assert "no full backup" in plan["keep"][0]["reason"]


def test_a_file_with_no_finished_at_is_kept():
    """An engine that could not state when it finished. "Unknown age" and "old" are not the same
    fact, and only one of them is a reason to delete something."""
    files = [_file("full", 40), _file("full", 20),
             {"path": "/backup/db/LOG/mystery.trn", "kind": "log", "database": None,
              "size": 10, "finished_at": None}]

    plan = plan_retention(files, retention_days=14, now=NOW)

    assert "/backup/db/LOG/mystery.trn" not in _paths(plan["obsolete"])
    assert any("age unknown" in row["reason"] for row in plan["keep"])


def test_each_database_is_judged_against_its_own_chain():
    """One SQL Server directory holds every database on the instance. Anchored across all of them,
    a database backed up nightly would judge one backed up monthly."""
    files = [
        _file("full", 40, database="NIGHTLY"), _file("full", 20, database="NIGHTLY"),
        _file("full", 40, database="MONTHLY"),          # its only full
    ]

    plan = plan_retention(files, retention_days=14, mode=RECOVERY_WINDOW, now=NOW)

    obsolete = _paths(plan["obsolete"])
    assert "/backup/NIGHTLY/FULL/full_20260628_120000.bak" in obsolete
    # MONTHLY's only full is its anchor; losing it would leave that database with nothing.
    assert "/backup/MONTHLY/FULL/full_20260628_120000.bak" not in obsolete


def test_the_obsolete_paths_are_exactly_what_delete_files_takes():
    """Returned as a flat array of paths so deciding and deleting stay two deliberate steps that
    fit together without translation."""
    files = [_file("full", 40, name="old"), _file("full", 20, name="anchor")]

    plan = plan_retention(files, retention_days=14, mode=RECOVERY_WINDOW, now=NOW)

    assert plan["obsolete_paths"] == [row["path"] for row in plan["obsolete"]]
    assert plan["obsolete_paths"] == ["/backup/db/FULL/old_20260628_120000.bak"]


def test_reclaimable_bytes_counts_only_the_obsolete():
    files = [_file("full", 40, size=500, name="old"), _file("full", 20, size=900, name="anchor")]

    plan = plan_retention(files, retention_days=14, mode=RECOVERY_WINDOW, now=NOW)

    assert plan["reclaimable_bytes"] == 500


def test_a_longer_window_keeps_more():
    """The knob does what it says: 30 days reaches back past a full that 14 days does not need."""
    files = [_file("full", 40, name="oldest"), _file("full", 25, name="middle"),
             _file("full", 5, name="newest")]

    short = plan_retention(files, retention_days=14, mode=RECOVERY_WINDOW, now=NOW)
    long = plan_retention(files, retention_days=30, mode=RECOVERY_WINDOW, now=NOW)

    # 14 days: the newest full at or before the cutoff is `middle`, so only `oldest` falls away.
    assert _paths(short["obsolete"]) == {"/backup/db/FULL/oldest_20260628_120000.bak"}
    # 30 days: the cutoff moves back past `middle`, the anchor becomes `oldest`, and nothing goes.
    assert long["counts"]["obsolete"] == 0


@pytest.mark.parametrize("bad", [0, -1])
def test_a_window_of_zero_days_is_refused(bad):
    """It would mark the entire set obsolete, including the backup taken a minute ago. If that is
    genuinely wanted it is a delete-files call with explicit paths, not a retention policy."""
    with pytest.raises(RetentionError, match="at least 1"):
        plan_retention([_file("full", 1)], retention_days=bad, now=NOW)


def test_a_non_numeric_window_is_refused():
    with pytest.raises(RetentionError, match="whole number of days"):
        plan_retention([_file("full", 1)], retention_days="fortnight", now=NOW)


def test_every_row_carries_a_reason():
    """The verdict has to be readable by whoever is about to approve a deletion."""
    files = [_file("full", 40, name="old"), _file("full", 20, name="anchor"), _file("log", 1)]

    plan = plan_retention(files, retention_days=14, mode=RECOVERY_WINDOW, now=NOW)

    for row in plan["obsolete"] + plan["keep"]:
        assert row["reason"], f"no reason given for {row['path']}"
        assert row["verdict"] in (OBSOLETE, "keep")


# --------------------------------------------------------------------------- #
# `age` — the default, and what it does that recovery_window does not
# --------------------------------------------------------------------------- #
def test_age_is_the_default_rule():
    """Asked for by name in the answer, so a stored response says which rule produced it. A plan
    that does not record its own rule cannot be re-checked six months later."""
    plan = plan_retention([_file("full", 20)], retention_days=14, now=NOW)

    assert plan["mode"] == AGE


def test_age_deletes_full_diff_and_log_alike_once_they_are_old_enough():
    """No exemption by kind: the operator's rule is the file's age, and a 20-day-old full is as
    obsolete as a 20-day-old log."""
    files = [_file("full", 20), _file("diff", 20), _file("log", 20),
             _file("full", 3), _file("log", 1)]

    plan = plan_retention(files, retention_days=14, mode=AGE, now=NOW)

    assert plan["counts"]["obsolete"] == 3
    assert {row["kind"] for row in plan["obsolete"]} == {"full", "diff", "log"}
    assert {row["kind"] for row in plan["keep"]} == {"full", "log"}


def test_age_and_recovery_window_agree_when_a_full_is_taken_daily():
    """What this estate actually runs. The two rules only diverge when fulls are rare relative to
    the window, so on a daily full against 14 days the answer is the same either way."""
    files = [_file("full", days) for days in range(0, 21)]

    by_age = plan_retention(files, retention_days=14, mode=AGE, now=NOW)
    by_window = plan_retention(files, retention_days=14, mode=RECOVERY_WINDOW, now=NOW)

    assert _paths(by_age["obsolete"]) == _paths(by_window["obsolete"])


def test_age_deletes_a_full_that_the_window_rule_would_keep():
    """The documented difference, pinned so nobody has to take the docstring's word for it: the
    20-day-old full is the base for the 10-day-old diff, and `age` removes it anyway."""
    files = [_file("full", 20, name="base"), _file("diff", 10, name="dependent")]

    by_age = plan_retention(files, retention_days=14, mode=AGE, now=NOW)
    by_window = plan_retention(files, retention_days=14, mode=RECOVERY_WINDOW, now=NOW)

    assert _paths(by_age["obsolete"]) == {"/backup/db/FULL/base_20260718_120000.bak"}
    assert by_window["counts"]["obsolete"] == 0


def test_an_unknown_mode_is_refused_by_name():
    with pytest.raises(RetentionError, match="mode must be one of"):
        plan_retention([_file("full", 1)], retention_days=14, mode="forever", now=NOW)
