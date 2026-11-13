"""A backup file is deleted ONE at a time, and only when its name, path, bytes and sha256 all match (0.37.0).

Why it matters: the delete at the source after a verified repository copy removes the only file on
the database server. The operator, 2026-11-10 (translated): *deleting this file is very dangerous - the
code must be very careful and very well tested before anything is deleted*, with six rules:

1. never one command that deletes two files or more;
2. never one command that deletes a folder that still holds a file - even one file must fail;
3. each command deletes one file only, or one empty folder only;
4. a file delete carries the file's name, path, bytes and checksum, and deletes only when all are right;
5. the tests catch every case, the forbidden ones included;
6. it works on Windows and Ubuntu, over SSH and over WinRM.

So this file holds, in that order:

* every request a delete must never act on is refused BEFORE the host is reached (the host function is
  replaced by one that fails the test if it is called);
* the scripts themselves, run for real - PowerShell on Windows, POSIX ``sh`` (Git Bash here, ``sh`` on
  Linux) - against real files: a match deletes exactly that one file; a different size, a different
  sha256, a folder, a link, a missing file each leave everything as it was;
* the empty-folder delete, for real: one entry of any kind - a file, a hidden file, an empty
  sub-folder - refuses;
* the way to each host: WinRM sends the PowerShell itself, SSH to Ubuntu sends the POSIX script, SSH to
  a Windows OpenSSH server and a local Windows run wrap it in ``-EncodedCommand``, a container gets
  ``docker exec ... sh -lc`` - and each, fed to a real shell, does what it says;
* an answer from the host that is not exactly one well-formed line, or that reports a delete of
  other facts than the ones stated, is a failure - never read as success;
* the scripts can only ever remove one thing: no recursive flag, no pattern, no second operand.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.common import deleteverified as D
from dbabrain.common import hostcmd

WINDOWS_HERE = os.name == "nt"
# The Windows script, run where its paths are Windows paths: a Linux runner has pwsh too, and its
# POSIX temp folder is no path the script takes (0.38.0, the first CI run on Linux).
POWERSHELL = (shutil.which("powershell") or shutil.which("pwsh")) if os.name == "nt" else None


def _git_bash() -> str | None:
    if not WINDOWS_HERE:
        return shutil.which("sh")
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if candidate and os.path.exists(candidate) and "System32" not in candidate:
            return candidate
    return None


SH = _git_bash()
needs_powershell = pytest.mark.skipif(not POWERSHELL, reason="PowerShell runs the Windows script")
needs_sh = pytest.mark.skipif(not SH, reason="a POSIX shell runs the Linux script")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _posix(path: Path | str) -> str:
    """A path as the POSIX shell here names it (Git Bash: /c/Users/...)."""
    text = str(path)
    if WINDOWS_HERE and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


# --------------------------------------------------------------------------- #
# Real shells, standing in for the host
# --------------------------------------------------------------------------- #
def _run_powershell(script: str) -> dict:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    done = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                          capture_output=True, text=True, timeout=120)
    return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": ""}


def _run_sh(script: str) -> dict:
    done = subprocess.run([SH, "-c", script], capture_output=True, text=True, timeout=120)
    return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


@pytest.fixture
def on_shell(monkeypatch):
    """``D.run`` replaced by the real shell the host's runtime names - every request rule and answer
    parse is the module's own; only the transport is local."""
    def run(host, command, *, timeout=300, session=None):
        return _run_powershell(command) if host.is_windows else _run_sh(command)
    monkeypatch.setattr(D, "run", run)


@pytest.fixture
def host_never_reached(monkeypatch):
    def run(*_a, **_k):
        raise AssertionError("the host was reached for a request that must be refused before it")
    monkeypatch.setattr(D, "run", run)


WIN = {"runtime": "windows"}
LINUX = {"runtime": "linux", "host": "192.0.2.10", "username": "u", "password": "p"}


def _file_request(path: str, root: str, data: bytes, **override) -> dict:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return {"host": LINUX, "path": path, "file_name": name, "size_bytes": len(data), "sha256": _sha(data),
            "must_be_under": root, **override}


