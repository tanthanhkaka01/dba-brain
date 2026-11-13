"""Hash files on the host that holds them - ``common.cli file-checksum``.

The one way this tool takes a file's sha256 (0.34.0, 1.210; rules R43). Before it there were two:
``filetransfer._hash_command`` for pack/pull/push and ``file_transfer._SHA256_COMMAND`` for
pack-files, each with its own idea of the Windows command, and a backup catalog would have been a
third. Both now build their command here.

**The hash runs where the file is.** Streaming a 60 GB FULL to this process to hash it costs the
same bytes as copying it, which is the cost a checksum before and after a copy must not double. So
a batch of paths goes to the host as one command and comes back as one line per file.

**Windows hashes through .NET, not ``Get-FileHash``.** ``Get-FileHash`` arrived in PowerShell 4.0;
Windows Server 2008 R2 ships 2.0, and this estate still has 2008 R2 hosts. The same script works on
every version, so there is one Windows path, not a fallback.

A missing file is an answer, not an error: it is listed under ``missing``, and the caller - a
restore gate, the scrub - decides what a missing backup means.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from datetime import datetime, timezone
from typing import Any

from dbabrain.common.hostcmd import WINDOWS, Host, HostCommandError, open_session, parse_host, run
from dbabrain.lib import errors

# The algorithm list is the catalog's (lib.integrity), one definition.
from dbabrain.lib.integrity import ALGORITHMS, SHA256  # noqa: E402,F401 - re-exported

#: The longest command one batch may build. Windows is the tight one: the script travels as
#: UTF-16 base64 in ``-EncodedCommand`` (about 2.7 characters per character) and cmd.exe stops at
#: 8191, so a batch is cut well under that; Linux allows far more, but a bound keeps one call's
#: stdout and one failure's blast radius small.
MAX_COMMAND_CHARS = {WINDOWS: 2400, "posix": 60000}

#: Seconds one batch may take. A 60 GB FULL hashes at disk speed - minutes, not seconds. Named for
#: what it limits: a verified delete hashes the file first and takes this same limit.
HASH_TIMEOUT_SECONDS = 3600
#: The most files one ``folder`` request hashes; more is said (``truncated``), never silently cut.
DEFAULT_MAX_FILES = 5000


class FileChecksumError(errors.OperationFailed):
    """The files could not be hashed: no host, a host that refused, an answer that could not be read."""


def _utc(epoch: float | int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ps_literal(path: str) -> str:
    # A PowerShell single-quoted string: only the quote itself needs escaping, by doubling it.
    return "'" + path.replace("'", "''") + "'"


def windows_script(paths: list[str]) -> str:
    """The PowerShell that hashes ``paths``: one tab-separated line per path, ``OK`` or ``MISSING``."""
    listed = ",".join(_ps_literal(p) for p in paths)
    return (
        "$sha=[System.Security.Cryptography.SHA256]::Create();"
        "$epoch=New-Object DateTime 1970,1,1,0,0,0,([DateTimeKind]::Utc);"
        f"foreach($p in @({listed})){{"
        "if(Test-Path -LiteralPath $p -PathType Leaf){"
        "$i=Get-Item -LiteralPath $p;$s=[System.IO.File]::OpenRead($i.FullName);"
        "try{$h=[BitConverter]::ToString($sha.ComputeHash($s)).Replace('-','').ToLower()}"
        "finally{$s.Close()};"
        "$t=[int64]($i.LastWriteTimeUtc-$epoch).TotalSeconds;"
        "\"OK`t$h`t$($i.Length)`t$t`t$p\"}"
        "else{\"MISSING`t`t`t`t$p\"}}"
    )


def _posix_arg(path: str) -> str:
    # A name starting with "-" would be read as an option; "./" keeps it a name without `--`.
    return shlex.quote(path if not path.startswith("-") else f"./{path}")


def posix_commands(paths: list[str]) -> tuple[str, str]:
    """``sha256sum`` and ``stat`` over ``paths`` - two commands, each reporting what it could read."""
    quoted = " ".join(_posix_arg(p) for p in paths)
    return f"sha256sum {quoted}", f"stat -c '%s %Y %n' {quoted}"


def hash_command(shell: str, path: str) -> str:
    """The command that prints one file's sha256 on ``shell`` (``powershell`` or ``bash``).

    For the callers that hash a single file inside a session they already hold (pack, pull, push,
    pack-files); its output's first word is the hash on both shells.
    """
    if shell == "powershell":
        # `foreach` is a statement and cannot be piped; a script block run with `&` can.
        return "&{" + windows_script([path]) + "}|ForEach-Object{($_ -split \"`t\")[1]}"
    return f"sha256sum {_posix_arg(path)}"


def _batches(paths: list[str], *, windows: bool) -> list[list[str]]:
    budget = MAX_COMMAND_CHARS[WINDOWS if windows else "posix"]
    batches: list[list[str]] = []
    current: list[str] = []
    used = 0
    for path in paths:
        cost = len(path) + 4
        if current and used + cost > budget:
            batches.append(current)
            current, used = [], 0
        current.append(path)
        used += cost
    if current:
        batches.append(current)
    return batches


def _unescape_sha256sum(name: str) -> str:
    # sha256sum prefixes a line with "\" when the name holds a backslash or a newline, and escapes
    # those two; every other name is printed as given. Read left to right: two replace() calls in
    # either order turn the `\\n` of a name like `odd\name` into a newline.
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), name)


def _parse_posix(paths: list[str], hashed: str, stated: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for line in hashed.splitlines():
        escaped = line.startswith("\\")
        body = line[1:] if escaped else line
        digest, sep, name = body.partition("  ")
        if not sep or len(digest) != 64:
            continue
        name = _unescape_sha256sum(name) if escaped else name
        found[name] = {"path": name, "sha256": digest.lower(), "size_bytes": None, "mtime_utc": None}
    for line in stated.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[2] in found:
            found[parts[2]]["size_bytes"] = int(parts[0])
            found[parts[2]]["mtime_utc"] = _utc(int(parts[1])) if parts[1].isdigit() else None
    answered: dict[str, dict[str, Any]] = {}
    for path in paths:
        item = found.get(path) or found.get(f"./{path}")
        if item:
            answered[path] = {**item, "path": path}
    return answered


def _parse_windows(paths: list[str], out: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        parts = line.rstrip("\r").split("\t", 4)
        if len(parts) == 5 and parts[0] == "OK" and len(parts[1]) == 64:
            found[parts[4]] = {
                "path": parts[4], "sha256": parts[1].lower(),
                "size_bytes": int(parts[2]) if parts[2].isdigit() else None,
                "mtime_utc": _utc(int(parts[3])) if parts[3].lstrip("-").isdigit() else None,
            }
    return {path: found[path] for path in paths if path in found}


def _hash_here(paths: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not os.path.isfile(path):
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        stat = os.stat(path)
        found[path] = {"path": path, "sha256": digest.hexdigest(), "size_bytes": stat.st_size,
                       "mtime_utc": _utc(stat.st_mtime)}
    return found


def _hash_on(host: Host, paths: list[str], *, timeout: int) -> dict[str, dict[str, Any]]:
    windows = host.runtime == WINDOWS
    found: dict[str, dict[str, Any]] = {}
    session = open_session(host)
    try:
        for batch in _batches(paths, windows=windows):
            if windows:
                result = run(host, windows_script(batch), timeout=timeout, session=session)
                got = _parse_windows(batch, result["stdout"] or "")
                found.update(got)
                # Every path answers OK or MISSING; a batch that answered nothing did not run.
                if not got and "MISSING" not in (result["stdout"] or "") and result["exit_code"] != 0:
                    raise FileChecksumError(
                        f"hashing on {host.host} failed (exit {result['exit_code']}): "
                        f"{(result['stderr'] or result['stdout'] or '').strip()[-400:]}")
                continue
            hash_cmd, stat_cmd = posix_commands(batch)
            hashed = run(host, hash_cmd, timeout=timeout, session=session)
            stated = run(host, stat_cmd, timeout=timeout, session=session)
            # A non-zero exit with nothing read means the command itself did not run (no
            # sha256sum, a dead container); with some read it means some files are missing.
            if hashed["exit_code"] != 0 and not (hashed["stdout"] or "").strip():
                stderr = (hashed["stderr"] or "").strip()
                if "No such file" not in stderr:
                    raise FileChecksumError(
                        f"hashing on {host.host} failed (exit {hashed['exit_code']}): {stderr[-400:]}")
            found.update(_parse_posix(batch, hashed["stdout"] or "", stated["stdout"] or ""))
    finally:
        close = getattr(session, "close", None)
        if close:
            close()
    return found


def _list_folder(host: Host | None, folder: str, *, timeout: int) -> list[str] | None:
    """Every file under ``folder`` where it is - ``None`` when the folder is not there.

    One command: ``find`` on a POSIX host, ``Get-ChildItem -Recurse`` on Windows (PowerShell 2.0 has
    no ``-File``, so folders are left out by ``PSIsContainer``)."""
    if host is None or host.is_local:
        if not os.path.isdir(folder):
            return None
        return [os.path.join(root, name) for root, _dirs, names in os.walk(folder) for name in names]
    if host.runtime == WINDOWS:
        script = (f"$root = {_ps_literal(folder)}\n"
                  "if (-not (Test-Path -LiteralPath $root -PathType Container)) { 'NOFOLDER'; exit 0 }\n"
                  "Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue |"
                  " Where-Object { -not $_.PSIsContainer } | ForEach-Object { 'FILE|' + $_.FullName }\n")
        out = run(host, script, timeout=timeout)["stdout"] or ""
        lines = [line.rstrip("\r") for line in out.splitlines()]
        if "NOFOLDER" in lines:
            return None
        return [line[5:] for line in lines if line.startswith("FILE|")]
    quoted = _posix_arg(folder)
    result = run(host, f"if [ -d {quoted} ]; then find {quoted} -type f -print; else echo NOFOLDER; fi",
                 timeout=timeout)
    lines = [line for line in (result["stdout"] or "").splitlines() if line]
    if lines[:1] == ["NOFOLDER"]:
        return None
    if result["exit_code"] != 0 and not lines:
        raise FileChecksumError(f"listing {folder} on {host.host} failed (exit {result['exit_code']}): "
                                f"{(result['stderr'] or '').strip()[-400:]}")
    return lines


def _folder_on_the_host(host: Host | None, folder: str, container: str) -> str:
    """The folder as the host sees it, for a folder inside a container - through its mounts, the
    translation the PostgreSQL restore step already asks docker for."""
    if not container:
        return folder
    if host is None or host.is_local:
        raise FileChecksumError("container needs a host: the folder is translated through its mounts there.")
    from dbabrain.common.restorestep import RestoreStepError
    from dbabrain.common.restorestep.postgresql import _container_mounts, _to_host_path

    try:
        return _to_host_path(_container_mounts(host, container, sudo=host.sudo), container, folder)
    except RestoreStepError as exc:
        raise FileChecksumError(str(exc)) from exc


def _folder_paths(request: dict[str, Any], host: Host | None, *, timeout: int) -> tuple[list[str], dict[str, Any]]:
    """The ``folder`` form: the files under it whose name matches ``include``, and what was listed."""
    import fnmatch

    folder = str(request.get("folder") or "").strip()
    include = request.get("include") or ["*"]
    if not isinstance(include, list) or not all(isinstance(item, str) and item for item in include):
        raise FileChecksumError('include must be a list of file name patterns, e.g. ["*.bak", "*.trn"].')
    try:
        max_files = int(request.get("max_files") or DEFAULT_MAX_FILES)
    except (TypeError, ValueError) as exc:
        raise FileChecksumError(f"max_files must be a whole number; got {request.get('max_files')!r}.") from exc
    if max_files <= 0:
        raise FileChecksumError("max_files must be greater than zero.")
    container = str(request.get("container") or "").strip()
    host_folder = _folder_on_the_host(host, folder, container)
    listed = _list_folder(host, host_folder, timeout=timeout)
    patterns = [item.lower() for item in include]

    def name_of(path: str) -> str:
        return path.replace("\\", "/").rsplit("/", 1)[-1].lower()

    names = sorted(path for path in (listed or [])
                   if any(fnmatch.fnmatchcase(name_of(path), pattern) for pattern in patterns))
    return names[:max_files], {"folder": folder, "host_folder": host_folder, "folder_found": listed is not None,
                               "listed": len(names), "truncated": len(names) > max_files}


def checksum(request: dict[str, Any]) -> dict[str, Any]:
    """Hash ``paths`` on ``host`` (or here, with no host) and say which could not be read.

    Request::

        {"host": {...},                 // hostcmd's block; omit to hash on this machine
         "paths": ["/opt/backup/APPDB_FULL_20261029.bak", ...],
         "algorithm": "sha256",         // the only one; stated so a later one is a choice
         "timeout_seconds": 3600}

    Or, instead of ``paths``, every file under a folder (0.34.0, 1.215 - a catalog backfill)::

        {"host": {...}, "folder": "/var/opt/mssql/backup", "include": ["*.bak", "*.trn"],
         "container": "mssql",          // the folder is inside it: read through its mounts on the host
         "max_files": 5000}

    Answer: ``files`` (``path``, ``sha256``, ``size_bytes``, ``mtime_utc``) in the order asked,
    ``missing`` (the paths that are not a file there), ``file_count``, ``bytes_total``; with a
    folder also ``folder``, ``host_folder`` (the paths in ``files`` are under it), ``folder_found``,
    ``listed`` and ``truncated``.
    """
    algorithm = str(request.get("algorithm") or SHA256).strip().lower()
    if algorithm not in ALGORITHMS:
        raise FileChecksumError(f"algorithm must be one of {', '.join(ALGORITHMS)}; got {algorithm!r}.")
    timeout = int(request.get("timeout_seconds") or HASH_TIMEOUT_SECONDS)
    raw_host = request.get("host")
    host = parse_host(raw_host) if raw_host else None
    raw_paths = request.get("paths")
    listing: dict[str, Any] = {}
    if request.get("folder") not in (None, ""):
        if raw_paths:
            raise FileChecksumError("give paths or folder, not both.")
        try:
            paths, listing = _folder_paths(request, host, timeout=timeout)
        except HostCommandError as exc:
            raise FileChecksumError(str(exc)) from exc
    else:
        if not isinstance(raw_paths, list) or not raw_paths:
            raise FileChecksumError("paths is required: a non-empty list of files to hash (or a folder).")
        paths = []
        for item in raw_paths:
            text = str(item or "").strip()
            if text and text not in paths:
                paths.append(text)
        if not paths:
            raise FileChecksumError("paths holds no file name.")

    try:
        if not paths:
            found = {}
        elif host and not host.is_local:
            found = _hash_on(host, paths, timeout=timeout)
        else:
            found = _hash_here(paths)
    except HostCommandError as exc:
        raise FileChecksumError(str(exc)) from exc

    files = [found[path] for path in paths if path in found]
    return {
        **listing,
        "algorithm": algorithm,
        "host": host.host if host and not host.is_local else "local",
        "files": files,
        "missing": [path for path in paths if path not in found],
        "file_count": len(files),
        "bytes_total": sum(int(item.get("size_bytes") or 0) for item in files),
    }
