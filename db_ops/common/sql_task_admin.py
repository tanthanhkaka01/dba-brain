"""Register a SQL task, and register one target for it — the two halves, as commands.

``config_admin.add_sql_task`` covers the common case in one call: here is some SQL, here is the
one server it runs on, write the file and both config entries. Everything else was a hand-edit of
``data/sql_commands.json`` and ``data/sql_targets.json`` until 2026-09-18, and the estate has the
scars: a task whose ``input.parameter`` named a parameter the command did not declare, a
``script_path`` pointing at a file nobody had written yet, and — the one that started this module —
a task built by a python script at the keyboard because no command could say
``input_type: python`` at all.

Two commands rather than one, because they are two decisions:

* ``sql-command-add`` — **what runs**: the script or scripts, and, for a task whose rows do not
  start in a database, the program that fetches them (``input_type: "python"``).
* ``sql-target-add`` — **where it runs**: one server, one database, one schedule, one route for
  its output. A task that runs on Testing, UAT and production is one command and three of these,
  which is the shape ``{target_server_id}`` / ``{target_database}`` in ``input.args`` exists for.

Both refuse what the runner would refuse *later*, at the moment the config is written rather than
at 03:00 in a scheduled run: a missing script file, a placeholder no parameter declares, an
``input.parameter`` with no matching entry in ``parameters``, a ``target_no`` already taken. The
runner's own loader is the authority for the shape; this is the same check moved forward to where
somebody is watching.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from db_ops.common.config_admin import (
    DEFAULT_DATA_DIR,
    KNOWN_DB_TYPES,
    MANUAL_ONLY,
    SQL_TASK_NOTIFY_CHAT,
    TOOL_ROOT,
    ConfigAdminError,
    _atomic_write,
    _dump_json,
    _notify_rule_dict,
    _read_json,
    next_sql_id,
    next_target_no,
    normalize_time_window,
    slugify,
)
from db_ops.lib import task_output
from db_ops.lib.task_output import TaskOutputError

__all__ = [
    "USAGE_COMMAND",
    "USAGE_TARGET",
    "SqlTaskAdminError",
    "add_sql_command",
    "add_sql_target",
]


class SqlTaskAdminError(ValueError):
    """Anything refused here. Carries the reason a person can act on, never a traceback."""


#: The reserved names ``input.args`` may use without declaring them as parameters: the runner
#: fills them from the target it is running for. Kept literal rather than imported from
#: ``db_ops.sql_tasks.python_source`` — ``common`` is the API layer and does not import an app
#: (ORD 13), the same reason ``config_admin`` spells out the collector types it knows.
TARGET_PLACEHOLDERS = ("target_server_id", "target_database")

SCRIPT_TYPES = ("single", "array", "folder")
INPUT_TYPES = ("none", "python")

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")

USAGE_COMMAND = """usage: python -m db_ops.common.cli sql-command-add <json>|@<file>|- [--data-dir ...]

Registers WHAT a SQL task runs, in data/sql_commands.json. Where it runs is sql-target-add.

  {"sql_name": "Drain the working-hour queue",     // required
   "db_type": "sqlserver",                          // required: sqlserver|oracle|postgresql|mysql
   "script_type": "single",                         // single (default) | array | folder
   "script_path": "assets/tasks/sqlserver/030_x.sql",   // single/folder: a file that EXISTS
   "sql_text": "SELECT 1;",                         // ...or the SQL itself, and db_ops writes
                                                    //    the file (script_path then names WHERE)
   "script_paths": ["a.sql", "b.sql"],              // array, in run order
   "final_script_paths": ["roll_up.sql"],           // run ONCE after the last batch
   "sql_id": 30,                                    // optional; the next free one when absent
   "sql_code": "SQLSERVER-030-DRAIN",               // optional; derived from db_type + id + name
   "version_from": "2017", "version_to": null,
   "autocommit": false,
   "active": true,
   "note": "why this exists, and what it writes to",
   "replace": false,                                // overwrite an existing sql_id

   // A task whose rows do not start in a database names the program that fetches them:
   "input_type": "python",
   "input": {"script": "assets/tasks/python/get_rows.py",
             "args": ["--target", "{target_server_id}", "--database", "{target_database}"],
             "rows_path": "data", "parameter": "payload", "batch_rows": 2000,
             "timeout_seconds": 600, "accept_exit_codes": [0]},
   "parameters": [{"name": "payload", "type": "nvarchar(max)"}]}

