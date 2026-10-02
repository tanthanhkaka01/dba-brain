"""A step down in *how* something is done is said in the answer, and can be forbidden.

The owner's rule of 2026-10-01 has two halves. *Which* thing is acted on - host, port, database,
login - is stated or the command does not run (review notes G2, G3). *How* it is done may still step
down to a second way, because the second way is what reaches an old server or a host without tar;
but (G4) **the answer always says which way ran**, and **the way can be pinned**, and a pinned way
that cannot be used is an error, never a reason to try the other.

Each of these stepped down without a word in its answer:

* a backup directory crossing between two hosts - one tar stream, or one SFTP transfer per file,
  which across two internet hops is the difference between minutes and hours;
* a staged file taking its name - ``posix-rename`` in one step, or remove-then-rename, during which
  the destination does not exist;
* a command over WinRM - ``pypsrp``, or a local PowerShell driving ``Invoke-Command``, which a
  WORKGROUP host refuses with words that blame the host;
* a SQL Server connection - ODBC, or pymssql - the one that already reported itself
  (``run-sql``'s ``tool.actual``) and could already be pinned (``sqlserver_driver``).
"""

from __future__ import annotations

import stat
import sys
import types

import pytest

from db_ops.common import backup_copy, remote_exec, ssh_relay
from db_ops.lib.restore import copy_mode


# --------------------------------------------------------------------------- #
# The copy between two hosts: tar, or file by file
# --------------------------------------------------------------------------- #
class _Entry:
    def __init__(self, name: str, size: int) -> None:
        self.filename, self.st_size, self.st_mode = name, size, stat.S_IFREG | 0o644


class _Sftp:
    """A directory of files, enough of SFTP for the copy to list, probe and write one."""

    def __init__(self, files: dict[str, int]) -> None:
        self.files = dict(files)
        self.put: list[str] = []

    def listdir_attr(self, path):
        return [_Entry(name, size) for name, size in self.files.items()]

    def stat(self, path):
        return object()

    def mkdir(self, path):
        pass

    def remove(self, path):
        pass

    def open(self, path, mode="r"):
        class _File:
            def write(self, _data):
                pass

            def prefetch(self, _size):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        return _File()

    def putfo(self, _reader, remote_path, file_size=0):
        self.put.append(remote_path)


class _Session:
    def __init__(self, files: dict[str, int]) -> None:
        self._sftp = _Sftp(files)

    def sftp(self):
        return self._sftp


def _copy(monkeypatch, *, tar_works: bool, mode: str = "auto", target_files=None):
    """One new file to copy, with the tar stream working or not; returns (result, target, tried)."""
    source = _Session({"new.bak": 200})
    target = _Session({backup_copy.STAGING_MARKER: 1, **(target_files or {})})
    tried: list[int] = []
    monkeypatch.setattr(backup_copy, "_stream_files",
                        lambda **kwargs: tried.append(len(kwargs["files"])) or tar_works)
    result = backup_copy.sync_backup_dir(
        source_session=source, source_dir="/src", target_session=target, target_dir="/dst",
        copy_mode=mode)
    return result, target.sftp(), tried


def test_a_copy_that_went_as_one_stream_says_tar(monkeypatch):
    result, target, _tried = _copy(monkeypatch, tar_works=True)

    assert result.as_dict()["copy_mode"] == "tar"
    assert result.as_dict()["copy_fell_back"] is False
    assert target.put == []


def test_a_copy_that_stepped_down_to_one_file_at_a_time_says_so(monkeypatch):
    """It was a line on stderr: a drill that took hours looked, in its answer, like any other."""
    result, target, tried = _copy(monkeypatch, tar_works=False)

    assert tried == [1], "the stream is still tried first"
    assert target.put == ["/dst/new.bak"]
    assert result.as_dict()["copy_mode"] == "sftp"
    assert result.as_dict()["copy_fell_back"] is True


def test_a_copy_pinned_to_tar_does_not_go_file_by_file(monkeypatch):
    with pytest.raises(backup_copy.CopyModeError) as caught:
        _copy(monkeypatch, tar_works=False, mode="tar")

    assert "pinned" in str(caught.value) and "copy_mode" in str(caught.value)


