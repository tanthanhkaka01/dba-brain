"""What the MCP server offers, and on what terms - ``data/mcp_tools.json`` (0.38.0, 1.312-1.321).

The operator, 2026-11-11 (translated): *let MCP run every common cli, SQL included, and read the
dbabrain store.* So the server (``dbabrain mcp serve``) offers **one tool per ``common.cli``
command** - the list read from the reference (``shared_config_objects.json``), where every command
already has its ``input_`` entry, so a new command is a new tool with nothing written here - and two
tools of its own on the runtime store, ``store_schema`` and ``store_query`` (one SELECT, read-only,
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

from dataclasses import dataclass
from typing import Any

from dbabrain.lib import errors

FILE_NAME = "mcp_tools.json"

#: The two tools on the runtime store - not common.cli commands, the server's own.
STORE_TOOLS = ("store_schema", "store_query")

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
                      "verify-", "describe", "show-", "guide", "store-", "backup-history", "inventory-summary",
                      "restore-drill-status", "sql-run-history")


#: Commands that only read, whose names say otherwise.
READ_ONLY_COMMANDS = frozenset({"due-check", "read-file", "self-status", "smb-list", "metric-batch",
                                "sqlserver-precheck", "sqlserver-verify-build", "sqlserver-verify-instance"})

#: ``run-sql`` is two tools (the directory refuses one tool that both reads and writes): a read-only one
#: under the command's own name - one statement held to ``lib.read_only_sql``, never committed - and
#: this one, which runs SQL that changes data, kept only when its request says ``commit``.
SQL_WRITE_TOOL = "run-sql-write"

#: The SQL each engine reads - a custom query tool names its language's reference (review criteria).
SQL_REFERENCES = ("SQL Server T-SQL https://learn.microsoft.com/sql/t-sql/language-reference, "
                  "PostgreSQL https://www.postgresql.org/docs/current/sql-commands.html, "
                  "Oracle https://docs.oracle.com/en/database/oracle/oracle-database/23/sqlrf/, "
                  "MySQL https://dev.mysql.com/doc/refman/8.4/en/sql-statements.html")


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
        return self.enabled and tool not in self.disabled_tools


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
    """True for a command the emergency-operations catalogue prices, or one whose name takes away."""
    return command in operations or command.startswith(DESTRUCTIVE_PREFIXES)


def is_read_only(command: str, operations: set[str]) -> bool:
    """Only reads. Every other tool is marked destructive - the directory takes one of the two hints."""
    if command == "run-sql":
        return True                       # the read-only half; SQL_WRITE_TOOL is the other
    return not is_destructive(command, operations) and (command.startswith(READ_ONLY_PREFIXES)
                                                        or command in READ_ONLY_COMMANDS)


def operation_commands(catalogue: dict[str, Any]) -> set[str]:
    """The commands ``emergency_operations.json`` prices - ``host-service (stop)`` is ``host-service``."""
    return {str(name).split(" (", 1)[0].strip() for name in (catalogue.get("operations") or {})}


def describe(command: str, entry: dict[str, Any], *, fills: bool, destructive: bool, max_field_chars: int = 220
             ) -> str:
    """The text Claude reads for a command's tool: what it does, then each field of its request."""
    lines = [f"dbabrain common.cli `{command}`: {entry.get('one_line') or ''}".strip()]
    if command == "run-sql":
        lines.append(f"Read-only: one SELECT (or WITH ... SELECT), checked before it runs and always rolled back. "
                     f"The SQL is the target engine's: {SQL_REFERENCES}.")
    if fills:
        lines.append("`server_id` (or `target`) names the server; its connection or host login is completed from "
                     "this node's inventory and encrypted secret store, so a request carries no password.")
    if destructive:
        lines.append("Changes or removes something. A command on the emergency-operations list takes `confirm`: "
                     "\"yes\" and a `reason` in its request, as on the command line.")
    lines.append("Request fields (pass them as `request`):")
    for field in entry.get("fields") or []:
        name = str(field.get("field") or "")
        purpose = " ".join(str(field.get("purpose") or "").split())
        if len(purpose) > max_field_chars:
            purpose = purpose[: max_field_chars - 3] + "..."
        lines.append(f"- {name}{' (required)' if field.get('required') else ''}: {purpose}")
    return "\n".join(lines)


def describe_sql_write(entry: dict[str, Any]) -> str:
    """``run-sql-write``'s text: the write half of ``run-sql``."""
    return describe("run-sql", entry, fills=True, destructive=True).replace(
        "dbabrain common.cli `run-sql`:", "dbabrain common.cli `run-sql` (writing):", 1).replace(
        "Read-only: one SELECT (or WITH ... SELECT), checked before it runs and always rolled back. ", "") + (
        f"\nRuns SQL that changes data; what it changed is kept only when the request says `commit` (or "
        f"`autocommit`) true - otherwise rolled back. The SQL is the target engine's: {SQL_REFERENCES}.")


def capped(text: str, max_chars: int) -> str:
    """``text``, cut at ``max_chars`` with a last line saying how much more there was."""
    if len(text) <= max_chars:
        return text
    rest = len(text) - max_chars
    return text[:max_chars] + f"\n[cut: {rest} more character(s) - narrow the request, or raise max_chars in {FILE_NAME}]"
