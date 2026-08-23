""""Run now" has to reach the daemon, run once, and never fire at a moment nobody chose.

The console cannot start an app itself — it runs in a different process from the daemon, which
owns the working directory, the log scope, the forwarded key and the timeout reaper. So the button
writes a request and the daemon acts on it, and these are the properties that makes safe:

* **A request runs the app once.** Two presses queue one run; the second is told it is already
  queued. The database enforces it, not the button.
* **A request overrides the schedule but never a run in flight.** "Run it now" is asked outside
  the allowed hours and right after the last run — that is the whole point — but starting a second
  copy of something mid-flight is how two collectors write the same metric run.
* **A request that nobody could act on expires.** If the daemon was down, firing hours later when
  it comes back would run the app at a time nobody chose.
* **The run is ordinary.** It writes the same ``job_runs`` row every scheduled run writes, with
  the requester stamped on it, so the dashboard, the control app's summary and the Telegram alert
  keep agreeing about what ran.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from db_ops.config import DbOpsConfig
from db_ops.db import DbOpsStore
from db_ops.db.run_requests import (
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_STARTED,
    RunRequestStore,
    utc_now,
    utc_text,
)
from db_ops.jobs import daemon

#: An hour range that is closed *now*, wherever and whenever this runs.
#:
#: The window used to be a literal 03:00-04:00, which made "not due" a claim about the clock on the
#: machine running the suite. It held on a developer's laptop in UTC+7 and was false on a CI runner
#: in UTC for one hour a day — and on 2026-08-23 a push landed at 03:26 UTC and two tests failed
#: for a reason that had nothing to do with the change in them. A fixture that says "emphatically
#: not due" has to be true at 3am.
_CLOSED_FROM_HOUR = (datetime.now().hour + 6) % 24
_CLOSED_TO_HOUR = (datetime.now().hour + 7) % 24

#: An app command that is emphatically *not* due: it repeats daily, and only inside a window that
#: is shut right now. Anything that starts it in a test can only have come from the request queue.
NOT_DUE_COMMAND = {
    "app_command_id": "APP-TEST",
    "app_code": "APP-TEST",
    "app_name": "test",
    "display_name": "Test command",
    "command_text": f"{sys.executable} -c \"print('ran')\"",
    "working_dir": ".",
    "active": True,
    "node_role": "worker",
    "log_scope": "app",
    "app_ord": 1,
    "time_window": {"repeat_interval": 86400, "retry_interval": 60, "timeout": 60,
                    "from_hour": _CLOSED_FROM_HOUR, "to_hour": _CLOSED_TO_HOUR},
}


@pytest.fixture()
def queue(tmp_path: Path) -> RunRequestStore:
    return RunRequestStore(tmp_path / "db_ops.sqlite")


@pytest.fixture()
def estate(tmp_path: Path):
    """A daemon's world: a store, a data dir with one never-due command, and a running dict."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "app_commands.json").write_text(
        json.dumps({"app_commands": [NOT_DUE_COMMAND]}), encoding="utf-8")
    store_path = tmp_path / "db_ops.sqlite"
    # node_role matters: the command declares "worker", and a daemon running as master filters
    # it out before the queue is ever consulted.
    config = DbOpsConfig(sqlite_path=store_path, log_dir=tmp_path / "logs",
                         runtime_dir=tmp_path / "runtime", node_role="worker")
    store = DbOpsStore(store_path)
    store.initialize()
    return {"config": config, "store": store, "data": data, "running": {},
            "queue": RunRequestStore(store_path)}


def scan(estate) -> None:
    daemon.run_scheduler_scan(config=estate["config"], store=estate["store"],
                              data_dir=estate["data"], logger=None,
                              running_commands=estate["running"])


# --------------------------------------------------------------------------- #
# Queueing
# --------------------------------------------------------------------------- #
def test_a_request_is_recorded_with_who_asked(queue: RunRequestStore) -> None:
    answer = queue.request_run(app_command_id="APP-METRICS", requested_by="thanh",
                               source="console")
    assert answer["created"] is True
    row = queue.list_requests()[0]
    assert row["app_command_id"] == "APP-METRICS"
    assert row["requested_by"] == "thanh"
    assert row["request_source"] == "console"
    assert row["status"] == STATUS_PENDING


