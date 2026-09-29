"""A script is a file on the machine that runs it - never the shell's stdin.

Until 0.25.0 a bash script went to the remote shell as its **stdin** (``bash -s``). Any command in
it that read stdin read the rest of the script instead: ``docker compose exec`` does (``-i`` is its
default), so does ``cat`` or ``read``. The run then ended early with exit 0, no error, and the tail
never ran. On 2026-09-28 a rehearsal on a lab host lost the end of three scripts that way before
anyone saw why (1.65). A PowerShell script went as ``-EncodedCommand``, which runs out of command
line past ~8 KB of script.

So every script is written to a file first - private to the login, created where no other run can
hold it - run with stdin closed, and removed. One way, in ``remote_exec``, for every caller.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from db_ops.common import remote_exec as rx


def _local():
    return rx.open_session({"method": "local"})


def test_a_local_script_runs_from_a_private_file_with_an_empty_stdin(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        path = argv[-1]
        seen["argv"], seen["input"] = argv, kwargs.get("input")
        with open(path, "rb") as file:
            seen["bytes"] = file.read()
        seen["mode"] = os.stat(path).st_mode & 0o777
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(rx.subprocess, "run", fake_run)

    with _local() as session:
        result = session.run_script("echo ok", shell="bash", env={"PASSPHRASE": "s3cret"})

    assert seen["argv"][0] == "bash" and seen["argv"][1].endswith(".sh")
    assert os.path.basename(seen["argv"][1]).startswith(rx.SCRIPT_FILE_PREFIX)
    assert seen["input"] == "", "an empty stdin, never this process's own"
    assert seen["bytes"] == b"export PASSPHRASE='s3cret'\necho ok"
    if os.name == "posix":
        assert seen["mode"] == 0o600, "the prelude can hold a passphrase"
    assert not os.path.exists(seen["argv"][1]), "removed after the run"
    assert result.stdout == "ok\n"


def test_a_local_powershell_script_is_a_file_with_a_bom(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        with open(argv[-1], "rb") as file:
            seen["bytes"] = file.read()
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(rx.subprocess, "run", fake_run)

    with _local() as session:
        session.run_script("Get-Date", shell="powershell")

    assert seen["argv"][-2] == "-File" and seen["argv"][-1].endswith(".ps1")
    assert seen["bytes"] == b"\xef\xbb\xbfGet-Date"


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"),
                    reason="needs a POSIX bash - ci's Linux runners have one")
def test_a_command_that_reads_stdin_no_longer_swallows_the_rest_of_the_script():
    """The failure itself, on a real shell: ``cat`` read the script's remaining lines."""
    with _local() as session:
        result = session.run_script("cat\necho after", shell="bash")

    assert result.exit_code == 0
    assert result.stdout == "after\n"


def test_an_ssh_script_is_removed_even_when_the_run_fails(monkeypatch):
    removed: list[str] = []

    class _Handle:
        def write(self, data):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

    class _Sftp:
        def open(self, name, mode):
            return _Handle()

        def chmod(self, name, mode):
            pass

        def normalize(self, name):
            return "/home/u/" + name

        def remove(self, name):
            removed.append(name)

    session = rx.SshSession(rx.RemoteAccess.from_json(
        {"method": "ssh", "host": "192.0.2.10", "username": "u", "auth_type": "password", "password": "p"}))
    monkeypatch.setattr(session, "sftp", lambda: _Sftp())

    def dropped(*_args, **_kwargs):
        raise rx.RemoteExecError("SSH command execution error: connection dropped", method="ssh")

    monkeypatch.setattr(session, "run", dropped)

    with pytest.raises(rx.RemoteExecError):
        session.run_script("echo hi", shell="bash")

    assert len(removed) == 1 and removed[0].startswith(rx.SCRIPT_FILE_PREFIX)


def test_a_cmd_script_is_still_one_command_line():
    """cmd.exe has no script-from-stdin mode and needs no file: its lines are joined."""
    command, stdin = rx._script_to_command("echo a\n\necho b", shell=rx.SHELL_CMD)

    assert command == "cmd.exe /c echo a && echo b" and stdin is None
