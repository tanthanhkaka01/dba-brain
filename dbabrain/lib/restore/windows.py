"""A restore onto SQL Server running natively on a Windows host: its paths and what it refuses (0.39.0).

The operator, 2026-11-14: test restores into SQL Server installed on Windows (the lab host, SQL Server 2025,
a named instance on a static TCP port), from the repository's encrypted copies, to the newest point and
to a point in time. The script-driven restore knew two targets - a Linux host, and a container on one -
and a Windows one fell through as ``linux``: ``df`` and ``chmod`` sent to cmd.exe, a staging path
refused for not starting with ``/``.

The shape it takes, every part of it stated by the target's inventory record and the entry:

* **reached over SSH** - Windows OpenSSH. The staging is written over SFTP (which names ``C:\\x`` as
  ``/C:/x`` and takes ``C:/x`` too - measured on the lab host, 2026-11-14) and checked, listed and cleaned
  by PowerShell through the same login. A Windows host reached only by WinRM has no SFTP to stage
  through: refused, by name;
* **SQL Server only, natively** - no container on a Windows host. PostgreSQL and Oracle restore by
  laying down a data directory (``pg_combinebackup``) or by an RMAN ``DUPLICATE`` run through bash;
  neither is built for Windows, so either is refused before anything is copied, not half-run;
* **a drive path** for the staging (``target_backup_dir``, ``target_visible_dir``), stated with
  either separator; it is carried with forward slashes, the form every hop takes, and handed to SQL
  Server with backslashes, the form it names its own files in.

Pure values and rules: the app reads the inventory and decides, ``common`` runs - both read these.
"""

from __future__ import annotations

import re
from typing import Any

#: The inventory's word for a Windows host (``db_instances.json``, the instance's ``platform``).
PLATFORM = "windows"

#: ``C:/x``, ``C:\x`` - or ``/C:/x``, the way Windows OpenSSH's SFTP names a drive.
_DRIVE = re.compile(r"^/?([A-Za-z]):[\\/]")
#: Characters no Windows file name may hold - and the wildcards a path must never be.
_NOT_IN_A_NAME = set('*?"<>|')

#: What finish-staging does about the engine's access on Windows: nothing. The staging folder is read
#: by the SQL Server service account (``NT Service\MSSQL$<instance>``) through the folder's ACL, which
#: the operator grants once; files written under it inherit it. A ``chmod`` has no meaning there.
ENGINE_ACCESS = ("nothing changed: on Windows SQL Server's service account reads the staging folder through "
                 "its ACL, granted once by the operator, and every file written under it inherits it")


def is_drive_path(value: Any) -> bool:
    """Does ``value`` name a place on a Windows drive (``C:/x``, ``C:\\x``, ``/C:/x``)?"""
    return isinstance(value, str) and bool(_DRIVE.match(value.strip()))


def drive_path(value: Any, *, field: str) -> str:
    """``value`` as a Windows drive path written with forward slashes, no trailing one - or refused.

    ``C:\\dbabrain_restore\\X``, ``C:/dbabrain_restore/X/`` and ``/C:/dbabrain_restore/X`` are one
    place, and come back as ``C:/dbabrain_restore/X``. The drive letter keeps its case: Windows does
    not care, and a path the entry stated is read back in its own words.
    """
    text = str(value or "").strip()
    found = _DRIVE.match(text)
    if not found:
        raise ValueError(f"{field} must be a Windows drive path, e.g. C:/dbabrain_restore/<restore_id>; got {value!r}.")
    parts = [part for part in re.split(r"[\\/]+", text[found.end():]) if part]
    for part in parts:
        if part in (".", ".."):
            raise ValueError(f"{field} must not contain '.' or '..' segments: {value!r}.")
        if ":" in part:
            # An alternate data stream (`a.bak:x`) is not the file the name says.
            raise ValueError(f"{field} must not hold ':' past its drive: {value!r}.")
        if _NOT_IN_A_NAME.intersection(part) or any(ord(char) < 32 for char in part):
            raise ValueError(f"{field} must name one place - no wildcard, no character Windows refuses: {value!r}.")
    return f"{found.group(1)}:/" + "/".join(parts)


def sftp_form(value: Any, *, field: str) -> str:
    """The drive path as Windows OpenSSH's SFTP names it: ``/C:/dbabrain_restore/X``.

    An absolute POSIX path, so ``posixpath`` joins and relates the staging's files to it the way it does
    on a Linux host; SFTP takes ``C:/x`` as well, but a path without its leading slash reads as relative
    to everything else that handles it.
    """
    return "/" + drive_path(value, field=field)


def sql_server_path(value: Any, *, field: str = "path") -> str:
    """The drive path as SQL Server names its own files: ``C:\\dbabrain_restore\\X``."""
    return drive_path(value, field=field).replace("/", "\\")


def refusal(*, db_type: Any, access: Any, container: Any, target_dir: Any, visible_dir: Any = "") -> str:
    """Why an entry cannot restore onto this Windows target - ``""`` when it can.

    Every reason is one the restore would otherwise meet half way: after the copy, or as a command
    cmd.exe does not know. Said before anything is copied.
    """
    engine = str(db_type or "").strip().lower()
    if engine not in ("sqlserver", "mssql"):
        return (f"its target is a Windows host, and a {engine or 'restore'} restore onto Windows is not built - "
                "SQL Server only (PostgreSQL and Oracle lay down a data directory or run an RMAN DUPLICATE "
                "through bash, which a Windows host does not have). Restore it onto a Linux target.")
    if str(container or "").strip():
        return ("its target is a Windows host, where SQL Server runs natively: state no target_container "
                "(a container on a Windows host is not built).")
    if str(access or "").strip().lower() != "ssh":
        return (f"its target is a Windows host reached by {str(access or '').strip() or 'nothing'}: the staging is "
                "written over SFTP and cleaned through PowerShell over SSH, so the target's cmd_access.method must "
                "be ssh (Windows OpenSSH) - WinRM has no SFTP to stage through.")
    for field, value in (("target_backup_dir", target_dir), ("target_visible_dir", visible_dir)):
        if not str(value or "").strip():
            continue
        try:
            drive_path(value, field=field)
        except ValueError as exc:
            return f"its target is a Windows host, and {exc}"
    return ""


def linux_refusal(*, target_dir: Any, visible_dir: Any = "") -> str:
    """Why a target whose record says Linux cannot take a Windows staging path - ``""`` when it can."""
    for field, value in (("target_backup_dir", target_dir), ("target_visible_dir", visible_dir)):
        if is_drive_path(value):
            return (f"its {field} {value!r} is a Windows drive path, and its target's inventory record does not say "
                    "platform windows - state the platform on the record, or a POSIX path for the staging.")
    return ""
