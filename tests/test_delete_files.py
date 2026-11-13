"""``delete-file`` is the forced delete of ONE stray file; ``delete-files`` deletes nothing.

Deleting backup files is the one operation where being wrong is not recoverable: a delete that
removed the wrong file removed the thing the restore was going to use. Until 0.38.0 these two
commands took a path, or a list of paths, and removed each with one ``rm`` - nothing on the host
held to anything. A backup file dbabrain recorded is now deleted only by ``delete-verified-file``
(its name, path, bytes and sha256 all stated and all true); what is left here is the one thing that
cannot do - clearing a stray file nobody recorded - and it is fenced as tightly as a delete without
stored facts can be (the operator, 2026-11-11): ``"force": true`` or nothing, one file per command,
never a folder, never a pattern, and the answer says what was removed.

Nothing here touches a filesystem: the point is the decision, and a test that had to create real
files could not exercise "the path is on a Windows host" at all. The real delete on this machine's
own disk is in ``test_a_backup_file_is_deleted_only_by_a_verified_command.py``.
"""

from __future__ import annotations

import json

import pytest

from dbabrain.common import cli_delete_files, deleteverified
from dbabrain.common.deleteverified import DeleteVerifiedError

SHA = "c" * 64
LINUX = {"runtime": "linux", "access": "ssh", "host": "192.0.2.40", "username": "u", "password": "p"}
WINDOWS = {"runtime": "windows", "access": "winrm", "host": "192.0.2.41", "username": "u", "password": "p"}


