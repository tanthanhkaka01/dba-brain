"""A long run must not stop the queue, and must never be started twice.

Two failures, opposite in shape, and a fix that has to hold both ends at once.

**The queue.** On 2026-09-19 SQL task 29 ran for 22 minutes. ``APP-SQL_TASKS`` is one app command,
the daemon ran one process per app command, and that process worked through its due list in order —
so for 22 minutes no other SQL task could start. Earlier the same day a leftover ``running`` row
held the same queue for 30 minutes. ``APP-BACKUP-RESTORE`` had the same shape.

**The duplicate.** Simply allowing more processes produces the worse failure: the same task run
twice, writing to production twice. Eight duplicate production SQL runs on 2026-09-08; a second
restore that began 47 minutes into the first on 2026-09-14. What stopped them before was a SELECT
taken before the decision, which is safe only while exactly one process can ever be deciding.

So the fix is two parts and the order matters. **The claim** is a unique index: one ``running`` row
per unit of work, so the second process is *told* and moves on. **The field** is per app command:
``sync`` keeps today's behaviour, ``async`` lets the daemon call it while a run is in flight — which
is only safe because the claim exists.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from db_ops.db import DbOpsStore
from db_ops.db.job_runs import JobRun
from db_ops.db.store import RunAlreadyClaimed
from db_ops.lib import run_claim, run_mode


@pytest.fixture()
def store(tmp_path):
    built = DbOpsStore(tmp_path / "db_ops.sqlite")
    built.initialize()
    return built


def _sql_run(store, *, run_key="29|1|SRV", status="running", host=None):
    metadata = {"host_name": host} if host else None
    return store.insert_sql_run(
        run_key=run_key, sql_id=29, sql_code="SQL029", target_no=1, server_id="SRV",
        db_type="sqlserver", service_name="S", instance_name="I", database_name="D",
        credential_name="C", status=status, level="logging", message="m",
        started_at="2026-09-19T10:00:00Z", metadata=metadata)


def _job_run(store, *, job_code, claim_key, host="HOST-A", status="RUNNING"):
    return store.insert_job_run(JobRun(
        job_code=job_code, claim_key=claim_key, level="logging", status=status, message="m",
        started_at="2026-09-19T10:00:00Z", host_name=host))


# --------------------------------------------------------------------------- #
# The claim: a SQL task
# --------------------------------------------------------------------------- #
def test_the_same_task_and_target_cannot_be_claimed_twice(store):
    """The 2026-09-08 failure, from the store's side: whoever inserts owns it."""
    first = _sql_run(store)

    with pytest.raises(RunAlreadyClaimed):
        _sql_run(store)

    assert first


def test_the_claim_is_released_when_the_run_finishes(store):
    first = _sql_run(store)
    store.update_sql_run(sql_run_id=first, status="done", level="logging", message="m",
                         finished_at="2026-09-19T10:05:00Z")

    assert _sql_run(store), "a finished run holds nothing"


def test_a_different_target_of_the_same_task_is_not_blocked(store):
    """The claim is per unit of work, not per task: task 29 against two servers is two runs."""
    _sql_run(store, run_key="29|1|SRV-A")

    assert _sql_run(store, run_key="29|2|SRV-B")


def test_a_finished_row_is_not_a_claim_whatever_its_status(store):
    for status in ("done", "error", "timeout", "skipped"):
        assert _sql_run(store, run_key=f"k-{status}", status=status)


# --------------------------------------------------------------------------- #
# The claim: a backup job, a restore, and an app command that claims nothing
# --------------------------------------------------------------------------- #
def test_one_backup_job_runs_at_a_time_on_one_host(store):
    code = "backup_restore.backup_job.PG.wal"
    _job_run(store, job_code=code, claim_key=code)

    with pytest.raises(RunAlreadyClaimed):
        _job_run(store, job_code=code, claim_key=code)


def test_a_second_restore_into_the_same_entry_is_refused(store):
    """2026-09-14, reproduced: the second one asked, and now it is told."""
    code = "backup_restore.restore_job.DRILL"
    _job_run(store, job_code=code, claim_key=code)

    with pytest.raises(RunAlreadyClaimed):
        _job_run(store, job_code=code, claim_key=code)


def test_the_claim_is_per_host_so_a_dead_node_cannot_block_a_live_one(store):
    code = "backup_restore.backup_job.PG.wal"
    _job_run(store, job_code=code, claim_key=code, host="HOST-A")

    assert _job_run(store, job_code=code, claim_key=code, host="HOST-B")


