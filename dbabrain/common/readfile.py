"""Read ONE small file's bytes on the host that holds it - ``common.cli read-file`` (0.37.0).

For a backup certificate pair (``.cer`` / ``.pvk``) the vault takes into dbabrain before the server's
copy is deleted. It runs where :mod:`dbabrain.common.hostcmd` reaches - a Windows host (PowerShell,
locally, over SSH or WinRM), Linux, a container - so a pair beside a containerised SQL Server is read
inside the container, where the backup script wrote it.

**Proven on arrival.** The host prints the file's size and sha256 beside its bytes (base64); the
bytes are decoded here and held to both. A file read short, or changed on the way, is an error - a
certificate kept on the word of a transfer that did not check would be the one copy left.

**Small files only** (``max_bytes``, default 1 MiB, at most 16 MiB): the bytes travel inside one
answer. A backup is never read this way - ``copy-files`` streams those.
"""

from __future__ import annotations

import base64
import hashlib
import re
import shlex
from typing import Any

from dbabrain.common.hostcmd import WINDOWS, Host, HostCommandError, parse_host, run
from dbabrain.lib import errors
from dbabrain.lib.powershell import quote_powershell as _ps_quote

DEFAULT_MAX_BYTES = 1 << 20
LIMIT_MAX_BYTES = 16 << 20
FIELDS = frozenset({"host", "path", "max_bytes", "timeout_seconds"})


class ReadFileError(errors.RequestError):
    """The file could not be read, or did not arrive as the host described it."""


def read_file(request: dict[str, Any]) -> dict[str, Any]:
    """``{path, file_name, size_bytes, sha256, base64}`` - the bytes, proven against the host's own facts."""
    if not isinstance(request, dict):
        raise ReadFileError("read-file takes one JSON object.")
    unknown = sorted(set(request) - FIELDS)
    if unknown:
        raise ReadFileError(f"read-file: unknown field(s) {', '.join(unknown)}; it takes {', '.join(sorted(FIELDS))}.")
    host = parse_host(request.get("host"))
    path = str(request.get("path") or "")
    if not path or path != path.strip() or any(ord(c) < 32 for c in path):
        raise ReadFileError("path is required: one full path, no surrounding space or control character.")
    if any(c in path for c in "*?[]"):
        raise ReadFileError(f"path must name one file, not a pattern: {path!r}.")
    absolute = (path.startswith("\\\\") or (len(path) > 2 and path[1] == ":" and path[2] in "\\/")) \
        if host.runtime == WINDOWS else path.startswith("/")
    if not absolute:
        raise ReadFileError(f"path must be absolute: {path!r}.")
    if any(part in (".", "..") for part in re.split(r"[\\/]+", path)):
        raise ReadFileError(f"path must not contain '.' or '..' segments: {path!r}.")
    limit = request.get("max_bytes", DEFAULT_MAX_BYTES)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= LIMIT_MAX_BYTES:
        raise ReadFileError(f"max_bytes must be a whole number 1 to {LIMIT_MAX_BYTES}; got {limit!r}.")
    timeout = request.get("timeout_seconds", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 10:
        raise ReadFileError(f"timeout_seconds must be a whole number, 10 or more; got {timeout!r}.")

    script = windows_script(path, limit) if host.is_windows else posix_script(path, limit)
    try:
        result = run(host, script, timeout=timeout)
    except HostCommandError as exc:
        raise ReadFileError(f"{path}: the host could not be reached: {exc}") from exc
    return _read_answer(path, result, limit=limit)


def posix_script(path: str, limit: int) -> str:
    p = shlex.quote(path)
    return (
        f"p={p}; "
        "if [ -L \"$p\" ]; then echo 'LINK'; elif [ ! -f \"$p\" ]; then echo 'MISSING'; "
        "else s=$(stat -c %s -- \"$p\") || exit 3; "
        f"if [ \"$s\" -gt {int(limit)} ]; then echo \"TOO_BIG $s\"; "
        "else h=$(sha256sum < \"$p\") || exit 4; printf 'OK %s %s\\n' \"$s\" \"${h%% *}\"; "
        "base64 < \"$p\" | tr -d '\\n'; echo; fi; fi"
    )


def windows_script(path: str, limit: int) -> str:
    return (
        "&{$ErrorActionPreference='Stop';"
        f"$p={_ps_quote(path)};"
        "if(-not(Test-Path -LiteralPath $p -PathType Leaf)){'MISSING';return};"
        "$i=Get-Item -LiteralPath $p -Force;"
        "if(($i.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0){'LINK';return};"
        f"if($i.Length -gt {int(limit)}){{\"TOO_BIG $($i.Length)\";return}};"
        "$b=[System.IO.File]::ReadAllBytes($i.FullName);"
        "$sha=[System.Security.Cryptography.SHA256]::Create();"
        "$h=[BitConverter]::ToString($sha.ComputeHash($b)).Replace('-','').ToLower();"
        "\"OK $($b.Length) $h\";[Convert]::ToBase64String($b)}"
    )


def _read_answer(path: str, result: dict[str, Any], *, limit: int) -> dict[str, Any]:
    lines = [line.strip() for line in str(result.get("stdout") or "").splitlines() if line.strip()]
    head = lines[0].split() if lines else []
    word = head[0] if head else ""
    if word == "MISSING":
        raise ReadFileError(f"{path}: no such file.")
    if word == "LINK":
        raise ReadFileError(f"{path}: is a link - never followed.")
    if word == "TOO_BIG":
        raise ReadFileError(f"{path}: {head[1] if len(head) > 1 else '?'} bytes, over max_bytes {limit}.")
    if word != "OK" or len(head) != 3 or len(lines) > 2:
        stderr = str(result.get("stderr") or "").strip()
        raise ReadFileError(f"{path}: unexpected answer from the host: {(stderr or ' '.join(head))[:300]!r}.")
    stated_size, stated_sha = int(head[1]), head[2].lower()
    data = base64.b64decode(lines[1] if len(lines) == 2 else "", validate=True)
    sha = hashlib.sha256(data).hexdigest()
    if len(data) != stated_size or sha != stated_sha:
        raise ReadFileError(f"{path}: arrived as {len(data)} bytes / {sha}, the host stated "
                            f"{stated_size} bytes / {stated_sha} - not used.")
    name = re.split(r"[\\/]", path.rstrip("\\/"))[-1]
    return {"path": path, "file_name": name, "size_bytes": len(data), "sha256": sha,
            "base64": base64.b64encode(data).decode("ascii")}


__all__ = ["read_file", "ReadFileError", "Host"]