def test_asking_twice_queues_one_run(queue: RunRequestStore) -> None:
    """A double-click must not run the app twice, and must not be an error either."""
    first = queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    second = queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    assert second["created"] is False
    assert second["request_id"] == first["request_id"]
    assert len(queue.list_requests()) == 1


def test_the_database_refuses_a_second_pending_row(queue: RunRequestStore) -> None:
    """The one-at-a-time rule is the partial unique index, not a check in the caller."""
    queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    with pytest.raises(sqlite3.IntegrityError):
        with queue.connect() as conn:
            conn.execute(
                "INSERT INTO app_command_requests (app_command_id, status, requested_at) "
                "VALUES (?, ?, ?)",
                ("APP-METRICS", STATUS_PENDING, utc_text(utc_now())),
            )


def test_two_different_apps_can_be_queued_at_once(queue: RunRequestStore) -> None:
    queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    queue.request_run(app_command_id="APP-TELEGRAM", requested_by="thanh")
    assert len(queue.open_requests()) == 2


def test_a_pending_request_can_be_cancelled_and_a_claimed_one_cannot(queue: RunRequestStore) -> None:
    answer = queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    assert queue.cancel_request(answer["request_id"], actor="thanh") is True
    assert queue.list_requests()[0]["status"] == STATUS_CANCELLED

    again = queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    queue.claim("APP-METRICS")
    assert queue.cancel_request(again["request_id"], actor="thanh") is False, (
        "the daemon is already acting on it; cancelling would leave it half-run")


def test_a_cancelled_key_can_be_queued_again(queue: RunRequestStore) -> None:
    """Cancelling frees the app for a new request — the partial index only covers pending rows."""
    first = queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    queue.cancel_request(first["request_id"], actor="thanh")
    second = queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    assert second["created"] is True and second["request_id"] != first["request_id"]


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #
def test_a_request_nobody_picked_up_expires_instead_of_firing_late(queue: RunRequestStore) -> None:
    """The daemon-was-down case. Running it hours later surprises whoever is on shift."""
    answer = queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    with queue.connect() as conn:
        conn.execute("UPDATE app_command_requests SET requested_at = ? WHERE request_id = ?",
                     (utc_text(utc_now() - timedelta(hours=6)), answer["request_id"]))

    assert queue.claim("APP-METRICS") is None
    assert queue.list_requests()[0]["status"] == STATUS_EXPIRED


def test_expire_stale_reports_how_many_it_retired(queue: RunRequestStore) -> None:
    queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    queue.request_run(app_command_id="APP-TELEGRAM", requested_by="thanh")
    with queue.connect() as conn:
        conn.execute("UPDATE app_command_requests SET requested_at = ?",
                     (utc_text(utc_now() - timedelta(hours=6)),))
    assert queue.expire_stale() == 2
    assert queue.open_requests() == {}


def test_claiming_is_exclusive(queue: RunRequestStore) -> None:
    """Two daemons on one store must not both come away with the same request."""
    queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    assert queue.claim("APP-METRICS") is not None
    assert queue.claim("APP-METRICS") is None


def test_a_released_request_goes_back_to_pending(queue: RunRequestStore) -> None:
    """A spawn that failed must not leave the console showing "queued" forever."""
    queue.request_run(app_command_id="APP-METRICS", requested_by="thanh")
    claimed = queue.claim("APP-METRICS")
    queue.release(int(claimed["request_id"]), note="Start failed.")
    assert queue.pending_for("APP-METRICS") is not None


# --------------------------------------------------------------------------- #
# The daemon acts on it
# --------------------------------------------------------------------------- #
def test_without_a_request_a_command_that_is_not_due_does_not_run(estate) -> None:
    scan(estate)
    assert estate["running"] == {}