def test_an_async_app_command_claims_nothing_and_may_run_many_times_over(store):
    """This is what `async` means at the store: the daemon calls it again while one is running,
    and the rows do not fight. What must not double is the work inside it, which claims its own."""
    for _ in range(5):
        assert _job_run(store, job_code="APP-SQL_TASKS", claim_key=None)


def test_a_sync_app_command_still_claims_its_own_id(store):
    _job_run(store, job_code="APP-METRICS", claim_key="APP-METRICS")

    with pytest.raises(RunAlreadyClaimed):
        _job_run(store, job_code="APP-METRICS", claim_key="APP-METRICS")


def test_the_upgrade_closes_duplicate_running_rows_rather_than_failing_on_them():
    """An existing store has rows left by every node that was ever killed mid-run. A unique index
    built over that data fails at whoever next runs `init`, which is the wrong person."""
    from db_ops.db import store as store_module

    path = Path(tempfile.mkdtemp()) / "old.sqlite"
    old = DbOpsStore(path)
    old.initialize()
    # Put the store back into the shape it had before the claim: no index, and two running rows
    # for one key — which is what every node killed mid-run left behind.
    with old.connect() as conn:
        conn.execute("DROP INDEX IF EXISTS ux_job_runs_claim;")
        for _ in range(2):
            conn.execute(
                "INSERT INTO job_runs (job_code, claim_key, level, status, message, started_at,"
                " host_name, metadata_json) VALUES (?, ?, 'logging', 'RUNNING', 'm',"
                " '2026-09-19T10:00:00Z', 'HOST-A', '{}');",
                ("backup_restore.backup_job.PG.wal", "backup_restore.backup_job.PG.wal"))
        conn.commit()

    with old.connect() as conn:
        closed = store_module.prepare_run_claims(conn)
        conn.commit()

    assert closed["job_runs"] == 1, "the newest stays running, the older one is closed"


# --------------------------------------------------------------------------- #
# The field
# --------------------------------------------------------------------------- #
def test_a_command_that_says_nothing_behaves_exactly_as_it_did_before():
    """An older config must not change behaviour by being read by a newer build."""
    mode = run_mode.parse({})

    assert mode.mode == "sync"
    assert run_mode.may_start(mode, in_flight=0)
    assert not run_mode.may_start(mode, in_flight=1)


def test_async_runs_up_to_its_cap_and_no_further():
    mode = run_mode.parse({"run_mode": "async", "max_parallel": 3})

    assert [run_mode.may_start(mode, in_flight=n) for n in range(5)] == [True, True, True, False, False]


def test_async_without_a_cap_still_has_one():
    """`APP-SQL_TASKS` is due every second. Uncapped, `async` means a new process every second for
    as long as the first one runs — the loop, arriving by a different door than the claim closes."""
    assert run_mode.parse({"run_mode": "async"}).max_parallel == run_mode.DEFAULT_MAX_PARALLEL


def test_a_cap_of_zero_is_refused_and_names_the_honest_way_to_stop_a_command():
    with pytest.raises(run_mode.RunModeError) as caught:
        run_mode.parse({"run_mode": "async", "max_parallel": 0})

    assert "active" in str(caught.value)


def test_a_cap_on_a_sync_command_is_refused_rather_than_ignored():
    """Accepted silently, it reads as a command that runs four at a time and does not."""
    with pytest.raises(run_mode.RunModeError):
        run_mode.parse({"run_mode": "sync", "max_parallel": 4})


def test_an_unknown_mode_is_refused():
    with pytest.raises(run_mode.RunModeError):
        run_mode.parse({"run_mode": "parallel"})


def test_the_two_commands_that_queued_are_the_two_that_ship_async():
    """The estate's own config, so the fix is actually applied and not merely available."""
    import json

    from db_ops.lib.paths import TOOL_ROOT

    shipped = json.loads(
        (TOOL_ROOT / "db_ops/jobs/catalogue/app_commands.json").read_bytes().decode("utf-8-sig"))
    modes = {c["app_command_id"]: run_mode.parse(c) for c in shipped["app_commands"]}

    assert modes["APP-SQL_TASKS"].is_async
    assert modes["APP-BACKUP-RESTORE"].is_async
    assert not modes["APP-WEBHOST"].is_async, "a long-running service is not a queue"