def test_a_copy_pinned_to_tar_leaves_the_target_untouched_when_tar_fails(monkeypatch):
    source, target = _Session({"new.bak": 200}), _Session({backup_copy.STAGING_MARKER: 1})
    monkeypatch.setattr(backup_copy, "_stream_files", lambda **kwargs: False)

    with pytest.raises(backup_copy.CopyModeError):
        backup_copy.sync_backup_dir(source_session=source, source_dir="/src",
                                    target_session=target, target_dir="/dst", copy_mode="tar")

    assert target.sftp().put == []


def test_a_copy_pinned_to_sftp_never_tries_tar_and_did_not_fall_back(monkeypatch):
    result, target, tried = _copy(monkeypatch, tar_works=True, mode="sftp")

    assert tried == []
    assert target.put == ["/dst/new.bak"]
    assert (result.copy_mode, result.fell_back) == ("sftp", False), "chosen, not stepped down to"


def test_a_copy_with_nothing_to_move_says_none(monkeypatch):
    result, _target, tried = _copy(monkeypatch, tar_works=True, target_files={"new.bak": 200})

    assert tried == [] and result.as_dict()["copy_mode"] == "none"


def test_a_misspelled_pin_is_refused_rather_than_read_as_auto():
    """`"copy_mode": "tarr"` read as auto is a copy that steps down while its entry says it may not."""
    with pytest.raises(ValueError, match="copy_mode must be one of auto, tar, sftp"):
        copy_mode.parse_copy_mode("tarr")
    assert copy_mode.parse_copy_mode(None) == "auto"
    assert copy_mode.parse_copy_mode(" TAR ") == "tar"


def test_the_restore_entry_s_copy_mode_reaches_the_copy_command(monkeypatch):
    from db_ops.backup_restore import restore_script
    from db_ops.lib.time_window import TimeWindow

    job = restore_script.ScriptRestore(
        restore_id="PG_DRILL", db_type="postgresql", server_id="SRC", backup_dir="/backup",
        script="restore.sh", time_window=TimeWindow(), target_server_id="DST",
        target_backup_dir="/opt/db_ops/backup/pg_restore", source_backup_host_dir="/opt/backup/pg",
        copy_mode="tar")
    sent: dict[str, dict] = {}

    def run(command, request, **_kwargs):
        sent[command] = request
        return {"include": []} if command == "backup-chain" else {"copied": 0, "skipped": 0}

    monkeypatch.setattr(restore_script, "_ssh_login", lambda target, **_kwargs: {"host": "192.0.2.49"})
    monkeypatch.setattr(restore_script.common_cli, "run", run)
    endpoint = types.SimpleNamespace(container_name="")

    restore_script.transfer_backup_to_target(job, source=endpoint, target=endpoint, prune=False)

    assert sent["copy-backup-dir"]["copy_mode"] == "tar"


def test_an_entry_with_a_misspelled_copy_mode_is_refused_by_name(tmp_path):
    import json

    from db_ops.backup_restore import restore_script

    config = tmp_path / "restore_config.json"
    config.write_text(json.dumps({"backup_restore": {"restores": [{
        "restore_id": "PG_DRILL", "db_type": "postgresql", "server_id": "SRC",
        "target_server_id": "DST", "backup_dir": "/backup", "script": "restore.sh",
        "target_backup_dir": "/opt/db_ops/backup/pg_restore/stage",
        "source_backup_host_dir": "/opt/backup/pg", "copy_mode": "fast"}]}}), encoding="utf-8")

    with pytest.raises(ValueError, match="PG_DRILL: copy_mode must be one of"):
        restore_script.load_script_restores(str(config))


