"""A run left `running` by a task that is no longer configured is closed - when this node left it.

The sweep closes abandoned runs of the tasks it scans, and a task removed from ``sql_commands.json``
is never scanned again: on the 0.26 soak node's store three September runs of ``SQLSERVER-030`` to
``-032`` were still ``running`` two weeks later, closed by hand (1.86). They held nothing - the task
did not exist - but a ``running`` row past its timeout is what a soak counts as a hung run.

Such a row is now swept like any other, with the default timeout, **only when this node left it**:
a store can be shared, and a task this node does not configure may be running on another node, with
a timeout this node cannot know.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db_ops.lib import run_claim
from db_ops.sql_tasks import runner
from tests.test_sql_task_claims import FakeLogger, RecordingSqlRunStore, sql_command, sql_target

HOST = "this-node"


def _row(*, age_seconds: int, sql_id: int = 30, **claim) -> dict:
    started_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"sql_run_id": 3846, "run_key": f"{sql_id}|1|server|sqlserver|svc|inst|APPDB",
            "sql_id": sql_id, "sql_code": f"SQLSERVER-0{sql_id}", "target_no": 1, "status": "running",
            "started_at": started_at, "finished_at": None, "created_at": started_at,
            "metadata_json": run_claim.claim_fields(**claim)}


def _sweep(monkeypatch, rows, *, alive: bool = False, node: str = "node-a"):
    store = RecordingSqlRunStore()
    monkeypatch.setattr(runner.socket, "gethostname", lambda: HOST)
    monkeypatch.setattr(runner.process_liveness, "is_pid_alive", lambda pid: alive)
    monkeypatch.setattr(runner.process_liveness, "stop_process_and_children", lambda pid, *, started: True)
    monkeypatch.setattr(runner.node_identity, "current", lambda: node)
    # sql_id 9 is configured; the rows below are of sql_id 30, which is not.
    runner.mark_stale_running_sql_runs(
        store=store, commands={9: sql_command()}, targets=[sql_target()], running_runs=rows,
        telegram_groups={}, logger=FakeLogger())
    return store


def test_a_run_this_host_left_of_a_removed_task_is_closed_once_its_process_is_gone(monkeypatch):
    store = _sweep(monkeypatch, [_row(age_seconds=14 * 86400, pid=4321, host=HOST)])

    (closed,) = store.updated
    assert closed["status"] == "error" and closed["only_if_status"] == "running"
    assert "SQLSERVER-030 stale running" in closed["error_text"]
    assert "no longer in sql_commands.json" in closed["error_text"]


def test_a_run_this_node_left_under_another_host_name_is_closed(monkeypatch):
    """A recreated container keeps its node identity and gets a new host name."""
    store = _sweep(monkeypatch, [_row(age_seconds=14 * 86400, pid=67, host="old-container", node="node-a")],
                   node="node-a")

    assert len(store.updated) == 1


def test_another_node_s_run_of_a_task_this_node_does_not_configure_is_left_alone(monkeypatch):
    store = _sweep(monkeypatch, [_row(age_seconds=14 * 86400, pid=4321, host="other-host", node="node-b")],
                   node="node-a")

    assert store.updated == []


def test_a_live_run_of_a_removed_task_inside_the_default_timeout_is_held(monkeypatch):
    store = _sweep(monkeypatch, [_row(age_seconds=60, pid=4321, host=HOST)], alive=True)

    assert store.updated == []


def test_a_configured_task_is_swept_as_before(monkeypatch):
    store = _sweep(monkeypatch, [_row(age_seconds=14 * 86400, sql_id=9, pid=4321, host=HOST)])

    (closed,) = store.updated
    assert "no longer in sql_commands.json" not in closed["error_text"]