# --------------------------------------------------------------------------- #
# Liveness: the other way this ends in a loop
# --------------------------------------------------------------------------- #
def test_a_live_process_keeps_its_claim_however_long_it_has_run():
    """The loop. A task that legitimately outruns its timeout used to have its row closed, and the
    next scan started a second copy on top of the first."""
    verdict = run_claim.reap_verdict(
        metadata=run_claim.claim_fields(pid=4242, host="HOST-A"),
        this_host="HOST-A", elapsed_seconds=99999, timeout_seconds=1800, pid_alive=True)

    assert not verdict.reap
    assert "alive" in verdict.reason


def test_a_dead_process_frees_its_claim_at_once():
    """No waiting: a process that is gone will not come back, and the work must be able to resume."""
    verdict = run_claim.reap_verdict(
        metadata=run_claim.claim_fields(pid=4242, host="HOST-A"),
        this_host="HOST-A", elapsed_seconds=5, timeout_seconds=1800, pid_alive=False)

    assert verdict.reap


def test_another_host_s_run_is_left_alone_until_a_long_grace_has_passed():
    """This host cannot ask that one whether its pid is alive, so the only safe reading is age."""
    owned_elsewhere = run_claim.claim_fields(pid=4242, host="HOST-B")

    within = run_claim.reap_verdict(metadata=owned_elsewhere, this_host="HOST-A",
                                   elapsed_seconds=1900, timeout_seconds=1800, pid_alive=None)
    past = run_claim.reap_verdict(metadata=owned_elsewhere, this_host="HOST-A",
                                  elapsed_seconds=1800 + run_claim.FOREIGN_HOST_GRACE_SECONDS,
                                  timeout_seconds=1800, pid_alive=None)

    assert not within.reap
    assert past.reap


def test_a_row_with_no_claim_falls_back_to_age_exactly_as_before():
    """Rows written by an older build. Changing their answer would reap or keep them differently on
    the first run after an upgrade, which is not a change anybody asked for."""
    assert run_claim.reap_verdict(metadata={}, this_host="HOST-A", elapsed_seconds=2000,
                                  timeout_seconds=1800, pid_alive=None).reap
    assert not run_claim.reap_verdict(metadata={}, this_host="HOST-A", elapsed_seconds=100,
                                      timeout_seconds=1800, pid_alive=None).reap


def test_a_service_with_no_timeout_is_never_reaped_on_age():
    """`timeout: 0` is how a long-running service declares itself. Only a dead pid frees it."""
    assert not run_claim.reap_verdict(metadata={}, this_host="HOST-A", elapsed_seconds=10 ** 7,
                                      timeout_seconds=0, pid_alive=None).reap


# --------------------------------------------------------------------------- #
# Startup knows one more thing than the periodic sweep
# --------------------------------------------------------------------------- #
def test_a_row_left_by_a_stopped_daemon_is_freed_at_startup_not_at_its_timeout():
    """Measured on 2026-09-19: a restart left APP-SQL_TASKS blocked for 30 minutes and APP-METRICS
    for 40, on rows written before pids were recorded, whose processes had been gone the whole
    time. A daemon that has just started owns no children, so an open row on this host is over."""
    verdict = run_claim.startup_verdict(
        metadata={}, this_host="HOST-A", elapsed_seconds=60, timeout_seconds=1800, pid_alive=None)

    assert verdict.reap
    assert "owns no children" in verdict.reason


def test_a_child_that_outlived_its_daemon_keeps_its_row_at_startup():
    """The one case the extra fact does not cover: an orphan is still working, and closing its row
    would let the new daemon start a second copy beside it."""
    verdict = run_claim.startup_verdict(
        metadata=run_claim.claim_fields(pid=4242, host="HOST-A"),
        this_host="HOST-A", elapsed_seconds=60, timeout_seconds=1800, pid_alive=True)

    assert not verdict.reap
    assert "outlived its daemon" in verdict.reason


def test_startup_does_not_take_another_host_s_row_early():
    """Restarting here says nothing about a process over there."""
    verdict = run_claim.startup_verdict(
        metadata=run_claim.claim_fields(pid=4242, host="HOST-B"),
        this_host="HOST-A", elapsed_seconds=60, timeout_seconds=1800, pid_alive=None)

    assert not verdict.reap


