"""``smb-list`` / ``smb-get`` / ``smb-delete`` / ``smb-credential`` - a Windows share, reached here.

Plumbing only: :mod:`db_ops.common.smb` does the work, :mod:`db_ops.lib.response` shapes the
answer. The SQL Server restore reached its backup shares itself until 0.24.0 (rules R10); it asks
these now, and keeps deciding which files, which window and what is obsolete.

**stdin only**, like ``run-sqlcmd``: every request carries the share's password.
"""

from __future__ import annotations

import sys
from typing import Any

from db_ops.lib import response

COMMANDS = ("smb-list", "smb-get", "smb-delete", "smb-credential")

USAGE = """\
Usage: <request> | python -m db_ops.common.cli smb-list|smb-get|smb-delete|smb-credential -

A Windows share, reached from this node: through its UNC path on Windows (after the login is
stored with cmdkey), through smbclient elsewhere. Reads no config - the request states everything,
and arrives on stdin because it carries a password.

Every request:
  "host", "share"          the share is //host/share
  "username", "password"   the login; "DOMAIN\\user" or a separate "domain"
  "backend"                smbclient | unc - default: unc on Windows, smbclient elsewhere
  "timeout_seconds"        default 600 (a listing, a delete) / 3600 (one file)

  smb-list        {"path": "SQLBK\\\\APPDB", "recurse": true, "suffixes": [".bak", ".trn"]}
                  -> data.files: [{"path", "name", "size_bytes", "modified_epoch"}], path relative
                  to "path", backslash-separated
  smb-get         {"remote_path": "APPDB\\\\FULL\\\\x.bak", "local_path": "/tmp/stage/APPDB/FULL/x.bak"}
                  -> data: {"local_path", "bytes", "exit_code", "detail"} - a non-zero exit is an
                  answer; compare "bytes" with the size smb-list gave
  smb-delete      {"paths": ["APPDB\\\\LOG\\\\old.trn", ...]} - exactly these files, never a pattern
                  -> data: {"results": [{"path", "status", "bytes", "error"}], "deleted", "failed"}
  smb-credential  {"target": "192.0.2.10", "username": "...", "password": "..."} - Windows only
                  (cmdkey); elsewhere answers registered false, which is not a failure
"""


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    if argv and argv[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    if not argv or argv[0] != "-":
        print(USAGE, file=sys.stderr)
        return response.emit(response.fail(
            operation, "the request must arrive on stdin (-): it carries the share's password, which "
                       "inline is visible on the command line"))
    request, code = read_request("-", USAGE)
    if request is None:
        return code

    from db_ops.common import smb

    work = {"smb-list": smb.listing, "smb-get": smb.get, "smb-delete": smb.delete,
            "smb-credential": smb.register_credential}[operation]
    try:
        data = work(request)
    except (smb.SmbError, OSError) as exc:
        return response.emit(response.fail(operation, str(exc)))
    if operation == "smb-list":
        message = f"{data['count']} file(s) under //{data['host']}/{data['share']}/{data['path']}"
    elif operation == "smb-get":
        message = f"{data['remote_path']} -> {data['local_path']} ({data['bytes']} bytes, exit {data['exit_code']})"
    elif operation == "smb-delete":
        message = f"{data['deleted']} deleted, {data['failed']} failed"
    else:
        message = (f"login stored for {data['target']}" if data["registered"]
                   else f"nothing to store for {data['target']}: {data['reason']}")
    ok = not (operation == "smb-delete" and data["failed"])
    return response.emit(response.ok(operation, message=message, data=data) if ok
                         else response.fail(operation, message, data=data))
