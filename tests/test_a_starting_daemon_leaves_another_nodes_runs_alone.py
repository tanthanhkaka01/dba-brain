"""A daemon that starts closes the runs ITS node left - never another node's live ones.

On 2026-09-30 at 08:41:56Z a daemon was started on the master PC against the worker's store. Its
start-up recovery closed three of the worker's rows as `timeout` the same second: the web host the
worker was serving, and an `APP-SQL_TASKS` and an `APP-BACKUP-RESTORE` run started on the worker two
seconds earlier. `worker-status` then read the web host as timed out for five days while it served
(0.27.0 item 1.96).

Three things let it happen, and each is held here:

* the rows were written by a build that keeps `pid` in the metadata and the host only in the row's
  `host_name` column - read without that column, a row of another host was "pid N is gone";
* a service (timeout 0) was closed at start-up whatever the verdict said;
* the recovery reconciled every command in the file, including the ones only a worker runs.

What must not change: this node's own leftovers are still freed at once - a dead pid, a row left
under its old host name, a service its own daemon left open.

The two sweeps that run while the daemon is up - the SQL-task one and the backup/restore one - read
the owner the same way since 0.27.0: until then they left the column out (the SQL-task sweep's
query did not even select it), so a row of that shape was judged on age alone there and on its
owner at start-up.
"""

from __future__ import annotations

import dataclasses
import json
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from db_ops.backup_restore import schedule
from db_ops.db import DbOpsStore
from db_ops.jobs import daemon
from db_ops.lib import run_claim
from db_ops.sql_tasks import runner

from test_jobs_daemon import FakeStore, _make_app_command, _make_row
from tests.test_sql_task_claims import FakeLogger, RecordingSqlRunStore, sql_command, sql_target

WORKER_HOST = "5b5682f96e0c"


def _service(app_command_id="APP-WEBHOST", node_role="worker"):
    return dataclasses.replace(_make_app_command(app_command_id, timeout=0), node_role=node_role)


def _job(app_command_id="APP-SQL_TASKS", node_role="worker"):
    return dataclasses.replace(_make_app_command(app_command_id, timeout=3600), node_role=node_role)


def _worker_row(seconds_ago, log_id, pid):
    row = _make_row("running", started_seconds_ago=seconds_ago, log_id=log_id)
    row["host_name"] = WORKER_HOST
    row["metadata_json"] = json.dumps({"pid": pid})   # the older shape: host only in the column
    return row


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #
def test_a_row_naming_its_host_only_in_the_column_is_that_hosts():
    """The 08:41:54 row: two seconds old, pid in the metadata, host in the column."""
    verdict = run_claim.startup_verdict(
        metadata={"pid": 4242}, this_host="DB-THANH", elapsed_seconds=2, timeout_seconds=3600,
        pid_alive=None, host_fallback=WORKER_HOST)

    assert not verdict.reap, verdict.reason


def test_another_hosts_service_is_never_reaped_on_age():
    verdict = run_claim.reap_verdict(
        metadata=run_claim.claim_fields(pid=7, host=WORKER_HOST), this_host="DB-THANH",
        elapsed_seconds=10 * 86400, timeout_seconds=0, pid_alive=None)

    assert not verdict.reap
    assert "service" in verdict.reason


def test_another_hosts_job_still_goes_after_its_timeout_and_grace():
    """Only the service rule is new: a job of another host is judged on age as before."""
    verdict = run_claim.reap_verdict(
        metadata=run_claim.claim_fields(pid=7, host=WORKER_HOST), this_host="DB-THANH",
        elapsed_seconds=3600 + run_claim.FOREIGN_HOST_GRACE_SECONDS + 1, timeout_seconds=3600,
        pid_alive=None)

    assert verdict.reap


# --------------------------------------------------------------------------- #
# The start-up recovery
# --------------------------------------------------------------------------- #
def test_a_master_starting_on_the_workers_store_closes_none_of_its_runs():
    """The 2026-09-30 shape, whole: the worker's web host and a run two seconds old."""
    commands = {"APP-WEBHOST": _service(), "APP-SQL_TASKS": _job()}
    store = FakeStore(latest={"APP-WEBHOST": _worker_row(1899, 1, pid=31),
                              "APP-SQL_TASKS": _worker_row(2, 2, pid=4242)})

    daemon.recover_stale_running_jobs(store=store, app_commands=commands, config=SimpleNamespace(),
                                      logger=None, node_role="master")

    assert store.updated == []


def test_even_on_a_node_of_the_same_role_another_hosts_runs_are_held():
    """Two workers on one store is a fault of its own (one scheduler per estate); it must still
    not end in one of them closing the other's live service."""
    commands = {"APP-WEBHOST": _service(), "APP-SQL_TASKS": _job()}
    store = FakeStore(latest={"APP-WEBHOST": _worker_row(1899, 1, pid=31),
                              "APP-SQL_TASKS": _worker_row(2, 2, pid=4242)})

    daemon.recover_stale_running_jobs(store=store, app_commands=commands, config=SimpleNamespace(),
                                      logger=None, node_role="worker")

    assert store.updated == []