# --------------------------------------------------------------------------- #
# 1. Refused before the host is reached
# --------------------------------------------------------------------------- #
_P, _R, _DATA = "/backup/APPDB/FULL/APPDB_FULL.bak", "/backup", b"full backup bytes"


@pytest.mark.parametrize("override, words", [
    # rule 1 and 3: one file per command, never a list
    ({"paths": [_P, "/backup/APPDB/LOG/a.trn"]}, "ONE file per command"),
    ({"paths": [_P]}, "ONE file per command"),
    ({"files": [{"path": _P}]}, "ONE file per command"),
    # rule 4: every fact stated, and stated right
    ({"path": None}, "path is required"),
    ({"file_name": None}, "file_name is required"),
    ({"size_bytes": None}, "size_bytes is required"),
    ({"sha256": None}, "sha256 is required"),
    ({"must_be_under": None}, "must_be_under is required"),
    ({"must_be_under": ""}, "must_be_under is required"),
    ({"file_name": "APPDB_FULL_OTHER.bak"}, "is not the last part of path"),
    ({"file_name": "FULL/APPDB_FULL.bak"}, "is not the last part of path"),
    ({"size_bytes": -1}, "whole number of bytes"),
    ({"size_bytes": True}, "whole number of bytes"),
    ({"size_bytes": 17.0}, "whole number of bytes"),
    ({"size_bytes": "17"}, "whole number of bytes"),
    ({"sha256": "abc"}, "64 hexadecimal"),
    ({"sha256": "z" * 64}, "64 hexadecimal"),
    ({"sha256": _sha(_DATA) + "0"}, "64 hexadecimal"),
    ({"sha256": 12345}, "64 hexadecimal"),
    # a misspelt field is a check not made - refused, never ignored
    ({"sha_256": _sha(_DATA)}, "unknown field"),
    ({"mustBeUnder": _R}, "unknown field"),
    ({"recursive": True}, "unknown field"),
    # one place, named in full, inside its fence
    ({"path": "/backup/APPDB/FULL/*.bak", "file_name": "*.bak"}, "not a pattern"),
    ({"path": "/backup/APPDB/FULL/APPDB_FULL?.bak", "file_name": "APPDB_FULL?.bak"}, "not a pattern"),
    ({"path": "/backup/APPDB/FULL/[A]PPDB.bak", "file_name": "[A]PPDB.bak"}, "not a pattern"),
    ({"must_be_under": "/backup/*"}, "not a pattern"),
    ({"path": "backup/APPDB/FULL/APPDB_FULL.bak"}, "must be absolute"),
    ({"must_be_under": "backup"}, "must be absolute"),
    ({"path": "/backup/APPDB/../APPDB/FULL/APPDB_FULL.bak"}, "'.' or '..'"),
    ({"path": "/backup/./APPDB/FULL/APPDB_FULL.bak"}, "'.' or '..'"),
    ({"must_be_under": "/backup/APPDB/.."}, "'.' or '..'"),
    ({"path": "/etc/APPDB_FULL.bak"}, "is not inside must_be_under"),
    ({"path": "/backup2/APPDB_FULL.bak"}, "is not inside must_be_under"),
    ({"path": "/backup/APPDB/FULL/APPDB_FULL.bak", "must_be_under": "/backup/APPDB/FULL/APPDB_FULL.bak"},
     "is not inside must_be_under"),
    ({"path": "/backup/APPDB/FULL/APPDB_FULL.bak/", "file_name": "APPDB_FULL.bak"}, "end in a separator"),
    ({"path": "/backup/APPDB/FULL/APPDB\nFULL.bak", "file_name": "APPDB\nFULL.bak"}, "control character"),
    ({"path": "/backup/APPDB/FULL/APPDB_FULL.bak "}, "start or end with a space"),
    ({"dry_run": "yes"}, "true or false"),
    ({"timeout_seconds": 1}, "10 or more"),
])
def test_a_file_delete_that_must_never_run_is_refused_before_the_host_is_reached(
        host_never_reached, override, words):
    # None in an override is the field left out.
    request = {k: v for k, v in {**_file_request(_P, _R, _DATA), **override}.items() if v is not None}
    with pytest.raises(D.DeleteVerifiedError, match=words):
        D.delete_verified_file(request)


