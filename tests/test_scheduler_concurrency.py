import json
from datetime import datetime, timedelta, timezone

import pytest

from db_ops.lib.time_window import TimeWindow
from db_ops.jobs import daemon


class FakeConfig:
    def __init__(self, log_dir):
        self.log_dir = log_dir
        self.sqlite_path = log_dir / "db_ops.sqlite"



    @property
    def store(self):
        """SQLite store declaration matching this fake's sqlite_path (see db_ops.config)."""
        from db_ops.config import SqliteStoreConfig, StoreConfig
        from pathlib import Path as _Path

        return StoreConfig(sqlite=SqliteStoreConfig(path=_Path(str(self.sqlite_path))))

class FakeProcess:
    next_pid = 5000

    def __init__(self, returncode=None):
        FakeProcess.next_pid += 1
        self.pid = FakeProcess.next_pid
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class RaceStore:
    def __init__(self, *, stale_latest=None):
        self.inserted = []
        self.updated = []
        self.stale_latest = stale_latest or {}
        self.force_empty_latest = True

    def fetch_latest_job_runs_by_job_code(self):
        if self.force_empty_latest:
            return {}
        if self.stale_latest:
            return self.stale_latest
        return {
            row["job_code"]: {
                "started_at": row["started_at"],
                "finished_at": None,
                "created_at": row["started_at"],
                "status": row["status"],
            }
            for row in self.inserted
        }

    def insert_job_run(self, item):
        row = {
            "claimed_by": item.metadata.get("claimed_by") if item.metadata else None,
            "job_code": item.job_code,
            "status": item.status,
            "started_at": item.started_at,
        }
        self.inserted.append(row)
        return len(self.inserted)

    def update_job_run(self, **kwargs):
        self.updated.append(kwargs)


def write_app_commands(data_dir, commands):
    data_dir.mkdir()
    (data_dir / "app_commands.json").write_text(json.dumps({"app_commands": commands}), encoding="utf-8")


def app_command(app_command_id, *, log_scope=None, working_dir="."):
    return {
        "app_command_id": app_command_id,
        "app_name": app_command_id.lower(),
        "display_name": app_command_id,
        "log_scope": log_scope or app_command_id.lower().replace("app-", "").replace("-", "_"),
        "working_dir": working_dir,
        "command_text": "python -c \"print('ok')\"",
        "time_window": {
            "from_day": 1,
            "to_day": 31,
            "from_hour": 0,
            "to_hour": 23,
            "repeat_interval": 1,
            "timeout": 60,
        },
        "active": True,
    }


@pytest.mark.xfail(strict=True, reason="Design gap: app daemon has no durable cross-process app_command claim/lease.")
def test_two_daemons_cannot_claim_same_app_command(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-METRICS")])
    store = RaceStore()
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: FakeProcess(returncode=None))

    daemon.run_scheduler_scan(
        config=FakeConfig(tmp_path / "logs"),
        store=store,
        data_dir=data_dir,
        logger=None,
        running_commands={},
    )
    daemon.run_scheduler_scan(
        config=FakeConfig(tmp_path / "logs"),
        store=store,
        data_dir=data_dir,
        logger=None,
        running_commands={},
    )

    assert len(store.inserted) == 1
    assert store.inserted[0]["claimed_by"]
    assert store.inserted[0]["status"] == "running"


def test_different_task_same_process_runs_when_scopes_do_not_overlap(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(
        data_dir,
        [
            app_command("APP-DB-X", log_scope="db_x"),
            app_command("APP-DB-Y", log_scope="db_y"),
        ],
    )
    store = RaceStore()
    store.force_empty_latest = False
    running = {}
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: FakeProcess(returncode=None))

    daemon.run_scheduler_scan(
        config=FakeConfig(tmp_path / "logs"),
        store=store,
        data_dir=data_dir,
        logger=None,
        running_commands=running,
    )

    assert len(store.inserted) == 2
    assert set(running) == {"APP-DB-X", "APP-DB-Y"}
    assert {row["status"] for row in store.inserted} == {"running"}


def test_retry_after_fail_uses_retry_interval_without_replacing_repeat_interval():
    command = daemon.AppCommand(
        app_command_id="APP-METRICS",
        app_code="APP-METRICS",
        app_name="metrics",
        display_name="Metrics",
        log_scope="metrics",
        working_dir=".",
        command_text="python -c pass",
        time_window=TimeWindow(
            from_day=1,
            to_day=31,
            from_hour=0,
            to_hour=23,
            repeat_interval=60,
            retry_interval=20,
        ),
        active=True,
    )
    now = datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
    latest = {
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:05Z",
        "created_at": "2026-01-01T00:00:00Z",
        "status": "error",
    }

    assert daemon.app_command_is_due(command, latest, now=now) is False
    assert daemon.app_command_is_due(command, latest, now=now + timedelta(seconds=10)) is True

    command_with_slow_retry = daemon.AppCommand(
        app_command_id="APP-METRICS",
        app_code="APP-METRICS",
        app_name="metrics",
        display_name="Metrics",
        log_scope="metrics",
        working_dir=".",
        command_text="python -c pass",
        time_window=TimeWindow(
            from_day=1,
            to_day=31,
            from_hour=0,
            to_hour=23,
            repeat_interval=60,
            retry_interval=120,
        ),
        active=True,
    )
    assert daemon.app_command_is_due(
        command_with_slow_retry,
        latest,
        now=datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc),
    ) is True


@pytest.mark.xfail(strict=True, reason="Design gap: app daemon startup does not reconcile stale persisted RUNNING job_runs.")
def test_daemon_restart_marks_stale_running_app_before_new_run(tmp_path, monkeypatch):
    stale_started = (datetime.now(timezone.utc) - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-METRICS")])
    store = RaceStore(
        stale_latest={
            "APP-METRICS": {
                "log_id": 99,
                "started_at": stale_started,
                "finished_at": None,
                "created_at": stale_started,
                "status": "running",
            }
        }
    )
    store.force_empty_latest = False
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: FakeProcess(returncode=None))

    daemon.run_scheduler_scan(
        config=FakeConfig(tmp_path / "logs"),
        store=store,
        data_dir=data_dir,
        logger=None,
        running_commands={},
    )

    assert store.updated
    assert store.updated[0]["status"] in {"timeout", "stale"}
    assert len(store.inserted) == 1
