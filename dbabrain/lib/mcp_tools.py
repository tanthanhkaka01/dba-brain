"""What the MCP server offers, and on what terms - ``data/mcp_tools.json`` (0.38.0, 1.312-1.321).

The operator, 2026-11-11 (translated): *let MCP run every common cli, SQL included, and read the
dbabrain store.* So the server (``dbabrain mcp serve``) offers **one tool per ``common.cli``
command** - the list read from the reference (``shared_config_objects.json``), where every command
already has its ``input_`` entry, so a new command is a new tool with nothing written here - and two
tools of its own on the runtime store, ``store-schema`` and ``store-query`` (one SELECT, read-only,
rolled back). That replaced the sheet's first plan (read-only tools only, 1.315); what keeps it safe
is unchanged from the CLI:

* **each command keeps its own gate** - a destructive one (``kill-spid``, ``shrink-log``,
  ``host-restart``, a delete ...) still refuses without the ``confirm`` / ``reason`` its rules ask for,
  exactly as from a terminal with no TTY;
* **the client asks the person** - every tool carries MCP's hints, and the destructive ones say so
  (``destructiveHint``), which Claude Desktop and Claude Code show before running it;
* **no secret crosses** - every answer is taken through ``lib.redaction`` (every value of the secret
  store, and the shapes a secret is printed in) before the model sees it;
* **an answer is capped** at ``max_chars`` and says how much was cut;
* **a tool is switched off by name** in ``disabled_tools``.

Pure: the file's rules, the tool list and the text each tool is described by. The server is the
``mcp`` app's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from dbabrain.lib import errors
from dbabrain.lib.mcp_tool_text import SQL_WRITE_TEXT, TOOL_TEXT
from dbabrain.lib.read_only_sql import ReadOnlyRefusal, read_only_sql
from dbabrain.lib.sql_text import expand_sqlplus_defines

FILE_NAME = "mcp_tools.json"

#: The two tools on the runtime store - not common.cli commands, the server's own. Named ``store_schema`` and
#: ``store_query`` until 1.3.0 (1.439): the only snake_case names among kebab-case ones (Glama's review of 1.2.0).
STORE_TOOLS = ("store-schema", "store-query")

#: Every field is stated (R50); ``upgrade-config`` writes these where one is missing.
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "log_calls": True,
    "max_chars": 60000,
    "timeout_seconds": 600,
    "store_query_max_rows": 500,
    "store_query_timeout_seconds": 60,
    "store_blocked_tables": ["web_users", "web_sessions", "web_login_attempts"],
    "disabled_tools": [],
}

#: A command whose name starts so takes something away: it is marked destructive, whatever the
#: emergency-operations catalogue says. The catalogue names the rest (``host-restart``, ``kill-spid``...).
DESTRUCTIVE_PREFIXES = ("delete-", "prune-", "drop-", "kill-", "shrink-", "smb-delete", "rotate-", "host-restart",
                        "host-service", "disable-", "remove-",
                        # Whatever the text they are given does: a shell command on a host, a sqlcmd
                        # batch - and the two that write on a restore's target (a folder, its marker,
                        # the files' times and modes).
                        "run-cmd", "run-sqlcmd", "prepare-staging", "finish-staging")

#: The reference's output entry for a command that reads its request and refuses it (0.38.0): such a
#: command is not offered as a tool - it could only ever answer its own refusal.
REFUSED_OUTPUT = "output_refused_command"

#: A command whose name starts so only reads - said to the client as ``readOnlyHint``. A hint, not a gate.
READ_ONLY_PREFIXES = ("list-", "check-", "probe-", "db-status", "host-facts", "file-checksum", "trace-session",
                      "verify-", "describe", "show-", "store-", "backup-history", "restore-drill-status", "sql-run-history")


#: **No secret enters through MCP** (0.39.0) - the twin of *no secret crosses* (every answer redacted). The
#: operator, 2026-11-14: a request over MCP is written by the model, so a password, a key or a token in it
#: came through the chat - and on way 2 through the relay. These keys hold a secret wherever they sit in a
#: request (a login's ``password`` inside ``connection`` / ``access`` too); a request carrying one is
#: refused, and the person types the secret on the node instead (``secret-set``, ``ai-key``), then names
#: it here by its reference (``password_ref``). A login the node completes from its own store is not
#: one: the model never sees it.
SECRET_KEYS = frozenset({"password", "sudo_password", "encryption_password", "passwords", "secrets", "identity",
                         "passphrase", "api_key", "token"})

#: Commands whose purpose is a secret the caller gives - ``value`` is it.
SECRET_VALUE_COMMANDS = frozenset({"secret-set", "secret-remove", "ai-key"})

#: Commands the MCP server does not offer because another tool does the same (1.3.0, 1.434): a one-item list
#: is the singular, and renewing a key is requesting one again - the same sign-in, the new key replacing
#: the old. Glama's review of 1.1.0 named these pairs as tools an agent could not choose between. The
#: commands stay in common.cli; over MCP a call to one is refused, naming the tool that does it.
COVERED_TOOLS = {"delete-verified-file": "delete-verified-files", "delete-empty-folder": "delete-empty-folders",
                 "key-renew": "key-request",
                 # 1.439: "the older add-sql" - a script and its target in one call; the two newer ones do it.
                 "add-sql": "sql-command-add + sql-target-add"}

#: Commands that ask a person at the node's terminal (1.3.0, 1.439): over MCP there is none, so the call could only
#: wait or be refused. The client's own approval prompt is the person an MCP call is shown to.
TERMINAL_TOOLS = frozenset({"ask", "authorize"})

#: MCP tools that are several common.cli commands, one field choosing which (1.3.0, 1.439). Each group's commands
#: share one request entry and one hint, and the choice is one enum field - the grouping the operator allows
#: (2026-11-27: *group a few tools that are too alike*; the plan of that morning, 108 -> 97). Glama's review of 1.2.0
#: named *numerous backup/restore variants*. The commands stay as they are in common.cli; a call by a command's own
#: name answers which tool and which value. ``{tool: (field, {value: command}, what the field says)}``.
GROUPED_TOOLS: dict[str, tuple[str, dict[str, str], str]] = {
    "restore": ("step", {"full": "restore-full", "diff": "restore-diff", "log": "restore-log"},
                "Which restore step: full, then diff (if any), then log - one call each."),
    "list-objects": ("kind", {"databases": "list-databases", "schemas": "list-schemas", "jobs": "list-jobs"},
                     "What to list: the instance's databases, one database's schemas, or the scheduler's jobs."),
    "check-config": ("check", {"objects": "check-objects", "references": "check-references",
                               "credentials": "check-credentials"},
                     "Which check: objects (fields against the reference), references (pointers between files), "
                     "credentials (every target resolves a login)."),
    "sqlserver-cu-check": ("check", {"precheck": "sqlserver-precheck", "build": "sqlserver-verify-build"},
                           "precheck before a cumulative update is applied, build after it."),
    "list-backup-files": ("action", {"list": "list-backup-files", "prune-plan": "prune-backup-files"},
                          "list: the files the directory holds; prune-plan: which of them a retention rule would "
                          "remove (nothing is deleted)."),
}


def grouped_in(command: str) -> tuple[str, str] | None:
    """``(tool, value)`` when ``command`` is offered as one value of a grouped tool."""
    for tool, (_field, choices, _text) in GROUPED_TOOLS.items():
        for value, member in choices.items():
            if member == command:
                return tool, value
    return None


def member_command(tool: str) -> str:
    """The command whose hints a tool carries - a grouped tool's first (they share one hint)."""
    return next(iter(GROUPED_TOOLS[tool][1].values())) if tool in GROUPED_TOOLS else tool