def test_a_request_runs_a_command_that_the_schedule_would_not_have(estate) -> None:
    """It overrides both gates on purpose: the interval and the allowed-hours window."""
    estate["queue"].request_run(app_command_id="APP-TEST", requested_by="thanh")
    scan(estate)
    assert "APP-TEST" in estate["running"]


def test_the_run_records_who_asked_for_it(estate) -> None:
    """Otherwise "why did this run at 03:00" is unanswerable from job_runs alone."""
    estate["queue"].request_run(app_command_id="APP-TEST", requested_by="thanh")
    scan(estate)

    run = estate["store"].fetch_latest_job_runs_by_job_code()["APP-TEST"]
    metadata = json.loads(run["metadata_json"])
    assert metadata["requested_by"] == "thanh"
    assert metadata["run_request_id"] == 1
    assert "on request from thanh" in run["message"]


def test_the_requester_survives_the_run_finishing(estate) -> None:
    """The finish rewrites the row's metadata; leaving the request off there erases the evidence.

    A completed requested run then looks exactly like a scheduled one, and the question the stamp
    exists to answer — why did this run outside its window — stops being answerable the moment the
    run succeeds.
    """
    estate["queue"].request_run(app_command_id="APP-TEST", requested_by="thanh")
    scan(estate)
    estate["running"]["APP-TEST"].process.wait(timeout=30)
    scan(estate)  # reaps it

    run = estate["store"].fetch_latest_job_runs_by_job_code()["APP-TEST"]
    metadata = json.loads(run["metadata_json"])
    assert run["status"] == "done"
    assert metadata["requested_by"] == "thanh"
    assert metadata["run_request_id"] == 1


def test_a_scheduled_run_carries_no_requester(estate) -> None:
    """The stamp has to mean something: a run nobody asked for must not claim one."""
    from db_ops.jobs.daemon import start_app_command, load_app_commands

    command = load_app_commands(estate["data"] / "app_commands.json")["APP-TEST"]
    start_app_command(config=estate["config"], store=estate["store"], data_dir=estate["data"],
                      logger=None, running_commands=estate["running"], app_command=command)
    run = estate["store"].fetch_latest_job_runs_by_job_code()["APP-TEST"]
    assert "requested_by" not in json.loads(run["metadata_json"])


def test_the_request_is_linked_to_the_run_it_produced(estate) -> None:
    estate["queue"].request_run(app_command_id="APP-TEST", requested_by="thanh")
    scan(estate)

    row = estate["queue"].list_requests()[0]
    assert row["status"] == STATUS_STARTED
    assert row["job_run_id"] is not None
    run = estate["store"].fetch_latest_job_runs_by_job_code()["APP-TEST"]
    assert int(row["job_run_id"]) == int(run["log_id"])


def test_a_request_does_not_start_a_second_copy_of_a_running_command(estate) -> None:
    """The one gate a request must not override."""
    estate["queue"].request_run(app_command_id="APP-TEST", requested_by="thanh")
    scan(estate)
    first = estate["running"]["APP-TEST"].process.pid

    estate["queue"].request_run(app_command_id="APP-TEST", requested_by="thanh")
    scan(estate)
    assert estate["running"]["APP-TEST"].process.pid == first
    assert estate["queue"].pending_for("APP-TEST") is not None, (
        "the request should still be waiting, not consumed by a run that never happened")


def test_a_second_scan_does_not_run_it_again(estate) -> None:
    """The claim is what makes the request one-shot rather than a standing instruction."""
    estate["queue"].request_run(app_command_id="APP-TEST", requested_by="thanh")
    scan(estate)
    estate["running"].clear()  # pretend it finished and was reaped
    scan(estate)
    assert estate["running"] == {}


def test_a_queue_that_cannot_be_read_does_not_stop_the_schedule(estate, monkeypatch) -> None:
    """The scheduled work is the whole estate's monitoring; a broken extra must not take it down."""
    def explode(*args, **kwargs):
        raise RuntimeError("queue table is gone")

    monkeypatch.setattr(daemon, "_run_request_store", explode)
    scan(estate)  # must not raise
    assert estate["running"] == {}
