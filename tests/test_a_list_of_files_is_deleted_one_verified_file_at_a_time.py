"""A list of files is deleted one verified file at a time - each item its own facts, its own script (0.37.0).

Why it matters: the operator's delete rules (2026-11-10) say a command deletes one file whose name,
path, bytes and checksum are all stated and all true. Cleaning up a restored test cluster is thousands
of files, and one process and one SSH login per file is hours - so the operator allowed *a list of
files* (same day, translated: "delete it, but by an array of files"). The list must change the
transport and nothing else:

* every item states its own name, path, bytes, sha256 and fence - an item without one is refused, and
  so is the whole list, BEFORE the host is reached;
* every item is checked and removed by its own script - the same one ``delete-verified-file`` sends,
  one non-recursive delete of one file - and all of them go over one connection;
* the first item that is not deleted stops the list: what follows is ``not_attempted`` and is never
  sent, so a list built from a stale listing cannot carry on deleting past the first surprise;
* the same for empty folders, in the order given: a parent named before its child stops the list;
* the single-file command still refuses a list: the list is its own command, never a field.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.common import deleteverified as D

WINDOWS_HERE = os.name == "nt"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _git_bash() -> str | None:
    if not WINDOWS_HERE:
        return shutil.which("sh")
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if candidate and os.path.exists(candidate) and "System32" not in candidate:
            return candidate
    return None


SH = _git_bash()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _posix(path: Path | str) -> str:
    text = str(path)
    if WINDOWS_HERE and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


def _run_sh(script: str) -> dict:
    done = subprocess.run([SH, "-c", script], capture_output=True, text=True, timeout=120)
    return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


def _run_powershell(script: str) -> dict:
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    done = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                          capture_output=True, text=True, timeout=120)
    return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": ""}


LINUX = {"runtime": "linux", "host": "192.0.2.10", "username": "u", "password": "p"}
WIN = {"runtime": "windows"}


class _Session:
    """The one connection: counts how often it is opened and closed, and what is sent over it."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def sessions():
    box = SimpleNamespace(opened=[])

    def opener(host):
        session = _Session()
        box.opened.append(session)
        return session

    box.opener = opener
    return box


@pytest.fixture
def host_never_reached(monkeypatch):
    def run(*_a, **_k):
        raise AssertionError("the host was reached for a list that must be refused before it")
    monkeypatch.setattr(D, "run", run)

    def opener(_host):
        raise AssertionError("a connection was opened for a list that must be refused before it")
    return opener


def _item(path: str, root: str, data: bytes, **override) -> dict:
    return {"path": path, "file_name": path.replace("\\", "/").rsplit("/", 1)[-1],
            "size_bytes": len(data), "sha256": _sha(data), "must_be_under": root, **override}


# --------------------------------------------------------------------------- #
# 1. The whole list is refused before the host is reached
# --------------------------------------------------------------------------- #
_A = _item("/nas/_test/pgdata/base/1/1247", "/nas/_test", b"a")
_B = _item("/nas/_test/pgdata/base/1/1249", "/nas/_test", b"b")


@pytest.mark.parametrize("files, words", [
    (None, "non-empty list"),
    ([], "non-empty list"),
    ("not a list", "non-empty list"),
    (["/nas/_test/a"], "must be an object"),
    ([_A, {**_B, "host": LINUX}], "files[1]: unknown field(s) host"),
    ([_A, {**_B, "dry_run": True}], "files[1]: unknown field(s) dry_run"),
    ([_A, {k: v for k, v in _B.items() if k != "sha256"}], "files[1]: sha256 is required"),
    ([_A, {k: v for k, v in _B.items() if k != "size_bytes"}], "files[1]: size_bytes is required"),
    ([_A, {k: v for k, v in _B.items() if k != "must_be_under"}], "files[1]: must_be_under is required"),
    ([_A, {**_B, "file_name": "other"}], "files[1]: file_name 'other' is not the last part"),
    ([_A, {**_B, "path": "/etc/passwd", "file_name": "passwd"}], "files[1]: path '/etc/passwd' is not inside"),
    ([_A, {**_B, "path": "/nas/_test/pgdata/*", "file_name": "*"}], "files[1]: path must name one place"),
    ([_A, {**_B, "path": "/nas/_test", "file_name": "_test"}], "is not inside"),   # the fence itself
    ([_A, {**_B, "sha256": "abc"}], "files[1]: sha256 is required and must be 64"),
    ([_A, dict(_A)], "names '/nas/_test/pgdata/base/1/1247' twice"),
])
def test_one_bad_item_refuses_the_whole_list_before_the_host_is_reached(host_never_reached, files, words):
    with pytest.raises(D.DeleteVerifiedError) as caught:
        D.delete_verified_files({"host": LINUX, "files": files}, open_session=host_never_reached)
    assert words in str(caught.value)


