"""Move one **named** file between this host and a remote one, over the SSH access it is
already configured with.

This is the manual-operations primitive. Every db_ops app that moves files does it as part of a
larger job — ``backup_restore.transfer`` syncs a whole backup directory between two remote hosts,
``backup_restore.copy_backup`` pulls a window of backups off an SMB share — and neither can answer
"put *this* file *there*". So that ended up being typed by hand as ``ssh``/``scp``/``docker cp``,
which is exactly the shape the repo has been burned by before: a throwaway command answers once
and takes its target resolution and its edge cases with it.

**Not a replacement for the backup transfers.** Per-file SFTP across two internet hops measured
10 KB/s here — about eight hours for the ~3900-file PostgreSQL backup set, which is why
:mod:`db_ops.backup_restore.transfer` streams a whole directory as a single ``tar`` instead.
Routing bulk staging through this module would reintroduce that, and would also stage the bytes
on the intermediate host's disk rather than passing them through in 256 KB chunks. Use it for a
handful of files you can name: a backup piece to inspect, a script to place, a log to collect.

**Input is a JSON object**, the same contract as ``run-sql`` and ``host-facts``::

    {
      "target": "CLOUD-203-0-113-188-ORA-1521",   // server_id / ip, from db_instances.json
      "access": { ... },                           // OR an inline cmd_access, for an unlisted host
      "remote_path": "/opt/oracle/backup/dbops/FREE_L0_20260802_f32div12_3555_1_1.bkp",
      "local_path": "runtime/incoming/FREE_L0_20260802_f32div12_3555_1_1.bkp",
      "overwrite": false,
      "make_dirs": true
    }

``local_path`` is resolved against the tool root when relative, never the process's working
directory: the daemon's cwd is not something a caller can predict, and a config that means one
file on the master and another on the worker is not a config.

The rules below are not defensive programming for its own sake — each one is a failure this
repository has already had:

* **The size is verified after every transfer**, and a short copy deletes what it wrote and
  fails. A backup piece that arrives truncated looks complete to everything downstream;
  ``copy_backup`` learned this the hard way and now re-fetches and finally refuses such files.
* **Overwriting requires saying so**, and even then the bytes land beside the target and are
  moved onto it atomically. A half-written file under the real name is worse than no file.
* **A destination that already holds the same size is left alone** and reported as skipped,
  matching how every other copy path in db_ops decides.
* **mtime is preserved.** The restore log-chain filter selects logs by file time relative to the
  full backup, so a transfer that resets mtime silently changes which logs get restored.
"""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path, PurePosixPath
from typing import Any

from db_ops.common import host_ops
from db_ops.common.remote_exec import RemoteExecError
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.paths import resolve_tool_path


#: Written beside the destination while the bytes are in flight, then renamed onto it.
PARTIAL_SUFFIX = ".dbops_partial"

STATUS_COPIED = "COPIED"
STATUS_REPLACED = "REPLACED"
STATUS_SKIPPED_EXISTS = "SKIPPED_EXISTS"

__all__ = [
    "FileTransferError",
    "PARTIAL_SUFFIX",
    "STATUS_COPIED",
    "STATUS_REPLACED",
    "STATUS_SKIPPED_EXISTS",
    "fetch_file",
    "pack_files",
    "relay_file",
    "send_file",
]


class FileTransferError(RuntimeError):
    """A user-facing failure: unknown target, missing source, refused overwrite, short copy."""


def _resolve_local(path_text: str) -> Path:
    path = Path(str(path_text))
    return resolve_tool_path(path)


def _require(request: dict[str, Any], field: str) -> str:
    value = str(request.get(field) or "").strip()
    if not value:
        raise FileTransferError(f"{field} is required.")
    return value


def _open(request: dict[str, Any], *, data_dir, secrets):
    target = host_ops.resolve_host(request, data_dir=data_dir)
    return target, host_ops.open_host_session(target, data_dir=data_dir, secrets=secrets)