@pytest.mark.parametrize("override, words", [
    ({"path": "D:\\SQLBK\\HR\\FULL\\HR.bak:hidden", "file_name": "HR.bak:hidden"}, "':' past its drive"),
    ({"path": "SQLBK\\HR\\FULL\\HR.bak"}, "must be absolute"),
    ({"path": "D:\\SQLBK2\\HR.bak", "file_name": "HR.bak"}, "is not inside must_be_under"),
    ({"path": "D:\\SQLBK", "file_name": "SQLBK"}, "is not inside must_be_under"),
    ({"path": "D:\\SQLBK\\HR\\..\\HR\\FULL\\HR.bak"}, "'.' or '..'"),
])
def test_a_windows_path_is_held_to_the_same_rules(host_never_reached, override, words):
    request = {**_file_request("D:\\SQLBK\\HR\\FULL\\HR.bak", "D:\\SQLBK", _DATA), "host": WIN, **override}
    with pytest.raises(D.DeleteVerifiedError, match=words):
        D.delete_verified_file(request)


@pytest.mark.parametrize("override, words", [
    ({"paths": ["/backup/APPDB/FULL", "/backup/APPDB/LOG"]}, "ONE folder per command"),
    ({"recursive": True}, "unknown field"),
    ({"force": True}, "unknown field"),
    ({"sha256": "0" * 64}, "unknown field"),
    ({"must_be_under": None}, "must_be_under is required"),
    ({"path": "/backup"}, "is not inside must_be_under"),
    ({"path": "/"}, "end in a separator"),
    ({"path": "/backup/*"}, "not a pattern"),
    ({"path": "/backup/APPDB/.."}, "'.' or '..'"),
    ({"path": "relative/folder"}, "must be absolute"),
])
def test_a_folder_delete_that_must_never_run_is_refused_before_the_host_is_reached(
        host_never_reached, override, words):
    request = {"host": LINUX, "path": "/backup/APPDB/FULL", "must_be_under": "/backup", **override}
    request = {k: v for k, v in request.items() if v is not None}
    with pytest.raises(D.DeleteVerifiedError, match=words):
        D.delete_empty_folder(request)


def test_a_posix_runtime_for_this_windows_machine_is_refused_rather_than_handed_to_cmd(host_never_reached,
                                                                                      monkeypatch):
    monkeypatch.setattr(D.os, "name", "nt")
    with pytest.raises(D.DeleteVerifiedError, match='must be "windows"'):
        D.delete_verified_file({**_file_request(_P, _R, _DATA), "host": {"runtime": "linux"}})


# --------------------------------------------------------------------------- #
# 2. The file delete, run for real - Windows PowerShell and POSIX sh
# --------------------------------------------------------------------------- #
def _shells():
    out = []
    if POWERSHELL:
        out.append(pytest.param("windows", id="powershell"))
    if SH:
        out.append(pytest.param("posix", id="sh"))
    return out or [pytest.param("none", marks=pytest.mark.skip(reason="no shell to run the scripts"))]


class _Folder:
    """A backup folder on this machine, named as the chosen shell names it."""

    def __init__(self, root: Path, kind: str) -> None:
        self.root, self.kind = root, kind
        self.host = WIN if kind == "windows" else LINUX

    def name(self, path: Path) -> str:
        return str(path) if self.kind == "windows" else _posix(path)

    def file_request(self, path: Path, data: bytes, **override) -> dict:
        return {**_file_request(self.name(path), self.name(self.root), data), "host": self.host, **override}

    def folder_request(self, path: Path, **override) -> dict:
        return {"host": self.host, "path": self.name(path), "must_be_under": self.name(self.root), **override}


@pytest.fixture(params=_shells())
def backup(request, tmp_path, on_shell):
    root = tmp_path / "backup"
    (root / "APPDB" / "FULL").mkdir(parents=True)
    return _Folder(root, request.param)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_a_file_whose_every_fact_matches_is_deleted_and_only_that_file(backup):
    full = _write(backup.root / "APPDB" / "FULL" / "APPDB_FULL_1.bak", b"full one")
    other = _write(backup.root / "APPDB" / "FULL" / "APPDB_FULL_2.bak", b"full two")

    answer = D.delete_verified_file(backup.file_request(full, b"full one"))

    assert answer["deleted"] is True and answer["file"]["status"] == D.DELETED
    assert answer["file"]["size_bytes_found"] == len(b"full one")
    assert answer["file"]["sha256_found"] == _sha(b"full one")
    assert not full.exists() and other.read_bytes() == b"full two", "one command, one file"


