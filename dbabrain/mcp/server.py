"""The MCP server: every ``common.cli`` command and the runtime store, as tools for Claude (0.38.0).

``dbabrain mcp serve`` speaks MCP over stdio to Claude Desktop or Claude Code on the same machine
(way 1 of ``audits/20261110_plan_mcp_connector.md``). What it offers and on what terms is
``lib.mcp_tools``' and ``data/mcp_tools.json``'s; this module runs a call:

* a **command tool** takes ``request`` - the command's own JSON request - completes it from the
  node's inventory and secret store where the command needs a connection or a host login
  (``lib.data_sources.request_fill``, as ``run-sql-adhoc`` and ``run-cmd-adhoc`` do), runs it through
  ``dbabrain.transport`` - an app never imports ``common`` (R03) - and answers its JSON envelope;
* a **store tool** reads the runtime store through ``db.read_only_query`` - one SELECT, read-only,
  rolled back;
* **every answer** is redacted (``lib.redaction``: each value of the secret store, and the shapes a
  secret is printed in), capped at ``max_chars``, and - with ``log_calls`` - written as a ``job_runs``
  row ``mcp.<tool>`` (who asked is the node's user; which tool, which server_id, how long, ok or not),
  which the console's runs page shows.

Nothing here prints to stdout: in ``serve`` stdout IS the MCP channel.
"""

from __future__ import annotations

import contextvars
import json
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dbabrain.lib import errors
from dbabrain.lib import mcp_tools as M
from dbabrain.lib.redaction import Redactor, mask_sensitive_text
from dbabrain.transport import common_cli

SERVER_NAME = "dbabrain"

#: Who asked, when the call came through dbabrain.dev: the email the Worker signed in, carried as
#: ``x-dbabrain-user`` (behind the origin's token, or on the node's own connection). Recorded on the
#: call's job_runs row; empty for a local Claude.
ASKED_BY: contextvars.ContextVar[str] = contextvars.ContextVar("dbabrain_mcp_asked_by", default="")

INSTRUCTIONS = (
    "dbabrain is a DBA operations toolkit. Each tool named like a command (db-status, run-sql, list-databases, "
    "...) runs that dbabrain common.cli command; its JSON request is the `request` argument, and a server is "
    "named by its `server_id` (list-targets lists them) - the connection and the host login are completed from "
    "this node's inventory and encrypted secret store. run-sql reads only; run-sql-write changes data and keeps "
    "it only with commit. store_schema and store_query read dbabrain's own runtime store (job runs, metrics, "
    "the backup catalog, SLA). A tool marked destructive changes something; an emergency operation takes "
    "`confirm`: \"yes\" and a `reason` in its request."
)


class McpToolError(errors.RequestError):
    """A call the server refuses before running anything."""


@dataclass
class Node:
    """Where the server runs: the tool root's data, its config, its settings, the secrets read once."""

    data_dir: Path
    config_path: Path | None
    settings: M.McpSettings
    reference: list[dict[str, Any]]
    operations: set[str]
    _secrets: dict[str, str] | None = None
    secrets_error: str = ""
    _redactor: Redactor | None = None
    _config: Any = field(default=None, repr=False)

    def secrets(self) -> dict[str, str]:
        if self._secrets is None:
            from dbabrain.lib.secret_text import load_secret_text

            try:
                self._secrets = load_secret_text(self.data_dir)
            except Exception as exc:  # noqa: BLE001 - a node with no key still answers what needs none
                self._secrets, self.secrets_error = {}, f"{type(exc).__name__}: {exc}"
        return self._secrets

    def redact(self, text: str) -> str:
        if self._redactor is None:
            self._redactor = Redactor(self.secrets())
        return self._redactor(text)

    def config(self) -> Any:
        if self._config is None:
            from dbabrain.lib.config import load_config

            self._config = load_config(str(self.config_path) if self.config_path else "config.json")
        return self._config

    def commands(self) -> dict[str, dict[str, Any]]:
        return M.command_entries(self.reference)


