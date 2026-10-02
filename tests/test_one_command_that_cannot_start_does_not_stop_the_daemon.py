"""A command that cannot be started is that command's failure, not the daemon's.

Measured before the fix: `[APP-A, APP-BAD (working_dir missing), APP-C]` - the scan raised
`FileNotFoundError`, only APP-A had started, and `main` returned 1. Under docker's
`restart: unless-stopped` that was a crash loop in which nothing listed after the bad entry ever
ran. A child that outlives SIGKILL for five seconds (uninterruptible I/O on a hung mount) ended the
daemon the same way. And the daemon now tells each child the timeout it will be killed at.
"""

from __future__ import annotations

import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from db_ops.jobs import daemon
from db_ops.lib import app_timeout
from test_jobs_daemon import (
    FakeConfig,
    FakeProcess,
    FakeStore,
    app_command,
    commands_running,
    write_app_commands,
)


def _scan(tmp_path, data_dir, store=None):
    running: dict = {}
    daemon.run_scheduler_scan(config=FakeConfig(tmp_path / "logs"), store=store or FakeStore(),
                              data_dir=data_dir, logger=None, running_commands=running)
    return running


def test_a_missing_working_dir_is_recorded_and_the_rest_still_start(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    bad = app_command("APP-BAD")
    bad["working_dir"] = "does/not/exist"
    write_app_commands(data_dir, [app_command("APP-A"), bad, app_command("APP-C")])
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: FakeProcess(returncode=None))
    store = FakeStore()

    running = _scan(tmp_path, data_dir, store)

    assert commands_running(running) == {"APP-A", "APP-C"}
    failed = [run for run in store.inserted if run.job_code == "APP-BAD"]
    assert len(failed) == 1
    assert failed[0].status == "error" and "working_dir not found" in failed[0].error_text


def test_a_spawn_that_fails_is_recorded_too(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-A"), app_command("APP-C")])

    def popen(*args, **kwargs):
        if popen.calls == 0:
            popen.calls += 1
            raise OSError(11, "Resource temporarily unavailable")
        return FakeProcess(returncode=None)

    popen.calls = 0
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    store = FakeStore()

    running = _scan(tmp_path, data_dir, store)

    assert commands_running(running) == {"APP-C"}
    assert [run.job_code for run in store.inserted if run.status == "error"] == ["APP-A"]


def test_a_locked_store_is_still_the_loops_to_wait_out(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-A")])
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: FakeProcess(returncode=None))

    class LockedStore(FakeStore):
        def insert_job_run(self, item):
            raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        _scan(tmp_path, data_dir, LockedStore())


def test_a_child_that_outlives_sigkill_does_not_end_the_daemon():
    class Stuck:
        pid = 4242
        killed = False

        def terminate(self):
            pass

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("app", timeout)

    process = Stuck()
    daemon.terminate_timed_out_command(SimpleNamespace(process=process))

    assert process.killed


def _spawned_env(tmp_path, monkeypatch, **overrides):
    data_dir = tmp_path / "data"
    write_app_commands(data_dir, [app_command("APP-A", **overrides)])
    spawned: list[dict] = []
    monkeypatch.setattr(daemon.subprocess, "Popen",
                        lambda *a, **k: spawned.append(k) or FakeProcess(returncode=None))
    _scan(tmp_path, data_dir)
    return spawned[0]["env"]


def test_the_child_is_told_the_timeout_it_will_be_killed_at(tmp_path, monkeypatch):
    env = _spawned_env(tmp_path, monkeypatch, timeout=300)

    assert env[app_timeout.APP_TIMEOUT_ENV_VAR] == "300"


def test_a_service_with_no_timeout_is_told_none_and_inherits_none(tmp_path, monkeypatch):
    monkeypatch.setenv(app_timeout.APP_TIMEOUT_ENV_VAR, "999")

    env = _spawned_env(tmp_path, monkeypatch, timeout=0)

    assert app_timeout.APP_TIMEOUT_ENV_VAR not in env


@pytest.mark.parametrize("raw, expected", [("300", 300), ("0", None), ("", None), ("x", None), ("-5", None)])
def test_the_timeout_is_read_back_only_when_it_is_a_positive_number(raw, expected):
    assert app_timeout.timeout_seconds({app_timeout.APP_TIMEOUT_ENV_VAR: raw}) == expected
