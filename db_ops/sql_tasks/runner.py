from __future__ import annotations
from db_ops.lib.text_format import format_log_value  # noqa: F401 - one definition, see that module
from db_ops.common.data_sources import _server_id_from_instance  # noqa: F401 - one definition, see that module

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db_ops.lib import sql_access
# Imported by name, not as a module: `sql_text` is also a local variable in this file
# (the SQL itself), and a module bound to the same name shadows it silently.
from db_ops.lib.sql_text import (DEFAULT_CONNECT_TIMEOUT_SECONDS, SqlParameterError,
                                 build_parameter_prelude, check_sqlplus_define_value,
                                 expand_sqlplus_defines, resolve_password)
from db_ops.lib.notify import (
    NotifyConfig,
    NotifyRule,
    parse_notify_config,
    parse_notify_rule as common_parse_notify_rule,
)
from db_ops.lib.task_output import FILE_OUTPUT_FORMATS, OUTPUT_FORMATS
from db_ops.lib.telegram_route import telegram_groups
from db_ops.lib.sql_text import DEFAULT_MAX_ROWS as SQL_RUN_MAX_ROWS
from db_ops.lib import result_format
from db_ops.db.queue_message import queue_message, store_block_from
from db_ops.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from db_ops.common import data_sources
from db_ops.lib import common_cli
from db_ops.lib.secret_text import add_key_argument, set_key_env
# Connecting and executing are `common`'s, reached through `common.cli run-sql` — this app
# imported nine driver-level helpers from `sql_execution` for a connection it no longer opens, and
# had stopped using most of them long before (2026-08-16). What is left is what was always a
# value: how to declare a script's parameters, how to read a credential's password, and the JSON
# reader — all of them `lib`, importable by anything.
from db_ops.lib.json_io import load_json_file
from db_ops.lib.time_window import MANUAL_ONLY, TimeWindow, is_time_window_open as common_time_window_open, job_due, parse_time_window_config, repeat_due
from db_ops.db import DbOpsStore
from db_ops.db.store import utc_now_text
from db_ops.logging_ops import log_event, log_function_error, setup_app_logger
from db_ops.logging_ops.runtime_stdout import patch_stdout
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.paths import asset_candidates


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
#: Ceiling on what `output.max_rows` may ask for, so one config edit cannot flood a group.
MAX_INLINE_MAX_ROWS = 5000
# Rows an `output: xlsx` target may export. The same ceiling /spbot_sql_to_xlsx uses, so an
# ad-hoc export and a task export truncate at the same point instead of two surprising ones.
XLSX_MAX_ROWS = SQL_RUN_MAX_ROWS
# What goes into sql_runs.result_json regardless of how many rows were fetched: an export must
# not turn every run row into a multi-megabyte JSON blob in the store. **Its own number, not an
# alias of the inline cap** — it used to be `= MAX_RESULT_ROWS`, so raising how much an operator
# sees in chat would have quietly multiplied the size of every stored run row too.
STORED_RESULT_MAX_ROWS = 100
#: Kept for callers that imported it; the inline default now carries the meaning.
MAX_RESULT_ROWS = DEFAULT_INLINE_MAX_ROWS
DEFAULT_SQL_TIMEOUT_SECONDS = 1800

#: How many of a script's result sets are kept, for the store row and the Telegram table. Five,
#: because that is what `execute_cursor_batches` kept before this app called `run-sql` instead and
#: `sql_runs.result_json` is read against it. The rows of the sets beyond it are still *counted*
#: into `row_count` — dropping them from the total would make a run look smaller than it was.
MAX_STORED_RESULT_SETS = 5


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
    parameters: tuple[dict[str, Any], ...] = ()
    # When true, run the script with the connection in autocommit mode (no wrapping
    # transaction). Required for procedures that refuse to run inside an open transaction
    # (e.g. schedule.usp_Run_V2 raises "must not be called inside an active transaction").
    # Default false keeps the transactional/atomic behavior for ordinary DML scripts.
    autocommit: bool = False
    #: One Telegram message per finished file, on top of the start and done messages.
    #: ``None`` = automatic: on when the task has more than one file, off otherwise — a
    #: single-file task would otherwise send "started", "[1/1] done" and "finished", which is
    #: three messages saying one thing. Set true/false in `sql_commands.json` to override.
    #: Only ever sends when the target's ``logging_on_run`` is enabled; this decides how *often*
    #: to report, never *whether* the target reports at all.
    progress_per_file: bool | None = None


def _progress_summary(file_results: list[dict[str, Any]], total_files: int) -> str:
    """What a failed multi-file run actually got done, for the last message it will ever send.

    A folder task that dies on file 3 of 5 used to end on the SQL error alone. That names the
    fault but not the state: whether files 1 and 2 committed, how many rows they wrote, whether
    anything ran at all. The done path has always ended with totals, and a failure is exactly when
    somebody needs them — the next decision is "re-run the whole folder, or resume from 3".

    Empty when the task is not multi-file, so a single-file failure stays one line.
    """
    if total_files <= 1:
        return ""
    done = len(file_results)
    if not done:
        return f"\nfiles done: 0/{total_files} (nothing completed)"
    rows = sum(int(item.get("row_count") or 0) for item in file_results)
    # `file_name` is the configured value, which for a folder task is an absolute path on the
    # machine that built the image. The stored record keeps it; the chat message wants the name.
    names = ", ".join(Path(str(item.get("file_name") or "")).name for item in file_results)
    return f"\nfiles done: {done}/{total_files} ({names}), {rows} row(s)"


# A SQL target notifies only when it says so, and each rule has its own default level.
SQL_TARGET_NOTIFY_DEFAULTS = NotifyConfig(
    logging_on_run=NotifyRule(enabled=False, telegram_chat="logging"),
    alert_on_error=NotifyRule(enabled=False, telegram_chat="error"),
)


