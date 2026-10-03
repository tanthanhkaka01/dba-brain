from __future__ import annotations
from db_ops.lib import errors
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from db_ops.lib.rows import row_value
from db_ops.lib import data_sources
from db_ops.lib.target_flags import is_record_active
from db_ops.lib import field_names
from db_ops.lib import node_role as node_role_rule
from db_ops.lib.listing import active_only, choice_lines, hidden_note
from db_ops.lib.secret_text import SECRET_KEY_ENV_VAR
from db_ops.lib.config import DEFAULT_CONFIG_PATH, load_config
from db_ops.transport import common_cli
from db_ops.lib.common_cli import build_command, common_invocation
from db_ops.lib.data_sources import request_fill
from db_ops.lib import workflow_steps as ws
from db_ops.lib.process_liveness import (  # noqa: F401 - re-exported, see above
    is_pid_alive as _is_pid_alive,
    is_windows_pid_alive as _is_windows_pid_alive,
    is_zombie as _is_zombie,
    process_start_marker,
    stop_process_tree,
)
from db_ops.lib.telegram_command_text import (  # noqa: F401 - re-exported, see above
    command_key_from_message,
    first_command_token,
    normalize_command_text,
    parse_command_message,
    split_with_verbatim_tail,
    render_command_line,
    split_command_tokens,
    strip_bot_username,
)
from db_ops.db.queue_message import queue_message, store_block_from
from db_ops.lib.time_window import MANUAL_ONLY, weekdays_text, window_of
from db_ops.db.job_runs import telegram_log_metadata
from db_ops.db import DbOpsStore
from db_ops.logging_ops import log_event, setup_app_logger
from db_ops.telegram.commands import can_run_command
from db_ops.telegram.sql_commands import execute_sql_support_command
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.timezone import file_stamp
from db_ops.telegram.detached_exit import ARGV_SEPARATOR as DETACHED_ARGV_SEPARATOR
from db_ops.telegram.command_base import (  # noqa: F401 - re-exported: every name kept its address
    CLAIM_STALE_SECONDS,
    DEFAULT_COMMANDS_PATH,
    SupportCommand,
    TelegramCommandError,
    _DISPATCH_LOGGERS,
    _DISPATCH_LOG_SCOPE,
    _describe_source,
    _dispatch_log,
    _dispatch_logger,
    _format_argument_position,
    _parameter_at_position,
    _safe_values_text,
    mask_sensitive_text,
    mask_sensitive_value,
    secret_values,
    sensitive_key,
)
from db_ops.telegram.command_replies import (  # noqa: F401 - re-exported: every name kept its address
    COMMAND_STATUS_DONE,
    COMMAND_STATUS_NOT_FOUND,
    COMMAND_STATUS_SKIPPED,
    UNKNOWN_SUPPORT_COMMAND_HELP,
    _PLACEHOLDER_PATTERN,
    _close_interrupted_command,
    is_unknown_support_command,
    queue_command_reply,
    queue_unknown_command_reply,
    render_reply_text,
    telegram_username,
    validate_target_ip,
)
from db_ops.telegram.command_permissions import (  # noqa: F401 - re-exported: every name kept its address
    ACTION_LEVEL_RECOMMENDED,
    ACTION_TYPES,
    _WARNED_LEVELS,
    _command_runs_on_node,
    _resolve_node_role,
    command_permission,
    load_support_commands,
    permission_denied_reply_text,
    queue_permission_denied_reply,
    telegram_group_allow_command,
    telegram_user_type,
    warn_low_level,
)
from db_ops.telegram.command_cli import (  # noqa: F401 - re-exported: every name kept its address
    CliCommandError,
    FINISHED_FROM_THE_STORE,
    _ERROR_RE,
    _JSON_SCALAR,
    _PLACEHOLDER,
    _PROGRESS_RE,
    _PULL_RE,
    _UNMASKED_CREDENTIAL_RE,
    _default_worker_host,
    _extract_error_from_output,
    _extract_flag_words,
    _inject_resolved_target,
    _is_normal_cli_log_line,
    _is_progress_noise,
    _meaningful_cli_error_lines,
    _run_finished_common_command,
    build_cli_argv,
    cli_action_values,
    command_env,
    create_sql_run_result_file,
    extract_result_column_text,
    finish_common_request,
    finishes,
    is_relative_to,
    parse_cli_result,
    parse_json_from_output,
    render_json_template,
    render_template,
    resolve_result_output_dir,
    resolve_working_dir,
    run_configured_cli_command,
    safe_error_summary,
    safe_output_file_name,
    safe_output_path_component,
    sanitized_cli_result,
    with_last_runs,
)
from db_ops.telegram.command_conversation import (  # noqa: F401 - re-exported: every name kept its address
    SKIPPED_LAST_STEP,
    PROMPT_CHOICES_TIMEOUT_SECONDS,
    _answered_parameters,
    _chain_next_conversation_parameter,
    _fill_placeholders,
    _reask_with_note,
    _target_has_no_database,
    answer_kind_for,
    answer_rejection,
    apply_conversation_control,
    first_missing_prompt_parameter,
    forget_secret_answer,
    load_json_object,
    masked_answer,
    normalise_answer,
    prompt_choice_text,
    prompt_condition_holds,
    queue_missing_parameter_prompt,
    queue_step_prompt,
    queue_workflow_closing_message,
    record_inline_answers,
    render_prompt_text,
    skip_parameter_value,
    sql_task_parameter_names,
    sql_tasks_listing,
    state_json_dict,
    workflow_history,
    workflow_run_key,
)
from db_ops.telegram.command_background import (  # noqa: F401 - re-exported: every name kept its address
    _exit_code_path,
    _format_dispatch_value,
    _is_our_process,
    _job_run_metadata_matches,
    _probe_completion,
    _read_exit_code_file,
    _read_file_safe,
    _remove_file_safe,
    _render_completion_probe,
    _stop_task_tree,
    _tail_text,
    check_cli_background_tasks,
    completion_verdict,
    execute_cli_background_command,
)
from db_ops.telegram.command_listing import (  # noqa: F401 - re-exported: every name kept its address
    TELEGRAM_LISTING_TEXT_LIMIT,
    _command_parameter_summary,
    _format_time_window_line,
    _queue_listing_document,
    execute_list_all_command_command,
    execute_list_metrics_command,
    execute_list_server_id_command,
    execute_list_sql_tasks_command,
    execute_metric_toggle_command,
    menu_order_of,
)
from db_ops.telegram.command_sql import (  # noqa: F401 - re-exported: every name kept its address
    MANUAL_SCHEDULE_WORD,
    _ADD_SQL_NONE_TOKENS,
    _add_sql_optional,
    _download_document_base64,
    _download_document_text,
    _max_file_bytes,
    _message_document,
    execute_add_sql_task_command,
    execute_create_table_from_xlsx_command,
    execute_sql_to_xlsx_command,
    parse_add_sql_time_window,
)