def test_a_file_of_another_size_is_refused_and_kept(backup):
    full = _write(backup.root / "APPDB" / "FULL" / "APPDB_FULL.bak", b"full one")

    answer = D.delete_verified_file(backup.file_request(full, b"full one", size_bytes=9))

    assert answer["file"]["status"] == D.REFUSED and answer["file"]["size_bytes_found"] == 8
    assert full.read_bytes() == b"full one"


def test_a_file_of_the_same_size_with_other_bytes_is_refused_and_kept(backup):
    full = _write(backup.root / "APPDB" / "FULL" / "APPDB_FULL.bak", b"full ONE")   # encrypted, say

    answer = D.delete_verified_file(backup.file_request(full, b"full one"))

    assert answer["file"]["status"] == D.REFUSED and "checksum" in answer["file"]["reason"]
    assert answer["file"]["sha256_found"] == _sha(b"full ONE")
    assert full.read_bytes() == b"full ONE"


def test_a_dry_run_checks_every_fact_and_deletes_nothing(backup):
    full = _write(backup.root / "APPDB" / "FULL" / "APPDB_FULL.bak", b"full one")

    answer = D.delete_verified_file(backup.file_request(full, b"full one", dry_run=True))

    assert answer["file"]["status"] == D.SKIPPED and answer["deleted"] is False
    assert answer["file"]["sha256_found"] == _sha(b"full one") and full.exists()


def test_a_file_that_is_not_there_is_not_found_and_nothing_else_is_touched(backup):
    other = _write(backup.root / "APPDB" / "FULL" / "APPDB_FULL_2.bak", b"full two")

    answer = D.delete_verified_file(backup.file_request(backup.root / "APPDB" / "FULL" / "APPDB_FULL_1.bak",
                                                         b"full one"))

    assert answer["file"]["status"] == D.NOT_FOUND and other.exists()


def test_a_folder_named_as_a_file_is_refused_and_everything_in_it_kept(backup):
    folder = backup.root / "APPDB" / "FULL"
    inside = _write(folder / "APPDB_FULL.bak", b"full one")

    answer = D.delete_verified_file(backup.file_request(folder, b"full one"))

    assert answer["file"]["status"] == D.REFUSED and "folder" in answer["file"]["reason"]
    assert inside.exists() and folder.is_dir()


def test_an_empty_file_is_deleted_only_with_its_own_facts(backup):
    empty = _write(backup.root / "APPDB" / "FULL" / "empty.bak", b"")

    assert D.delete_verified_file(backup.file_request(empty, b"x"))["file"]["status"] == D.REFUSED
    assert empty.exists()
    assert D.delete_verified_file(backup.file_request(empty, b""))["file"]["status"] == D.DELETED
    assert not empty.exists()


def test_a_name_with_spaces_and_quotes_is_one_file_not_several(backup):
    odd = _write(backup.root / "APPDB" / "FULL" / "APP DB 'full' $x; rm.bak", b"odd")
    near = _write(backup.root / "APPDB" / "FULL" / "APP", b"near")

    answer = D.delete_verified_file(backup.file_request(odd, b"odd"))

    assert answer["file"]["status"] == D.DELETED and not odd.exists() and near.exists()


def test_a_link_is_refused_and_neither_it_nor_its_target_is_deleted(backup, tmp_path):
    target = _write(tmp_path / "elsewhere" / "precious.bak", b"precious")
    link = backup.root / "APPDB" / "FULL" / "precious.bak"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot create a symbolic link without privilege")

    answer = D.delete_verified_file(backup.file_request(link, b"precious"))

    assert answer["file"]["status"] == D.REFUSED and "link" in answer["file"]["reason"]
    assert os.path.islink(link) and target.read_bytes() == b"precious"


