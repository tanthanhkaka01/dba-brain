"""Every entry under a folder, with its kind, bytes and time - and no hash (0.37.0, ``list-files``).

The repository check (the operator, 2026-11-10: *a file in /mnt/nas/<server_id> that is not in the
backup tables must fail the backup run and be reported to its group*) reads a server's whole
repository folder on every offload pass. ``file-checksum``'s folder form would hash every byte there -
terabytes, every pass. What the check needs is cheaper and is exactly this: which entries are there,
of what kind, how many bytes. The hashes were taken when each file was proven; a file whose bytes
changed since is the catalog's scrub's to find.

**Links and other non-files are listed, not skipped.** A link in a repository folder is a finding in
itself - it can make a folder look full of a backup that lives elsewhere - so ``find`` lists every
entry that is not a folder, and the answer says which kind each one is.

One command per call: GNU ``find -printf`` on a POSIX host (each entry ``NUL``-terminated, so a name
holding a newline is still one entry), ``Get-ChildItem -Recurse -Force`` on Windows (PowerShell 2.0
has no ``-File``: folders are left out by ``PSIsContainer``, reparse points kept and called links),
``os.walk`` with ``lstat`` here.
"""

from __future__ import annotations

import os
import shlex
import stat as stat_module
from datetime import datetime, timezone
from typing import Any

from dbabrain.common.hostcmd import WINDOWS, Host, HostCommandError, parse_host, run
from dbabrain.lib import errors
from dbabrain.lib.powershell import quote_powershell

FIELDS = frozenset({"host", "folder", "max_files", "timeout_seconds", "with_folders"})
#: A repository folder holds a server's backups for as long as they are kept: tens of thousands of
#: WAL segments is ordinary. More than this is answered as truncated, never cut silently.
DEFAULT_MAX_FILES = 200000
MAX_MAX_FILES = 2000000
DEFAULT_TIMEOUT_SECONDS = 600

FILE, LINK, OTHER = "file", "link", "other"
_POSIX_KIND = {"f": FILE, "l": LINK}
#: A folder's kind in a listing made ``with_folders`` - parted from the entries before the answer.
_FOLDER = "folder"


class ListFilesError(errors.OperationFailed):
    """The folder could not be listed."""