def parse_notify_rule(value: Any, *, default_level: str) -> NotifyRule:
    """Parse one ``logging_on_run``/``alert_on_error`` switch on a SQL target.

    A thin adapter over :func:`db_ops.lib.notify.parse_notify_rule`, which owns the shape
    for every app. Kept for callers that hold a single rule rather than a whole entry.
    """
    return common_parse_notify_rule(
        value, default=NotifyRule(enabled=False, telegram_chat=default_level)
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


@dataclass(frozen=True)
class SqlScanResult:
    due_count: int = 0
    success_count: int = 0
    error_count: int = 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one DB Ops SQL task scheduler scan.")
    parser.add_argument("--config", default=None, help="Path to config JSON. Defaults to config.sql_tasks.json or config.json.")
    add_key_argument(parser)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Directory containing SQL task JSON files.")
    parser.add_argument("--dry-run", action="store_true", help="Print due SQL tasks without executing SQL or writing sql_runs.")
    subparsers = parser.add_subparsers(dest="command")

    run_sql_id = subparsers.add_parser("run-sql-id", help="Run all SQL task targets matching one sql_id.")
    run_sql_id.add_argument("--sql-id", type=int, required=True, help="SQL task ID from sql_commands.json.")
    run_sql_id.add_argument(
        "--param", action="append", default=[], metavar="NAME=VALUE",
        help="Value for a parameter the task declares in sql_commands.json. Repeatable. The "
             "script uses it as @NAME; the value is bound, never pasted into the SQL.")
    run_sql_id.add_argument(
        "--params", default="", metavar='"NAME=VALUE NAME=VALUE"',
        help="The same, as one quoted string. For callers with a single argument slot to fill — "
             "a Telegram command renders one template per argv entry and cannot repeat --param. "
             "Split with shell quoting rules, so a value containing spaces goes in quotes.")
    run_sql_id.add_argument(
        "--force",
        action="store_true",
        help="Run without checking active flags, time windows, or intervals.",
    )
    run_sql_id.add_argument(
        "--output-chat-id",
        default=None,
        help="Deliver this run's result (the output block) to this Telegram chat instead of the "
             "target's configured one. /spbot_run_sql_task passes the chat that asked, so the "
             "xlsx comes back to whoever requested it rather than to the logging group.",
    )

    list_tasks = subparsers.add_parser(
        "list-tasks",
        help="Print the configured SQL tasks (and their parameters and targets) as JSON.",
    )
    list_tasks.add_argument(
        "--sql-id", type=int, default=None,
        help="Only this task. Omit for every task.",
    )
    list_tasks.add_argument(
        "--all", action="store_true",
        help="Include inactive tasks and targets. Default lists only what would actually run.",
    )
    return parser.parse_args(argv)


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
            "sql_name": command.sql_name,
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
                "time_window": target.time_window.to_dict()
                if hasattr(target.time_window, "to_dict") else {
                    "repeat_interval": target.time_window.repeat_interval,
                    "timeout": target.time_window.timeout,
                    "from_hour": target.time_window.from_hour,
                    "to_hour": target.time_window.to_hour,
                },
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


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    # `list-tasks` reads configuration and nothing else: no runtime store, no secret key, no
    # logger. A caller asking what tasks exist must not be blocked by a database being down,
    # and must not have to hold the passphrase to find out.
    if getattr(args, "command", None) == "list-tasks":
        try:
            payload = collect_sql_tasks(
                Path(args.data_dir).resolve(),
                sql_id=args.sql_id,
                include_inactive=bool(args.all),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0

    set_key_env(args.key, args.key_base64)
    logger = None
    try:
        config = load_config(resolve_config_path("sql_tasks", args.config))
        patch_stdout(config.log_dir / "sql_tasks_runtime.log", app_name="sql_tasks")
        logger = setup_app_logger(config, app_name="sql_tasks", enable_telegram_alerts=False)
        store = DbOpsStore.from_config(config)
        store.initialize()
        data_dir = Path(args.data_dir).resolve()
        if args.command == "run-sql-id":
            if not args.force:
                raise RuntimeError("run-sql-id requires --force.")
            log_event(logger, level="logging", message=f"sql_tasks.runner.start|scope=sql_tasks|mode=force|sql_id={args.sql_id}|data_dir={data_dir}")
            result = run_sql_id_tasks(
                parameter_values=parse_parameter_arguments(args.param, args.params),
                store=store,
                data_dir=data_dir,
                sql_id=int(args.sql_id),
                force=bool(args.force),
                dry_run=bool(args.dry_run),
                telegram_groups=telegram_groups(),
                logger=logger,
                output_chat_id=_opt_str(args.output_chat_id),
            )
            if args.dry_run:
                print(f"Selected SQL tasks: {result.due_count}")
        else:
            log_event(logger, level="logging", message=f"sql_tasks.runner.start|scope=sql_tasks|mode=scan|data_dir={data_dir}")
            result = run_scheduler_scan(
                store=store,
                data_dir=data_dir,
                dry_run=bool(args.dry_run),
                telegram_groups=telegram_groups(),
                logger=logger,
            )
            if args.dry_run:
                print(f"Due SQL tasks: {result.due_count}")
        if result.error_count:
            print(f"SQL task scan failed tasks: {result.error_count}", file=sys.stderr)
            return 1
        return 0
    except KeyboardInterrupt:
        if logger:
            log_event(logger, level="logging", message="DB Ops SQL task scan stopped by keyboard interrupt.")
        return 0
    except Exception as exc:  # noqa: BLE001 - command-line failure path.
        if logger:
            log_function_error(logger, function_name="sql_tasks.runner", error_text=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def run_scheduler_scan(
    *,
    store: DbOpsStore,
    data_dir: Path,
    dry_run: bool,
    telegram_groups: dict[str, str],
    logger: Any,
) -> SqlScanResult:
    commands = load_sql_commands(data_dir / "sql_commands.json", logger=logger)
    targets = load_sql_targets(data_dir / "sql_targets.json", logger=logger)
    latest_done_or_running_runs = store.fetch_latest_done_or_running_sql_runs_by_run_key()
    secrets = data_sources.load_secret_text(data_dir)
    inventory = data_sources.load_inventory(data_dir)
    credentials = data_sources.load_all_credentials(data_dir)
    mark_stale_running_sql_runs(store=store, commands=commands, targets=targets,
                                latest_runs=latest_done_or_running_runs,
                                telegram_groups=telegram_groups, logger=logger)
    latest_done_or_running_runs = store.fetch_latest_done_or_running_sql_runs_by_run_key()
    # The most recent run per key REGARDLESS of status — so a task that keeps FAILING is backed
    # off (retry_interval) instead of being retried every scan tick. Without this, a task with
    # only 'error' rows has no done/running row, so repeat_due(None) is always True and it
    # hammers the target every minute.
    latest_any_runs = store.fetch_latest_sql_runs_by_run_key()

    due_pairs = due_sql_tasks(commands=commands, targets=targets,
                              latest_runs=latest_done_or_running_runs, latest_any_runs=latest_any_runs)
    success_count = 0
    error_count = 0
    for command, target in due_pairs:
        if dry_run:
            print(
                f"{command.sql_code} target={target.target_no} server={target.server_id} "
                f"db={target.database_name or target.service_name} script_type={command.script_type} "
                f"files={len(command.script_files)} file_order={format_script_file_order(command)}"
            )
            continue
        success = run_one_sql_task(
            # None, not a caller's values: a scheduled scan has no operator to take parameters
            # from, so each task falls back to the defaults it declares itself (see
            # build_parameter_prelude). This line used to read `parameter_values=parameter_values`,
            # a name that exists only on the single-task path — so **every** scan raised NameError
            # before running a single task. The outer handler turned that into "exit 1" once a
            # minute, which is indistinguishable from a scheduler with nothing due: manual runs
            # kept working and scheduled SQL tasks silently stopped for a day (last scheduled run
            # 2026-08-12T09:22Z, found 2026-08-13).
            parameter_values=None,
            store=store,
            data_dir=data_dir,
            telegram_groups=telegram_groups,
            command=command,
            target=target,
            inventory=inventory,
            credentials=credentials.get(target.db_type.lower(), []),
            secrets=secrets,
            logger=logger,
        )
        if success:
            success_count += 1
        else:
            error_count += 1
    return SqlScanResult(due_count=len(due_pairs), success_count=success_count, error_count=error_count)


def run_sql_id_tasks(
    *,
    store: DbOpsStore,
    data_dir: Path,
    sql_id: int,
    force: bool,
    dry_run: bool,
    telegram_groups: dict[str, str],
    logger: Any,
    output_chat_id: str = "",
    parameter_values: dict[str, str] | None = None,
) -> SqlScanResult:
    if not force:
        raise RuntimeError("run-sql-id requires --force.")
    commands = load_sql_commands(data_dir / "sql_commands.json", logger=logger)
    targets = load_sql_targets(data_dir / "sql_targets.json", logger=logger)
    command = commands.get(sql_id)
    if command is None:
        raise RuntimeError(f"SQL command not found for sql_id={sql_id}.")
    # Values typed without a name are bound here and nowhere earlier: this is the first point
    # where the task that declares those names is known.
    parameter_values = bind_parameter_values(command, parameter_values)
    selected_targets = [target for target in targets if target.sql_id == sql_id and target.db_type.lower() == command.db_type.lower()]
    if not selected_targets:
        raise RuntimeError(f"No SQL targets found for sql_id={sql_id}.")

    secrets = data_sources.load_secret_text(data_dir)
    inventory = data_sources.load_inventory(data_dir)
    credentials = data_sources.load_all_credentials(data_dir)

    success_count = 0
    error_count = 0
    for target in selected_targets:
        if dry_run:
            print(
                f"{command.sql_code} target={target.target_no} server={target.server_id} "
                f"db={target.database_name or target.service_name} script_type={command.script_type} "
                f"files={len(command.script_files)} file_order={format_script_file_order(command)} force={force}"
            )
            continue
        success = run_one_sql_task(
            parameter_values=parameter_values,
            store=store,
            data_dir=data_dir,
            telegram_groups=telegram_groups,
            command=command,
            target=target,
            inventory=inventory,
            credentials=credentials.get(target.db_type.lower(), []),
            secrets=secrets,
            logger=logger,
            output_chat_id=output_chat_id,
        )
        if success:
            success_count += 1
        else:
            error_count += 1
    return SqlScanResult(due_count=len(selected_targets), success_count=success_count, error_count=error_count)


def mark_stale_running_sql_runs(
    *,
    store: DbOpsStore,
    commands: dict[int, SqlCommand],
    targets: list[SqlTarget],
    latest_runs: dict[str, Any],
    telegram_groups: dict[str, str],
    logger: Any,
) -> None:
    """Close out runs left `running` by a process that died, and ALERT on each one.

    The alert is the point. This path used to write the error row and log it, and nothing else:
    `alert_on_error` was only wired into the exception handler inside `run_sql_target`, and a run
    whose process was killed never reaches that handler. On 2026-09-03 sql_id 28 overran the
    daemon's own command timeout, was killed mid-cycle, and the failure sat in the store for half
    an hour with no message anywhere — while the SQL it had started went on running on the server,
    holding the task's application lock, so every following cycle reported SKIPPED. A silent error
    class is worse than a noisy one: nobody reads a table they have no reason to open.
    """
    now = datetime.now(timezone.utc)
    targets_by_key = {(target.sql_id, target.target_no): target for target in targets}
    for row in latest_runs.values():
        if str(row["status"]).lower() != "running":
            continue
        command = commands.get(int(row["sql_id"]))
        if command is None:
            continue
        started = sql_run_time(row)
        if started is None:
            continue
        target = targets_by_key.get((int(row["sql_id"]), int(row["target_no"])))
        timeout_seconds = target.timeout_seconds if target is not None else DEFAULT_SQL_TIMEOUT_SECONDS
        if started + timedelta(seconds=timeout_seconds) > now:
            continue
        message = f"SQL task {row['sql_code']} stale running exceeded timeout_seconds={timeout_seconds}."
        store.update_sql_run(
            sql_run_id=int(row["sql_run_id"]),
            status="error",
            level="error",
            message=message,
            finished_at=utc_now_text(),
            error_text=message,
            metadata={"stale_running": True},
        )
        log_function_error(logger, function_name="sql_tasks.stale_running", error_text=message)
        # A target is how a run learns where to complain. Without one there is no notify block to
        # read, so the log line above is all this can be - the same fallback the timeout above uses.
        if target is not None and target.alert_on_error.enabled:
            enqueue_sql_task_message(
                store=store,
                telegram_groups=telegram_groups,
                rule=target.alert_on_error,
                command=command,
                target=target,
                status="error",
                message=(f"{message} The run process is gone, but the SQL it started may still be "
                         f"executing on {target.server_id}/{target.service_name} - check for an "
                         f"orphaned session before the next cycle."),
                sql_run_id=int(row["sql_run_id"]),
                # There are no rows to show: the process died before it reported any.
                include_result_table=False,
            )


def due_sql_tasks(
    *,
    commands: dict[int, SqlCommand],
    targets: list[SqlTarget],
    latest_runs: dict[str, Any],
    latest_any_runs: dict[str, Any] | None = None,
) -> list[tuple[SqlCommand, SqlTarget]]:
    now = datetime.now(timezone.utc)
    local_now = datetime.now().astimezone()
    latest_any_runs = latest_any_runs or {}
    due: list[tuple[SqlCommand, SqlTarget]] = []
    for target in targets:
        if not target.active:
            continue
        # repeat_interval == -1. `job_due` below already refuses a manual target, but saying it
        # here keeps "the scheduler does not start this" visible where the scan is read, and
        # skips the window/command work that can only end in the same answer.
        if target.manual_only:
            continue
        if not is_time_window_open(target.time_window, local_now):
            continue
        command = commands.get(target.sql_id)
        if command is None or not command.active:
            continue
        if command.db_type.lower() != target.db_type.lower():
            continue
        latest = latest_runs.get(target.run_key)
        if latest and str(latest["status"]).lower() == "running":
            continue
        # The most recent run of ANY status drives the schedule, so a failing task backs off
        # instead of re-running every tick. job_due (shared with the daemon and metrics) applies
        # the run-once convention, the repeat interval after a success, and retry_interval after
        # a failure. retry_interval defaults to the repeat interval — a failed SQL task is never
        # retried faster than its normal schedule unless the target sets retry_interval explicitly.
        recent = latest_any_runs.get(target.run_key) or latest
        last_time = sql_run_time(recent)
        last_status = str(recent["status"]).lower() if recent else None
        window = target.time_window
        retry_interval = window.retry_interval if window.retry_interval is not None else window.repeat_interval
        if job_due(
            last_run=last_time,
            last_status=last_status,
            repeat_interval=window.repeat_interval,
            retry_interval=retry_interval,
            now=now,
            timeout=window.timeout,
            default_repeat=300,
        ):
            due.append((command, target))
    return due


def is_time_window_open(time_window: TimeWindow | None, local_now: datetime) -> bool:
    return common_time_window_open(time_window, local_now)


def run_one_sql_task(
    *,
    store: DbOpsStore,
    data_dir: Path,
    telegram_groups: dict[str, str],
    command: SqlCommand,
    target: SqlTarget,
    inventory: list[dict[str, Any]],
    credentials: list[dict[str, Any]],
    secrets: dict[str, str],
    logger: Any,
    output_chat_id: str = "",
    parameter_values: dict[str, str] | None = None,
) -> bool:
    started = datetime.now(timezone.utc)
    started_text = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    sql_paths = resolve_sql_files(command.script_files, data_dir=data_dir)
    database = find_database_inventory(target, inventory)
    credential = find_database_credential(target, credentials)
    file_results: list[dict[str, Any]] = []
    metadata = {
        "host_name": socket.gethostname(),
        "script_type": command.script_type,
        "sql_files": [str(path) for path in sql_paths],
        "sql_file_count": len(sql_paths),
        "database": database,
        "credential": scrub_credential(credential),
    }
    if command.script_type == "folder":
        log_sql_task_event(
            logger,
            "sql_tasks.runner.script.discovered",
            command=command,
            target=target,
            sql_id=command.sql_id,
            sql_code=command.sql_code,
            script_type=command.script_type,
            files=",".join(str(path) for path in sql_paths),
        )
    sql_run_id = store.insert_sql_run(
        run_key=target.run_key,
        sql_id=command.sql_id,
        sql_code=command.sql_code,
        target_no=target.target_no,
        server_id=target.server_id,
        db_type=target.db_type,
        service_name=target.service_name,
        instance_name=target.instance_name,
        database_name=target.database_name,
        credential_name=target.credential_name,
        status="running",
        level="logging",
        message=f"SQL task {command.sql_code} started.",
        started_at=started_text,
        metadata=metadata,
    )
    log_sql_task_event(
        logger,
        "sql_tasks.runner.task.start",
        command=command,
        target=target,
        status="running",
        run_id=sql_run_id,
    )
    if target.logging_on_run.enabled:
        enqueue_sql_task_message(
            store=store,
            telegram_groups=telegram_groups,
            rule=target.logging_on_run,
            command=command,
            target=target,
            status="running",
            message=f"SQL task {command.sql_code} started on {target.server_id}/{target.service_name}.",
            sql_run_id=sql_run_id,
        )

    # Which file is in flight, so a failure names it. A folder task fails with a SQL error and
    # nothing else; "invalid object name" across six scripts is six places to look.
    failing_file = ""
    total_files = 0
    try:
        if database is None:
            target_label = f"{target.server_id}/{target.service_name}"
            if target.database_name:
                target_label = f"{target_label}/{target.database_name}"
            raise RuntimeError(f"Target database not found in database-inventory.json: {target_label}")
        if target.credential_name == _AMBIGUOUS_CREDENTIAL:
            raise RuntimeError(
                f"{target.server_id} runs more than one {target.db_type} instance, so server_id "
                "alone does not say which one to run against. Set instance_name (and "
                "credential_name) on this sql_targets entry."
            )
        if credential is None:
            raise RuntimeError(f"Credential not found: {target.credential_name}")
        password = resolve_password(credential, secrets)
        total_row_count = 0
        # Automatic unless the command says otherwise: worth it for a folder of scripts, noise
        # for a single file. Resolved here because this is the only scope that knows the count.
        total_files = len(sql_paths)
        report_progress = (total_files > 1 if command.progress_per_file is None
                           else bool(command.progress_per_file))
        for file_no, sql_path in enumerate(sql_paths, start=1):
            file_started = datetime.now(timezone.utc)
            configured_file_name = command.script_files[file_no - 1]
            # `script_files` holds absolute paths for a folder task, so the configured name is
            # the master's full path — useless in a chat message and it leaks a build-host path.
            # The file name is what identifies the step; the full path stays in the log line.
            display_name = sql_path.name
            failing_file = f"[{file_no}/{len(sql_paths)}] {display_name}"
            log_sql_task_event(
                logger,
                "sql_tasks.runner.script.execute",
                command=command,
                target=target,
                sql_id=command.sql_id,
                sql_code=command.sql_code,
                script_type=command.script_type,
                actual_file=str(sql_path),
            )
            sql_text = sql_path.read_text(encoding="utf-8-sig")
            result = execute_sql(
                command=command,
                target=target,
                database=database,
                credential=credential,
                password=password,
                sql_text=sql_text,
                parameter_values=parameter_values,
                secrets=secrets,
            )
            file_finished = datetime.now(timezone.utc)
            file_duration_ms = int((file_finished - file_started).total_seconds() * 1000)
            file_result = {
                "file_no": file_no,
                "file_name": configured_file_name,
                "sql_file": str(sql_path),
                "status": "done",
                "duration_ms": file_duration_ms,
                "row_count": int(result.get("row_count") or 0),
                "result_sets": result.get("result_sets", []),
            }
            file_results.append(file_result)
            total_row_count += int(result.get("row_count") or 0)
            metadata["file_results"] = file_results
            # One message per finished file, not just one at the end. A folder task can run for
            # hours per file, and until the whole thing finished the only two signals were
            # "started" and silence — indistinguishable from a task that died. The progress
            # counter is the point: `[2/6]` says both that file 2 is done and that 4 remain.
            if target.logging_on_run.enabled and report_progress:
                enqueue_sql_task_message(
                    store=store,
                    telegram_groups=telegram_groups,
                    rule=target.logging_on_run,
                    command=command,
                    target=target,
                    status="running",
                    message=(
                        f"SQL task {command.sql_code} [{file_no}/{len(sql_paths)}] "
                        f"{display_name} done on "
                        f"{target.server_id}/{target.service_name} in {file_duration_ms} ms, "
                        f"{file_result['row_count']} row(s)."
                    ),
                    sql_run_id=sql_run_id,
                )
        result = {"row_count": total_row_count, "files": file_results}
        finished = datetime.now(timezone.utc)
        duration_ms = int((finished - started).total_seconds() * 1000)
        # The workbook is written from the FULL result and before the run row is stored, because
        # what goes into the store is the trimmed copy below — an export of 5000 rows must not
        # also become a 5000-row JSON blob in sql_runs. A failure to write must not fail the SQL
        # task either: the SQL already ran and committed, so the run is recorded as done and the
        # operator is told the delivery failed.
        document_path = None
        if target.output_format in FILE_OUTPUT_FORMATS:
            try:
                document_path = write_sql_task_output(
                    command=command,
                    target=target,
                    result=result,
                    sql_run_id=sql_run_id,
                    output_dir=data_dir.parent / "runtime" / "output" / "sql_tasks",
                )
                if document_path is None:
                    metadata["output_note"] = (
                        f"output={target.output_format} but the script returned no result set."
                    )
            except OSError as exc:
                metadata["output_note"] = f"output={target.output_format} failed to write: {exc}"
                log_sql_task_event(
                    logger,
                    "sql_tasks.runner.output.error",
                    command=command,
                    target=target,
                    level="error",
                    error=str(exc),
                )
        stored_result = trim_result_for_store(result)
        store.update_sql_run(
            sql_run_id=sql_run_id,
            status="done",
            level="logging",
            message=f"SQL task {command.sql_code} finished.",
            finished_at=finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            duration_ms=duration_ms,
            row_count=int(result.get("row_count") or 0),
            result=stored_result,
            metadata=metadata,
        )
        result = stored_result
        # Whoever asked for this run gets the rows. A file already worked that way; an inline
        # table did not, so a `/spbot_run_sql_task` in one chat reported "finished" there and
        # printed the actual answer in the task's configured group - which the person who ran it
        # may not even be in. The rows go to the requester as their own message and the log line
        # stays an audit line, exactly as it is for an export.
        delivered_to_requester = bool(output_chat_id) and enqueue_sql_task_result_text(
            store=store,
            command=command,
            target=target,
            sql_run_id=sql_run_id,
            result=result,
            chat_id=output_chat_id,
        )
        if target.logging_on_run.enabled:
            enqueue_sql_task_message(
                store=store,
                telegram_groups=telegram_groups,
                rule=target.logging_on_run,
                command=command,
                target=target,
                status="done",
                message=f"SQL task {command.sql_code} finished on {target.server_id}/{target.service_name} in {duration_ms} ms.",
                sql_run_id=sql_run_id,
                result=result,
                include_result_table=not delivered_to_requester,
            )
        # The workbook goes out on its own message, not attached to the run log. Two reasons:
        # the log is an audit line that belongs in the notify chat, while the file is a
        # deliverable that belongs wherever it was asked for; and a target with
        # logging_on_run disabled must still receive the export it configured.
        if document_path is not None:
            enqueue_sql_task_document(
                store=store,
                telegram_groups=telegram_groups,
                command=command,
                target=target,
                sql_run_id=sql_run_id,
                document_path=document_path,
                row_count=int(result.get("row_count") or 0),
                override_chat_id=output_chat_id,
            )
        log_sql_task_event(
            logger,
            "sql_tasks.runner.task.done",
            command=command,
            target=target,
            status="done",
            run_id=sql_run_id,
            elapsed_ms=duration_ms,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - scheduler must continue after one task fails.
        finished = datetime.now(timezone.utc)
        duration_ms = int((finished - started).total_seconds() * 1000)
        metadata["file_results"] = file_results
        store.update_sql_run(
            sql_run_id=sql_run_id,
            status="error",
            level="error",
            message=(f"SQL task {command.sql_code} failed"
                     f"{' at ' + failing_file if failing_file else ''}."),
            finished_at=finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            duration_ms=duration_ms,
            error_text=str(exc),
            metadata=metadata,
        )
        if target.alert_on_error.enabled:
            enqueue_sql_task_message(
                store=store,
                telegram_groups=telegram_groups,
                rule=target.alert_on_error,
                command=command,
                target=target,
                status="error",
                message=(f"SQL task {command.sql_code} failed on "
                         f"{target.server_id}/{target.service_name}"
                         f"{' at ' + failing_file if failing_file else ''} "
                         f"after {duration_ms} ms."
                         f"{_progress_summary(file_results, total_files)}"
                         f"\nerror: {exc}"),
                sql_run_id=sql_run_id,
            )
        log_sql_task_event(
            logger,
            "sql_tasks.runner.task.error",
            command=command,
            target=target,
            level="error",
            status="error",
            reason=str(exc),
            run_id=sql_run_id,
            elapsed_ms=duration_ms,
        )
        return False


#: The Unicode range that only ever exists as half of a pair. A string holding one on its own
#: cannot be encoded to anything, which is why every driver refuses it - pyodbc most visibly,
#: because SQL Server takes NVARCHAR as UTF-16LE.
_SURROGATE_RANGE = range(0xD800, 0xE000)


def _surrogate_context(sql_text: str, index: int, *, window: int = 45) -> str:
    """The characters either side of *index*, escaped, so the string can be recognised.

    The whole point of the guard: knowing *which* string carried the surrogate is what a codec
    error never says. Printed as ``ascii`` so a second bad character in the window cannot make
    the report itself unprintable - which is exactly how this defect wasted an afternoon.
    """
    start = max(0, index - window)
    return ascii(sql_text[start:index + window])


def check_sql_text_is_encodable(sql_text: str, *, source: str) -> None:
    """Refuse SQL carrying a lone surrogate, and say which character and where.

    On 2026-08-27 one ``/spbot_run_sql_task 18`` run died with ``'utf-16-le' codec can't encode
    character U+DC97 in position 350: surrogates not allowed``.

    U+DC97 is byte 0x97 recovered by ``surrogateescape``, and 0x97 is the em dash in the Windows
    ANSI code page. The script is valid UTF-8, character 350 *is* an em dash, and the file on disk
    was byte-identical to the source tree's - so that one run received the text after a round trip
    through cp1252 that no other run made. Four faithful reproductions, up to and including the
    dispatch's exact argv under a detached console-less process, all succeeded.

    This does not fix that round trip, because I could not find it. What it does is turn an
    intermittent driver error naming only a codec into one that names **the file, the character
    and its position** - the difference between "it failed again" and a report somebody can act
    on. It costs a scan of a string already in memory, and it runs for every engine, because a
    lone surrogate is unencodable everywhere.

    **The script file is not the whole story, and that is the finding.** Its two non-ASCII
    characters are em dashes at 286 and 346, both inside ``--`` comments, and it contains no 0x97
    byte anywhere. The driver was handed a string whose character *350* is byte 0x97. So the
    statement that reached the driver was not simply this file's text - something composes it, or
    re-reads it, between the ``read_text(encoding="utf-8-sig")`` and the send. That is why the
    message below carries the surrounding characters: the next occurrence identifies the string.

    Writing this docstring hit the same class of bug: the patch script that inserted it put a real
    lone surrogate into the source and Python refused to save the file. Hence U+DC97 in prose
    rather than an escape.
    """
    for index, character in enumerate(sql_text):
        if ord(character) not in _SURROGATE_RANGE:
            continue
        byte = ord(character) - 0xDC00
        recovered = bytes([byte]).decode("cp1252", errors="replace") if 0 <= byte <= 0xFF else "?"
        raise RuntimeError(
            f"{source}: character {index} is a lone surrogate (U+{ord(character):04X}), which no "
            f"encoding accepts, so the driver cannot send this statement. It is byte 0x{byte:02x} "
            f"recovered by surrogateescape - {recovered!r} in the Windows ANSI code page - so the "
            f"script text passed through a non-UTF-8 round trip between being read and being "
            f"executed.\n"
            f"  context: {_surrogate_context(sql_text, index)}\n"
            f"  statement length: {len(sql_text)} characters."
        )


def execute_sql(
    *,
    command: SqlCommand,
    target: SqlTarget,
    database: dict[str, Any],
    credential: dict[str, Any],
    password: str,
    sql_text: str,
    parameter_values: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> dict[str, Any]:
    check_sql_text_is_encodable(sql_text, source=f"{command.sql_code} target={target.target_no}")
    return execute_on_target(command=command, target=target, database=database,
                             credential=credential, password=password, sql_text=sql_text,
                             parameter_values=parameter_values, secrets=secrets)


# Every row that was fetched is rendered. The table used to stop at 20 with "… N more row(s)",
# which cut off exactly what somebody had run the task to see; the send layer now splits an
# over-long body across messages, so length is no longer a reason to drop rows. How many rows a
# task should return is the SQL's business — a script that produces too many should say TOP/LIMIT.
# The column and cell bounds stay: they keep a row on one phone-width line, and a row that wraps
# five times is unreadable however many of them arrive.
RESULT_TABLE_MAX_COLS = 8
RESULT_CELL_MAX_LEN = 24


def _md_cell(value: Any) -> str:
    """One markdown-table cell: stringify, keep it single-line, cap the width."""
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > RESULT_CELL_MAX_LEN:
        text = text[: RESULT_CELL_MAX_LEN - 1] + "…"
    return text


def format_result_sets_markdown(result: dict[str, Any] | None) -> str:
    """Render a SQL task's returned rows as GitHub-style markdown table(s).

    Reads the ``result_sets`` ({"columns": [...], "rows": [[...]]}) captured per file
    by the executor. Empty result sets render as ``(0 rows)``; wide tables are clipped to
    ``RESULT_TABLE_MAX_COLS`` columns with a note. **Every fetched row is rendered** — the
    message is split across sends if it is long, rather than the rows being dropped. Returns ""
    when there is nothing tabular to show.
    """
    if not result:
        return ""
    blocks: list[str] = []
    set_index = 0
    for file_result in result.get("files", []) or []:
        for rset in file_result.get("result_sets", []) or []:
            columns = list(rset.get("columns") or [])
            rows = list(rset.get("rows") or [])
            if not columns:
                continue
            set_index += 1
            clipped_cols = columns[:RESULT_TABLE_MAX_COLS]
            col_note = "" if len(columns) <= RESULT_TABLE_MAX_COLS else f" (+{len(columns) - RESULT_TABLE_MAX_COLS} cols)"
            header = "| " + " | ".join(_md_cell(c) for c in clipped_cols) + " |"
            sep = "| " + " | ".join("---" for _ in clipped_cols) + " |"
            lines = [f"result set {set_index}: {len(rows)} row(s){col_note}", header, sep]
            for row in rows:
                cells = list(row)[:RESULT_TABLE_MAX_COLS]
                cells += [""] * (len(clipped_cols) - len(cells))
                lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
            if not rows:
                lines.append("(0 rows)")
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def trim_result_for_store(result: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``result`` whose result sets carry at most a preview of the rows.

    ``row_count`` is left alone — it is the real total, and an operator reading "5133 rows" next
    to 100 stored rows learns the truth. Clipping it to the stored length would report the
    export as smaller than it was.
    """
    files = []
    for file_result in result.get("files", []) or []:
        sets = []
        for rset in file_result.get("result_sets", []) or []:
            rows = list(rset.get("rows") or [])
            trimmed = dict(rset)
            trimmed["rows"] = rows[:STORED_RESULT_MAX_ROWS]
            if len(rows) > STORED_RESULT_MAX_ROWS:
                trimmed["rows_omitted"] = len(rows) - STORED_RESULT_MAX_ROWS
            sets.append(trimmed)
        copied = dict(file_result)
        copied["result_sets"] = sets
        files.append(copied)
    out = dict(result)
    out["files"] = files
    return out


def first_result_set(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """The first result set that actually has columns, or ``None``.

    A task script often ends with several statements; only one of them returns the rows the
    operator wants delivered. Picking the first *non-empty* set (rather than the last statement's)
    is what makes `USE db; GO; SELECT ...` behave the way it reads.
    """
    for file_result in (result or {}).get("files", []) or []:
        for rset in file_result.get("result_sets", []) or []:
            if rset.get("columns"):
                return rset
    return None


def write_sql_task_output(
    *,
    command: SqlCommand,
    target: SqlTarget,
    result: dict[str, Any] | None,
    sql_run_id: int,
    output_dir: Path,
    output_format: str = "",
) -> Path | None:
    """Write the task's first result set as its configured file, and return the path.

    ``None`` when the script returned no result set — a task that exports nothing is not an
    error, and the caller records it as a note on the run rather than failing SQL that already
    committed.

    The rendering goes through :mod:`db_ops.lib.result_format`, the same code path
    ``run-sql --format`` uses, so a scheduled export and an ad-hoc one are the same artifact.
    They used to be able to differ only because xlsx was the sole option; the moment csv and txt
    existed, a second renderer here would have been a second answer to "what does this task's
    output look like".
    """
    rset = first_result_set(result)
    if rset is None:
        return None
    fmt = (output_format or target.output_format or "xlsx").strip().lower()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"sql_{command.sql_id:03d}_{workflow_name_from_code(command.sql_code)}_{stamp}.{fmt}"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / name
    rows = [list(row) for row in (rset.get("rows") or [])]
    # One call, no branch on format. Which formats write themselves and which hand back a string
    # is result_format's problem, not this app's — every exporter that knew the difference grew
    # its own `if fmt == "xlsx"`, and they did not stay identical.
    result_format.write_result(
        {"ok": True, "columns": list(rset.get("columns") or []), "rows": rows,
         "row_count": len(rows)},
        fmt=fmt,
        path=path,
        sheet_name=f"sql_{command.sql_id}",
    )
    return path


def write_sql_task_xlsx(**kwargs) -> Path | None:
    """Deprecated alias kept for callers that predate the other file formats."""
    return write_sql_task_output(**kwargs, output_format="xlsx")


def resolve_output_chat_id(
    target: SqlTarget, telegram_groups: dict[str, str], *, override: str = ""
) -> str:
    """Where this target's result is delivered, most specific first.

    The override is the chat that asked (`/spbot_run_sql_task` passes it): a file someone
    requested by hand has to come back to them, not to the logging group they may not even be
    in. Only then the target's own configuration, and finally the notify chat, so a target that
    sets `output.format` but forgets `output.telegram_chat` still delivers somewhere.
    """
    if override:
        return override
    if target.output_chat_id:
        return target.output_chat_id
    if target.output_chat:
        chat_id = telegram_groups.get(target.output_chat, "")
        if chat_id:
            return chat_id
    return target.logging_on_run.resolve_chat_id(telegram_groups)


def enqueue_sql_task_document(
    *,
    store: DbOpsStore,
    telegram_groups: dict[str, str],
    command: SqlCommand,
    target: SqlTarget,
    sql_run_id: int,
    document_path: Path,
    row_count: int,
    override_chat_id: str = "",
) -> None:
    """Queue the exported workbook as its own Telegram document."""
    chat_id = resolve_output_chat_id(target, telegram_groups, override=override_chat_id)
    if not chat_id:
        return
    queue_message({
        "store": store_block_from(store),
        "chat_id": chat_id,
        # The caption carries no status of its own — the run already reported one. Declaring
        # `plain` keeps the header guess off a caption that names a SQL task.
        "message_type": "plain",
        "text": (
            f"{command.sql_code}\n"
            f"{command.sql_name}\n"
            f"server: {target.server_id}\n"
            f"rows: {row_count}\n"
            f"sql_run_id: {sql_run_id}"
        ),
        "note": f"sql_task:output:{target.output_format}",
        "source_type": "sql_runs",
        "source_id": str(sql_run_id),
        "metadata": {
            "sql_id": command.sql_id,
            "sql_code": command.sql_code,
            "target_no": target.target_no,
            "run_key": target.run_key,
            # send_queue sends this as a Telegram document with `text` as the caption.
            "document_path": str(document_path),
        },
    }, fallback_store=store)


def enqueue_sql_task_result_text(
    *,
    store: DbOpsStore,
    command: SqlCommand,
    target: SqlTarget,
    sql_run_id: int,
    result: dict[str, Any] | None,
    chat_id: str,
) -> bool:
    """Queue this run's rows to the chat that asked for it. True when something was queued.

    The inline-table twin of :func:`enqueue_sql_task_document`, and it exists for the same
    reason: the deliverable belongs to whoever requested the run, while the run log belongs in
    the notify chat. False - so the caller leaves the table on the log line - when the target
    exports a file instead (that path already delivers), reports status only, or the script
    returned nothing tabular.
    """
    if not chat_id or target.output_format in {*FILE_OUTPUT_FORMATS, "none"}:
        return False
    result_table = format_result_sets_markdown(result)
    if not result_table:
        return False
    queue_message({
        "store": store_block_from(store),
        "chat_id": str(chat_id),
        # No status of its own: the run already reported one, and `plain` keeps the header
        # guess off a table whose cells may contain words like "error".
        "message_type": "plain",
        "text": (
            f"{command.sql_code}\n"
            f"{command.sql_name}\n"
            f"server: {target.server_id}\n"
            f"rows: {int((result or {}).get('row_count') or 0)}\n"
            f"sql_run_id: {sql_run_id}\n\n"
            f"{result_table}"
        ),
        "note": f"sql_task:output:{target.output_format}",
        "source_type": "sql_runs",
        "source_id": str(sql_run_id),
        "metadata": {
            "sql_id": command.sql_id,
            "sql_code": command.sql_code,
            "target_no": target.target_no,
            "run_key": target.run_key,
        },
    }, fallback_store=store)
    return True


def enqueue_sql_task_message(
    *,
    store: DbOpsStore,
    telegram_groups: dict[str, str],
    rule: NotifyRule,
    command: SqlCommand,
    target: SqlTarget,
    status: str,
    message: str,
    sql_run_id: int,
    result: dict[str, Any] | None = None,
    document_path: Path | None = None,
    include_result_table: bool = True,
) -> None:
    level = rule.telegram_chat
    chat_id = rule.resolve_chat_id(telegram_groups)
    if not chat_id:
        return
    lines = [
        f"[{level.upper()}] SQL task {status}",
        f"sql_code: {command.sql_code}",
        f"sql_name: {command.sql_name}",
        f"server_id: {target.server_id}",
        f"service_name: {target.service_name}",
        f"instance_name: {target.instance_name}",
        f"target_no: {target.target_no}",
        f"sql_run_id: {sql_run_id}",
        f"message: {message}",
    ]
    # What the run does with its rows is the target's `output` setting. Any file format ships
    # them as an attachment (the table would just repeat it), `none` reports status only, and
    # anything else — including a target written before `output` existed — keeps the inline table.
    if include_result_table and target.output_format not in {*FILE_OUTPUT_FORMATS, "none"}:
        result_table = format_result_sets_markdown(result)
        if result_table:
            lines.append("")
            lines.append(result_table)
    text = "\n".join(lines)
    queue_message({
        "store": store_block_from(store),
        "chat_id": chat_id,
        "text": text,
        # A task run reports its own outcome ("running" then "done"/"error"); the level only
        # says which chat hears about it.
        "status": status,
        "level": level,
        "note": f"sql_task:{status}",
        "source_type": "sql_runs",
        "source_id": str(sql_run_id),
        "metadata": {
            "level": level,
            "status": status,
            "sql_id": command.sql_id,
            "sql_code": command.sql_code,
            "target_no": target.target_no,
            "run_key": target.run_key,
            # send_queue sends this as a Telegram document with `text` as the caption.
            **({"document_path": str(document_path)} if document_path else {}),
        },
    }, fallback_store=store)


def log_sql_task_event(
    logger: Any,
    event_name: str,
    *,
    command: SqlCommand,
    target: SqlTarget | None = None,
    level: str = "logging",
    **fields: Any,
) -> None:
    if logger is None:
        return
    base_fields = {
        "scope": "sql_tasks",
        "sql_task_code": command.sql_code,
        "sql_task_name": command.sql_name,
        "workflow": workflow_name_from_code(command.sql_code),
    }
    if target is not None:
        base_fields.update(
            {
                "target_no": target.target_no,
                "server_id": target.server_id,
                "db_type": target.db_type,
                "database": target.database_name or target.service_name,
            }
        )
    base_fields.update(fields)
    parts = [event_name]
    for key, value in base_fields.items():
        if value is None:
            continue
        parts.append(f"{key}={format_log_value(value)}")
    log_event(logger, level=level, message="|".join(parts))




def format_script_file_order(command: SqlCommand) -> str:
    names = [Path(value).name for value in command.script_files]
    return "[" + ", ".join(names) + "]"


def execute_on_target(
    *,
    command: SqlCommand,
    target: SqlTarget,
    database: dict[str, Any],
    credential: dict[str, Any],
    password: str,
    sql_text: str,
    parameter_values: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one task's SQL on its target, on whichever engine that target is.

    One call for every engine **and every transport**, through
    ``python -m db_ops.common.cli run-sql``. Three things used to be decided here that were never
    this app's to decide — how to reach a database, how to split a script into batches, and how to
    route an Oracle 8i target — and each was a second opinion that could drift from the shared one.
    The 2026-08-06 audit named the Oracle half, the 2026-08-11 audit found the SQL Server half
    still there, and on 2026-08-16 the whole thing became a request object.

    What stays here is what is genuinely a *task* concern:

    * **the commit mode.** An ``autocommit`` task runs with no wrapping transaction (each batch
      commits on its own), which is what procs that reject an open transaction
      (``@@TRANCOUNT > 0``) require; every other task commits once at the end.
    * **the two timeouts, kept apart.** The task's own timeout bounds the *statements*; the
      connect keeps its own short deadline, because a task allowed twenty minutes must not wait
      twenty minutes to discover the host is down.
    * **what a parameter means on this transport.** A normal target binds them; an 8i target
      cannot (the legacy tool runs one statement with no bind list), so its values are SQL*Plus
      substitutions instead — see :func:`legacy_define_values`, which refuses a name the command
      does not declare rather than letting it match no ``&VAR`` and vanish.

    Returns the shape the rest of this runner reads — ``{"row_count", "result_sets",
    "truncated"}`` — which is not ``run-sql``'s own, so the mapping is right below and explained
    where it differs.
    """
    engine = sql_access.normalize_db_type(target.db_type)
    if engine not in {"sqlserver", "oracle"}:
        raise RuntimeError(f"Unsupported db_type: {target.db_type}")

    request: dict[str, Any] = {
        "target": target.server_id,
        "database": target.database_name or "",
        "credential_name": target.credential_name or "",
        "sql": sql_text,
        "max_rows": target.capture_max_rows,
        "timeout_seconds": target.timeout_seconds,
        "connect_timeout_seconds": DEFAULT_CONNECT_TIMEOUT_SECONDS,
        "autocommit": bool(command.autocommit),
        "commit": not command.autocommit,
        # Every result set, uncapped, then cut to five below — the runner has always stored five
        # and counted the rows of all of them, and `row_count` would be short if the extra sets
        # were dropped before being counted.
        "capture": "all",
        "max_result_sets": 0,
        # The target's own transport, so `run-sql` routes an 8i host to the legacy tool exactly as
        # it would for an operator at a shell.
        "sql_access": target.sql_access or {},
    }
    if sql_access.is_legacy(target.sql_access):
        request["define"] = legacy_define_values(command, parameter_values)
    else:
        prelude, bound = build_parameter_prelude(command.parameters, parameter_values or {})
        request["prelude"] = prelude
        request["params"] = bound

    success, result, error = common_cli.run_allowing_failure("run-sql", request)
    if not success:
        raise RuntimeError(error or "run-sql failed without a reason.")

    sets = result.get("result_sets") or []
    return {
        # `run-sql` reports fetched rows and affected rows separately; this runner has always
        # reported one number covering both, and `sql_runs.row_count` is read as such.
        "row_count": sum(int(item.get("row_count") or 0) for item in sets)
        + int(result.get("affected_rows") or 0),
        "result_sets": [
            {"columns": item.get("columns") or [], "rows": item.get("rows") or [],
             "truncated": bool(item.get("truncated"))}
            for item in sets[:MAX_STORED_RESULT_SETS]
        ],
        # Any set cut, not just a kept one: the count above included the rows of sets six and up,
        # so their truncation is part of whether this answer is complete.
        "truncated": any(bool(item.get("truncated")) for item in sets),
    }


def legacy_define_values(
    command: SqlCommand, parameter_values: dict[str, Any] | None,
) -> dict[str, str]:
    """The SQL*Plus substitutions for this run: supplied value, else the parameter's declared
    default, else whatever the script's own ``DEFINE`` line says.

    **A name the command does not declare is refused.** It would otherwise match no ``&VAR`` in
    the script and be silently dropped — so a run with ``jobno=`` instead of ``job_no=`` would
    quietly export the job number the archived script was last saved with, and look like it
    worked. A required parameter with no value is refused for the same reason.
    """
    declared = {str(p.get("name") or "").strip().lower(): dict(p) for p in command.parameters or ()}
    supplied = {str(name).strip().lower(): str(value)
                for name, value in (parameter_values or {}).items()}

    unknown = sorted(set(supplied) - set(declared))
    if unknown and declared:
        raise SqlParameterError(
            f"{command.sql_code} does not declare parameter(s) {', '.join(unknown)}; "
            f"it takes {', '.join(sorted(declared)) or 'none'}."
        )

    values: dict[str, str] = {}
    for name, parameter in declared.items():
        if supplied.get(name, "").strip():
            values[name] = supplied[name]
        elif str(parameter.get("default") or "").strip():
            values[name] = str(parameter["default"])
        elif bool(parameter.get("required", False)):
            raise SqlParameterError(
                f"{command.sql_code} requires parameter {name}: pass --param {name}=<value>."
            )
        else:
            # Left out on purpose: the script's own DEFINE line is then the value, which is how
            # an archived script keeps running exactly as it was saved.
            continue
        check_sqlplus_define_value(name, values[name])
    return values


# `execute_legacy_oracle` lived here until 2026-08-16. It opened nothing — an 8i host has no
# connection db_ops can make — but it did decide *which transport* to use and then reshaped the
# bridge's answer into the runner's. `run-sql` routes `sql_access` itself and answers in one shape
# whichever transport replied, so both halves went away with it. What could not: deciding what a
# parameter *means* on that transport, which is `legacy_define_values` above and is config
# knowledge, not transport knowledge.


def parse_parameter_arguments(pairs: list[str] | None, joined: str = "") -> dict[str, str]:
    """``--param name=value`` repeated and/or ``--params "a=1 b=2"``, into one mapping.

    Two spellings because two kinds of caller: a shell or a scheduled command repeats a flag
    naturally, while a Telegram command renders one template per argv entry and has exactly one
    slot to put everything in. ``--params`` is split with shell quoting rules, so a value with
    spaces in it survives as ``"note=needs a look"``.

    Each pair splits on the FIRST ``=`` only: a value may legitimately contain one (a date range,
    a LIKE pattern), and splitting on all of them would silently truncate it.
    """
    import shlex

    items = list(pairs or [])
    if str(joined or "").strip():
        items.extend(shlex.split(str(joined)))
    values: dict[str, str] = {}
    positional: list[str] = []
    for pair in items:
        text = str(pair)
        # "-" is this bot's standing sentinel for "nothing here" (the conversation flow already
        # fills it in for a question that does not apply, see command_processor.skip_when). A
        # prompt that has to be answered needs a way to say "no values", and the alternative -
        # an empty message - is not something Telegram lets someone send.
        if text.strip() == "-":
            continue
        name, sep, value = text.partition("=")
        if not sep or not name.strip():
            # A bare value. Someone answering a prompt on a phone types the job number, not
            # `job_no=<job number>`, and being told "--param expects NAME=VALUE" for an answer
            # to a question that just named the parameter is the bot being obtuse about
            # something it already knows. It is bound to a declared parameter by position in
            # bind_parameter_values, where the task's own declaration is in scope.
            positional.append(text)
            continue
        values[name.strip()] = value
    if positional:
        values[POSITIONAL_PARAMETERS_KEY] = json.dumps(positional)
    return values


#: Where :func:`parse_parameter_arguments` parks values given without a name, until the task
#: that declares the names is loaded. A key no parameter can have (``=`` cannot appear in one),
#: so it can never collide with a real value.
POSITIONAL_PARAMETERS_KEY = "=positional="


def bind_parameter_values(
    command: SqlCommand, parameter_values: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve values given without a name against the parameters this task declares, in order.

    ``--param session_id=1068`` and ``1068`` mean the same thing for a task whose first
    parameter is ``session_id``. Named values are placed first, then the bare ones fill the
    remaining parameters in declared order — so naming one and leaving the other positional
    still lands where the operator meant.

    A bare value for a task that declares nothing, or more bare values than there are
    parameters left, is an error that names what the task actually takes: silently dropping it
    would run the task with different arguments than the person typed.
    """
    values = dict(parameter_values or {})
    raw = values.pop(POSITIONAL_PARAMETERS_KEY, None)
    if not raw:
        return values
    positional = json.loads(raw) if isinstance(raw, str) else list(raw)

    declared = [
        str(item.get("name") or "").strip()
        for item in (command.parameters or ())
        if str(item.get("name") or "").strip()
    ]
    unfilled = [name for name in declared if name not in values]
    if len(positional) > len(unfilled):
        takes = ", ".join(declared) if declared else "no parameters"
        raise SqlParameterError(
            f"{command.sql_code} takes {takes}; got {len(positional)} value(s) with no name and "
            f"only {len(unfilled)} parameter(s) left to fill. Name them as NAME=VALUE."
        )
    for name, value in zip(unfilled, positional):
        values[name] = value
    return values


def load_sql_commands(path: Path, *, logger: Any = None) -> dict[int, SqlCommand]:
    data = load_json_file(path)
    commands: list[SqlCommand] = []
    for item in data.get("sql_commands", []):
        sql_code = str(item["sql_code"])
        commands.append(
            SqlCommand(
                sql_id=int(item["sql_id"]),
                sql_code=sql_code,
                sql_name=str(item.get("sql_name", "")),
                db_type=str(item.get("db_type", "")),
                **load_sql_script_definition(item, data_dir=path.parent),
                active=bool(item.get("active", True)),
                parameters=tuple(dict(p) for p in (item.get("parameters") or [])),
                autocommit=bool(item.get("autocommit", False)),
                progress_per_file=(None if item.get("progress_per_file") is None
                                   else bool(item["progress_per_file"])),
            )
        )
    return {command.sql_id: command for command in commands}


def workflow_name_from_code(sql_task_code: str) -> str:
    code = sql_task_code.lower()
    if code.startswith("data_finalize"):
        return "data_finalize"
    if code.startswith("check_errorjob"):
        return "check_errorjob"
    return code


def load_sql_script_definition(item: dict[str, Any], *, data_dir: Path) -> dict[str, Any]:
    command_name = str(item.get("sql_code", item.get("sql_id")))
    legacy_keys = [key for key in ("file_name", "file_names", "folder_name") if key in item]
    if legacy_keys:
        raise RuntimeError(f"SQL command {command_name} uses deprecated script field(s): {', '.join(legacy_keys)}. Use script_type with script_path or script_paths.")

    script_type = str(item.get("script_type", "")).strip().lower()
    if script_type not in {"single", "array", "folder"}:
        raise RuntimeError(f"SQL command {command_name} has unsupported script_type: {script_type or '<missing>'}. Expected single, array, or folder.")

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
    }


def log_deprecated_time_window_warnings(logger: Any, warnings: tuple[str, ...]) -> None:
    if logger is None:
        return
    for message in warnings:
        log_event(logger, level="warning", message=f"sql_tasks.runner.config.deprecated_time_window|scope=sql_tasks|message={format_log_value(message)}")


def load_sql_targets(path: Path, *, logger: Any = None) -> list[SqlTarget]:
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
        log_deprecated_time_window_warnings(logger, parsed_time_window.warnings)
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
    output_format = _opt_str(raw.get("format")).lower() or "none"
    if output_format not in OUTPUT_FORMATS:
        raise RuntimeError(
            f"sql_targets.sql_id={item.get('sql_id')}: output.format must be one of "
            f"{OUTPUT_FORMATS}, got {raw.get('format')!r}."
        )
    # Refused rather than clamped silently: a target asking for 20000 rows in chat has
    # misunderstood what inline output is for, and a number quietly reduced to 5000 would look
    # like it worked until somebody counted the rows.
    max_rows = _opt_str(raw.get("max_rows"))
    if max_rows and (not max_rows.isdigit() or not 0 < int(max_rows) <= MAX_INLINE_MAX_ROWS):
        raise RuntimeError(
            f"sql_targets.sql_id={item.get('sql_id')}: output.max_rows must be a whole number "
            f"between 1 and {MAX_INLINE_MAX_ROWS}, got {raw.get('max_rows')!r}. Rows go out as "
            f"~30-per-Telegram-message and a group is rate-limited to about 20 messages a "
            f"minute; export a file for more than this."
        )
    return {
        "output_format": output_format,
        "output_chat": _opt_str(raw.get("telegram_chat")),
        "output_chat_id": _opt_str(raw.get("chat_id")),
        "output_max_rows": int(max_rows) if max_rows else 0,
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
            service_name=str(item.get("service_name") or item.get("db_name") or ""),
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




def resolve_sql_files(script_files: tuple[str, ...], *, data_dir: Path) -> list[Path]:
    return [resolve_sql_file(script_file, data_dir=data_dir) for script_file in script_files]


def resolve_sql_file(file_name: str, *, data_dir: Path) -> Path:
    path = Path(file_name)
    candidates = [path] if path.is_absolute() else [
        TOOL_ROOT / path,
        # The operator's own task SQL, then the built-ins that ship with the package. `tasks/` is
        # written per server and mirrored back from the worker, so the operator's copy wins.
        *asset_candidates("tasks", str(path)),
        data_dir / path,
    ]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    raise FileNotFoundError(f"SQL file not found: {file_name}")


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


def sql_run_time(row: Any | None) -> datetime | None:
    if row is None:
        return None
    value = row["finished_at"] or row["started_at"]
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def scrub_credential(credential: dict[str, Any] | None) -> dict[str, Any] | None:
    if credential is None:
        return None
    clean = dict(credential)
    clean.pop("password", None)
    return clean


def find_database_inventory(target: SqlTarget, servers: list[dict[str, Any]]) -> dict[str, Any] | None:
    for server in servers:
        if str(server.get("server_id", "")) != target.server_id:
            continue
        for database in server.get("databases", []) or []:
            if str(database.get("db_type", "")).lower() != target.db_type.lower():
                continue
            instance_name = str(database.get("instance_name") or database.get("sid") or "")
            database_names = [str(name) for name in database.get("database_names", []) or []]
            # Identify the instance by server_id + db_type + instance_name only. The
            # connection is made by IP (service_name is not part of the SQL Server conn
            # string), so a target's service_name is NOT required to match here — the
            # actual database is picked by target.database_name or `USE <db>` in the script.
            if target.instance_name and instance_name and instance_name != target.instance_name:
                continue
            if target.database_name and database_names and target.database_name not in database_names:
                continue
            resolved = dict(database)
            resolved["server_id"] = server.get("server_id")
            resolved["company_code"] = server.get("company_code")
            resolved["ip"] = server.get("ip")
            return resolved
    return None


def find_database_credential(target: SqlTarget, credential_groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The credential this target runs as, or ``None`` — the caller reports the failed target.

    Selection is the shared :func:`db_ops.common.data_sources.find_database_credential`; a task
    target must name its credential (it always has), and an unnamed or unknown one resolves to
    nothing rather than to a guess.
    """
    try:
        return data_sources.find_database_credential(
            credential_groups,
            server_id=target.server_id,
            credential_name=target.credential_name,
            db_type=target.db_type,
            service_name=target.service_name,
            instance_name=target.instance_name,
        )
    except data_sources.CredentialNotFound:
        return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