def load_node(*, tool_root: Path, data_dir: Path) -> Node:
    """The node's settings (``data/mcp_tools.json``, the shipped one where the node has none yet),
    the reference and the emergency-operations catalogue."""
    from dbabrain.lib import shared_objects
    from dbabrain.lib.json_io import load_json_file
    from dbabrain.lib.paths import reference_file

    path = data_dir / M.FILE_NAME
    if not path.is_file():
        path = Path(__file__).resolve().parent / "catalogue" / M.FILE_NAME
    settings = M.parse_mcp_tools(load_json_file(path))
    operations = M.operation_commands(load_json_file(reference_file("emergency_operations.json", data_dir)))
    config = tool_root / "config.json"
    return Node(data_dir=data_dir, config_path=config if config.is_file() else None, settings=settings,
                reference=shared_objects.load(data_dir), operations=operations)


# --------------------------------------------------------------------------- #
# One call
# --------------------------------------------------------------------------- #
def call_tool(node: Node, tool: str, arguments: dict[str, Any] | None) -> str:
    """Run one tool and answer its text - redacted, capped, logged. Never raises: an error is an answer."""
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clock = time.monotonic()
    arguments = dict(arguments or {})
    server_id = ""
    ok = False
    try:
        if not node.settings.offers(tool):
            raise McpToolError(f"the tool {tool} is switched off in {M.FILE_NAME}"
                               + ("" if node.settings.enabled else " (enabled is false)") + ".")
        if tool in M.STORE_TOOLS:
            answer = _store_tool(node, tool, arguments)
            ok = True
        else:
            request = arguments.get("request") or {}
            if not isinstance(request, dict):
                raise McpToolError("`request` is the command's JSON request - an object.")
            server_id = str(request.get("server_id") or request.get("target") or "")
            answer, ok = _command_tool(node, tool, request)
    except Exception as exc:  # noqa: BLE001 - the model reads why; nothing escapes the call
        answer = {"success": False, "tool": tool, "error": f"{type(exc).__name__}: {exc}"}
    text = json.dumps(answer, ensure_ascii=False, indent=1, default=str)
    text = M.capped(node.redact(text), node.settings.max_chars)
    if node.settings.log_calls:
        _log(node, tool=tool, server_id=server_id, ok=ok, started_at=started_at,
             duration_ms=int((time.monotonic() - clock) * 1000), text=text)
    return text