def test_the_periodic_sweep_is_still_patient_with_a_row_it_cannot_check():
    """The difference between the two rules, stated: mid-run, a row with no pid may well belong to
    something that is working, and only its timeout can say otherwise."""
    assert not run_claim.reap_verdict(metadata={}, this_host="HOST-A", elapsed_seconds=60,
                                      timeout_seconds=1800, pid_alive=None).reap


# --------------------------------------------------------------------------- #
# The daemon's half of the field
# --------------------------------------------------------------------------- #
def test_the_daemon_counts_its_own_processes_per_command():
    from db_ops.jobs import daemon

    class Fake:
        def __init__(self, app_command_id):
            self.app_command = type("C", (), {"app_command_id": app_command_id})()

    running = {
        daemon.running_slot_key("APP-SQL_TASKS", 1): Fake("APP-SQL_TASKS"),
        daemon.running_slot_key("APP-SQL_TASKS", 2): Fake("APP-SQL_TASKS"),
        daemon.running_slot_key("APP-METRICS", 3): Fake("APP-METRICS"),
    }

    assert daemon.count_in_flight(running, "APP-SQL_TASKS") == 2
    assert daemon.count_in_flight(running, "APP-METRICS") == 1
    assert daemon.count_in_flight(running, "APP-CONTROL") == 0


def test_two_runs_of_one_command_can_be_held_at_once():
    """A dict keyed by app_command_id cannot hold two, which is why the slot key exists."""
    from db_ops.jobs import daemon

    assert daemon.running_slot_key("APP-SQL_TASKS", 1) != daemon.running_slot_key("APP-SQL_TASKS", 2)


# --------------------------------------------------------------------------- #
# The gate that made the field a no-op
# --------------------------------------------------------------------------- #
def test_an_async_command_is_due_again_while_its_own_run_is_still_going():
    """Found by a probe, not by reading. Two tasks that hold a session - one for a minute, one for
    two - were registered and watched: they ran strictly one after the other, and only one scan was
    ever in flight. `run_mode: async` was set and had no effect.

    The in-memory gate had been changed; the **store-side due check** had not, and it answered
    "running within timeout" for the command's own run. So the daemon never got as far as asking
    whether it might start another."""
    from datetime import datetime, timezone

    from db_ops.jobs import daemon
    from db_ops.lib import time_window as tw

    started = datetime(2026, 9, 19, 13, 31, 37, tzinfo=timezone.utc)
    row = {"started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "running",
           "finished_at": None}
    window = tw.TimeWindow(repeat_interval=1, timeout=1800)
    now = datetime(2026, 9, 19, 13, 33, 0, tzinfo=timezone.utc)

    class Command:
        time_window = window
        run_mode = run_mode.parse({"run_mode": "async", "max_parallel": 4})

    class SyncCommand:
        time_window = window
        run_mode = run_mode.parse({})

    assert daemon.app_command_due(Command(), row, now).due, "async: its own run does not block it"
    assert not daemon.app_command_due(SyncCommand(), row, now).due, "sync is unchanged"


def test_the_interval_still_applies_to_an_async_command():
    """`running_blocks=False` skips the wait-for-it-to-finish branch and nothing else. A command
    due every hour does not become due every second by being async."""
    from datetime import datetime, timezone

    from db_ops.jobs import daemon
    from db_ops.lib import time_window as tw

    started = datetime(2026, 9, 19, 13, 0, 0, tzinfo=timezone.utc)
    row = {"started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "running",
           "finished_at": None}

    class Command:
        time_window = tw.TimeWindow(repeat_interval=3600, timeout=7200)
        run_mode = run_mode.parse({"run_mode": "async"})

    verdict = daemon.app_command_due(Command(), row,
                                     datetime(2026, 9, 19, 13, 10, 0, tzinfo=timezone.utc))

    assert not verdict.due
    assert "interval not elapsed" in verdict.reason


def test_the_shared_rule_still_blocks_on_a_running_row_by_default():
    """Every other caller - sql_tasks, backup_restore, metrics - must be untouched by this."""
    from datetime import datetime, timezone

    from db_ops.lib import time_window as tw

    row = {"started_at": "2026-09-19T13:00:00Z", "status": "running", "finished_at": None}
    verdict = tw.due_from_row(time_window=tw.TimeWindow(repeat_interval=10, timeout=1800), row=row,
                              now=datetime(2026, 9, 19, 13, 5, 0, tzinfo=timezone.utc))

    assert not verdict.due
    assert "running within timeout" in verdict.reason