def test_this_hosts_own_service_left_open_is_still_closed_at_start_up():
    """The rule the service exception was for: its daemon is gone, so its row is closed - as
    `timeout`, so the web host is started again."""
    row = _make_row("running", started_seconds_ago=1899, log_id=5)
    row["host_name"] = socket.gethostname()
    store = FakeStore(latest={"APP-WEBHOST": row})

    daemon.recover_stale_running_jobs(store=store, app_commands={"APP-WEBHOST": _service()},
                                      config=SimpleNamespace(), logger=None, node_role="worker")

    assert [update["log_id"] for update in store.updated] == [5]
    assert store.updated[0]["status"] == "timeout"


def test_a_removed_commands_row_claimed_by_another_node_is_left_to_it():
    row = _make_row("running", started_seconds_ago=60, log_id=9)
    row["host_name"] = WORKER_HOST
    row["metadata_json"] = json.dumps({
        "app_command_id": "APP-ONLY-ON-THE-WORKER",
        **run_claim.claim_fields(pid=7, host=WORKER_HOST, node="0123456789abcdef0123456789abcdef")})
    store = FakeStore(latest={"APP-ONLY-ON-THE-WORKER": row})

    daemon.recover_stale_running_jobs(store=store, app_commands={}, config=SimpleNamespace(),
                                      logger=None, node_role="master")

    assert store.updated == []


# --------------------------------------------------------------------------- #
# The sweeps read the same column
# --------------------------------------------------------------------------- #
THIS_HOST = "DB-THANH"


def _stamp(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _RunningJobRows:
    """The two calls the backup/restore sweep makes on the store."""

    def __init__(self, rows):
        self.rows, self.updated = rows, []

    def fetch_running_job_runs(self, job_code_prefix=""):
        return [row for row in self.rows if str(row["job_code"]).startswith(job_code_prefix)]

    def update_job_run(self, **kwargs):
        self.updated.append(kwargs)
        return True


def _restore_row(*, host: str, seconds_ago: int) -> dict:
    return {"log_id": 3, "job_code": schedule.restore_job_code("R1"), "status": "running",
            "host_name": host, "started_at": _stamp(seconds_ago), "created_at": _stamp(seconds_ago),
            "metadata_json": json.dumps({"pid": 4242, "restore_id": "R1"})}


def test_the_restore_sweep_holds_another_hosts_run_inside_its_grace(monkeypatch):
    """Past its timeout by a minute, on the worker: this host cannot see that pid, so it waits the
    grace. Read without the column it was a row with no owner, closed on age."""
    monkeypatch.setattr(schedule.socket, "gethostname", lambda: THIS_HOST)
    store = _RunningJobRows([_restore_row(host=WORKER_HOST, seconds_ago=7260)])

    assert schedule.reap_stale_runs(store=store, timeouts={schedule.restore_job_code("R1"): 7200}) == []
    assert store.updated == []


def test_the_restore_sweep_frees_this_hosts_dead_run_at_once(monkeypatch):
    monkeypatch.setattr(schedule.socket, "gethostname", lambda: THIS_HOST)
    monkeypatch.setattr(schedule.process_liveness, "is_pid_alive", lambda pid: False)
    store = _RunningJobRows([_restore_row(host=THIS_HOST, seconds_ago=60)])

    reaped = schedule.reap_stale_runs(store=store, timeouts={schedule.restore_job_code("R1"): 7200})

    assert [item["restore_id"] for item in reaped] == ["R1"]
    assert "pid 4242 is gone" in store.updated[0]["message"]


def test_the_sql_task_sweep_frees_this_hosts_dead_run_at_once(monkeypatch):
    """Inside its timeout, its pid gone on this host: freed now, not at the timeout."""
    monkeypatch.setattr(runner.socket, "gethostname", lambda: THIS_HOST)
    monkeypatch.setattr(runner.process_liveness, "is_pid_alive", lambda pid: False)
    store = RecordingSqlRunStore()
    row = {"sql_run_id": 77, "run_key": "9|1|server|sqlserver|svc|inst|APPDB", "sql_id": 9,
           "sql_code": "SQLSERVER-009", "target_no": 1, "status": "running",
           "started_at": _stamp(60), "finished_at": None, "created_at": _stamp(60),
           "host_name": THIS_HOST, "metadata_json": {"pid": 4242}}

    runner.mark_stale_running_sql_runs(
        store=store, commands={9: sql_command()},
        targets=[sql_target(timeout=7200, alert_on_error=runner.NotifyRule(enabled=False, telegram_chat=""))],
        running_runs=[row], telegram_groups={}, logger=FakeLogger())

    assert "pid 4242 is gone" in store.updated[0]["error_text"]


def test_the_running_sql_runs_carry_their_host_column(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    store.insert_sql_run(
        run_key="9|1|server|sqlserver|svc|inst|APPDB", sql_id=9, sql_code="SQLSERVER-009",
        target_no=1, server_id="server", db_type="sqlserver", service_name="svc",
        instance_name="inst", database_name="APPDB", credential_name="cred", status="running",
        level="logging", message="started")

    (row,) = store.fetch_running_sql_runs()

    assert run_claim.row_host(row) == socket.gethostname()


def test_a_row_without_the_column_reads_as_no_host():
    assert run_claim.row_host({"metadata_json": "{}"}) == ""
    assert run_claim.row_host(None) == ""
