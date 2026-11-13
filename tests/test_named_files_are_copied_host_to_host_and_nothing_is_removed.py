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

**Every line carries its file's facts** (0.37.0, the operator 2026-11-10: *the copy too - each file's
full facts, and only when they are right is it copied*): ``file_name``, ``size_bytes`` and ``sha256``
beside ``source`` and ``target``. A line missing one, or with one that does not fit, refuses the
whole request before a byte moves; a source whose size or streamed hash is not the stated one never
takes the target's name and leaves no partial behind.

**Overwriting is stated on every request** (``overwrite``, required; false is the value to state): with
false a name that already holds a file is left exactly as it is and the source is not read; with true
it is replaced in one step.
"""

from __future__ import annotations

import hashlib
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

    def rename(self, old, new):
        # SFTP's plain rename: an existing name is refused, as OpenSSH does.
        if new in self.files:
            raise IOError(f"Failure: {new} exists")
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


#: What the fixture's source holds - the facts a line states are these files' own.
SOURCE = {"/b/APPDB/FULL/full.bak": b"full backup bytes", "/b/APPDB/LOG/log.trn": b"log bytes"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _line(source: str, target: str, data: bytes | None = None, **override) -> dict:
    data = SOURCE.get(source, b"bytes of a file the source does not have") if data is None else data
    name = target.rsplit("/", 1)[-1]
    return {"source": source, "target": target, "file_name": name, "size_bytes": len(data),
            "sha256": _sha(data), **override}


def _request(*pairs, **extra):
    return {"source": {"host": "src"}, "target": {"host": "repo"}, "overwrite": False, "transfer": "sftp",
            "files": [_line(s, t) for s, t in pairs], **extra}


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


def test_with_overwrite_a_copy_over_an_earlier_attempt_replaces_it_in_one_step(hosts):
    hosts.target.files["/repo/S1/APPDB/LOG/log.trn"] = b"a corrupted earlier attempt"
    filecopy.copy_files(_request(("/b/APPDB/LOG/log.trn", "/repo/S1/APPDB/LOG/log.trn"), overwrite=True))
    assert hosts.target.files["/repo/S1/APPDB/LOG/log.trn"] == b"log bytes"


def test_without_overwrite_a_file_already_at_the_name_is_left_as_it_is_and_the_source_not_read(hosts, monkeypatch):
    hosts.target.files["/repo/S1/APPDB/LOG/log.trn"] = b"what the repository already holds"
    opened = []
    real_open = hosts.source.open
    monkeypatch.setattr(hosts.source, "open", lambda path, mode="rb": opened.append(path) or real_open(path, mode))

    answer = filecopy.copy_files(_request(("/b/APPDB/LOG/log.trn", "/repo/S1/APPDB/LOG/log.trn")))

    [line] = answer["files"]
    assert line["ok"] is False and line["exists"] is True and "overwrite is false" in line["error"]
    assert hosts.target.files["/repo/S1/APPDB/LOG/log.trn"] == b"what the repository already holds"
    assert opened == [] and hosts.target.renames == []


def test_without_overwrite_a_file_that_appears_at_the_name_meanwhile_is_not_replaced(hosts, monkeypatch):
    """The name was free when looked at and is taken by the time the copy lands: the plain rename
    refuses it, the partial goes, the file there stays."""
    target = "/repo/S1/APPDB/LOG/log.trn"
    monkeypatch.setattr(filecopy, "_exists", lambda sftp, path: False)
    hosts.target.files[target] = b"written by someone else meanwhile"

    answer = filecopy.copy_files(_request(("/b/APPDB/LOG/log.trn", target)))

    assert answer["files"][0]["ok"] is False
    assert hosts.target.files[target] == b"written by someone else meanwhile"
    assert target + ".dbabrain_partial" not in hosts.target.files


@pytest.mark.parametrize("value", [None, 0, 1, "false", "no"])
def test_overwrite_must_be_stated_as_true_or_false(hosts, value):
    request = _request(("/b/APPDB/LOG/log.trn", "/repo/S1/APPDB/LOG/log.trn"), overwrite=value)
    if value is None:
        del request["overwrite"]
    with pytest.raises(filecopy.FileCopyError, match='"overwrite" is required'):
        filecopy.copy_files(request)
    assert hosts.target.renames == []


@pytest.mark.parametrize("request_, words", [
    ({"files": []}, "non-empty list"),
    ({"files": [_line("/a", "relative/a")]}, "absolute path"),
    ({"files": [_line("/a", "/repo/", file_name="a")]}, "must name a file"),
    ({"files": [{"source": "/a"}]}, "needs target, file_name, size_bytes, sha256"),
])
def test_a_request_that_would_write_somewhere_unnamed_is_refused_before_anything_moves(hosts, request_, words):
    with pytest.raises(filecopy.FileCopyError, match=words):
        filecopy.copy_files({"source": {"host": "src"}, "target": {"host": "repo"}, "overwrite": False, "transfer": "sftp", **request_})
    assert hosts.target.renames == []


_FULL, _TO = "/b/APPDB/FULL/full.bak", "/repo/S1/APPDB/FULL/full.bak"


@pytest.mark.parametrize("missing", ["source", "target", "file_name", "size_bytes", "sha256"])
def test_a_line_without_one_of_its_files_facts_is_refused_before_anything_moves(hosts, missing):
    line = _line(_FULL, _TO)
    del line[missing]
    with pytest.raises(filecopy.FileCopyError, match=f"needs {missing}"):
        filecopy.copy_files({"source": {"host": "src"}, "target": {"host": "repo"}, "overwrite": False, "transfer": "sftp", "files": [line]})
    assert hosts.target.renames == [] and _TO not in hosts.target.files


@pytest.mark.parametrize("override, words", [
    ({"file_name": "other.bak"}, "must be the last part of both"),
    ({"size_bytes": -1}, "whole number of bytes"),
    ({"size_bytes": True}, "whole number of bytes"),
    ({"size_bytes": 17.0}, "whole number of bytes"),
    ({"size_bytes": "17"}, "whole number of bytes"),
    ({"sha256": "abc"}, "64 hexadecimal"),
    ({"sha256": "g" * 64}, "64 hexadecimal"),
    ({"sha_256": "0" * 64}, "unknown field"),
    ({"source": "/b/APPDB/../FULL/full.bak"}, "'..' segments"),
    ({"target": "/repo/S1/../full.bak"}, "'..' segments"),
    ({"file_name": "full\nbak"}, "control character"),
])
def test_a_line_whose_facts_do_not_fit_is_refused_before_anything_moves(hosts, override, words):
    line = {**_line(_FULL, _TO), **override}
    with pytest.raises(filecopy.FileCopyError, match=words):
        filecopy.copy_files({"source": {"host": "src"}, "target": {"host": "repo"}, "overwrite": False, "transfer": "sftp", "files": [line]})
    assert hosts.target.renames == [] and _TO not in hosts.target.files


def test_one_bad_line_refuses_the_whole_list_before_the_first_file_moves(hosts):
    lines = [_line("/b/APPDB/LOG/log.trn", "/repo/S1/APPDB/LOG/log.trn"), _line(_FULL, _TO, sha256="x")]
    with pytest.raises(filecopy.FileCopyError, match="files\\[1\\]"):
        filecopy.copy_files({"source": {"host": "src"}, "target": {"host": "repo"}, "overwrite": False, "transfer": "sftp", "files": lines})
    assert hosts.target.renames == [] and "/repo/S1/APPDB/LOG/log.trn" not in hosts.target.files


def test_a_source_of_another_size_than_stated_is_not_read_at_all(hosts):
    line = _line(_FULL, _TO, size_bytes=len(SOURCE[_FULL]) + 1)
    answer = filecopy.copy_files({"source": {"host": "src"}, "target": {"host": "repo"}, "overwrite": False, "transfer": "sftp", "files": [line]})

    [out] = answer["files"]
    assert out["ok"] is False and "not the" in out["error"] and "stated" in out["error"]
    assert not any(name.startswith(_TO) for name in hosts.target.files), "no name, no partial"


def test_bytes_that_hash_to_another_sha256_never_take_the_files_name(hosts):
    line = _line(_FULL, _TO, sha256=_sha(b"the backup as it was recorded"))
    answer = filecopy.copy_files({"source": {"host": "src"}, "target": {"host": "repo"}, "overwrite": False, "transfer": "sftp", "files": [line]})

    [out] = answer["files"]
    assert out["ok"] is False and "hash to" in out["error"]
    assert hosts.target.renames == []
    assert not any(name.startswith(_TO) for name in hosts.target.files), "no name, no partial"


def test_a_stream_of_unknown_size_is_still_held_to_the_stated_bytes(hosts, monkeypatch):
    """smbclient's FIFO has no size to check up front: the count streamed is held to size_bytes at
    the end, before the rename."""

    class _NoSize:
        size = None

        def __init__(self, _path):
            self.data = io.BytesIO(b"share bytes")

        def read(self, count):
            return self.data.read(count)

        def finish(self):
            return ""

        close = finish

    monkeypatch.setattr(filecopy, "_reader_factory", lambda request, **_k: (_NoSize, lambda: None, "smbclient"))
    line = _line("HR\\FULL\\hr.bak", "/repo/S1/HR/FULL/hr.bak", data=b"share bytes and more")
    answer = filecopy.copy_files({"source": {"host": "src"}, "target": {"host": "repo"}, "overwrite": False, "transfer": "sftp", "files": [line]})

    [out] = answer["files"]
    assert out["ok"] is False and "bytes streamed, not the" in out["error"]
    assert not any(name.startswith("/repo/S1/HR") for name in hosts.target.files)


def test_each_answer_line_carries_the_facts_it_was_held_to_and_the_hash_it_streamed(hosts):
    answer = filecopy.copy_files(_request((_FULL, _TO)))

    [out] = answer["files"]
    assert out["ok"] is True and out["file_name"] == "full.bak"
    assert out["size_bytes"] == len(SOURCE[_FULL]) == out["bytes_copied"]
    assert out["sha256"] == out["sha256_streamed"] == _sha(SOURCE[_FULL])


def test_a_source_is_an_ssh_host_or_a_share_never_both(hosts):
    with pytest.raises(filecopy.FileCopyError, match="not both"):
        filecopy.copy_files(_request(("/a", "/repo/a"), source_share={"host": "h", "share": "s"}))


def test_the_command_stops_starting_files_once_its_time_is_spent(hosts):
    ticks = iter([0, 0, 10_000, 10_000])
    answer = filecopy.copy_files(_request(("/b/APPDB/LOG/log.trn", "/repo/S1/log.trn"),
                                          ("/b/APPDB/FULL/full.bak", "/repo/S1/full.bak"), timeout_seconds=60),
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
    answer = filecopy.copy_files({"source": {"host": "src"}, "target": {"host": "repo"}, "overwrite": False, "transfer": "sftp",
                                  "files": [_line("HR\\FULL\\hr.bak", "/repo/S1/HR/FULL/hr.bak",
                                                  data=b"the first half and the rest")]})

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


# --------------------------------------------------------------------------- #
# transfer (0.37.0): stated in every request - cat or sftp - the same rules on both
# --------------------------------------------------------------------------- #
def test_a_request_that_does_not_state_its_transfer_is_refused(hosts):
    request = _request(("/b/APPDB/FULL/full.bak", "/repo/S1/APPDB/FULL/full.bak"))
    del request["transfer"]
    with pytest.raises(filecopy.FileCopyError, match='"transfer" is required'):
        filecopy.copy_files(request)
    request["transfer"] = "scp"
    with pytest.raises(filecopy.FileCopyError, match='"transfer" is required'):
        filecopy.copy_files(request)


def test_the_two_transfers_are_one_list_in_lib_and_in_common():
    from dbabrain.lib import offload as offload_rules
    assert offload_rules.TRANSFERS == filecopy.TRANSFERS == ("cat", "sftp")


class _CatChannel:
    """A cat channel: `cat -- <path>` answers the source's bytes, `cat > <partial>` writes the target."""

    def __init__(self, world):
        self.world, self.command, self.buffer, self.status = world, "", b"", 0

    def settimeout(self, _s):
        pass

    def exec_command(self, command):
        self.command = command
        if command.startswith("cat -- "):
            path = command[len("cat -- "):].strip("'")
            self.data = self.world.source.files.get(path, b"")
            self.status = 0 if path in self.world.source.files else 1

    def makefile(self, _mode, _size):
        outer = self

        class _Out(io.BytesIO):
            channel = outer
        return _Out(self.data)

    def makefile_stderr(self, _mode):
        return io.BytesIO(b"")

    def recv_exit_status(self):
        return self.status

    def sendall(self, data):
        self.buffer += data

    def shutdown_write(self):
        partial = self.command[len("cat > "):].strip("'")
        self.world.target.files[partial] = self.buffer

    def close(self):
        pass