def list_files(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ListFilesError("list-files takes one JSON object.")
    unknown = sorted(set(request) - FIELDS)
    if unknown:
        raise ListFilesError(f"list-files: unknown field(s) {', '.join(unknown)}; it takes {', '.join(sorted(FIELDS))}.")
    host = parse_host(request.get("host")) if request.get("host") else None
    folder = request.get("folder")
    if not isinstance(folder, str) or not folder.strip() or folder != folder.strip():
        raise ListFilesError("folder is required: the absolute folder to list.")
    windows = host is not None and host.runtime == WINDOWS
    if not (folder.startswith("/") or (len(folder) > 2 and folder[1] == ":") or folder.startswith("\\\\")):
        raise ListFilesError(f"folder must be absolute: {folder!r}.")
    if any(part == ".." for part in folder.replace("\\", "/").split("/")):
        raise ListFilesError(f"folder must not contain '..': {folder!r}.")
    max_files = _whole(request.get("max_files"), "max_files", DEFAULT_MAX_FILES, 1, MAX_MAX_FILES)
    timeout = _whole(request.get("timeout_seconds"), "timeout_seconds", DEFAULT_TIMEOUT_SECONDS, 10, 86400)
    with_folders = request.get("with_folders", False)
    if not isinstance(with_folders, bool):
        raise ListFilesError(f"with_folders must be true or false; got {with_folders!r}.")

    if host is None or host.is_local:
        entries = _here(folder, with_folders=with_folders)
    elif windows:
        entries = _on_windows(host, folder, timeout=timeout, with_folders=with_folders)
    else:
        entries = _on_posix(host, folder, timeout=timeout, with_folders=with_folders)
    if entries is None:
        return {"folder": folder, "folder_found": False, "files": [], "listed": 0, "truncated": False,
                **({"folders": []} if with_folders else {})}
    # The folders are their own list (0.38.0): `files` stays what it was - every entry that is not
    # a folder - so the repository check reads the same answer, and a staging cleanup can name the
    # folders an emptied backup set left behind.
    folders = sorted(entry["path"] for entry in entries if entry["type"] == _FOLDER)
    entries = [entry for entry in entries if entry["type"] != _FOLDER]
    entries.sort(key=lambda entry: entry["path"])
    return {"folder": folder, "folder_found": True, "files": entries[:max_files], "listed": len(entries),
            "truncated": len(entries) > max_files, **({"folders": folders} if with_folders else {})}


def _whole(value: Any, field: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ListFilesError(f"{field} must be a whole number from {low} to {high}; got {value!r}.")
    return value


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _here(folder: str, *, with_folders: bool = False) -> list[dict[str, Any]] | None:
    if not os.path.isdir(folder) or os.path.islink(folder):
        return None
    found: list[dict[str, Any]] = []
    for root, dirs, names in os.walk(folder):
        # A linked folder is an entry (a link), never walked into.
        for name in list(dirs):
            if os.path.islink(os.path.join(root, name)):
                dirs.remove(name)
                names.append(name)
            elif with_folders:
                path = os.path.join(root, name)
                found.append({"path": path, "type": _FOLDER, "size_bytes": 0,
                              "mtime_utc": _utc(os.lstat(path).st_mtime)})
        for name in names:
            path = os.path.join(root, name)
            info = os.lstat(path)
            kind = LINK if stat_module.S_ISLNK(info.st_mode) else FILE if stat_module.S_ISREG(info.st_mode) else OTHER
            found.append({"path": path, "type": kind, "size_bytes": int(info.st_size), "mtime_utc": _utc(info.st_mtime)})
    return found


def posix_command(folder: str, *, with_folders: bool = False) -> str:
    """``find`` every entry that is not a folder: kind, bytes, time, path - each ending in NUL.
    ``with_folders``: the folders too (kind ``d``)."""
    f = shlex.quote(folder.rstrip("/") or "/")
    only = "" if with_folders else "! -type d "
    return (f"if [ -d {f} ] && [ ! -L {f} ]; then "
            f"find {f} -mindepth 1 {only}-printf '%y\\t%s\\t%T@\\t%p\\0'; "
            "else echo NOFOLDER; fi")


def _on_posix(host: Host, folder: str, *, timeout: int, with_folders: bool = False) -> list[dict[str, Any]] | None:
    try:
        result = run(host, posix_command(folder, with_folders=with_folders), timeout=timeout)
    except HostCommandError as exc:
        raise ListFilesError(f"{folder}: the host could not be reached: {exc}") from exc
    out = result.get("stdout") or ""
    if out.strip() == "NOFOLDER":
        return None
    if int(result.get("exit_code") or 0) != 0:
        # find says so when part of the tree cannot be read: a listing with a hole in it is not a listing.
        raise ListFilesError(f"listing {folder} failed (exit {result.get('exit_code')}): "
                             f"{(result.get('stderr') or '').strip()[-400:]}")
    entries = []
    for record in out.split("\0"):
        if not record.strip("\n"):
            continue
        parts = record.lstrip("\n").split("\t", 3)
        if len(parts) != 4 or not parts[1].isdigit():
            raise ListFilesError(f"listing {folder}: an entry this command cannot read: {record[:200]!r}")
        kind, size, mtime, path = parts
        entries.append({"path": path, "type": _FOLDER if kind == "d" else _POSIX_KIND.get(kind, OTHER),
                        "size_bytes": int(size), "mtime_utc": _utc(float(mtime))})
    return entries


def windows_script(folder: str, *, with_folders: bool = False) -> str:
    folders = ("  if ($_.PSIsContainer -and -not $link) { 'ENTRY|d|0|' + "
               "[long](($_.LastWriteTimeUtc - $epoch).TotalSeconds) + '|' + $_.FullName; return }\n"
               if with_folders else
               "  if ($_.PSIsContainer -and -not $link) { return }\n")
    return (f"$root = {quote_powershell(folder)}\n"
            "if (-not (Test-Path -LiteralPath $root -PathType Container)) { 'NOFOLDER'; exit 0 }\n"
            "$epoch = New-Object DateTime 1970,1,1,0,0,0,([DateTimeKind]::Utc)\n"
            "Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction Stop | ForEach-Object {\n"
            "  $link = ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0\n"
            + folders +
            "  $kind = if ($link) { 'l' } else { 'f' }\n"
            "  $len = if ($_.PSIsContainer) { 0 } else { $_.Length }\n"
            "  $t = [long](($_.LastWriteTimeUtc - $epoch).TotalSeconds)\n"
            "  'ENTRY|' + $kind + '|' + $len + '|' + $t + '|' + $_.FullName\n"
            "}\n")


def _on_windows(host: Host, folder: str, *, timeout: int, with_folders: bool = False) -> list[dict[str, Any]] | None:
    try:
        result = run(host, windows_script(folder, with_folders=with_folders), timeout=timeout)
    except HostCommandError as exc:
        raise ListFilesError(f"{folder}: the host could not be reached: {exc}") from exc
    lines = [line.rstrip("\r") for line in (result.get("stdout") or "").splitlines()]
    if "NOFOLDER" in lines:
        return None
    if int(result.get("exit_code") or 0) != 0:
        raise ListFilesError(f"listing {folder} failed (exit {result.get('exit_code')}): "
                             f"{(result.get('stderr') or '').strip()[-400:]}")
    entries = []
    for line in lines:
        if not line.startswith("ENTRY|"):
            continue
        _tag, kind, size, mtime, path = line.split("|", 4)
        entries.append({"path": path, "type": _FOLDER if kind == "d" else _POSIX_KIND.get(kind, OTHER),
                        "size_bytes": int(size), "mtime_utc": _utc(float(mtime))})
    return entries