Every script named must already exist: a command that points at a file nobody has written is a
task that fails on its schedule rather than here. {name} in input.args must be a declared
parameter, or one of the reserved target placeholders the runner fills per target:
  {target_server_id}  {target_database}
"""

USAGE_TARGET = """usage: python -m db_ops.common.cli sql-target-add <json>|@<file>|- [--data-dir ...]

Registers WHERE a SQL task runs, in data/sql_targets.json. One call per server.

  {"sql_id": 30,                                   // required; the command must exist
   "server_id": "ACME-192-0-2-111",                 // required
   "database_name": "PAYROLL_Test",
   "instance_name": "MSSQLSERVER", "service_name": "PAYROLL-DEV",
   "credential_name": "sqlserver_...",              // default: the instance's own
   "target_no": 2,                                  // optional; the next free one when absent
   "time_window": {"from_hour": 0, "to_hour": 23, "from_day": 1, "to_day": 31,
                   "repeat_interval": 60, "timeout": 600},
   "manual_only": false,                            // shortcut for repeat_interval -1
   "active": true,
   "output": "none",                                // none|plain|xlsx|csv|txt|xml|json
   "output_chat": "sql", "output_chat_id": "",
   "logging_on_run": false, "alert_on_error": true, // a 60-second task must not log every run
   "logging_chat": "sql", "error_chat": "sql",
   "note": "what makes this target different from the others",
   "replace": false}                                // overwrite an existing target_no

