"""The backup shares this app reads and cleans - reached through ``common.cli`` (rules R10).

Until 0.24.0 this app met a Windows share three ways of its own: ``smbclient`` on a Linux worker, a
local PowerShell over a UNC path on a Windows master, and ``cmdkey`` to store the login first. A
share is an operation every app that meets one needs the same way, so it is ``common``'s now
(``smb-list``, ``smb-get``, ``smb-delete``, ``smb-credential``); this module states each request -
the login resolved by the caller, never read in ``common`` - and hands back the answer.

What stays here is what is this app's: which files, which window, what is obsolete.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db_ops.transport import common_cli


class ShareError(RuntimeError):
    """The share answered with a failure, or never answered."""


def unc_parts(unc: object) -> tuple[str, str, str]:
    """Split ``\\\\host\\share\\sub\\dir`` into ``(host, share, "sub\\dir")``."""
    parts = [segment for segment in str(unc).replace("/", "\\").split("\\") if segment]
    if len(parts) < 2:
        raise ShareError(f"Invalid SMB share path: {unc}")
    return parts[0], parts[1], "\\".join(parts[2:])


def _request(unc: object, *, username: str, password: str, **extra: Any) -> dict[str, Any]:
    host, share, subpath = unc_parts(unc)
    request: dict[str, Any] = {"host": host, "share": share, "username": username or "",
                               "password": password or "", **extra}
    request.setdefault("path", subpath)
    return request


def _call(command: str, request: dict[str, Any]) -> dict[str, Any]:
    try:
        return common_cli.run(command, request)
    except common_cli.CommonCliError as exc:
        raise ShareError(str(exc)) from exc


def list_files(unc: object, *, username: str, password: str, recurse: bool = True,
               suffixes: tuple[str, ...] = (), timeout_seconds: int | None = None) -> list[dict[str, Any]]:
    """``[{"path", "name", "size_bytes", "modified_epoch"}]`` under ``unc``, paths relative to it."""
    request = _request(unc, username=username, password=password, recurse=recurse,
                       suffixes=list(suffixes))
    if timeout_seconds:
        request["timeout_seconds"] = int(timeout_seconds)
    return list(_call("smb-list", request).get("files") or [])


def get_file(unc_root: object, remote_path: str, local_path: Path, *, username: str, password: str,
             timeout_seconds: int | None = None) -> dict[str, Any]:
    """One file under the share root to ``local_path``; ``bytes`` is what arrived."""
    host, share, _subpath = unc_parts(unc_root)
    request: dict[str, Any] = {"host": host, "share": share, "username": username or "",
                               "password": password or "", "remote_path": remote_path,
                               "local_path": str(local_path)}
    if timeout_seconds:
        request["timeout_seconds"] = int(timeout_seconds)
    return _call("smb-get", request)


def delete_files(unc_root: object, paths: list[str], *, username: str, password: str) -> dict[str, Any]:
    """Delete exactly ``paths`` (share-relative). A file that could not go is in the answer, not raised."""
    host, share, _subpath = unc_parts(unc_root)
    ok, data, error = common_cli.run_allowing_failure("smb-delete", {
        "host": host, "share": share, "username": username or "", "password": password or "",
        "paths": list(paths)})
    if not ok and not data.get("results"):
        raise ShareError(error)
    return data


def store_login(*, target: str, username: str, password: str) -> dict[str, Any]:
    """Store the login Windows uses for ``target``'s UNC paths (``cmdkey``); nothing to do elsewhere."""
    return _call("smb-credential", {"target": target, "username": username, "password": password})


def share_relative(unc_root: object, full_path: object) -> str:
    """``full_path`` as a path under the share of ``unc_root`` - what ``smb-get`` / ``smb-delete`` take."""
    _host, _share, subpath = unc_parts(unc_root)
    relative = str(full_path).replace("/", "\\")
    root = str(unc_root).replace("/", "\\").rstrip("\\")
    if relative.lower().startswith(root.lower() + "\\"):
        relative = relative[len(root) + 1:]
        return f"{subpath}\\{relative}" if subpath else relative
    return relative.lstrip("\\")
