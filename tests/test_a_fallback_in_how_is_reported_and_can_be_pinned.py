"""A step down in *how* something is done is said in the answer, and can be forbidden.

The owner's rule of 2026-10-01 has two halves. *Which* thing is acted on - host, port, database,
login - is stated or the command does not run (review notes G2, G3). *How* it is done may still step
down to a second way, because the second way is what reaches an old server or a host without tar;
but (G4) **the answer always says which way ran**, and **the way can be pinned**, and a pinned way
that cannot be used is an error, never a reason to try the other.

Each of these stepped down without a word in its answer:

* a backup directory crossing between two hosts - one tar stream, or one SFTP transfer per file,
  which across two internet hops was the difference between minutes and hours;
* a staged file taking its name - ``posix-rename`` in one step, or remove-then-rename, during which
  the destination does not exist;
* a command over WinRM - ``pypsrp``, or a local PowerShell driving ``Invoke-Command``, which a
  WORKGROUP host refuses with words that blame the host;
* a SQL Server connection - ODBC, or pymssql - the one that already reported itself
  (``run-sql``'s ``tool.actual``) and could already be pinned (``sqlserver_driver``).

The first of them has no step down left (0.38.0, 1.329): the bytes travel by ``copy-files``, each
file with its size and sha256, in the one way the entry's ``copy_mode`` names. The tests of the tar
stream trying first, of its fall to SFTP being reported, and of a pinned ``tar`` failing instead of
falling went with the stream. What is held of that case now: the entry's word reaches the command
as its ``transfer``, the answer still says which way ran, and a way that fails is tried again as
itself - never swapped for the other. The stream's own word, ``tar``, is refused by name: a pinned
way that cannot be used is an error, so it is not quietly served by ``cat``.
"""

from __future__ import annotations

import hashlib
import sys
import types

import pytest

from dbabrain.common import remote_exec, ssh_relay
from dbabrain.lib.integrity import RestoreIntegrity
from dbabrain.lib.restore import copy_mode


# --------------------------------------------------------------------------- #
# The copy between two hosts: the way the entry names, and no other
# --------------------------------------------------------------------------- #
SOURCE_HOST, TARGET_HOST = "192.0.2.49", "192.0.2.50"
PIECE = b"one backup piece"


def _staged_copy(monkeypatch, *, mode: str, already_staged: bool = False, lands: bool = True):
    """One restore entry with ``copy_mode`` = ``mode`` staging one file; returns (the call that
    runs it, every ``copy-files`` request it sent).

    ``already_staged`` puts the file on the target beforehand; ``lands`` false makes ``copy-files``
    answer its line as not copied, every time."""
    from dbabrain.backup_restore import restore_script
    from dbabrain.lib.time_window import TimeWindow

    job = restore_script.ScriptRestore(
        restore_id="PG_DRILL", db_type="postgresql", server_id="SRC", backup_dir="/backup",
        script="restore.sh", time_window=TimeWindow(), target_server_id="DST",
        target_backup_dir="/opt/db_ops/backup/pg_restore", source_backup_host_dir="/opt/backup/pg",
        copy_mode=copy_mode.parse_copy_mode(mode), integrity=RestoreIntegrity(copy_retry_interval_seconds=0))
    on_target = {"there": already_staged}
    copies: list[dict] = []
    selected = [{"path": "new.bak", "size_bytes": len(PIECE)}]

    def run(command, request, **_kwargs):
        if command == "prepare-staging":
            if request.get("plan_only"):
                return {"files": selected, "include": []}
            travelling = not on_target["there"] or "new.bak" in request["replace"]
            return {"files": selected, "pending": [{**selected[0], "mtime": 1_790_000_000}] if travelling else [],
                    "skipped": 0 if travelling else 1}
        if command == "file-checksum":
            held = request["host"]["host"] == SOURCE_HOST or on_target["there"]
            return {"files": [{"path": path, "sha256": hashlib.sha256(PIECE).hexdigest(),
                               "size_bytes": len(PIECE)} for path in request["paths"]] if held else []}
        if command == "copy-files":
            copies.append(request)
            on_target["there"] = lands
            return {"files": [{"target": one["target"], "ok": lands, "bytes_copied": len(PIECE) if lands else 0,
                               "error": None if lands else "Permission denied"} for one in request["files"]]}
        if command == "finish-staging":
            return {"timed": len(request["files"]), "not_timed": [], "opened_for_engine": True}
        raise AssertionError(f"unexpected command {command}")

    monkeypatch.setattr(restore_script.common_cli, "run", run)
    request = {"source": {"host": SOURCE_HOST}, "target": {"host": TARGET_HOST}, "include": []}
    return (lambda: restore_script._copy_gated_between_hosts(job, request, source=None, target=None)), copies