def test_a_list_too_long_is_refused(host_never_reached):
    files = [_item(f"/nas/_test/f{i}", "/nas/_test", b"x") for i in range(D.MAX_ITEMS + 1)]
    with pytest.raises(D.DeleteVerifiedError, match="at most"):
        D.delete_verified_files({"host": LINUX, "files": files}, open_session=host_never_reached)


@pytest.mark.parametrize("extra", [{"path": "/nas/_test/a"}, {"paths": ["/nas/_test/a"]}, {"sha_256": "x"}])
def test_an_unknown_field_on_the_list_is_refused(host_never_reached, extra):
    with pytest.raises(D.DeleteVerifiedError, match="unknown field"):
        D.delete_verified_files({"host": LINUX, "files": [_A], **extra}, open_session=host_never_reached)


@pytest.mark.parametrize("field", ["files", "folders", "paths"])
def test_the_single_file_command_still_refuses_a_list(host_never_reached, field):
    with pytest.raises(D.DeleteVerifiedError, match="ONE file per command"):
        D.delete_verified_file({**_A, "host": LINUX, field: [_A]})


def test_the_single_folder_command_still_refuses_a_list(host_never_reached):
    with pytest.raises(D.DeleteVerifiedError, match="ONE folder per command"):
        D.delete_empty_folder({"host": LINUX, "path": "/nas/_test/a", "must_be_under": "/nas/_test",
                               "folders": [{"path": "/nas/_test/b", "must_be_under": "/nas/_test"}]})


def test_a_folder_list_item_outside_its_fence_refuses_the_list(host_never_reached):
    with pytest.raises(D.DeleteVerifiedError, match=r"folders\[1\]: path '/etc' is not inside"):
        D.delete_empty_folders({"host": LINUX, "folders": [
            {"path": "/nas/_test/a", "must_be_under": "/nas/_test"},
            {"path": "/etc", "must_be_under": "/nas/_test"}]}, open_session=host_never_reached)


# --------------------------------------------------------------------------- #
# 2. Over one connection, one script per item - the single command's own script
# --------------------------------------------------------------------------- #
@pytest.fixture
def scripted(monkeypatch):
    """``D.run`` answering from a table keyed by path; records (session, command) for every call."""
    box = SimpleNamespace(sent=[], answers={})

    def run(host, command, *, timeout=300, session=None):
        box.sent.append((session, command))
        for path, answer in box.answers.items():
            if path in command:
                return {"exit_code": 0, "stdout": answer, "stderr": ""}
        return {"exit_code": 0, "stdout": "MISSING\n", "stderr": ""}

    monkeypatch.setattr(D, "run", run)
    return box


def test_every_item_is_its_own_script_over_one_connection(scripted, sessions):
    scripted.answers = {_A["path"]: f"DELETED\t1\t{_sha(b'a')}\n", _B["path"]: f"DELETED\t1\t{_sha(b'b')}\n"}

    answer = D.delete_verified_files({"host": LINUX, "files": [_A, _B]}, open_session=sessions.opener)

    assert len(sessions.opened) == 1 and sessions.opened[0].closed == 1, "one connection, closed once"
    assert [session for session, _ in scripted.sent] == [sessions.opened[0]] * 2
    assert [command for _, command in scripted.sent] == [
        D.posix_file_script(_A["path"], size=1, sha256=_sha(b"a"), dry_run=False),
        D.posix_file_script(_B["path"], size=1, sha256=_sha(b"b"), dry_run=False)]
    assert answer["deleted"] == 2 and answer["complete"] is True and answer["stopped_at"] is None


def test_the_first_refusal_stops_the_list_and_what_follows_is_never_sent(scripted, sessions):
    c = _item("/nas/_test/pgdata/base/1/1255", "/nas/_test", b"c")
    scripted.answers = {_A["path"]: f"DELETED\t1\t{_sha(b'a')}\n", _B["path"]: f"SHA\t1\t{_sha(b'B')}\n"}

    answer = D.delete_verified_files({"host": LINUX, "files": [_A, _B, c]}, open_session=sessions.opener)

    assert [row["status"] for row in answer["files"]] == [D.DELETED, D.REFUSED, D.NOT_ATTEMPTED]
    assert len(scripted.sent) == 2 and c["path"] not in scripted.sent[-1][1], "the third was never sent"
    assert answer["complete"] is False and answer["stopped_at"]["path"] == _B["path"]
    assert sessions.opened[0].closed == 1, "closed even when the list stops"