#: Tools renamed over MCP, and what each was called; a call by the old name answers the new one.
RENAMED_TOOLS = {"store_schema": "store-schema", "store_query": "store-query"}

#: A tool and the names it was offered under before: switched off in ``disabled_tools`` under ANY of them, it stays
#: off - a node that switched ``restore-log`` off is not handed every restore step by a rename.
FORMER_NAMES: dict[str, tuple[str, ...]] = {**{tool: tuple(choices.values()) for tool, (_f, choices, _t)
                                               in GROUPED_TOOLS.items()},
                                            "store-schema": ("store_schema",), "store-query": ("store_query",)}


def not_an_mcp_tool(tool: str) -> str:
    """Why ``tool`` - a common.cli command or a former name - is not called over MCP; "" when it may be."""
    if tool in COVERED_TOOLS:
        return f"{tool} is not an MCP tool - {COVERED_TOOLS[tool]} does the same."
    if tool in TERMINAL_TOOLS:
        return (f"{tool} asks a person at the node's terminal, and an MCP call has none - not an MCP tool. Your "
                "client's own approval prompt is that person.")
    grouped = grouped_in(tool)
    if grouped and grouped[0] != tool:
        return f"{tool} is the MCP tool {grouped[0]} with {GROUPED_TOOLS[grouped[0]][0]} {grouped[1]!r}."
    if tool in RENAMED_TOOLS:
        return f"{tool} is called {RENAMED_TOOLS[tool]} since 1.3.0."
    return ""