@pytest.mark.parametrize("mode, transfer", [("sftp", "sftp"), ("auto", "cat"), ("cat", "cat")])
def test_the_entry_s_copy_mode_reaches_copy_files_as_its_transfer(monkeypatch, mode, transfer):
    """``sftp`` pins SFTP, ``cat`` pins one cat stream per file, and ``auto`` is that stream too:
    the tar stream ``auto`` tried first went with ``copy-backup-dir``."""
    run, copies = _staged_copy(monkeypatch, mode=mode)

    run()

    assert [request["transfer"] for request in copies] == [transfer]


@pytest.mark.parametrize("mode, transfer", [("sftp", "sftp"), ("auto", "cat")])
def test_a_copy_says_in_its_answer_which_way_ran(monkeypatch, mode, transfer):
    """It was a line on stderr: a drill that took hours looked, in its answer, like any other."""
    run, _copies = _staged_copy(monkeypatch, mode=mode)

    assert run()["copy_mode"] == transfer


def test_a_copy_with_nothing_to_move_says_none(monkeypatch):
    run, copies = _staged_copy(monkeypatch, mode="sftp", already_staged=True)

    assert run()["copy_mode"] == "none"
    assert copies == []


@pytest.mark.parametrize("mode, transfer", [("sftp", "sftp"), ("auto", "cat")])
def test_a_way_that_fails_is_tried_again_as_itself_and_never_as_the_other(monkeypatch, mode, transfer):
    """The pin's other half, where it still has a home. A file that does not land is an attempt
    spent and the round is made again - by the same transfer every time, and the restore is refused
    when none of them lands it."""
    from dbabrain.backup_restore import restore_script

    run, copies = _staged_copy(monkeypatch, mode=mode, lands=False)

    with pytest.raises(restore_script.CopyNotProvenError):
        run()

    assert [request["transfer"] for request in copies] == [transfer] * 4, "copy_max_retry 3: four rounds, one way"


def test_a_misspelled_pin_is_refused_rather_than_read_as_auto():
    """`"copy_mode": "tarr"` read as auto is a copy that goes another way than its entry says."""
    with pytest.raises(ValueError, match="copy_mode must be one of auto, cat, sftp"):
        copy_mode.parse_copy_mode("tarr")
    assert copy_mode.parse_copy_mode(None) == "auto"
    assert copy_mode.parse_copy_mode(" SFTP ") == "sftp"


def test_the_tar_streams_word_is_refused_by_name_and_never_served_another_way():
    """An entry that pinned ``tar`` said: one tar stream, or an error. The stream is gone, so by
    the same rule the entry is an error - read as ``cat`` it would be a pin served by another way,
    and its owner would never be told. The sentence names what to write instead."""
    with pytest.raises(ValueError, match='copy_mode "tar" went with the tar stream in 0.38.0') as refused:
        copy_mode.parse_copy_mode(" TAR ")
    assert "auto, cat, sftp" in str(refused.value)
    assert "tar" not in copy_mode.COPY_MODES


def test_an_entry_still_pinning_tar_is_refused_by_its_id(tmp_path):
    import json

    from dbabrain.backup_restore import restore_script

    config = tmp_path / "restore_config.json"
    config.write_text(json.dumps({"backup_restore": {"restores": [{
        "restore_id": "PG_DRILL", "db_type": "postgresql", "server_id": "SRC",
        "target_server_id": "DST", "backup_dir": "/backup", "script": "restore.sh",
        "target_backup_dir": "/opt/db_ops/backup/pg_restore/stage",
        "source_backup_host_dir": "/opt/backup/pg", "copy_mode": "tar"}]}}), encoding="utf-8")

    with pytest.raises(ValueError, match='PG_DRILL: copy_mode "tar" went with the tar stream'):
        restore_script.load_script_restores(str(config))


def test_an_entry_with_a_misspelled_copy_mode_is_refused_by_name(tmp_path):
    import json

    from dbabrain.backup_restore import restore_script

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
    return ssh_relay.atomic_replace(session, "/tmp/a.dbabrain_partial", "/tmp/a"), sftp.calls


def test_a_replace_in_one_step_is_named_as_one():
    assert _replace(has_posix_rename=True) == ("posix-rename", ["posix_rename"])


def test_a_replace_that_removed_the_destination_first_says_so():
    """Between the two steps the destination does not exist - which nothing used to say."""
    assert _replace(has_posix_rename=False) == ("remove+rename", ["remove", "rename"])


def test_a_sent_file_s_answer_carries_how_it_took_its_name():
    from pathlib import Path

    from dbabrain.common import file_transfer

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
    from dbabrain.common.sql_execution import SqlServerConnection

    stepped = SqlServerConnection(conn=None, driver="pymssql", odbc_error=RuntimeError("TLS"))
    first_try = SqlServerConnection(conn=None, driver="ODBC Driver 18 for SQL Server")

    assert stepped.describe_tool()["fell_back"] is True
    assert first_try.describe_tool()["fell_back"] is False
