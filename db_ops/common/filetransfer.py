"""Packing a backup set into one archive, and moving that archive between a host and here.

Three operations that belong together because they are one workflow: pack on the machine that
already holds the files, move the single archive, unpack where it is needed. Splitting the move
from the pack is what makes it fast — per-file SFTP across two internet hops measured 10 KB/s on
this estate, and a backup set is thousands of small files.

Everything is driven by the request, like the rest of this layer: the host, the credentials, the
runtime it runs in. Nothing is looked up.

**The sha256 is the point of the result, not decoration.** Size alone catches a truncated copy and
nothing else; a backup that arrived corrupted is a backup that restores and is wrong, which is the
failure this whole layer keeps trying to make impossible. So the archive is hashed where it was
made and hashed again where it landed, and the two are compared.
"""

from __future__ import annotations

import hashlib
import os
import shlex
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from db_ops.common.hostcmd import WINDOWS, Host, HostCommandError, open_client, parse_host, run

TAR = "tar"
ZIP = "zip"
FORMATS = (TAR, ZIP)


class FileTransferError(RuntimeError):
    """The pack or the move could not be completed."""


def _remote_path(host: Host, path: str):
    """A path object for the TARGET's filesystem, never for the one this process runs on.

    ``pathlib.Path`` picks its flavour from the machine executing the code, so computing a remote
    parent with it turned ``/b/one.bkp`` into ``\b`` when the worker was Windows - a directory
    that does not exist on either end. The target's runtime decides the flavour, because the
    target is whose filesystem the string describes.
    """
    return PureWindowsPath(path) if host.runtime == WINDOWS else PurePosixPath(path)


def _pack_command(host: Host, *, archive: str, folder: str, files: list[str],
                  fmt: str) -> str:
    """The command that makes the archive, in that host's own idiom.

    Windows is not an afterthought here: ``tar.exe`` ships with Windows 10/2019 onward and behaves,
    while ``zip`` does not exist at all — that is ``Compress-Archive``. Picking the wrong one fails
    with "not recognized as a cmdlet", which reads like a broken host rather than a wrong verb.
    """
    if host.runtime == WINDOWS:
        if fmt == ZIP:
            source = folder if folder else ",".join(f"'{f}'" for f in files)
            item = f"'{folder}\\*'" if folder else source
            return (f"Compress-Archive -Path {item} -DestinationPath '{archive}' -Force")
        listed = " ".join(f'"{_remote_path(host, f).name}"' for f in files)
        base = folder or (str(_remote_path(host, files[0]).parent) if files else "")
        return f'tar -cf "{archive}" -C "{base}" {listed or "."}'

    if fmt == ZIP:
        if folder:
            return f"cd {shlex.quote(folder)} && zip -r -q {shlex.quote(archive)} ."
        names = " ".join(shlex.quote(f) for f in files)
        return f"zip -q -j {shlex.quote(archive)} {names}"

    if folder:
        return f"tar -cf {shlex.quote(archive)} -C {shlex.quote(folder)} ."
    base = str(_remote_path(host, files[0]).parent) if files else "/"
    names = " ".join(shlex.quote(_remote_path(host, f).name) for f in files)
    return f"tar -cf {shlex.quote(archive)} -C {shlex.quote(base)} {names}"


def _hash_command(host: Host, path: str) -> str:
    if host.runtime == WINDOWS:
        return f"(Get-FileHash -Algorithm SHA256 '{path}').Hash.ToLower()"
    return f"sha256sum {shlex.quote(path)} | cut -d' ' -f1"


def _size_command(host: Host, path: str) -> str:
    if host.runtime == WINDOWS:
        return f"(Get-Item '{path}').Length"
    return f"stat -c %s {shlex.quote(path)}"


