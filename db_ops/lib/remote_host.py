"""A Linux host reached through ``common.cli`` - what an app holds instead of an SSH client.

Two apps held a raw paramiko client from ``common``: ``control`` drives the worker for a deploy, and
``backup_restore`` stages SQL Server backups on a Linux restore target. An app may not import
``common`` (rules R03), so this is what they hold instead: every method is one ``common.cli`` call -
``run-cmd`` for a command, ``push-file`` / ``pull-file`` for one file - made through the ``call`` the
app passes in (``db_ops.transport.common_cli.run_allowing_failure``). This module builds each
request and reads each answer; it starts nothing itself (R07).

**Each call is its own SSH session.** That is slower than one long-lived channel - a second or two
per call - and it is why the batch methods exist: :meth:`put_files` and :meth:`get_files` move many
files as one tar, so a push of a few hundred small files costs three calls, not a few hundred.

Failures are :class:`RemoteError`, an ``OSError``: code written against SFTP caught ``IOError`` for
"that file is not there / not readable", and keeps meaning the same thing.
"""

from __future__ import annotations

from db_ops.lib import errors
import base64
import errno
import shlex
import stat as stat_mod
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from db_ops.lib.common_cli import CommonCliError

__all__ = ["BATCH_FILE_LIMIT_BYTES", "RemoteEntry", "RemoteError", "RemoteHost", "RemoteRun"]

#: ``(command, request, *, timeout_seconds=None) -> (success, data, error)`` - the transport's reader.
Call = Callable[..., tuple[bool, dict[str, Any], str]]

#: A file at least this big travels on its own; smaller ones share one tar. The deploy bundle holds
#: an image tar of several hundred MB beside a few hundred small files, and packing the big one
#: again would copy it on disk for nothing.
BATCH_FILE_LIMIT_BYTES = 8 * 1024 * 1024

#: How much longer than the remote command the process around it may take: python starting, the SSH
#: handshake, the answer. Only a stuck process meets it.
PROCESS_MARGIN_SECONDS = 120

#: How a ``run-cmd`` answer says the SSH session never opened - refused, reset, unreachable, timed
#: out. ``common.ssh`` names these at connect time, before any command is sent, so trying again
#: cannot run a command twice. An authentication failure is not one of them: it would only repeat
#: the same wrong password.
NEVER_CONNECTED = ("SSH connection to ", "SSH connect to ")

#: Seconds to wait before each further attempt at a session that never opened. A lab VM dropped
#: one connection in a burst on 2026-09-27 (``WinError 10054``) and that one failure failed a
#: database's whole restore; a moment later the host answered every call.
CONNECT_RETRY_DELAYS_SECONDS = (2, 5)


class RemoteError(errors.DbOpsError, OSError):
    """The host could not be reached, or the command or transfer did not happen."""

    kind = errors.KIND_FAILED


@dataclass(frozen=True)
class RemoteRun:
    """What one command did on the host."""

    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class RemoteEntry:
    """One directory entry, as ``stat`` reports it - the fields SFTP's ``SFTPAttributes`` gave."""

    name: str
    size: int
    mtime: float
    mode: int

    @property
    def is_dir(self) -> bool:
        return stat_mod.S_ISDIR(self.mode)

    # The SFTP spellings, so a caller reading an entry does not change with the transport.
    @property
    def filename(self) -> str:
        return self.name

    @property
    def st_size(self) -> int:
        return self.size

    @property
    def st_mode(self) -> int:
        return self.mode

    @property
    def st_mtime(self) -> float:
        return self.mtime


def quote(path: str | PurePosixPath) -> str:
    return shlex.quote(str(path))


