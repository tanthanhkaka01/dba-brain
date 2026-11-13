"""send-file and fetch-file work to a host reached by WinRM (2026-11-10).

Why it matters: sending an image build to a Windows build host over its WinRM access died with
``'WinrmSession' object has no attribute 'sftp'`` - every file path in ``file_transfer`` was SFTP,
and WinRM has none. The image went over the host's OpenSSH instead. Both commands now copy over the
remoting channel with the same contract: staged under a partial name, moved onto its name, and
proven by bytes and SHA-256 read on the host. Any other SFTP use against a WinRM session says what
it needs instead of an AttributeError.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.common import file_transfer as FT
from dbabrain.common.remote_exec import RemoteExecError, WinrmSession


class _FakeWinrm(WinrmSession):
    """A host's files as a dict; every call the transfer makes, and nothing over the network."""

    def __init__(self, files=None, corrupt=False):  # noqa: D401 - no access to build
        self.files: dict[str, bytes] = dict(files or {})
        self.corrupt = corrupt
        self.access = SimpleNamespace(host="win-host", method="winrm")
        self.made: list[str] = []

    def file_facts(self, path):
        data = self.files.get(path)
        return None if data is None else (len(data), hashlib.sha256(data).hexdigest())

    def put(self, local_path, remote_path):
        data = Path(local_path).read_bytes()
        self.files[remote_path] = data[:-1] if self.corrupt else data

    def get(self, remote_path, local_path):
        Path(local_path).write_bytes(self.files[remote_path])

    def mkdirs(self, path):
        self.made.append(path)

    def move_onto(self, staged, destination):
        self.files[destination] = self.files.pop(staged)

    def remove(self, path):
        self.files.pop(path, None)

    def close(self):
        pass


_TARGET = SimpleNamespace(host="win-host", server_id="WIN", is_windows=True, access={"username": "u"},
                          describe=lambda: "WIN (win-host)")


@pytest.fixture
def host(monkeypatch):
    session = _FakeWinrm()
    monkeypatch.setattr(FT, "_open", lambda request, **_k: (_TARGET, session))
    return session


def test_a_file_sent_over_winrm_lands_whole_under_its_name(tmp_path, host):
    local = tmp_path / "image.tar"
    local.write_bytes(b"layer" * 100)
    answer = FT.send_file({"local_path": str(local), "remote_path": r"C:\build\image.tar"})
    assert answer["status"] == FT.STATUS_COPIED and answer["replace_mode"] == "move-item"
    assert host.files == {r"C:\build\image.tar": b"layer" * 100}, "no partial file left behind"
    assert host.made == [r"C:\build"]


def test_a_copy_that_arrives_different_is_removed_and_refused(tmp_path, host):
    host.corrupt = True
    local = tmp_path / "image.tar"
    local.write_bytes(b"layer" * 100)
    with pytest.raises(FT.FileTransferError, match="partial file was removed"):
        FT.send_file({"local_path": str(local), "remote_path": r"C:\build\image.tar"})
    assert host.files == {}


def test_the_same_file_already_there_is_skipped_and_another_is_not_overwritten_unasked(tmp_path, host):
    local = tmp_path / "a.txt"
    local.write_bytes(b"same")
    host.files[r"C:\x\a.txt"] = b"same"
    assert FT.send_file({"local_path": str(local), "remote_path": r"C:\x\a.txt"})["status"] == FT.STATUS_SKIPPED_EXISTS
    host.files[r"C:\x\a.txt"] = b"diff"
    with pytest.raises(FT.FileTransferError, match="overwrite"):
        FT.send_file({"local_path": str(local), "remote_path": r"C:\x\a.txt"})


def test_a_file_fetched_over_winrm_arrives_whole(tmp_path, host):
    host.files[r"C:\out\image.tar"] = b"built" * 50
    local = tmp_path / "got" / "image.tar"
    answer = FT.fetch_file({"local_path": str(local), "remote_path": r"C:\out\image.tar"})
    assert answer["status"] == FT.STATUS_COPIED and local.read_bytes() == b"built" * 50
    assert not list(local.parent.glob("*" + FT.PARTIAL_SUFFIX))


def test_a_missing_file_is_said_by_name(tmp_path, host):
    with pytest.raises(FT.FileTransferError, match="does not exist"):
        FT.fetch_file({"local_path": str(tmp_path / "x"), "remote_path": r"C:\none.bin"})


def test_any_other_sftp_use_on_a_winrm_session_says_what_it_needs():
    session = _FakeWinrm()
    with pytest.raises(RemoteExecError, match="no SFTP"):
        WinrmSession.sftp(session)
