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

The bytes are not hashed here. The checksum is the caller's (``file-checksum`` on each host, held to
what the backup recorded): a copier that judged its own copy would be the one witness the copy gate
exists not to trust.

Every value is in the request (R09): both logins, the share and its password. No password reaches a
command line - ``smbclient`` reads it from :func:`dbabrain.common.smb._auth_file`.
"""

from __future__ import annotations

import os
import posixpath
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from dbabrain.lib import errors

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


class FileCopyError(errors.RequestError):
    """The request cannot be run as written, or a host it names cannot be reached."""


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
def _files(request: dict[str, Any]) -> list[dict[str, str]]:
    files = request.get("files")
    if not isinstance(files, list) or not files:
        raise FileCopyError('"files" must be a non-empty list of {"source": ..., "target": ...}.')
    out = []
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise FileCopyError(f"files[{index}] must be an object with source and target.")
        source, target = str(item.get("source") or "").strip(), str(item.get("target") or "").strip()
        if not source or not target:
            raise FileCopyError(f"files[{index}] needs both source and target.")
        if not target.startswith("/"):
            raise FileCopyError(f"files[{index}].target must be an absolute path on the target host; got {target!r}.")
        if target.endswith("/") or posixpath.basename(target) in ("", ".", ".."):
            raise FileCopyError(f"files[{index}].target must name a file; got {target!r}.")
        out.append({"source": source, "target": target})
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
    return session


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


class _SftpReader:
    """A file on an SSH source, read with read-ahead (paramiko's prefetch)."""

    def __init__(self, sftp, path: str, *, stall: int = STALL_SECONDS) -> None:
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


def _reader_factory(request: dict[str, Any], *, stall: int = STALL_SECONDS
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
    session = _ssh(request.get("source"), role="source")
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


def copy_one(reader: Any, target_sftp, target: str, *, stall: int = STALL_SECONDS) -> int:
    """Stream one open source into ``target`` through its partial name; the bytes written."""
    _mkdirs(target_sftp, posixpath.dirname(target))
    partial = target + PARTIAL_SUFFIX
    written = 0
    with target_sftp.open(partial, "wb") as out:
        _tune(out, stall=stall, pipelined=True)
        while True:
            chunk = reader.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
    if reader.size is not None and written != reader.size:
        raise OSError(f"{written} bytes arrived of {reader.size} - the source changed or the stream was cut")
    # A stream with no size of its own (smbclient's FIFO) ends the same way whether the file was read
    # to its end or smbclient died half way: its exit says which, and it is asked BEFORE the rename.
    failure = reader.finish()
    if failure:
        raise OSError(failure)
    _replace(target_sftp, partial, target)
    return written


def copy_files(request: dict[str, Any], *, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Copy each named file; answer one line per file (``ok``, ``bytes_copied``, ``error``)."""
    files = _files(request)
    timeout = int(request.get("timeout_seconds") or TIMEOUT_SECONDS)
    stall = int(request.get("stall_seconds") or STALL_SECONDS)
    if stall < 10:
        raise FileCopyError(f'"stall_seconds" must be 10 or more; got {stall}.')
    target_session = _ssh(request.get("target"), role="target")
    open_source, close_source, kind = None, None, ""
    answers: list[dict[str, Any]] = []
    started = clock()
    try:
        open_source, close_source, kind = _reader_factory(request, stall=stall)
        target_sftp = target_session.sftp()
        for item in files:
            line: dict[str, Any] = {"source": item["source"], "target": item["target"], "ok": False,
                                    "bytes_copied": 0, "error": ""}
            answers.append(line)
            if clock() - started > timeout:
                line["error"] = f"not started: the command's {timeout}s were spent"
                continue
            reader = None
            try:
                reader = open_source(item["source"])
                line["bytes_copied"] = copy_one(reader, target_sftp, item["target"], stall=stall)
                line["ok"] = True
            except Exception as exc:  # noqa: BLE001 - one file's failure is that file's answer
                line["error"] = f"{type(exc).__name__}: {exc}"[:1000]
                try:
                    target_sftp.remove(item["target"] + PARTIAL_SUFFIX)
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
    return {"source_kind": kind, "files": answers, "copied": len(copied),
            "failed": len(answers) - len(copied), "bytes_copied": sum(line["bytes_copied"] for line in copied)}
