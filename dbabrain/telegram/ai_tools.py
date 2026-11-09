"""The assistant's tools: what the model may read of dbabrain, and how (0.34.0, 1.260).

The operator, 2026-10-31: *give the AI agent the APIs, tools, context and data it needs - read only
for now; it proposes a Telegram command and the user runs it.* petweb's answer was to let the model
call the page's own GET routes as the asker; dbabrain's is to let it call what already exists:

* ``store_schema`` / ``store_query`` - the runtime store: job runs, metrics, SLA, the backup catalog,
  Telegram history, the assistant's own tables. One SELECT, in a read-only transaction that is
  always rolled back.
* ``list_files`` / ``read_file`` / ``grep_files`` - the tool root's configuration, assets, logs,
  results and the package source, inside ``file_roots`` and never a ``file_denied`` path.
* ``run_sql`` - a read-only SELECT on a monitored server, through the path ``/spbot_sql_to_xlsx``
  uses (``telegram.sql_commands.run_sql_to_xlsx`` -> ``common.cli run-sql``, never committed),
  behind its own switch and server list.
* ``bot_commands`` - the bot's commands the asker may run, so an answer proposes one exactly.

Three rules hold for every tool: **the output is redacted** of every value in the encrypted secret
store before it goes to the model or into the store; **it is cut** to ``tool_result_max_chars`` and
says so; **an error is a result** (``is_error``), never an exception out of the loop.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbabrain.lib import errors
from dbabrain.lib import ai_assist as A

# --------------------------------------------------------------------------- #
# The read-only check
# --------------------------------------------------------------------------- #
#: Words that make a statement more than a read, on any of the engines dbabrain reaches. Matched on
#: the text with its strings and comments taken out, so a column called 'updated_at' or a literal
#: 'DELETE' in a WHERE clause is not a refusal. ``INTO`` covers SELECT ... INTO, which creates.
WRITE_WORDS = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "DROP", "ALTER", "CREATE", "TRUNCATE", "RENAME",
    "GRANT", "REVOKE", "DENY", "COPY", "CALL", "DO", "EXEC", "EXECUTE", "VACUUM", "ANALYZE", "REINDEX",
    "CLUSTER", "LOCK", "UNLOCK", "SET", "RESET", "COMMIT", "ROLLBACK", "BEGIN", "SAVEPOINT", "PREPARE",
    "DEALLOCATE", "LISTEN", "NOTIFY", "ATTACH", "DETACH", "PRAGMA", "REFRESH", "SECURITY", "LOAD", "IMPORT",
    "INTO", "KILL", "RECONFIGURE", "SHUTDOWN", "BACKUP", "RESTORE", "DBCC", "BULK", "OPENROWSET", "OPENQUERY",
    "OPENDATASOURCE", "WAITFOR", "USE", "DECLARE", "CHECKPOINT", "PURGE", "FLASHBACK", "AUDIT", "NOAUDIT",
    "COMMENT", "UNDROP", "DISCARD",
)
#: Functions that act though they are called from a SELECT.
WRITE_FUNCTIONS = re.compile(
    r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_promote|set_config|"
    r"pg_sleep\w*|lo_\w+|dblink\w*|pg_read_file|pg_read_binary_file|pg_ls_\w+|pg_stat_file|pg_advisory\w*|"
    r"pg_switch_wal|pg_create_\w+|pg_drop_\w+|pg_replication_origin_\w+|pg_logical_emit_message|nextval|setval|"
    r"txid_current|pg_current_xact_id|xp_\w+|sp_\w+|dbms_\w+|utl_\w+)\s*\(", re.IGNORECASE)
_WRITE_WORDS = re.compile(r"\b(" + "|".join(WRITE_WORDS) + r")\b", re.IGNORECASE)
_STRINGS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\$\$.*?\$\$", re.DOTALL)
#: Literals only - a double-quoted name is an IDENTIFIER, and "web_users" is still that table.
_LITERALS = re.compile(r"'(?:[^']|'')*'|\$\$.*?\$\$", re.DOTALL)
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
#: A comment some engine RUNS: MySQL's /*! ... */ executes its text, so taking comments out would
#: hide whatever it holds from every check below.
_EXECUTED_COMMENT = re.compile(r"/\*\s*!")


class ReadOnlyRefusal(errors.RequestError):
    """A statement that is not one plain read."""


def read_only_sql(sql: str, *, blocked_tables: list[str] | tuple[str, ...] = ()) -> str:
    """The statement, trimmed, when it is one SELECT (or WITH ... SELECT) and touches no blocked
    table - else :class:`ReadOnlyRefusal` naming why. A guard, not the only one: the store query
    runs in a read-only transaction, and a monitored server's run is never committed."""
    text = str(sql or "").strip().rstrip(";").strip()
    if not text:
        raise ReadOnlyRefusal("the SQL is empty.")
    if _EXECUTED_COMMENT.search(_LITERALS.sub("''", text)):
        raise ReadOnlyRefusal("a /*! ... */ comment is executed by MySQL - write the statement without it.")
    bare = _COMMENTS.sub(" ", _STRINGS.sub("''", text))
    names = _COMMENTS.sub(" ", _LITERALS.sub("''", text))
    if ";" in bare:
        raise ReadOnlyRefusal("one statement only - no ';' inside it.")
    first = bare.strip().split(None, 1)[0].upper() if bare.strip() else ""
    if first not in ("SELECT", "WITH", "(", "VALUES") and not first.startswith("("):
        raise ReadOnlyRefusal(f"only a SELECT (or WITH ... SELECT) is run; this starts with {first or 'nothing'}.")
    word = _WRITE_WORDS.search(bare)
    if word:
        raise ReadOnlyRefusal(f"{word.group(1).upper()} is not allowed - the tools only read.")
    function = WRITE_FUNCTIONS.search(bare)
    if function:
        raise ReadOnlyRefusal(f"{function.group(1)}() is not allowed - it acts, it does not read.")
    for table in blocked_tables:
        if re.search(rf"\b{re.escape(table)}\b", names, re.IGNORECASE):
            raise ReadOnlyRefusal(f"table {table} is not readable by the assistant.")
    return text


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
class Redactor:
    """Every value of the encrypted secret store, taken out of a text wherever it appears."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        values = {str(value) for value in (secrets or {}).values() if len(str(value or "").strip()) >= 4}
        spellings: set[str] = set()
        for value in values:
            spellings.add(value)
            spellings.add(json.dumps(value)[1:-1])  # as it appears inside JSON
        # Longest first, so a secret that contains another is taken out whole.
        self.values = sorted(spellings, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        from dbabrain.telegram.command_base import mask_sensitive_text

        out = str(text)
        for value in self.values:
            if value and value in out:
                out = out.replace(value, "***")
        return mask_sensitive_text(out)


# --------------------------------------------------------------------------- #
# The context every tool runs in
# --------------------------------------------------------------------------- #
@dataclass
class ToolContext:
    settings: A.AiAssistSettings
    store_target: Any  # dbabrain.db.backend.StoreTarget
    tool_root: Path
    commands_path: Path
    users_path: Path
    asker_user_id: str
    redact: Redactor = field(default_factory=Redactor)
    data_dir: Path | None = None


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False
    row_count: int | None = None


def _ok(payload: Any, *, rows: int | None = None) -> ToolOutput:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    return ToolOutput(text, False, rows)


def _error(message: str) -> ToolOutput:
    return ToolOutput(f"ERROR: {message}", True)


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def _store_schema(context: ToolContext, args: dict[str, Any]) -> ToolOutput:
    table = str(args.get("table") or "").strip()
    blocked = {name.lower() for name in context.settings.store_query_blocked_tables}
    if table and table.lower() in blocked:
        return _error(f"table {table} is not readable by the assistant.")
    with context.store_target.connect() as conn:
        if context.store_target.is_sqlite:
            names = [row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name").fetchall()]
            columns = {name: [(row["name"], row["type"]) for row in conn.execute(f'PRAGMA table_info("{name}")')]
                       for name in names if not table or name == table}
        else:
            binds: tuple[Any, ...] = ()
            sql = ("SELECT table_name, column_name, data_type FROM information_schema.columns "
                   "WHERE table_schema = current_schema()")
            if table:
                sql += " AND table_name = ?"
                binds = (table,)
            columns = {}
            for row in conn.execute(sql + " ORDER BY table_name, ordinal_position", binds).fetchall():
                columns.setdefault(row["table_name"], []).append((row["column_name"], row["data_type"]))
        conn.rollback()
    columns = {name: cols for name, cols in columns.items() if name.lower() not in blocked}
    if table:
        if table not in columns:
            return _error(f"no table {table} in the store.")
        return _ok({"table": table, "columns": [{"name": name, "type": kind} for name, kind in columns[table]]})
    return _ok({"tables": {name: ", ".join(col for col, _kind in cols) for name, cols in sorted(columns.items())}},
               rows=len(columns))


def _store_query(context: ToolContext, args: dict[str, Any]) -> ToolOutput:
    settings = context.settings
    try:
        sql = read_only_sql(str(args.get("sql") or ""), blocked_tables=settings.store_query_blocked_tables)
    except ReadOnlyRefusal as exc:
        return _error(str(exc))
    limit = min(int(args.get("max_rows") or settings.store_query_max_rows), settings.store_query_max_rows)
    with context.store_target.connect() as conn:
        try:
            if context.store_target.is_sqlite:
                conn.execute("PRAGMA query_only = ON")
            else:
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute(f"SET LOCAL statement_timeout = {int(settings.store_query_timeout_seconds) * 1000}")
            cursor = conn.execute(sql)
            names = [item[0] for item in (cursor.description or [])]
            rows = cursor.fetchmany(limit + 1) if hasattr(cursor, "fetchmany") else cursor.fetchall()[: limit + 1]
        finally:
            conn.rollback()
            if context.store_target.is_sqlite:
                conn.execute("PRAGMA query_only = OFF")
    truncated = len(rows) > limit
    rows = rows[:limit]
    values = [[row[index] for index in range(len(names))] for row in rows]
    return _ok({"columns": names, "rows": values, "row_count": len(values), "truncated": truncated}, rows=len(values))


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
#: The readable root that is the package's own source. In a source checkout and in the image it is a
#: folder of the tool root; a wheel installs it in site-packages, and the tool root holds no
#: ``dbabrain/`` at all - so on a new install the assistant could not read the guide it is told to
#: follow (``dbabrain/common/agents_guide.md``), and said so (1.267, 2026-11-06).
PACKAGE_ROOT = "dbabrain"


def _package_base(context: ToolContext) -> Path | None:
    """The folder ``dbabrain/...`` paths are read under when the tool root has no package of its own:
    the installed package's parent (site-packages). ``None`` when the tool root has it."""
    if (context.tool_root / PACKAGE_ROOT).is_dir():
        return None
    import dbabrain

    return Path(dbabrain.__file__).resolve().parent.parent


