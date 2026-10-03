"""The Telegram support commands: the listing actions - servers, commands, SQL tasks, metrics - and the metric toggle.

Split out of ``telegram/command_processor.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``command_processor`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
import json
from typing import Any
from db_ops.lib import data_sources
from db_ops.lib import field_names
from db_ops.lib.listing import active_only, hidden_note
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
from db_ops.db import DbOpsStore
from db_ops.telegram.commands import can_run_command
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.timezone import file_stamp
from db_ops.telegram.command_base import SupportCommand, TelegramCommandError
from db_ops.telegram.command_permissions import telegram_group_allow_command, telegram_user_type
from db_ops.telegram.command_cli import is_relative_to, resolve_result_output_dir, safe_output_file_name
from db_ops.telegram.command_conversation import load_json_object, sql_tasks_listing


def execute_list_server_id_command() -> dict[str, Any]:
    """Build the server-target listing for /spbot_list_server_id (reply via {result_listing})."""
    from db_ops.lib import data_sources as target_resolve

    targets = target_resolve.list_target_instances()
    return {
        "listing": target_resolve.format_target_list(),
        "target_count": len(targets),
    }


# A Telegram message caps at 4096 chars; listings longer than this are sent as a JSON document.
TELEGRAM_LISTING_TEXT_LIMIT = 3500


def _command_parameter_summary(config: dict[str, Any]) -> str:
    """``<target> <format> <sql_text...>`` — the arguments, in the order they are typed.

    Derived from the command's own ``parameters`` block, so a command that gains an argument
    describes itself correctly the moment its config changes. Optional arguments are bracketed,
    and a ``consume_rest`` one gets an ellipsis: "the rest of the message goes here" is the single
    thing operators most often get wrong, and it is the reason `/spbot_sql_export` had to put its
    format argument before the SQL rather than after it.
    """
    parameters = sorted(
        (item for item in (config.get("parameters") or []) if isinstance(item, dict)),
        key=lambda item: int(item.get("position") or 0),
    )
    parts: list[str] = []
    for parameter in parameters:
        name = str(parameter.get("name") or "").strip() or "arg"
        if parameter.get("consume_rest"):
            name += "..."
        parts.append(f"<{name}>" if parameter.get("required") else f"[{name}]")
    return " ".join(parts)


def menu_order_of(entry: Any) -> float:
    """Where a command sits in the listing, as a number the config carries.

    It is a **float** so a command can be inserted between two that already exist - 2.5 between 2
    and 3 - without renumbering the file and re-reading every diff to see whether anything else
    moved. An entry with no `menu_order` sorts to the end rather than to the front: a command
    somebody forgot to place should be visible, not first.
    """
    try:
        # sort_order is the standard name (0.22.0 section 1.0); menu_order is what the file says
        # until every node reads both.
        return float(field_names.read(entry, "telegram_support_command", "sort_order"))
    except (TypeError, ValueError):
        return float("inf")


def execute_list_all_command_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    source_id: str,
) -> dict[str, Any]:
    """Every command this bot answers, built from ``telegram_support_commands.json`` itself.

    Nothing in the reply is written by hand. A listing that must be edited when a command is added
    is a listing that is wrong the first time somebody forgets — which has already happened twice
    to the Markdown doc (``tests/test_listing.py`` says so in its own docstring), and that doc at
    least has a test guarding it. So this reads the same config the dispatcher reads and describes
    each command from its own entry: arguments from its ``parameters``, clearance from its
    ``command_type``, where it may be typed from ``is_private`` / ``is_group``.

    **Only what the caller can actually run is listed.** Offering a command the permission check
    will refuse is the failure :mod:`db_ops.lib.listing` exists to prevent — it invites someone
    to type something that cannot work — so commands above the caller's clearance, and commands
    that do not run in this kind of chat, are dropped and counted rather than silently omitted.
    """
    from db_ops.lib import listing as listing_lib

    data_dir = TOOL_ROOT / "data"
    entries = sorted(
        (entry for entry in load_json_object(data_dir / "telegram_support_commands.json",
                                             "telegram_support_commands")
         if isinstance(entry, dict)),
        key=menu_order_of,
    )

    keys = row.keys() if hasattr(row, "keys") else {}
    chat_type = str((row["chat_type"] if "chat_type" in keys else "") or "private")
    user_id = str((row["user_id"] if "user_id" in keys else "") or "")
    user_type = telegram_user_type(data_dir / "telegram_users.json", user_id=user_id)
    allow_command = (
        user_type if chat_type == "private"
        else telegram_group_allow_command(data_dir / "telegram_groups.json",
                                          chat_id=str(row["chat_id"]))
    )

    # A negative command_type is how a command is switched off (see process_one_command_message),
    # so that — not an `active` key — is what decides "runnable" here.
    runnable, disabled = listing_lib.active_only(
        entries, key=lambda entry: int(entry.get("command_type", 0) or 0) >= 0,
    )
    cleared, above_clearance = listing_lib.active_only(
        runnable,
        key=lambda entry: can_run_command(
            allow_command=allow_command,
            user_type=user_type,
            command_type=int(entry.get("command_type", 0) or 0),
        ),
    )
    here, wrong_chat = listing_lib.active_only(
        cleared,
        key=lambda entry: bool(
            entry.get("is_private") == 1 if chat_type == "private" else entry.get("is_group") == 1
        ),
    )
    here = sorted(here, key=lambda entry: str(entry.get("command_text") or ""))

    lines = [f"Bot commands you can run here: {len(here)}"]
    for entry in here:
        arguments = _command_parameter_summary(dict(entry.get("action_config") or {}))
        clearance = int(entry.get("command_type", 0) or 0)
        suffix = "" if clearance == 0 else f"  [clearance {clearance}]"
        lines.append(f"/{entry.get('command_text')} {arguments}".rstrip() + suffix)

    # Each reason is counted separately: "12 hidden" tells an operator nothing, while "above your
    # clearance" and "only runs in a group" are two different things to do about it.
    notes = []
    if above_clearance:
        notes.append(f"({above_clearance} hidden: above your clearance.)")
    if wrong_chat:
        where = "a group" if chat_type == "private" else "a private chat"
        notes.append(f"({wrong_chat} hidden: they only run in {where}.)")
    if disabled:
        notes.append(f"({disabled} hidden: turned off with command_type < 0.)")
    if notes:
        lines.extend(["", *notes])
    listing = "\n".join(lines)

    result: dict[str, Any] = {
        "command_count": len(here),
        "hidden_count": above_clearance + wrong_chat + disabled,
    }
    if len(listing) > TELEGRAM_LISTING_TEXT_LIMIT:
        file_path = _queue_listing_document(
            store=store, row=row, command=command, source_id=source_id,
            payload={"commands": [
                {"command_text": entry.get("command_text"),
                 "arguments": _command_parameter_summary(dict(entry.get("action_config") or {})),
                 "command_type": entry.get("command_type"),
                 "action_type": entry.get("action_type")}
                for entry in here
            ]},
            file_prefix="bot_commands",
            caption=f"Bot commands you can run here: {len(here)}. Full list attached as JSON.",
        )
        result["listing"] = (
            lines[0] + "\nThe listing is too long for one message - full JSON file attached."
        )
        result["file_path"] = file_path
        result["_queued_reply_count"] = 1
    else:
        result["listing"] = listing
    return result


def _format_time_window_line(window: dict[str, Any] | None) -> str:
    """Compact one-line time-window text: only the set bounds + repeat/timeout.

    Read through the scheduler's parser (rules R20), so a legacy field name or a blank shows what
    the scheduler will do; a window it refuses says so instead of looking like a schedule.
    """
    parsed = window_of({"time_window": window if isinstance(window, dict) else {}})
    if parsed is None:
        return "invalid time_window (the scheduler refuses it)"
    # A manual entry keeps its day/hour bounds in the JSON, but nothing ever consults them.
    # Printing "day 1..31 hour 0..23" would tell the operator it runs all day, every day.
    if parsed.repeat_interval == MANUAL_ONLY:
        suffix = f" timeout {parsed.timeout}s" if parsed.timeout is not None else ""
        return f"manual (run with /spbot_run_sql_task){suffix}"
    parts: list[str] = []
    for name in ("year", "month", "day", "hour", "minute"):
        from_value = getattr(parsed, f"from_{name}")
        to_value = getattr(parsed, f"to_{name}")
        if from_value is None and to_value is None:
            continue
        parts.append(f"{name} {'-' if from_value is None else from_value}..{'-' if to_value is None else to_value}")
    if parsed.weekdays is not None:
        parts.append(f"on {weekdays_text(parsed.weekdays)}")
    if parsed.repeat_interval == 0:
        parts.append("run-once")
    elif parsed.repeat_interval is not None:
        parts.append(f"every {parsed.repeat_interval}s")
    if parsed.timeout is not None:
        parts.append(f"timeout {parsed.timeout}s")
    return " ".join(parts) or "always"


def _queue_listing_document(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    source_id: str,
    payload: dict[str, Any],
    file_prefix: str,
    caption: str,
) -> str:
    """Write ``payload`` as a JSON file and queue it back as a Telegram document."""
    timestamp = file_stamp()
    output_dir = resolve_result_output_dir("runtime/output/telegram/config_exports").resolve()
    if not is_relative_to(output_dir, TOOL_ROOT):
        raise TelegramCommandError(
            f"Refusing to create result folder outside tools/db_ops: {output_dir}", exit_code=2
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = safe_output_file_name(f"{file_prefix}_{timestamp}.json")
    file_path = (output_dir / file_name).resolve()
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    queue_message({
        "store": store_block_from(store),
        "message_type": "plain",
        "chat_id": str(row["chat_id"]),
        "text": caption,
        "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
        "note": f"Document for command {command.command_text}",
        "source_type": "telegram_command_messages",
        "source_id": source_id,
        "metadata": {
            "command_id": command.command_id,
            "command_text": command.command_text,
            "action_type": command.action_type,
            "status": "success_document",
            "document_path": str(file_path),
        },
    }, fallback_store=store)
    return str(file_path)


def execute_list_sql_tasks_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    source_id: str,
) -> dict[str, Any]:
    """List every configured SQL task with its targets and time windows
    (/spbot_list_sql_tasks, reply via {result_listing}). A listing longer than one
    Telegram message is sent as a JSON document instead.

    The tasks come from the sql_tasks app's own CLI (:func:`sql_tasks_listing`) — including
    which of them count as runnable, which this app used to decide for itself and could
    therefore get wrong. All that is left here is turning them into lines someone can read on a
    phone, which is this app's job and nobody else's.
    """
    listing_data = sql_tasks_listing()
    if not listing_data.get("ok"):
        return {
            "command_count": 0,
            "target_count": 0,
            "listing": (
                "Could not read the SQL task list from the sql_tasks app: "
                f"{listing_data.get('error') or 'unknown error'}"
            ),
        }

    sql_tasks = list(listing_data.get("sql_tasks") or [])
    command_count = int(listing_data.get("command_count") or 0)
    target_count = int(listing_data.get("target_count") or 0)

    lines = [f"SQL tasks: {command_count} command(s), {target_count} target(s)"]
    for item in sql_tasks:
        lines.append(f"#{item.get('sql_id')} {item.get('sql_code') or ''}")
        # What the operator has to be ready to supply before choosing this number in
        # /spbot_run_sql_task. Required ones are marked: that is the difference between a task
        # that runs on its own and one that will ask a question back.
        parameters = list(item.get("parameters") or [])
        if parameters:
            required = set(item.get("required_parameter_names") or [])
            rendered = ", ".join(
                f"{name}{'*' if name in required else ''}"
                for name in (item.get("parameter_names") or [])
            )
            lines.append(f"  params: {rendered}   (* = required)")
        for target in item.get("targets") or []:
            database_name = str(target.get("database_name") or "-")
            window_text = _format_time_window_line(target.get("time_window"))
            output_format = str(target.get("output_format") or "").strip()
            output_text = f" output={output_format}" if output_format else ""
            method = str(target.get("sql_access_method") or "direct")
            via_text = "" if method == "direct" else f" via={method}"
            lines.append(
                f"  -> {target.get('server_id') or '?'} db={database_name} "
                f"{window_text}{output_text}{via_text}"
            )
    note = hidden_note(int(listing_data.get("hidden_count") or 0), noun="entry")
    if note:
        lines.extend(["", note])
    listing = "\n".join(lines)

    result: dict[str, Any] = {
        "command_count": command_count,
        "target_count": target_count,
    }
    if len(listing) > TELEGRAM_LISTING_TEXT_LIMIT:
        file_path = _queue_listing_document(
            store=store, row=row, command=command, source_id=source_id,
            payload={"sql_tasks": sql_tasks},
            file_prefix="sql_tasks",
            caption=(
                f"SQL task list: {command_count} command(s), {target_count} target(s). "
                "Full configuration attached as JSON."
            ),
        )
        result["listing"] = lines[0] + "\nThe listing is too long for one message — full JSON file attached."
        result["file_path"] = file_path
        result["_queued_reply_count"] = 1
    else:
        result["listing"] = listing
    return result


def execute_list_metrics_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    source_id: str,
) -> dict[str, Any]:
    """List every metric definition with its repeat interval (/spbot_list_metrics,
    reply via {result_listing}); sends a JSON document when too long for one message."""
    from db_ops.lib.time_window import parse_time_window_config

    # metric_definitions.json belongs to metrics and is read in exactly one place
    # (common.data_sources) since 2026-08-15; this app only lists what is in it.
    metrics = data_sources.load_metric_definition_records(data_dir=TOOL_ROOT / "data")

    entries: list[dict[str, Any]] = []
    for item in sorted(metrics, key=lambda entry: str(entry.get("metric_code") or "")):
        metric_code = str(item.get("metric_code") or "").strip()
        if not metric_code:
            continue
        try:
            window = parse_time_window_config(item, context=f"metric_definitions.{metric_code}").time_window
        except RuntimeError:
            continue
        entries.append({
            "metric_code": metric_code,
            "db_type": str(item.get("db_type") or ""),
            "collector_type": str(item.get("collector_type") or ""),
            "active": bool(item.get("active", True)),
            "repeat_interval": window.repeat_interval,
            "timeout": window.timeout,
        })
    entries, hidden = active_only(entries)

    lines = [f"Metrics: {len(entries)} definition(s)"]
    for entry in entries:
        interval = entry["repeat_interval"]
        interval_text = "run-once" if interval == 0 else (f"every {interval}s" if interval is not None else "every -")
        lines.append(f"{entry['metric_code']} [{entry['collector_type']}/{entry['db_type']}] {interval_text}")
    note = hidden_note(hidden, noun="metric")
    if note:
        lines.extend(["", note])
    listing = "\n".join(lines)

    result: dict[str, Any] = {"metric_count": len(entries)}
    if len(listing) > TELEGRAM_LISTING_TEXT_LIMIT:
        file_path = _queue_listing_document(
            store=store, row=row, command=command, source_id=source_id,
            payload={"metrics": entries},
            file_prefix="metrics",
            caption=f"Metric list: {len(entries)} definition(s) with time windows. Attached as JSON.",
        )
        result["listing"] = lines[0] + "\nThe listing is too long for one message — full JSON file attached."
        result["file_path"] = file_path
        result["_queued_reply_count"] = 1
    else:
        result["listing"] = listing
    return result


def execute_metric_toggle_command(*, command: SupportCommand, args: list[str]) -> dict[str, Any]:
    """Enable/disable metric collection for one server (/spbot_metric_toggle).

    args: ``server_id`` ``on|off`` ``scope`` where scope is ``all``,
    ``collector:<sql|cmd|docker|k8s>``, or one metric_code. The config write goes through
    ``python -m db_ops.common.cli metric-toggle`` — the same atomic ``db_instances.json`` update
    an operator gets at a shell, reached the same way, so the bot cannot drift from the CLI."""
    from db_ops.transport import common_cli

    server_id = str(args[0]).strip() if len(args) >= 1 else ""
    state = str(args[1]).strip().lower() if len(args) >= 2 else ""
    scope = str(args[2]).strip() if len(args) >= 3 else ""
    if state not in {"on", "off"}:
        raise TelegramCommandError("state must be 'on' or 'off'.", exit_code=2)
    try:
        result = common_cli.run("metric-toggle",
                                {"server_id": server_id, "state": state, "scope": scope})
    except common_cli.CommonCliError as exc:
        raise TelegramCommandError(str(exc), exit_code=1) from exc
    detail_parts = list(result.get("changes") or []) + list(result.get("warnings") or [])
    return {
        "status": "changed" if result.get("changed") else "no change",
        "server_id": result.get("server_id"),
        "scope": result.get("scope"),
        "state": state,
        "detail": "\n".join(detail_parts) or "-",
    }
