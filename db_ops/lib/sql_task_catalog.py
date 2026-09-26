"""The SQL task configuration, read: ``sql_commands.json`` and ``sql_targets.json`` (0.24.0).

What a task is - its scripts, its parameters, its input, its targets and their windows, outputs and
notify blocks - decided once, here, for everyone who asks. It lived in the SQL task runner until
0.24.0, and the Telegram bot, which has to know whether ``/spbot_run_sql_task`` must ask for
parameters, learned the answer by starting the runner's CLI (``sql_tasks.cli list-tasks``): one app
driving another's CLI (rules R42). Before that it parsed the files itself and disagreed with the
runner. A reader in ``lib`` is the fix for both: one definition, read in-process by whoever needs it.

It reads the configuration and nothing else - no runtime store, no secret, no logger. A
deprecation found while reading is handed to ``on_warning`` for the caller to log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from db_ops.lib import data_sources, field_names, sql_access, task_input
from db_ops.lib.data_sources import _server_id_from_instance
from db_ops.lib.json_io import load_json_file
from db_ops.lib.notify import NotifyConfig, NotifyRule, parse_notify_config
from db_ops.lib.paths import TOOL_ROOT, asset_candidates
from db_ops.lib.sql_text import DEFAULT_MAX_ROWS as SQL_RUN_MAX_ROWS
from db_ops.lib.task_input import PythonSource
from db_ops.lib.task_output import (FILE_OUTPUT_FORMATS, MAX_INLINE_MAX_ROWS, OUTPUT_FORMATS,
                                    TaskOutputError, parse_output)
from db_ops.lib.time_window import MANUAL_ONLY, TimeWindow, parse_time_window_config


def _warn(on_warning: Callable[[str], None] | None, warnings: Any) -> None:
    """Hand each deprecation to the caller, who owns the logger."""
    if on_warning is None:
        return
    for message in warnings or ():
        on_warning(str(message))


# db_ops is a standalone repo root; keep REPO_ROOT as an alias so path resolution
# never escapes the project (was TOOL_ROOT.parents[1] under the old repo/tools/db_ops layout).
# Rows an inline (`plain`) target fetches, when its config does not say otherwise. Was 100, which
# was chosen when the message showed only the first 20 anyway; now that every fetched row is
# rendered, 100 was the thing silently deciding how much of an answer an operator got. Raised to
# 1000, and overridable per target with `output.max_rows`.
#
# Not unbounded, and the reason is Telegram rather than memory: rows arrive as ~3900-character
# messages, so roughly 30 rows per message, and Telegram rate-limits a group to about 20 messages
# a minute. A truly uncapped result would not "just be long" — it would 429 partway through and
# arrive in pieces. A task that needs more than this should say so in its SQL, or export a file.
DEFAULT_INLINE_MAX_ROWS = 1000


# The ceiling on `output.max_rows` is MAX_INLINE_MAX_ROWS, defined once in lib/task_output.
# Rows an `output: xlsx` target may export. The same ceiling /spbot_sql_to_xlsx uses, so an
# ad-hoc export and a task export truncate at the same point instead of two surprising ones.
XLSX_MAX_ROWS = SQL_RUN_MAX_ROWS


DEFAULT_SQL_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class SqlCommand:
    sql_id: int
    sql_code: str
    sql_name: str
    db_type: str
    script_type: str
    script_path: str | None
    script_paths: tuple[str, ...]
    script_files: tuple[str, ...]
    active: bool
    #: Parameters the script declares, e.g. [{"name": "session_id", "type": "int",
    #: "required": true}]. The script then uses `@session_id` as an ordinary T-SQL variable;
    #: `common.sql_text.build_parameter_prelude` writes the DECLARE and binds the value, so
    #: what arrives from a Telegram message is never interpolated into SQL. Empty = no parameters,
    #: which is every task that existed before 2026-08-12.
    #: On a direct Oracle or PostgreSQL target the script says `:session_id` instead, bound by name
    #: (`named_parameter_values`); on an 8i bridge target, `&SESSION_ID` (`legacy_define_values`).
    parameters: tuple[dict[str, Any], ...] = ()
    # When true, run the script with the connection in autocommit mode (no wrapping
    # transaction). Required for procedures that refuse to run inside an open transaction
    # (e.g. schedule.usp_Run_V2 raises "must not be called inside an active transaction").
    # Default false keeps the transactional/atomic behavior for ordinary DML scripts.
    autocommit: bool = False
    #: One Telegram message per finished file, on top of the start and done messages.
    #: ``None`` = automatic: on when the task has more than one file, off otherwise — a
    #: single-file task would otherwise send "started", "[1/1] done" and "finished", which is
    #: three messages saying one thing. Off for a Python-fed task, whose steps are batches, not
    #: files. Set true/false in `sql_commands.json` to override.
    #: Only ever sends when the target's ``logging_on_run`` is enabled; this decides how *often*
    #: to report, never *whether* the target reports at all.
    progress_per_file: bool | None = None
    #: Where this task's rows come from: ``none`` (the SQL is the whole task) or ``python``.
    #: Orthogonal to ``script_type``, which says what the SQL half is — see :data:`INPUT_TYPES`.
    input_type: str = "none"
    #: Set when ``input_type == "python"``: the script whose stdout is this task's input, and how
    #: its rows reach the SQL. See :mod:`db_ops.sql_tasks.python_source`.
    python_source: PythonSource | None = None
    #: SQL that runs **once, after the last batch**, rather than once per batch. Without it a
    #: "now roll the loaded rows onward" step would run once per batch - 29 times for a window
    #: that arrives in 29 batches - which is neither what it means nor what it costs.
    final_script_files: tuple[str, ...] = ()


# A SQL target notifies only when it says so, and each rule has its own default level.
SQL_TARGET_NOTIFY_DEFAULTS = NotifyConfig(
    logging_on_run=NotifyRule(enabled=False, telegram_chat="logging"),
    alert_on_error=NotifyRule(enabled=False, telegram_chat="error"),
)


def _target_notify(item: dict[str, Any]) -> dict[str, NotifyRule]:
    """The two notify rules of one SQL target entry, in either spelling.

    **Required, for the same reason ``output`` is.** An absent block used to fall back to the
    app's defaults, which meant a target's routing could only be discovered by running it.
    """
    if not isinstance(item.get("notify"), dict):
        raise RuntimeError(
            f"sql_targets.sql_id={item.get('sql_id')} target_no={item.get('target_no')}: "
            f"'notify' is required and must be an object, e.g. "
            f'{{"logging_on_run": {{"enabled": true, "telegram_chat": "sql", "chat_id": ""}}, '
            f'"alert_on_error": {{"enabled": true, "telegram_chat": "sql", "chat_id": ""}}}}. '
            f"Where a task's messages go is a decision, not a default."
        )
    config = parse_notify_config(
        item, context=f"sql_targets[{item.get('sql_id', '?')}]", defaults=SQL_TARGET_NOTIFY_DEFAULTS
    )
    return {"logging_on_run": config.logging_on_run, "alert_on_error": config.alert_on_error}


@dataclass(frozen=True)
class SqlTarget:
    sql_id: int
    target_no: int
    server_id: str
    db_type: str
    service_name: str
    instance_name: str
    credential_name: str
    time_window: TimeWindow
    active: bool
    logging_on_run: NotifyRule = field(default_factory=NotifyRule)
    alert_on_error: NotifyRule = field(default_factory=NotifyRule)
    database_name: str | None = None
    output_format: str = "none"
    output_chat: str = ""
    output_chat_id: str = ""
    #: `output.max_rows`, or 0 to take the default. Config rather than a literal, because how many
    #: rows are worth reading is a property of the task, not of the runner.
    output_max_rows: int = 0
    # How this target's SQL is reached: a database connection ("direct"), or the legacy Oracle
    # tool ("api"/"subprocess") for an 8i host no driver can connect to. Read from the target's
    # db_instance so a task inherits the transport the estate already declared for that server.
    sql_access: dict[str, Any] = field(default_factory=lambda: {"method": "direct"})

    @property
    def manual_only(self) -> bool:
        """``repeat_interval == -1``: never scheduled, only a forced run starts it.

        Derived rather than stored as its own key so there is exactly one place a target says
        when it runs. A separate `manual_only: true` beside a `repeat_interval: 3600` could
        disagree with itself, and the JSON would not show which one won.
        """
        return self.time_window.repeat_interval == MANUAL_ONLY

    @property
    def capture_max_rows(self) -> int:
        """How many rows to fetch per result set: a preview, or the whole export.

        A file export takes everything. An inline target takes `output.max_rows` if it declares
        one, otherwise :data:`DEFAULT_INLINE_MAX_ROWS` — clamped to :data:`MAX_INLINE_MAX_ROWS`,
        because the limit that matters for inline output is Telegram's rate limit rather than
        memory, and one config edit should not be able to flood a group with hundreds of messages.
        What lands in the store is bounded separately by `STORED_RESULT_MAX_ROWS`, so fetching
        more for the reader does not enlarge every stored run row.
        (The file constant keeps its name: the cap is the same number whatever the file format,
        and renaming it would churn every caller for nothing.)
        """
        if self.output_format in FILE_OUTPUT_FORMATS:
            return XLSX_MAX_ROWS
        if self.output_max_rows > 0:
            return min(self.output_max_rows, MAX_INLINE_MAX_ROWS)
        return DEFAULT_INLINE_MAX_ROWS

    @property
    def interval_second(self) -> int:
        return int(self.time_window.repeat_interval or 300)

    @property
    def timeout_seconds(self) -> int:
        return int(self.time_window.timeout or DEFAULT_SQL_TIMEOUT_SECONDS)

    @property
    def run_key(self) -> str:
        parts = [
            str(self.sql_id),
            str(self.target_no),
            self.server_id,
            self.db_type,
            self.service_name,
            self.instance_name,
            self.database_name or "",
            self.credential_name,
        ]
        return "|".join(parts)


# Marks "this server_id alone does not identify one instance". Stored in the loose credential
# index instead of a name, so the failure is reported as the ambiguity it is rather than as a
# missing credential the operator would go looking for.
_AMBIGUOUS_CREDENTIAL = "\x00ambiguous"


def _opt_str(value: Any) -> str:
    """JSON ``null`` -> ``""``, not the literal ``"None"``.

    ``str(item.get(key, ""))`` returns ``"None"`` when the key is present and null, which is
    what a bot-created target has for the fields the operator skipped. A target then carried
    ``instance_name == "None"``, and because that string is truthy,
    :func:`find_database_inventory` compared it against every real instance and matched none —
    the task failed at run time with "Target database not found ... /None" while its config
    looked fine. An absent value must stay absent.
    """
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"none", "null"} else text


def collect_sql_tasks(
    data_dir: Path, *, sql_id: int | None = None, include_inactive: bool = False,
) -> dict[str, Any]:
    """Every configured SQL task, as data: what it is, what it takes, where it runs.

    **The answer to "what SQL tasks are there" belongs to this app**, not to whoever is asking.
    The Telegram bot used to read ``sql_commands.json`` and ``sql_targets.json`` itself and
    re-implement which of them count as runnable — so it could answer differently from the
    runner, and did: a task's declared parameters were invisible to it, and ``/spbot_run_sql_task``
    never asked for them. This is built from the same loaders the runner executes with
    (:func:`load_sql_commands` / :func:`load_sql_targets`), so a listing cannot drift from what
    running the task would actually do.

    Only what would run is listed unless ``include_inactive``: a task is off when the command is
    off, and equally when every target is — a command with no active target runs nowhere, so it
    is dropped rather than listed as something that does nothing. ``hidden_count`` says how many
    were left out so the count is never silently short.

    Presentation is deliberately absent. The caller renders; a time window is returned as the
    object it is, not as a line of text, so a Telegram message and a JSON export can differ in
    layout without either one deciding for the other.
    """
    commands = load_sql_commands(data_dir / "sql_commands.json")
    targets = load_sql_targets(data_dir / "sql_targets.json")

    targets_by_sql_id: dict[int, list[SqlTarget]] = {}
    for target in targets:
        if include_inactive or target.active:
            targets_by_sql_id.setdefault(int(target.sql_id), []).append(target)

    listed: list[dict[str, Any]] = []
    hidden = 0
    for command in sorted(commands.values(), key=lambda item: int(item.sql_id)):
        own_targets = targets_by_sql_id.get(int(command.sql_id), [])
        runnable = (include_inactive or command.active) and bool(own_targets)
        if sql_id is not None and int(command.sql_id) != int(sql_id):
            continue
        if not runnable and sql_id is None:
            hidden += 1
            continue
        listed.append({
            "sql_id": int(command.sql_id),
            "sql_code": command.sql_code,
            "display_name": command.sql_name,
            "db_type": command.db_type,
            "script_type": command.script_type,
            "script_files": list(command.script_files),
            "active": bool(command.active),
            "autocommit": bool(command.autocommit),
            # What a caller must supply, and what it may leave out. This is the field the bot
            # needs to know whether to ask the operator anything at all.
            "parameters": [dict(item) for item in command.parameters],
            "parameter_names": [
                str(item.get("name") or "").strip() for item in command.parameters
                if str(item.get("name") or "").strip()
            ],
            "required_parameter_names": [
                str(item.get("name") or "").strip() for item in command.parameters
                if str(item.get("name") or "").strip() and bool(item.get("required", False))
            ],
            "targets": [{
                "target_no": int(target.target_no),
                "server_id": target.server_id,
                "db_type": target.db_type,
                "service_name": target.service_name,
                "instance_name": target.instance_name,
                "database_name": target.database_name,
                "credential_name": target.credential_name,
                "active": bool(target.active),
                "manual_only": bool(target.manual_only),
                "output_format": target.output_format,
                # TimeWindow.to_dict, never a subset written here: this used to fall back to four
                # hand-listed names and the listing therefore hid `weekdays` entirely.
                "time_window": target.time_window.to_dict(),
                # Which transport this target's SQL takes: "direct" (a database connection) or
                # the legacy Oracle tool. Visible in the listing because it is the difference
                # between a task that needs the bridge up and one that does not.
                "sql_access_method": str((target.sql_access or {}).get("method") or "direct"),
            } for target in own_targets],
        })
        if sql_id is not None and not runnable:
            # Asked for by id: report it with active=False rather than pretending it is missing.
            listed[-1]["runnable"] = False

    return {
        "ok": True,
        "command_count": len(listed),
        "target_count": sum(len(item["targets"]) for item in listed),
        "hidden_count": hidden,
        "sql_tasks": listed,
    }


def load_sql_commands(path: Path, *, on_warning: Callable[[str], None] | None = None) -> dict[int, SqlCommand]:
    data = load_json_file(path)
    commands: list[SqlCommand] = []
    for item in data.get("sql_commands", []):
        sql_code = str(item["sql_code"])
        commands.append(
            SqlCommand(
                sql_id=int(item["sql_id"]),
                sql_code=sql_code,
                sql_name=str(field_names.read(item, "sql_command", "display_name", "")),
                db_type=str(item.get("db_type", "")),
                **load_sql_script_definition(item, data_dir=path.parent),
                **load_input_definition(item, command_name=sql_code),
                active=bool(item.get("active", True)),
                parameters=tuple(dict(p) for p in (item.get("parameters") or [])),
                autocommit=bool(item.get("autocommit", False)),
                progress_per_file=(None if item.get("progress_per_file") is None
                                   else bool(item["progress_per_file"])),
            )
        )
    return {command.sql_id: command for command in commands}


def load_sql_script_definition(item: dict[str, Any], *, data_dir: Path) -> dict[str, Any]:
    command_name = str(item.get("sql_code", item.get("sql_id")))
    legacy_keys = [key for key in ("file_name", "file_names", "folder_name") if key in item]
    if legacy_keys:
        raise RuntimeError(f"SQL command {command_name} uses deprecated script field(s): {', '.join(legacy_keys)}. Use script_type with script_path or script_paths.")

    script_type = str(item.get("script_type", "")).strip().lower()
    if script_type not in {"single", "array", "folder"}:
        raise RuntimeError(f"SQL command {command_name} has unsupported script_type: {script_type or '<missing>'}. Expected single, array, or folder.")

    # Orthogonal to script_type, like input_type: it says *when* a file runs, not what the task is.
    raw_final = item.get("final_script_paths") or []
    if not isinstance(raw_final, list):
        raise RuntimeError(
            f"SQL command {command_name} final_script_paths must be an array of file paths.")
    final_script_files = tuple(str(value).strip() for value in raw_final if str(value).strip())

    has_script_path = "script_path" in item
    has_script_paths = "script_paths" in item
    raw_script_path = str(item.get("script_path", "")).strip()

    if script_type == "single":
        if not raw_script_path:
            raise RuntimeError(f"SQL command {command_name} script_type=single requires script_path.")
        if has_script_paths:
            raise RuntimeError(f"SQL command {command_name} script_type=single must not define script_paths.")
        return {
            "script_type": script_type,
            "script_path": raw_script_path,
            "script_paths": (),
            "script_files": (raw_script_path,),
            "final_script_files": final_script_files,
        }

    if script_type == "array":
        if has_script_path:
            raise RuntimeError(f"SQL command {command_name} script_type=array must not define script_path.")
        raw_script_paths = item.get("script_paths")
        if not isinstance(raw_script_paths, list):
            raise RuntimeError(f"SQL command {command_name} script_type=array requires script_paths as a non-empty array.")
        script_paths = tuple(str(value).strip() for value in raw_script_paths if str(value).strip())
        if not script_paths:
            raise RuntimeError(f"SQL command {command_name} script_type=array requires script_paths as a non-empty array.")
        return {
            "script_type": script_type,
            "script_path": None,
            "script_paths": script_paths,
            "script_files": script_paths,
            "final_script_files": final_script_files,
        }

    if not raw_script_path:
        raise RuntimeError(f"SQL command {command_name} script_type=folder requires script_path.")
    if has_script_paths:
        raise RuntimeError(f"SQL command {command_name} script_type=folder must not define script_paths.")
    folder_path = resolve_sql_folder(raw_script_path, data_dir=data_dir)
    script_files = tuple(str(path) for path in sorted(folder_path.glob("*.sql"), key=lambda path: path.name))
    if not script_files:
        raise RuntimeError(f"SQL command {command_name} script_type=folder has no *.sql files in script_path: {raw_script_path}.")
    return {
        "script_type": script_type,
        "script_path": raw_script_path,
        "script_paths": (),
        "script_files": script_files,
        "final_script_files": final_script_files,
    }


#: Where a task's row input comes from, as opposed to what its SQL is. Two axes, deliberately
#: separate: ``script_type`` says single file / list / folder, ``input_type`` says whether anything
#: feeds it. Folding "runs a python script" into ``script_type`` would have made every combination
#: of the two a new word, and the first thing it forced was a python task pretending to be an
#: ``array`` — a spelling that is right about the files and silent about the part that matters.
INPUT_TYPES = frozenset({"none", "python"})


def load_input_definition(item: dict[str, Any], *, command_name: str) -> dict[str, Any]:
    """Read ``input_type`` and its block off a ``sql_commands.json`` entry.

    ``none`` is the default and is every task that existed before 2026-09-14: the SQL is the whole
    task and it runs once per file. ``python`` runs a program first and hands its rows to that same
    SQL in batches — see :mod:`db_ops.sql_tasks.python_source`.
    """
    input_type = str(item.get("input_type") or "none").strip().lower()
    if input_type not in INPUT_TYPES:
        raise RuntimeError(
            f"SQL command {command_name} has unsupported input_type: {input_type or '<missing>'}. "
            f"Expected one of {sorted(INPUT_TYPES)}.")

    block = item.get("input") or {}
    if not isinstance(block, dict):
        raise RuntimeError(f"SQL command {command_name} input must be an object.")
    if input_type == "none":
        if block:
            raise RuntimeError(
                f"SQL command {command_name} carries an input block but input_type is none, so "
                "nothing would read it. Set input_type, or remove the block.")
        return {"input_type": input_type, "python_source": None}

    source = task_input.parse(block, command_name=command_name)
    declared = {str(p.get("name") or "").strip() for p in (item.get("parameters") or [])}
    if source.parameter not in declared:
        raise RuntimeError(
            f"SQL command {command_name} binds each batch to @{source.parameter}, which is not in "
            "its parameters. Declare it there with type nvarchar(max): that entry is what writes "
            f"the DECLARE the SQL reads. Declared: {sorted(declared) or 'none'}.")
    return {"input_type": input_type, "python_source": source}


def load_sql_targets(path: Path, *, on_warning: Callable[[str], None] | None = None) -> list[SqlTarget]:
    data = load_json_file(path)
    # Read once, through the one reader that owns db_instances.json (common.data_sources).
    instances = data_sources.load_db_instances(path.parent)
    default_credentials = load_default_credential_names(instances)
    sql_access_by_server = load_sql_access_by_server(instances)
    targets: list[SqlTarget] = []
    for item in data.get("sql_targets", []):
        target_name = f"sql_targets.sql_id={item.get('sql_id')}.target_no={item.get('target_no')}"
        parsed_time_window = parse_time_window_config(
            item,
            context=target_name,
            defaults={
                "from_day": 1,
                "to_day": 31,
                "from_hour": 0,
                "to_hour": 23,
                "repeat_interval": 300,
            },
        )
        _warn(on_warning, parsed_time_window.warnings)
        targets.append(
            SqlTarget(
                sql_id=int(item["sql_id"]),
                target_no=int(item["target_no"]),
                server_id=_opt_str(item.get("server_id")),
                db_type=_opt_str(item.get("db_type")),
                service_name=_opt_str(item.get("service_name")),
                instance_name=_opt_str(item.get("instance_name")),
                credential_name=_opt_str(
                    item.get("credential_name")
                    or default_credentials.get(
                        _target_default_key(
                            server_id=_opt_str(item.get("server_id")),
                            db_type=_opt_str(item.get("db_type")),
                            service_name=_opt_str(item.get("service_name")),
                            instance_name=_opt_str(item.get("instance_name")),
                        ),
                        "",
                    )
                ),
                time_window=parsed_time_window.time_window,
                active=bool(item.get("active", True)),
                # One read of the shared notify object (db_ops.lib.notify): it takes the
                # whole entry, so the canonical `notify` block and the older top-level
                # logging_on_run/alert_on_error keys both land in the same shape.
                **_target_notify(item),
                database_name=str(item["database_name"]) if item.get("database_name") else None,
                # The transport belongs to the *server*, not to the task: an 8i host is
                # unreachable by every task alike. Read from its db_instance so one entry
                # covers every task on it; a sql_targets entry may still override.
                sql_access=sql_access.normalize_sql_access(
                    item.get("sql_access")
                    or sql_access_by_server.get(_opt_str(item.get("server_id"))),
                    label=target_name,
                ),
                **_target_output(item),
            )
        )
    return targets


def load_sql_access_by_server(instances: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each db_instance's ``sql_access`` block, keyed by ``server_id``.

    Only the servers that declare one appear; everything else is a plain database connection.
    Takes the already-read records: reading db_instances.json is common.data_sources' job.
    """
    access: dict[str, dict[str, Any]] = {}
    for item in instances or []:
        raw = item.get("sql_access")
        server_id = str(item.get("server_id") or _server_id_from_instance(item)).strip()
        if raw and server_id:
            access[server_id] = dict(raw)
    return access


