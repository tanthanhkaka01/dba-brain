"""The background-task poller trusts a PID only while it still belongs to the task it started.

A bot command that runs for long (a restore, a lab build) is started detached, and a poller looks
at it every cycle by its PID. Three things were wrong with that (review 0.25.0, B1.6):

* **A PID is reused.** Once the task ended - or after a container restart, where PIDs start low
  again - the number belonged to another process. The poller saw it "still running", waited out
  the task's timeout, and then killed whatever held the number: possibly the daemon itself.
* **The exit code was read after the liveness check**, so a finished task whose PID had been
  reused was never seen as finished.
* **A timeout killed the wrapper and not the command.** The wrapper runs the real command as its
  child; killing the wrapper's PID left the command running while the chat was told it timed out.

Now the exit-code file is read first, the process's start time recorded at launch is compared
before the PID is believed or killed, and a timeout stops the whole tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import patch_telegram

from db_ops.db import DbOpsStore
from db_ops.lib.process_liveness import is_pid_alive, process_start_marker
from db_ops.telegram import command_processor


def _task(tmp_path: Path, *, pid: int, started: str | None, age_seconds: int = 0, timeout: int = 60):
    sqlite_path = tmp_path / "db_ops.sqlite"
    store = DbOpsStore(sqlite_path)
    store.initialize()
    stdout_path = tmp_path / "task.stdout.txt"
    stdout_path.write_text("", encoding="utf-8")
    store.insert_telegram_background_task(
        chat_id="100", message_id=1, user_id="100", command_id=19, command_text="spbot_restore",
        source_id=1, pid=pid, stdout_path=str(stdout_path), stderr_path=str(tmp_path / "task.err"),
        task_data={"timeout_seconds": timeout, "pid_started": started, "values": {},
                   "timeout_text": "timed out", "failure_text": "failed", "success_text": "done"})
    if age_seconds:
        with store.connect() as conn:
            conn.execute("UPDATE telegram_background_tasks SET created_at = ?",
                         (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_seconds)),))
    return sqlite_path, stdout_path


@pytest.fixture
def kills(monkeypatch):
    stopped: list[int] = []
    patch_telegram(monkeypatch, "_stop_task_tree", lambda pid, recorded=None: stopped.append(pid))
    monkeypatch.setattr(command_processor.os, "kill",
                        lambda *a: pytest.fail("a bare os.kill on a task PID"))
    return stopped


def test_a_pid_now_held_by_another_process_is_never_killed(tmp_path, monkeypatch, kills):
    """Same number, different start time: a stranger. Past the timeout it used to be killed."""
    sqlite_path, _ = _task(tmp_path, pid=4242, started="111", age_seconds=600, timeout=60)
    patch_telegram(monkeypatch, "_is_pid_alive", lambda _pid: True)
    patch_telegram(monkeypatch, "process_start_marker", lambda _pid: "999")

    command_processor.check_cli_background_tasks(sqlite_path=sqlite_path)

    assert kills == []


def test_a_task_that_wrote_its_exit_code_is_finished_whoever_holds_the_pid(tmp_path, monkeypatch, kills):
    sqlite_path, stdout_path = _task(tmp_path, pid=4242, started="111", age_seconds=600, timeout=60)
    Path(str(stdout_path) + ".rc").write_text("0", encoding="utf-8")
    patch_telegram(monkeypatch, "_is_pid_alive", lambda _pid: True)
    patch_telegram(monkeypatch, "process_start_marker", lambda _pid: "111")

    counts = command_processor.check_cli_background_tasks(sqlite_path=sqlite_path)

    assert kills == []
    assert counts["completed"] == 1 and counts["timed_out"] == 0


def test_our_own_task_past_its_timeout_is_stopped_as_a_tree(tmp_path, monkeypatch, kills):
    sqlite_path, _ = _task(tmp_path, pid=4242, started="111", age_seconds=600, timeout=60)
    patch_telegram(monkeypatch, "_is_pid_alive", lambda _pid: True)
    patch_telegram(monkeypatch, "process_start_marker", lambda _pid: "111")

    counts = command_processor.check_cli_background_tasks(sqlite_path=sqlite_path)

    assert kills == [4242]
    assert counts["timed_out"] == 1


def test_stopping_a_task_stops_the_command_under_its_wrapper(tmp_path):
    """The real thing: the wrapper `detached_exit` and the sleeping command it runs both end."""
    child_pid_file = tmp_path / "child.pid"
    command = (f"import os, time, pathlib; pathlib.Path({str(child_pid_file)!r}).write_text("
               "str(os.getpid())); time.sleep(120)")
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # as the launch: detached, own group
    else:
        kwargs["start_new_session"] = True
    wrapper = subprocess.Popen([sys.executable, "-m", "db_ops.telegram.detached_exit",
                                str(tmp_path / "rc"), "--", sys.executable, "-c", command], **kwargs)
    deadline = time.monotonic() + 30
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    assert process_start_marker(wrapper.pid)

    command_processor._stop_task_tree(wrapper.pid, process_start_marker(wrapper.pid))
    wrapper.wait(timeout=30)

    deadline = time.monotonic() + 15
    while is_pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not is_pid_alive(child_pid), "the command outlived its wrapper"
    if os.name != "nt":
        assert wrapper.returncode != 0