def process_pending_command_messages(
    *,
    sqlite_path: str | Path,
    commands_path: str | Path = DEFAULT_COMMANDS_PATH,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    limit: int = 50,
) -> dict[str, int]:
    store = DbOpsStore(sqlite_path)
    rows = store.fetch_pending_telegram_command_messages(limit=limit)

    counts = {
        "read": len(rows),
        "processed": 0,
        "queued_reply": 0,
        "skipped": 0,
        "not_found": 0,
        "already_claimed": 0,
    }

    now = datetime.now(timezone.utc)
    claimed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_before = (now - timedelta(seconds=CLAIM_STALE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for row in rows:
        # Read before the claim overwrites it: a row that already carries a claim was started
        # once and never finished.
        interrupted = bool(row_value(row, "claimed_at"))
        # A message is only marked done once its action finishes. The workflow runs every
        # second, so without an exclusive claim the next cycle re-reads the same pending row
        # and dispatches the command a second time (observed: five "started" replies for one
        # /spbot_report_hourly_metrics, and the collect running repeatedly).
        if not store.claim_telegram_command_message(
            telegram_command_message_id=int(row["telegram_command_message_id"]),
            claimed_at=claimed_at,
            stale_before=stale_before,
        ):
            counts["already_claimed"] += 1
            continue
        if interrupted:
            _close_interrupted_command(store, row)
            counts["skipped"] += 1
            counts["queued_reply"] += 1
            continue

        result = process_one_command_message(
            sqlite_path=sqlite_path,
            telegram_command_message_id=int(row["telegram_command_message_id"]),
            commands_path=commands_path,
            config_path=config_path,
        )
        counts["processed"] += int(result["processed"])
        counts["queued_reply"] += int(result["queued_reply"])
        counts["skipped"] += int(result["skipped"])
        counts["not_found"] += int(result["not_found"])

    return counts


def process_pending_conversation_messages(
    *,
    sqlite_path: str | Path,
    commands_path: str | Path = DEFAULT_COMMANDS_PATH,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    limit: int = 50,
    delete_message: Any = None,
) -> dict[str, int]:
    """``delete_message(chat_id, message_id)`` removes a secret answer from the chat (F8.4)."""
    store = DbOpsStore(sqlite_path)
    commands = load_support_commands(commands_path)
    commands_by_id = {command.command_id: command for command in commands}
    states = store.fetch_waiting_telegram_conversation_states(limit=limit)
    counts = {
        "read": len(states),
        "waiting": 0,
        "processed": 0,
        "queued_reply": 0,
        "failed": 0,
        "already_claimed": 0,
    }

    now = datetime.now(timezone.utc)
    claimed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_before = (now - timedelta(seconds=CLAIM_STALE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for state in states:
        message = store.fetch_next_telegram_message_for_state(
            chat_id=str(state["chat_id"]),
            user_id=str(state["user_id"]),
            after_message_id=int(state["wait_after_message_id"]),
        )
        if message is None:
            # Still waiting for the user. Claiming here would lock the state for the whole
            # stale window and the reply, when it comes, would sit unprocessed.
            counts["waiting"] += 1
            continue

        # Same exclusivity as pending command messages: a state stays 'waiting' while its
        # action runs, so an overlapping workflow cycle would act on the same user reply twice.
        if not store.claim_telegram_conversation_state(
            state_id=int(state["state_id"]), claimed_at=claimed_at, stale_before=stale_before,
        ):
            counts["already_claimed"] += 1
            continue
        command = commands_by_id.get(int(state["command_id"]))
        if command is None:
            store.update_telegram_conversation_state(
                state_id=int(state["state_id"]),
                status="error",
                state_data=state_json_dict(state),
                consumed_telegram_message_id=int(message["telegram_message_id"]),
                note=f"Command not found: {state['command_id']}",
            )
            counts["failed"] += 1
            continue

        state_data = state_json_dict(state)
        args = list(state_data.get("args") or [])
        parameter_position = int(state_data.get("parameter_position") or 1)
        while len(args) < parameter_position:
            args.append("")
        value = str(message["text"] or "").strip()

        # File-attachment support: if the awaited parameter accepts a file (e.g. the
        # add_sql_task SQL body) and the message carries a document instead of text,
        # download the file and use its contents as the value. Failures reply to the
        # user and mark the state as errored rather than crashing the processor.
        awaited = _parameter_at_position(command, parameter_position)
        from_file = False
        if awaited is not None and awaited.get("accept_file") and not value:
            document = _message_document(message)
            if document is not None:
                try:
                    # A .sql body is text; a .xlsx is a zip, and decoding it as utf-8 either
                    # raises or silently mangles it. `file_encoding: "base64"` says the awaited
                    # parameter wants the bytes, carried the way a JSON request can carry them.
                    # A per-parameter cap (review 0.25.0, F8.3): the file is read whole into memory
                    # and, as base64, grows by a third inside a JSON request. Absent, Telegram's own
                    # bot limit is the only bound, as before.
                    max_bytes = _max_file_bytes(awaited)
                    if str(awaited.get("file_encoding") or "").lower() == "base64":
                        value = _download_document_base64(document, config_path=config_path,
                                                          max_bytes=max_bytes)
                    else:
                        value = _download_document_text(document, config_path=config_path,
                                                        max_bytes=max_bytes)
                    from_file = True
                except Exception as exc:  # noqa: BLE001 - report and fail the state.
                    queue_message({
                        "store": store_block_from(store),
                        "message_type": "failed",
                        "chat_id": str(state["chat_id"]),
                        "text": f"Could not read the attached file: {safe_error_summary(exc)}",
                        "reply_message_id": int(message["message_id"]) if message["message_id"] is not None else None,
                        "note": f"File download failed for {command.command_text}",
                        "source_type": "telegram_conversation_states",
                        "source_id": str(state["state_id"]),
                        "metadata": {"command_id": command.command_id, "command_text": command.command_text},
                    }, fallback_store=store)
                    store.update_telegram_conversation_state(
                        state_id=int(state["state_id"]), status="error", state_data=state_data,
                        consumed_telegram_message_id=int(message["telegram_message_id"]),
                        note="file download failed")
                    counts["queued_reply"] += 1
                    counts["failed"] += 1
                    continue

        awaited_step = dict(awaited or {})
        run_key = workflow_run_key(
            state["source_telegram_command_message_id"], state["state_id"])

        # Back / Skip / Cancel are read before the answer is: they are actions on the workflow,
        # not values for this step. A step that legitimately offers one of those words as an
        # answer keeps it (resolve_control consults the step's own options).
        control = None if from_file else ws.resolve_control(value, awaited_step)
        if control is not None:
            outcome = apply_conversation_control(
                store=store, state=state, message=message, command=command, args=args,
                state_data=state_data, step=awaited_step, control=control, run_key=run_key,
            )
            if outcome != SKIPPED_LAST_STEP:
                counts["processed"] += 1
                counts["queued_reply"] += 1
                continue
            # Skip on the last step: the args hold its skip value, and the run goes on below to
            # be executed like any finished workflow.
        else:
            if not from_file and ws.is_secret(awaited_step):
                # A password typed to the bot: kept in this run's memory and arguments only. The stored
                # message rows get `***`, and the message itself is deleted from the chat, where every
                # member could read it (review 0.25.0, F8.4).
                forget_secret_answer(store, message, delete_message=delete_message)

            # Validate now, not at execution. A value mistyped at step 2 of a 14-step workflow used
            # to be reported after step 14, by which point going back to fix it was impossible.
            rejection = None if from_file else answer_rejection(awaited_step, value)
            if rejection is not None:
                store.finish_telegram_workflow_step(
                    run_key=run_key, status="rejected",
                    answer_text=masked_answer(awaited_step, value), answer_kind="text",
                    answer_telegram_message_id=int(message["telegram_message_id"]),
                )
                queue_message({
                    "store": store_block_from(store),
                    "message_type": "failed",
                    "chat_id": str(state["chat_id"]),
                    "text": rejection,
                    "reply_message_id": int(message["message_id"]) if message["message_id"] is not None else None,
                    "note": f"Rejected answer for {command.command_text}",
                    "source_type": "telegram_conversation_states",
                    "source_id": str(state["state_id"]),
                    "metadata": {"command_id": command.command_id,
                                 "command_text": command.command_text},
                }, fallback_store=store)
                _chain_next_conversation_parameter(
                    store=store, state=state, message=message, command=command, args=args,
                    next_missing=awaited_step, state_data=state_data,
                )
                counts["processed"] += 1
                counts["queued_reply"] += 1
                continue

            stored_value = normalise_answer(awaited_step, value) if awaited_step and not from_file else value
            args[parameter_position - 1] = stored_value
            if from_file:
                # The argument is the file's content (base64 for a workbook). Its name is kept beside
                # it, so a command listing can say which file was sent instead of printing it.
                arg_files = dict(state_data.get("arg_files") or {})
                arg_files[str(parameter_position)] = str(document.get("file_name") or "file")
                state_data["arg_files"] = arg_files
            store.finish_telegram_workflow_step(
                run_key=run_key, status="answered",
                answer_text=masked_answer(awaited_step, stored_value),
                answer_kind="file" if from_file else answer_kind_for(awaited_step, value),
                answer_telegram_message_id=int(message["telegram_message_id"]),
            )

        next_missing = first_missing_prompt_parameter(command, args)
        if next_missing is not None:
            _chain_next_conversation_parameter(
                store=store,
                state=state,
                message=message,
                command=command,
                args=args,
                next_missing=next_missing,
                state_data=state_data,
            )
            counts["processed"] += 1
            counts["queued_reply"] += 1
            continue

        action_result: dict[str, Any] | None = None
        action_error: str | None = None
        try:
            action_result = execute_command_action(
                store=store,
                row=message,
                command=command,
                args=args,
                sqlite_path=sqlite_path,
                config_path=config_path,
                source_id=str(state["source_telegram_command_message_id"] or state["state_id"]),
            )
        except Exception as exc:  # noqa: BLE001 - reply to user and mark state failed.
            action_error = safe_error_summary(exc)

        reply_text = render_reply_text(
            command.reply_text,
            row={"telegram_command_message_id": state["source_telegram_command_message_id"] or ""},
            command=command,
            args=args,
            action_result=action_result,
            action_error=action_error,
        )
        if reply_text:
            queue_message({
                "store": store_block_from(store),
                "chat_id": str(state["chat_id"]),
                "text": reply_text,
                "reply_message_id": int(message["message_id"]) if message["message_id"] is not None else None,
                # action_error is the command's verdict: set means the action raised.
                "message_type": "failed" if action_error else "success",
                "note": f"Reply for conversation command {command.command_text}",
                "source_type": "telegram_conversation_states",
                "source_id": str(state["state_id"]),
                "metadata": {
                    "command_id": command.command_id,
                    "command_text": command.command_text,
                    "action_type": command.action_type,
                    "action_result": action_result,
                    "action_error": action_error,
                    # The run is over: take the step keyboard away with its last message, or the
                    # operator is left holding Back and Cancel for a workflow that has ended.
                    "reply_markup": ws.hide_keyboard(),
                },
            }, fallback_store=store)
            counts["queued_reply"] += 1

        state_data["args"] = args
        state_data["action_result"] = action_result
        state_data["action_error"] = action_error
        store.update_telegram_conversation_state(
            state_id=int(state["state_id"]),
            status="error" if action_error else "done",
            state_data=state_data,
            consumed_telegram_message_id=int(message["telegram_message_id"]),
            note=action_error or "processed",
        )
        if action_error:
            counts["failed"] += 1
        else:
            counts["processed"] += 1

    return counts


def find_support_command_by_key(
    command_key: str,
    commands: list[SupportCommand],
) -> SupportCommand | None:
    normalized_key = normalize_command_text(command_key)

    matched: SupportCommand | None = None
    matched_len = -1

    for command in commands:
        command_text = normalize_command_text(command.command_text)

        if normalized_key == command_text or normalized_key.startswith(f"{command_text}_"):
            if len(command_text) > matched_len:
                matched = command
                matched_len = len(command_text)

    return matched

def consume_rest_position(command: SupportCommand) -> int:
    """The position of this command's ``consume_rest`` parameter, or 0 when it has none."""
    for parameter in (command.action_config or {}).get("parameters") or []:
        if bool(parameter.get("consume_rest")):
            return int(parameter.get("position", 0) or 0)
    return 0


def command_args_from_text(text: str, command: SupportCommand, args: list[str]) -> list[str]:
    """The arguments of an inline command, with a ``consume_rest`` tail kept verbatim.

    Only commands that declare such a parameter are re-read; for every other command the shlex
    tokens are already right and re-parsing could only introduce a difference.
    """
    position = consume_rest_position(command)
    if position < 1:
        return args
    return split_with_verbatim_tail(text, position)


def process_one_command_message(
    *,
    sqlite_path: str | Path,
    telegram_command_message_id: int,
    commands_path: str | Path = DEFAULT_COMMANDS_PATH,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> dict[str, int | str]:
    store = DbOpsStore(sqlite_path)
    row = store.fetch_telegram_command_message(telegram_command_message_id=telegram_command_message_id)
    if row is None:
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": 0,
            "skipped": 0,
            "not_found": 1,
            "status": "row_not_found",
        }
    if int(row["command_status"]) != 0:
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": 0,
            "skipped": 1,
            "not_found": 0,
            "status": "not_pending",
        }

    commands = load_support_commands(commands_path)
    parsed_message = parse_command_message(str(row["text"] or ""))
    command_key = parsed_message["command_key"] or command_key_from_message(
        str(row["command_prefix"] or ""),
        str(row["command_payload"] or ""),
    )
    command = find_support_command_by_key(command_key, commands)
    if command is not None:
        parsed_message["args"] = command_args_from_text(
            str(row["text"] or ""), command, parsed_message["args"])

    if command is None:
        queued_reply = 0
        queued_reply_id = None
        if is_unknown_support_command(command_key):
            queued_reply_id = queue_unknown_command_reply(
                store=store,
                row=row,
                command_key=command_key,
            )
            queued_reply = 1
        store.update_telegram_command_message_status(
            telegram_command_message_id=telegram_command_message_id,
            command_status=COMMAND_STATUS_NOT_FOUND,
            process_note=(
                f"Command not found: {command_key}"
                + (f"; queued_unknown_command_reply={queued_reply_id}" if queued_reply_id is not None else "")
            ),
        )
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": queued_reply,
            "skipped": 0,
            "not_found": 1,
            "status": "command_not_found",
        }

    node_role = _resolve_node_role(config_path)
    if not _command_runs_on_node(command.node_role, node_role):
        store.update_telegram_command_message_status(
            telegram_command_message_id=telegram_command_message_id,
            command_status=COMMAND_STATUS_SKIPPED,
            process_note=(
                f"Command {command.command_text} runs on node_role={command.node_role}; "
                f"this node is {node_role}. Left for the other node."
            ),
        )
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": 0,
            "skipped": 1,
            "not_found": 0,
            "status": "skipped_wrong_node_role",
        }

    # Only a negative command_type disables a command. command_type=0 is the public tier
    # (runs for everyone) and must fall through to the permission check, not be treated as off.
    if command.command_type < 0:
        store.update_telegram_command_message_status(
            telegram_command_message_id=telegram_command_message_id,
            command_status=COMMAND_STATUS_SKIPPED,
            process_note=f"Command disabled: {command.command_text}",
        )
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": 0,
            "skipped": 1,
            "not_found": 0,
            "status": "command_disabled",
        }

    permission = command_permission(
        row=row,
        command=command,
        data_dir=Path(commands_path).resolve().parent,
    )
    if not permission["allowed"]:
        queued_reply_id = queue_permission_denied_reply(
            store=store,
            row=row,
            command=command,
            permission=permission,
        )
        store.update_telegram_command_message_status(
            telegram_command_message_id=telegram_command_message_id,
            command_status=COMMAND_STATUS_SKIPPED,
            command_id=command.command_id,
            process_note=f"{permission['reason']}; queued_permission_denied_reply={queued_reply_id}",
        )
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": 1,
            "skipped": 1,
            "not_found": 0,
            "status": "permission_denied",
        }

    queued_reply_id = None
    queued_reply = 0
    action_result: dict[str, Any] | None = None
    action_error: str | None = None
    if command.action_type in ACTION_TYPES:
        missing_parameter = first_missing_prompt_parameter(command, parsed_message["args"])
        if missing_parameter is not None:
            return queue_missing_parameter_prompt(
                store=store,
                row=row,
                command=command,
                missing_parameter=missing_parameter,
                args=parsed_message["args"],
            )
        # Nothing left to ask: every answer came in the one message. Recorded before the action
        # runs, so a command that fails still shows what it was asked to do.
        record_inline_answers(store=store, row=row, command=command, args=parsed_message["args"])
        try:
            action_result = execute_command_action(
                store=store,
                row=row,
                command=command,
                args=parsed_message["args"],
                sqlite_path=sqlite_path,
                config_path=config_path,
                source_id=str(row["telegram_command_message_id"]),
            )
        except Exception as exc:  # noqa: BLE001 - command failure should be reported back to Telegram.
            action_error = safe_error_summary(exc)
        if action_result is not None and int(action_result.get("_queued_reply_count", 0) or 0) > 0:
            queued_reply += int(action_result.get("_queued_reply_count", 0) or 0)

    if command.reply_default == 1 and command.reply_text:
        reply_text = render_reply_text(
            command.reply_text,
            row=row,
            command=command,
            args=parsed_message["args"],
            action_result=action_result,
            action_error=action_error,
        )
        queued_reply_id = queue_message({
            "store": store_block_from(store),
            "chat_id": str(row["chat_id"]),
            "text": reply_text,
            "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
            "message_type": "failed" if action_error else "success",
            "note": f"Reply for command {command.command_text}",
            "source_type": "telegram_command_messages",
            "source_id": str(row["telegram_command_message_id"]),
            "metadata": {
                "command_id": command.command_id,
                "command_text": command.command_text,
                "action_type": command.action_type,
                "action_result": action_result,
                "action_error": action_error,
            },
        }, fallback_store=store)
        queued_reply = 1

    process_note = f"Command matched: {command.command_text}"
    if action_result is not None:
        process_note = f"{process_note}; action={command.action_type}; row_count={action_result.get('row_count')}"
    if action_error is not None:
        process_note = f"{process_note}; action_error={action_error}"
    if queued_reply_id is not None:
        process_note = f"{process_note}; queued_reply={queued_reply_id}"

    store.update_telegram_command_message_status(
        telegram_command_message_id=telegram_command_message_id,
        command_status=COMMAND_STATUS_SKIPPED if action_error else COMMAND_STATUS_DONE,
        command_id=command.command_id,
        process_note=process_note,
    )
    return {
        "telegram_command_message_id": telegram_command_message_id,
        "processed": 0 if action_error else 1,
        "queued_reply": queued_reply,
        "skipped": 1 if action_error else 0,
        "not_found": 0,
        "status": "action_failed" if action_error else "processed",
    }


