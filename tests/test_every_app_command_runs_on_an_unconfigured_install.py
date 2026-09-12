"""Every scheduled app ships switched on, and every one of them runs cleanly on an empty install.

Until 2026-09-11 three of the nine app commands shipped ``active: false`` — the inventory workflow,
the web host and backup/restore — each for a reason that was really a defect in something else:

* the inventory workflow failed with a bare ``[Errno 2]`` because nothing created the canonical
  inventory it merged into;
* backup/restore failed every cycle with the whole error text ``'prod_backup_share'`` because the
  loader parsed an empty configuration as one malformed entry, and ``init`` wrote no
  ``restore_config.json`` at all;
* the web host never exits, and ``daemon --once`` waited for it for ever.

The flag hid all three instead of fixing them, and an operator who switched one on found the defect
the flag was hiding. Each is fixed at its root now, and the operator's rule from that day is that
**the release proves it**: every command in the shipped schedule is active, and on a root where
``init`` has run and nothing else — no inventory, no token, no restore entry, no SQL task — every one
of them finishes ``status=done`` and says what is missing rather than failing. A default schedule that
errors every cycle on a correct install teaches its reader to ignore the log.

These run the real commands in subprocesses against a real ``init``-ed root, because the property
is about the product as it is started, not about any one function inside it.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from db_ops import scaffold
from db_ops.jobs.daemon import use_this_interpreter

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = REPO_ROOT / "db_ops" / "jobs" / "catalogue" / "app_commands.json"


def _catalogue() -> list[dict]:
    return json.loads(CATALOGUE.read_text(encoding="utf-8-sig"))["app_commands"]


def _is_service(command: dict) -> bool:
    """timeout 0 is the daemon's own definition of a long-running service (AppCommand.timeout_disabled)."""
    return (command.get("time_window") or {}).get("timeout") == 0


JOBS = [c for c in _catalogue() if not _is_service(c)]
SERVICES = [c for c in _catalogue() if _is_service(c)]


@pytest.fixture(scope="module")
def bare_root(tmp_path_factory):
    """A root where `init` has run and nothing else has."""
    root = tmp_path_factory.mktemp("unconfigured")
    scaffold.initialise(root, app_name="probe")
    return root


def _env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "DB_OPS_HOME": str(root),
        "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
        # A new node sets its own passphrase before anything else; the guide says so first.
        "DB_OPS_SECRET_KEY": "unconfigured-install-passphrase",
        "PYTHONIOENCODING": "utf-8",
        # Nothing here is a worker in a two-node estate; the shipped entries are node_role "all".
        "DB_OPS_NODE_ROLE": "master",
    })
    return env


def test_every_shipped_app_command_is_active():
    off = [c["app_command_id"] for c in _catalogue() if c.get("active") is not True]
    assert not off, (
        f"{off} ship inactive. Every app command ships on, and one that cannot run on an empty "
        "install is a defect in that app, not a reason for the flag - see this module's docstring.")


@pytest.mark.parametrize("command", JOBS, ids=[c["app_command_id"] for c in JOBS])
def test_each_job_exits_cleanly_with_nothing_configured(bare_root, command):
    result = subprocess.run(use_this_interpreter(command["command_text"]), shell=True,
                            cwd=bare_root, env=_env(bare_root), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120)

    assert result.returncode == 0, (
        f"{command['app_command_id']} exited {result.returncode} on an unconfigured install:\n"
        f"{(result.stderr or result.stdout)[-2000:]}")
    assert "Traceback" not in result.stderr, result.stderr[-2000:]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("command", SERVICES, ids=[c["app_command_id"] for c in SERVICES])
def test_each_service_starts_and_answers_with_nothing_configured(bare_root, command):
    """A service has no exit code to judge. It passes by answering HTTP on an empty root."""
    port = _free_port()
    text = use_this_interpreter(command["command_text"]).replace("--port 8080", f"--port {port}")
    process = subprocess.Popen(text, shell=True, cwd=bare_root, env=_env(bare_root),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        answered = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/db_ops/login", timeout=2) as reply:
                    answered = reply.status
                    break
            except OSError:
                time.sleep(0.3)
        assert process.poll() is None, (
            f"{command['app_command_id']} exited on an unconfigured install: "
            f"{process.stderr.read().decode('utf-8', 'replace')[-2000:]}")
        assert answered == 200
    finally:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True) \
            if sys.platform == "win32" else process.kill()
        process.wait(timeout=10)


def test_one_daemon_pass_over_the_whole_schedule_returns_and_every_job_is_done(tmp_path):
    """The scheduler's view of the same property, and the regression that kept the web host off:
    with a service in the schedule, `--once` must still return."""
    scaffold.initialise(tmp_path, app_name="probe")
    # If --once ever starts the service again, it must not be left holding 8080 — the port real
    # nodes on this machine listen on — after this test gives up on it.
    schedule = tmp_path / "data" / "app_commands.json"
    schedule.write_text(schedule.read_text(encoding="utf-8-sig")
                        .replace("--port 8080", f"--port {_free_port()}"), encoding="utf-8")

    process = subprocess.Popen([sys.executable, "-m", "db_ops.cli", "daemon", "--config",
                                "config.json", "--once"], cwd=tmp_path, env=_env(tmp_path),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _out, err_bytes = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        # The whole tree: a service the pass started is a grandchild, and killing only the daemon
        # would orphan it.
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        else:
            process.kill()
        process.communicate()
        pytest.fail("daemon --once did not return within 120 s - a service in the schedule was "
                    "started and waited on, which is what kept APP-WEBHOST switched off")
    stderr = err_bytes.decode("utf-8", "replace")

    assert process.returncode == 0, stderr[-2000:]
    assert "long_running_service_not_run_by_once" in stderr
    # The store the root itself declares. Opened read-only: sqlite3 silently creates a missing file,
    # which turns a wrong path into "no such table" instead of "no such store".
    store = json.loads((tmp_path / "data" / "store_config.json").read_text(encoding="utf-8"))
    store_file = tmp_path / store["sqlite"]["path"]
    with sqlite3.connect(f"{store_file.as_uri()}?mode=ro", uri=True) as conn:
        status = dict(conn.execute("SELECT job_code, status FROM job_runs").fetchall())
    for command in JOBS:
        assert status.get(command["app_command_id"]) == "done", (
            f"{command['app_command_id']}: {status.get(command['app_command_id'])!r}")
    assert not {c["app_command_id"] for c in SERVICES} & set(status), "a service ran in --once"