@pytest.fixture
def ran(monkeypatch):
    """Record every script that would have been run on the host, and answer with a scripted line."""
    calls: list[str] = []
    answer = {"line": f"DELETED\t10\t{SHA}"}

    def fake_run(host, script, *, timeout=None, session=None):
        calls.append(script)
        return {"stdout": answer["line"] + "\n", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(deleteverified, "run", fake_run)
    return SimpleCalls(calls, answer)


class SimpleCalls:
    def __init__(self, calls, answer):
        self.calls, self.answer = calls, answer


def _forced(path, root, host=LINUX, **more):
    return deleteverified.delete_forced_file({"host": host, "path": path, "must_be_under": root, "force": True, **more})


def test_without_force_nothing_is_run_and_the_verified_delete_is_named(ran):
    for value in (None, False, "true", 1):
        request = {"host": LINUX, "path": "/bk/a.bak", "must_be_under": "/bk"}
        if value is not None:
            request["force"] = value
        with pytest.raises(DeleteVerifiedError, match="delete-verified-file"):
            deleteverified.delete_forced_file(request)
    assert ran.calls == []


def test_a_wildcard_is_refused_rather_than_expanded(ran):
    for path in ("/bk/*.bak", "/bk/a?.bak", "/bk/[ab].bak"):
        with pytest.raises(DeleteVerifiedError, match="not a pattern"):
            _forced(path, "/bk")
    assert ran.calls == []


@pytest.mark.parametrize("path", ["bk/a.bak", "./a.bak", "a.bak"])
def test_a_relative_path_is_refused(ran, path):
    with pytest.raises(DeleteVerifiedError, match="absolute"):
        _forced(path, "/bk")
    assert ran.calls == []


def test_a_windows_unc_path_is_absolute(ran):
    row = _forced("\\\\filer\\share\\bk\\stray.tmp", "\\\\filer\\share\\bk", host=WINDOWS)["file"]
    assert row["status"] == "deleted" and len(ran.calls) == 1


def test_the_fence_is_required_and_refuses_a_path_outside_it(ran):
    with pytest.raises(DeleteVerifiedError, match="must_be_under is required"):
        deleteverified.delete_forced_file({"host": LINUX, "path": "/bk/a.bak", "force": True})
    with pytest.raises(DeleteVerifiedError, match="not inside must_be_under"):
        _forced("/var/lib/a.bak", "/bk")
    assert ran.calls == []


def test_the_fence_is_not_fooled_by_a_shared_prefix(ran):
    with pytest.raises(DeleteVerifiedError, match="not inside must_be_under"):
        _forced("/bk-other/a.bak", "/bk")
    # ... and is itself never the thing deleted.
    with pytest.raises(DeleteVerifiedError, match="not inside must_be_under"):
        _forced("/bk", "/bk")
    assert ran.calls == []


@pytest.mark.parametrize("extra", [{"paths": ["/bk/a.bak", "/bk/b.bak"]}, {"files": [{"path": "/bk/a.bak"}]}])
def test_a_list_is_refused_before_the_host_is_reached(ran, extra):
    with pytest.raises(DeleteVerifiedError, match="ONE file per command"):
        deleteverified.delete_forced_file({"host": LINUX, "must_be_under": "/bk", "force": True, **extra})
    assert ran.calls == []


def test_a_field_this_command_does_not_know_is_refused(ran):
    # `sha256` belongs to the verified delete: stated here it would look like a check and be none.
    with pytest.raises(DeleteVerifiedError, match="unknown field"):
        _forced("/bk/a.bak", "/bk", sha256=SHA)
    assert ran.calls == []


def test_a_file_that_is_already_gone_is_a_success(ran):
    ran.answer["line"] = "MISSING"
    answer = _forced("/bk/a.bak", "/bk")
    assert answer["file"]["status"] == "not_found" and answer["deleted"] is False


@pytest.mark.parametrize("token, word", [("DIR", "folder"), ("LINK", "link"), ("NOT_REGULAR", "regular file"),
                                         (f"CHANGED\t11\t{SHA}", "changed while")])
def test_what_is_not_one_quiet_regular_file_is_refused_by_the_host(ran, token, word):
    ran.answer["line"] = token
    row = _forced("/bk/a.bak", "/bk")["file"]
    assert row["status"] == "refused" and word in row["reason"]


def test_the_answer_records_the_bytes_and_sha256_of_what_was_removed(ran):
    row = _forced("/bk/a.bak", "/bk")["file"]
    assert (row["status"], row["size_bytes_found"], row["sha256_found"], row["file_name"]) == (
        "deleted", 10, SHA, "a.bak")


def test_a_delete_the_host_cannot_describe_is_a_failure_not_a_success(ran):
    ran.answer["line"] = "DELETED"
    assert _forced("/bk/a.bak", "/bk")["file"]["status"] == "failed"
    ran.answer["line"] = "all good!"
    assert _forced("/bk/a.bak", "/bk")["file"]["status"] == "failed"


def test_a_dry_run_script_contains_no_delete_and_a_real_one_removes_one_file(ran):
    ran.answer["line"] = f"WOULD\t10\t{SHA}"
    assert _forced("/bk/a.bak", "/bk", dry_run=True)["file"]["status"] == "skipped"
    assert "rm --" not in ran.calls[-1]
    ran.answer["line"] = f"DELETED\t10\t{SHA}"
    _forced("/bk/a.bak", "/bk")
    script = ran.calls[-1]
    assert script.count("rm -- \"$p\"") == 1 and "rm -r" not in script and "rm -f" not in script
    _forced("C:\\bk\\a.bak", "C:\\bk", host=WINDOWS)
    assert "[System.IO.File]::Delete" in ran.calls[-1] and "Remove-Item" not in ran.calls[-1]


# ---------------------------------------------------------------------------- the two commands
def _read(payload):
    return lambda _arg, _usage: (payload, 0)


def test_the_command_says_what_it_removed_in_one_line(ran, capsys):
    code = cli_delete_files.run("delete-file", ["-"], read_request=_read(
        {"host": LINUX, "path": "/bk/stray.partial", "must_be_under": "/bk", "force": True}))
    answer = json.loads(capsys.readouterr().out)
    assert code == 0 and answer["success"] is True
    assert answer["message"] == f"/bk/stray.partial deleted (forced): 10 bytes, sha256 {SHA}."
    assert answer["data"]["forced"] is True and answer["metrics"]["freed_bytes"] == 10


def test_a_refused_file_is_a_failed_answer_that_still_carries_the_row(ran, capsys):
    ran.answer["line"] = "DIR"
    code = cli_delete_files.run("delete-file", ["-"], read_request=_read(
        {"host": LINUX, "path": "/bk/set", "must_be_under": "/bk", "force": True}))
    answer = json.loads(capsys.readouterr().out)
    assert code != 0 and answer["success"] is False and "folder" in answer["error"]
    assert answer["data"]["file"]["status"] == "refused"


def test_delete_files_reads_its_request_and_then_refuses_it(ran, capsys):
    code = cli_delete_files.run("delete-files", ["-"], read_request=_read(
        {"host": LINUX, "paths": ["/bk/a.bak"], "must_be_under": "/bk", "force": True}))
    answer = json.loads(capsys.readouterr().out)
    assert code != 0 and answer["success"] is False
    assert "one command never deletes two files" in answer["error"]
    for replacement in ("delete-verified-file", "delete-verified-files", "delete-file", "delete-empty-folder"):
        assert replacement in answer["error"]
    assert ran.calls == []