class _CatClient:
    def __init__(self, world):
        self.world = world

    def get_transport(self):
        world = self.world

        class _T:
            def open_session(self, window_size=None):
                return _CatChannel(world)
        return _T()

    def exec_command(self, command, **_k):
        path = command.split(" -- ", 1)[1].strip("'")
        data = self.world.source.files.get(path)
        text = str(len(data)).encode() if data is not None else b""

        class _Out(io.BytesIO):
            channel = SimpleNamespace(recv_exit_status=lambda: 0 if data is not None else 1)
        return None, _Out(text), io.BytesIO(b"No such file")


@pytest.fixture
def cat_hosts(hosts, monkeypatch):
    for session in hosts.sessions.values():
        session.client = _CatClient(hosts)
    # The child process that reads a cat source, run in this process against the fake source.
    monkeypatch.setattr(filecopy, "ProcessReader", lambda login, container, path, stall=300:
                        filecopy._ExecReader(_CatClient(hosts), container, path, stall=stall))
    return hosts


def test_the_cat_transfer_copies_through_a_cat_channel_held_to_the_same_facts(cat_hosts):
    answer = filecopy.copy_files(_request(("/b/APPDB/FULL/full.bak", "/repo/S1/APPDB/FULL/full.bak"), transfer="cat"))

    assert answer["copied"] == 1 and answer["transfer"] == "cat" and answer["source_kind"] == "cat"
    assert cat_hosts.target.files["/repo/S1/APPDB/FULL/full.bak"] == b"full backup bytes"
    assert not any(p.endswith(".dbabrain_partial") for p in cat_hosts.target.files)


