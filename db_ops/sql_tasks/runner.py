from __future__ import annotations
from db_ops.lib.text_format import format_log_value, format_message_time  # noqa: F401 - one definition, see that module
from db_ops.lib.data_sources import _server_id_from_instance  # noqa: F401 - one definition, see that module

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

from db_ops.lib import field_names, sql_access
from db_ops.sql_tasks import python_source as python_source_module
from db_ops.sql_tasks.python_source import PythonSource, PythonSourceError, batches
# What a task IS - read from sql_commands.json / sql_targets.json - is lib's since 0.24.0
# (lib/sql_task_catalog.py), so the bot reads the same answer in-process instead of starting this
# app's CLI (rules R42). Imported here under the names this module and its tests always used.
from db_ops.lib.sql_task_catalog import (  # noqa: F401 - re-exported for this module's callers
    _AMBIGUOUS_CREDENTIAL, DEFAULT_INLINE_MAX_ROWS, DEFAULT_SQL_TIMEOUT_SECONDS, INPUT_TYPES,
    SQL_TARGET_NOTIFY_DEFAULTS, XLSX_MAX_ROWS, SqlCommand, SqlTarget, _opt_str, collect_sql_tasks,
    load_default_credential_names, load_input_definition, load_sql_access_by_server,
    load_sql_commands, load_sql_script_definition, load_sql_targets, resolve_sql_folder)
# Imported by name, not as a module: `sql_text` is also a local variable in this file
# (the SQL itself), and a module bound to the same name shadows it silently.
from db_ops.lib.sql_text import (DEFAULT_CONNECT_TIMEOUT_SECONDS, NAMED_BIND_DB_TYPES,
                                 SqlParameterError, build_parameter_prelude,
                                 check_sqlplus_define_value, expand_sqlplus_defines,
                                 named_placeholders, resolve_password,
                                 sqlplus_substitution_names)
from db_ops.lib.notify import (
    NotifyConfig,
    NotifyRule,
    parse_notify_config,
    parse_notify_rule as common_parse_notify_rule,
)
from db_ops.lib.task_output import (
    FILE_OUTPUT_FORMATS,
    MAX_INLINE_MAX_ROWS,
    OUTPUT_FORMATS,
    TaskOutputError,
    merge_result_sets,
    parse_output,
)
from db_ops.lib.telegram_route import telegram_groups
from db_ops.lib.sql_text import DEFAULT_MAX_ROWS as SQL_RUN_MAX_ROWS
from db_ops.lib import result_format
from db_ops.db.queue_message import queue_message, store_block_from
from db_ops.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from db_ops.lib import data_sources
from db_ops.lib.data_sources import request_fill
from db_ops.transport import common_cli
from db_ops.lib import process_liveness
from db_ops.lib import sql_task_target
from db_ops.lib import run_claim
from db_ops.lib.secret_text import add_key_argument, set_key_env
# Connecting and executing are `common`'s, reached through `common.cli run-sql` — this app
# imported nine driver-level helpers from `sql_execution` for a connection it no longer opens, and
# had stopped using most of them long before (2026-08-16). What is left is what was always a
# value: how to declare a script's parameters, how to read a credential's password, and the JSON
# reader — all of them `lib`, importable by anything.
from db_ops.lib.json_io import load_json_file
from db_ops.lib.time_window import MANUAL_ONLY, TimeWindow, due_from_row, is_time_window_open as common_time_window_open, parse_time_window_config, repeat_due, run_anchor
from db_ops.lib.timezone import display_now, file_stamp
from db_ops.db import DbOpsStore
from db_ops.db.store import RunAlreadyClaimed, utc_now_text
from db_ops.logging_ops import log_event, log_function_error, setup_app_logger
from db_ops.logging_ops.runtime_stdout import patch_stdout
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.paths import asset_candidates


# What goes into sql_runs.result_json regardless of how many rows were fetched: an export must
# not turn every run row into a multi-megabyte JSON blob in the store. **Its own number, not an
# alias of the inline cap** — it used to be `= MAX_RESULT_ROWS`, so raising how much an operator
# sees in chat would have quietly multiplied the size of every stored run row too.
STORED_RESULT_MAX_ROWS = 100
#: Kept for callers that imported it; the inline default now carries the meaning.
MAX_RESULT_ROWS = DEFAULT_INLINE_MAX_ROWS

#: How many of a script's result sets are kept, for the store row and the Telegram table. Five,
#: because that is what `execute_cursor_batches` kept before this app called `run-sql` instead and
#: `sql_runs.result_json` is read against it. The rows of the sets beyond it are still *counted*
#: into `row_count` — dropping them from the total would make a run look smaller than it was.
MAX_STORED_RESULT_SETS = 5


@dataclass(frozen=True)
class ExecutionStep:
    """One SQL file, run once, with the parameter values that run gets.

    A plain task has one step per file and this is only bookkeeping. A ``script_type: "python"``
    task has one step per (batch, file): the same SQL runs again for the next few thousand rows,
    with the batch bound to its parameter. Building the whole plan first is what keeps that a
    single loop with a single failure path, rather than a nested one where "which file failed"
    stops being a straight answer.
    """

    file_no: int
    total: int
    sql_path: Path
    configured_name: str
    #: ``[3/30]`` for a batched task, ``""`` for every other one, so a message reads
    #: ``[2/4] load.sql [3/30]`` only when there is a second axis to report.
    batch_label: str
    parameter_values: dict[str, Any]

    @property
    def label(self) -> str:
        base = f"[{self.file_no}/{self.total}] {self.sql_path.name}"
        return f"{base} {self.batch_label}" if self.batch_label else base


