"""The SMB SQL Server restore runs its statements through `common.cli run-sqlcmd` (1.38).

The nightly restore of a production estate from a backup share built its `sqlcmd` command in the
`backup_restore` app and ran it there too - its own SSH channel for a Linux target, a local
PowerShell `Invoke-Command` for a Windows one. The operator's rule for 0.23.0: a restore goes
through `common.cli`, which reads no configuration. The app still decides everything; `common`
runs what it decided, handed every value.

What must not happen is a restore that behaves differently because of the move, and it cannot be
proven on the Windows target from a lab - so the proof here is that `common` builds **the same
command, byte for byte**, as the app did for the same restore entry, on every path.
"""

from __future__ import annotations

import dataclasses
import subprocess

import pytest

from db_ops.backup_restore.restore_database import _sqlcmd_argv, _sqlcmd_request, build_sqlcmd_query_command
from db_ops.common import sqlcmd_run
from tests.test_backup_restore import make_config

SQL = "RESTORE DATABASE [APPDB] FROM DISK = N'E:\\SQLBK_IMPORT\\APPDB.bak' WITH NORECOVERY, STATS = 10"


@pytest.fixture
def windows(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_PASSWORD", "vm-secret")
    monkeypatch.setenv("RESTORE_SQL_PASSWORD", "sql-secret")
    return dataclasses.replace(
        make_config(tmp_path), vm_platform="windows", vm_credential_target="198.51.100.129",
        vm_username=r"VM_NAME\vmadmin", vm_password_env="VM_PASSWORD",
        restore_sql_username="restore_user", restore_sql_password_env="RESTORE_SQL_PASSWORD",
        remote_command_timeout_seconds=45, restore_command_timeout_seconds=7200)


# --------------------------------------------------------------------------- #
# The same command as before, on every path
# --------------------------------------------------------------------------- #
def test_a_windows_target_gets_the_same_invoke_command(windows, monkeypatch):
    # The executable name is resolved per machine; pin it so the two sides are compared, not PATHs.
    monkeypatch.setattr("db_ops.lib.powershell.powershell_executable", lambda: "powershell")
    before = _sqlcmd_argv(sql=SQL, config=windows)

    request = _sqlcmd_request(build_sqlcmd_query_command(sql=SQL, config=windows), windows, via="winrm")
    after = sqlcmd_run.winrm_argv(request, request["host"], timeout=int(request["timeout_seconds"]))

    assert after == before


def test_a_windows_target_without_a_password_gets_no_credential_either(windows, monkeypatch):
    monkeypatch.setattr("db_ops.lib.powershell.powershell_executable", lambda: "powershell")
    config = dataclasses.replace(windows, vm_username="", vm_password_env="",
                                 restore_command_timeout_seconds=0)

    request = _sqlcmd_request(build_sqlcmd_query_command(sql=SQL, config=config), config, via="winrm")

    assert sqlcmd_run.winrm_argv(request, request["host"], timeout=0) == _sqlcmd_argv(sql=SQL, config=config)


def test_a_local_run_gets_the_same_sqlcmd_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTORE_SQL_PASSWORD", "sql-secret")
    config = dataclasses.replace(make_config(tmp_path), restore_sql_username="restore_user",
                                 restore_sql_password_env="RESTORE_SQL_PASSWORD")

    request = _sqlcmd_request(build_sqlcmd_query_command(sql=SQL, config=config), config, via="local")

    assert sqlcmd_run.local_argv(request) == _sqlcmd_argv(sql=SQL, config=config)


def test_a_linux_target_gets_the_same_remote_command(monkeypatch):
    """What the app's own SSH channel used to exec, character for character."""
    ran = {}

    class Channel:
        def exec_command(self, command):
            ran["command"] = command

        def exit_status_ready(self):
            return True

        def recv_ready(self):
            return False

        def recv_stderr_ready(self):
            return False

        def recv_exit_status(self):
            return 0

    class Client:
        def get_transport(self):
            return type("T", (), {"open_session": lambda self, timeout=None: ran.update(timeout=timeout) or Channel()})()

        def close(self):
            ran["closed"] = True

    monkeypatch.setattr("db_ops.common.hostcmd.open_client", lambda host: Client())
    answer = sqlcmd_run.run_sqlcmd({
        "sql": "RESTORE LOG [db] FROM DISK = N'/tmp/log.trn';", "instance": "localhost,1433",
        "sqlcmd_path": "sqlcmd", "username": "sa", "password": "p'w", "login_timeout_seconds": 60,
        "query_timeout_seconds": 0, "via": "ssh",
        "host": {"host": "198.51.100.31", "username": "tuser", "password": "x", "open_timeout_seconds": 60}})

    assert answer["exit_code"] == 0 and answer["timed_out"] is False and ran["closed"]
    assert ran["timeout"] == 60
    assert ran["command"] == (
        "export PATH=$PATH:/opt/mssql-tools/bin:/opt/mssql-tools18/bin; sqlcmd -S localhost,1433 "
        "-C -U sa -P 'p'\"'\"'w' -l 60 -t 0 -b -Q 'RESTORE LOG [db] FROM DISK = N'\"'\"'/tmp/log.trn'\"'\"';'")


# --------------------------------------------------------------------------- #
# Answers
# --------------------------------------------------------------------------- #
def test_a_run_that_outlasts_its_deadline_says_so_rather_than_raising(monkeypatch):
    """The caller must tell "never started" from "started and cut off": a RESTORE LOG cut off
    leaves a database whose state has to be inspected, not retried blindly."""
    import sys

    answer = sqlcmd_run._run_local([sys.executable, "-c", "import time; print('10 percent processed.', flush=True); time.sleep(30)"],
                                   via="local", timeout=2, started=0.0)

    assert answer["timed_out"] is True and answer["exit_code"] is None
    assert "10 percent processed." in answer["stdout"]


def test_every_line_is_echoed_to_stderr_as_it_arrives(capfd):
    import sys

    sqlcmd_run._run_local([sys.executable, "-c", "print('10 percent processed.')"],
                          via="local", timeout=30, started=0.0)

    assert "10 percent processed." in capfd.readouterr().err


def test_no_login_means_integrated_auth():
    assert sqlcmd_run.local_argv({"sql": "SELECT 1", "instance": "localhost"})[4] == "-E"


@pytest.mark.parametrize("request_, message", [
    ({"instance": "x"}, "sql is required"),
    ({"sql": "SELECT 1"}, "instance is required"),
    ({"sql": "SELECT 1", "instance": "x", "via": "telnet"}, "via must be one of"),
    ({"sql": "SELECT 1", "instance": "x", "via": "ssh"}, "needs \"host\""),
    ({"sql": "SELECT 1", "instance": "x", "username": "sa"}, "both username and password"),
])
def test_a_request_that_cannot_run_is_refused_by_name(request_, message):
    with pytest.raises(sqlcmd_run.SqlcmdRunError, match=message):
        sqlcmd_run.run_sqlcmd(request_) if "username" not in request_ else sqlcmd_run.local_argv(request_)


def test_the_app_reads_a_timeout_answer_as_a_started_command(monkeypatch):
    from db_ops.backup_restore import restore_database
    from db_ops.transport import common_cli

    monkeypatch.setattr(common_cli, "run_allowing_failure",
                        lambda *a, **k: (True, {"timed_out": True, "exit_code": None}, ""))

    with pytest.raises(restore_database.RestoreCommandTimeoutError) as raised:
        restore_database._sqlcmd_in_common({"via": "ssh", "timeout_seconds": 5}, cmd=["x"])
    assert raised.value.command_started is True


def test_the_app_reads_a_command_that_could_not_run_as_an_error(monkeypatch):
    from db_ops.backup_restore import restore_database
    from db_ops.transport import common_cli

    monkeypatch.setattr(common_cli, "run_allowing_failure",
                        lambda *a, **k: (False, {}, "could not connect to tuser@198.51.100.31"))

    with pytest.raises(RuntimeError, match="could not connect"):
        restore_database._sqlcmd_in_common({"via": "ssh"}, cmd=["x"])


def test_a_completed_run_comes_back_as_the_process_it_was(monkeypatch):
    from db_ops.backup_restore import restore_database
    from db_ops.transport import common_cli

    monkeypatch.setattr(common_cli, "run_allowing_failure", lambda *a, **k: (
        True, {"timed_out": False, "exit_code": 1, "stdout": "Msg 3013", "stderr": ""}, ""))

    result = restore_database._sqlcmd_in_common({"via": "local"}, cmd=["sqlcmd"])

    assert isinstance(result, subprocess.CompletedProcess)
    assert (result.returncode, result.stdout) == (1, "Msg 3013")


# --------------------------------------------------------------------------- #
# Found proving it on the labs (a LABTEST backup restored as LAB138 on the .250 container)
# --------------------------------------------------------------------------- #
def test_a_batch_that_finishes_before_the_first_poll_is_still_echoed(monkeypatch, capfd):
    """The 0.04 s RESTORE on the lab left all its output for the final drain, which stored it and
    echoed none: whoever watched saw nothing at all."""
    class Channel:
        def __init__(self):
            self.chunks = [b"26 percent processed.\n100 percent processed.\n", b"RESTORE DATABASE done"]

        def exec_command(self, command):
            pass

        def exit_status_ready(self):
            return True

        def recv_ready(self):
            return bool(self.chunks)

        def recv(self, _size):
            return self.chunks.pop(0)

        def recv_stderr_ready(self):
            return False

        def recv_exit_status(self):
            return 0

    class Client:
        def get_transport(self):
            return type("T", (), {"open_session": lambda self, timeout=None: Channel()})()

        def close(self):
            pass

    monkeypatch.setattr("db_ops.common.hostcmd.open_client", lambda host: Client())
    answer = sqlcmd_run.run_sqlcmd({"sql": "RESTORE", "instance": "localhost", "via": "ssh",
                                    "host": {"host": "198.51.100.31", "username": "u", "password": "p"}})

    err = capfd.readouterr().err
    assert "26 percent processed." in err and "100 percent processed." in err
    assert "RESTORE DATABASE done" in err, "the last line, with no newline after it"
    assert answer["stdout"].endswith("RESTORE DATABASE done")


def test_a_linux_restore_logs_its_progress_too(tmp_path, monkeypatch):
    """The Linux path logged no progress events at all before 0.23.0 - only the Windows and local
    ones did. The answer carries the same lines on every path."""
    from db_ops.backup_restore import restore_database

    config = dataclasses.replace(make_config(tmp_path), vm_platform="linux",
                                 vm_credential_target="198.51.100.31")
    events = []
    monkeypatch.setattr(restore_database, "log_event", lambda _logger, **kw: events.append(kw["message"]))
    monkeypatch.setattr(restore_database, "_sqlcmd_in_common", lambda request, *, cmd: subprocess.CompletedProcess(
        cmd, 0, "50 percent processed.\n100 percent processed.\n", ""))

    restore_database.run_sqlcmd_query_command(
        build_sqlcmd_query_command(sql=SQL, config=config), config=config, logger=object(),
        progress_step="restore-full", progress_database="APPDB", restore_id="R")

    assert [e for e in events if "progress" in e] == [
        "restore-db restore-full progress restore_id=R database=APPDB percent=50",
        "restore-db restore-full progress restore_id=R database=APPDB percent=100"]