# --------------------------------------------------------------------------- #
# A staged file taking its name
# --------------------------------------------------------------------------- #
class _RenameSftp:
    def __init__(self, *, has_posix_rename: bool) -> None:
        self.has_posix_rename = has_posix_rename
        self.calls: list[str] = []

    def posix_rename(self, staged, destination):
        if not self.has_posix_rename:
            raise IOError("Operation unsupported")
        self.calls.append("posix_rename")

    def remove(self, destination):
        self.calls.append("remove")

    def rename(self, staged, destination):
        self.calls.append("rename")


def _replace(*, has_posix_rename: bool):
    sftp = _RenameSftp(has_posix_rename=has_posix_rename)
    session = types.SimpleNamespace(sftp=lambda: sftp)
    return ssh_relay.atomic_replace(session, "/tmp/a.dbops_partial", "/tmp/a"), sftp.calls


def test_a_replace_in_one_step_is_named_as_one():
    assert _replace(has_posix_rename=True) == ("posix-rename", ["posix_rename"])


def test_a_replace_that_removed_the_destination_first_says_so():
    """Between the two steps the destination does not exist - which nothing used to say."""
    assert _replace(has_posix_rename=False) == ("remove+rename", ["remove", "rename"])


def test_a_sent_file_s_answer_carries_how_it_took_its_name():
    from pathlib import Path

    from db_ops.common import file_transfer

    target = types.SimpleNamespace(server_id="LAB", host="192.0.2.49")
    common = dict(target=target, status="COPIED", local_path=Path("a"), remote_path="/tmp/a",
                  size=1, started=0.0)

    sent = file_transfer._result(direction="send", replace_mode="remove+rename", **common)
    fetched = file_transfer._result(direction="fetch", **common)

    assert sent["replace_mode"] == "remove+rename"
    assert "replace_mode" not in fetched, "a fetch renames nothing on the remote host"


# --------------------------------------------------------------------------- #
# A command over WinRM
# --------------------------------------------------------------------------- #
def _winrm_session():
    return remote_exec.open_session({"method": "winrm", "host": "192.0.2.60",
                                     "username": "operator", "password": "x"})


def _ran(session, backend: str = ""):
    return remote_exec.RemoteResult(method="winrm", host="192.0.2.60", command="hostname",
                                    exit_code=0, stdout="LAB\n", backend=backend)


def test_a_winrm_command_run_by_the_local_powershell_says_so(monkeypatch):
    """Only a failure ever named the backend: the same command and credential answered exit 0 the
    moment pypsrp was installed, after two days of a backup failing "on the host"."""
    monkeypatch.setitem(sys.modules, "pypsrp.client", None)
    session = _winrm_session()
    monkeypatch.setattr(type(session), "_run_via_local_powershell", lambda self, text, timeout: _ran(self))

    result = session.run_script("hostname")

    assert result.backend == "powershell"
    assert result.to_dict()["backend"] == "powershell"


def test_a_winrm_command_run_by_pypsrp_says_so(monkeypatch):
    module = types.ModuleType("pypsrp.client")
    module.Client = object
    monkeypatch.setitem(sys.modules, "pypsrp", types.ModuleType("pypsrp"))
    monkeypatch.setitem(sys.modules, "pypsrp.client", module)
    session = _winrm_session()
    monkeypatch.setattr(type(session), "_run_via_pypsrp", lambda self, client, text, timeout: _ran(self))

    assert session.run_script("hostname").to_dict()["backend"] == "pypsrp"


def test_an_answer_over_ssh_names_no_backend():
    over_ssh = remote_exec.RemoteResult(method="ssh", host="192.0.2.49", command="true", exit_code=0)

    assert "backend" not in over_ssh.to_dict()


# --------------------------------------------------------------------------- #
# A SQL Server connection - the one that already said
# --------------------------------------------------------------------------- #
def test_a_connection_that_stepped_down_to_pymssql_reports_it():
    from db_ops.common.sql_execution import SqlServerConnection

    stepped = SqlServerConnection(conn=None, driver="pymssql", odbc_error=RuntimeError("TLS"))
    first_try = SqlServerConnection(conn=None, driver="ODBC Driver 18 for SQL Server")

    assert stepped.describe_tool()["fell_back"] is True
    assert first_try.describe_tool()["fell_back"] is False