A task that runs on Testing, UAT and production is ONE command and three of these - not three
copies of the command, which is what the {target_*} placeholders in input.args are for.
"""


def _root(data_dir: str | Path | None, tool_root: str | Path | None) -> tuple[Path, Path]:
    data_root = Path(data_dir).resolve() if data_dir else DEFAULT_DATA_DIR
    root = Path(tool_root).resolve() if tool_root else TOOL_ROOT
    return data_root, root


def _require_file(root: Path, relpath: str, *, field: str) -> str:
    """A script path that resolves inside the tool root and exists. Both halves matter.

    Outside the root it cannot travel with the node — the same rule ``python_source`` applies to
    ``input.script`` — and missing, it is a task that will fail on a schedule with nobody reading.
    """
    text = str(relpath or "").strip().replace("\\", "/")
    if not text:
        raise SqlTaskAdminError(f"{field} is required.")
    resolved = (root / text).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise SqlTaskAdminError(
            f"{field} {text} resolves outside the tool root ({root}). A task's scripts live with "
            "the node that runs them.") from None
    if not resolved.exists():
        raise SqlTaskAdminError(f"{field} not found: {text}")
    return text


def _check_input_block(raw: Any, parameters: list[dict[str, Any]], root: Path) -> dict[str, Any]:
    """The ``input`` block of an ``input_type: "python"`` command, refused early when wrong."""
    if not isinstance(raw, dict):
        raise SqlTaskAdminError('input_type "python" requires an "input" object.')
    block: dict[str, Any] = {}
    block["script"] = _require_file(root, raw.get("script", ""), field="input.script")

    args = raw.get("args", [])
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise SqlTaskAdminError("input.args must be an array of strings.")
    declared = {str(p.get("name") or "").strip() for p in parameters}
    for arg in args:
        for name in _PLACEHOLDER.findall(arg):
            if name in TARGET_PLACEHOLDERS or name in declared:
                continue
            raise SqlTaskAdminError(
                f"input.args references {{{name}}}, which is neither a declared parameter nor a "
                f"reserved target placeholder. Declared: {sorted(declared) or 'none'}; reserved: "
                f"{list(TARGET_PLACEHOLDERS)}.")
    block["args"] = list(args)

    parameter = str(raw.get("parameter") or "payload").strip()
    if parameter not in declared:
        raise SqlTaskAdminError(
            f"input.parameter {parameter!r} must also appear in parameters - the runner writes the "
            f"DECLARE from that entry, and it should be nvarchar(max). Declared: "
            f"{sorted(declared) or 'none'}.")
    block["parameter"] = parameter
    block["rows_path"] = str(raw.get("rows_path") or "data").strip()

    batch_rows = raw.get("batch_rows", 2000)
    try:
        block["batch_rows"] = int(batch_rows)
    except (TypeError, ValueError):
        raise SqlTaskAdminError(f"input.batch_rows must be an integer, got {batch_rows!r}.") from None
    if block["batch_rows"] < 1:
        raise SqlTaskAdminError("input.batch_rows must be at least 1.")

    timeout = raw.get("timeout_seconds", 3600)
    try:
        block["timeout_seconds"] = int(timeout)
    except (TypeError, ValueError):
        raise SqlTaskAdminError(f"input.timeout_seconds must be an integer, got {timeout!r}.") from None

    codes = raw.get("accept_exit_codes", [0])
    if not isinstance(codes, list) or not codes or any(not isinstance(c, int) for c in codes):
        raise SqlTaskAdminError("input.accept_exit_codes must be a non-empty array of integers.")
    block["accept_exit_codes"] = list(codes)
    return block


def _write_script(root: Path, request: dict[str, Any], *, sql_id: int, sql_name: str,
                  db_type: str, replace: bool) -> str:
    """Write ``sql_text`` to a .sql file and return its path relative to the tool root.

    Where it goes is ``script_path`` when the caller names one, and otherwise the same shape
    ``config_admin.add_sql_task`` has always used — ``assets/tasks/<db_type>/<id>_<name>.sql`` —
    so a task registered from Telegram and one registered from a shell land in the same place
    and neither reader has to know which it was.
    """
    text = str(request.get("sql_text") or "")
    if not text.strip():
        raise SqlTaskAdminError("sql_text is empty.")
    named = str(request.get("script_path") or "").strip().replace("\\", "/")
    relpath = named or f"assets/tasks/{db_type}/{sql_id:03d}_{slugify(sql_name)}.sql"
    resolved = (root / relpath).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise SqlTaskAdminError(
            f"script_path {relpath} resolves outside the tool root ({root}).") from None
    if resolved.exists() and not replace:
        raise SqlTaskAdminError(
            f"SQL file already exists: {relpath}. Pass \"replace\": true to overwrite it, or name "
            "a different script_path.")
    _atomic_write(resolved, text if text.endswith("\n") else text + "\n")
    return relpath


def add_sql_command(request: dict[str, Any], *, data_dir: str | Path | None = None,
                    tool_root: str | Path | None = None) -> dict[str, Any]:
    """Write one entry into ``data/sql_commands.json``. Returns what was written."""
    if not isinstance(request, dict):
        raise SqlTaskAdminError("request must be a JSON object.")
    data_root, root = _root(data_dir, tool_root)

    db_type = str(request.get("db_type") or "").strip().lower()
    if db_type not in KNOWN_DB_TYPES:
        raise SqlTaskAdminError(f"db_type must be one of {KNOWN_DB_TYPES}, got {db_type!r}.")
    sql_name = str(request.get("sql_name") or "").strip()
    if not sql_name:
        raise SqlTaskAdminError("sql_name is required.")

    script_type = str(request.get("script_type") or "single").strip().lower()
    if script_type not in SCRIPT_TYPES:
        raise SqlTaskAdminError(f"script_type must be one of {SCRIPT_TYPES}, got {script_type!r}.")

    commands_path = data_root / "sql_commands.json"
    commands = _read_json(commands_path)
    commands.setdefault("sql_commands", [])

    replace = bool(request.get("replace"))
    raw_id = request.get("sql_id")
    if raw_id in (None, ""):
        sql_id = next_sql_id(commands)
    else:
        try:
            sql_id = int(raw_id)
        except (TypeError, ValueError):
            raise SqlTaskAdminError(f"sql_id must be an integer, got {raw_id!r}.") from None
    existing = [c for c in commands["sql_commands"] if int(c.get("sql_id", -1)) == sql_id]
    if existing and not replace:
        raise SqlTaskAdminError(
            f"sql_id {sql_id} is already registered as {existing[0].get('sql_code')!r}. Pass "
            '"replace": true to overwrite it, or leave sql_id out for the next free one.')

    entry: dict[str, Any] = {
        "sql_id": sql_id,
        "sql_code": str(request.get("sql_code") or
                        f"{db_type.upper()}-{sql_id:03d}-{slugify(sql_name).upper()}").strip(),
        "sql_name": sql_name,
        "db_type": db_type,
        "script_type": script_type,
    }

    written_script: str | None = None
    if script_type == "array":
        raw_paths = request.get("script_paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise SqlTaskAdminError("script_type=array requires script_paths as a non-empty array.")
        entry["script_paths"] = [_require_file(root, p, field="script_paths[]") for p in raw_paths]
    elif str(request.get("sql_text") or "").strip():
        # The other half of this command: the SQL arrives as TEXT and db_ops writes the file.
        # It is the same call either way, because the two are the same decision seen from two
        # places — a task typed into Telegram has no file yet, and a task written in the repo has
        # nothing to type. Splitting them into two commands is what left `/spbot_add_sql` unable
        # to register anything the repo could.
        if script_type != "single":
            raise SqlTaskAdminError("sql_text writes one file, so it needs script_type=single.")
        written_script = _write_script(root, request, sql_id=sql_id, sql_name=sql_name,
                                       db_type=db_type, replace=replace)
        entry["script_path"] = written_script
    else:
        entry["script_path"] = _require_file(root, request.get("script_path", ""),
                                             field="script_path")
        if script_type == "folder" and not (root / entry["script_path"]).is_dir():
            raise SqlTaskAdminError(f"script_type=folder needs a directory: {entry['script_path']}")

    final_paths = request.get("final_script_paths") or []
    if final_paths:
        if not isinstance(final_paths, list):
            raise SqlTaskAdminError("final_script_paths must be an array of file paths.")
        entry["final_script_paths"] = [_require_file(root, p, field="final_script_paths[]")
                                       for p in final_paths]

    entry["version_from"] = request.get("version_from")
    entry["version_to"] = request.get("version_to")

    parameters = request.get("parameters") or []
    if not isinstance(parameters, list) or any(not isinstance(p, dict) for p in parameters):
        raise SqlTaskAdminError('parameters must be an array of {"name", "type"} objects.')

    input_type = str(request.get("input_type") or "none").strip().lower()
    if input_type not in INPUT_TYPES:
        raise SqlTaskAdminError(f"input_type must be one of {INPUT_TYPES}, got {input_type!r}.")
    if input_type == "python":
        entry["input_type"] = "python"
        entry["input"] = _check_input_block(request.get("input"), parameters, root)
    if parameters:
        entry["parameters"] = parameters

    entry["active"] = bool(request.get("active", True))
    if request.get("autocommit"):
        entry["autocommit"] = True
    note = str(request.get("note") or "").strip()
    if note:
        entry["note"] = note

    if existing:
        index = commands["sql_commands"].index(existing[0])
        commands["sql_commands"][index] = entry
    else:
        commands["sql_commands"].append(entry)
    _atomic_write(commands_path, _dump_json(commands))

    return {
        "ok": True,
        "sql_id": sql_id,
        "sql_code": entry["sql_code"],
        "db_type": db_type,
        "script_type": script_type,
        "input_type": input_type,
        "replaced": bool(existing),
        "active": entry["active"],
        "script_path": entry.get("script_path") or entry.get("script_paths"),
        "script_written": written_script,
        "files_written": (["sql_commands.json"] if written_script is None
                          else [written_script, "sql_commands.json"]),
        "next": [f'db-ops common sql-target-add \'{{"sql_id": {sql_id}, "server_id": "..."}}\'',
                 f"db-ops sql_tasks list-tasks --sql-id {sql_id}"],
    }


def add_sql_target(request: dict[str, Any], *,
                   data_dir: str | Path | None = None) -> dict[str, Any]:
    """Write one entry into ``data/sql_targets.json``. Returns what was written."""
    if not isinstance(request, dict):
        raise SqlTaskAdminError("request must be a JSON object.")
    data_root, _ = _root(data_dir, None)

    raw_id = request.get("sql_id")
    try:
        sql_id = int(raw_id)
    except (TypeError, ValueError):
        raise SqlTaskAdminError("sql_id is required and must be an integer.") from None
    server_id = str(request.get("server_id") or "").strip()
    if not server_id:
        raise SqlTaskAdminError("server_id is required.")

    commands = _read_json(data_root / "sql_commands.json")
    command = next((c for c in commands.get("sql_commands", [])
                    if int(c.get("sql_id", -1)) == sql_id), None)
    if command is None:
        # A target for a command that does not exist is a row the runner never reads, and nothing
        # else would ever say so.
        raise SqlTaskAdminError(
            f"no SQL command with sql_id {sql_id}. Register it with sql-command-add first.")

    targets_path = data_root / "sql_targets.json"
    targets = _read_json(targets_path)
    targets.setdefault("sql_targets", [])

    replace = bool(request.get("replace"))
    raw_no = request.get("target_no")
    if raw_no in (None, ""):
        target_no = next_target_no(targets, sql_id)
    else:
        try:
            target_no = int(raw_no)
        except (TypeError, ValueError):
            raise SqlTaskAdminError(f"target_no must be an integer, got {raw_no!r}.") from None
    existing = [t for t in targets["sql_targets"]
                if int(t.get("sql_id", -1)) == sql_id and int(t.get("target_no", -1)) == target_no]
    if existing and not replace:
        raise SqlTaskAdminError(
            f"sql_id {sql_id} already has target_no {target_no} ({existing[0].get('server_id')}). "
            'Pass "replace": true to overwrite it, or leave target_no out for the next free one.')

    window = normalize_time_window(request.get("time_window"))
    if request.get("manual_only"):
        window["repeat_interval"] = MANUAL_ONLY
    try:
        output_format = task_output.normalize_output(request.get("output") or "none")
    except TaskOutputError as exc:
        raise SqlTaskAdminError(str(exc)) from exc

    logging_chat = str(request.get("logging_chat") or SQL_TASK_NOTIFY_CHAT)
    error_chat = str(request.get("error_chat") or SQL_TASK_NOTIFY_CHAT)
    entry: dict[str, Any] = {
        "sql_id": sql_id,
        "target_no": target_no,
        "server_id": server_id,
        "db_type": str(request.get("db_type") or command.get("db_type") or "").strip().lower(),
        "service_name": request.get("service_name"),
        "instance_name": request.get("instance_name"),
        "database_name": request.get("database_name"),
        "credential_name": request.get("credential_name"),
        "time_window": window,
        "active": bool(request.get("active", True)),
        "notify": {
            "logging_on_run": _notify_rule_dict(
                enabled=bool(request.get("logging_on_run", True)),
                telegram_chat=logging_chat, chat_id=request.get("logging_chat_id")),
            "alert_on_error": _notify_rule_dict(
                enabled=bool(request.get("alert_on_error", True)),
                telegram_chat=error_chat, chat_id=request.get("error_chat_id")),
        },
        "output": {
            "format": output_format,
            "telegram_chat": str(request.get("output_chat") or logging_chat),
            "chat_id": request.get("output_chat_id") or "",
        },
    }
    note = str(request.get("note") or "").strip()
    if note:
        entry["note"] = note

    if existing:
        index = targets["sql_targets"].index(existing[0])
        targets["sql_targets"][index] = entry
    else:
        targets["sql_targets"].append(entry)
    _atomic_write(targets_path, _dump_json(targets))

    return {
        "ok": True,
        "sql_id": sql_id,
        "target_no": target_no,
        "server_id": server_id,
        "database_name": entry["database_name"],
        "replaced": bool(existing),
        "active": entry["active"],
        "manual_only": window["repeat_interval"] == MANUAL_ONLY,
        "repeat_interval": window["repeat_interval"],
        "output": output_format,
        "files_written": ["sql_targets.json"],
        "next": [f"db-ops sql_tasks list-tasks --sql-id {sql_id}"],
    }


def _describe(outcome: dict[str, Any]) -> str:
    """One line for the CLI, so a caller reading stdout sees what changed without the JSON."""
    return json.dumps(outcome, ensure_ascii=False)
