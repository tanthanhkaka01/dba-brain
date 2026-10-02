"""The decryption passphrase reaches the daemon and its children in the environment, never in argv.

Review 0.25.0: B2.4 - the daemon appended `--key`/`--key-base64 <value>` to every child's command
line, readable by any user on the host (`ps`, /proc/<pid>/cmdline), and with commands starting
every second it was in the process table almost permanently. F6.1 - `control deploy` started the
worker as `db_ops daemon --key_base64 <base64>`, in the container's command for its whole life.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from types import SimpleNamespace

from db_ops.control.deploy import daemon_start_script

SECRET = "pass phrase with 'quotes' and $dollars"


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None or shutil.which("base64") is None,
                    reason="runs the generated POSIX shell")
def test_the_deploy_script_exports_the_key_and_never_passes_it_as_an_argument(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    seen = tmp_path / "seen"
    (fake_bin / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" > {seen}.argv\n'
        f'printf "%s" "$DB_OPS_SECRET_KEY" > {seen}.env\n', encoding="utf-8")
    (fake_bin / "docker").chmod(0o755)
    remote = tmp_path / "remote"
    remote.mkdir()

    script = daemon_start_script(remote_dir=str(remote), node_role="worker", container="db_ops_daemon",
                                 secret_key=SECRET)
    subprocess.run(["bash", "-s"], input=script, text=True, check=True,
                   env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"})

    argv = (tmp_path / "seen.argv").read_text(encoding="utf-8")
    assert (tmp_path / "seen.env").read_text(encoding="utf-8") == SECRET
    assert "-e DB_OPS_SECRET_KEY " in argv and "daemon" in argv
    assert "--key" not in argv and "pass phrase" not in argv
    assert "pass phrase" not in script, "only an encoded form travels, and only on stdin"


# --------------------------------------------------------------------------- #
# F11.2 - the SQL login's password is never sqlcmd's argument
# --------------------------------------------------------------------------- #
def test_a_local_sqlcmd_gets_its_password_in_the_environment(monkeypatch):
    from db_ops.common import remote_exec, sqlcmd_run

    seen = {}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def run(self, command, **kwargs):
            seen["argv"], seen["env"] = command, kwargs.get("env")
            return remote_exec.RemoteResult(method="local", host="", command="", exit_code=0,
                                            stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(remote_exec, "open_session", lambda access: Session())
    sqlcmd_run.run_sqlcmd({"sql": "SELECT 1", "instance": "localhost", "username": "sa",
                           "password": "S3cret!", "via": "local"})

    assert "S3cret!" not in " ".join(seen["argv"]) and "-P" not in seen["argv"]
    assert seen["env"] == {"SQLCMDPASSWORD": "S3cret!"}


def test_a_windows_sqlcmd_gets_its_password_from_the_script_body():
    from db_ops.common import sqlcmd_run

    script = sqlcmd_run.winrm_script({"sql": "SELECT 1", "instance": "localhost", "username": "sa",
                                      "password": "it's"})

    assert "$env:SQLCMDPASSWORD = 'it''s'" in script
    assert "'-P'" not in script


# --------------------------------------------------------------------------- #
# F10.4 - the SQL Server backup/restore scripts
# --------------------------------------------------------------------------- #
from pathlib import Path  # noqa: E402 - grouped with the scripts it reads

_ROOT = Path(__file__).resolve().parents[1] / "db_ops" / "common"
_SCRIPTS = [_ROOT / "backup_scripts" / "sqlserver" / "mssql_backup_database.sh",
            _ROOT / "restore_scripts" / "sqlserver" / "mssql_restore.sh",
            _ROOT / "backup_scripts" / "sqlserver" / "mssql_backup_database.ps1"]


@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: p.name)
def test_no_script_passes_the_sql_password_as_an_argument(script):
    text = script.read_text(encoding="utf-8")

    assert '-P "$mssql_password"' not in text and "'-P', $mssqlPass" not in text
    assert "SQLCMDPASSWORD" in text


@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: p.name)
def test_every_batch_that_carries_the_encryption_password_goes_in_on_stdin_or_a_file(script):
    text = script.read_text(encoding="utf-8")
    callers = ("run_sql_secret", "Invoke-SqlSecret")
    for marker in ("ENCRYPTION BY PASSWORD", "DECRYPTION BY PASSWORD"):
        start = 0
        while (at := text.find(marker, start)) != -1:
            before = text[:at]
            opened = max(before.rfind(word) for word in ("run_sql", "Invoke-Sql", "with_key="))
            assert before[opened:].startswith(callers) or before[opened:].startswith("with_key="), \
                f"{script.name}: {marker} at {at} is not sent through a secret-safe call"
            start = at + 1


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("script", _SCRIPTS[:2], ids=lambda p: p.name)
def test_the_shell_scripts_still_parse(script):
    # The resolved path, not "bash": on Windows CreateProcess finds System32's WSL stub before the
    # bash on PATH, and it exits 127 on a machine with no distribution installed.
    subprocess.run([shutil.which("bash"), "-n", str(script)], check=True)


# --------------------------------------------------------------------------- #
# F5 / B8.1 - SRE commands that carry a password go as a script on stdin
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="runs the generated POSIX shell")
def test_a_script_for_a_node_reaches_it_on_the_hops_stdin(tmp_path, monkeypatch):
    from db_ops.sre import service

    config = SimpleNamespace(guest_user=lambda: "tuser", bastion_host=lambda: "10.0.0.1",
                             ssh_identity_file=lambda: "")
    request = service._remote_request(config, "printf '%s' \"$((6*7))\" > " + str(tmp_path / "ran"),
                                      host="10.0.0.9", script=True)
    assert "command" not in request

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # The hop: whatever it was asked to run, it runs the script it receives on stdin.
    (fake_bin / "ssh").write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" > {tmp_path}/hop.argv\nexec bash -s\n',
                                  encoding="utf-8")
    (fake_bin / "ssh").chmod(0o755)
    subprocess.run(["bash", "-s"], input=request["script"], text=True, check=True,
                   env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert (tmp_path / "ran").read_text(encoding="utf-8") == "42"
    assert "printf" not in (tmp_path / "hop.argv").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# F2.1 - a recovered console password is printed, not logged
# --------------------------------------------------------------------------- #
def test_user_password_show_bypasses_the_runtime_log(tmp_path, monkeypatch, capsys):
    import io
    import sys

    from db_ops.logging_ops.runtime_stdout import patch_stdout
    from db_ops.webhost import cli as webhost_cli

    log = tmp_path / "webhost_runtime.log"
    real = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", real)
    monkeypatch.setattr(sys, "stdout", real)
    patch_stdout(log, app_name="webhost")
    try:
        monkeypatch.setattr(webhost_cli, "_stores", lambda args: (None, SimpleNamespace(get_user=lambda name: {}), None, None))
        monkeypatch.setattr("db_ops.db.web_auth_store.recall_password", lambda name, key=None: "Hunter2!x")
        code = webhost_cli._handle_user_password_show(SimpleNamespace(username="thanh"), None, None)
        sys.stdout.flush()
    finally:
        sys.stdout = real

    assert code == 0
    assert "Hunter2!x" in real.getvalue()
    assert "Hunter2!x" not in (log.read_text(encoding="utf-8") if log.exists() else "")
