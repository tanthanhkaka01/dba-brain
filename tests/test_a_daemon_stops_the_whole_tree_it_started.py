"""The daemon stops everything an app command started, and leaves no run open behind it.

Four gaps of the scheduler (review 0.25.0, B2.3, F1.1, B2.5) and one of its client (F1.2):

* **A timeout killed the shell, not the app.** An app command runs through `shell=True`.
  `terminate()` reached `/bin/sh` - or `cmd.exe` on a Windows master, which stays the parent - and the
  app under it kept working after its run was closed as a timeout, with the claim released for a
  duplicate. Its own `common.cli` children survived the same way. Each app now starts as the head of
  a process tree of its own, and a timeout stops the tree. **Only a tree it can prove is its own**:
  the head's start time is read at launch, and a PID that no longer carries it is never walked - a
  walk from a bare PID ends whoever holds the number now, which the first cut of this did from the
  suite's fake processes.
* **A stopped daemon closed its rows and left the children running**, claims released - the next
  daemon started a duplicate beside them. They are stopped first now.
* **A `running` row whose app command was removed from `app_commands.json` stayed open for ever**:
  start-up skipped it, and the sweep its comment promised did not exist.
* **One line on stdout ahead of a command's JSON answer lost the answer**, and a JSON list raised
  the wrong error.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from db_ops.db import DbOpsStore
from db_ops.db.job_runs import JobRun
from db_ops.jobs import daemon
from db_ops.lib.common_cli import CommonCliError, read_answer
from db_ops.lib.process_liveness import (
    child_start_marker,
    is_pid_alive,
    own_group_kwargs,
    stop_process_tree,
)


def _shell_with_a_child(tmp_path: Path) -> tuple[subprocess.Popen, int]:
    """What the daemon starts: a shell running an app that has a child of its own."""
    pid_file = tmp_path / "grandchild.pid"
    child = tmp_path / "child.py"
    child.write_text("import os, pathlib, sys, time\n"
                     "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                     "time.sleep(120)\n", encoding="utf-8")
    app = tmp_path / "app.py"
    app.write_text("import subprocess, sys, time\n"
                   "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
                   "time.sleep(120)\n", encoding="utf-8")
    command = subprocess.list2cmdline([sys.executable, str(app), str(child), str(pid_file)])
    process = subprocess.Popen(command, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               **own_group_kwargs())
    deadline = time.monotonic() + 30
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    return process, int(pid_file.read_text(encoding="utf-8"))


def _gone(pid: int, seconds: float = 15) -> bool:
    deadline = time.monotonic() + seconds
    while is_pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    return not is_pid_alive(pid)


def test_a_timeout_stops_the_app_and_its_child_not_only_the_shell(tmp_path):
    process, grandchild = _shell_with_a_child(tmp_path)

    daemon.terminate_timed_out_command(SimpleNamespace(process=process,
                                                       start_marker=child_start_marker(process.pid)))

    assert _gone(grandchild), "the app's child outlived the timeout"


def test_a_stopping_daemon_stops_its_children_before_closing_their_rows(tmp_path, monkeypatch):
    process, grandchild = _shell_with_a_child(tmp_path)
    closed: list[int] = []
    store = SimpleNamespace(update_job_run=lambda **kw: closed.append(kw["log_id"]))
    running = {"APP-X#1": SimpleNamespace(process=process, log_id=7,
                                          start_marker=child_start_marker(process.pid),
                                          app_command=SimpleNamespace(app_command_id="APP-X"))}
    monkeypatch.setattr(daemon, "app_command_metadata", lambda *a, **k: {})

    daemon.close_running_on_shutdown(store=store, logger=None, running_commands=running, reason="signal_15")

    assert closed == [7]
    assert _gone(grandchild), "a child of a stopped daemon kept running with its claim released"


def test_a_pid_without_its_start_marker_is_never_walked(tmp_path):
    """The stranger case: the number is alive, but nothing proves it is the process that was started."""
    process, grandchild = _shell_with_a_child(tmp_path)
    try:
        class Fake:
            pid = process.pid
            stopped = False

            def terminate(self):
                self.stopped = True

            kill = terminate

            def wait(self, timeout=None):
                return 0

        fake = Fake()
        stop_process_tree(process.pid, started="not-its-start-time", process=fake)
        stop_process_tree(process.pid, started=None)

        assert fake.stopped
        assert is_pid_alive(process.pid) and is_pid_alive(grandchild)
    finally:
        stop_process_tree(process.pid, started=child_start_marker(process.pid), process=process)


def test_a_process_that_is_not_our_child_gets_no_marker():
    import os

    assert child_start_marker(os.getppid()) is None


def test_start_up_closes_a_daemon_row_whose_command_was_removed_and_leaves_a_backup_s(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    removed = store.insert_job_run(JobRun(
        job_code="APP-RETIRED", claim_key="APP-RETIRED", level="logging", status="running", message="m",
        started_at="2026-09-30T00:00:00Z", host_name="old-host",
        metadata={"app_command_id": "APP-RETIRED"}))
    backup = store.insert_job_run(JobRun(
        job_code="backup_restore.backup_job.LAB251_MSSQL_FULL.full", claim_key=None, level="logging",
        status="running", message="m", started_at="2026-09-30T00:00:00Z", host_name="old-host",
        metadata={"backup_id": "LAB251_MSSQL_FULL"}))

    daemon.recover_stale_running_jobs(store=store, app_commands={}, config=SimpleNamespace(), logger=None)

    still_running = {int(row["log_id"]) for row in store.fetch_running_job_runs()}
    assert removed not in still_running
    assert backup in still_running


def test_the_answer_is_found_behind_a_native_tool_s_line():
    stdout = 'NOTICE: native tool says hello\n{\n "success": true,\n "data": {"rows": 3}\n}\n'

    assert read_answer("run-sql", returncode=0, stdout=stdout, stderr="") == (True, {"rows": 3}, "")


def test_an_answer_that_is_not_an_object_is_no_answer():
    with pytest.raises(CommonCliError, match="without a JSON response"):
        read_answer("run-sql", returncode=0, stdout="[1, 2]", stderr="")