def pack(request: dict[str, Any]) -> dict[str, Any]:
    """Pack a folder, or a named list of files, into one archive on the host that holds them."""
    host = parse_host(request.get("host"))
    archive = str(request.get("archive_path") or "").strip()
    folder = str(request.get("folder") or "").strip()
    files = [str(f) for f in (request.get("files") or []) if str(f).strip()]
    fmt = str(request.get("format") or TAR).strip().lower()

    if not archive:
        raise FileTransferError("archive_path is required: where to write the archive.")
    if fmt not in FORMATS:
        raise FileTransferError(f"format must be one of {', '.join(FORMATS)}; got {fmt!r}.")
    if bool(folder) == bool(files):
        # Both, or neither. Both is a request that says two things; neither says nothing.
        raise FileTransferError("give exactly one of folder or files.")

    command = _pack_command(host, archive=archive, folder=folder, files=files, fmt=fmt)
    result = run(host, command, timeout=int(request.get("timeout_seconds") or 3600))
    if result["exit_code"] != 0:
        raise FileTransferError(
            f"packing failed (exit {result['exit_code']}): "
            f"{(result['stderr'] or result['stdout']).strip()[-400:]}")

    digest = run(host, _hash_command(host, archive))["stdout"].strip().split()[0].lower()
    size = run(host, _size_command(host, archive))["stdout"].strip()
    return {"archive_path": archive, "format": fmt, "sha256": digest,
            "size": int(size) if size.isdigit() else None,
            "packed": len(files) if files else None, "folder": folder or None}


def _sha256_local(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(expected: str, actual: str, *, where: str) -> None:
    """Compare the two hashes, and say plainly when they differ.

    Reported rather than silently retried: a mismatch means the bytes changed in flight, and a
    retry that succeeds hides a link or a disk that is doing this occasionally.
    """
    if expected and actual and expected.lower() != actual.lower():
        raise FileTransferError(
            f"sha256 mismatch {where}: expected {expected}, got {actual}. The archive that landed "
            "is not the archive that was packed - do not restore from it."
        )


def pull(request: dict[str, Any]) -> dict[str, Any]:
    """Copy one file from a host to this machine, and prove it arrived intact."""
    host = parse_host(request.get("host"))
    remote = str(request.get("remote_path") or "").strip()
    local = str(request.get("local_path") or "").strip()
    if not remote or not local:
        raise FileTransferError("remote_path and local_path are both required.")

    expected = run(host, _hash_command(host, remote))["stdout"].strip().split()
    expected_hash = expected[0].lower() if expected else ""

    Path(local).parent.mkdir(parents=True, exist_ok=True)
    client = open_client(host)
    try:
        sftp = client.open_sftp()
        try:
            sftp.get(remote, local)
        finally:
            sftp.close()
    finally:
        client.close()

    actual = _sha256_local(local)
    _verify(expected_hash, actual, where=f"pulling {remote}")
    return {"remote_path": remote, "local_path": local, "sha256": actual,
            "size": os.path.getsize(local), "verified": bool(expected_hash)}


def push(request: dict[str, Any]) -> dict[str, Any]:
    """Copy one file from this machine to a host, and prove it arrived intact."""
    host = parse_host(request.get("host"))
    local = str(request.get("local_path") or "").strip()
    remote = str(request.get("remote_path") or "").strip()
    if not remote or not local:
        raise FileTransferError("local_path and remote_path are both required.")
    if not Path(local).exists():
        raise FileTransferError(f"local_path does not exist: {local}")

    expected = _sha256_local(local)
    client = open_client(host)
    try:
        sftp = client.open_sftp()
        try:
            # The destination directory has to exist; SFTP will not make it, and the failure it
            # gives ("No such file") names the file rather than the directory that is missing.
            parent = remote.rsplit("\\", 1)[0] if host.runtime == WINDOWS else remote.rsplit("/", 1)[0]
            if parent and parent != remote:
                _ensure_dir(host, parent)
            sftp.put(local, remote)
        finally:
            sftp.close()
    finally:
        client.close()

    landed = run(host, _hash_command(host, remote))["stdout"].strip().split()
    _verify(expected, landed[0] if landed else "", where=f"pushing to {remote}")
    return {"local_path": local, "remote_path": remote, "sha256": expected,
            "size": os.path.getsize(local), "verified": bool(landed)}


def _ensure_dir(host: Host, directory: str) -> None:
    if host.runtime == WINDOWS:
        run(host, f"New-Item -ItemType Directory -Force -Path '{directory}' | Out-Null")
    else:
        run(host, f"mkdir -p {shlex.quote(directory)}")
