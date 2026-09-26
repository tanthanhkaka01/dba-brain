"""SMB - list, fetch and delete files on a Windows share, and register the login Windows uses for it.

Until 0.24.0 the SQL Server restore reached its backup shares itself: ``smbclient`` on a Linux
worker, a local PowerShell ``Get-ChildItem`` / ``Remove-Item`` over a UNC path on a Windows master,
``cmdkey`` to store the login first. Each app that met a share would have grown its own way, and the
backup app already had three. The operator's rule (R10): an app does not reach a host itself - it
asks ``common.cli``. So the share is reached here, one way per platform, and the app keeps what is
its own - which files, which window, what counts as obsolete.

**One answer shape whatever reached the share.** ``smbclient`` where there is no UNC (a Linux node),
the UNC path itself where there is (Windows, after ``cmdkey``). A file is ``{"path", "name",
"size_bytes", "modified_epoch"}`` with ``path`` backslash-separated and relative to the directory
listed, which is the layout the restore stages into.

**Every value is in the request** (R09): the host, the share, the login and its password. The
password never reaches a command line - ``smbclient`` reads it from an auth file this module writes
0600 and deletes, and every request arrives on stdin (R14). ``cmdkey`` is the exception Windows
leaves no way around: ``/pass:`` is its only form, as it was when the app ran it.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

__all__ = ["SmbError", "delete", "get", "listing", "register_credential"]

#: A listing of one directory tree - not a transfer.
LIST_TIMEOUT_SECONDS = 600
#: One file: a full backup of a large database is many GB, so this is generous and still bounded.
GET_TIMEOUT_SECONDS = 3600

BACKENDS = ("smbclient", "unc")


class SmbError(ValueError):
    """The request cannot be run as written, or the share refused it."""


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
def _share(request: dict[str, Any]) -> tuple[str, str]:
    host = str(request.get("host") or "").strip()
    share = str(request.get("share") or "").strip().strip("\\/")
    if not host or not share:
        raise SmbError('needs "host" and "share" - the share is //host/share; common.cli reads no '
                       "configuration (rules R09), so the caller states both.")
    return host, share


def _login(request: dict[str, Any]) -> tuple[str, str, str]:
    """``(domain, username, password)``; a ``DOMAIN\\user`` username is split, as Windows writes it."""
    username = str(request.get("username") or "")
    domain = str(request.get("domain") or "")
    for separator in ("\\", "/"):
        if separator in username and not domain:
            domain, username = username.split(separator, 1)
            break
    return domain, username, str(request.get("password") or "")


def _backend(request: dict[str, Any]) -> str:
    """Where this node can reach a share from: its own UNC paths on Windows, ``smbclient`` elsewhere."""
    backend = str(request.get("backend") or ("unc" if os.name == "nt" else "smbclient")).strip().lower()
    if backend not in BACKENDS:
        raise SmbError(f'"backend" must be one of {", ".join(BACKENDS)}; got {backend!r}.')
    return backend


def _timeout(request: dict[str, Any], default: int) -> int:
    try:
        return max(1, int(request.get("timeout_seconds") or default))
    except (TypeError, ValueError) as exc:
        raise SmbError(f'"timeout_seconds" must be a number; got {request.get("timeout_seconds")!r}.') from exc


def _subpath(value: Any) -> str:
    """A path under the share, backslash-separated, no leading or trailing separator."""
    return str(value or "").replace("/", "\\").strip("\\")


def _unc(host: str, share: str, subpath: str = "") -> Path:
    return Path(f"\\\\{host}\\{share}" + (f"\\{subpath}" if subpath else ""))


# --------------------------------------------------------------------------- #
# smbclient
# --------------------------------------------------------------------------- #
def _auth_file(request: dict[str, Any]) -> Path:
    """The login as an ``smbclient -A`` file, readable by this user only, so no password is an arg."""
    domain, username, password = _login(request)
    handle, name = tempfile.mkstemp(suffix=".smbauth")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(f"username = {username}\npassword = {password}\n")
        if domain:
            stream.write(f"domain = {domain}\n")
    os.chmod(name, 0o600)
    return Path(name)


def _smbclient(request: dict[str, Any], commands: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    """One ``smbclient`` session running ``commands``; killed as a process group past the timeout.

    The group, not the process: a hung transfer's children kept the pipes open, and ``communicate``
    then waited on them for ever after the timeout had fired.
    """
    host, share = _share(request)
    auth = _auth_file(request)
    args = ["smbclient", f"//{host}/{share}", "-A", str(auth), "-c", "; ".join(commands)]
    new_session = os.name != "nt"
    try:
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       start_new_session=new_session)
        except FileNotFoundError as exc:
            raise SmbError("smbclient is not installed on this node - install samba-client (smbclient), "
                           "or reach the share from Windows, where the UNC path is used instead.") from exc
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL) if new_session else process.kill()
            except OSError:
                process.kill()
            process.communicate()
            raise SmbError(f"smbclient timed out after {timeout_seconds}s on //{host}/{share}") from exc
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    finally:
        auth.unlink(missing_ok=True)


#: One entry of ``smbclient ls``: ``  NAME   A   11121102848  Wed Jun 24 01:01:11 2026``.
_ENTRY = re.compile(r"\s+(\S+)\s+([AHSRDN]+)\s+(\d+)\s+(\w{3}\s+\w{3}\s+\d+\s+\d+:\d+:\d+\s+\d{4})")


def parse_ls(output: str, *, subpath: str) -> list[dict[str, Any]]:
    """The files of a recursive ``smbclient ls``, relative to ``subpath``.

    ``smbclient`` prints each directory as a header line with backslashes (``\\a\\b\\<db>\\FULL``)
    and its entries under it. The prefix is what makes a path *relative*: when a multi-segment
    sub-path was compared unnormalized, every file kept its full sub-path and staged several
    directories too deep, where the restore does not look, with no error anywhere.
    """
    normalized = _subpath(subpath)
    prefix = f"\\{normalized}\\" if normalized else "\\"
    current = ""
    files: list[dict[str, Any]] = []
    for line in output.splitlines():
        if line.startswith("\\"):
            header = line.rstrip()
            current = header[len(prefix):] if header.startswith(prefix) else header.lstrip("\\")
            continue
        match = _ENTRY.match(line)
        if not match or "D" in match.group(2):
            continue
        name = match.group(1)
        try:
            # smbclient prints the time in this node's local zone.
            modified = time.mktime(time.strptime(" ".join(match.group(4).split()), "%a %b %d %H:%M:%S %Y"))
        except (ValueError, OverflowError):
            modified = None
        files.append({"path": f"{current}\\{name}" if current else name, "name": name,
                      "size_bytes": int(match.group(3)), "modified_epoch": modified})
    return files


# --------------------------------------------------------------------------- #
# The operations
# --------------------------------------------------------------------------- #
def listing(request: dict[str, Any]) -> dict[str, Any]:
    """Every file under ``path`` on the share - recursively unless ``recurse`` is false."""
    host, share = _share(request)
    subpath = _subpath(request.get("path"))
    recurse = request.get("recurse", True) is not False
    suffixes = tuple(str(item).lower() for item in (request.get("suffixes") or []))
    timeout = _timeout(request, LIST_TIMEOUT_SECONDS)
    backend = _backend(request)
    if backend == "smbclient":
        commands = (["recurse ON"] if recurse else []) + ["prompt OFF"]
        if subpath:
            commands.append(f'cd "{subpath}"')
        commands.append("ls")
        done = _smbclient(request, commands, timeout_seconds=timeout)
        if done.returncode != 0:
            raise SmbError(f"smbclient could not list //{host}/{share}/{subpath}: "
                           f"{(done.stderr or done.stdout).strip() or f'exit code {done.returncode}'}")
        files = parse_ls(done.stdout, subpath=subpath)
    else:
        _register_if_given(request, host)
        root = _unc(host, share, subpath)
        if not root.exists():
            raise SmbError(f"{root} cannot be reached - the share is down, the path is wrong, or this "
                           "login has no access to it.")
        files = []
        walker = os.walk(root) if recurse else [(str(root), [], [e.name for e in os.scandir(root) if e.is_file()])]
        for folder, _dirs, names in walker:
            for name in names:
                full = Path(folder) / name
                try:
                    stats = full.stat()
                except OSError:
                    continue
                files.append({"path": str(full.relative_to(root)), "name": name,
                              "size_bytes": int(stats.st_size), "modified_epoch": float(stats.st_mtime)})
    if suffixes:
        files = [item for item in files if item["name"].lower().endswith(suffixes)]
    return {"backend": backend, "host": host, "share": share, "path": subpath, "files": files,
            "count": len(files)}


def get(request: dict[str, Any]) -> dict[str, Any]:
    """One file from the share to ``local_path`` on this node.

    A non-zero ``smbclient`` exit is an answer, not a failure: with a file still being written the
    exit code says nothing reliable, so the caller compares ``bytes`` with the size it listed.
    """
    host, share = _share(request)
    remote = _subpath(request.get("remote_path"))
    local = str(request.get("local_path") or "").strip()
    if not remote or not local:
        raise SmbError('needs "remote_path" (under the share) and "local_path" (the file to write here).')
    target = Path(local)
    target.parent.mkdir(parents=True, exist_ok=True)
    timeout = _timeout(request, GET_TIMEOUT_SECONDS)
    backend = _backend(request)
    exit_code, detail = 0, ""
    if backend == "smbclient":
        done = _smbclient(request, ["prompt OFF", f"lcd {target.parent}", f'get "{remote}" "{target.name}"'],
                          timeout_seconds=timeout)
        exit_code, detail = done.returncode, (done.stderr or "").strip()
    else:
        _register_if_given(request, host)
        try:
            shutil.copyfile(_unc(host, share, remote), target)
        except OSError as exc:
            exit_code, detail = 1, str(exc)
    size = target.stat().st_size if target.exists() else None
    return {"backend": backend, "remote_path": remote, "local_path": str(target), "bytes": size,
            "exit_code": exit_code, "detail": detail[:2000]}


def delete(request: dict[str, Any]) -> dict[str, Any]:
    """Delete exactly the files ``paths`` names, each under the share - never a pattern, never a
    folder. What to delete is the caller's decision (an age, a chain); this only carries it out."""
    host, share = _share(request)
    if not isinstance(request.get("paths"), list):
        raise SmbError('needs "paths": the files to delete, each relative to the share.')
    paths = [_subpath(item) for item in request["paths"] if _subpath(item)]
    timeout = _timeout(request, LIST_TIMEOUT_SECONDS)
    backend = _backend(request)
    results: list[dict[str, Any]] = []
    if backend == "unc":
        _register_if_given(request, host)
    for path in paths:
        if backend == "smbclient":
            done = _smbclient(request, [f'del "{path}"'], timeout_seconds=timeout)
            failed = done.returncode != 0 or "NT_STATUS" in (done.stdout + done.stderr)
            results.append({"path": path, "status": "FAILED" if failed else "DELETED", "bytes": None,
                            "error": (done.stderr or done.stdout).strip()[:500] if failed else ""})
            continue
        full = _unc(host, share, path)
        try:
            size = full.stat().st_size
            full.unlink()
            results.append({"path": path, "status": "DELETED", "bytes": int(size), "error": ""})
        except OSError as exc:
            results.append({"path": path, "status": "FAILED", "bytes": None, "error": str(exc)[:500]})
    return {"backend": backend, "host": host, "share": share, "results": results,
            "deleted": sum(1 for item in results if item["status"] == "DELETED"),
            "failed": sum(1 for item in results if item["status"] == "FAILED")}


def register_credential(request: dict[str, Any]) -> dict[str, Any]:
    """Store a login in the Windows credential manager, so the UNC paths to ``target`` open.

    Windows only: a Linux node reaches a share through ``smbclient``, which takes the login with
    every call, so there is nothing to register there - said, not treated as a failure.
    """
    target = str(request.get("target") or "").strip()
    domain, username, password = _login(request)
    if not target or not username or not password:
        raise SmbError('needs "target" (the host), "username" and "password".')
    if os.name != "nt":
        return {"registered": False, "target": target,
                "reason": "not Windows - smbclient takes the login with each call"}
    user = f"{domain}\\{username}" if domain else username
    done = subprocess.run(["cmdkey", f"/add:{target}", f"/user:{user}", f"/pass:{password}"],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SmbError(f"cmdkey could not store the login for {target}: "
                       f"{(done.stderr or done.stdout).strip() or f'exit code {done.returncode}'}")
    return {"registered": True, "target": target, "reason": ""}


def _register_if_given(request: dict[str, Any], host: str) -> None:
    """A UNC path opens with whatever Windows holds for the host; a login in the request is stored first."""
    _domain, username, password = _login(request)
    if username and password:
        register_credential({**request, "target": host})
