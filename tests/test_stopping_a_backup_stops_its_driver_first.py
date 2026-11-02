"""Stopping a running backup stops its driver on the host first, then its sessions (0.34.0, 1.238).

Why it matters: on a production SQL Server (2026-10-29) the operator said "stop it" to a manual FULL, and the
engine session doing the backup was killed. The script driving it went through its database list,
reconnected a second later and backed up the next database - 548 GB from and onto the drive holding
the data file - for another hour, slowing the application's bulk inserts. It was finally stopped by
ending the run's WinRM host and its sqlcmd, by pid and parent.

So every run carries a tag (``DBABRAIN_RUN_TAG``, ``<backup_id>_<job_runs id>``):
- its script writes its own pid under it;
- every sqlcmd of the run names its session ``host_name = dbabrain:<tag>``;
- ``backup-stop`` ends that process tree, then those sessions - never a native job's backup on the
  same instance;
- a pid is ended only when that process started before the pid file was written, so a stale file
  never ends a stranger;
- the run's row then says *stopped by the operator*, and the runner leaves that row alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import backup as backup_module
from dbabrain.common.backup import stop
from dbabrain.lib import backup_run
from tests.test_every_backup_run_records_its_files_and_their_checksums import (  # noqa: F401 - the fixture
    _job, _scheduled, catalog)

SCRIPTS = Path("dbabrain/common/backup_scripts")


# --------------------------------------------------------------------------- #
# The tag
# --------------------------------------------------------------------------- #
def test_a_runs_tag_is_its_backup_id_and_its_row_and_safe_on_a_host():
    assert backup_run.run_tag("ACME_MSSQL_SALES_FULL", 8812) == "ACME_MSSQL_SALES_FULL_8812"
    assert backup_run.run_tag("odd id/with spaces;", 7) == "odd_id_with_spaces__7"
    assert backup_run.RUN_TAG_RE.match(backup_run.run_tag("x" * 300, 1))


def test_every_scheduled_run_hands_its_script_the_tag(monkeypatch, catalog):  # noqa: F811
    seen: dict = {}
    _scheduled(monkeypatch, catalog, _job(), [], seen_env=seen)
    assert seen[backup_run.RUN_TAG_ENV] == "ACME_250_APPDB_1", "<backup_id>_<job_runs id>"


# --------------------------------------------------------------------------- #
# The scripts: a pid file under the tag, and sqlcmd sessions named by it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", sorted(str(p) for p in SCRIPTS.glob("*/*.sh") if p.parent.name != "stop")
                         + [str(SCRIPTS / "sqlserver" / "mssql_backup_database.ps1")])
def test_every_backup_script_writes_its_pid_under_the_tag_and_removes_it(script):
    text = Path(script).read_text(encoding="utf-8")
    assert "DBABRAIN_RUN_TAG" in text and "dbabrain_runs" in text
    assert ("trap 'rm -f \"$run_file\"' EXIT" in text) or (text.count("Remove-RunFile") >= 3), \
        "removed on exit - the PowerShell one on each of its exits"


def test_every_sqlcmd_of_a_sql_server_run_names_its_session_by_the_tag():
    ps1 = (SCRIPTS / "sqlserver" / "mssql_backup_database.ps1").read_text(encoding="utf-8")
    assert ps1.count("+ (Get-TrustFlag) + $runHost") == ps1.count("+ (Get-TrustFlag)") >= 3
    assert "@('-H', \"dbabrain:$($env:DBABRAIN_RUN_TAG)\")" in ps1
    sh = (SCRIPTS / "sqlserver" / "mssql_backup_database.sh").read_text(encoding="utf-8")
    assert sh.count("-S localhost") == sh.count('-S localhost ${run_host[@]+"${run_host[@]}"}') >= 3
    assert 'run_host=(-H "dbabrain:${DBABRAIN_RUN_TAG}")' in sh


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash on this machine")
@pytest.mark.parametrize("script", sorted(p.as_posix() for p in SCRIPTS.glob("*/*.sh")))
def test_every_bash_script_still_parses(script):
    assert subprocess.run(["bash", "-n", script], capture_output=True).returncode == 0


# --------------------------------------------------------------------------- #
# common.cli backup-stop
# --------------------------------------------------------------------------- #
def test_the_stop_lines_read_as_one_answer():
    answer = stop.parse_stop_lines(
        "noise\nSTOP\tpid=11488\nSTOP\tkilled=14344\nSTOP\tkilled=11488\nSTOP\talive=0\n"
        "STOP\tsession_killed=508\tdb=SALESDB\nSTOP\tsession_left=77\tdb=HR\nRESULT=ok\n")
    assert answer["pid"] == 11488 and answer["killed"] == [14344, 11488] and answer["process_alive"] is False
    assert answer["sessions_killed"] == [{"session_id": 508, "database": "SALESDB"}]
    assert answer["sessions_left"] == [{"session_id": 77, "database": "HR"}]


@pytest.mark.parametrize("runtime, marker", [("windows", "Get-WmiObject Win32_Process"), ("linux", "pgrep -P")])
def test_the_hosts_platform_picks_the_stop_and_only_the_runs_login_travels(monkeypatch, runtime, marker):
    sent: dict = {}

    def run_script(host, script, *, env=None, timeout=None):
        sent.update(script=script, env=env, timeout=timeout)
        return {"exit_code": 0, "stdout": "STOP\tpid_file=missing\nRESULT=ok\n", "stderr": ""}

    monkeypatch.setattr(stop, "run_script", run_script)
    data = stop.stop_backup({"run_tag": "ACME_SALES_9", "db_type": "sqlserver",
                             "host": {"runtime": runtime, "host": "192.0.2.245"},
                             "env": {"MSSQL_USER": "u", "MSSQL_PASSWORD": "p", "BACKUP_ENCRYPTION_PASSWORD": "x"}})
    assert marker in sent["script"]
    assert sent["env"] == {"MSSQL_USER": "u", "MSSQL_PASSWORD": "p", "DBABRAIN_RUN_TAG": "ACME_SALES_9",
                           "DB_TYPE": "sqlserver"}, "the backup's other secrets never reach the stop"
    assert data["stopped"] is True and data["pid_file"] == "missing"


def test_a_session_still_open_means_not_stopped(monkeypatch):
    monkeypatch.setattr(stop, "run_script", lambda *a, **k: {
        "exit_code": 0, "stdout": "STOP\tpid=5\nSTOP\talive=0\nSTOP\tsession_left=77\tdb=HR\nRESULT=ok\n"})
    data = stop.stop_backup({"run_tag": "T_1", "db_type": "sqlserver", "host": {"runtime": "linux"}})
    assert data["stopped"] is False and data["sessions_left"][0]["session_id"] == 77


@pytest.mark.parametrize("tag", ["", "a b", "x';DROP", "../etc", "x" * 101])
def test_a_tag_that_could_reach_a_path_or_a_sql_literal_is_refused(tag):
    with pytest.raises(stop.BackupStopError, match="run_tag is required"):
        stop.stop_backup({"run_tag": tag, "db_type": "sqlserver", "host": {}})


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell stop, run for real, needs Windows")
def test_the_windows_stop_ends_a_real_runs_tree_and_never_a_stale_pids_process():
    """The script itself, on this machine: a stand-in run and its child are ended; a pid file naming a
    process that started after the file was written ends nothing."""
    tag = f"PYTEST_STOP_{os.getpid()}"
    run_dir = Path(tempfile.gettempdir()) / "dbabrain_runs"
    run_dir.mkdir(exist_ok=True)
    pid_file = run_dir / f"{tag}.pid"
    runner = (f"Set-Content -LiteralPath '{pid_file}' -Value $PID -Encoding ASCII; "
              "& ping.exe -n 60 127.0.0.1 > $null")
    run = subprocess.Popen(["powershell", "-NoProfile", "-Command", runner],
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        for _ in range(50):
            if pid_file.exists():
                break
            time.sleep(0.2)
        time.sleep(1)
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              str(SCRIPTS / "stop" / "backup_stop.ps1")],
                             env={**os.environ, "DBABRAIN_RUN_TAG": tag, "DB_TYPE": "postgresql"},
                             capture_output=True, text=True, timeout=60).stdout
        answer = stop.parse_stop_lines(out)
        assert answer["pid"] == run.pid and run.pid in answer["killed"] or answer["killed"], out
        assert run.wait(timeout=10) is not None and answer["process_alive"] is False
        assert not pid_file.exists()

        late = subprocess.Popen(["ping.exe", "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        pid_file.write_text(str(late.pid), encoding="ascii")
        old = time.time() - 600
        os.utime(pid_file, (old, old))
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              str(SCRIPTS / "stop" / "backup_stop.ps1")],
                             env={**os.environ, "DBABRAIN_RUN_TAG": tag, "DB_TYPE": "postgresql"},
                             capture_output=True, text=True, timeout=60).stdout
        assert stop.parse_stop_lines(out)["killed"] == [] and late.poll() is None, \
            "a process started after the pid file was written is someone else's"
        late.kill()
    finally:
        if run.poll() is None:
            run.kill()
        pid_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# The app: the row says "stopped by the operator", and the runner leaves it
# --------------------------------------------------------------------------- #
def test_a_stop_closes_the_running_row_as_stopped_and_the_runner_leaves_it(tmp_path, monkeypatch):
    from dbabrain.db.job_runs import JobRun
    from dbabrain.db.store import DbaBrainStore

    store = DbaBrainStore(tmp_path / "store.sqlite")
    store.initialize()
    job = SimpleNamespace(backup_id="ACME_MSSQL_SALES_FULL", job="full", label="ACME_MSSQL_SALES_FULL/full",
                          job_code="backup_restore.backup.ACME_MSSQL_SALES_FULL.full", notify={})
    log_id = store.insert_job_run(JobRun(job_code=job.job_code, claim_key=job.job_code, level="logging",
                                         status="RUNNING", message="started", started_at="2026-10-29T06:02:15Z",
                                         host_name="node", metadata={"backup_id": job.backup_id}))
    asked: list = []
    events: list = []
    monkeypatch.setattr(backup_module, "load_backup_jobs", lambda _p: [job])
    monkeypatch.setattr(backup_module, "DbaBrainStore", SimpleNamespace(from_config=lambda _c: store))
    monkeypatch.setattr(backup_module, "resolve_backup_target", lambda *a, **k: None)
    monkeypatch.setattr(backup_module, "_backup_request", lambda *a, **k: {
        "db_type": "sqlserver", "host": {"runtime": "windows", "host": "192.0.2.245"},
        "env": {"MSSQL_USER": "u", "MSSQL_PASSWORD": "p"}})
    monkeypatch.setattr(backup_module.common_cli, "run_allowing_failure", lambda command, request: (
        asked.append((command, request)) or (True, {
            "stopped": True, "pid": 11488, "killed": [14344, 11488], "process_alive": False,
            "sessions_killed": [{"session_id": 508, "database": "SALESDB"}], "sessions_left": []}, "")))
    monkeypatch.setattr(backup_module, "emit_backup_restore_event", lambda **k: events.append(k))

    summary = backup_module.stop_backup_run(app_config=None, config_path="x", backup_id=job.backup_id)

    assert asked[0][0] == "backup-stop" and asked[0][1]["run_tag"] == f"ACME_MSSQL_SALES_FULL_{log_id}"
    assert summary["stopped"] is True and summary["running"] == 1
    row = store.fetch_job_run(log_id)
    assert row["status"] == "ERROR" and "stopped by the operator - 2 process(es), 1 session(s) ended" in row["message"]
    assert json.loads(row["metadata_json"])["stopped_by_operator"]["sessions_killed"][0]["session_id"] == 508
    assert events[0]["level"] == "warning"
    assert backup_module._stopped_by_operator(store, log_id) is True, \
        "the runner, returning from the killed script, finds the row closed and leaves it"
    assert backup_module.stop_backup_run(app_config=None, config_path="x", backup_id=job.backup_id)["running"] == 0