def _relative(context: ToolContext, path: Path) -> str:
    try:
        return path.relative_to(context.tool_root).as_posix()
    except ValueError:
        base = _package_base(context)
        if base is None:
            raise
        return path.relative_to(base).as_posix()


def _denied(context: ToolContext, relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    for pattern in context.settings.file_denied:
        simple = pattern.replace("**/", "")
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative, simple) or fnmatch.fnmatch(name, simple):
            return True
        if "/" in simple.strip("*/") and f"/{simple.strip('*/')}/" in f"/{relative}/":
            return True
    return False


def _resolve(context: ToolContext, raw: str, *, allow_root: bool = False) -> Path:
    """A path inside one of ``file_roots`` and not denied - else ``PermissionError`` naming why.

    The tool root itself only to ``list_files``, which answers it with the readable roots and
    nothing else: anything that walked it (``grep_files`` on ``.``) would read every folder the
    roots leave out."""
    text = str(raw or "").strip().replace("\\", "/").lstrip("/")
    root = context.tool_root.resolve()
    package = _package_base(context)
    base = package if package is not None and text.split("/", 1)[0] == PACKAGE_ROOT else root
    target = (base / text).resolve() if text else root
    try:
        relative = target.relative_to(base).as_posix()
    except ValueError:
        raise PermissionError(f"{raw!r} is outside the tool root.") from None
    if base is not root and relative != PACKAGE_ROOT and not relative.startswith(PACKAGE_ROOT + "/"):
        # "dbabrain/../x" under site-packages is another package's file, never a readable root.
        raise PermissionError(f"{raw!r} is outside the package.")
    roots = [item.strip("/") for item in context.settings.file_roots]
    if relative in ("", "."):
        if allow_root:
            return target
        raise PermissionError(f"name a path under the readable roots ({', '.join(roots)}).")
    if not any(relative == item or relative.startswith(item + "/") for item in roots):
        raise PermissionError(f"{relative} is not under the readable roots ({', '.join(roots)}).")
    if _denied(context, relative):
        raise PermissionError(f"{relative} is not readable by the assistant.")
    return target