# --------------------------------------------------------------------------- #
# 3. The empty-folder delete, run for real
# --------------------------------------------------------------------------- #
def test_an_empty_folder_is_deleted(backup):
    folder = backup.root / "APPDB" / "LOG"
    folder.mkdir()

    answer = D.delete_empty_folder(backup.folder_request(folder))

    assert answer["folder"]["status"] == D.DELETED and not folder.exists()
    assert (backup.root / "APPDB" / "FULL").is_dir(), "its sibling is not touched"


@pytest.mark.parametrize("entry", ["one.bak", ".hidden", "sub/"])
def test_a_folder_with_even_one_entry_is_refused_and_kept_whole(backup, entry):
    folder = backup.root / "APPDB" / "LOG"
    if entry.endswith("/"):
        (folder / entry.rstrip("/")).mkdir(parents=True)
    else:
        _write(folder / entry, b"x")

    answer = D.delete_empty_folder(backup.folder_request(folder))

    assert answer["folder"]["status"] == D.REFUSED and answer["folder"]["entries"] == 1
    assert (folder / entry.rstrip("/")).exists()


def test_a_folder_with_many_entries_says_how_many(backup):
    folder = backup.root / "APPDB" / "FULL"
    for index in range(3):
        _write(folder / f"f{index}.bak", b"x")

    answer = D.delete_empty_folder(backup.folder_request(folder))

    assert answer["folder"]["status"] == D.REFUSED and answer["folder"]["entries"] == 3
    assert len(list(folder.iterdir())) == 3


def test_a_file_named_as_a_folder_is_refused_and_kept(backup):
    full = _write(backup.root / "APPDB" / "FULL" / "APPDB_FULL.bak", b"full one")

    answer = D.delete_empty_folder(backup.folder_request(full))

    assert answer["folder"]["status"] == D.REFUSED and full.exists()


def test_a_folder_that_is_not_there_is_not_found(backup):
    answer = D.delete_empty_folder(backup.folder_request(backup.root / "APPDB" / "GONE"))
    assert answer["folder"]["status"] == D.NOT_FOUND


def test_a_dry_run_of_an_empty_folder_deletes_nothing(backup):
    folder = backup.root / "APPDB" / "LOG"
    folder.mkdir()

    answer = D.delete_empty_folder(backup.folder_request(folder, dry_run=True))

    assert answer["folder"]["status"] == D.SKIPPED and folder.is_dir()


@pytest.mark.skipif(not WINDOWS_HERE or not POWERSHELL, reason="a junction is a Windows object")
def test_a_junction_to_a_full_folder_is_refused_and_what_it_points_at_is_kept(tmp_path, on_shell):
    root = tmp_path / "backup"
    root.mkdir()
    target = tmp_path / "elsewhere"
    _write(target / "precious.bak", b"precious")
    junction = root / "linked"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("mklink /J is not available")
    try:
        answer = D.delete_empty_folder({"host": WIN, "path": str(junction), "must_be_under": str(root)})
        assert answer["folder"]["status"] == D.REFUSED and "link" in answer["folder"]["reason"]
        assert (target / "precious.bak").read_bytes() == b"precious"
    finally:
        os.rmdir(junction)


# --------------------------------------------------------------------------- #
# 4. The way to each host - WinRM, SSH to Ubuntu, SSH to Windows, a container, here
# --------------------------------------------------------------------------- #
class _Live:
    """A remote_exec session: records the command it is given and answers ``reply(command)``."""

    def __init__(self, reply) -> None:
        self.reply, self.sent = reply, []

    def run(self, command, timeout_seconds=None):
        self.sent.append(command)
        return SimpleNamespace(exit_code=0, stdout=self.reply(command), stderr="")

    def close(self):
        pass