def execute_command_action(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    args: list[str],
    sqlite_path: str | Path,
    config_path: str | Path,
    source_id: str,
) -> dict[str, Any]:
    if command.action_type == "sql_execute":
        return execute_sql_support_command(command=command, args=args)
    if command.action_type == "cli_execute":
        return execute_configured_cli_command(
            store=store,
            row=row,
            command=command,
            args=args,
            config_path=config_path,
            source_id=source_id,
        )
    if command.action_type == "add_sql_task":
        return execute_add_sql_task_command(command=command, args=args)
    if command.action_type == "sql_to_xlsx":
        return execute_sql_to_xlsx_command(
            store=store, row=row, command=command, args=args, source_id=source_id
        )
    if command.action_type == "list_server_id":
        return execute_list_server_id_command()
    if command.action_type == "list_all_command":
        return execute_list_all_command_command(
            store=store, row=row, command=command, source_id=source_id
        )
    if command.action_type == "list_sql_tasks":
        return execute_list_sql_tasks_command(store=store, row=row, command=command, source_id=source_id)
    if command.action_type == "list_metrics":
        return execute_list_metrics_command(store=store, row=row, command=command, source_id=source_id)
    if command.action_type == "metric_toggle":
        return execute_metric_toggle_command(command=command, args=args)
    if command.action_type == "create_table_from_xlsx":
        return execute_create_table_from_xlsx_command(command=command, args=args)
    return {}