def _command_tool(node: Node, command: str, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if command == "run-sql":
        request = _read_only_sql_request(request)
    elif command == M.SQL_WRITE_TOOL:
        command = "run-sql"
    if command not in node.commands():
        raise McpToolError(f"{command} is not a common.cli command (list-targets, run-sql, db-status, ...).")
    from dbabrain.lib.data_sources import request_fill

    needs = request_fill.FILLS.get(command, ())
    filled = request_fill.fill_request(command, request, data_dir=node.data_dir,
                                       secrets=node.secrets() if needs else None)
    # The command reads the tool root the server was started for (DBABRAIN_HOME), not its own guess.
    result, why = common_cli.spawn(command, filled, timeout_seconds=node.settings.timeout_seconds)
    if result is None:
        return {"success": False, "operation": command, "error": why}, False
    try:
        envelope = json.loads(result.stdout)
        if not isinstance(envelope, dict):
            raise ValueError("not an object")
    except ValueError:
        envelope = {"success": result.returncode == 0, "operation": command,
                    "output": str(result.stdout or "")[-20000:], "stderr": mask_sensitive_text(str(result.stderr or ""))[-4000:]}
    # The request it ran is never handed back: it holds the password it was completed with.
    envelope.pop("request", None)
    return envelope, bool(envelope.get("success"))


def _read_only_sql_request(request: dict[str, Any]) -> dict[str, Any]:
    """``run-sql``'s read-only half: one statement given as text, held to ``lib.read_only_sql`` before it
    runs, and never committed - a DDL statement commits by itself on Oracle and MySQL, so a rollback alone
    would not keep this tool read-only."""
    from dbabrain.lib.read_only_sql import read_only_sql

    if request.get("sql_file"):
        raise McpToolError("run-sql takes the statement as `sql_text` (a file's SQL is not checked); "
                           f"{M.SQL_WRITE_TOOL} runs a file.")
    checked = dict(request)
    checked["sql_text"] = read_only_sql(str(request.get("sql_text") or request.get("sql") or ""))
    checked.pop("sql", None)
    for flag in ("commit", "autocommit"):
        checked.pop(flag, None)
    return checked


def _store_tool(node: Node, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from dbabrain.db.backend import StoreTarget
    from dbabrain.db.read_only_query import store_query, store_schema

    target = StoreTarget.from_config(node.config())
    settings = node.settings
    if tool == "store_schema":
        return store_schema(target, table=str(arguments.get("table") or ""),
                            blocked_tables=settings.store_blocked_tables)
    asked = arguments.get("max_rows") or settings.store_query_max_rows
    return store_query(target, str(arguments.get("sql") or ""),
                       max_rows=min(int(asked), settings.store_query_max_rows),
                       timeout_seconds=settings.store_query_timeout_seconds,
                       blocked_tables=settings.store_blocked_tables)


def _log(node: Node, *, tool: str, server_id: str, ok: bool, started_at: str, duration_ms: int, text: str) -> None:
    """A ``job_runs`` row per call; a store that cannot be written never fails the call."""
    try:
        from dbabrain.db.job_runs import JobRun
        from dbabrain.db.store import DbaBrainStore

        DbaBrainStore.from_config(node.config()).insert_job_run(JobRun(
            job_code=f"mcp.{tool}", level="logging" if ok else "error", status="DONE" if ok else "ERROR",
            message=f"MCP {tool}" + (f" on {server_id}" if server_id else "") + (" - done" if ok else " - failed"),
            started_at=started_at, finished_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            duration_ms=duration_ms, error_text=None if ok else text[:2000], host_name=socket.gethostname(),
            metadata={"tool": tool, "server_id": server_id, "ok": ok, "user": os.environ.get("USERNAME")
                      or os.environ.get("USER") or "", "via": "mcp", "asked_by": ASKED_BY.get()}))
    except Exception:  # noqa: BLE001 - logging is a record of the call, never a reason to fail it
        pass


# --------------------------------------------------------------------------- #
# The server
# --------------------------------------------------------------------------- #
def tool_list(node: Node) -> list[dict[str, Any]]:
    """Every tool the node offers: its name, its description, and the hints the client shows."""
    from dbabrain.lib.data_sources import request_fill

    tools = []
    for command, entry in node.commands().items():
        if not node.settings.offers(command):
            continue
        # One of the two hints on every tool (the directory's rule): what does not only read, changes.
        read_only = M.is_read_only(command, node.operations)
        tools.append({"name": command, "kind": "command", "destructive": not read_only, "read_only": read_only,
                      "description": M.describe(command, entry, fills=bool(request_fill.FILLS.get(command)),
                                                destructive=not read_only)})
        if command == "run-sql" and node.settings.offers(M.SQL_WRITE_TOOL):
            tools.append({"name": M.SQL_WRITE_TOOL, "kind": "command", "destructive": True, "read_only": False,
                          "description": M.describe_sql_write(entry)})
    for name, text in (("store_schema", "dbabrain's runtime store: every table and its columns, or one table's "
                                        "columns with their types (`table`)."),
                       ("store_query", "One read-only SELECT on dbabrain's runtime store (`sql`), always rolled "
                                       "back; `max_rows` up to the node's cap. Tables: store_schema.")):
        if node.settings.offers(name):
            tools.append({"name": name, "kind": "store", "destructive": False, "read_only": True,
                          "description": text})
    return tools


def _tool_function(node: Node, name: str) -> Any:
    """The function the SDK registers for one tool - its parameters are the tool's input schema, so
    nothing else may be one (a bound name as a default argument would be offered to the model)."""
    if name == "store_schema":
        def store_schema_tool(table: str = "") -> str:
            return call_tool(node, name, {"table": table})
        return store_schema_tool
    if name == "store_query":
        def store_query_tool(sql: str, max_rows: int = 0) -> str:
            return call_tool(node, name, {"sql": sql, "max_rows": max_rows})
        return store_query_tool

    def command_tool(request: dict[str, Any] | None = None) -> str:
        return call_tool(node, name, {"request": request or {}})
    return command_tool


def build_server(node: Node) -> Any:
    """The SDK's server with every offered tool registered. Needs the ``mcp`` extra."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    from dbabrain.lib.version import __version__

    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)
    for spec in tool_list(node):
        name = spec["name"]
        annotations = ToolAnnotations(title=name, read_only_hint=spec["read_only"],
                                      destructive_hint=spec["destructive"], open_world_hint=spec["kind"] == "command")
        fn = _tool_function(node, name)
        server.add_tool(fn, name=name, description=spec["description"], annotations=annotations)
    return server


# --------------------------------------------------------------------------- #
# Over HTTP - way 2's origin (0.38.0)
# --------------------------------------------------------------------------- #
#: The environment variable the HTTP origin's token is read from - never a file, never a flag that
#: would sit in a process list or a shell history.
HTTP_TOKEN_ENV = "DBABRAIN_MCP_HTTP_TOKEN"
#: A token shorter than this is refused: it is the only thing between the internet and every
#: common.cli command once the origin is reachable through a tunnel.
HTTP_TOKEN_MIN_CHARS = 32


class _BearerOnly:
    """An ASGI wrapper: every HTTP request carries ``Authorization: Bearer <token>``, compared in
    constant time - else 401 and the server is never reached. ``/healthz`` answers without it, and
    says nothing but ``ok``. The token is the Worker's on dbabrain.dev, which has already signed the
    person in (Cloudflare Access); the origin trusts nothing else."""

    def __init__(self, app: Any, token: str) -> None:
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        import hmac

        if scope.get("type") == "http":
            if scope.get("path") == "/healthz":
                await _plain(send, 200, b"ok")
                return
            headers = dict(scope.get("headers") or [])
            if not hmac.compare_digest(headers.get(b"authorization", b""), self.expected):
                await _plain(send, 401, b"unauthorized")
                return
            # Trusted only here, behind the token: the Worker states who it signed in.
            token = ASKED_BY.set(headers.get(b"x-dbabrain-user", b"").decode("utf-8", "replace")[:200])
            try:
                await self.app(scope, receive, send)
            finally:
                ASKED_BY.reset(token)
            return
        await self.app(scope, receive, send)


async def _plain(send: Any, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def http_app(node: Node, *, token: str, allowed_hosts: list[str] | None = None) -> Any:
    """The MCP server as an ASGI app on ``/mcp`` (Streamable HTTP, stateless), behind the bearer check.

    Stateless: each request stands alone, so the Worker in front may route any request anywhere and
    a restart loses nothing. ``allowed_hosts`` is the Host header the tunnel delivers (DNS-rebinding
    protection); empty, the check is off - for a test."""
    if len(token or "") < HTTP_TOKEN_MIN_CHARS:
        raise McpToolError(f"the HTTP origin needs {HTTP_TOKEN_ENV} of {HTTP_TOKEN_MIN_CHARS} characters or more "
                           "- it is all that stands between the tunnel and every command.")
    from mcp.server.transport_security import TransportSecuritySettings

    security = TransportSecuritySettings(enable_dns_rebinding_protection=bool(allowed_hosts),
                                         allowed_hosts=list(allowed_hosts or []), allowed_origins=[])
    app = build_server(node).streamable_http_app(stateless_http=True, json_response=True,
                                                 transport_security=security, host="0.0.0.0")
    return _BearerOnly(app, token)