@pytest.fixture
def remote(monkeypatch):
    """hostcmd's one bridge to remote_exec, replaced: the module's own ``run`` decides the command."""
    box = SimpleNamespace(live=None, hosts=[])

    def through(host, session, call):
        box.hosts.append(host)
        result = call(box.live)
        return {"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}

    monkeypatch.setattr(hostcmd, "_through_remote_exec", through)
    return box


_SHA_ONE = _sha(b"full one")


def _request_to(host: dict, path: str, root: str) -> dict:
    return {"host": host, "path": path, "file_name": path.replace("\\", "/").rsplit("/", 1)[-1],
            "size_bytes": 8, "sha256": _SHA_ONE, "must_be_under": root}


def _decoded(command: str) -> str:
    assert command.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    return base64.b64decode(command.rsplit(" ", 1)[-1]).decode("utf-16-le")


def test_over_winrm_the_powershell_script_itself_is_sent(remote):
    remote.live = _Live(lambda _c: f"DELETED\t8\t{_SHA_ONE}\r\n")
    host = {"runtime": "windows", "access": "winrm", "host": "192.0.2.20", "username": "u", "password": "p"}

    answer = D.delete_verified_file(_request_to(host, "D:\\SQLBK\\HR\\FULL\\HR.bak", "D:\\SQLBK"))

    [sent] = remote.live.sent
    assert sent == D.windows_file_script("D:\\SQLBK\\HR\\FULL\\HR.bak", size=8, sha256=_SHA_ONE, dry_run=False)
    assert remote.hosts[0].is_winrm and answer["file"]["status"] == D.DELETED


def test_over_ssh_to_ubuntu_the_posix_script_itself_is_sent(remote):
    remote.live = _Live(lambda _c: f"DELETED\t8\t{_SHA_ONE}\n")

    answer = D.delete_verified_file(_request_to(LINUX, "/backup/APPDB/FULL/a.bak", "/backup"))

    assert remote.live.sent == [D.posix_file_script("/backup/APPDB/FULL/a.bak", size=8, sha256=_SHA_ONE,
                                                    dry_run=False)]
    assert answer["file"]["status"] == D.DELETED


def test_over_ssh_to_a_windows_openssh_server_the_script_travels_encoded(remote):
    remote.live = _Live(lambda _c: f"WOULD\t8\t{_SHA_ONE}\r\n")
    host = {"runtime": "windows", "access": "ssh", "host": "192.0.2.20", "username": "u", "password": "p"}

    answer = D.delete_verified_file({**_request_to(host, "D:\\SQLBK\\a.bak", "D:\\SQLBK"), "dry_run": True})

    [sent] = remote.live.sent
    assert _decoded(sent) == D.windows_file_script("D:\\SQLBK\\a.bak", size=8, sha256=_SHA_ONE, dry_run=True)
    assert answer["file"]["status"] == D.SKIPPED


def test_in_a_container_the_script_runs_through_docker_exec(remote):
    remote.live = _Live(lambda _c: "MISSING\n")
    host = {**LINUX, "runtime": "docker", "container": "pg_lab", "sudo": True}

    answer = D.delete_verified_file(_request_to(host, "/backup/a.bak", "/backup"))

    [sent] = remote.live.sent
    assert " exec -i pg_lab sh -lc " in sent and answer["file"]["status"] == D.NOT_FOUND


@needs_powershell
def test_what_winrm_is_sent_deletes_the_file_when_powershell_runs_it(remote, tmp_path):
    full = _write(tmp_path / "SQLBK" / "HR" / "HR.bak", b"full one")
    remote.live = _Live(lambda command: _run_powershell(command)["stdout"])
    host = {"runtime": "windows", "access": "winrm", "host": "192.0.2.20", "username": "u", "password": "p"}

    answer = D.delete_verified_file(_request_to(host, str(full), str(tmp_path / "SQLBK")))

    assert answer["file"]["status"] == D.DELETED and not full.exists()


@needs_sh
def test_what_ssh_to_ubuntu_is_sent_deletes_the_file_when_sh_runs_it(remote, tmp_path):
    full = _write(tmp_path / "backup" / "a.bak", b"full one")
    remote.live = _Live(lambda command: _run_sh(command)["stdout"])

    answer = D.delete_verified_file(_request_to(LINUX, _posix(full), _posix(tmp_path / "backup")))

    assert answer["file"]["status"] == D.DELETED and not full.exists()


@pytest.mark.skipif(not WINDOWS_HERE or not POWERSHELL, reason="the local Windows path")
def test_on_this_windows_machine_the_real_transport_deletes_one_file(tmp_path):
    """No stand-in at all: hostcmd.run, remote_exec's local session, cmd.exe, -EncodedCommand."""
    full = _write(tmp_path / "SQLBK" / "a.bak", b"full one")
    keep = _write(tmp_path / "SQLBK" / "b.bak", b"full one")

    answer = D.delete_verified_file({**_request_to(WIN, str(full), str(tmp_path / "SQLBK"))})

    assert answer["file"]["status"] == D.DELETED and not full.exists() and keep.exists()


@pytest.mark.skipif(WINDOWS_HERE, reason="the local Linux path - the worker and CI")
def test_on_this_linux_machine_the_real_transport_deletes_one_file(tmp_path):
    full = _write(tmp_path / "backup" / "a.bak", b"full one")
    keep = _write(tmp_path / "backup" / "b.bak", b"full one")

    answer = D.delete_verified_file(_request_to({"runtime": "linux"}, str(full), str(tmp_path / "backup")))

    assert answer["file"]["status"] == D.DELETED and not full.exists() and keep.exists()


# --------------------------------------------------------------------------- #
# 5. An answer that is not exactly what the script prints is never success
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stdout, status", [
    (f"DELETED\t9\t{_SHA_ONE}\n", D.FAILED),                       # a delete of other facts
    (f"DELETED\t8\t{'0' * 64}\n", D.FAILED),
    ("DELETED\n", D.FAILED),                                     # a delete with no facts at all
    (f"DELETED\t8\t{_SHA_ONE}\nDELETED\t8\t{_SHA_ONE}\n", D.FAILED),  # two lines: never two deletes
    ("", D.FAILED),
    ("Welcome to Ubuntu\n", D.FAILED),
    ("SIZE\t9\n", D.REFUSED),
    (f"SHA\t8\t{'1' * 64}\n", D.REFUSED),
    (f"CHANGED\t9\t{_SHA_ONE}\n", D.REFUSED),
    ("LINK\n", D.REFUSED),
    ("DIR\n", D.REFUSED),
    ("NOT_REGULAR\n", D.REFUSED),
    ("UNREADABLE\t\t\tPermission denied\n", D.REFUSED),
    (f"FAILED\t8\t{_SHA_ONE}\tstill there after rm\n", D.FAILED),
])
def test_only_a_well_formed_answer_with_the_stated_facts_reads_as_deleted(remote, stdout, status):
    remote.live = _Live(lambda _c: stdout)
    answer = D.delete_verified_file(_request_to(LINUX, "/backup/a.bak", "/backup"))
    assert answer["file"]["status"] == status and answer["deleted"] is False


