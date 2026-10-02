"""A SQL task run past its timeout is over: closed as an error by timeout, its process stopped.

On 2026-09-30 the third target of a nightly engine task logged its start at 02:00 and nothing after
it for 13 hours, until the container was stopped; two days earlier one had sat for 35. The row said
``running``, its process was alive, and the rule was that a row whose process is alive is left alone
however old it is - because the row is the claim, and closing it under a process still working
starts a second copy on top of the first. So the target did not run again, and nothing said why.

The operator's rule (2026-10-02, 0.26.0 §1.70): *before that SQL runs, look for a run of the same
sql_id still ``running`` past its timeout, and mark it an error by timeout.* The timeout is the
answer again. The second copy is prevented the other way: **the owner is stopped before the row is
closed** - and only a process that still carries the start time its claim recorded, never whoever
holds the pid now.

It is the SQL-task reaper's rule only. A restore and an app command keep the old one: the daemon
stops those at their timeout itself, and a restore outliving its estimate is not an error.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from db_ops.db import DbOpsStore
from db_ops.lib import process_liveness, run_claim
from db_ops.sql_tasks import runner
from tests.test_sql_task_claims import FakeLogger, RecordingSqlRunStore, sql_command, sql_target

HOST = "this-node"


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
def _verdict(*, elapsed: float, timeout: float, alive: bool | None = True, host: str = HOST,
             at_timeout: bool = True) -> run_claim.ReapVerdict:
    return run_claim.reap_verdict(
        metadata=run_claim.claim_fields(pid=4321, host=host), this_host=HOST,
        elapsed_seconds=elapsed, timeout_seconds=timeout, pid_alive=alive, at_timeout=at_timeout)


def test_a_run_whose_process_is_alive_is_closed_once_it_is_past_its_timeout():
    verdict = _verdict(elapsed=7300, timeout=7200)

    assert verdict.reap and verdict.timed_out
    assert verdict.reason == "7300s is past its timeout of 7200s"


def test_inside_its_timeout_a_live_run_is_held_as_before():
    verdict = _verdict(elapsed=7100, timeout=7200)

    assert not verdict.reap and not verdict.timed_out


def test_a_task_that_declares_no_timeout_is_never_closed_on_age():
    assert not _verdict(elapsed=10 ** 7, timeout=0).reap


def test_a_dead_process_is_still_freed_at_once_and_is_not_a_timeout():
    """"The process is gone" is the more useful sentence, and there is nothing left to stop."""
    verdict = _verdict(elapsed=5, timeout=7200, alive=False)

    assert verdict.reap and not verdict.timed_out and "is gone" in verdict.reason


def test_another_host_s_run_is_closed_at_its_timeout_too():
    """It waited for the timeout plus an hour of grace; past its timeout it is over, like any other."""
    verdict = _verdict(elapsed=7300, timeout=7200, alive=None, host="another-node")

    assert verdict.reap and verdict.timed_out


def test_a_restore_or_an_app_command_keeps_the_old_rule():
    """Only the SQL-task reaper asks ``at_timeout``; every other caller is answered as before."""
    assert not _verdict(elapsed=10 ** 6, timeout=7200, at_timeout=False).reap


# --------------------------------------------------------------------------- #
# The sweep: stop the owner, then close the row, then say so
# --------------------------------------------------------------------------- #
def _overdue_row(*, age_seconds: int = 7300, **claim) -> list[dict]:
    started_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{
        "sql_run_id": 77, "run_key": "9|1|server|sqlserver|svc|inst|APPDB", "sql_id": 9,
        "sql_code": "SQLSERVER-009", "target_no": 1, "status": "running", "started_at": started_at,
        "finished_at": None, "created_at": started_at,
        "metadata_json": run_claim.claim_fields(**claim) if claim else {},
    }]


def _sweep(monkeypatch, rows, *, timeout: int = 7200, alive: bool = True, stopped: bool = True,
           alert: bool = True):
    store = RecordingSqlRunStore()
    order: list[str] = []
    real_update = store.update_sql_run
    store.update_sql_run = lambda **kwargs: (order.append("closed"), real_update(**kwargs))[1]
    monkeypatch.setattr(runner.socket, "gethostname", lambda: HOST)
    monkeypatch.setattr(runner.process_liveness, "is_pid_alive", lambda pid: alive)
    monkeypatch.setattr(runner.process_liveness, "stop_process_and_children",
                        lambda pid, *, started: (order.append(f"stopped {pid} {started}"), stopped)[1])
    target = sql_target(timeout=timeout, alert_on_error=runner.NotifyRule(
        enabled=alert, telegram_chat="sql"))
    runner.mark_stale_running_sql_runs(
        store=store, commands={9: sql_command()}, targets=[target], running_runs=rows,
        telegram_groups={"sql": "chat-7"}, logger=FakeLogger())
    return store, order


def test_the_thirteen_hour_run_is_closed_as_an_error_by_timeout(monkeypatch):
    rows = _overdue_row(age_seconds=13 * 3600, pid=4321, host=HOST, started="133000000")

    store, order = _sweep(monkeypatch, rows)

    closed = store.updated[0]
    assert closed["status"] == "error" and closed["only_if_status"] == "running"
    assert "SQLSERVER-009 error by timeout" in closed["error_text"]
    assert "is past its timeout of 7200s" in closed["error_text"]
    assert "its process (pid 4321) was stopped" in closed["error_text"]
    assert closed["metadata"] == {"stale_running": True, "timed_out": True}
    assert order == ["stopped 4321 133000000", "closed"], \
        "the owner is stopped BEFORE the claim is released - never a second copy on top of the first"


def test_the_alert_names_the_timeout_and_warns_about_the_statement_on_the_server(monkeypatch):
    store, _order = _sweep(monkeypatch, _overdue_row(pid=4321, host=HOST, started="133000000"))

    text = store.messages[0]["message_text"]
    assert "error by timeout" in text and "timeout_seconds=7200" in text
    assert "may still be executing" in text, "stopping the process closes the connection, not the statement"


def test_a_live_run_inside_its_timeout_is_left_alone_and_nothing_is_stopped(monkeypatch):
    store, order = _sweep(monkeypatch, _overdue_row(age_seconds=60, pid=4321, host=HOST, started="1"))

    assert store.updated == [] and order == []


def test_a_claim_from_before_this_version_is_closed_and_its_process_left_with_a_word(monkeypatch):
    """No start time on the claim: the pid cannot be told from another process holding the number,
    so nothing is stopped - and the message says the process was left."""
    rows = _overdue_row(pid=4321, host=HOST)
    monkeypatch.setattr(runner.process_liveness, "process_start_marker", lambda pid: "999")

    store = RecordingSqlRunStore()
    monkeypatch.setattr(runner.socket, "gethostname", lambda: HOST)
    monkeypatch.setattr(runner.process_liveness, "is_pid_alive", lambda pid: True)
    runner.mark_stale_running_sql_runs(
        store=store, commands={9: sql_command()}, targets=[sql_target(timeout=7200)],
        running_runs=rows, telegram_groups={}, logger=FakeLogger())

    assert store.updated[0]["status"] == "error"
    assert "was left running - the claim records no start time" in store.updated[0]["error_text"]


@pytest.mark.parametrize("claim, alive, expected", [
    (dict(pid=4321, host="another-node"), True, "is on another-node and cannot be stopped from this-node"),
    (dict(pid=4321, host=HOST, started="1"), False, "had already ended"),
])
def test_what_was_done_about_the_owner_is_one_clause_of_the_message(monkeypatch, claim, alive, expected):
    metadata = run_claim.claim_fields(**claim)
    monkeypatch.setattr(runner.process_liveness, "is_pid_alive", lambda pid: alive)

    clause = runner.stop_overdue_owner(
        *run_claim.claim_owner(metadata), this_host=HOST, started=run_claim.claim_started(metadata))

    assert expected in clause


def test_a_pid_that_now_belongs_to_somebody_else_is_not_stopped(monkeypatch):
    monkeypatch.setattr(runner.process_liveness, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(runner.process_liveness, "stop_process_and_children", lambda pid, *, started: False)

    clause = runner.stop_overdue_owner(4321, HOST, this_host=HOST, started="133000000")

    assert "held by another process now" in clause and "nothing else was touched" in clause


# --------------------------------------------------------------------------- #
# A run asked for by hand sweeps the task it is about to run
# --------------------------------------------------------------------------- #
def test_a_run_by_hand_first_closes_that_task_s_overdue_runs(tmp_path, monkeypatch):
    swept: list[list[int]] = []

    class Store:
        def fetch_running_sql_runs(self):
            return [{"sql_id": 9, "sql_run_id": 1}, {"sql_id": 12, "sql_run_id": 2}]

    monkeypatch.setattr(runner, "load_sql_commands", lambda path, **_kw: {9: sql_command()})
    monkeypatch.setattr(runner, "load_sql_targets", lambda path, **_kw: [sql_target()])
    monkeypatch.setattr(runner, "bind_parameter_values", lambda command, values: values)
    monkeypatch.setattr(runner.data_sources, "load_secret_text", lambda data_dir: {})
    monkeypatch.setattr(runner.data_sources, "load_inventory", lambda data_dir: [])
    monkeypatch.setattr(runner.data_sources, "load_all_credentials", lambda data_dir: {})
    monkeypatch.setattr(runner, "mark_stale_running_sql_runs",
                        lambda **kwargs: swept.append([row["sql_run_id"] for row in kwargs["running_runs"]]))
    monkeypatch.setattr(runner, "run_one_sql_task", lambda **kwargs: (swept.append("ran"), True)[1])

    runner.run_sql_id_tasks(store=Store(), data_dir=tmp_path, sql_id=9, force=True, dry_run=False,
                            telegram_groups={}, logger=FakeLogger())

    assert swept == [[1], "ran"], "its own sql_id only, and before it runs"


# --------------------------------------------------------------------------- #
# The claim says when its process started
# --------------------------------------------------------------------------- #
def test_a_claim_records_when_its_process_started(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    store.insert_sql_run(
        run_key="9|1|server|sqlserver|svc|inst|APPDB", sql_id=9, sql_code="SQLSERVER-009",
        target_no=1, server_id="server", db_type="sqlserver", service_name="svc",
        instance_name="inst", database_name="APPDB", credential_name="cred", status="running",
        level="logging", message="started")

    (row,) = store.fetch_running_sql_runs()
    metadata = run_claim.row_metadata(row)

    assert run_claim.claim_owner(metadata)[0] == os.getpid()
    assert run_claim.claim_started(metadata) == process_liveness.process_start_marker(os.getpid())


def test_a_claim_with_no_start_time_reads_as_none():
    assert run_claim.claim_started(run_claim.claim_fields(pid=1, host=HOST)) == ""
    assert run_claim.STARTED_FIELD not in run_claim.claim_fields(pid=1, host=HOST)


# --------------------------------------------------------------------------- #
# Stopping a process this one did not start - real processes
# --------------------------------------------------------------------------- #
PARENT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n"
)


@pytest.fixture()
def run_with_a_child():
    """A stand-in for a scan and the ``run-sql`` child it waits on: ``(parent pid, child pid)``."""
    parent = subprocess.Popen([sys.executable, "-c", PARENT], stdout=subprocess.PIPE, text=True)
    child_pid = int(parent.stdout.readline().strip())
    try:
        yield parent.pid, child_pid
    finally:
        for pid in (child_pid, parent.pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        parent.wait(timeout=10)
        parent.stdout.close()


def _gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not process_liveness.is_pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_the_run_and_the_child_it_waits_on_are_both_stopped(run_with_a_child):
    parent_pid, child_pid = run_with_a_child
    started = process_liveness.process_start_marker(parent_pid)

    assert process_liveness.stop_process_and_children(parent_pid, started=started) is True
    assert _gone(parent_pid) and _gone(child_pid), "the statement's own process goes with the scan"


def test_a_pid_that_does_not_carry_the_recorded_start_time_is_not_touched(run_with_a_child):
    parent_pid, child_pid = run_with_a_child

    assert process_liveness.stop_process_and_children(parent_pid, started="1") is False
    assert process_liveness.stop_process_and_children(parent_pid, started=None) is False
    assert process_liveness.is_pid_alive(parent_pid) and process_liveness.is_pid_alive(child_pid)


def test_this_process_is_never_what_gets_stopped():
    mine = process_liveness.process_start_marker(os.getpid())

    assert process_liveness.stop_process_and_children(os.getpid(), started=mine) is False


def test_a_process_already_gone_counts_as_stopped():
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait(timeout=30)

    assert process_liveness.stop_process_and_children(finished.pid, started="1") is True
