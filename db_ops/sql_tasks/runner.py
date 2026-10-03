from __future__ import annotations
from db_ops.lib import errors
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
from db_ops.lib.sql_task_catalog import (  # noqa: F401 - re-exported for this module's callers
    _AMBIGUOUS_CREDENTIAL, DEFAULT_INLINE_MAX_ROWS, DEFAULT_SQL_TIMEOUT_SECONDS, INPUT_TYPES,
    SQL_TARGET_NOTIFY_DEFAULTS, XLSX_MAX_ROWS, SqlCommand, SqlTarget, _opt_str, collect_sql_tasks,
    load_default_credential_names, load_input_definition, load_sql_access_by_server,
    load_sql_commands, load_sql_script_definition, load_sql_targets, resolve_sql_folder)
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
from db_ops.lib.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from db_ops.lib import data_sources
from db_ops.transport import common_cli
from db_ops.lib import process_liveness
from db_ops.lib import sql_task_target
from db_ops.lib import node_identity, run_claim
from db_ops.lib.secret_text import add_key_argument, set_key_env
from db_ops.lib.json_io import load_json_file
from db_ops.lib.time_window import MANUAL_ONLY, TimeWindow, due_from_row, is_time_window_open as common_time_window_open, parse_time_window_config, repeat_due, run_anchor
from db_ops.lib.timezone import display_now, file_stamp
from db_ops.db import DbOpsStore
from db_ops.db.store import RunAlreadyClaimed, utc_now_text
from db_ops.logging_ops import log_event, log_function_error, setup_app_logger
from db_ops.logging_ops.runtime_stdout import patch_stdout
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.paths import asset_candidates
from db_ops.sql_tasks.runner_plan import (  # noqa: F401 - re-exported: every name kept its address
    ExecutionStep,
    SqlScanResult,
    _progress_summary,
    build_execution_plan,
    parse_notify_rule,
    workflow_name_from_code,
)
from db_ops.sql_tasks.runner_parameters import (  # noqa: F401 - re-exported: every name kept its address
    POSITIONAL_PARAMETERS_KEY,
    bind_parameter_values,
    legacy_define_values,
    named_parameter_values,
    parse_parameter_arguments,
)
from db_ops.sql_tasks.runner_output import (  # noqa: F401 - re-exported: every name kept its address
    MAX_RESULT_ROWS,
    MAX_STORED_RESULT_SETS,
    MULTI_SET_FILE_FORMATS,
    RESULT_CELL_MAX_LEN,
    RESULT_TABLE_MAX_COLS,
    STORED_RESULT_MAX_ROWS,
    _md_cell,
    all_result_sets,
    enqueue_sql_task_document,
    enqueue_sql_task_message,
    enqueue_sql_task_result_text,
    format_result_sets_markdown,
    log_sql_task_event,
    resolve_output_chat_id,
    trim_result_for_store,
    unexported_result_sets,
    write_sql_task_output,
    write_sql_task_xlsx,
)
from db_ops.sql_tasks.runner_execute import (  # noqa: F401 - re-exported: every name kept its address
    RUN_DEADLINE_FACTOR,
    _SURROGATE_RANGE,
    _deprecation_logger,
    _run_python_source,
    _surrogate_context,
    check_sql_text_is_encodable,
    credential_problem,
    diagnose_connect_failure,
    execute_on_target,
    execute_sql,
    find_database_credential,
    find_database_inventory,
    format_script_file_order,
    log_deprecated_time_window_warnings,
    resolve_sql_file,
    resolve_sql_files,
    run_deadline_seconds,
    scrub_credential,
    sql_run_time,
    target_location,
    task_connection,
)


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

    close_run = subparsers.add_parser(
        "close-run",
        help="Close ONE run left 'running' by a process that no longer exists, releasing its "
             "target. Refuses a row that is not running.",
    )
    close_run.add_argument("--sql-run-id", type=int, required=True,
                           help="The sql_runs row to close.")
    close_run.add_argument("--reason", required=True,
                           help="Why it is being closed - written into the row, so the close is "
                                "not read later as a run that failed on its own.")
    close_run.add_argument("--confirm", default="", metavar="yes",
                           help="Must be `yes`. Closing a row whose process is in fact alive lets "
                                "the next scan start a second copy on top of it. Never prompted.")
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
        if args.command == "close-run":
            answer = close_orphaned_run(store=store, sql_run_id=int(args.sql_run_id),
                                        reason=str(args.reason), confirm=_opt_str(args.confirm))
            print(json.dumps(answer, ensure_ascii=False, indent=1))
            log_event(logger, level="error" if answer["closed"] else "logging",
                      message=(f"sql_tasks.runner.close_run|sql_run_id={args.sql_run_id}"
                               f"|closed={answer['closed']}|reason={answer['reason']}"))
            return 0 if answer["closed"] else 1
        if args.command == "run-sql-id":
            if not args.force:
                raise errors.InvalidRequest("run-sql-id requires --force.")
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