def test_the_cat_transfer_never_places_bytes_that_hash_otherwise(cat_hosts):
    line = _line("/b/APPDB/FULL/full.bak", "/repo/S1/APPDB/FULL/full.bak", sha256="0" * 64)
    answer = filecopy.copy_files({"source": {"host": "src"}, "target": {"host": "repo"}, "overwrite": False,
                                  "transfer": "cat", "files": [line]})
    assert answer["copied"] == 0 and "hash to" in answer["files"][0]["error"]
    assert "/repo/S1/APPDB/FULL/full.bak" not in cat_hosts.target.files


def test_the_read_ahead_hands_over_the_bytes_in_order_and_never_more_than_asked():
    class _Slow:
        size = 10
        chunks = [b"0123", b"4567", b"89", b""]

        def read(self, _n):
            return self.chunks.pop(0)

        def finish(self):
            return ""

        def close(self):
            return ""

    ahead = filecopy._ReadAhead(_Slow(), stall=5)
    got = b""
    while True:
        part = ahead.read(3)
        assert len(part) <= 3
        if not part:
            break
        got += part
    assert got == b"0123456789" and ahead.finish() == ""


def test_a_read_ahead_that_fails_hands_the_error_to_the_writer():
    class _Broken:
        size = None

        def read(self, _n):
            raise OSError("link reset")

        def finish(self):
            return ""

    with pytest.raises(OSError, match="link reset"):
        filecopy._ReadAhead(_Broken(), stall=5).read(10)