def test_a_host_that_cannot_be_reached_is_a_failure_not_a_delete(monkeypatch):
    def unreachable(*_a, **_k):
        raise hostcmd.HostCommandError("could not connect to u@192.0.2.10")
    monkeypatch.setattr(D, "run", unreachable)

    answer = D.delete_verified_file(_request_to(LINUX, "/backup/a.bak", "/backup"))

    assert answer["file"]["status"] == D.FAILED and "could not connect" in answer["file"]["reason"]


@pytest.mark.parametrize("stdout, status", [
    ("DELETED\n", D.DELETED), ("NOT_EMPTY\t1\n", D.REFUSED), ("FILE\n", D.REFUSED), ("LINK\n", D.REFUSED),
    ("DELETED\nDELETED\n", D.FAILED), ("", D.FAILED), ("REMOVED\n", D.FAILED),
])
def test_a_folder_answer_is_read_as_strictly(remote, stdout, status):
    remote.live = _Live(lambda _c: stdout)
    answer = D.delete_empty_folder({"host": LINUX, "path": "/backup/APPDB", "must_be_under": "/backup"})
    assert answer["folder"]["status"] == status


# --------------------------------------------------------------------------- #
# 6. The scripts can only ever remove one thing
# --------------------------------------------------------------------------- #
def _scripts():
    path, sha = "/backup/APPDB/FULL/a.bak", _SHA_ONE
    return {
        "posix file": D.posix_file_script(path, size=8, sha256=sha, dry_run=False),
        "windows file": D.windows_file_script("D:\\SQLBK\\a.bak", size=8, sha256=sha, dry_run=False),
        "posix folder": D.posix_folder_script("/backup/APPDB", dry_run=False),
        "windows folder": D.windows_folder_script("D:\\SQLBK\\HR", dry_run=False),
    }