def _target_output(item: dict[str, Any]) -> dict[str, str]:
    """The `output` block of a sql_targets entry: what to do with the result set.

    **Required, and it was not always.** An absent block used to mean ``plain``, on the reasoning
    that tasks written before ``output`` existed had had their rows pasted into the run message
    from the start, so inferring ``none`` would have stopped delivering results people relied on.
    That reasoning was right about ``none`` and wrong about the inference: the very next sentence
    of the old docstring said *"``none`` is a choice the operator makes, never one inferred from
    silence"*, and every other format is a choice too.

    What the default cost was not a wrong delivery but an unanswerable question — reading
    ``sql_targets.json`` did not tell you whether a task sent a file, sent rows, or sent nothing,
    because thirteen of seventeen targets said nothing at all and the answer lived in this
    function. Those thirteen now say ``plain`` in the file, which is what they were already
    doing, and the inference is gone.

    ``add-sql`` has always asked for ``output`` and marked it required, so nothing that
    registered a task through the documented path is affected.
    """
    raw = item.get("output")
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"sql_targets.sql_id={item.get('sql_id')} target_no={item.get('target_no')}: "
            f"'output' is required and must be an object. Add one naming what to do with the "
            f"result set, e.g. "
            f'{{"format": "plain", "telegram_chat": "sql", "chat_id": ""}} - '
            f"format is one of {OUTPUT_FORMATS} ('plain' pastes the rows into the run message, "
            f"'none' sends status only). It is not inferred: a task's delivery is a decision, "
            f"and one that is not written down is one nobody can read back."
        )
    # One parser for the block (lib/task_output), the same one check-objects holds the file to.
    try:
        parsed = parse_output(raw)
    except TaskOutputError as exc:
        raise RuntimeError(f"sql_targets.sql_id={item.get('sql_id')}: {exc}") from exc
    return {
        "output_format": parsed["format"],
        "output_chat": parsed["telegram_chat"],
        "output_chat_id": parsed["chat_id"],
        "output_max_rows": parsed["max_rows"],
    }


