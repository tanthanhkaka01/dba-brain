"""`lib.remote_host.RemoteHost` - what an app holds instead of an SSH client (rules R03).

`control` drove the worker and `backup_restore` staged backups through a raw paramiko client out of
`common.ssh`. They hold a `RemoteHost` now, and every method is one `common.cli` call. These pin what
the callers rely on, against a fake of the transport's reader: which command each method sends and
in what shape, that a password never lands on a command line, that "the command never ran" is not
confused with "the command failed", and that a batch of files travels as one tar under exactly the
paths it was given.
"""

from __future__ import annotations

import io
import stat as stat_mod
import tarfile
from pathlib import Path

import pytest

from db_ops.lib import remote_host
from db_ops.lib.common_cli import CommonCliError
from db_ops.lib.remote_host import RemoteError, RemoteHost


class FakeCli:
    """Answers like the transport's reader; remembers every request."""

    def __init__(self, answers=None):
        self.calls: list[tuple[str, dict]] = []
        self.answers = answers or {}
        self.pushed: dict[str, bytes] = {}

    def __call__(self, command, request, *, timeout_seconds=None):
        self.calls.append((command, request))
        if command == "push-file":
            self.pushed[request["remote_path"]] = Path(request["local_path"]).read_bytes()
            return True, {"verified": True}, ""
        answer = self.answers.get(command)
        if callable(answer):
            return answer(request)
        return answer or (True, {"exit_code": 0, "stdout": "", "stderr": ""}, "")


def _host(cli, **over):
    kwargs = {"host": "192.0.2.10", "username": "dev", "password": "pw", "call": cli}
    kwargs.update(over)
    return RemoteHost(**kwargs)


def test_a_command_goes_to_run_cmd_with_the_login_in_the_access_block_only():
    cli = FakeCli()
    _host(cli).run("docker ps", sudo=True)

    command, request = cli.calls[0]
    assert command == "run-cmd"
    assert request["command"] == "docker ps" and request["sudo"] is True
    assert request["access"]["method"] == "ssh" and request["access"]["password"] == "pw"
    assert "pw" not in request["command"], "sudo is run-cmd's: the password goes on stdin there"
    assert request["confirm"] is True and request["assume_yes"] is True


def test_a_file_goes_with_the_host_block_push_and_pull_read():
    """`runtime` means the machine's OS there and where a command runs in run-cmd - two blocks."""
    cli = FakeCli(answers={"pull-file": (True, {"verified": True}, "")})
    host = _host(cli)
    host.get("/opt/x/a.json", "a.json")

    command, request = cli.calls[0]
    assert command == "pull-file"
    assert request["host"]["runtime"] == "linux" and request["host"]["access"] == "ssh"
    assert "runtime" not in host.access()


def test_a_command_that_never_ran_is_an_error_not_an_exit_code():
    cli = FakeCli(answers={"run-cmd": (False, {}, "Authentication failed.")})
    with pytest.raises(RemoteError, match="Authentication failed"):
        _host(cli).run("true")


def test_a_command_that_ran_and_failed_is_its_exit_code():
    cli = FakeCli(answers={"run-cmd": (False, {"exit_code": 2, "stdout": "", "stderr": "no match"}, "exit=2")})
    result = _host(cli).run("grep x y")
    assert (result.exit_code, result.stderr, result.ok) == (2, "no match", False)


def test_no_answer_at_all_is_a_remote_error_so_except_ioerror_still_catches_it():
    def broken(_request):
        raise CommonCliError("run-cmd exited 1 without a JSON response")

    with pytest.raises(OSError):
        _host(FakeCli(answers={"run-cmd": broken})).run("true")


def test_stat_reads_size_mtime_and_mode_and_a_missing_path_is_file_not_found():
    cli = FakeCli(answers={"run-cmd": (True, {"exit_code": 0, "stdout": "8555 1790391141 81a4\n", "stderr": ""}, "")})
    entry = _host(cli).stat("/opt/x/app_commands.json")
    assert (entry.filename, entry.st_size, entry.st_mtime) == ("app_commands.json", 8555, 1790391141.0)
    assert stat_mod.S_ISREG(entry.st_mode) and stat_mod.S_IMODE(entry.st_mode) == 0o644

    missing = FakeCli(answers={"run-cmd": (False, {"exit_code": 1, "stdout": "", "stderr": "cannot stat"}, "")})
    with pytest.raises(FileNotFoundError):
        _host(missing).stat("/nope")


def test_a_listing_names_directories_as_directories():
    out = "4096 1790000000.5 755 d tasks\n120 1790000001.0 644 f a b.sql\n"
    cli = FakeCli(answers={"run-cmd": (True, {"exit_code": 0, "stdout": out, "stderr": ""}, "")})
    entries = _host(cli).listdir_attr("/opt/x/assets")
    assert [(e.filename, e.is_dir) for e in entries] == [("a b.sql", False), ("tasks", True)]


def test_small_files_travel_as_one_tar_under_their_exact_paths_and_a_big_one_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_host, "BATCH_FILE_LIMIT_BYTES", 10)
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "big.tar").write_bytes(b"x" * 64)
    cli = FakeCli()
    arrived: list[str] = []

    _host(cli).put_files([(tmp_path / "a.json", "/opt/w/data/a.json"),
                          (tmp_path / "big.tar", "/opt/w/image.tar")],
                         on_file=lambda _local, remote: arrived.append(remote))

    pushes = [request["remote_path"] for command, request in cli.calls if command == "push-file"]
    assert pushes[0] == "/opt/w/image.tar"
    assert pushes[1].startswith("/tmp/db_ops_push_") and pushes[1].endswith(".tar")
    with tarfile.open(fileobj=io.BytesIO(cli.pushed[pushes[1]])) as tar:
        assert tar.getnames() == ["opt/w/data/a.json"]
    commands = [request["command"] for command, request in cli.calls if command == "run-cmd"]
    assert commands[0].startswith("tar -xf /tmp/db_ops_push_") and "-C / --no-same-owner" in commands[0]
    assert commands[1].startswith("rm -f -- /tmp/db_ops_push_"), "the staged tar is removed"
    assert sorted(arrived) == ["/opt/w/data/a.json", "/opt/w/image.tar"]


def test_many_files_come_back_as_one_tar(tmp_path):
    staged: dict[str, str] = {}

    def pack(request):
        if "script" in request:   # the pack; the other run-cmd is the staged tar's removal
            staged["script"] = request["script"]
        return True, {"exit_code": 0, "stdout": "", "stderr": ""}, ""

    def pull(request):
        archive = Path(request["local_path"])
        with tarfile.open(archive, "w") as tar:
            for name, body in (("opt/w/a.sql", b"select 1"), ("opt/w/t/b.sql", b"select 2")):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        return True, {"verified": True}, ""

    cli = FakeCli(answers={"run-cmd": pack, "pull-file": pull})
    _host(cli).get_files([("/opt/w/a.sql", tmp_path / "a.sql"), ("/opt/w/t/b.sql", tmp_path / "t" / "b.sql")])

    assert (tmp_path / "t" / "b.sql").read_bytes() == b"select 2"
    assert "opt/w/a.sql\nopt/w/t/b.sql" in staged["script"], "names go on a here-document, not argv"
    assert [command for command, _ in cli.calls].count("pull-file") == 1