def build_execution_plan(
    *,
    sql_paths: list[Path],
    configured_names: tuple[str, ...],
    parameter_values: dict[str, Any] | None,
    payloads: list[str] | None = None,
    payload_parameter: str = "",
    final_paths: list[Path] | None = None,
    final_names: tuple[str, ...] = (),
) -> list[ExecutionStep]:
    """The ordered list of SQL executions this run performs.

    ``payloads`` is the batched JSON from a python source; without it the plan is what it has
    always been, one step per file in order. With it, the *files* stay the inner loop: batch 1
    runs every file, then batch 2 does, because a task's files are a sequence that belongs
    together (stage, then merge, then log) and running file 1 thirty times before file 2 has ever
    run would break every folder task's meaning of order.
    """
    base = dict(parameter_values or {})
    chunks = payloads if payloads is not None else [None]
    final_paths = list(final_paths or ())
    total = len(sql_paths) * len(chunks) + len(final_paths)
    steps: list[ExecutionStep] = []
    file_no = 0
    for batch_no, payload in enumerate(chunks, start=1):
        for index, sql_path in enumerate(sql_paths):
            file_no += 1
            values = dict(base)
            if payload is not None and payload_parameter:
                values[payload_parameter] = payload
            steps.append(ExecutionStep(
                file_no=file_no, total=total, sql_path=sql_path,
                configured_name=configured_names[index] if index < len(configured_names)
                else str(sql_path),
                batch_label=f"[batch {batch_no}/{len(chunks)}]" if payload is not None else "",
                parameter_values=values,
            ))
    # Once, after the last batch, and **without the payload bound**: a final step is not a row
    # consumer. Binding it anyway would hand it the last batch, which is the subtlest possible
    # way to write a step that looks like it saw everything and saw one batch of it.
    for index, sql_path in enumerate(final_paths):
        file_no += 1
        steps.append(ExecutionStep(
            file_no=file_no, total=total, sql_path=sql_path,
            configured_name=final_names[index] if index < len(final_names) else str(sql_path),
            batch_label="[final]" if payloads is not None else "",
            parameter_values=dict(base),
        ))
    return steps


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


def parse_notify_rule(value: Any, *, default_level: str) -> NotifyRule:
    """Parse one ``logging_on_run``/``alert_on_error`` switch on a SQL target.

    A thin adapter over :func:`db_ops.lib.notify.parse_notify_rule`, which owns the shape
    for every app. Kept for callers that hold a single rule rather than a whole entry.
    """
    return common_parse_notify_rule(
        value, default=NotifyRule(enabled=False, telegram_chat=default_level)
    )