#: Commands that only read, whose names say otherwise.
READ_ONLY_COMMANDS = frozenset({"due-check", "key-status", "read-file", "self-status", "smb-list",
                                # 1.3.0, 1.435: a retention rule's plan - its `delete` is refused since 0.38.0.
                                "prune-backup-files",
                                # 1.1.0, 1.421: SQL of their own, the request's names written as literals;
                                # query-plan's statement is compiled with SHOWPLAN / EXPLAIN, never run.
                                "table-info", "object-source", "db-activity", "query-plan",
                                "sqlserver-precheck", "sqlserver-verify-build", "sqlserver-verify-instance"})
# Not among them since 1.1.0 (1.415): `metric-batch` starts a program on the node or ships a script to a host,
# `guide` writes AGENTS.md when its request says so, `inventory-summary` writes its file and may merge an overlay
# into the inventory. Each stays a tool; the client is told it changes something.

#: ``run-sql`` is two tools (the directory refuses one tool that both reads and writes): a read-only one
#: under the command's own name - one statement held to ``lib.read_only_sql``, never committed - and
#: this one, which runs SQL that changes data, kept only when its request says ``commit``.
SQL_WRITE_TOOL = "run-sql-write"
#: ``run-sql``'s paragraph on whose rights it runs with - not ``run-sql-write``'s, which says it changes data.
RUN_SQL_RIGHTS = ("Runs with the rights of the login this node stores for the server (users.json: the instance's "
                  "default_credential_name, or credential_name): what that login may do, this may do - DML and DDL "
                  "included. The statement is checked to be one SELECT and the batch is rolled back, but the check reads "
                  "text and does not bound what the login can do; a login with read rights only is what keeps it to "
                  "reading. ")
#: The SQL each engine reads - a custom query tool names its language's reference (review criteria).
SQL_REFERENCES = ("SQL Server T-SQL https://learn.microsoft.com/sql/t-sql/language-reference, "
                  "PostgreSQL https://www.postgresql.org/docs/current/sql-commands.html, "
                  "Oracle https://docs.oracle.com/en/database/oracle/oracle-database/23/sqlrf/, "
                  "MySQL https://dev.mysql.com/doc/refman/8.4/en/sql-statements.html")


#: **How a server is reached is the inventory's, never an MCP request's** (1.1.0, 1.415). A ``sql_access`` block
#: may start a program on the node (``launcher``, ``tool_dir``, ``python_exe``) or send the login it was completed
#: with - or any secret of the store, by its ref - to an address (``bridge_url``). A server whose inventory says so
#: is still reached that way: the node completes it from ``server_id``.
SQL_ACCESS_ROUTES = frozenset({"launcher", "tool_dir", "python_exe", "bridge_url", "secret_ref", "connect_ref"})

#: Where a tool that only reads may put the file its request asks for, under the node's own root.
OUTPUT_FOLDERS = ("runtime/output/", "runtime/instance_bundles/")
OUTPUT_FIELDS = ("output_path", "output_dir")

#: The node's own secret files: never read through ``read-file``, whichever host it is asked of.
SECRET_FILES = re.compile(r"(^|/)(ssh_keys|secrets)/|(^|/)[^/]*secret_text[^/]*\.json$|(^|/)[^/]*\.env$", re.IGNORECASE)

#: ``prelude`` over MCP: what ``lib.sql_text.build_parameter_prelude`` writes - variables declared, their values
#: bound - and nothing else, because it is put in front of the checked statement.
DECLARE_PRELUDE = re.compile(
    r"(?:\s*DECLARE\s+@[A-Za-z_][A-Za-z0-9_]{0,63}\s+[A-Za-z0-9_]+(?:\(\s*(?:\d+|max)\s*(?:,\s*\d+\s*)?\))?"
    r"(?:\s*=\s*\?)?\s*;)+\s*", re.IGNORECASE)