# --------------------------------------------------------------------------- #
# The reading child process (0.37.0): its own core; size first, then bytes; a failure is never a file
# --------------------------------------------------------------------------- #
class _Conn:
    def __init__(self):
        self.sent = []

    def send(self, item):
        self.sent.append(item)

    def send_bytes(self, data):
        self.sent.append(bytes(data))

    def close(self):
        self.sent.append("closed")


def test_the_child_sends_the_size_the_bytes_their_end_and_how_the_read_ended(monkeypatch):
    from dbabrain.common import filecopy_source

    class _Reader:
        size = 5
        chunks = [b"hel", b"lo", b""]

        def read(self, _n):
            return self.chunks.pop(0)

        def finish(self):
            return ""

        def close(self):
            return ""

    monkeypatch.setattr(filecopy, "_ssh", lambda login, role: SimpleNamespace(client=None, close=lambda: None))
    monkeypatch.setattr(filecopy, "_ExecReader", lambda client, container, path, stall=300: _Reader())
    conn = _Conn()
    filecopy_source.stream({"source": {"host": "h"}, "container": "", "path": "/b/f", "stall": 30}, conn)
    assert conn.sent == [("SIZE", 5), b"hel", b"lo", b"", ("END", ""), "closed"]


def test_a_child_that_cannot_reach_the_source_answers_an_error_and_no_size(monkeypatch):
    from dbabrain.common import filecopy_source

    def refused(login, role):
        raise filecopy.FileCopyError("source h: login refused")
    monkeypatch.setattr(filecopy, "_ssh", refused)
    conn = _Conn()
    filecopy_source.stream({"source": {"host": "h"}, "path": "/b/f"}, conn)
    assert conn.sent[0][0] == "ERROR" and "login refused" in conn.sent[0][1]


