"""A file transfer's remote end is a path stated whole for the host it is on.

On 2026-10-09 a `send-file` request carried `/opt/dbabrain/runtime/...` through Git Bash, which
rewrote it into `C:/Program Files/Git/opt/dbabrain/...`. On the Linux worker that is a relative
path; the SSH server resolved it against the login's home, created the tree there, and the command
answered `COPIED`. The file was found by looking for it (0.28.0 sheet §3.3 T2).

A relative remote path always names *some* place, so it never fails on its own: the transfer has
to refuse it, before a session is opened, and say why when the path carries the shell's mark.
"""

from __future__ import annotations

import pytest

from dbabrain.common import file_transfer, host_ops


class _Host:
    def __init__(self, windows: bool = False) -> None:
        self.is_windows = windows

    def describe(self) -> str:
        return "ACME-192-0-2-10 (linux, ssh)"


@pytest.fixture
def opened(monkeypatch):
    """The sessions a transfer opened - none, when it refuses."""
    sessions: list[object] = []
    monkeypatch.setattr(host_ops, "resolve_stated_host", lambda request, what="": _Host(
        windows=str(request.get("platform") or "") == "windows"))
    monkeypatch.setattr(host_ops, "open_host_session", lambda target, secrets=None: sessions.append(target))
    return sessions


def _send(tmp_path, remote_path: str, **extra) -> dict:
    local = tmp_path / "set.tar"
    local.write_bytes(b"x")
    return file_transfer.send_file({"remote_path": remote_path, "local_path": str(local), **extra})


@pytest.mark.parametrize("remote_path", [
    "C:/Program Files/Git/opt/dbabrain/runtime/set.tar",
    "opt/dbabrain/runtime/set.tar",
    "runtime/set.tar",
])
def test_a_relative_path_on_a_linux_host_is_refused_before_anything_opens(tmp_path, opened, remote_path):
    with pytest.raises(file_transfer.FileTransferError, match="absolute path"):
        _send(tmp_path, remote_path)
    assert opened == []


def test_the_refusal_says_a_windows_shell_rewrote_the_path(tmp_path, opened):
    with pytest.raises(file_transfer.FileTransferError, match="MSYS_NO_PATHCONV"):
        _send(tmp_path, "C:/Program Files/Git/opt/dbabrain/runtime/set.tar")


def test_a_fetch_is_held_to_the_same_rule(tmp_path, opened):
    with pytest.raises(file_transfer.FileTransferError, match="absolute path"):
        file_transfer.fetch_file({"remote_path": "backup/a.bak", "local_path": str(tmp_path / "a.bak")})
    assert opened == []


def test_a_relay_checks_both_ends(opened):
    with pytest.raises(file_transfer.FileTransferError, match="destination.path"):
        file_transfer.relay_file({"source": {"path": "/tmp/a.tar"}, "destination": {"path": "tmp/a.tar"}})


@pytest.mark.parametrize("path, windows", [
    ("/opt/dbabrain/runtime/set.tar", False),
    ("D:/SQLBK/set.bak", True),
    ("D:\\SQLBK\\set.bak", True),
    ("\\\\server\\share\\set.bak", True),
])
def test_an_absolute_path_is_let_through(path, windows) -> None:
    file_transfer._absolute_on(_Host(windows=windows), path, "remote_path")
