"""``key-request`` / ``key-renew`` / ``key-status`` - plumbing only (1.0.0, plan K3).

:mod:`dbabrain.common.dbabrain_key_admin` does the work, :mod:`dbabrain.lib.response` shapes the
answer. Bare is ``{}``: these are what a person types (``dbabrain key-request``), not what a
scheduler sends.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from dbabrain.lib import response

KEY_REQUEST_USAGE = """\
Usage: dbabrain key-request ['<json>'|@file|-]      (also: python -m dbabrain.common.cli key-request)

Get this node a dbabrain key from dbabrain.dev (free, an email sign-in): a code and a link are
printed; open the link, sign in with your email, approve - the key is stored in this node's
encrypted secret store as DBABRAIN_KEY. A key lasts 14 days; your account's earlier key stops
working. Without one, dbabrain keeps its metrics, instant alerts, SLA and SQL tasks; reports,
backup / restore, SRE, the MCP server and the assistant need it.

  {}                                     // dbabrain.dev, this machine's name
  {"node": "prod-worker-1",              // how the key names this machine (shown, never trusted)
   "server": "https://mcp.dbabrain.dev", // the service that issues keys
   "wait": true}                         // wait for the approval with no terminal (worker-run)

Needs DBABRAIN_SECRET_KEY (the store's passphrase). Answers data: {ref, store, replaced, key, state}.
key-renew is the same command: a new key, the earlier one replaced.
"""

KEY_STATUS_USAGE = """\
Usage: dbabrain key-status ['<json>'|@file|-]       (also: python -m dbabrain.common.cli key-status)

This node's dbabrain key: offline (the signature, the term - 14 days, then 3 days of grace) and,
when it holds one, dbabrain.dev's answer about it (revoked or replaced ends it at once; a check
that fails changes nothing). Says what each gated feature may do now.

  {}                                     // ask dbabrain.dev too
  {"online": false}                      // offline only

Needs DBABRAIN_SECRET_KEY. Answers data: {standing, online, features, how_to_get}.
"""


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    usage = KEY_STATUS_USAGE if operation == "key-status" else KEY_REQUEST_USAGE
    if argv and argv[0] in {"-h", "--help"}:
        print(usage)
        return 0
    if len(argv) > 1:
        return response.emit(response.fail(operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))
    if argv:
        request, code = read_request(argv[0], usage)
        if request is None:
            return code
    else:
        request = {}

    from dbabrain.common import dbabrain_key_admin as admin
    from dbabrain.lib.paths import resolve_data_dir, resolve_tool_root

    # Resolved now, not at import: DBABRAIN_HOME / DBABRAIN_DATA_DIR as this process states them.
    tool_root = resolve_tool_root()
    data_dir = resolve_data_dir(tool_root=tool_root)
    server = str(request.get("server") or admin.DEFAULT_SERVER)
    try:
        if operation == "key-status":
            data = admin.key_status(data_dir, server=server, online=request.get("online", True) is not False)
            standing = data["standing"]
            message = f"dbabrain key: {standing['state']} - {standing['reason']}"
            if data["how_to_get"]:
                message += f"; {data['how_to_get']}"
            return response.emit(response.ok(operation, message=message, data=data))
        if not sys.stdin.isatty() and not request.get("wait"):
            # A sign-in nobody is looking at expires in ten minutes - a scheduler or a pipe would wait
            # for no one (what `mcp connect --wait-for-link` learnt). Said, not waited out.
            return response.emit(response.fail(
                operation, "a person must open a link and approve: run it on a terminal, or say \"wait\": true "
                           "when someone reads this command's output"))
        data = admin.request_key(data_dir, server=server, node=str(request.get("node") or ""),
                                 plaintext_store=Path(tool_root) / "secrets" / "secret_text.json")
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))
    key = data["key"]
    return response.emit(response.ok(
        operation,
        message=(f"{'replaced the' if data['replaced'] else 'stored a'} dbabrain key for {key['email']} as "
                 f"{data['ref']}, valid until {key['expires_at']}"),
        data=data))