def test_a_failed_delete_stops_the_list_too(scripted, sessions):
    scripted.answers = {_A["path"]: f"FAILED\t1\t{_sha(b'a')}\tstill there after rm\n"}

    answer = D.delete_verified_files({"host": LINUX, "files": [_A, _B]}, open_session=sessions.opener)

    assert [row["status"] for row in answer["files"]] == [D.FAILED, D.NOT_ATTEMPTED]


def test_a_file_already_gone_is_not_found_and_the_list_goes_on(scripted, sessions):
    scripted.answers = {_B["path"]: f"DELETED\t1\t{_sha(b'b')}\n"}       # _A answers MISSING

    answer = D.delete_verified_files({"host": LINUX, "files": [_A, _B]}, open_session=sessions.opener)

    assert [row["status"] for row in answer["files"]] == [D.NOT_FOUND, D.DELETED]
    assert answer["complete"] is True and answer["not_found"] == 1


def test_an_answer_reporting_other_facts_is_a_failure_and_stops_the_list(scripted, sessions):
    scripted.answers = {_A["path"]: f"DELETED\t2\t{_sha(b'zz')}\n"}

    answer = D.delete_verified_files({"host": LINUX, "files": [_A, _B]}, open_session=sessions.opener)

    assert [row["status"] for row in answer["files"]] == [D.FAILED, D.NOT_ATTEMPTED]


def test_no_connection_means_nothing_is_attempted(monkeypatch):
    def run(*_a, **_k):
        raise AssertionError("nothing may run without the connection")
    monkeypatch.setattr(D, "run", run)

    def refused(_host):
        raise D.HostCommandError("could not connect to u@192.0.2.10: refused")

    answer = D.delete_verified_files({"host": LINUX, "files": [_A, _B]}, open_session=refused)

    assert [row["status"] for row in answer["files"]] == [D.NOT_ATTEMPTED, D.NOT_ATTEMPTED]
    assert answer["complete"] is False and "refused" in answer["files"][0]["reason"]


def test_every_script_sent_removes_one_file_and_nothing_else(scripted, sessions):
    files = [_item(f"/nas/_test/pgdata/base/1/{n}", "/nas/_test", b"x") for n in range(5)]

    D.delete_verified_files({"host": LINUX, "files": files}, open_session=sessions.opener)

    for _, command in scripted.sent:
        assert command.count("rm ") == 1 and command.count("rm --") == 1
        assert "rm -r" not in command and "rm -f" not in command


# --------------------------------------------------------------------------- #
# 3. For real - POSIX sh and PowerShell against real files
# --------------------------------------------------------------------------- #
def _shells() -> list:
    out = []
    if SH:
        out.append("posix")
    if WINDOWS_HERE and POWERSHELL:
        out.append("windows")
    return out or [pytest.param("none", marks=pytest.mark.skip(reason="no shell"))]


@pytest.fixture(params=_shells())
def tree(request, tmp_path, monkeypatch):
    kind = request.param

    def run(host, command, *, timeout=300, session=None):
        return _run_powershell(command) if host.is_windows else _run_sh(command)
    monkeypatch.setattr(D, "run", run)
    root = tmp_path / "_test"
    name = (lambda p: str(p)) if kind == "windows" else _posix
    return SimpleNamespace(root=root, name=name, host=WIN if kind == "windows" else LINUX,
                           opener=lambda _h: _Session())


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _items_for(tree, *pairs) -> list:
    return [_item(tree.name(path), tree.name(tree.root), data) for path, data in pairs]


def test_a_whole_list_that_matches_is_deleted_and_nothing_beside_it(tree):
    a = _write(tree.root / "pgdata" / "base" / "1" / "1247", b"one")
    b = _write(tree.root / "pgdata" / "base" / "1" / "1249", b"two")
    kept = _write(tree.root / "pgdata" / "base" / "1" / "1255", b"kept")

    answer = D.delete_verified_files({"host": tree.host, "files": _items_for(tree, (a, b"one"), (b, b"two"))},
                                     open_session=tree.opener)

    assert answer["deleted"] == 2 and answer["complete"] is True
    assert not a.exists() and not b.exists() and kept.read_bytes() == b"kept"


