"""`repeat_interval: 300` means the same thing in every file that can carry it.

Four apps schedule from a `time_window`, and each one reads "the previous run" off a store row of
its own. The rule was shared from the start; the *anchor* was not. `sql_tasks` read
`sql_runs.finished_at` and the reports app the instant its last send completed, so a target
declaring 300 seconds ran every 540 if its task took 240 — and nothing in the config, the log or
the docs said which of the three meanings applied where.

The fix was one function (`due_from_row`) and one place that picks the column (`run_anchor`). What
holds it is this file: a test per app, asserting on the operator's own example — a task that runs
for 240 seconds with `repeat_interval: 300` is due again 60 seconds after it finishes, not 300.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db_ops.backup_restore import schedule as backup_schedule
from db_ops.jobs import daemon
from db_ops.lib.time_window import TimeWindow, due_from_row, run_anchor
from db_ops.sql_tasks import runner as sql_runner

START = datetime(2026, 9, 19, 1, 0, 0, tzinfo=timezone.utc)
RAN_FOR = 240
INTERVAL = 300
#: What the interval costs in practice: a run may begin `min(interval * 5%, 30)` seconds early so a
#: scheduler sweep boundary does not cost a whole cycle. 5% of 300 is 15.
GRACE = 15


def _row(*, status: str = "done", started_at: datetime = START,
         finished_at: datetime | None = None) -> dict:
    """One finished run, spelled the way a `job_runs` / `sql_runs` row is."""
    return {
        "status": status,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "created_at": started_at.isoformat().replace("+00:00", "Z"),
        "finished_at": (finished_at or started_at + timedelta(seconds=RAN_FOR))
        .isoformat().replace("+00:00", "Z"),
        "sql_id": 1,
        "target_no": 1,
    }


def _window(**overrides) -> TimeWindow:
    values = {"repeat_interval": INTERVAL, "retry_interval": 600, "timeout": 900}
    values.update(overrides)
    return TimeWindow(**values)


# --------------------------------------------------------------------------- #
# The anchor itself
# --------------------------------------------------------------------------- #
def test_the_anchor_is_started_at_even_when_the_row_also_carries_finished_at():
    assert run_anchor(_row()) == START


def test_a_row_without_started_at_falls_back_rather_than_reporting_nothing():
    row = _row()
    row["started_at"] = ""
    assert run_anchor(row) == START  # created_at carries the same instant


def test_no_row_at_all_has_no_anchor_which_is_how_a_first_run_becomes_due():
    assert run_anchor(None) is None


# --------------------------------------------------------------------------- #
# The operator's example, once per app
# --------------------------------------------------------------------------- #
def test_a_task_that_runs_240_seconds_on_a_300_second_interval_is_due_60_seconds_later():
    finished = START + timedelta(seconds=RAN_FOR)
    verdict = due_from_row(time_window=_window(), row=_row(),
                           now=finished + timedelta(seconds=INTERVAL - RAN_FOR - GRACE))
    assert verdict.due, verdict.reason
    assert verdict.next_due_at is None


def test_it_is_not_due_while_the_interval_has_not_elapsed_and_says_how_far_off_it_is():
    verdict = due_from_row(time_window=_window(), row=_row(),
                           now=START + timedelta(seconds=200))
    assert not verdict.due
    assert verdict.reason == "interval not elapsed: 200s/300s"
    assert verdict.next_due_at == START + timedelta(seconds=INTERVAL - GRACE)


def test_the_daemon_measures_an_app_command_from_the_start_of_the_last_run():
    command = daemon.AppCommand(
        app_command_id="APP-X", app_code="x", app_name="x", display_name="x",
        log_scope="x", working_dir="tools/db_ops", command_text="python -c pass",
        time_window=_window(), active=True,
    )
    row = _row()
    due_at = START + timedelta(seconds=INTERVAL - GRACE)
    assert not daemon.app_command_is_due(command, row, due_at - timedelta(seconds=1))
    assert daemon.app_command_is_due(command, row, due_at)


def test_a_sql_target_measures_from_its_start_not_from_when_the_task_finished():
    """The regression this file exists for: on `finished_at` the second assert would be False,
    because 300 seconds from the finish is 540 seconds from the start."""
    row = _row()
    due_at = START + timedelta(seconds=INTERVAL - GRACE)
    assert sql_runner.sql_run_time(row) == START
    assert not due_from_row(time_window=_window(), row=row,
                            now=due_at - timedelta(seconds=1)).due
    assert due_from_row(time_window=_window(), row=row, now=due_at).due


def test_a_backup_job_measures_from_its_start_too():
    row = _row()
    assert backup_schedule.run_time(row) == START
    assert backup_schedule.is_due(
        job_code="backup_restore.backup_job.X.full", time_window=_window(),
        latest_runs={"backup_restore.backup_job.X.full": row},
        now=START + timedelta(seconds=INTERVAL - GRACE),
        local_now=datetime(2026, 9, 19, 8, 0, 0),
    )


# --------------------------------------------------------------------------- #
# The three statuses that are not "done"
# --------------------------------------------------------------------------- #
def test_a_failed_run_can_be_retried_EARLIER_than_its_interval_and_measures_that_from_the_start():
    """`retry_interval` brings a retry forward; it cannot postpone one.

    The order in the rule is: still running -> interval elapsed -> failed. So a failed row becomes
    due the moment `repeat_interval` elapses whatever `retry_interval` says, and a `retry_interval`
    LARGER than `repeat_interval` changes nothing at all. That is long-standing behaviour, stated
    here because `docs/04_metrics_engine.md` claimed the opposite until 2026-09-19 and the shipped
    metric catalogue leans on it: 600 s against a 150 s metric is not the back-off it reads as.
    """
    row = _row(status="error")
    window = _window(retry_interval=120)
    verdict = due_from_row(time_window=window, row=row, now=START + timedelta(seconds=100))
    assert not verdict.due
    assert verdict.reason == "retry not elapsed: 100s/120s"
    assert due_from_row(time_window=window, row=row, now=START + timedelta(seconds=120)).due

    # And the other direction: retry 600 on a 300 interval does not hold it back to 600.
    held_back = due_from_row(time_window=_window(retry_interval=600), row=row,
                             now=START + timedelta(seconds=INTERVAL - GRACE))
    assert held_back.due


def test_a_running_row_is_left_alone_until_its_timeout_rather_than_started_twice():
    """Checked before the interval, which is the order that matters: a run outliving its own
    interval would otherwise have a second copy started on top of it."""
    row = _row(status="running")
    verdict = due_from_row(time_window=_window(), row=row,
                           now=START + timedelta(seconds=400))
    assert not verdict.due
    assert verdict.reason == "running within timeout: 400s/900s"
    assert due_from_row(time_window=_window(), row=row,
                        now=START + timedelta(seconds=900)).due


def test_a_manual_entry_is_never_due_and_says_so():
    verdict = due_from_row(time_window=_window(repeat_interval=-1), row=_row(), now=START)
    assert not verdict.due
    assert verdict.reason == "manual only: never scheduled"


def test_a_run_once_entry_that_succeeded_is_never_due_again():
    verdict = due_from_row(time_window=_window(repeat_interval=0), row=_row(),
                           now=START + timedelta(days=7))
    assert not verdict.due
    assert verdict.reason == "run-once: already ran"


# --------------------------------------------------------------------------- #
# The window and the interval are different questions
# --------------------------------------------------------------------------- #
def test_a_closed_hour_window_blocks_a_due_run_and_names_the_hour():
    verdict = due_from_row(
        time_window=_window(from_hour=1, to_hour=6), row=_row(),
        now=START + timedelta(days=1),
        local_now=datetime(2026, 9, 19, 14, 0, 0),
    )
    assert not verdict.due
    assert verdict.reason == "outside allowed hour window: 14"


def test_without_a_local_clock_the_window_is_not_consulted_at_all():
    """A caller that has already checked the window must not have it checked again against a
    zone this function had to guess at — which is what passing `now` would have meant."""
    verdict = due_from_row(time_window=_window(from_hour=1, to_hour=6), row=_row(),
                           now=START + timedelta(days=1))
    assert verdict.due