def execute_configured_cli_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    args: list[str],
    config_path: str | Path,
    source_id: str,
) -> dict[str, Any]:
    try:
        values = cli_action_values(
            command=command, args=args, config_path=config_path,
            chat_id=str(row["chat_id"]) if row["chat_id"] is not None else None,
            user_id=str(row["user_id"]) if row["user_id"] is not None else None,
        )
    except Exception as exc:
        error_summary = safe_error_summary(exc)
        queue_command_reply(
            store=store,
            row=row,
            command=command,
            message_text=error_summary,
            source_id=source_id,
            status="validation_error",
        )
        raise
    config = dict(command.action_config or {})
    if bool(config.get("requires_secret_key")) and not os.environ.get(SECRET_KEY_ENV_VAR, "").strip():
        message = (
            f"Required secret key is not available ({SECRET_KEY_ENV_VAR}). "
            "Start the worker with --key/--key-base64 or set the secret key env."
        )
        queue_command_reply(
            store=store,
            row=row,
            command=command,
            message_text=message,
            source_id=source_id,
            status="validation_error",
        )
        raise TelegramCommandError(message, exit_code=2)
    if bool(config.get("background") or config.get("detached")):
        return execute_cli_background_command(
            store=store,
            row=row,
            command=command,
            values=values,
            source_id=source_id,
        )
    target_ip = str(values.get("target_ip") or "")
    start_time = datetime.now(timezone.utc)
    start_text = str((command.action_config or {}).get("start_text") or "Command started.")
    queue_command_reply(
        store=store,
        row=row,
        command=command,
        message_text=render_template(start_text, values),
        source_id=source_id,
        status="started",
    )
    file_result_config = dict((command.action_config or {}).get("result_file") or {})
    file_result: dict[str, Any] | None = None
    try:
        result = run_configured_cli_command(command=command, values=values)
        success_values = values | result
        if file_result_config:
            file_result = create_sql_run_result_file(
                store=store,
                config=file_result_config,
                values=success_values,
            )
            result.update(file_result)
    except Exception as exc:
        exit_code = int(getattr(exc, "exit_code", 1))
        # This path still holds the raw values, so the run's own secrets are scrubbed
        # literally — not just the shapes a pattern anticipates.
        error_summary = safe_error_summary(exc, secrets=secret_values(values))
        end_time = datetime.now(timezone.utc)
        failure_values = values | {"exit_code": exit_code, "error_summary": error_summary}
        failure_text = str(
            (command.action_config or {}).get("failure_text")
            or "Command failed.\nExit code: {exit_code}\nError: {error_summary}"
        )
        queue_command_reply(
            store=store,
            row=row,
            command=command,
            message_text=render_template(failure_text, failure_values),
            source_id=source_id,
            status="failed",
            metadata=telegram_log_metadata(
                telegram_user_id=str(row["user_id"] or ""),
                telegram_username=telegram_username(row),
                telegram_command=command.command_text,
                target_ip=target_ip,
                target_id=str(values.get("target_id") or ""),
                start_time=start_time,
                end_time=end_time,
                status="failed",
                error_summary=error_summary,
            )
            | {"cli_result": getattr(exc, "result", {})},
        )
        raise TelegramCommandError(error_summary, exit_code=exit_code) from exc

    end_time = datetime.now(timezone.utc)
    success_values = values | result
    success_text = str((command.action_config or {}).get("success_text") or "Command completed.")
    success_reply_id = queue_command_reply(
        store=store,
        row=row,
        command=command,
        message_text=render_template(success_text, success_values),
        source_id=source_id,
        status="success",
        metadata=telegram_log_metadata(
            telegram_user_id=str(row["user_id"] or ""),
            telegram_username=telegram_username(row),
            telegram_command=command.command_text,
            target_ip=target_ip,
            target_id=str(result.get("target_id") or ""),
            start_time=start_time,
            end_time=end_time,
            status="success",
            error_summary="",
        )
        | {"cli_result": sanitized_cli_result(result)},
    )
    queued_reply_count = 2
    if file_result_config and file_result is not None:
        document_caption = render_template(
            str(file_result_config.get("caption") or "Generated file: {file_name}"),
            success_values | file_result,
        )
        queue_message({
            "store": store_block_from(store),
            "message_type": "plain",
            "chat_id": str(row["chat_id"]),
            "text": document_caption,
            "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
            "note": f"Document for command {command.command_text}",
            "source_type": "telegram_command_messages",
            "source_id": source_id,
            "metadata": {
                "command_id": command.command_id,
                "command_text": command.command_text,
                "action_type": command.action_type,
                "status": "success_document",
                "document_path": file_result["file_path"],
                "success_reply_id": success_reply_id,
                "file_result": file_result,
            },
        }, fallback_store=store)
        queued_reply_count += 1
    result["_queued_reply_count"] = queued_reply_count
    return result