def test_a_changed_file_in_the_middle_stops_the_list_and_keeps_it_and_everything_after(tree):
    a = _write(tree.root / "a", b"one")
    b = _write(tree.root / "b", b"TWO")              # the listing said "two"
    c = _write(tree.root / "c", b"three")

    answer = D.delete_verified_files(
        {"host": tree.host, "files": _items_for(tree, (a, b"one"), (b, b"two"), (c, b"three"))},
        open_session=tree.opener)

    assert [row["status"] for row in answer["files"]] == [D.DELETED, D.REFUSED, D.NOT_ATTEMPTED]
    assert not a.exists() and b.read_bytes() == b"TWO" and c.read_bytes() == b"three"


def test_a_dry_run_list_checks_every_file_and_deletes_none(tree):
    a = _write(tree.root / "a", b"one")
    b = _write(tree.root / "b", b"two")

    answer = D.delete_verified_files({"host": tree.host, "files": _items_for(tree, (a, b"one"), (b, b"two")),
                                      "dry_run": True}, open_session=tree.opener)

    assert answer["skipped"] == 2 and a.exists() and b.exists()


def test_a_folder_in_the_list_is_refused_and_everything_in_it_kept(tree):
    folder = tree.root / "pgdata" / "base"
    inside = _write(folder / "1" / "1247", b"one")

    answer = D.delete_verified_files({"host": tree.host, "files": _items_for(tree, (folder, b"one"))},
                                     open_session=tree.opener)

    assert answer["files"][0]["status"] == D.REFUSED and inside.exists()


def test_empty_folders_deepest_first_are_all_deleted(tree):
    deep = tree.root / "pgdata" / "base" / "1"
    deep.mkdir(parents=True)
    fence = tree.name(tree.root)

    answer = D.delete_empty_folders({"host": tree.host, "folders": [
        {"path": tree.name(deep), "must_be_under": fence},
        {"path": tree.name(deep.parent), "must_be_under": fence},
        {"path": tree.name(deep.parent.parent), "must_be_under": fence}]}, open_session=tree.opener)

    assert answer["deleted"] == 3 and answer["complete"] is True
    assert tree.root.is_dir() and not (tree.root / "pgdata").exists(), "the fence itself stays"


def test_a_parent_named_before_its_child_stops_the_list_and_both_are_kept(tree):
    deep = tree.root / "pgdata" / "base"
    deep.mkdir(parents=True)
    fence = tree.name(tree.root)

    answer = D.delete_empty_folders({"host": tree.host, "folders": [
        {"path": tree.name(deep.parent), "must_be_under": fence},
        {"path": tree.name(deep), "must_be_under": fence}]}, open_session=tree.opener)

    assert [row["status"] for row in answer["folders"]] == [D.REFUSED, D.NOT_ATTEMPTED]
    assert deep.is_dir()


def test_a_folder_holding_a_file_stops_the_folder_list(tree):
    folder = tree.root / "pgdata"
    left = _write(folder / "PG_VERSION", b"18")

    answer = D.delete_empty_folders({"host": tree.host, "folders": [
        {"path": tree.name(folder), "must_be_under": tree.name(tree.root)}]}, open_session=tree.opener)

    assert answer["folders"][0]["status"] == D.REFUSED and answer["folders"][0]["entries"] == 1
    assert left.exists()


# --------------------------------------------------------------------------- #
# 4. The command line: a list that stopped is a failed response
# --------------------------------------------------------------------------- #
def test_a_stopped_list_is_a_failed_response_naming_where_it_stopped(scripted, monkeypatch, capsys):
    from dbabrain.common import cli_delete_verified

    scripted.answers = {_A["path"]: "SIZE\t9\n"}
    monkeypatch.setattr(D, "open_session", lambda _h: _Session())
    request = {"host": LINUX, "files": [_A, _B]}

    code = cli_delete_verified.run("delete-verified-files", ["-"], read_request=lambda _a, _u: (request, 0))

    answer = json.loads(capsys.readouterr().out)
    assert code != 0 and answer["success"] is False
    assert _A["path"] in answer["message"] and answer["data"]["not_attempted"] == 1


def test_a_complete_list_is_a_success_counting_freed_bytes(scripted, monkeypatch, capsys):
    from dbabrain.common import cli_delete_verified

    scripted.answers = {_A["path"]: f"DELETED\t1\t{_sha(b'a')}\n", _B["path"]: f"DELETED\t1\t{_sha(b'b')}\n"}
    monkeypatch.setattr(D, "open_session", lambda _h: _Session())
    request = {"host": LINUX, "files": [_A, _B]}

    code = cli_delete_verified.run("delete-verified-files", ["-"], read_request=lambda _a, _u: (request, 0))

    answer = json.loads(capsys.readouterr().out)
    assert code == 0 and answer["success"] is True and answer["metrics"]["freed_bytes"] == 2
