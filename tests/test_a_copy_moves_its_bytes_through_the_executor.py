"""The file copy streams over a session's connection - and the executor is who opens the way (R44).

``copy-files`` reads a file by ``cat`` on a channel with a large window, writes it through another,
gives a reader an SFTP client of its own and raises the connection's re-key limit: 43 MB/s into the
repository against SFTP's 25, and no "Key-exchange timed out" in the middle of a 6 GB file. Each of
those was a paramiko call made by the copy itself - seven in ``common/filecopy.py`` and one in
``common/repoencrypt.py``, found 2026-11-11 by the guard that holds *one executor runs everything on
a host* (0.38.0, 1.338). An executor beside the executor is how five of them grew before 0.25.0:
each with its own idea of a timeout.

So the calls live in ``common/remote_exec.py``, and the copy asks for them with the session's own
connection. What is held here is that they do what the copy relied on - the window, the stall limit
set before the command starts, the fallback for a connection with no transport - because the guard
only sees where a call is written, not what it does.
"""

from __future__ import annotations

import io

import pytest

from dbabrain.common import filecopy, remote_exec


class _Channel:
    def __init__(self, log):
        self.log = log

    def settimeout(self, seconds):
        self.log.append(("settimeout", seconds))

    def exec_command(self, command):
        self.log.append(("exec_command", command))


class _Transport:
    def __init__(self, log):
        self.log = log
        self.packetizer = type("Packetizer", (), {"REKEY_BYTES": 1 << 29, "REKEY_PACKETS": 1 << 29})()

    def open_session(self, window_size=None):
        self.log.append(("open_session", window_size))
        return _Channel(self.log)


class _Connection:
    """A connection with a transport, as paramiko's ``SSHClient`` has one."""

    def __init__(self):
        self.log: list[tuple] = []
        self.transport = _Transport(self.log)

    def get_transport(self):
        return self.transport

    def exec_command(self, command, **kwargs):
        self.log.append(("stream", command, kwargs))
        return None, io.BytesIO(b"out"), io.BytesIO(b"")

    def open_sftp(self):
        self.log.append(("open_sftp",))
        return "the reader's own sftp"


class _NoTransport:
    """A stand-in that can run a command and nothing else."""

    def exec_command(self, command, **kwargs):
        return None, io.BytesIO(b""), io.BytesIO(b"")


def test_a_channel_gets_its_window_and_its_stall_limit_before_the_command_starts():
    """The window is the channel's from its opening, and a ``cat`` that stops moving must already
    be on the clock when its first byte is due - a limit set after the command is a hang that a
    slow start can fall into."""
    connection = _Connection()

    channel = remote_exec.channel_on(connection, "cat -- /b/full.bak", window_bytes=64 << 20, timeout_seconds=300)

    assert isinstance(channel, _Channel)
    assert connection.log == [("open_session", 64 << 20), ("settimeout", 300), ("exec_command", "cat -- /b/full.bak")]


def test_a_writing_channel_takes_no_stall_limit_of_its_own():
    connection = _Connection()

    remote_exec.channel_on(connection, "cat > /repo/f.dbabrain_partial", window_bytes=64 << 20)

    assert [step[0] for step in connection.log] == ["open_session", "exec_command"]


def test_a_connection_with_no_transport_has_no_channel_and_the_caller_streams_instead():
    assert remote_exec.channel_on(_NoTransport(), "cat -- /b/f", window_bytes=1 << 20) is None


def test_a_stream_is_started_with_its_timeout_and_its_buffer():
    connection = _Connection()

    _stdin, out, _err = remote_exec.stream_on(connection, "stat -c %s -- /b/f", timeout_seconds=60, bufsize=1 << 20)

    assert out.read() == b"out"
    assert connection.log == [("stream", "stat -c %s -- /b/f", {"bufsize": 1 << 20, "timeout": 60})]


def test_a_readers_own_sftp_is_opened_on_the_sessions_connection():
    connection = _Connection()

    assert remote_exec.sftp_on(connection) == "the reader's own sftp"
    assert connection.log == [("open_sftp",)]


def test_the_copy_raises_the_connections_re_key_limit_and_leaves_a_stand_in_alone():
    """paramiko re-keys every 512 MB of its own accord, and with a large window in flight each one
    stalls the stream. The copy raises that to 16 GB; the server's own limit still applies."""
    connection = _Connection()

    filecopy._tune_transport(connection)

    packetizer = connection.transport.packetizer
    assert (packetizer.REKEY_BYTES, packetizer.REKEY_PACKETS) == (16 << 30, 1 << 31)
    filecopy._tune_transport(_NoTransport())  # nothing to raise, and no error


def test_a_target_with_no_channel_to_write_through_says_so_by_the_file():
    """It was an ``AttributeError`` on ``None`` - a sentence about Python where one about the file
    was owed."""
    with pytest.raises(OSError, match="/repo/f.dbabrain_partial: the connection to the target has no channel"):
        filecopy._CatWriter(_NoTransport(), "/repo/f.dbabrain_partial")