def _remote_size(session, path: str) -> int | None:
    try:
        return int(session.sftp().stat(str(PurePosixPath(path))).st_size)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _result(*, target, status, local_path: Path, remote_path: str, size: int, started: float,
            direction: str) -> dict[str, Any]:
    return {
        "ok": True,
        "direction": direction,
        "status": status,
        "server_id": target.server_id,
        "host": target.host,
        "remote_path": str(remote_path),
        "local_path": str(local_path),
        "bytes": int(size),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def fetch_file(request: dict[str, Any], *, data_dir=None, secrets=None) -> dict[str, Any]:
    """Copy ``remote_path`` on the target down to ``local_path`` here."""
    started = time.monotonic()
    remote_path = _require(request, "remote_path")
    local_path = _resolve_local(_require(request, "local_path"))
    overwrite = bool(request.get("overwrite", False))
    make_dirs = bool(request.get("make_dirs", True))

    target, session = _open(request, data_dir=data_dir, secrets=secrets)
    try:
        remote_size = _remote_size(session, remote_path)
        if remote_size is None:
            raise FileTransferError(
                f"{target.describe()}: {remote_path} does not exist or is not readable as "
                f"{target.access.get('username') or 'the configured user'}."
            )
        if local_path.exists():
            existing = local_path.stat().st_size
            if existing == remote_size:
                return _result(target=target, status=STATUS_SKIPPED_EXISTS, local_path=local_path,
                               remote_path=remote_path, size=remote_size, started=started,
                               direction="fetch")
            if not overwrite:
                raise FileTransferError(
                    f"{local_path} already exists with a different size "
                    f"(local {existing} bytes, remote {remote_size} bytes). "
                    "Pass \"overwrite\": true to replace it."
                )
        if make_dirs:
            local_path.parent.mkdir(parents=True, exist_ok=True)

        staged = local_path.with_name(local_path.name + PARTIAL_SUFFIX)
        try:
            session.get(remote_path, staged)
            actual = staged.stat().st_size
            if actual != remote_size:
                raise FileTransferError(
                    f"short read: {remote_path} is {remote_size} bytes on {target.host} but "
                    f"{actual} bytes arrived. The partial file was removed."
                )
            mtime = session.sftp().stat(str(PurePosixPath(remote_path))).st_mtime
            replaced = local_path.exists()
            os.replace(staged, local_path)
            if mtime:
                os.utime(local_path, (mtime, mtime))
        finally:
            # Never leave a partial behind under any name — the next run would size-compare
            # against it and a stale .dbops_partial is a file nobody knows the provenance of.
            if staged.exists():
                staged.unlink(missing_ok=True)

        return _result(target=target, status=STATUS_REPLACED if replaced else STATUS_COPIED,
                       local_path=local_path, remote_path=remote_path, size=remote_size,
                       started=started, direction="fetch")
    except RemoteExecError as exc:
        raise FileTransferError(f"{target.describe()}: {exc}") from exc
    finally:
        session.close()


def send_file(request: dict[str, Any], *, data_dir=None, secrets=None) -> dict[str, Any]:
    """Copy ``local_path`` here up to ``remote_path`` on the target."""
    started = time.monotonic()
    remote_path = _require(request, "remote_path")
    local_path = _resolve_local(_require(request, "local_path"))
    overwrite = bool(request.get("overwrite", False))
    make_dirs = bool(request.get("make_dirs", True))

    if not local_path.is_file():
        raise FileTransferError(f"{local_path} does not exist or is not a file.")
    local_size = local_path.stat().st_size

    target, session = _open(request, data_dir=data_dir, secrets=secrets)
    try:
        existing = _remote_size(session, remote_path)
        if existing is not None:
            if existing == local_size:
                return _result(target=target, status=STATUS_SKIPPED_EXISTS, local_path=local_path,
                               remote_path=remote_path, size=local_size, started=started,
                               direction="send")
            if not overwrite:
                raise FileTransferError(
                    f"{remote_path} already exists on {target.host} with a different size "
                    f"(remote {existing} bytes, local {local_size} bytes). "
                    "Pass \"overwrite\": true to replace it."
                )
        parent = str(PurePosixPath(remote_path).parent)
        if make_dirs and parent not in ("", "/"):
            session.mkdirs(parent)

        staged = remote_path + PARTIAL_SUFFIX
        session.put(local_path, staged)
        arrived = _remote_size(session, staged)
        if arrived != local_size:
            try:
                session.sftp().remove(staged)
            except OSError:
                pass
            raise FileTransferError(
                f"short write: {local_path} is {local_size} bytes but {arrived} bytes arrived on "
                f"{target.host}. The partial file was removed."
            )
        _atomic_replace(session, staged, remote_path)
        try:
            stat = local_path.stat()
            session.sftp().utime(str(PurePosixPath(remote_path)), (stat.st_atime, stat.st_mtime))
        except OSError:
            pass

        return _result(target=target, status=STATUS_REPLACED if existing is not None else STATUS_COPIED,
                       local_path=local_path, remote_path=remote_path, size=local_size,
                       started=started, direction="send")
    except RemoteExecError as exc:
        raise FileTransferError(f"{target.describe()}: {exc}") from exc
    finally:
        session.close()


def _atomic_replace(session, staged: str, destination: str) -> None:
    """Move ``staged`` onto ``destination`` in one step where the server supports it.

    Same rule and same fallback as ``backup_restore.transfer``: prefer the OpenSSH
    ``posix-rename`` extension, which overwrites in a single syscall, and only fall back to
    remove-then-rename when the server lacks it.
    """
    sftp = session.sftp()
    try:
        sftp.posix_rename(staged, destination)
        return
    except (IOError, OSError, AttributeError):
        pass
    try:
        sftp.remove(destination)
    except (IOError, OSError):
        pass
    sftp.rename(staged, destination)


# --------------------------------------------------------------------------- #
# Packing — the answer to "I need more than one file"
# --------------------------------------------------------------------------- #
# fetch_file/send_file move exactly one named file, and that is deliberate: per-file SFTP is what
# makes a many-file transfer take hours (10 KB/s measured across two internet hops). So the way to
# move a set is to make it ONE file first, on the host that already holds it, and move that. The
# bytes then cross the link already grouped and the link pays one transfer's latency, not N.
#
# The result carries a sha256 because size alone cannot answer the question an operator actually
# has after a transfer: "is what landed the same bytes I packed?" Size is what the copy paths
# compare — it is the only thing SFTP offers cheaply per file — and it catches a truncated copy
# but not a corrupted one.

#: Hashing runs on the host that holds the file. Streaming it here to hash it would move exactly
#: the bytes that packing exists to avoid moving.
_SHA256_COMMAND = {
    "bash": "sha256sum {path}",
    "powershell": "(Get-FileHash -Algorithm SHA256 -LiteralPath {path}).Hash.ToLower()",
}


def _shell_of(target) -> str:
    shell = str((target.access or {}).get("shell") or "").strip().lower()
    if shell in _SHA256_COMMAND:
        return shell
    return "powershell" if str(getattr(target, "platform", "")).lower() == "windows" else "bash"


def _run_checked(session, command: str, *, what: str, target) -> str:
    result = session.run(command)
    if not result.ok:
        raise FileTransferError(
            f"{target.describe()}: {what} failed (exit {result.exit_code}): "
            f"{(result.stderr or result.stdout or '').strip()[:400]}"
        )
    return (result.stdout or "").strip()


def _local_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pack_files(request: dict[str, Any], *, data_dir=None, secrets=None) -> dict[str, Any]:
    """Pack ``files`` (or everything under ``folder``) into one archive, and describe it.

    Runs on the host named by ``target``; with no ``target`` it packs here. Either way the archive
    is built where the files already are, so nothing crosses the network in order to be packed.

    Request::

        {
          "target": "CLOUD-203-0-113-188-ORA-1521",       // omit to pack on this host
          "files": ["/opt/.../a.bkp", "/opt/.../b.bkp"],   // OR
          "folder": "/opt/oracle/backup/dbops",
          "include": "*.bkp",              // optional shell glob, only with "folder"
          "archive_path": "/tmp/ora_pieces_20260804.tar",
          "format": "tar" | "tar.gz" | "zip",   // default "tar"
          "checksum_files": false,         // per-member sha256 too; off by default because it
                                           // re-reads every byte and these sets are GBs
          "overwrite": false
        }

    ``format`` defaults to plain ``tar``, not a compressed one, because the sets this exists for
    are backup pieces the engine has already compressed: gzip then spends CPU on every byte to
    save almost nothing. Ask for ``tar.gz`` when the contents are text (logs, configs), where it
    earns its keep.
    """
    started = time.monotonic()
    archive_path = _require(request, "archive_path")
    fmt = str(request.get("format") or "tar").strip().lower()
    if fmt not in {"tar", "tar.gz", "zip"}:
        raise FileTransferError(f"format must be tar, tar.gz or zip; got '{fmt}'.")
    files = [str(item) for item in (request.get("files") or []) if str(item).strip()]
    folder = str(request.get("folder") or "").strip()
    if bool(files) == bool(folder):
        raise FileTransferError('give exactly one of "files" (an array) or "folder".')
    include = str(request.get("include") or "").strip()
    if include and not folder:
        raise FileTransferError('"include" filters a "folder"; it does nothing with "files".')
    overwrite = bool(request.get("overwrite", False))
    checksum_files = bool(request.get("checksum_files", False))

    names_a_host = str(request.get("target") or request.get("server_id") or "").strip()
    if not names_a_host and not request.get("access"):
        return _pack_local(
            archive_path=archive_path, fmt=fmt, files=files, folder=folder, include=include,
            overwrite=overwrite, checksum_files=checksum_files, started=started,
        )
    return _pack_remote(
        request, archive_path=archive_path, fmt=fmt, files=files, folder=folder, include=include,
        overwrite=overwrite, checksum_files=checksum_files, started=started,
        data_dir=data_dir, secrets=secrets,
    )


def _pack_remote(request, *, archive_path, fmt, files, folder, include, overwrite,
                 checksum_files, started, data_dir, secrets) -> dict[str, Any]:
    target, session = _open(request, data_dir=data_dir, secrets=secrets)
    try:
        shell = _shell_of(target)
        if shell != "bash":
            raise FileTransferError(
                f"{target.describe()}: packing on a Windows host is not implemented. Pack it "
                "there by hand, or fetch the files one at a time with fetch-file."
            )
        quoted_archive = shlex.quote(archive_path)
        if _remote_size(session, archive_path) is not None and not overwrite:
            raise FileTransferError(
                f"{archive_path} already exists on {target.host}. "
                'Pass "overwrite": true to replace it.'
            )

        if folder:
            quoted_folder = shlex.quote(folder)
            if fmt == "zip":
                # cd first so the archive holds paths relative to the folder: unpacking elsewhere
                # must not recreate the source host's whole absolute path.
                command = f"cd {quoted_folder} && zip -q -0 -r {quoted_archive} {include or '.'}"
            else:
                flags = "-czf" if fmt == "tar.gz" else "-cf"
                if include:
                    # find -print0 into tar --null, not `ls` in a command substitution: the shell
                    # word-splits `ls` output, so one file name containing a space becomes two
                    # members that do not exist and tar fails the whole archive.
                    command = (
                        f"cd {quoted_folder} && find . -maxdepth 1 -type f -name {shlex.quote(include)} "
                        f"-print0 | tar {flags} {quoted_archive} --null -T -"
                    )
                else:
                    command = f"tar {flags} {quoted_archive} -C {quoted_folder} ."
        else:
            missing = [path for path in files if _remote_size(session, path) is None]
            if missing:
                raise FileTransferError(
                    f"{target.describe()}: {len(missing)} requested file(s) do not exist, "
                    f"first: {missing[0]}"
                )
            joined = " ".join(shlex.quote(path) for path in files)
            if fmt == "zip":
                command = f"zip -q -0 -j {quoted_archive} {joined}"
            else:
                # -P keeps the absolute paths readable in the listing while tar still stores them
                # relative; without it tar warns on every member and the warning looks like a fault.
                flags = "-czf" if fmt == "tar.gz" else "-cf"
                command = f"tar {flags} {quoted_archive} {joined}"
        _run_checked(session, command, what=f"building {archive_path}", target=target)

        size = _remote_size(session, archive_path)
        if not size:
            raise FileTransferError(f"{target.describe()}: {archive_path} is empty or absent after packing.")
        sha = _run_checked(
            session, _SHA256_COMMAND[shell].format(path=shlex.quote(archive_path)),
            what="hashing the archive", target=target,
        ).split()[0]

        members: list[dict[str, Any]] = []
        if checksum_files and files:
            for path in files:
                line = _run_checked(
                    session, _SHA256_COMMAND[shell].format(path=shlex.quote(path)),
                    what=f"hashing {path}", target=target,
                )
                members.append({"path": path, "bytes": _remote_size(session, path),
                                "sha256": line.split()[0]})

        return {
            "ok": True,
            "packed_on": target.host,
            "server_id": target.server_id,
            "archive_path": archive_path,
            "format": fmt,
            "bytes": int(size),
            "sha256": sha,
            "file_count": len(files) or None,
            "files": members or None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except RemoteExecError as exc:
        raise FileTransferError(f"{target.describe()}: {exc}") from exc
    finally:
        session.close()


def _pack_local(*, archive_path, fmt, files, folder, include, overwrite, checksum_files,
                started) -> dict[str, Any]:
    import fnmatch
    import tarfile
    import zipfile

    destination = _resolve_local(archive_path)
    if destination.exists() and not overwrite:
        raise FileTransferError(
            f'{destination} already exists. Pass "overwrite": true to replace it.'
        )
    if folder:
        root = _resolve_local(folder)
        if not root.is_dir():
            raise FileTransferError(f"{root} is not a directory.")
        members = [path for path in sorted(root.rglob("*")) if path.is_file()
                   and (not include or fnmatch.fnmatch(path.name, include))]
        arcnames = [str(path.relative_to(root)) for path in members]
    else:
        members = [_resolve_local(item) for item in files]
        absent = [path for path in members if not path.is_file()]
        if absent:
            raise FileTransferError(
                f"{len(absent)} requested file(s) do not exist, first: {absent[0]}"
            )
        arcnames = [path.name for path in members]
    if not members:
        raise FileTransferError("nothing matched; the archive would be empty.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "zip":
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
            for path, arcname in zip(members, arcnames):
                archive.write(path, arcname)
    else:
        with tarfile.open(destination, "w:gz" if fmt == "tar.gz" else "w") as archive:
            for path, arcname in zip(members, arcnames):
                archive.add(path, arcname=arcname)

    return {
        "ok": True,
        "packed_on": "local",
        "server_id": "",
        "archive_path": str(destination),
        "format": fmt,
        "bytes": destination.stat().st_size,
        "sha256": _local_sha256(destination),
        "file_count": len(members),
        "files": ([{"path": str(path), "bytes": path.stat().st_size,
                    "sha256": _local_sha256(path)} for path in members]
                  if checksum_files else None),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


# --------------------------------------------------------------------------- #
# Relaying — the answer to "this file has to end up on the OTHER host"
# --------------------------------------------------------------------------- #
# fetch_file + send_file already move a file between a host and here, so a host-to-host copy
# could be typed as two commands. It is not, for two reasons this repository has already paid
# for. The bytes would land on the orchestrator's disk — a Windows master moving a 3 GB docker
# image bundle has to have 3 GB free for a file it never reads — and the two halves would be
# separately verified, so a hash mismatch names the *second* hop and leaves the operator
# guessing which copy is the bad one. Relaying streams the source's stdout straight into the
# destination's stdin in 256 KB chunks, hashes both ends of the whole trip, and stages nothing
# here.
#
# The target pulling directly from the source would be better still and is deliberately not
# done: the two hosts are not assumed to reach each other, and an SSH trust created between two
# database hosts to serve one file move is a permanent widening of access for a temporary need.
# The orchestrator already holds both credentials, so it is the one place that can bridge them
# without creating anything. Same reasoning as ``backup_restore.transfer``, which is where the
# streaming shape below comes from.

_RELAY_CHUNK = 1 << 18


class _Drained:
    """Read a channel's stderr on its own thread for the whole transfer.

    Nothing reads these streams until after ``recv_exit_status()``, which cannot be reached
    while the copy is still running — so a command that complains on every chunk fills its
    stderr window, blocks on the write, stops reading stdin, and the pipeline seizes with no
    error anywhere. ``backup_restore.transfer`` learned that by sitting at RUNNING for two
    hours; the lesson applies to one file exactly as it did to four thousand.
    """

    def __init__(self, handle) -> None:
        import threading

        self._chunks: list[str] = []
        self._thread = threading.Thread(target=self._pump, args=(handle,), daemon=True)
        self._thread.start()

    def _pump(self, handle) -> None:
        try:
            for line in handle:
                self._chunks.append(
                    line if isinstance(line, str) else line.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - diagnostics must never raise over the real error
            pass

    def text(self) -> str:
        self._thread.join(timeout=5)
        return "".join(self._chunks)


def relay_file(request: dict[str, Any], *, data_dir=None, secrets=None) -> dict[str, Any]:
    """Copy one file from one host to another, streaming through here without staging.

    Request::

        {"source":      {"target": "ACME-192-0-2-249-HOST", "path": "/tmp/bundle.tar.gz"},
         "destination": {"target": "ACME-192-0-2-11-MSSQL25-1433", "path": "/tmp/b.tar.gz"},
         "overwrite": false, "make_dirs": true}

    Each side is resolved exactly like ``fetch-file``/``send-file`` resolve theirs — a
    ``target`` from ``db_instances.json`` or an inline ``access`` block — so no password is
    ever written into the request.

    Linux/SSH on both ends. A Windows end is refused rather than half-supported: the stream is
    ``cat``/``sha256sum``, and the PowerShell equivalents do not compose into a pipe the same
    way. Use fetch-file then send-file there, and accept the staging.
    """
    started = time.monotonic()
    source_req = request.get("source")
    dest_req = request.get("destination")
    if not isinstance(source_req, dict) or not isinstance(dest_req, dict):
        raise FileTransferError('relay-file needs "source" and "destination" objects.')

    source_path = _require(source_req, "path")
    dest_path = _require(dest_req, "path")
    overwrite = bool(request.get("overwrite", False))
    make_dirs = bool(request.get("make_dirs", True))

    src_target, src_session = _open(source_req, data_dir=data_dir, secrets=secrets)
    dst_session = None
    try:
        dst_target, dst_session = _open(dest_req, data_dir=data_dir, secrets=secrets)
        for target in (src_target, dst_target):
            if _shell_of(target) != "bash":
                raise FileTransferError(
                    f"{target.describe()}: relay-file streams over sh; a Windows end is not "
                    "supported. Use fetch-file then send-file for that hop."
                )

        size = _remote_size(src_session, source_path)
        if size is None:
            raise FileTransferError(
                f"{src_target.describe()}: {source_path} does not exist or is not readable as "
                f"{src_target.access.get('username') or 'the configured user'}."
            )
        if not overwrite and _remote_size(dst_session, dest_path) is not None:
            raise FileTransferError(
                f"{dst_target.describe()}: {dest_path} already exists; pass "
                '"overwrite": true to replace it.'
            )

        # Hash the source BEFORE the stream, not after: a file still being written by whatever
        # produced it would otherwise hash one set of bytes and send another, and the mismatch
        # would be blamed on the network.
        source_sha = _run_checked(
            src_session, f"sha256sum {shlex.quote(source_path)}",
            what="sha256sum", target=src_target).split()[0]

        staged = f"{dest_path}{PARTIAL_SUFFIX}"
        directory = str(PurePosixPath(dest_path).parent)
        prefix = f"mkdir -p {shlex.quote(directory)} && " if make_dirs else ""

        src_in, src_out, src_err = src_session.client.exec_command(
            f"cat {shlex.quote(source_path)}", timeout=None)
        dst_in, dst_out, dst_err = dst_session.client.exec_command(
            f"{prefix}cat > {shlex.quote(staged)}", timeout=None)
        src_errors, dst_errors = _Drained(src_err), _Drained(dst_err)
        moved = 0
        try:
            src_in.close()
            while True:
                chunk = src_out.read(_RELAY_CHUNK)
                if not chunk:
                    break
                dst_in.write(chunk)
                moved += len(chunk)
            dst_in.flush()
            dst_in.channel.shutdown_write()
            src_rc = src_out.channel.recv_exit_status()
            dst_rc = dst_out.channel.recv_exit_status()
        finally:
            for handle in (src_in, dst_in):
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass
        if src_rc != 0 or dst_rc != 0:
            detail = (src_errors.text() + dst_errors.text()).strip()
            raise FileTransferError(
                f"relay {source_path} -> {dest_path} failed (source exit {src_rc}, destination "
                f"exit {dst_rc}): {detail[:400]}"
            )

        dest_sha = _run_checked(
            dst_session, f"sha256sum {shlex.quote(staged)}",
            what="sha256sum", target=dst_target).split()[0]
        if dest_sha != source_sha:
            # Leave nothing behind under either name: a corrupt .partial that a later run
            # mistakes for a resumable transfer is the failure this check exists to prevent.
            dst_session.run(f"rm -f {shlex.quote(staged)}")
            raise FileTransferError(
                f"relay {source_path} -> {dest_path}: sha256 mismatch (source {source_sha}, "
                f"destination {dest_sha}); the copy was discarded."
            )
        _atomic_replace(dst_session, staged, dest_path)

        return {
            "ok": True,
            "direction": "relay",
            "status": STATUS_COPIED,
            "source": {"server_id": src_target.server_id, "host": src_target.host,
                       "path": source_path},
            "destination": {"server_id": dst_target.server_id, "host": dst_target.host,
                            "path": dest_path},
            "bytes": int(moved),
            "sha256": source_sha,
            "verified": True,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    finally:
        for session in (src_session, dst_session):
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