def _list_files(context: ToolContext, args: dict[str, Any]) -> ToolOutput:
    try:
        target = _resolve(context, str(args.get("path") or ""), allow_root=True)
    except PermissionError as exc:
        return _error(str(exc))
    root = context.tool_root.resolve()
    if target == root:
        entries = [{"path": item, "readable_root": True} for item in context.settings.file_roots
                   if (root / item).exists() or (item == PACKAGE_ROOT and _package_base(context) is not None)]
        return _ok({"path": ".", "entries": entries}, rows=len(entries))
    if not target.exists():
        return _error(f"{_relative(context, target)} does not exist on this node.")
    if target.is_file():
        stat = target.stat()
        return _ok({"path": _relative(context, target), "size_bytes": stat.st_size,
                    "modified_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime))})
    entries = []
    for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        relative = _relative(context, child)
        if _denied(context, relative):
            continue
        stat = child.stat()
        entries.append({"name": child.name + ("/" if child.is_dir() else ""),
                        "size_bytes": None if child.is_dir() else stat.st_size,
                        "modified_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime))})
        if len(entries) >= 500:
            break
    return _ok({"path": _relative(context, target), "entries": entries}, rows=len(entries))


def _text_of(path: Path) -> str:
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise ValueError("a binary file is not read.")
    return data.decode("utf-8", errors="replace")


def _read_file(context: ToolContext, args: dict[str, Any]) -> ToolOutput:
    try:
        target = _resolve(context, str(args.get("path") or ""))
    except PermissionError as exc:
        return _error(str(exc))
    if not target.is_file():
        return _error(f"{args.get('path')} is not a file on this node.")
    try:
        lines = _text_of(target).splitlines()
    except ValueError as exc:
        return _error(str(exc))
    find = str(args.get("find") or "")
    if find:
        try:
            pattern = re.compile(find, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(find), re.IGNORECASE)
        picked = [(number, line) for number, line in enumerate(lines, start=1) if pattern.search(line)]
    else:
        offset = max(int(args.get("offset") or 1), 1)
        limit = max(int(args.get("limit") or 400), 1)
        picked = list(enumerate(lines, start=1))[offset - 1: offset - 1 + limit]
    out, size = [], 0
    for number, line in picked:
        entry = f"{number}: {line}"
        size += len(entry) + 1
        if size > context.settings.file_max_chars:
            out.append(f"... cut at {context.settings.file_max_chars} characters; read on with offset={number}.")
            break
        out.append(entry)
    header = f"{_relative(context, target)} ({len(lines)} lines)"
    return _ok(header + "\n" + "\n".join(out), rows=len(out))


def _grep_files(context: ToolContext, args: dict[str, Any]) -> ToolOutput:
    raw = str(args.get("pattern") or "")
    if not raw:
        return _error("pattern is required.")
    flags = re.IGNORECASE if args.get("ignore_case", True) else 0
    try:
        pattern = re.compile(raw, flags)
    except re.error as exc:
        return _error(f"pattern is not a regular expression: {exc}")
    glob = str(args.get("glob") or "")
    asked = str(args.get("path") or "").strip().strip("/\\.")
    # The tool root ("", ".", "/") is not a place to search: it means every readable root.
    starts = [str(args.get("path"))] if asked else list(context.settings.file_roots)
    matches: list[str] = []
    limit = context.settings.grep_max_matches
    for start in starts:
        try:
            base = _resolve(context, start)
        except PermissionError as exc:
            if asked:
                return _error(str(exc))
            continue
        if not base.exists():
            # Said, not "no match": a guide searched at a path that is not there read as a guide
            # without the command (2026-11-06).
            if asked:
                return _error(f"{_relative(context, base)} does not exist on this node.")
            continue
        files = [base] if base.is_file() else sorted(item for item in base.rglob("*") if item.is_file())
        for path in files:
            relative = _relative(context, path)
            if _denied(context, relative) or (glob and not fnmatch.fnmatch(path.name, glob)):
                continue
            try:
                if path.stat().st_size > context.settings.grep_max_file_bytes:
                    continue
                text = _text_of(path)
            except (OSError, ValueError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    matches.append(f"{relative}:{number}: {line[:500]}")
                    if len(matches) >= limit:
                        matches.append(f"... stopped at {limit} matches; narrow the pattern or the path.")
                        return _ok("\n".join(matches), rows=limit)
    return _ok("\n".join(matches) if matches else "no match.", rows=len(matches))


# --------------------------------------------------------------------------- #
# A monitored server
# --------------------------------------------------------------------------- #
def _run_sql(context: ToolContext, args: dict[str, Any]) -> ToolOutput:
    settings = context.settings
    server_id = str(args.get("server_id") or "").strip()
    if not server_id:
        return _error("server_id is required.")
    if settings.run_sql_server_ids and server_id not in settings.run_sql_server_ids:
        return _error(f"run_sql may not reach {server_id}; it may reach {', '.join(settings.run_sql_server_ids)}.")
    try:
        sql = read_only_sql(str(args.get("sql") or ""))
    except ReadOnlyRefusal as exc:
        return _error(str(exc))
    from dbabrain.telegram import sql_commands

    try:
        result = sql_commands.run_sql_to_xlsx(
            target=server_id, sql_text=sql, database=str(args.get("database") or ""),
            credential_name=settings.run_sql_credential_name,
            data_dir=context.data_dir, max_rows=settings.run_sql_max_rows,
            timeout_seconds=settings.run_sql_timeout_seconds)
    except sql_commands.SqlToXlsxError as exc:
        return _error(str(exc))
    if int(result.get("affected_rows") or 0) > 0:
        # Rolled back by run-sql; said, because a read that changed rows is not what was asked.
        result["note"] = "the statement reported affected rows; they were rolled back."
    return _ok({"server_id": result["server_id"], "database": result["database"],
                "login": result.get("username", ""), "columns": result["columns"], "rows": result["rows"],
                "row_count": result["row_count"], "truncated": result["truncated"], **({"note": result["note"]}
                                                                                    if "note" in result else {})},
               rows=int(result["row_count"]))


# --------------------------------------------------------------------------- #
# The bot's commands
# --------------------------------------------------------------------------- #
def _menu_descriptions(commands_path: Path) -> dict[str, str]:
    menu = commands_path.with_suffix(".md")
    descriptions: dict[str, str] = {}
    if menu.is_file():
        for line in menu.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^\s*(spbot_\w+)\s+-\s+(.+)$", line)
            if match:
                descriptions.setdefault(match.group(1), match.group(2).strip())
    return descriptions


def _command_notes(command: Any) -> str:
    """What a command says about itself when the BotFather menu is not on this node: its notes and
    what it answers on start - the worker carries the commands' JSON, not the menu file."""
    config = command.action_config or {}
    notes = [str(value) for key, value in sorted(config.items()) if key.startswith("_note") and value]
    if config.get("start_text"):
        notes.append(str(config["start_text"]))
    return " ".join(notes)[:600]


def _bot_commands(context: ToolContext, args: dict[str, Any]) -> ToolOutput:
    from dbabrain.telegram.command_permissions import load_support_commands, telegram_user_type
    from dbabrain.telegram.commands import can_run_command

    find = str(args.get("find") or "").lower()
    user_type = telegram_user_type(context.users_path, user_id=context.asker_user_id)
    descriptions = _menu_descriptions(context.commands_path)
    entries = []
    for command in load_support_commands(context.commands_path):
        text = command.command_text
        description = descriptions.get(text, "") or _command_notes(command)
        declared = [dict(item) for item in (command.action_config or {}).get("parameters") or []
                    if isinstance(item, dict) and (item.get("name") or item.get("key"))]
        searchable = " ".join([text, description, *(str(item.get("prompt_text") or "") for item in declared)]).lower()
        if find and find not in searchable:
            continue
        # Enough to write the command on ONE line: each argument's position, and which one takes the
        # rest of the line (a consume_rest parameter - everything after it is that one value).
        parameters = [{"name": str(item.get("name") or item.get("key")), "position": item.get("position"),
                       "required": bool(item.get("required")), "takes_rest_of_line": bool(item.get("consume_rest")),
                       "pattern": item.get("pattern"), "asks": str(item.get("prompt_text") or "")[:240]}
                      for item in declared if item.get("source", "arg") == "arg"]
        entries.append({
            "command": "/" + text, "description": description, "level": command.command_type,
            "parameters": parameters, "in_group": bool(command.is_group),
            "in_private_chat": bool(command.is_private),
            "asker_may_run_in_private_chat": bool(command.is_private) and can_run_command(
                allow_command=user_type, user_type=user_type, command_type=command.command_type),
        })
    return _ok({"asker_level": user_type, "commands": entries}, rows=len(entries))


# --------------------------------------------------------------------------- #
# Definitions and dispatch
# --------------------------------------------------------------------------- #
def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOLS: dict[str, dict[str, Any]] = {
    "store_schema": {
        "switch": "store_query_enabled", "run": _store_schema,
        "description": "List the tables of dbabrain's runtime store with their columns, or one table's columns "
                       "with types. Read this before writing a store_query.",
        "input_schema": _schema({"table": {"type": "string", "description": "One table; empty = every table."}}, []),
    },
    "store_query": {
        "switch": "store_query_enabled", "run": _store_query,
        "description": "Run ONE read-only SELECT (or WITH ... SELECT) on dbabrain's runtime store (PostgreSQL "
                       "or SQLite, see the context): job_runs, metric results, SLA results, backup_runs / "
                       "backup_files / restore_runs (the backup catalog), telegram_messages, ai_requests. "
                       "Times are UTC text 'YYYY-MM-DDTHH:MM:SSZ'. Rows are capped; aggregate in SQL.",
        "input_schema": _schema({"sql": {"type": "string"},
                                 "max_rows": {"type": "integer", "description": "Fewer rows than the cap."}},
                                ["sql"]),
    },
    "list_files": {
        "switch": "files_enabled", "run": _list_files,
        "description": "List a folder of the tool root (data/, assets/, logs/, runtime/output/, the dbabrain/ "
                       "package source). Empty path = the readable roots.",
        "input_schema": _schema({"path": {"type": "string"}}, []),
    },
    "read_file": {
        "switch": "files_enabled", "run": _read_file,
        "description": "Read a text file of the tool root: lines from offset (1-based) for limit lines, or only "
                       "the lines matching find (a regex, case-insensitive), each with its line number.",
        "input_schema": _schema({"path": {"type": "string"}, "offset": {"type": "integer"},
                                 "limit": {"type": "integer"}, "find": {"type": "string"}}, ["path"]),
    },
    "grep_files": {
        "switch": "files_enabled", "run": _grep_files,
        "description": "Search the tool root's files for a regex: every matching line as path:line: text. "
                       "Narrow with path (a folder or file) and glob (a file name pattern such as '*.log').",
        "input_schema": _schema({"pattern": {"type": "string"}, "path": {"type": "string"},
                                 "glob": {"type": "string"}, "ignore_case": {"type": "boolean"}}, ["pattern"]),
    },
    "run_sql": {
        "switch": "run_sql_enabled", "run": _run_sql,
        "description": "Run ONE read-only SELECT on a monitored database server by its server_id (DMVs, msdb "
                       "history, pg_stat_* views, v$ views, the application's tables). Never committed. "
                       "Find server_ids in data/db_instances.json. Keep it light: this is a production server.",
        "input_schema": _schema({"server_id": {"type": "string"}, "sql": {"type": "string"},
                                 "database": {"type": "string", "description": "Database to run in; empty = "
                                                                               "the instance's own."}},
                                ["server_id", "sql"]),
    },
    "bot_commands": {
        "switch": "bot_commands_enabled", "run": _bot_commands,
        "description": "The bot's /spbot_ commands: what each does, its parameters (position, required, the one "
                       "that takes the rest of the line), its level, and whether the asker may run it. Use it to "
                       "propose the exact command the user can run themselves, on one line, arguments in position "
                       "order; or the bare command, which asks one question at a time.",
        "input_schema": _schema({"find": {"type": "string", "description": "A word to filter by."}}, []),
    },
}


def tool_definitions(settings: A.AiAssistSettings) -> list[dict[str, Any]]:
    """The tools the model is offered: none when ``tools_mode`` is off, else every switched-on one,
    in a fixed order - the list is part of the cached prefix, so it never changes between questions."""
    if settings.tools_mode == "off":
        return []
    return [{"name": name, "description": spec["description"], "input_schema": spec["input_schema"]}
            for name, spec in TOOLS.items() if getattr(settings, spec["switch"])]


def run_tool(context: ToolContext, name: str, raw_input: Any) -> ToolOutput:
    """Run one tool by name; anything that goes wrong is a result, and every result is redacted."""
    spec = TOOLS.get(name)
    if spec is None or context.settings.tools_mode == "off" or not getattr(context.settings, spec["switch"]):
        return ToolOutput(f"ERROR: no tool {name} is available.", True)
    args = raw_input if isinstance(raw_input, dict) else {}
    try:
        output = spec["run"](context, args)
    except Exception as exc:  # noqa: BLE001 - a tool's failure is the model's to read, not the loop's to die of.
        output = _error(f"{type(exc).__name__}: {exc}")
    output.content = context.redact(output.content)
    return output


def for_the_model(output: ToolOutput, max_chars: int) -> str:
    """What the model reads: the result, cut to ``max_chars`` with the cut said."""
    if len(output.content) <= max_chars:
        return output.content
    return (output.content[:max_chars]
            + f"\n... cut: {len(output.content) - max_chars} more characters. Narrow the query or read in parts.")