class RemoteHost:
    """One Linux host, reached over SSH by ``common.cli``. Nothing is open between calls."""

    def __init__(self, *, host: str, username: str, call: Call, port: int = 22, password: str = "",
                 key_file: str = "", auth_type: str = "", connect_timeout_seconds: int = 30) -> None:
        self.host = str(host)
        self.username = str(username)
        self.port = int(port or 22)
        self.password = str(password or "")
        self.key_file = str(key_file or "")
        self.auth_type = str(auth_type or "") or ("password" if self.password else "key")
        self.connect_timeout_seconds = int(connect_timeout_seconds or 30)
        self._call = call

    def __repr__(self) -> str:  # never the password
        return f"RemoteHost({self.username}@{self.host}:{self.port})"

    # -- what every request carries ----------------------------------------------------------- #
    def access(self) -> dict[str, Any]:
        """``run-cmd``'s ``access`` block: a cmd_access, reached over SSH, Linux."""
        block: dict[str, Any] = {
            "method": "ssh", "platform": "linux",
            "host": self.host, "port": self.port, "username": self.username,
            "auth_type": self.auth_type, "timeout_seconds": self.connect_timeout_seconds,
        }
        return self._login(block)

    def host_block(self) -> dict[str, Any]:
        """``push-file`` / ``pull-file``'s ``host`` block. Two shapes because ``runtime`` means two
        things: where a command runs for ``run-cmd`` (host, docker, k8s), the machine's OS here."""
        return self._login({"runtime": "linux", "access": "ssh", "host": self.host,
                            "port": self.port, "username": self.username})

    def _login(self, block: dict[str, Any]) -> dict[str, Any]:
        if self.password:
            block["password"] = self.password
        if self.key_file:
            block["key_file"] = self.key_file
        return block

    def close(self) -> None:
        """Nothing to close: every call opened and closed its own session."""

    def __enter__(self) -> "RemoteHost":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- commands ----------------------------------------------------------------------------- #
    def run(self, command: str, *, sudo: bool = False, timeout_seconds: int | None = None) -> RemoteRun:
        """One shell line. ``sudo`` runs it through ``sudo -S`` with the login's own password on
        stdin - never on the remote argv, where ``ps`` would show it."""
        request: dict[str, Any] = {"access": self.access(), "command": command, "sudo": bool(sudo),
                                   "confirm": True, "assume_yes": True}
        return self._run_cmd(request, timeout_seconds)

    def run_script(self, script: str, *, timeout_seconds: int | None = None) -> RemoteRun:
        """A multi-line script, given to the remote shell on stdin."""
        request: dict[str, Any] = {"access": self.access(), "script": script,
                                   "confirm": True, "assume_yes": True}
        return self._run_cmd(request, timeout_seconds)

    def check(self, command: str, *, sudo: bool = False, timeout_seconds: int | None = None) -> RemoteRun:
        """:meth:`run`, raising :class:`RemoteError` unless the command exits 0."""
        result = self.run(command, sudo=sudo, timeout_seconds=timeout_seconds)
        if not result.ok:
            detail = (result.stderr or result.stdout).strip()[:400]
            raise RemoteError(f"{self}: exit {result.exit_code} from {command!r}"
                              + (f": {detail}" if detail else ""))
        return result

    def _run_cmd(self, request: dict[str, Any], timeout_seconds: int | None) -> RemoteRun:
        if timeout_seconds:
            request["timeout_seconds"] = int(timeout_seconds)
        delays = list(CONNECT_RETRY_DELAYS_SECONDS)
        while True:
            _success, data, error = self._invoke("run-cmd", request, timeout_seconds)
            if "exit_code" in data:
                break
            # No exit code is not a command that failed - it is a command that never ran. When it
            # never ran because the session never opened, it is safe to ask again.
            if delays and any(marker in str(error or "") for marker in NEVER_CONNECTED):
                time.sleep(delays.pop(0))
                continue
            raise RemoteError(f"{self}: {error or 'run-cmd gave no answer'}")
        return RemoteRun(int(data.get("exit_code") or 0), str(data.get("stdout") or ""),
                         str(data.get("stderr") or ""))

    def _invoke(self, command: str, request: dict[str, Any],
                timeout_seconds: int | None = None) -> tuple[bool, dict[str, Any], str]:
        process_timeout = int(timeout_seconds) + PROCESS_MARGIN_SECONDS if timeout_seconds else None
        try:
            return self._call(command, request, timeout_seconds=process_timeout)
        except CommonCliError as exc:
            raise RemoteError(f"{self}: {exc}") from exc

    # -- the file system ---------------------------------------------------------------------- #
    def exists(self, path: str) -> bool:
        return self.run(f"test -e {quote(path)}").ok

    def stat(self, path: str) -> RemoteEntry:
        """Size, mtime and mode of one path; ``FileNotFoundError`` when it is not there."""
        result = self.run(f"stat -c '%s %Y %f' -- {quote(path)}")
        fields = result.stdout.split()
        if not result.ok or len(fields) < 3:
            raise FileNotFoundError(errno.ENOENT, (result.stderr.strip() or "no such file"), str(path))
        return RemoteEntry(PurePosixPath(str(path)).name, int(fields[0]), float(fields[1]),
                           int(fields[2], 16))

    def listdir_attr(self, path: str) -> list[RemoteEntry]:
        """The entries directly under ``path``; ``FileNotFoundError`` when it is not a directory."""
        result = self.run(f"find {quote(path)} -mindepth 1 -maxdepth 1 -printf '%s %T@ %m %y %f\\n'")
        if not result.ok:
            raise FileNotFoundError(errno.ENOENT, (result.stderr.strip() or "no such directory"),
                                    str(path))
        entries: list[RemoteEntry] = []
        for line in result.stdout.splitlines():
            parts = line.split(" ", 4)
            if len(parts) < 5:
                continue
            size, mtime, perms, kind, name = parts
            kind_bits = {"d": stat_mod.S_IFDIR, "l": stat_mod.S_IFLNK}.get(kind, stat_mod.S_IFREG)
            entries.append(RemoteEntry(name, int(size), float(mtime), kind_bits | int(perms, 8)))
        return sorted(entries, key=lambda entry: entry.name)

    def listdir(self, path: str) -> list[str]:
        return [entry.name for entry in self.listdir_attr(path)]

    def mkdirs(self, path: str) -> None:
        self.check(f"mkdir -p -- {quote(path)}")

    def remove(self, path: str) -> None:
        self.check(f"rm -f -- {quote(path)}")

    def rename(self, source: str, target: str) -> None:
        """Replace ``target`` with ``source`` - ``mv -f``, atomic on one file system, as SFTP's
        ``posix_rename`` was."""
        self.check(f"mv -f -- {quote(source)} {quote(target)}")

    def set_mtime(self, path: str, mtime: float) -> None:
        self.check(f"touch -m -d @{int(mtime)} -- {quote(path)}")

    # -- files ------------------------------------------------------------------------------- #
    def put(self, local: str | Path, remote: str) -> dict[str, Any]:
        """One file here -> there, its directory made, its sha256 compared at both ends."""
        request = {"local_path": str(Path(local).resolve()), "remote_path": str(remote),
                   "host": self.host_block()}
        return self._transfer("push-file", request)

    def put_bytes(self, content: bytes, remote: str) -> None:
        """A few kilobytes straight into a file there, its directory made.

        Base64 on a here-document inside the script, rather than a file staged here and pushed: a
        backup certificate's private key goes this way, and it should touch no disk but the one it
        is meant for. It never reaches an argv either - the script is ``run-cmd``'s stdin, and the
        gate echoes only its first line, which is this command and not the content.
        """
        marker = f"DB_OPS_BYTES_{uuid.uuid4().hex}"
        encoded = base64.b64encode(content).decode("ascii")
        parent = str(PurePosixPath(remote).parent)
        script = (f"mkdir -p -- {quote(parent)} && base64 -d > {quote(remote)} <<'{marker}'\n"
                  f"{encoded}\n{marker}\n")
        result = self.run_script(script)
        if not result.ok:
            raise RemoteError(f"{self}: could not write {remote}: "
                              f"{(result.stderr or result.stdout).strip()[:400]}")

    def get(self, remote: str, local: str | Path) -> dict[str, Any]:
        """One file there -> here, hashed at both ends; :class:`RemoteError` if it cannot be read."""
        request = {"remote_path": str(remote), "local_path": str(Path(local).resolve()),
                   "host": self.host_block()}
        return self._transfer("pull-file", request)

    def _transfer(self, command: str, request: dict[str, Any]) -> dict[str, Any]:
        success, data, error = self._invoke(command, request)
        if not success:
            raise RemoteError(f"{self}: {command} {request.get('remote_path')}: {error}")
        return data

    def put_files(self, pairs: Iterable[tuple[Path, str]], *,
                  on_file: Callable[[Path, str], None] | None = None) -> None:
        """Every ``(local, remote)`` pair, the directories above them made.

        A big file goes on its own; the rest travel as one tar unpacked at ``/``, so the remote
        paths are exactly the ones named. ``on_file`` is told of each file once it has arrived.
        """
        small: list[tuple[Path, str]] = []
        for local, remote in ((Path(local), str(remote)) for local, remote in pairs):
            if local.stat().st_size < BATCH_FILE_LIMIT_BYTES:
                small.append((local, remote))
                continue
            self.put(local, remote)
            if on_file is not None:
                on_file(local, remote)
        if not small:
            return
        staged = f"/tmp/db_ops_push_{uuid.uuid4().hex}.tar"
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "batch.tar"
            with tarfile.open(archive, "w") as tar:
                for local, remote in small:
                    tar.add(str(local), arcname=str(PurePosixPath(remote)).lstrip("/"), recursive=False)
            self.put(archive, staged)
        try:
            # `--no-same-owner`: the files belong to the login that unpacks them, as an SFTP put's
            # did - never to the uid they had on the machine that packed them.
            self.check(f"tar -xf {quote(staged)} -C / --no-same-owner")
        finally:
            self.run(f"rm -f -- {quote(staged)}")
        if on_file is not None:
            for local, remote in small:
                on_file(local, remote)

    def put_tree(self, local_dir: Path, remote_dir: str, *,
                 on_file: Callable[[Path, str], None] | None = None) -> None:
        """Every file under ``local_dir`` to the same place under ``remote_dir``."""
        base = Path(local_dir)
        pairs = [(path, f"{remote_dir.rstrip('/')}/{path.relative_to(base).as_posix()}")
                 for path in sorted(base.rglob("*")) if path.is_file()]
        self.put_files(pairs, on_file=on_file)

    def get_files(self, pairs: Iterable[tuple[str, Path]]) -> None:
        """Every ``(remote, local)`` pair as one tar: packed there, pulled once, unpacked here."""
        pairs = [(str(remote), Path(local)) for remote, local in pairs]
        if not pairs:
            return
        staged = f"/tmp/db_ops_pull_{uuid.uuid4().hex}.tar"
        names = "\n".join(str(PurePosixPath(remote)).lstrip("/") for remote, _local in pairs)
        # The names go in on a here-document rather than the command line: a few hundred paths
        # would outgrow an argv, and a file name is data, never shell.
        marker = f"DB_OPS_NAMES_{uuid.uuid4().hex}"
        script = f"tar -cf {quote(staged)} -C / -T - <<'{marker}'\n{names}\n{marker}\n"
        try:
            packed = self.run_script(script)
            if not packed.ok:
                raise RemoteError(f"{self}: could not pack {len(pairs)} file(s): "
                                  f"{(packed.stderr or packed.stdout).strip()[:400]}")
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "batch.tar"
                self.get(staged, archive)
                with tarfile.open(archive) as tar:
                    for remote, local in pairs:
                        member = tar.extractfile(str(PurePosixPath(remote)).lstrip("/"))
                        if member is None:
                            raise RemoteError(f"{self}: {remote} is not a file")
                        local.parent.mkdir(parents=True, exist_ok=True)
                        local.write_bytes(member.read())
        finally:
            self.run(f"rm -f -- {quote(staged)}")