def test_each_script_holds_exactly_one_removal_of_exactly_one_named_thing():
    scripts = _scripts()
    assert scripts["posix file"].count('rm -- "$p"') == 1 and scripts["posix file"].count("rm ") == 1
    assert scripts["windows file"].count("[System.IO.File]::Delete($i.FullName)") == 1
    assert scripts["posix folder"].count('rmdir -- "$p"') == 1 and "rm " not in scripts["posix folder"]
    assert scripts["windows folder"].count("[System.IO.Directory]::Delete($i.FullName,$false)") == 1


@pytest.mark.parametrize("forbidden", ["rm -r", "rm -f", "rm -rf", " -R ", "Remove-Item", "-Recurse",
                                       "rmtree", "$true)", "find ", "xargs", "/*", "*.", "del ", "rd /s"])
def test_no_script_can_recurse_force_or_expand_a_pattern(forbidden):
    for name, script in _scripts().items():
        assert forbidden not in script, f"{name} holds {forbidden!r}"


def test_a_dry_run_script_removes_nothing_at_all():
    for script in (D.posix_file_script("/b/a.bak", size=1, sha256=_SHA_ONE, dry_run=True),
                   D.posix_folder_script("/b/x", dry_run=True)):
        assert "rm" not in script.replace("printf", "")
    for script in (D.windows_file_script("D:\\b\\a.bak", size=1, sha256=_SHA_ONE, dry_run=True),
                   D.windows_folder_script("D:\\b\\x", dry_run=True)):
        assert "Delete(" not in script


def test_the_file_script_checks_size_and_sha256_before_its_one_removal():
    for name in ("posix file", "windows file"):
        script = _scripts()[name]
        removal = script.index("rm --") if name.startswith("posix") else script.index("File]::Delete(")
        assert script.index("SIZE") < removal and script.index("SHA") < removal and script.index("CHANGED") < removal


# --------------------------------------------------------------------------- #
# 7. Through common.cli - a refusal is a failed response, a delete an ok one
# --------------------------------------------------------------------------- #
def _cli(capsys, command, request):
    from dbabrain.common import cli

    code = cli.main([command, json.dumps(request)])
    return code, json.loads(capsys.readouterr().out)


def test_the_cli_answers_a_list_with_a_failed_response_and_deletes_nothing(capsys, host_never_reached):
    code, body = _cli(capsys, "delete-verified-file", {**_file_request(_P, _R, _DATA), "paths": [_P]})
    assert code != 0 and body["success"] is False and "ONE file per command" in body["error"]


def test_the_cli_answers_a_refused_file_with_a_failed_response_carrying_what_the_host_found(capsys, monkeypatch):
    monkeypatch.setattr(D, "run", lambda *_a, **_k: {"exit_code": 0, "stdout": "SIZE\t9\n", "stderr": ""})
    code, body = _cli(capsys, "delete-verified-file", _file_request(_P, _R, _DATA))
    assert code != 0 and body["success"] is False and body["data"]["file"]["size_bytes_found"] == 9


def test_the_cli_answers_a_deleted_file_with_an_ok_response_and_the_bytes_freed(capsys, monkeypatch):
    stdout = f"DELETED\t{len(_DATA)}\t{_sha(_DATA)}\n"
    monkeypatch.setattr(D, "run", lambda *_a, **_k: {"exit_code": 0, "stdout": stdout, "stderr": ""})
    code, body = _cli(capsys, "delete-verified-file", _file_request(_P, _R, _DATA))
    assert code == 0 and body["success"] is True and body["data"]["deleted"] is True
    assert body["metrics"]["freed_bytes"] == len(_DATA)


def test_the_cli_answers_a_folder_that_is_not_empty_with_a_failed_response(capsys, monkeypatch):
    monkeypatch.setattr(D, "run", lambda *_a, **_k: {"exit_code": 0, "stdout": "NOT_EMPTY\t1\n", "stderr": ""})
    code, body = _cli(capsys, "delete-empty-folder", {"host": LINUX, "path": "/backup/APPDB",
                                                      "must_be_under": "/backup"})
    assert code != 0 and body["success"] is False and body["data"]["folder"]["entries"] == 1