#: How often a substitution may bring another one before it is refused.
_DEFINE_ROUNDS = 10


class McpToolsError(errors.ConfigError):
    """``mcp_tools.json`` missing a field, or a field of the wrong kind."""


@dataclass(frozen=True)
class McpSettings:
    enabled: bool
    log_calls: bool
    max_chars: int
    timeout_seconds: int
    store_query_max_rows: int
    store_query_timeout_seconds: int
    store_blocked_tables: tuple[str, ...]
    disabled_tools: tuple[str, ...]

    def offers(self, tool: str) -> bool:
        off = {tool, *FORMER_NAMES.get(tool, ())} & set(self.disabled_tools)
        return self.enabled and not off and not not_an_mcp_tool(tool)


def _int(document: dict[str, Any], field: str, *, low: int) -> int:
    value = document[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise McpToolsError(f"{FILE_NAME}: {field} is a whole number, {low} or more; got {value!r}.")
    return value


def _names(document: dict[str, Any], field: str) -> tuple[str, ...]:
    value = document[field]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise McpToolsError(f"{FILE_NAME}: {field} is a list of names; got {value!r}.")
    return tuple(item.strip() for item in value)


def parse_mcp_tools(document: Any) -> McpSettings:
    """The file's settings - every field stated (R50), each of its kind."""
    if not isinstance(document, dict):
        raise McpToolsError(f"{FILE_NAME} is one JSON object.")
    missing = [field for field in DEFAULTS if field not in document]
    if missing:
        raise McpToolsError(f"{FILE_NAME} does not state {', '.join(missing)} - every field is stated; "
                            "`dbabrain common upgrade-config` writes the shipped value.")
    unknown = sorted(set(document) - set(DEFAULTS))
    if unknown:
        raise McpToolsError(f"{FILE_NAME}: unknown field(s) {', '.join(unknown)}.")
    for field in ("enabled", "log_calls"):
        if not isinstance(document[field], bool):
            raise McpToolsError(f"{FILE_NAME}: {field} is true or false; got {document[field]!r}.")
    return McpSettings(
        enabled=document["enabled"], log_calls=document["log_calls"],
        max_chars=_int(document, "max_chars", low=1000), timeout_seconds=_int(document, "timeout_seconds", low=10),
        store_query_max_rows=_int(document, "store_query_max_rows", low=1),
        store_query_timeout_seconds=_int(document, "store_query_timeout_seconds", low=1),
        store_blocked_tables=_names(document, "store_blocked_tables"),
        disabled_tools=_names(document, "disabled_tools"))


def state_mcp_tools(document: dict[str, Any]) -> list[str]:
    """Write the shipped value of every field the document does not state; the fields written."""
    added = [field for field in DEFAULTS if field not in document]
    for field in added:
        value = DEFAULTS[field]
        document[field] = list(value) if isinstance(value, list) else value
    return added


# --------------------------------------------------------------------------- #
# The tools: one per common.cli command, from the reference
# --------------------------------------------------------------------------- #
def command_entries(reference: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each ``common.cli`` command and its ``input_`` entry - the reference names every one (R16).
    A command the reference says is refused (:data:`REFUSED_OUTPUT`) is left out."""
    refused = {str(command) for item in reference if item.get("object") == REFUSED_OUTPUT
               for command in item.get("commands") or []}
    entries: dict[str, dict[str, Any]] = {}
    for item in reference:
        if item.get("kind") == "input":
            for command in item.get("commands") or []:
                if str(command) not in refused:
                    entries[str(command)] = item
    return dict(sorted(entries.items()))


def is_destructive(command: str, operations: set[str]) -> bool:
    """True for a command the emergency-operations catalogue prices, or one whose name takes away - unless it
    is one of the READ_ONLY_COMMANDS whose name says otherwise (``prune-backup-files`` only plans)."""
    if command in READ_ONLY_COMMANDS and command not in operations:
        return False
    return command in operations or command.startswith(DESTRUCTIVE_PREFIXES)


def secrets_carried(command: str, request: Any) -> list[str]:
    """Where a request holds a secret value - ``password``, ``connection.password``, ``secret-set``'s
    ``value`` ... - as dotted paths; empty when it holds none. An empty value is not one."""
    found: list[str] = []
    if command in SECRET_VALUE_COMMANDS and isinstance(request, dict) and request.get("value") not in (None, ""):
        found.append("value")

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                where = f"{path}.{key}" if path else str(key)
                if str(key).lower() in SECRET_KEYS and item not in (None, "", [], {}):
                    found.append(where)
                else:
                    walk(item, where)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(request, "")
    return found


def routes_stated(request: Any) -> list[str]:
    """Where a request states how a server is reached - dotted paths; empty when nowhere. ``{"method": "direct"}``
    is the ordinary connection, and not one."""
    found: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                where = f"{path}.{key}" if path else str(key)
                if key != "sql_access":
                    walk(item, where)
                elif item not in (None, "", {}) and (
                        not isinstance(item, dict) or SQL_ACCESS_ROUTES & set(item)
                        or str(item.get("method") or "direct").strip().lower() != "direct"):
                    found.append(where)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(request, "")
    return found


def written_outside(request: dict[str, Any]) -> list[str]:
    """The output fields naming a place a read-only tool may not write: outside :data:`OUTPUT_FOLDERS`."""
    bad = []
    for field in OUTPUT_FIELDS:
        value = request.get(field)
        if value in (None, ""):
            continue
        path = str(value).replace("\\", "/")
        inside = any(path.startswith(folder) and len(path) > len(folder) for folder in OUTPUT_FOLDERS)
        if not inside or ":" in path or any(part in ("", ".", "..") for part in path.rstrip("/").split("/")):
            bad.append(field)
    return bad


def is_secret_file(path: str) -> bool:
    """One of the node's own secret files - its store, its keys, an environment file."""
    return bool(SECRET_FILES.search(str(path or "").replace("\\", "/")))


def holds_a_secret(data: bytes, secrets: dict[str, str]) -> bool:
    """A value of the secret store in a file's bytes, as UTF-8 or UTF-16. Short values are left out: in a
    binary file eight characters or more are never a coincidence."""
    values = [str(value) for value in secrets.values() if len(str(value or "").strip()) >= 8]
    return any(value.encode(coding) in data for value in values for coding in ("utf-8", "utf-16-le"))


def read_only_request(request: dict[str, Any]) -> dict[str, Any]:
    """``run-sql``'s checked half (:data:`SQL_WRITE_TOOL` is the other): every field stays, and whatever becomes
    part of the SQL is held to the check the statement is (1.1.0, 1.415).

    ``define`` values are put into the text **before** it is checked: they were substituted after, so a value
    was SQL nothing had read. ``prelude`` is the declarations of bound values and nothing more. Never committed.
    A check of text, not a boundary: what the statement may do is what the stored login may do (:func:`is_read_only`)."""
    if request.get("sql_file"):
        raise ReadOnlyRefusal("run-sql takes the statement as `sql_text` (a file's SQL is not checked); "
                              f"{SQL_WRITE_TOOL} runs a file.")
    checked = dict(request)
    text = str(checked.pop("sql_text", None) or checked.get("sql") or "")
    for field in ("sql", "commit", "autocommit"):
        checked.pop(field, None)
    defines = checked.pop("define", None) or checked.pop("defines", None)
    checked.pop("defines", None)
    if defines is not None and not isinstance(defines, dict):
        raise ReadOnlyRefusal("`define` is an object: {name: value}.")
    for _round in range(_DEFINE_ROUNDS):
        expanded = expand_sqlplus_defines(text, defines)
        # A DEFINE line a value brought in would be read again by run-sql, after the check.
        if expanded == text:
            break
        text = expanded
    else:
        raise ReadOnlyRefusal("the substitutions do not end - a `define` value names itself.")
    checked["sql_text"] = read_only_sql(text)
    prelude = str(checked.get("prelude") or "")
    if prelude.strip() and not DECLARE_PRELUDE.fullmatch(prelude):
        raise ReadOnlyRefusal("`prelude` only declares bound values here: DECLARE @name type = ?; - one per value "
                              "in `params`.")
    if any(mark in str(checked.get("driver") or "") for mark in "/\\"):
        raise ReadOnlyRefusal("`driver` is a driver's name, not a file.")
    return checked


def takes_a_secret_only(command: str, entry: dict[str, Any], *, filled: Any = ()) -> bool:
    """A command that over MCP could only be refused - its purpose is a secret, or it cannot run without
    one the node does not fill (``filled``: what ``request_fill`` completes for it): not offered as a tool
    (as a refused command is not). The caller says what is filled - this module reads no data folder."""
    if command in SECRET_VALUE_COMMANDS:
        return True
    filled = set(filled or ())
    return any(str(field.get("field")) in SECRET_KEYS and field.get("required") and field.get("field") not in filled
               for field in entry.get("fields") or [])


def is_read_only(command: str, operations: set[str]) -> bool:
    """Only reads. Every other tool is marked destructive - the directory takes one of the two hints.

    Not ``run-sql`` (1.1.0, the operator 2026-11-23: *do not call run-sql read-only - it can still run DML and
    DDL; say plainly that it runs with the rights of the login stored in dbabrain*). Its check reads text, and
    what bounds it is the login the node completes the request with."""
    return not is_destructive(command, operations) and (command.startswith(READ_ONLY_PREFIXES)
                                                        or command in READ_ONLY_COMMANDS)


def needs_key(tool: str, operations: set[str]) -> bool:
    """Does this tool need a dbabrain key on the node (1.1.0, 1.425)?

    The operator, 2026-11-25 (translated): *make MCP let a few tools be called without signing in*. The line is the
    hint the client already shows: a tool that only reads (``readOnlyHint``) answers with no key, so an agent can look
    at an estate before anyone has signed in; a tool marked destructive - ``run-sql`` among them, whose check reads
    text - needs the key, as the whole server did in 1.0.0."""
    return tool not in STORE_TOOLS and not is_read_only(tool, operations)


def operation_commands(catalogue: dict[str, Any]) -> set[str]:
    """The commands ``emergency_operations.json`` prices - ``host-service (stop)`` is ``host-service``."""
    return {str(name).split(" (", 1)[0].strip() for name in (catalogue.get("operations") or {})}


#: The request fields an MCP call never states: the node fills each from its inventory and encrypted store, by the
#: kind ``data_sources.request_fill.FILLS`` names - and one stated by the agent would carry the login itself.
FILLED_FIELDS: dict[str, tuple[str, ...]] = {
    "connection": ("connection",), "access": ("access",), "policy": ("policy",), "instance_policy": ("policy",),
    "rules": ("rules",), "secrets": ("secrets",), "bridge_secrets": ("secrets",)}
#: Commands whose fills reach two servers (``source`` / ``destination``), not one ``server_id``. ``metric-batch`` is
#: not one: its ``target`` object of connection facts is what a ``server_id`` replaces (1.1.0, 1.415), and offering
#: that object as required would lead an agent to write a login into it.
FILLED_PER_SIDE = frozenset({"relay-file", "copy-schema"})
_JSON_TYPES = frozenset({"string", "boolean", "object", "integer", "array", "number"})


def request_schema(command: str, entry: dict[str, Any], *, fills: tuple[str, ...] = (),
                   max_field_chars: int = 300) -> dict[str, Any]:
    """The JSON schema of a command's ``request``, from its reference entry: each field's type, its allowed values
    and bounds, what it is for, and which are required.

    A tool took one ``request`` object with no properties, its fields listed only as prose: Glama's score of the
    server (2026-11-26) read that as parameters left undocumented, and an agent had to parse a paragraph to build a
    call. The fields the node fills are left out, and ``server_id`` leads instead - the agent names the server and
    never sees, or sends, its login. So are the fields an MCP request is refused for carrying - a secret
    (:data:`SECRET_KEYS`) and how a server is reached (``sql_access``, :data:`SQL_ACCESS_ROUTES`): a schema must not
    offer what the call would refuse."""
    hidden = {name for kind in fills for name in FILLED_FIELDS.get(kind, ())} | SECRET_KEYS | {"sql_access"}
    properties: dict[str, Any] = {}
    required: list[str] = []
    names_a_server = bool(fills) and command not in FILLED_PER_SIDE
    if names_a_server:
        properties["server_id"] = {
            "type": "string",
            "description": "The server, as list-targets names it (or `target`); its login is completed from this "
                           "node's inventory and encrypted secret store."}
    for field in entry.get("fields") or []:
        name = str(field.get("field") or "")
        if not name or name in hidden or name in properties:
            continue
        kind = str(field.get("type") or "")
        constraint = field.get("constraint") or {}
        prop: dict[str, Any] = {"type": kind} if kind in _JSON_TYPES else {}
        if constraint.get("enum"):
            prop["enum"] = list(constraint["enum"])
        if kind in ("integer", "number"):
            for key, word in (("min", "minimum"), ("max", "maximum")):
                if isinstance(constraint.get(key), (int, float)) and not isinstance(constraint.get(key), bool):
                    prop[word] = constraint[key]
        if kind == "array" and constraint.get("item_kind") in _JSON_TYPES:
            prop["items"] = {"type": constraint["item_kind"]}
        purpose = " ".join(str(field.get("purpose") or "").split())
        if len(purpose) > max_field_chars:
            purpose = purpose[: max_field_chars - 3] + "..."
        if purpose:
            prop["description"] = purpose
        properties[name] = prop
        # `target` and `server_id` name the same server: either one is enough.
        if field.get("required") and not (names_a_server and name == "target"):
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def describe(command: str, entry: dict[str, Any], *, max_field_chars: int = 220, list_fields: bool = True) -> str:
    """The text Claude reads for a command's tool: what it does, then each field of its request.

    An input entry several commands share has one ``one_line`` for all of them, so their tools opened alike and an
    agent could not tell ``table-info`` from ``query-plan`` (Glama's score of the server, 2026-11-26:
    disambiguation 1/5). Such an entry's ``command_lines`` gives each command its own first sentence.

    What every tool has in common - a server named by ``server_id`` with its login completed by the node, and an
    emergency operation's ``confirm`` + ``reason`` - is said once, in the server's instructions. Said again in each
    tool it was two of a tool's three sentences, so ``disable-job`` and ``start-job`` read 97% alike."""
    # 1.439: the tool's own text (lib.mcp_tool_text) - what it does, a verb first, and which sibling to take
    # instead; no "dbabrain common.cli `x`:" before it, which pushed that out of the first words an agent reads.
    own = TOOL_TEXT.get(command) or (entry.get("command_lines") or {}).get(command)
    lines = [str(own or entry.get("one_line") or "").strip()]
    if command == "run-sql":
        lines.append(RUN_SQL_RIGHTS + f"The SQL is the target engine's: {SQL_REFERENCES}.")
    if not list_fields:
        # The tool's input schema carries every field (request_schema); the prose list would say it twice.
        return "\n".join(lines)
    lines.append("Request fields (pass them as `request`):")
    for field in entry.get("fields") or []:
        name = str(field.get("field") or "")
        purpose = " ".join(str(field.get("purpose") or "").split())
        if len(purpose) > max_field_chars:
            purpose = purpose[: max_field_chars - 3] + "..."
        lines.append(f"- {name}{' (required)' if field.get('required') else ''}: {purpose}")
    return "\n".join(lines)


def grouped_schema(tool: str, schema: dict[str, Any]) -> dict[str, Any]:
    """A grouped tool's request: its commands' own (one entry for all of them), with the choosing field first."""
    field, choices, text = GROUPED_TOOLS[tool]
    chooser = {"type": "string", "enum": list(choices), "description": text}
    return {**schema, "properties": {field: chooser, **(schema.get("properties") or {})},
            "required": [field, *[name for name in schema.get("required") or [] if name != field]]}


def describe_sql_write(entry: dict[str, Any], *, list_fields: bool = True) -> str:
    """``run-sql-write``'s text: the write half of ``run-sql``. It opens with a sentence of its own - opening with
    ``run-sql``'s would leave the two tools alike to an agent choosing between them - and leaves out ``run-sql``'s
    paragraph on its SELECT check, which is not this tool's."""
    text = describe("run-sql", entry, list_fields=list_fields)
    rest = text.split("\n", 1)[1] if "\n" in text else ""
    return SQL_WRITE_TEXT + "\n" + rest.replace(RUN_SQL_RIGHTS, "")


def capped(text: str, max_chars: int) -> str:
    """``text``, cut at ``max_chars`` with a last line saying how much more there was."""
    if len(text) <= max_chars:
        return text
    rest = len(text) - max_chars
    return text[:max_chars] + f"\n[cut: {rest} more character(s) - narrow the request, or raise max_chars in {FILE_NAME}]"
