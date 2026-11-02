"""``copy-files`` copies the files it is named and removes nothing on either side (0.34.0, 1.240).

Why it matters: this is the transport of the repository copy, and a repository is only worth
anything if it still holds a backup after the database server has lost it. ``copy-backup-dir``
mirrors - it deletes from the target what the source no longer has - which is right for a restore's
staging and would, pointed at a repository, delete the only copy left the day the server is wiped.

So the promises held here: a file arrives under a temporary name and takes its real name only when
every byte is there; a file the target already holds under another name is never touched; a file
that cannot be copied is its own answer and the rest are still copied; a short read is a failure,
not a smaller file; and a request that would write a relative or empty target is refused before
anything moves.
"""

from __future__ import annotations

import io
import os
from types import SimpleNamespace

import pytest

from dbabrain.common import filecopy


class _Sftp:
    def __init__(self, files: dict[str, bytes], *, short: set[str] | None = None) -> None:
        self.files = files
        self.short = short or set()
        self.renames: list[tuple[str, str]] = []

    def stat(self, path):
        if path in self.files:
            return SimpleNamespace(st_size=len(self.files[path]))
        if any(name.startswith(path.rstrip("/") + "/") for name in self.files) or path == "/":
            return SimpleNamespace(st_size=0)
        raise IOError(path)

    def mkdir(self, path):
        return None

    def open(self, path, mode="rb"):
        if "w" in mode:
            files = self.files

            class _W(io.BytesIO):
                def close(inner):
                    files[path] = inner.getvalue()
                    super().close()
            return _W()
        data = self.files[path]
        return io.BytesIO(data[: len(data) // 2] if path in self.short else data)

    def posix_rename(self, old, new):
        self.renames.append((old, new))
        self.files[new] = self.files.pop(old)

    def remove(self, path):
        self.files.pop(path, None)


class _Session:
    def __init__(self, sftp):
        self._sftp = sftp
        self.closed = False

    def sftp(self):
        return self._sftp

    def close(self):
        self.closed = True


@pytest.fixture
def hosts(monkeypatch):
    source = _Sftp({"/b/APPDB/FULL/full.bak": b"full backup bytes", "/b/APPDB/LOG/log.trn": b"log bytes"})
    target = _Sftp({"/repo/S1/APPDB/FULL/old.bak": b"an older full the source deleted long ago"})
    sessions = {"src": _Session(source), "repo": _Session(target)}
    monkeypatch.setattr(filecopy, "_ssh", lambda login, role: sessions[login["host"]])
    return SimpleNamespace(source=source, target=target, sessions=sessions)


def _request(*pairs, **extra):
    return {"source": {"host": "src"}, "target": {"host": "repo"},
            "files": [{"source": s, "target": t} for s, t in pairs], **extra}


def test_each_file_arrives_under_a_temporary_name_and_takes_its_own_only_when_whole(hosts):
    answer = filecopy.copy_files(_request(("/b/APPDB/FULL/full.bak", "/repo/S1/APPDB/FULL/full.bak")))

    assert answer["copied"] == 1 and answer["failed"] == 0 and answer["source_kind"] == "sftp"
    assert answer["bytes_copied"] == len(b"full backup bytes")
    assert hosts.target.renames == [("/repo/S1/APPDB/FULL/full.bak.dbabrain_partial", "/repo/S1/APPDB/FULL/full.bak")]
    assert hosts.target.files["/repo/S1/APPDB/FULL/full.bak"] == b"full backup bytes"
    assert all(s.closed for s in hosts.sessions.values())


def test_nothing_on_the_target_is_removed_and_nothing_on_the_source_either(hosts):
    filecopy.copy_files(_request(("/b/APPDB/LOG/log.trn", "/repo/S1/APPDB/LOG/log.trn")))

    assert hosts.target.files["/repo/S1/APPDB/FULL/old.bak"] == b"an older full the source deleted long ago"
    assert set(hosts.source.files) == {"/b/APPDB/FULL/full.bak", "/b/APPDB/LOG/log.trn"}


def test_a_file_that_cannot_be_read_is_its_own_answer_and_the_rest_are_still_copied(hosts):
    answer = filecopy.copy_files(_request(("/b/gone.bak", "/repo/S1/gone.bak"),
                                          ("/b/APPDB/LOG/log.trn", "/repo/S1/APPDB/LOG/log.trn")))

    first, second = answer["files"]
    assert first["ok"] is False and first["error"] and second["ok"] is True
    assert answer["copied"] == 1 and answer["failed"] == 1
    assert "/repo/S1/gone.bak" not in hosts.target.files


def test_a_short_read_is_a_failure_and_leaves_no_partial_file_behind(hosts):
    hosts.source.short.add("/b/APPDB/FULL/full.bak")
    answer = filecopy.copy_files(_request(("/b/APPDB/FULL/full.bak", "/repo/S1/APPDB/FULL/full.bak")))

    [line] = answer["files"]
    assert line["ok"] is False and "bytes arrived of" in line["error"]
    assert not any(name.startswith("/repo/S1/APPDB/FULL/full.bak") for name in hosts.target.files)


def test_a_copy_over_an_earlier_attempt_replaces_it_in_one_step(hosts):
    hosts.target.files["/repo/S1/APPDB/LOG/log.trn"] = b"a corrupted earlier attempt"
    filecopy.copy_files(_request(("/b/APPDB/LOG/log.trn", "/repo/S1/APPDB/LOG/log.trn")))
    assert hosts.target.files["/repo/S1/APPDB/LOG/log.trn"] == b"log bytes"


@pytest.mark.parametrize("request_, words", [
    ({"files": []}, "non-empty list"),
    ({"files": [{"source": "/a", "target": "relative/a"}]}, "absolute path"),
    ({"files": [{"source": "/a", "target": "/repo/"}]}, "must name a file"),
    ({"files": [{"source": "/a"}]}, "needs both source and target"),
])
def test_a_request_that_would_write_somewhere_unnamed_is_refused_before_anything_moves(hosts, request_, words):
    with pytest.raises(filecopy.FileCopyError, match=words):
        filecopy.copy_files({"source": {"host": "src"}, "target": {"host": "repo"}, **request_})
    assert hosts.target.renames == []


def test_a_source_is_an_ssh_host_or_a_share_never_both(hosts):
    with pytest.raises(filecopy.FileCopyError, match="not both"):
        filecopy.copy_files(_request(("/a", "/repo/a"), source_share={"host": "h", "share": "s"}))


def test_the_command_stops_starting_files_once_its_time_is_spent(hosts):
    ticks = iter([0, 0, 10_000, 10_000])
    answer = filecopy.copy_files(_request(("/b/APPDB/LOG/log.trn", "/repo/S1/a.trn"),
                                          ("/b/APPDB/FULL/full.bak", "/repo/S1/b.bak"), timeout_seconds=60),
                                 clock=lambda: next(ticks))
    assert [line["ok"] for line in answer["files"]] == [True, False]
    assert "were spent" in answer["files"][1]["error"]


def test_a_stream_whose_reader_failed_half_way_never_takes_the_files_name(hosts, monkeypatch):
    """smbclient's FIFO has no size of its own: a link that dropped half way ends the stream like a
    whole file. Its exit is asked before the rename (the review of 2026-10-31)."""

    class _DiedHalfWay:
        size = None

        def __init__(self, _path):
            self.data = io.BytesIO(b"the first half")

        def read(self, count):
            return self.data.read(count)

        def finish(self):
            return "smbclient exit 1: NT_STATUS_CONNECTION_RESET"

        close = finish

    monkeypatch.setattr(filecopy, "_reader_factory", lambda request, **_k: (_DiedHalfWay, lambda: None, "smbclient"))
    answer = filecopy.copy_files(_request(("HR\\FULL\\hr.bak", "/repo/S1/HR/FULL/hr.bak")))

    [line] = answer["files"]
    assert line["ok"] is False and "NT_STATUS_CONNECTION_RESET" in line["error"]
    assert not any(name.startswith("/repo/S1/HR") for name in hosts.target.files), "no name, no partial"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="a FIFO is a POSIX object; the worker is Linux")
def test_a_share_is_streamed_through_a_fifo_and_smbclients_failure_is_named(tmp_path, monkeypatch):
    script = tmp_path / "smbclient"
    script.write_text("#!/bin/sh\n"
                      'cmd="$5"; dest=$(printf "%s" "$cmd" | sed \'s/.*" "\\(.*\\)"$/\\1/\')\n'
                      'case "$cmd" in *missing*) echo "NT_STATUS_OBJECT_NAME_NOT_FOUND" >&2; exit 1;; esac\n'
                      'printf "share bytes" > "$dest"\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    share = {"host": "192.0.2.45", "share": "SQLBK", "username": "u", "password": "p"}

    reader = filecopy._SmbclientReader(share, "HR\\FULL\\hr.bak")
    assert reader.read(1 << 20) == b"share bytes" and reader.read(10) == b""
    assert reader.close() == ""

    with pytest.raises(OSError, match="NT_STATUS_OBJECT_NAME_NOT_FOUND"):
        filecopy._SmbclientReader(share, "HR\\FULL\\missing.bak")