def close_orphaned_run(*, store: DbOpsStore, sql_run_id: int, reason: str, confirm: str) -> dict:
    """Close one ``running`` row by hand - what the stale sweep does, for the row it will not.

    A row whose owner cannot be checked from here waits for its timeout plus an hour of grace, and
    the row is the claim, so its target does not run meanwhile. On 2026-09-30 that was the
    production engine task's first target, and the row was closed with a script calling
    ``update_sql_run`` (0.26.0 §1.69). This is that call, with the same guard the sweep uses:
    ``only_if_status="running"``, so a run that finished in the meantime keeps its own ending.
    """
    if str(confirm or "").strip().lower() != "yes":
        return {"closed": False, "sql_run_id": sql_run_id,
                "reason": "not confirmed: pass --confirm yes"}
    if not str(reason or "").strip():
        return {"closed": False, "sql_run_id": sql_run_id, "reason": "a reason is required"}
    row = next((r for r in store.fetch_running_sql_runs() if int(r["sql_run_id"]) == sql_run_id),
               None)
    if row is None:
        return {"closed": False, "sql_run_id": sql_run_id,
                "reason": "no running row with that id - finished, closed, or never existed"}
    owner_pid, owner_host = run_claim.claim_owner(run_claim.row_metadata(row))
    message = (f"SQL task {row['sql_code']} target {row['target_no']} closed by hand: "
               f"{reason.strip()} (claimed by pid {owner_pid} on {owner_host or 'unknown host'}).")
    closed = store.update_sql_run(
        sql_run_id=sql_run_id, status="error", level="error", message=message,
        finished_at=utc_now_text(), error_text=message,
        metadata={"stale_running": True, "closed_by": "operator", "close_reason": reason.strip()},
        only_if_status="running",
    )
    return {"closed": bool(closed), "sql_run_id": sql_run_id, "sql_code": str(row["sql_code"]),
            "target_no": int(row["target_no"]), "claim_pid": owner_pid, "claim_host": owner_host,
            "reason": message if closed else "the row stopped being running before the close landed"}


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
        except Exception as exc:  # noqa: BLE001 - one task must not stop the scan (F4.1).
            from db_ops.lib import store_outage

            if store_outage.is_transient(exc):
                raise  # the store itself is down: every task after this one would fail the same way
            log_sql_task_event(
                logger, "sql_tasks.runner.task.error", command=command, target=target,
                sql_id=command.sql_id, sql_code=command.sql_code, status="error", level="error",
                error=f"{type(exc).__name__}: {exc}")
            error_count += 1
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
        raise errors.InvalidRequest("run-sql-id requires --force.")
    commands = load_sql_commands(data_dir / "sql_commands.json", on_warning=_deprecation_logger(logger))
    targets = load_sql_targets(data_dir / "sql_targets.json", on_warning=_deprecation_logger(logger))
    command = commands.get(sql_id)
    if command is None:
        raise errors.InvalidRequest(f"SQL command not found for sql_id={sql_id}.")
    # Values typed without a name are bound here and nowhere earlier: this is the first point
    # where the task that declares those names is known.
    parameter_values = bind_parameter_values(command, parameter_values)
    selected_targets = [target for target in targets if target.sql_id == sql_id and target.db_type.lower() == command.db_type.lower()]
    if not selected_targets:
        raise errors.NotConfigured(f"No SQL targets found for sql_id={sql_id}.")

    secrets = data_sources.load_secret_text(data_dir)
    inventory = data_sources.load_inventory(data_dir)
    credentials = data_sources.load_all_credentials(data_dir)
    if not dry_run:
        # Before this task runs: its own runs still `running` past their timeout are closed as
        # errors by timeout (0.26.0 §1.70). The scheduled scan sweeps every task at its start; a
        # run asked for by hand sweeps the one it is about to run.
        mark_stale_running_sql_runs(
            store=store, commands=commands, targets=targets,
            running_runs=[row for row in store.fetch_running_sql_runs()
                          if int(row["sql_id"]) == sql_id],
            telegram_groups=telegram_groups, logger=logger)

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
    """Close out runs left `running` by a process that died - or past their timeout - and ALERT on
    each one.

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
        if command is None and not _left_by_this_node(row, this_host=this_host):
            # Another node's task: the store may be shared, and what this node does not configure
            # may be running there, with a timeout this node cannot know. Its own sweep closes it.
            continue
        started = sql_run_time(row)
        if started is None:
            continue
        target = targets_by_key.get((int(row["sql_id"]), int(row["target_no"])))
        timeout_seconds = target.timeout_seconds if target is not None else DEFAULT_SQL_TIMEOUT_SECONDS
        # **A row past its timeout is over, whoever owns it** (the operator, 2026-10-02, 0.26.0
        # §1.70). Until then a row whose process was alive was left alone however old it was: the
        # `running` row is a claim (ux_sql_runs_claim), closing it releases the key, and a row closed
        # under a process still working started a second copy on top of the first. But nothing else
        # bounded a SQL task - one target stayed `running` for 13 hours on 2026-09-30, and did not
        # run again until the container was stopped. So the timeout is the answer again, and the
        # second copy is prevented the other way: the owner is stopped first (below). Inside its
        # timeout a row is judged as before - a dead pid frees it at once, a live one holds it.
        metadata = run_claim.row_metadata(row)
        owner_pid, owner_host = run_claim.claim_owner(metadata)
        verdict = run_claim.reap_verdict(
            metadata=metadata,
            this_host=this_host,
            elapsed_seconds=(now - started).total_seconds(),
            timeout_seconds=timeout_seconds,
            pid_alive=(process_liveness.is_pid_alive(owner_pid)
                       if owner_pid is not None and owner_host == this_host else None),
            this_node=node_identity.current(),
            at_timeout=True,
        )
        if not verdict.reap:
            continue
        # The two clock readings are the first thing anyone asks for: an alert that says only
        # "stale" leaves the reader to go and find out *when* the run they are being told about
        # died, and a reap can happen hours after the fact — a worker restarted at 06:13 was
        # reported at 13:05 with nothing in the text to tell the two apart.
        stale_minutes = int((now - started).total_seconds() // 60)
        if verdict.timed_out:
            # Before the row is closed, so the claim is never free while its owner still works.
            owner = stop_overdue_owner(owner_pid, owner_host, this_host=this_host,
                                       started=run_claim.claim_started(metadata))
            message = (f"SQL task {row['sql_code']} error by timeout: {verdict.reason}. It started "
                       f"at {format_message_time(started)} with timeout_seconds={timeout_seconds} "
                       f"and was still 'running' {stale_minutes} minutes later; {owner}.")
        else:
            message = (f"SQL task {row['sql_code']} stale running: {verdict.reason}. It started at "
                       f"{format_message_time(started)} with timeout_seconds={timeout_seconds} and was "
                       f"still 'running' {stale_minutes} minutes later.")
        if command is None:
            message += (" The task is no longer in sql_commands.json, so its timeout is the default"
                        f" ({DEFAULT_SQL_TIMEOUT_SECONDS}s) and nothing would ever have scanned it.")
        # Closed as a claim: ten scans run at once and each reads the same `running` row, so only
        # the one whose close lands may report it - the others were each sending the same alert
        # (three dead runs reported twice on 2026-09-26).
        closed = store.update_sql_run(
            sql_run_id=int(row["sql_run_id"]),
            status="error",
            level="error",
            message=message,
            finished_at=utc_now_text(),
            error_text=message,
            metadata={"stale_running": True, **({"timed_out": True} if verdict.timed_out else {})},
            only_if_status="running",
        )
        if not closed:
            continue
        log_function_error(
            logger, error_text=message,
            function_name="sql_tasks.timed_out" if verdict.timed_out else "sql_tasks.stale_running")
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
                message=(f"{message} The SQL it started may still be executing on "
                         f"{target_location(target)} - check for an orphaned session before the "
                         f"next cycle." if verdict.timed_out else
                         f"{message} The run process is gone, but the SQL it started may still be "
                         f"executing on {target_location(target)} - check for an "
                         f"orphaned session before the next cycle."),
                sql_run_id=int(row["sql_run_id"]),
                # There are no rows to show: the process died before it reported any.
                include_result_table=False,
            )


def _left_by_this_node(row: Any, *, this_host: str) -> bool:
    """Did this node start the run - by its identity, or (a row without one) by its host name?

    Only such a row of a task this node no longer configures is swept here. Before 2026-10-03 none
    was: a task removed from ``sql_commands.json`` is never scanned, so a run it left ``running``
    stayed open for ever - three from September on the 0.26 soak node's store, closed by hand
    (1.86). Its own pid and its timeout still decide, through the same verdict as any other row.
    """
    metadata = run_claim.row_metadata(row)
    node = run_claim.claim_node(metadata)
    if node:
        return node == node_identity.current()
    _pid, host = run_claim.claim_owner(metadata)
    return bool(host) and host == this_host


def stop_overdue_owner(owner_pid: int | None, owner_host: str, *, this_host: str, started: str) -> str:
    """Stop the process behind a run that is past its timeout, and say what was done - one clause.

    The row is about to be closed, which frees its claim; a process still working under it would be
    joined by the next run of the same task. Only a process on this host can be stopped, and only
    one that still carries the start time its claim recorded - a pid alone is whoever holds the
    number now (``process_liveness.stop_process_and_children``). A row claimed before 0.26.0 has no
    start time: its process is left, and the clause says so.
    """
    if owner_pid is None:
        return "no owner was recorded on it"
    if owner_host != this_host:
        return (f"its process (pid {owner_pid}) is on {owner_host or 'an unnamed host'} and cannot "
                f"be stopped from {this_host}")
    if not process_liveness.is_pid_alive(owner_pid):
        return f"its process (pid {owner_pid}) had already ended"
    if process_liveness.stop_process_and_children(owner_pid, started=started or None):
        return f"its process (pid {owner_pid}) was stopped"
    if not started:
        return (f"its process (pid {owner_pid}) was left running - the claim records no start time "
                "to tell it from another process holding that number")
    return (f"pid {owner_pid} is held by another process now, or would not stop; nothing else was "
            "touched")


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
    # Resolved here, before the run row exists, used to be outside every handler: one task whose
    # SQL file had been removed raised out of the scan, and every task after it in the scan was
    # skipped - on every scan (review 0.25.0, F4.1). A missing file is now this task's failure,
    # recorded and alerted like any other, below.
    sql_paths: list[Path] = []
    resolve_error: Exception | None = None
    try:
        sql_paths = resolve_sql_files(command.script_files, data_dir=data_dir)
    except FileNotFoundError as exc:
        resolve_error = exc
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
    if command.script_type == "folder" and resolve_error is None:
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
        if resolve_error is not None:
            raise resolve_error
        if database is None:
            # Only what connecting needs is checked here - the instance. Whether the database exists
            # is the server's to say, after connecting (see diagnose_connect_failure).
            raise errors.NotConfigured(sql_task_target.instance_not_found_message(
                server_id=target.server_id, db_type=target.db_type,
                instance_name=target.instance_name,
                # Each record under its server's id: the loader copies it onto the record, but the
                # server is what it belongs to, and a record without it read as "no such server".
                records=[{**record, "server_id": server.get("server_id")}
                         for server in inventory for record in server.get("databases") or []]))
        if target.credential_name == _AMBIGUOUS_CREDENTIAL:
            raise errors.InvalidConfig(
                f"{target.server_id} runs more than one {target.db_type} instance, so server_id "
                "alone does not say which one to run against. Set instance_name (and "
                "credential_name) on this sql_targets entry."
            )
        if credential is None:
            # The lookup's own reason, not just the name: it says where the credential is when the
            # target does not match its group, which "Credential not found: <name>" hid (1.10).
            raise errors.NotConfigured(credential_problem(target, credentials)
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
                raise errors.OperationFailed(f"{explained} ({exc})") from exc
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