def load_default_credential_names(instances: list[dict[str, Any]]) -> dict[tuple[str, str, str, str], str]:
    """Takes the already-read records: reading db_instances.json is common.data_sources' job."""
    defaults: dict[tuple[str, str, str, str], str] = {}
    for item in instances or []:
        default_credential_name = str(item.get("default_credential_name") or "").strip()
        if not default_credential_name:
            continue
        key = _target_default_key(
            server_id=str(item.get("server_id") or _server_id_from_instance(item)),
            db_type=str(item.get("db_type", "")),
            service_name=str(item.get("service_name") or ""),
            instance_name=str(item.get("instance_name", "")),
        )
        defaults[key] = default_credential_name
        # A target that names only its server_id (everything /spbot_add_sql no longer asks for)
        # still has to find its credential. Index it a second time under an empty
        # service/instance, but only while that stays unambiguous: on a server running two
        # instances the operator has to say which one, and a guess would silently run the SQL
        # against the wrong database.
        loose = _target_default_key(
            server_id=key[0], db_type=key[1], service_name="", instance_name="",
        )
        if loose in defaults and defaults[loose] != default_credential_name:
            defaults[loose] = _AMBIGUOUS_CREDENTIAL
        else:
            defaults.setdefault(loose, default_credential_name)
    return defaults


def _target_default_key(*, server_id: str, db_type: str, service_name: str, instance_name: str) -> tuple[str, str, str, str]:
    return (
        server_id.strip(),
        db_type.strip().lower(),
        service_name.strip().lower(),
        instance_name.strip().lower(),
    )


def resolve_sql_folder(folder_name: str, *, data_dir: Path) -> Path:
    path = Path(folder_name)
    candidates = [path] if path.is_absolute() else [
        TOOL_ROOT / path,
        # The operator's own task SQL, then the built-ins that ship with the package. `tasks/` is
        # written per server and mirrored back from the worker, so the operator's copy wins.
        *asset_candidates("tasks", str(path)),
        data_dir / path,
    ]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir():
            return resolved
    raise RuntimeError(f"SQL folder not found or not a folder: {folder_name}")