@dataclass(frozen=True)
class SqlScanResult:
    due_count: int = 0
    success_count: int = 0
    error_count: int = 0
    #: Tasks a **claim** turned away because another run already holds them. Neither a success nor
    #: a failure, and counted apart from both: folding it into errors would alert on the scheduler
    #: working correctly, and folding it into successes would say work happened that did not.
    skipped_count: int = 0


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
        "--confirm",
        default="",
        metavar="yes",
        help='The answer to the forced-run confirmation, when a human answered it somewhere '
             'other than this terminal. `/spbot_run_sql_task` asks the question over Telegram '
             'and passes the reply here. At a terminal, leave it out and the prompt is asked.',
    )
    run_sql_id.add_argument(
        "--assume-yes",
        action="store_true",
        help="Unattended automation: proceed without asking anybody. Recorded as such, so a run "
             "nobody watched never reads afterwards like a run somebody approved.",
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


#: This forced run's name in `data/emergency_operations.json`. A forced run is not an emergency,
#: but "how hard is this to authorize" has exactly one file in this project and one ladder in it.
FORCED_RUN_OPERATION = "run-sql-task"


def authorize_forced_run(
    *,
    sql_id: int,
    data_dir: Path,
    answer: str = "",
    assume_yes: bool = False,
    channel: str = "",
    echo: Any = None,
) -> bool:
    """One typed ``yes`` before a forced run — asked wherever the run was started from.

    ``--force`` is *intent*: it says the caller means to skip the time window, the repeat interval
    and the active flag. What it never said is *presence* — that somebody is looking at this task
    right now. Until 2026-09-04 the only guard was the Telegram clearance, and clearance answers
    who may ask, never how hard it is to ask: raising `/spbot_run_sql_task` to `command_type` 50
    put it beside `/spbot_kill_spid` in who may run it while it still cost nothing to run, which
    is the mismatch this closes. The gate is on the forced path only — a scheduled scan authorized
    itself when the operator wrote the schedule.

    The answer is read exactly the way `kill-spid` reads it, because a safety control that spells
    itself differently per command is one an operator cannot learn once: from the request when a
    human answered elsewhere (Telegram asks the prompt and passes ``--confirm yes``), from the
    terminal when there is one, refused when neither, and waived only by an explicit
    ``--assume-yes``.

    The banner names the task and the targets it will touch, because "run task 24?" tells an
    operator nothing they can check.
    """
    label = f"sql_id {sql_id}"
    effects: list[str] = ["the time window, the repeat interval and the active flag are skipped"]
    try:
        listing = collect_sql_tasks(data_dir, sql_id=int(sql_id), include_inactive=True)
        tasks = list(listing.get("sql_tasks") or [])
    except (OSError, ValueError, RuntimeError):
        # A config the run is about to fail on anyway. Still ask — a gate that opens itself when
        # it cannot read the target is a gate that opens on the day the file is wrong.
        tasks = []
    if tasks:
        task = tasks[0]
        name = str(task.get("display_name") or task.get("sql_code") or "").strip()
        label = f"sql_id {sql_id} {name}".strip()
        targets = list(task.get("targets") or [])
        where = ", ".join(str(item.get("server_id") or "?") for item in targets)
        effects.insert(0, f"runs on {len(targets)} target(s): {where or 'none configured'}")
        if not task.get("active", True):
            effects.append("this task is INACTIVE — nothing but a forced run reaches it")

    request: dict[str, Any] = {
        "operation": FORCED_RUN_OPERATION,
        "target_id": str(sql_id),
        "target_label": label,
        "effects": effects,
        # `--force` is the declared intent, so the flag itself sets it. The word `yes` is the
        # separate thing: the answer. Intent alone still asks, at a terminal and over Telegram.
        "confirm": str(answer).strip() or True,
        "reason": "forced run of a configured SQL task",
    }
    if assume_yes:
        request["assume_yes"] = True
    if channel:
        request["authorized_by"] = {"channel": channel}

    # Asked over the CLI, not imported: `common` is the API layer and an app calls it (ORD 13,
    # tests/test_app_common_imports.py). No deadline — there may be a human reading the prompt.
    # What the operation costs is this node's ladder, read here and sent as `rules`: common.cli
    # reads no configuration (rules R09).
    from db_ops.lib.data_sources import request_fill

    request = request_fill.fill_request("authorize", request)
    try:
        authorized, report, error = common_cli.run_allowing_failure("authorize", request)
    except common_cli.CommonCliError as exc:
        # The gate could not be asked at all. That is a refusal: a confirmation that fails open
        # is not a confirmation.
        if echo is not None:
            echo(f"[FAIL] confirm: {exc}")
        return False
    if not authorized and echo is not None:
        for gate in report.get("gates") or ():
            if str(gate.get("status")) != "OK":
                echo(f"[{gate.get('status')}] {gate.get('name')}: {gate.get('detail')}")
        if not (report.get("gates") or ()) and error:
            echo(f"[FAIL] confirm: {error}")
    return bool(authorized)


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
            # A rehearsal is not a run: asking to confirm something that will not happen is how
            # people learn to answer without reading (db_ops.common.confirm says the same).
            if not args.dry_run and not authorize_forced_run(
                sql_id=int(args.sql_id),
                data_dir=data_dir,
                answer=_opt_str(args.confirm),
                assume_yes=bool(args.assume_yes),
                channel="telegram" if _opt_str(args.output_chat_id) else "",
                echo=lambda line: print(line, file=sys.stderr, flush=True),
            ):
                log_event(logger, level="logging", message=f"sql_tasks.runner.refused|scope=sql_tasks|mode=force|sql_id={args.sql_id}|reason=not_confirmed")
                print(f"Forced run of sql_id {args.sql_id} was not authorized. Nothing ran.", file=sys.stderr)
                return 1
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
    commands = load_sql_commands(data_dir / "sql_commands.json", on_warning=_deprecation_logger(logger))
    targets = load_sql_targets(data_dir / "sql_targets.json", on_warning=_deprecation_logger(logger))
    secrets = data_sources.load_secret_text(data_dir)
    inventory = data_sources.load_inventory(data_dir)
    credentials = data_sources.load_all_credentials(data_dir)
    mark_stale_running_sql_runs(store=store, commands=commands, targets=targets,
                                running_runs=store.fetch_running_sql_runs(),
                                telegram_groups=telegram_groups, logger=logger)
    listed_at = utc_now_text()
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
    skipped_count = 0
    for command, target in due_pairs:
        if dry_run:
            print(
                f"{command.sql_code} target={target.target_no} server={target.server_id} "
                f"db={sql_task_target.connect_database(target.db_type, target.database_name, target.service_name)} script_type={command.script_type} "
                f"files={len(command.script_files)} file_order={format_script_file_order(command)}"
            )
            continue
        # Due when this scan listed it is not the same as still due. APP-SQL_TASKS is async: while
        # one scan works through a slow task, the next takes the tasks behind it - and the claim
        # stops the two only while a task is RUNNING, not the first from running it again once the
        # second has finished it (the backup/restore schedule test, 2026-09-25, found the same gap
        # there). On a production target that is the same SQL twice.
        if store.sql_run_started_since(target.run_key, listed_at):
            log_sql_task_event(
                logger, "sql_tasks.runner.task.taken_by_another_scan", command=command, target=target,
                sql_id=command.sql_id, sql_code=command.sql_code, status="skipped",
                reason="run by another scan since this one listed it")
            skipped_count += 1
            continue
        # A refused claim is an answer, not a failure: another scan is already running this task.
        # It is counted as neither a success nor an error, because it is neither — recording it as
        # an error would alert on the scheduler working correctly, which teaches the reader to stop
        # reading the alerts.
        try:
            success = run_one_sql_task(
                # None, not a caller's values: a scheduled scan has no operator to take parameters
                # from, so each task falls back to the defaults it declares itself (see
                # build_parameter_prelude). This line used to read
                # `parameter_values=parameter_values`, a name that exists only on the single-task
                # path — so **every** scan raised NameError before running a single task. The outer
                # handler turned that into "exit 1" once a minute, which is indistinguishable from a
                # scheduler with nothing due: manual runs kept working and scheduled SQL tasks
                # silently stopped for a day (last scheduled run 2026-08-12T09:22Z, found
                # 2026-08-13).
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
        except RunAlreadyClaimed as exc:
            log_sql_task_event(
                logger, "sql_tasks.runner.task.already_running", command=command, target=target,
                sql_id=command.sql_id, sql_code=command.sql_code, status="skipped",
                reason=str(exc))
            skipped_count += 1
            continue
        if success:
            success_count += 1
        else:
            error_count += 1
    return SqlScanResult(due_count=len(due_pairs), success_count=success_count,
                         error_count=error_count, skipped_count=skipped_count)


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
    commands = load_sql_commands(data_dir / "sql_commands.json", on_warning=_deprecation_logger(logger))
    targets = load_sql_targets(data_dir / "sql_targets.json", on_warning=_deprecation_logger(logger))
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
                f"db={sql_task_target.connect_database(target.db_type, target.database_name, target.service_name)} script_type={command.script_type} "
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
    running_runs: Any,
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

    **Every running row, not the newest one per task.** This read `fetch_latest_*_by_run_key`
    until 2026-09-04, which is the schedule's view of a task, not the list of runs that never
    ended. A worker restart killed sql_id 28 mid-run twice that day; each time the next cycle
    started a fresh run while the killed row was still inside its 950 s timeout, and from that
    moment the killed row was never "latest" again — so it never came back through here, never
    became an error, and never sent the alert its target asks for. The operator saw runs stop
    failing, not runs failing silently, which is the worse of the two.
    """
    now = datetime.now(timezone.utc)
    this_host = socket.gethostname()
    targets_by_key = {(target.sql_id, target.target_no): target for target in targets}
    for row in running_runs:
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
        # **A row whose process is still alive is left alone, however old it is.** The timeout used
        # to be the whole answer, and that is the loop: a task that legitimately outran its timeout
        # had its row closed here and the next scan started a second copy on top of the first. The
        # `running` row is now a claim (ux_sql_runs_claim), so closing it is what releases the key —
        # which makes this decision and "may another run start?" the same decision. A row from
        # another host cannot be checked from here and waits for its timeout plus a long grace.
        metadata = run_claim.row_metadata(row)
        owner_pid, owner_host = run_claim.claim_owner(metadata)
        verdict = run_claim.reap_verdict(
            metadata=metadata,
            this_host=this_host,
            elapsed_seconds=(now - started).total_seconds(),
            timeout_seconds=timeout_seconds,
            pid_alive=(process_liveness.is_pid_alive(owner_pid)
                       if owner_pid is not None and owner_host == this_host else None),
        )
        if not verdict.reap:
            continue
        # The two clock readings are the first thing anyone asks for: an alert that says only
        # "stale" leaves the reader to go and find out *when* the run they are being told about
        # died, and a reap can happen hours after the fact — a worker restarted at 06:13 was
        # reported at 13:05 with nothing in the text to tell the two apart.
        stale_minutes = int((now - started).total_seconds() // 60)
        message = (f"SQL task {row['sql_code']} stale running: {verdict.reason}. It started at "
                   f"{format_message_time(started)} with timeout_seconds={timeout_seconds} and was "
                   f"still 'running' {stale_minutes} minutes later.")
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
                         f"executing on {target_location(target)} - check for an "
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
    # Two clocks on purpose, and only one of them is configurable: `now` is the instant
    # intervals are measured on (UTC, like every stored timestamp), `local_now` is the
    # wall clock a from_hour/to_hour window is read against.
    local_now = display_now()
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
        if due_from_row(
            time_window=target.time_window,
            row=recent,
            now=now,
            retry_default=target.time_window.repeat_interval,
            default_repeat=300,
        ).due:
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
        "final_sql_files": list(command.final_script_files),
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
            message=f"SQL task {command.sql_code} started on {target_location(target)}.",
            sql_run_id=sql_run_id,
        )

    # Which file is in flight, so a failure names it. A folder task fails with a SQL error and
    # nothing else; "invalid object name" across six scripts is six places to look.
    failing_file = ""
    total_files = 0
    try:
        if database is None:
            # Only what connecting needs is checked here - the instance. Whether the database exists
            # is the server's to say, after connecting (see diagnose_connect_failure).
            raise RuntimeError(sql_task_target.instance_not_found_message(
                server_id=target.server_id, db_type=target.db_type,
                instance_name=target.instance_name,
                # Each record under its server's id: the loader copies it onto the record, but the
                # server is what it belongs to, and a record without it read as "no such server".
                records=[{**record, "server_id": server.get("server_id")}
                         for server in inventory for record in server.get("databases") or []]))
        if target.credential_name == _AMBIGUOUS_CREDENTIAL:
            raise RuntimeError(
                f"{target.server_id} runs more than one {target.db_type} instance, so server_id "
                "alone does not say which one to run against. Set instance_name (and "
                "credential_name) on this sql_targets entry."
            )
        if credential is None:
            # The lookup's own reason, not just the name: it says where the credential is when the
            # target does not match its group, which "Credential not found: <name>" hid (1.10).
            raise RuntimeError(credential_problem(target, credentials)
                               or f"Credential not found: {target.credential_name}")
        password = resolve_password(credential, secrets)
        total_row_count = 0
        run_warnings: list[str] = []
        payloads = None
        if command.python_source is not None:
            # Before a single statement runs: a fetch that fails must cost nothing, and a task
            # that opened a transaction and then went to an HTTP API would hold one open for the
            # length of the fetch.
            payloads = _run_python_source(
                command=command, target=target, data_dir=data_dir, metadata=metadata,
                parameter_values=parameter_values, logger=logger,
            )
        steps = build_execution_plan(
            sql_paths=sql_paths,
            configured_names=command.script_files,
            parameter_values=parameter_values,
            payloads=payloads,
            payload_parameter=(command.python_source.parameter
                               if command.python_source is not None else ""),
            final_paths=resolve_sql_files(command.final_script_files, data_dir=data_dir),
            final_names=command.final_script_files,
        )
        # Automatic unless the command says otherwise: worth it for a folder of scripts, noise
        # for a single file. Resolved here because this is the only scope that knows the count.
        # A batch is not a file: a Python-fed task runs one script per batch, and reporting each
        # one sent a message per 2000 rows - 70 for one run of a ten-day load. Batched tasks
        # report per batch only when the command asks for it.
        total_files = len(steps)
        automatic = total_files > 1 and command.python_source is None
        report_progress = (automatic if command.progress_per_file is None
                           else bool(command.progress_per_file))
        for step in steps:
            file_no = step.file_no
            sql_path = step.sql_path
            file_started = datetime.now(timezone.utc)
            # `script_files` holds absolute paths for a folder task, so the configured name is
            # the master's full path — useless in a chat message and it leaks a build-host path.
            # The file name is what identifies the step; the full path stays in the log line.
            configured_file_name = step.configured_name
            display_name = sql_path.name
            failing_file = step.label
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
            try:
                result = execute_sql(
                    command=command,
                    target=target,
                    database=database,
                    credential=credential,
                    password=password,
                    sql_text=sql_text,
                    parameter_values=step.parameter_values,
                    secrets=secrets,
                )
            except RuntimeError as exc:
                explained = diagnose_connect_failure(
                    target=target, error=str(exc),
                    connection=task_connection(target=target, database=database, credential=credential,
                                               password=password))
                if explained is None:
                    raise
                raise RuntimeError(f"{explained} ({exc})") from exc
            file_finished = datetime.now(timezone.utc)
            file_duration_ms = int((file_finished - file_started).total_seconds() * 1000)
            file_result = {
                "file_no": file_no,
                "file_name": configured_file_name,
                "sql_file": str(sql_path),
                "batch": step.batch_label,
                "status": "done",
                "duration_ms": file_duration_ms,
                "row_count": int(result.get("row_count") or 0),
                "result_sets": result.get("result_sets", []),
            }
            if result.get("warnings"):
                file_result["warnings"] = list(result["warnings"])
                run_warnings.extend(f"{step.label}: {text}" for text in result["warnings"])
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
                        f"SQL task {command.sql_code} {step.label} done on "
                        f"{target_location(target)} in {file_duration_ms} ms, "
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
                elif (left_out := unexported_result_sets(result, target.output_format)):
                    metadata["output_note"] = (
                        f"output={target.output_format} holds one table; {left_out} more result "
                        f"set(s) with other columns are not in the file - use txt, csv or json."
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
        # Done either way: a warning is not a failure. It is kept in the level and the message, so
        # a run that finished past one is not indistinguishable from one that saw nothing.
        warning_note = (f" {len(run_warnings)} SQL Server warning(s); the first: {run_warnings[0]}"
                        if run_warnings else "")
        store.update_sql_run(
            sql_run_id=sql_run_id,
            status="done",
            level="warning" if run_warnings else "logging",
            message=f"SQL task {command.sql_code} finished.{warning_note}",
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
                message=(f"SQL task {command.sql_code} finished on {target_location(target)} "
                         f"in {duration_ms} ms.{warning_note}"),
                sql_run_id=sql_run_id,
                result=result,
                include_result_table=not delivered_to_requester,
                # The header is what the severity emoji is read from: "done with a warning" is a
                # warning, not a success - and not the failure it used to be recorded as.
                headline="done with a warning" if run_warnings else None,
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
                         f"{target_location(target)}"
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


def _run_python_source(
    *,
    command: SqlCommand,
    target: SqlTarget,
    data_dir: Path,
    metadata: dict[str, Any],
    parameter_values: dict[str, Any] | None,
    logger: Any,
) -> list[str]:
    """Run the task's ``input_type: "python"`` step and return its rows, batched as JSON.

    Everything it learned goes on ``metadata`` before anything is sent to the database, so a run
    that then fails in the SQL still records what the fetch produced — how many rows, how long it
    took, what the script said on stderr, and whatever else its document carried at the top level
    (``status``, ``total``, ``errors`` on the estate's first one). A failed fetch that leaves no
    trace is a task that "failed" with nothing to look at.

    The tool root, not ``data_dir``: a script is addressed the way ``assets/`` is everywhere else
    in this tree, and ``data/`` is the folder beside it rather than the root.
    """
    source = command.python_source
    assert source is not None  # only called when input_type == python
    tool_root = Path(data_dir).parent
    log_sql_task_event(
        logger,
        "sql_tasks.runner.input.python.start",
        command=command,
        target=target,
        sql_id=command.sql_id,
        sql_code=command.sql_code,
        input_type=command.input_type,
        script=source.script_path,
    )
    try:
        produced = python_source_module.run(
            source, tool_root=tool_root, parameter_values=parameter_values,
            target={"target_server_id": target.server_id,
                    "target_database": target.database_name})
    except PythonSourceError as exc:
        metadata["input"] = {"type": command.input_type, "script": source.script_path,
                             "status": "failed", "error": str(exc)}
        log_sql_task_event(
            logger,
            "sql_tasks.runner.input.python.error",
            command=command,
            target=target,
            level="error",
            error=str(exc),
        )
        raise RuntimeError(str(exc)) from exc

    payloads = batches(produced.rows, source.batch_rows)
    metadata["input"] = {
        "type": command.input_type,
        "script": source.script_path,
        "status": "done",
        "batches": len(payloads),
        "batch_rows": source.batch_rows,
        "parameter": source.parameter,
        **produced.summary(),
        # Kept whether or not the script failed: a fetcher that succeeds and warns is the case
        # nobody looks at, and it is the one where a silently halved pull hides.
        "stderr_tail": produced.stderr_tail,
    }
    log_sql_task_event(
        logger,
        "sql_tasks.runner.input.python.done",
        command=command,
        target=target,
        sql_id=command.sql_id,
        sql_code=command.sql_code,
        rows=len(produced.rows),
        batches=len(payloads),
        duration_ms=produced.duration_ms,
        exit_code=produced.exit_code,
    )
    return payloads


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

    Reads the ``result_sets`` ({"columns": [...], "rows": [[...]]}) captured per file by the
    executor. Consecutive sets with the same columns are one table (``merge_result_sets``): a task
    that runs once per batch returns a set per batch, and 69 one-row tables were 69 headers
    around 69 rows. Wide tables are clipped to ``RESULT_TABLE_MAX_COLS`` columns
    with a note. **Every fetched row is rendered** — the message is split across sends if it is
    long, rather than the rows being dropped. Returns "" when there is nothing tabular to show.
    """
    if not result:
        return ""
    blocks: list[str] = []
    for set_index, rset in enumerate(merge_result_sets(all_result_sets(result)), start=1):
        columns = list(rset.get("columns") or [])
        rows = list(rset.get("rows") or [])
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


def all_result_sets(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every result set of every file, in the order they ran."""
    return [rset for file_result in (result or {}).get("files", []) or []
            for rset in file_result.get("result_sets", []) or []]


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


def write_sql_task_output(
    *,
    command: SqlCommand,
    target: SqlTarget,
    result: dict[str, Any] | None,
    sql_run_id: int,
    output_dir: Path,
    output_format: str = "",
) -> Path | None:
    """Write the task's result sets as its configured file, and return the path.

    ``None`` when the script returned no result set — a task that exports nothing is not an
    error, and the caller records it as a note on the run rather than failing SQL that already
    committed.

    **Every row, not the first set.** Consecutive sets with the same columns are joined
    (``merge_result_sets``), so a task that runs once per batch exports one table. The file used
    to hold only the first set, which for a batched load was one row of sixty-nine. When the
    script returns sets of *different* shapes, ``txt`` and ``csv`` write each as its own section
    and ``json`` writes a list; ``xlsx`` and ``xml`` hold one table, so they write the first and
    the caller notes the rest (:func:`unexported_result_sets`).

    The rendering goes through :mod:`db_ops.lib.result_format`, the same code path
    ``run-sql --format`` uses, so a scheduled export and an ad-hoc one are the same artifact.
    """
    sets = merge_result_sets(all_result_sets(result))
    if not sets:
        return None
    fmt = (output_format or target.output_format or "xlsx").strip().lower()
    stamp = file_stamp()
    name = f"sql_{command.sql_id:03d}_{workflow_name_from_code(command.sql_code)}_{stamp}.{fmt}"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / name
    payloads = [{"ok": True, "columns": rset["columns"], "rows": rset["rows"],
                 "row_count": len(rset["rows"])} for rset in sets]
    if len(payloads) > 1 and fmt in MULTI_SET_FILE_FORMATS:
        if fmt == "json":
            text = json.dumps(payloads, ensure_ascii=False, default=str, indent=1)
        else:
            text = "\n\n".join(result_format.render_result(payload, fmt=fmt)[0]
                               for payload in payloads)
        path.write_text(text, encoding="utf-8")
        return path
    # One call, no branch on format. Which formats write themselves and which hand back a string
    # is result_format's problem, not this app's.
    result_format.write_result(payloads[0], fmt=fmt, path=path, sheet_name=f"sql_{command.sql_id}")
    return path


#: File formats that can hold result sets of different shapes in one file.
MULTI_SET_FILE_FORMATS = ("txt", "csv", "json")


def unexported_result_sets(result: dict[str, Any] | None, output_format: str) -> int:
    """How many result sets a file of ``output_format`` leaves out: only ``xlsx`` / ``xml``, and
    only when the script returned sets of more than one shape."""
    if output_format in MULTI_SET_FILE_FORMATS:
        return 0
    return max(0, len(merge_result_sets(all_result_sets(result))) - 1)


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
    headline: str | None = None,
) -> None:
    level = rule.telegram_chat
    chat_id = rule.resolve_chat_id(telegram_groups)
    if not chat_id:
        return
    lines = [
        f"[{level.upper()}] SQL task {headline or status}",
        f"sql_code: {command.sql_code}",
        f"display_name: {command.sql_name}",
        f"server_id: {target.server_id}",
        # SQL Server has no service name - `service_name` there is a label the connection never
        # uses, and printing it made it read as part of the path. It shows the database opened.
        (f"database_name: {sql_task_target.connect_database(target.db_type, target.database_name, target.service_name)}"
         if sql_task_target.is_sqlserver(target.db_type) else f"service_name: {target.service_name}"),
        f"instance_name: {target.instance_name}",
        f"target_no: {target.target_no}",
        f"sql_run_id: {sql_run_id}",
        # When this happened, on the reader's clock. A Telegram message carries the time it was
        # *delivered*, which is not the time of the event: the queue can hold a row while the
        # sender backs off, and a reaped run is reported however long after it died. The line is
        # UTC unless DB_OPS_MESSAGE_UTC_OFFSET_HOURS says otherwise, and it always names the
        # offset, so it can be compared with sql_runs.started_at without arithmetic.
        f"time: {format_message_time()}",
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
      does not declare rather than letting it match no ``&VAR`` and vanish. On a direct Oracle or
      PostgreSQL connection the script says ``:name`` and the value is bound by name (or, on
      Oracle, ``&name`` as on the bridge) - :func:`named_parameter_values`.

    Returns the shape the rest of this runner reads — ``{"row_count", "result_sets",
    "truncated"}`` — which is not ``run-sql``'s own, so the mapping is right below and explained
    where it differs.
    """
    engine = sql_access.normalize_db_type(target.db_type)
    if engine not in sql_access.SQL_TASK_DB_TYPES:
        raise RuntimeError(f"Unsupported db_type: {target.db_type}")

    # The login this runner already resolved, stated whole: run-sql reads no configuration (rules
    # R09), so a server_id alone would be refused. The target stays as the answer's label.
    request: dict[str, Any] = {
        "target": target.server_id,
        "connection": task_connection(target=target, database=database, credential=credential,
                                      password=password),
        "database": target.database_name or "",
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
        # The bridge's signing secret, from the store this runner already opened.
        request["secrets"] = request_fill.bridge_secrets(target.sql_access, secrets or {})
    elif engine in NAMED_BIND_DB_TYPES:
        # Not the T-SQL prelude: neither engine reads a DECLARE, and until 0.24.0 an Oracle task with
        # parameters failed at its first run on a direct connection (0.23.0 section 1.55).
        defines, named = named_parameter_values(command, parameter_values, sql_text, db_type=engine)
        if defines:
            request["define"] = defines
        if named:
            request["named_params"] = named
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
        # A SQL Server warning that ended the reading (lib/driver_warnings.py). The run is done;
        # the warning, and what it hid, are said rather than turned into a failure. Only present
        # when there is one, so a clean run's result keeps the shape every reader already has.
        **({"warnings": [str(item) for item in result["warnings"]]} if result.get("warnings") else {}),
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


def named_parameter_values(
    command: SqlCommand, parameter_values: dict[str, Any] | None, sql_text: str, *, db_type: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    """This file's parameters on a direct Oracle or PostgreSQL connection, as ``(define, named)``.

    **The script says which it is.** ``:name`` is bound: the value never becomes SQL text, so a
    quote in it is data. On Oracle ``&name`` is a SQL*Plus substitution, exactly as on an 8i bridge
    target, so a command registered for both transports (``ORACLE-019`` is one) means the same on
    either; its value is checked as :func:`legacy_define_values` checks it, and with no value the
    script's own ``DEFINE`` stands. A parameter this file does not mention is left out: a folder
    task's other files may, and ``run-sql`` refuses a value no statement reads.

    As on SQL Server, a supplied value wins, then the declared ``default``, and a bound parameter
    with neither is NULL; ``required`` with no value is refused before anything connects. A name the
    command does not declare is refused, as on the bridge.
    """
    declared = {str(p.get("name") or "").strip().lower(): dict(p) for p in command.parameters or ()}
    supplied = {str(name).strip().lower(): value for name, value in (parameter_values or {}).items()}
    unknown = sorted(set(supplied) - set(declared))
    if unknown and declared:
        raise SqlParameterError(
            f"{command.sql_code} does not declare parameter(s) {', '.join(unknown)}; "
            f"it takes {', '.join(sorted(declared)) or 'none'}."
        )

    bound_here = named_placeholders(sql_text, db_type)
    substituted_here = sqlplus_substitution_names(sql_text) if db_type == "oracle" else set()
    defines: dict[str, str] = {}
    named: dict[str, Any] = {}
    for name, parameter in declared.items():
        given = supplied.get(name)
        if given is not None and str(given).strip():
            value = given
        elif "default" in parameter:
            value = parameter["default"]
        elif bool(parameter.get("required", False)):
            raise SqlParameterError(
                f"{command.sql_code} requires parameter {name}: pass --param {name}=<value>.")
        else:
            value = None
        if name in bound_here:
            named[name] = value
        if name in substituted_here and str(value if value is not None else "").strip():
            check_sqlplus_define_value(name, str(value))
            defines[name] = str(value)
    return defines, named


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


def workflow_name_from_code(sql_task_code: str) -> str:
    code = sql_task_code.lower()
    if code.startswith("data_finalize"):
        return "data_finalize"
    if code.startswith("check_errorjob"):
        return "check_errorjob"
    return code


def _deprecation_logger(logger: Any):
    """What the configuration reader hands back while reading, logged the way the runner always did."""
    if logger is None:
        return None
    return lambda message: log_deprecated_time_window_warnings(logger, (message,))


def log_deprecated_time_window_warnings(logger: Any, warnings: tuple[str, ...]) -> None:
    if logger is None:
        return
    for message in warnings:
        log_event(logger, level="warning", message=f"sql_tasks.runner.config.deprecated_time_window|scope=sql_tasks|message={format_log_value(message)}")


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


def sql_run_time(row: Any | None) -> datetime | None:
    """When a ``sql_runs`` row **started**, as an aware UTC datetime.

    ``finished_at`` came first here, and it made this app the odd one out: a target declaring
    ``repeat_interval: 300`` whose task took 240 seconds ran every 540, and the config said 300.
    Every other ``time_window`` consumer anchors on the start, so the same number meant two things
    depending on which file it was written in. Changed 2026-09-19 to the shared
    :func:`db_ops.lib.time_window.run_anchor`, which is now the only place that picks the column.
    """
    return run_anchor(row)


def scrub_credential(credential: dict[str, Any] | None) -> dict[str, Any] | None:
    if credential is None:
        return None
    clean = dict(credential)
    clean.pop("password", None)
    return clean


def target_location(target: SqlTarget) -> str:
    """Where this target runs, as a message names it: ``server/instance.database`` on SQL Server."""
    return sql_task_target.location(server_id=target.server_id, db_type=target.db_type,
                                    instance_name=target.instance_name,
                                    service_name=target.service_name,
                                    database_name=target.database_name)


def task_connection(*, target: SqlTarget, database: dict[str, Any], credential: dict[str, Any],
                    password: str) -> dict[str, Any]:
    """The ``connection`` run-sql takes for this target: the instance and login resolved above."""
    return request_fill.connection_from(database, credential, password, server_id=target.server_id)


def diagnose_connect_failure(*, target: SqlTarget, error: str,
                             connection: dict[str, Any] | None = None) -> str | None:
    """What a failure to connect means, in the server's own terms - or ``None`` when ``error`` is
    not about connecting (a SQL error inside the script is reported as it is).

    Connect first, ask on failure: the database list is read from the server only when the
    database could not be opened, so a working target costs nothing and a new database needs no
    config change. The listing is a read of ``sys.databases`` in ``master`` with the same login.
    """
    kind = sql_task_target.classify_connect_failure(error)
    if kind is None:
        return None
    where = target_location(target)
    if kind == "login":
        return (f"the login of {target.credential_name or 'this target'} was refused on {where} "
                "(18456) - check its password_ref and that the login exists")
    if kind == "unreachable":
        return (f"could not reach {where} - the instance is down, its address or port is wrong, "
                "or something between them blocks it")
    database_name = sql_task_target.connect_database(target.db_type, target.database_name,
                                                     target.service_name)
    if not sql_task_target.is_sqlserver(target.db_type):
        return f"database '{database_name}' could not be opened on {where}"
    instance = f"{target.server_id}/{target.instance_name or sql_task_target.SQLSERVER_DEFAULT_INSTANCE}"
    if not connection:
        return (f"database '{database_name}' could not be opened on {instance}, and without the "
                "login the server's databases could not be listed")
    ok, answer, listing_error = common_cli.run_allowing_failure("run-sql", {
        "target": target.server_id,
        "connection": connection,
        "database_name": "",
        "sql_text": "SELECT name FROM sys.databases ORDER BY name;",
        "timeout_seconds": 60,
        "sql_access": target.sql_access or {},
    })
    if not ok:
        return (f"database '{database_name}' could not be opened on {instance}, and the server's "
                f"databases could not be listed either: {listing_error}")
    names = [str(row[0]) for row in (answer or {}).get("rows") or [] if row]
    return sql_task_target.missing_database_message(database_name=database_name, where=instance,
                                                    existing=names)


def find_database_inventory(target: SqlTarget, servers: list[dict[str, Any]]) -> dict[str, Any] | None:
    for server in servers:
        if str(server.get("server_id", "")) != target.server_id:
            continue
        for database in server.get("databases", []) or []:
            if str(database.get("db_type", "")).lower() != target.db_type.lower():
                continue
            instance_name = str(database.get("instance_name") or database.get("sid") or "")
            # The instance only: server_id + db_type + instance_name, which is what connecting needs.
            # `service_name` is not part of a SQL Server connection. `database_names` used to be a
            # gate here too, compared case-sensitively - and it is a list no code writes, so a
            # database created yesterday was refused and `APPDB_PROD` failed against `APPDB_Prod`
            # (SQL033, 2026-09-24). The server says whether a database exists, after connecting.
            if not sql_task_target.instance_matches(instance_name, target.instance_name, target.db_type):
                continue
            resolved = dict(database)
            resolved["server_id"] = server.get("server_id")
            resolved["company_code"] = server.get("company_code")
            resolved["ip"] = server.get("ip")
            return resolved
    return None


def find_database_credential(target: SqlTarget, credential_groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The credential this target runs as, or ``None`` — the caller reports the failed target.

    Selection is the shared :func:`db_ops.lib.data_sources.find_database_credential`; a task
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


def credential_problem(target: SqlTarget, credential_groups: list[dict[str, Any]]) -> str:
    """Why :func:`find_database_credential` found nothing, in the shared lookup's words ("" if it did)."""
    try:
        data_sources.find_database_credential(
            credential_groups, server_id=target.server_id, credential_name=target.credential_name,
            db_type=target.db_type, service_name=target.service_name,
            instance_name=target.instance_name)
    except data_sources.CredentialNotFound as exc:
        return str(exc)
    return ""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
