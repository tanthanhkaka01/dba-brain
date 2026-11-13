"""Copy named files from the host that holds them to another host - never deleting anything there.

0.34.0, 1.240 - the transport of the repository copy: *each backup run's files go to a repository
(a shared folder or another VM), proven by checksum + bytes there, then deleted on the database
server* (the operator, 2026-10-30). ``copy-backup-dir`` already moves backups between two hosts, and
it is the wrong tool for this: it MIRRORS - whatever the source no longer has is removed from the
target - which is what a restore's staging wants and the opposite of what a repository is for. A
repository keeps the copy after the source deleted it; that is its whole point.

So this command copies exactly the files it is named, one at a time, and removes nothing:

* **The source** is an SSH host (Linux, or the host of a container - the path is the host's own,
  ``backup_files.host_path``), or a Windows share read by ``smbclient`` (a Windows database server
  reached over WinRM has no SFTP). ``smbclient`` writes into a FIFO this process reads, so a 500 GB
  FULL crosses the worker as a stream and never lands on the worker's disk. On a Windows node the
  share is its UNC path, read the same way.
* **The target** is an SSH host. Each file is written as ``<name>.dbabrain_partial`` and renamed over
  the real name only once every byte has arrived, so a copy cut off half way never stands at the
  name a restore or a check reads.
* **One file failing is that file's answer**, not the command's: the caller's checksum gate counts an
  attempt spent for that file and keeps the rest. A source or target that cannot be reached at all is
  the command's error.

**Every file carries its own facts, and is copied only when they hold** (0.37.0 - the operator,
2026-11-10, translated: *the copy too: the information must be complete - a list of files is fine, but
like the delete, each file's full facts, and only when they are right is it copied*). Each line of
``files`` names the file (``file_name``, the last part of both ``source`` and ``target``), its
``size_bytes`` and its ``sha256``. Before a byte is read, the source's size (where the reader knows
it) is held to ``size_bytes``; the bytes are hashed as they stream past - no second read - and the
partial is put in place under the real name only when the count AND the hash equal what the line
says. Anything else leaves no file at the target name and is that file's failure.

**Overwriting is stated, never assumed** (0.37.0 - the operator, 2026-11-10: *an option to
overwrite or not; the option is required; 0 - do not overwrite - is the default value*). ``overwrite``
is required on every request. ``false``: a target name that already holds a file is left exactly as it
is - the source is not even read - and the line fails saying so; the partial is put in place by a
rename that refuses an existing name (SFTP ``rename``), so a file that appears at the name meanwhile is
not replaced either. ``true``: the real name is replaced in one step (``posix-rename``).

**Encrypted on the way** (0.38.0, 1.330 - the operator, 2026-11-11: *ok, this way is fine*). With an
``encrypt`` block (``recipients``, ``key_id``, ``key_expires_at``) each file lands as ``<target>.age`` and
the plain bytes never reach the target: they are hashed as they stream (held to the line's facts as
always), encrypted by pyrage in the same stream for the public key only, and the ciphertext hashed as it
is written. A 52.8 GB FULL was read six to seven times before - copied, re-hashed on the repository,
re-hashed and encrypted there, the ``.age`` hashed, decrypted back - and took three hours from the
backup's end to its release. The line answers the ``.age``'s path, bytes and sha256; the proof that it
decrypts back to the backup is still the caller's (``verify-encrypted-file``). A key at or past its end
never encrypts (``lib.repository_keys.may_encrypt``), on this command's own clock as on
``encrypt-file``'s. An age stream cannot be resumed in the middle, so a copy cut half way leaves only
its partial (removed) and starts again from its first byte.

That is the copier refusing to land a copy that is not the file it was asked for - not the copy's
proof. The proof is still the caller's (``file-checksum`` on the repository, held to what the backup
recorded): a copier that certified its own copy would be the one witness the copy gate exists not to
trust.

Every value is in the request (R09): both logins, the share and its password. No password reaches a
command line - ``smbclient`` reads it from :func:`dbabrain.common.smb._auth_file`.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from dbabrain.lib import errors
from dbabrain.lib import repository_keys as K
from dbabrain.lib.integrity import SHA256_HEX as _SHA256
from dbabrain.lib.restore.copy_mode import TRANSFERS
from dbabrain.lib.verified_cleanup import last_part as _last_part

#: The suffix a file carries on the target while it is being written - relay-file's, one name for
#: "not whole yet" wherever this tool writes a file.
from dbabrain.common.ssh_relay import PARTIAL_SUFFIX  # noqa: E402
#: Bytes per read and write. SFTP's own packets are 32 KB; a megabyte keeps the loop cheap.
CHUNK = 1 << 20
#: How long one read or write may stall before the file is given up, when the request does not say
#: (``stall_seconds``) - not the whole copy: a FULL of half a terabyte takes hours, and only a link
#: that stopped moving is a failure. The repository copy states its own (``offload_stall_seconds``).
STALL_SECONDS = 300
#: The whole command, unless the request says otherwise.
TIMEOUT_SECONDS = 6 * 3600
#: Each SSH channel's window (0.37.0). paramiko's 2 MB default keeps a long link idle while it waits
#: for the window to reopen; measured to the repository, a large window and a plain `cat` channel
#: carried 43 MB/s where SFTP writes carried 25 MB/s into the same NFS folder.
WINDOW_BYTES = 64 << 20
#: Chunks read ahead of the writer (0.37.0): the read and the write overlap instead of taking turns.
READ_AHEAD_CHUNKS = 16
# How the bytes travel over SSH is stated in every request (``transfer``), never assumed: the two
# names are lib.restore.copy_mode.TRANSFERS, imported above - the one list.


def transfer_of(request: dict[str, Any], command: str) -> str:
    value = request.get("transfer")
    if value not in TRANSFERS:
        raise FileCopyError(f'"transfer" is required: "cat" (a cat channel, the faster) or "sftp"; got {value!r} '
                            f"({command}).")
    return value


class FileCopyError(errors.RequestError):
    """The request cannot be run as written, or a host it names cannot be reached."""


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
#: The fields one line of ``files`` takes - all required. Any other is refused: a misspelt
#: ``sha_256`` must not be a check silently not made.
FILE_FIELDS = ("source", "target", "file_name", "size_bytes", "sha256")


def _files(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Every line checked before the first byte moves - a bad line refuses the whole request while
    nothing has been copied yet."""
    files = request.get("files")
    if not isinstance(files, list) or not files:
        raise FileCopyError('"files" must be a non-empty list of '
                            '{"source", "target", "file_name", "size_bytes", "sha256"}.')
    out = []
    for index, item in enumerate(files):
        where = f"files[{index}]"
        if not isinstance(item, dict):
            raise FileCopyError(f"{where} must be an object with {', '.join(FILE_FIELDS)}.")
        unknown = sorted(set(item) - set(FILE_FIELDS))
        if unknown:
            raise FileCopyError(f"{where}: unknown field(s) {', '.join(unknown)}; a line takes {', '.join(FILE_FIELDS)}.")
        missing = [field for field in FILE_FIELDS if item.get(field) in (None, "")]
        if missing:
            raise FileCopyError(f"{where} needs {', '.join(missing)} - every file is copied only with its "
                                "name, path, bytes and sha256 stated.")
        source, target, name = (str(item[k]).strip() for k in ("source", "target", "file_name"))
        if any(ord(char) < 32 for char in source + target + name):
            raise FileCopyError(f"{where}: a path or name must not hold a control character.")
        if not target.startswith("/"):
            raise FileCopyError(f"{where}.target must be an absolute path on the target host; got {target!r}.")
        if target.endswith("/") or posixpath.basename(target) in ("", ".", ".."):
            raise FileCopyError(f"{where}.target must name a file; got {target!r}.")
        if any(part in (".", "..") for part in re.split(r"[\\/]+", source + "/" + target)):
            raise FileCopyError(f"{where}: source and target must not contain '.' or '..' segments.")
        if name != _last_part(source) or name != _last_part(target):
            raise FileCopyError(f"{where}.file_name {name!r} must be the last part of both source {source!r} "
                                f"and target {target!r}.")
        size = item["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise FileCopyError(f"{where}.size_bytes must be a whole number of bytes, 0 or more; got {size!r}.")
        sha = str(item["sha256"]).strip().lower()
        if not _SHA256.match(sha):
            raise FileCopyError(f"{where}.sha256 must be 64 hexadecimal characters; got {item['sha256']!r}.")
        out.append({"source": source, "target": target, "file_name": name, "size_bytes": size, "sha256": sha})
    return out


def _ssh(login: Any, *, role: str):
    """A connected SSH session - :mod:`remote_exec`, the one executor."""
    from dbabrain.common import remote_exec

    if not isinstance(login, dict) or not str(login.get("host") or "").strip():
        raise FileCopyError(f"{role} must be an object with host, username and a password or key_file.")
    key_file = str(login.get("key_file") or "").strip()
    access: dict[str, Any] = {
        "method": "ssh", "host": str(login["host"]).strip(), "port": int(login.get("port") or 22),
        "username": str(login.get("username") or "").strip(), "password": str(login.get("password") or ""),
        "auth_type": "key" if key_file else "password", "platform": "linux",
        "timeout_seconds": int(login.get("open_timeout_seconds") or 30)}
    if key_file:
        access["key_file"] = key_file
    session = remote_exec.open_session(access)
    try:
        session.client  # connect now: a refused login is this command's error, not a file's
    except Exception as exc:  # noqa: BLE001 - named by role, the caller decides
        raise FileCopyError(f"{role} {access['host']}: {type(exc).__name__}: {exc}") from exc
    _tune_transport(session.client)
    return session


def _tune_transport(client: Any) -> None:
    """paramiko re-keys a connection every 512 MB of its own accord, and with a large window in
    flight each re-key stalls the stream (measured on the store's 6 GB, 2026-11-10: "Key-exchange
    timed out" mid-file). Its own threshold is raised to OpenSSH's order (16 GB); the server still
    re-keys by its own RekeyLimit, so the keys are never used without limit."""
    from dbabrain.common import remote_exec

    remote_exec.raise_rekey_limits(client, rekey_bytes=16 << 30, rekey_packets=1 << 31)


# --------------------------------------------------------------------------- #
# Readers: an open stream of one source file, and how to close it
# --------------------------------------------------------------------------- #
def _tune(handle: Any, *, stall: int, prefetch: int | None = None, pipelined: bool = False) -> None:
    """paramiko's ``SFTPFile`` knobs, where the handle has them: a stall limit, read-ahead on a
    source (one request in flight per 32 KB otherwise - the 10 KB/s measured on this estate), and
    pipelined writes on a target."""
    if hasattr(handle, "settimeout"):
        handle.settimeout(stall)
    if prefetch is not None and hasattr(handle, "prefetch"):
        handle.prefetch(prefetch)
    if pipelined and hasattr(handle, "set_pipelined"):
        handle.set_pipelined(True)


_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def sftp_path(path: str) -> str:
    r"""The path an SFTP server takes for ``path``. A Windows drive path (``D:\SQLBK\x.bak``) is
    ``/D:/SQLBK/x.bak`` to Windows' OpenSSH (2026-11-11: a SQL Server reached over SSH, whose
    administrative share refuses its local account); a POSIX server never has one, so every other
    path is left as it is."""
    if _DRIVE_PATH.match(path):
        return "/" + path.replace("\\", "/")
    return path


class _SftpReader:
    """A file on an SSH source, read with read-ahead (paramiko's prefetch)."""

    def __init__(self, sftp, path: str, *, stall: int = STALL_SECONDS) -> None:
        path = sftp_path(path)
        size = sftp.stat(path).st_size
        self.size: int | None = int(size) if size is not None else None
        self.handle = sftp.open(path, "rb")
        _tune(self.handle, stall=stall, prefetch=self.size)

    def read(self, count: int) -> bytes:
        return self.handle.read(count)

    def finish(self) -> str:
        return ""

    def close(self) -> str:
        self.handle.close()
        return ""


class _LocalReader:
    """A file this node reads itself - a Windows node's UNC path."""

    def __init__(self, path: Path) -> None:
        self.handle = path.open("rb")
        self.size: int | None = path.stat().st_size

    def read(self, count: int) -> bytes:
        return self.handle.read(count)

    def finish(self) -> str:
        return ""

    def close(self) -> str:
        self.handle.close()
        return ""


class _SmbclientReader:
    """A file on a Windows share, streamed by ``smbclient`` through a FIFO.

    ``get`` writes to a local path; the FIFO makes that path a pipe this process reads, so nothing is
    staged on the worker. smbclient's own messages go to its stdout/stderr, never into the bytes.
    """

    def __init__(self, share: dict[str, Any], remote: str, *, stall: int = STALL_SECONDS) -> None:
        from dbabrain.common import smb

        self.folder = tempfile.mkdtemp(prefix="dbabrain_copy_")
        self.fifo = os.path.join(self.folder, "stream")
        os.mkfifo(self.fifo, 0o600)
        host, name = smb._share(share)
        self.auth = smb._auth_file(share)
        self.process = subprocess.Popen(
            ["smbclient", f"//{host}/{name}", "-A", str(self.auth),
             "-c", f'get "{smb._subpath(remote)}" "{self.fifo}"'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.size = None
        self.stall = stall
        # Opening a FIFO for reading blocks until smbclient opens it to write - or never, when
        # smbclient fails first (a wrong path, a refused login). Opened on a thread so that case
        # ends with smbclient's own words instead of a hang.
        opened: dict[str, Any] = {}
        thread = threading.Thread(target=lambda: opened.setdefault("handle", open(self.fifo, "rb")), daemon=True)
        thread.start()
        while thread.is_alive() and self.process.poll() is None:
            thread.join(0.2)
        if "handle" not in opened:
            thread.join(1)
        if "handle" not in opened:
            out, err = self.process.communicate()
            # The thread still waits in open(): a writer that opens and closes at once lets it
            # return (and read nothing), so no thread is left behind per failed file.
            try:
                os.close(os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            thread.join(1)
            if "handle" in opened:
                opened["handle"].close()
            self._cleanup()
            raise OSError(f"smbclient could not read {remote}: {(err or out or '').strip()[:500]}")
        self.handle = opened["handle"]
        self._ended: str | None = None

    def read(self, count: int) -> bytes:
        return self.handle.read(count)

    def finish(self) -> str:
        """Wait for smbclient and answer its failure ('' when it read the whole file)."""
        if self._ended is None:
            self.handle.close()
            try:
                out, err = self.process.communicate(timeout=self.stall)
            except subprocess.TimeoutExpired:
                self.process.kill()
                out, err = self.process.communicate()
            self._cleanup()
            self._ended = ("" if self.process.returncode == 0 else
                           f"smbclient exit {self.process.returncode}: {(err or out or '').strip()[:500]}")
        return self._ended

    def close(self) -> str:
        return self.finish()

    def _cleanup(self) -> None:
        for path in (self.fifo, str(self.auth)):
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.rmdir(self.folder)
        except OSError:
            pass


class _ExecReader:
    """A file streamed by ``cat`` over an SSH channel with a large window - on the host, or INSIDE a
    container by ``docker exec ... cat`` (0.37.0).

    The ``cat`` transfer: measured faster than SFTP on this estate's links. And the only way into a
    container:

    For a backup the database wrote into a Docker volume: on the host it lives under
    /var/lib/docker/volumes, which only root may enter - the SSH login sees nothing there (the
    store's first offload, 2026-11-10: "no file found to copy"). The backup itself was taken by
    ``docker exec`` into that container, so the copy reads it the same way, by its path in the
    container. The bytes are held to their stated size and sha256 by :func:`copy_one` as any other
    source's are; the exit status of ``cat`` is checked at the end.
    """

    def __init__(self, client, container: str, path: str, *, stall: int = STALL_SECONDS) -> None:
        q = shlex.quote
        inside = f"docker exec {q(container)} " if container else ""
        where = "in the container" if container else "on the host"
        from dbabrain.common import remote_exec

        _stdin, out, err = remote_exec.stream_on(client, f"{inside}stat -c %s -- {q(path)}", timeout_seconds=60)
        text = out.read().decode("utf-8", "replace").strip()
        if out.channel.recv_exit_status() != 0 or not text.isdigit():
            raise FileCopyError(f"{container + ':' if container else ''}{path}: cannot be read {where}: "
                                f"{(err.read().decode('utf-8', 'replace') or text).strip()[:300]}")
        self.size: int | None = int(text)
        # The executor opens the channel (R44): a large window, the stall limit on it.
        channel = remote_exec.channel_on(client, f"{inside}cat -- {q(path)}", window_bytes=WINDOW_BYTES,
                                         timeout_seconds=stall)
        if channel is not None:
            self.out, self.err = channel.makefile("rb", 1 << 20), channel.makefile_stderr("rb")
        else:
            _stdin, self.out, self.err = remote_exec.stream_on(client, f"{inside}cat -- {q(path)}",
                                                                bufsize=1 << 20, timeout_seconds=stall)
        self.label = f"{container}:{path}" if container else path

    def read(self, count: int) -> bytes:
        return self.out.read(count)

    def finish(self) -> str:
        """``""`` when ``cat`` ended well - else why, which fails the file before it is placed."""
        status = self.out.channel.recv_exit_status()
        return "" if status == 0 else (f"{self.label}: cat exited {status}: "
                                       f"{self.err.read().decode('utf-8', 'replace').strip()[:300]}")

    def close(self) -> str:
        self.out.channel.close()
        return ""


_ContainerReader = _ExecReader  # the name the container case had first


class ProcessReader:
    """A ``cat`` source read by a CHILD process - its own core (0.37.0, the operator: *make Python
    use more than one core*).

    Two SSH connections in one process take turns on one core: the store's 6 GB copy ran at 16 MB/s
    where each side alone made 69 and 43. The child (:func:`dbabrain.common.filecopy_source.stream`,
    started with ``multiprocessing`` - a function, not a command) holds the source's connection and
    sends the bytes over a pipe; this process hashes and writes. Its size comes first; the end of
    the file and how the read ended come last - a failure is the file's failure, never a short file.
    """

    def __init__(self, login: dict[str, Any], container: str, path: str, *, stall: int = STALL_SECONDS) -> None:
        import multiprocessing

        from dbabrain.common import filecopy_source

        self.stall, self.label = stall, f"{container + ':' if container else ''}{path}"
        context = multiprocessing.get_context("spawn")
        self.conn, child = context.Pipe(duplex=False)
        self.process = context.Process(target=filecopy_source.stream, daemon=True, name="dbabrain-copy-source",
                                       args=({"source": login, "container": container, "path": path,
                                              "stall": stall}, child))
        self.process.start()
        child.close()
        self.pending, self.ended, self.failure = b"", False, ""
        kind, value = self._message()
        if kind != "SIZE":
            self.close()
            raise FileCopyError(f"{self.label}: cannot be read: {value}"[:600])
        self.size: int | None = int(value) if value is not None else None

    def _wait(self) -> None:
        if not self.conn.poll(self.stall):
            raise OSError(f"{self.label}: the reading process sent nothing for {self.stall} s - stalled")

    def _message(self) -> tuple[str, Any]:
        try:
            self._wait()
            return self.conn.recv()
        except (EOFError, ConnectionError):  # Windows ends a dead pipe with BrokenPipeError, not EOFError
            return "ERROR", "the reading process ended without an answer"

    def read(self, count: int) -> bytes:
        while not self.pending and not self.ended:
            try:
                self._wait()
                chunk = self.conn.recv_bytes()
            except (EOFError, ConnectionError):  # Windows ends a dead pipe with BrokenPipeError
                self.ended, self.failure = True, "the reading process ended before the file did"
                break
            if not chunk:
                self.ended = True
                kind, value = self._message()
                self.failure = str(value or "") if kind == "END" else f"unexpected {kind}: {value}"
                break
            self.pending = chunk
        out, self.pending = self.pending[:count], self.pending[count:]
        return out

    def finish(self) -> str:
        return f"{self.label}: {self.failure}" if self.failure else ""

    def close(self) -> str:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
        self.process.join(timeout=10)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=10)
        return ""


def _reader_factory(request: dict[str, Any], *, stall: int = STALL_SECONDS, transfer: str = "sftp"
                    ) -> tuple[Callable[[str], Any], Callable[[], None], str]:
    """``(open(source_path) -> reader, close_all, source_kind)`` for the request's source."""
    share = request.get("source_share")
    if share is not None and request.get("source") is not None:
        raise FileCopyError('give "source" (an SSH host) or "source_share" (a Windows share), not both.')
    if isinstance(share, dict):
        from dbabrain.common import smb

        smb._share(share)  # host and share stated, or refused by name
        if os.name == "nt":
            host, name = smb._share(share)
            smb._register_if_given(share, host)
            return (lambda path: _LocalReader(smb._unc(host, name, smb._subpath(path)))), (lambda: None), "unc"
        return (lambda path: _SmbclientReader(share, path, stall=stall)), (lambda: None), "smbclient"
    if share is not None:
        raise FileCopyError('"source_share" must be an object {host, share, username, password, domain?}.')
    source = request.get("source")
    container = str((source or {}).get("container") or "").strip() if isinstance(source, dict) else ""
    if container or transfer == "cat":
        # A cat source - on the host, or IN a container by its path there (0.37.0) - is read by a
        # child process of its own: the copy then runs on two cores (see ProcessReader). The login
        # is proven here first, so a refused one is this command's error, not each file's.
        login = {k: v for k, v in source.items() if k != "container"}
        _ssh(login, role="source").close()
        kind = "container" if container else "cat"
        return (lambda path: ProcessReader(login, container, path, stall=stall)), (lambda: None), kind
    session = _ssh(source, role="source")
    sftp = session.sftp()
    return (lambda path: _SftpReader(sftp, path, stall=stall)), session.close, "sftp"


# --------------------------------------------------------------------------- #
# The copy
# --------------------------------------------------------------------------- #
def _mkdirs(sftp, directory: str) -> None:
    current = ""
    for part in [p for p in directory.split("/") if p]:
        current = f"{current}/{part}"
        try:
            sftp.stat(current)
        except IOError:
            try:
                sftp.mkdir(current)
            except IOError:
                pass  # raced, or unreadable: the open below names the real problem


def _place_new(sftp, partial: str, final: str) -> None:
    """``partial`` to ``final`` ONLY when nothing is at ``final``: SFTP's plain ``rename`` refuses an
    existing name (draft-ietf-secsh-filexfer-02, which OpenSSH implements). Never removes anything."""
    sftp.rename(partial, final)


def _exists(sftp, path: str) -> bool:
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False


def _replace(sftp, partial: str, final: str) -> None:
    """``partial`` over ``final`` in one step where the server can (OpenSSH's posix-rename), else
    removing ``final`` first - which only ever removes this copy's own earlier attempt at that name."""
    try:
        sftp.posix_rename(partial, final)
        return
    except (IOError, AttributeError):
        pass
    try:
        sftp.remove(final)
    except IOError:
        pass
    sftp.rename(partial, final)


class _ReadAhead:
    """Any reader, read in a thread of its own a few chunks ahead of the writer (0.37.0).

    A copy read a megabyte, then wrote it, then read the next: the source link idled while the
    target was written and the other way round - 20 MB/s for the store's 6 GB backup over links
    that each carry two to three times that. Reading ahead lets the two overlap. The bytes are the
    reader's, in order, untouched; an error in the reader is raised to the writer as it is."""

    def __init__(self, reader: Any, *, stall: int = STALL_SECONDS) -> None:
        import queue

        self.reader, self.stall = reader, stall
        self.size = getattr(reader, "size", None)
        self.pending = b""
        self.ended = False
        self.queue: Any = queue.Queue(maxsize=READ_AHEAD_CHUNKS)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="dbabrain-read-ahead", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            while not self.stop.is_set():
                chunk = self.reader.read(CHUNK)
                self.queue.put(chunk)
                if not chunk:
                    return
        except BaseException as exc:  # noqa: BLE001 - handed to the writer, never lost
            self.queue.put(exc)

    def read(self, count: int = -1) -> bytes:
        """At most ``count`` bytes (a file's contract - a decryptor reading through this relies on
        it), ``b""`` at the end."""
        import queue

        while not self.pending and not self.ended:
            try:
                item = self.queue.get(timeout=self.stall)
            except queue.Empty:
                raise OSError(f"the source sent nothing for {self.stall} s - stalled") from None
            if isinstance(item, BaseException):
                raise item
            if not item:
                self.ended = True
            self.pending = item
        size = len(self.pending) if count is None or count < 0 else count
        out, self.pending = self.pending[:size], self.pending[size:]
        return out

    def finish(self) -> str:
        self.thread.join(timeout=self.stall)
        return self.reader.finish()

    def halt(self) -> None:
        """Stop reading ahead - the file is done with, or failed. The reader itself is its owner's
        to close."""
        self.stop.set()
        while True:          # unblock a thread waiting on a full queue
            try:
                self.queue.get_nowait()
            except Exception:  # noqa: BLE001 - empty
                break


class _CatWriter:
    """The target file written through an SSH ``cat > partial`` channel with a large window (0.37.0)
    - measured at 43 MB/s into the repository's NFS folder, where SFTP writes made 25 MB/s. The exit
    status of ``cat`` is checked when the file is closed: a short write is an error, never a file."""

    def __init__(self, client: Any, partial: str) -> None:
        import shlex

        from dbabrain.common import remote_exec

        self.partial = partial
        self.channel = remote_exec.channel_on(client, f"cat > {shlex.quote(partial)}", window_bytes=WINDOW_BYTES)
        if self.channel is None:
            raise OSError(f"writing {partial}: the connection to the target has no channel to write through")

    def write(self, data: bytes) -> None:
        self.channel.sendall(data)

    def __enter__(self) -> "_CatWriter":
        return self

    def __exit__(self, kind, value, traceback) -> None:
        self.channel.shutdown_write()
        status = self.channel.recv_exit_status()
        if kind is None and status != 0:
            error = self.channel.recv_stderr(4096).decode("utf-8", "replace").strip()
            raise OSError(f"writing {self.partial} failed (cat exited {status}): {error[:300]}")


class FactMismatch(errors.DbaBrainError, OSError):
    """The file is not the one the line names: its size or its sha256 differs. Nothing is put in place."""

    kind = errors.KIND_FAILED


def copy_one(reader: Any, target_sftp, target: str, *, size_bytes: int, sha256: str, overwrite: bool,
             stall: int = STALL_SECONDS, target_client: Any = None) -> tuple[int, str]:
    """Stream one open source into ``target`` through its partial name - and put it under the real
    name only when its byte count and sha256 are the stated ones. ``(bytes written, sha256)``."""
    if reader.size is not None and int(reader.size) != int(size_bytes):
        # Refused before a byte is read: the source is not the file the line describes.
        raise FactMismatch(f"the source is {reader.size} bytes, not the {size_bytes} stated - not copied")
    _mkdirs(target_sftp, posixpath.dirname(target))
    partial = target + PARTIAL_SUFFIX
    written = 0
    digest = hashlib.sha256()
    ahead = None
    if target_client is not None:
        # The fast path (0.37.0): a `cat` channel, the source read ahead in its own thread.
        reader = ahead = _ReadAhead(reader, stall=stall)
        sink: Any = _CatWriter(target_client, partial)
    else:
        sink = target_sftp.open(partial, "wb")
        _tune(sink, stall=stall, pipelined=True)
    try:
        with sink as out:
            while True:
                chunk = reader.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
                written += len(chunk)
    except BaseException:
        if ahead is not None:
            ahead.halt()
        raise
    if reader.size is not None and written != reader.size:
        raise OSError(f"{written} bytes arrived of {reader.size} - the source changed or the stream was cut")
    # A stream with no size of its own (smbclient's FIFO) ends the same way whether the file was read
    # to its end or smbclient died half way: its exit says which, and it is asked BEFORE the rename.
    failure = reader.finish()
    if failure:
        raise OSError(failure)
    streamed = digest.hexdigest()
    if written != int(size_bytes):
        raise FactMismatch(f"{written} bytes streamed, not the {size_bytes} stated - not put in place")
    if streamed != sha256:
        raise FactMismatch(f"the bytes streamed hash to {streamed}, not the {sha256} stated - not put in place")
    if overwrite:
        _replace(target_sftp, partial, target)
    else:
        _place_new(target_sftp, partial, target)
    return written, streamed


class _Hashed:
    """A reader that hashes and counts what passes through it - the plain bytes, on their way to age."""

    def __init__(self, reader: Any) -> None:
        self.reader, self.hash, self.count = reader, hashlib.sha256(), 0

    def read(self, count: int = -1) -> bytes:
        data = self.reader.read(CHUNK if count is None or count < 0 else count)
        self.hash.update(data)
        self.count += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


class _HashedSink:
    """The ciphertext's writer: hashed and counted as it goes to the target's partial."""

    def __init__(self, out: Any) -> None:
        self.out, self.hash, self.count = out, hashlib.sha256(), 0

    def write(self, data: bytes) -> int:
        self.out.write(data)
        self.hash.update(data)
        self.count += len(data)
        return len(data)

    def flush(self) -> None:
        pass


def encryption_of(request: dict[str, Any], *, now: Callable[[], Any] | None = None) -> dict[str, Any] | None:
    """The request's ``encrypt`` block, checked - or None when the copy is plain."""
    block = request.get("encrypt")
    if block is None:
        return None
    if not isinstance(block, dict) or set(block) - {"recipients", "key_id", "key_expires_at"}:
        raise FileCopyError('"encrypt" is {"recipients": ["age1..."], "key_id": n, "key_expires_at": "..."} - '
                            f"nothing else; got {block!r}.")
    recipients = block.get("recipients")
    if not isinstance(recipients, list) or not recipients or not all(
            isinstance(r, str) and K.RECIPIENT.match(r) for r in recipients):
        raise FileCopyError('"encrypt.recipients" is a list of age1... public keys.')
    key_id = block.get("key_id")
    if isinstance(key_id, bool) or not isinstance(key_id, int) or key_id < 1:
        raise FileCopyError(f'"encrypt.key_id" is the key\'s number; got {key_id!r}.')
    from datetime import datetime, timezone

    until = K.parse_time(block.get("key_expires_at"), "encrypt.key_expires_at")
    moment = (now or (lambda: datetime.now(timezone.utc)))()
    if not K.may_encrypt(until, moment):
        raise FileCopyError(f"key {key_id} expired at {K.time_text(until)} - an expired key only decrypts; "
                            "nothing is copied with it.")
    return {"recipients": list(recipients), "key_id": key_id}


def copy_one_encrypted(reader: Any, target_sftp, target: str, *, size_bytes: int, sha256: str, overwrite: bool,
                       encrypt: dict[str, Any], stall: int = STALL_SECONDS, target_client: Any = None
                       ) -> dict[str, Any]:
    """Stream one open source through age into ``<target>.age`` - put in place only when the PLAIN bytes'
    count and sha256 are the stated ones. Answers the plain and the encrypted facts."""
    if reader.size is not None and int(reader.size) != int(size_bytes):
        raise FactMismatch(f"the source is {reader.size} bytes, not the {size_bytes} stated - not copied")
    pyrage, x25519 = K.age()
    recipients = [x25519.Recipient.from_str(r) for r in encrypt["recipients"]]
    final = target + K.SUFFIX
    _mkdirs(target_sftp, posixpath.dirname(final))
    partial = final + PARTIAL_SUFFIX
    ahead = None
    if target_client is not None:
        reader = ahead = _ReadAhead(reader, stall=stall)
        sink: Any = _CatWriter(target_client, partial)
    else:
        sink = target_sftp.open(partial, "wb")
        _tune(sink, stall=stall, pipelined=True)
    plain = _Hashed(reader)
    try:
        with sink as out:
            cipher = _HashedSink(out)
            pyrage.encrypt_io(plain, cipher, recipients)
    except BaseException:
        if ahead is not None:
            ahead.halt()
        raise
    if reader.size is not None and plain.count != reader.size:
        raise OSError(f"{plain.count} bytes arrived of {reader.size} - the source changed or the stream was cut")
    failure = reader.finish()
    if failure:
        raise OSError(failure)
    streamed = plain.hash.hexdigest()
    if plain.count != int(size_bytes):
        raise FactMismatch(f"{plain.count} bytes streamed, not the {size_bytes} stated - not put in place")
    if streamed != sha256:
        raise FactMismatch(f"the bytes streamed hash to {streamed}, not the {sha256} stated - not put in place")
    if overwrite:
        _replace(target_sftp, partial, final)
    else:
        _place_new(target_sftp, partial, final)
    import importlib.metadata as metadata

    return {"bytes_copied": plain.count, "sha256_streamed": streamed, "encrypted_path": final,
            "encrypted_size_bytes": cipher.count, "encrypted_sha256": cipher.hash.hexdigest(),
            "key_id": encrypt["key_id"], "age_version": f"pyrage {metadata.version('pyrage')}"}


def copy_files(request: dict[str, Any], *, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Copy each named file; answer one line per file (``ok``, ``bytes_copied``, ``error``)."""
    files = _files(request)
    transfer = transfer_of(request, "copy-files")
    overwrite = request.get("overwrite")
    if not isinstance(overwrite, bool):
        raise FileCopyError('"overwrite" is required: false (the default value to state - an existing target is '
                            f'left as it is) or true (it is replaced); got {overwrite!r}.')
    timeout = int(request.get("timeout_seconds") or TIMEOUT_SECONDS)
    stall = int(request.get("stall_seconds") or STALL_SECONDS)
    if stall < 10:
        raise FileCopyError(f'"stall_seconds" must be 10 or more; got {stall}.')
    encrypt = encryption_of(request)
    # Where each file lands: its own name, or <name>.age when it is encrypted on the way.
    landed = K.SUFFIX if encrypt else ""
    target_session = _ssh(request.get("target"), role="target")
    open_source, close_source, kind = None, None, ""
    answers: list[dict[str, Any]] = []
    started = clock()
    try:
        open_source, close_source, kind = _reader_factory(request, stall=stall, transfer=transfer)
        target_sftp = target_session.sftp()
        for item in files:
            line: dict[str, Any] = {"source": item["source"], "target": item["target"],
                                    "file_name": item["file_name"], "size_bytes": item["size_bytes"],
                                    "sha256": item["sha256"], "ok": False, "bytes_copied": 0,
                                    "sha256_streamed": None, "error": ""}
            answers.append(line)
            if clock() - started > timeout:
                line["error"] = f"not started: the command's {timeout}s were spent"
                continue
            reader = None
            if not overwrite and _exists(target_sftp, item["target"] + landed):
                # Left exactly as it is; the source is not read. The caller's hash of the target says
                # whether what is there is the file.
                line["error"] = "the target already holds a file and overwrite is false - left as it is"
                line["exists"] = True
                continue
            try:
                reader = open_source(item["source"])
                client = target_session.client if transfer == "cat" else None
                if encrypt:
                    line.update(copy_one_encrypted(
                        reader, target_sftp, item["target"], size_bytes=item["size_bytes"], sha256=item["sha256"],
                        overwrite=overwrite, encrypt=encrypt, stall=stall, target_client=client))
                else:
                    line["bytes_copied"], line["sha256_streamed"] = copy_one(
                        reader, target_sftp, item["target"], size_bytes=item["size_bytes"], sha256=item["sha256"],
                        overwrite=overwrite, stall=stall, target_client=client)
                line["ok"] = True
            except Exception as exc:  # noqa: BLE001 - one file's failure is that file's answer
                line["error"] = f"{type(exc).__name__}: {exc}"[:1000]
                try:
                    target_sftp.remove(item["target"] + landed + PARTIAL_SUFFIX)
                except Exception:  # noqa: BLE001 - nothing was written, or the link is gone
                    pass
            finally:
                if reader is not None:
                    try:
                        note = reader.close()
                    except Exception as exc:  # noqa: BLE001
                        note = f"{type(exc).__name__}: {exc}"
                    if note and line["ok"]:
                        # The bytes arrived, and the reader still says something went wrong: the
                        # caller's hash decides; the note travels with the file.
                        line["warning"] = note[:500]
    finally:
        if close_source is not None:
            close_source()
        target_session.close()
    copied = [line for line in answers if line["ok"]]
    return {"source_kind": kind, "transfer": transfer, "files": answers, "copied": len(copied),
            "failed": len(answers) - len(copied), "bytes_copied": sum(line["bytes_copied"] for line in copied),
            "encrypted": bool(encrypt)}