def _reader_over(messages):
    import multiprocessing

    parent, child = multiprocessing.Pipe(duplex=False)
    for item in messages:
        (child.send_bytes if isinstance(item, bytes) else child.send)(item)
    reader = filecopy.ProcessReader.__new__(filecopy.ProcessReader)
    reader.stall, reader.label, reader.conn = 5, "/b/f", parent
    reader.process = SimpleNamespace(join=lambda timeout=None: None, is_alive=lambda: False, kill=lambda: None)
    reader.pending, reader.ended, reader.failure = b"", False, ""
    return reader, child


def test_the_parent_reads_the_bytes_in_order_never_more_than_asked():
    reader, _child = _reader_over([b"0123", b"4567", b"89", b"", ("END", "")])
    data = b""
    while True:
        part = reader.read(3)
        assert len(part) <= 3
        if not part:
            break
        data += part
    assert data == b"0123456789" and reader.finish() == ""


def test_a_read_that_ended_badly_fails_the_file_even_after_its_bytes():
    reader, _child = _reader_over([b"half", b"", ("END", "cat exited 1: read error")])
    while reader.read(1024):
        pass
    assert "cat exited 1" in reader.finish()


def test_a_child_that_vanishes_mid_file_fails_the_file():
    reader, child = _reader_over([b"half"])
    child.close()
    while reader.read(1024):
        pass
    assert "ended before the file did" in reader.finish()


def test_a_real_child_process_that_cannot_reach_its_source_is_refused_by_name():
    """A real spawn: the child runs, cannot connect, says so - and the parent raises, no file."""
    with pytest.raises(filecopy.FileCopyError, match="cannot be read"):
        filecopy.ProcessReader({"host": "127.0.0.1", "port": 1, "username": "u", "password": "p"}, "", "/b/f",
                               stall=60)
