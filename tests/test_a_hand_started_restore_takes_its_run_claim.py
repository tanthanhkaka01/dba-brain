"""A share-driven restore started by hand takes its run claim, like every other restore (0.39.0, 1.342).

The scheduler's ``workflow`` claims an entry's RUNNING row before it restores it, and so does a
script-driven restore started by hand. ``restore-workflow`` on a share-driven entry - which is what
``/spbot_restore`` runs for one - took none (read 2026-11-12): a restore started by hand ran beside
the scheduled one of the same entry, the next due check did not see it, and a run that died left no
row for the reaper to close. Two restores into one database is the failure of 2026-09-14; the claim
is what stops it, and this door went round it.

Now it is claimed before anything is announced or touched, closed with what the restore came to,
and an entry already running is told so - not started, and not reported as a failure. This door is
also held to the refusals the scheduled run makes for an entry (``overwrite_existing`` stated, the
fields that decide a run): it asked for the source and the action only.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import cli, schedule
from dbabrain.db.store import DbaBrainStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    opened = DbaBrainStore(tmp_path / "store.sqlite")
    opened.initialize()
    monkeypatch.setattr(DbaBrainStore, "from_config", classmethod(lambda cls, config, **_: opened))
    return opened


def _entry(restore_id="APP_TO_DRILL", **over):
    values = dict(restore_id=restore_id, source_id="SRC", target_id="DRILL", restore_from="server",
                  action="restore", overwrite_existing=False, unstated=())
    values.update(over)
    return SimpleNamespace(**values)


def _latest(store, restore_id="APP_TO_DRILL"):
    return store.fetch_latest_job_runs_by_job_code().get(schedule.restore_job_code(restore_id))


# --------------------------------------------------------------------------- #
# The claim
# --------------------------------------------------------------------------- #
def test_each_selected_entry_is_claimed_under_its_own_restore_id(store):
    _store, claims = cli._claim_hand_started(object(), [_entry("A"), _entry("B")], action="restore")

    assert [entry.restore_id for entry, _run in claims] == ["A", "B"]
    for restore_id in ("A", "B"):
        row = _latest(store, restore_id)
        assert str(row["status"]).lower() == "running" and "started by hand" in row["message"]
    assert schedule.is_running(schedule.restore_job_code("A"), store.fetch_latest_job_runs_by_job_code())


def test_an_entry_whose_restore_is_running_is_not_started_and_nothing_is_claimed(store):
    schedule.start_run(store=store, job_code=schedule.restore_job_code("B"), message="scheduled", metadata={})

    with pytest.raises(cli.AlreadyRunning) as busy:
        cli._claim_hand_started(object(), [_entry("A"), _entry("B")], action="restore")

    assert "restore_id=B is already running" in str(busy.value) and "Nothing was started" in str(busy.value)
    assert _latest(store, "A") is None, "the entry beside it was not claimed for a run that is not going to happen"


def test_the_scheduler_sees_a_hand_started_restore_as_running(store):
    cli._claim_hand_started(object(), [_entry()], action="restore")

    with pytest.raises(Exception, match="(?i)claim|running|unique"):
        schedule.start_run(store=store, job_code=schedule.restore_job_code("APP_TO_DRILL"),
                           message="scheduled", metadata={})


def test_a_store_that_cannot_be_reached_does_not_stop_a_restore_by_hand(monkeypatch):
    """A restore by hand is what is run when things are broken: unclaimed, and said so in the log."""
    def down(cls, config, **_):
        raise OSError("the store did not answer")

    monkeypatch.setattr(DbaBrainStore, "from_config", classmethod(down))
    said = []
    monkeypatch.setattr(cli, "log_event", lambda logger, level, message: said.append((level, message)))

    store, claims = cli._claim_hand_started(object(), [_entry()], action="restore", logger=object())

    assert store is None and claims == []
    assert said and said[0][0] == "warning" and "no run claim taken" in said[0][1]


def test_an_entry_with_no_restore_id_has_nothing_to_be_claimed_under(store):
    _store, claims = cli._claim_hand_started(object(), [_entry(""), _entry("A")], action="restore")

    assert [entry.restore_id for entry, _run in claims] == ["A"]


# --------------------------------------------------------------------------- #
# Closed with what the restore came to
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("output, status, error", [
    ({"status": "SUCCESS", "per_database_restore_status": {"A": "SUCCESS", "B": "SUCCESS_RESUMED"}}, "done", None),
    ({"status": "SUCCESS", "per_database_restore_status": {"A": "SUCCESS", "B": "FAILED"}}, "error",
     "restore reported SUCCESS; 1 database(s) not restored or not clean"),
    ({"status": "FAILED"}, "error", "restore reported FAILED"),
    ({}, "error", "restore reported no status"),
    ({"status": "FAILED", "per_restore_results": [{"restore_id": "APP_TO_DRILL", "status": "SUCCESS", "failed": 0}]},
     "done", None),
    ({"status": "SUCCESS", "per_restore_results": [{"restore_id": "APP_TO_DRILL", "status": "SUCCESS", "failed": 2}]},
     "error", "restore reported SUCCESS; 2 database(s) not restored or not clean"),
])
def test_a_run_is_done_only_when_the_engine_said_so_and_no_database_was_lost(output, status, error):
    """The scheduled run's rule: returning without raising is not the restore having worked."""
    assert cli._hand_run_outcome(output, "APP_TO_DRILL") == (status, error)


def test_the_claimed_row_is_closed_with_the_outcome(store):
    _store, claims = cli._claim_hand_started(object(), [_entry()], action="restore")

    cli._close_hand_started(store, claims, started=time.monotonic(),
                            output={"status": "SUCCESS", "per_database_restore_status": {"APPDB": "SUCCESS"}})

    row = _latest(store)
    assert str(row["status"]).lower() == "done" and row["message"] == "Restore APP_TO_DRILL finished: done."
    assert not schedule.is_running(schedule.restore_job_code("APP_TO_DRILL"), store.fetch_latest_job_runs_by_job_code())


def test_a_restore_that_raised_closes_its_row_with_the_error(store):
    _store, claims = cli._claim_hand_started(object(), [_entry()], action="restore")

    cli._close_hand_started(store, claims, started=time.monotonic(), error=RuntimeError("the share went away"))

    row = _latest(store)
    assert str(row["status"]).lower() == "error" and "the share went away" in str(row["error_text"])


def test_closing_never_raises_and_does_nothing_without_a_claim(store):
    cli._close_hand_started(None, [(_entry(), 1)], started=time.monotonic(), output={})
    cli._close_hand_started(store, [], started=time.monotonic(), output={})
    cli._close_hand_started(store, [(_entry(), 999_999)], started=time.monotonic(), output={"status": "SUCCESS"})


# --------------------------------------------------------------------------- #
# The refusals of the scheduled run, at this door too
# --------------------------------------------------------------------------- #
def test_an_entry_that_states_no_overwrite_existing_is_refused_here_as_well():
    with pytest.raises(Exception, match="overwrite_existing"):
        cli._decide_copy_only([_entry(overwrite_existing=None)], asked=False)


def test_an_entry_that_leaves_a_deciding_field_out_is_refused_here_as_well():
    with pytest.raises(Exception, match="states no checkdb"):
        cli._decide_copy_only([_entry(unstated=("checkdb",))], asked=False)


def test_an_entry_that_states_everything_runs_as_its_action_says():
    assert cli._decide_copy_only([_entry()], asked=False) is False
    assert cli._decide_copy_only([_entry(action="copy_only")], asked=False) is True
    assert cli._decide_copy_only([_entry()], asked=True) is True
