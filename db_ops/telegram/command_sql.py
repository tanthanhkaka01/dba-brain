"""The Telegram support commands: the SQL actions - a result as a spreadsheet, a spreadsheet as a table, a new SQL task.

Split out of ``telegram/command_processor.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``command_processor`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
from db_ops.lib import errors
import json
import re
from pathlib import Path
from typing import Any
from db_ops.transport import common_cli
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
from db_ops.lib.time_window import MANUAL_ONLY
from db_ops.db import DbOpsStore
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.timezone import file_stamp
from db_ops.telegram.command_base import SupportCommand, TelegramCommandError, _describe_source, _format_argument_position
from db_ops.telegram.command_cli import finish_common_request, is_relative_to, render_template, resolve_result_output_dir, safe_error_summary, safe_output_file_name


def execute_create_table_from_xlsx_command(*, command: SupportCommand,
                                           args: list[str]) -> dict[str, Any]:
    """Create a table from an attached file: /spbot_xlsx_to_table.

    Four answers, collected by the ordinary prompt loop — server_id, database, schema, then the
    file itself. The file arrives as base64 because the awaited parameter declares
    ``file_encoding: "base64"``; from there it is the same JSON object
    :mod:`db_ops.common.table_load` takes from a shell, so the Telegram path and the CLI path
    cannot drift. An .xlsx and a delimited text file are both accepted and both look identical
    here — `table_load` decides which it is from the bytes.

    ``table_name`` is deliberately **not** prompted for. The common case is "I need this
    queryable now", the generated ``temp_<random>`` answers it, and one more prompt between an
    operator and the thing they wanted is a step at which people give up. The name is in the
    reply, which is what they need to find it again. Someone who wants to choose passes it as a
    fifth word on the command line.
    """
    def _arg(position: int) -> str:
        return str(args[position - 1]).strip() if len(args) >= position else ""

    request: dict[str, Any] = {
        "target": _arg(1),
        "database_name": _arg(2),
        "schema": _arg(3),
        "file_base64": _arg(4),
        "table_name": _arg(5),
    }
    # A command may pin any of these in its own config; the config is the one place that decides
    # whether this deployment lets a Telegram user drop an existing table.
    for key in ("if_exists", "load_rows", "text_length", "max_rows", "credential_name",
                "delimiter"):
        if key in (command.action_config or {}):
            request[key] = (command.action_config or {})[key]

    if not request["file_base64"]:
        raise TelegramCommandError(
            "No file received. Attach the .xlsx or delimited text file to the message that "
            "answers the last prompt.", exit_code=2)
    # Through the `common` CLI: building a table from a spreadsheet and loading it is work on a
    # customer database, and the request above is already the exact JSON that command takes — the
    # Telegram path and a shell caller hand over the same object.
    try:
        data = common_cli.run("create-table-from-xlsx", finish_common_request("create-table-from-xlsx", request))
    except common_cli.CommonCliError as exc:
        raise TelegramCommandError(str(exc), exit_code=1) from exc

    # Keys are returned **unprefixed**. `render_reply_text` exposes each scalar as
    # `{result_<key>}` itself, so returning `result_server_id` here becomes
    # `{result_result_server_id}` and the template renders it as nothing — which is exactly what
    # shipped on 2026-08-13: a reply that said "done" over blank fields, so the operator could
    # not tell which table had been created or whether anything had.
    return {
        "status": "success",
        "server_id": data["server_id"],
        "database": data["database_name"],
        "schema": data["schema"],
        "table_name": data["table_name"],
        "qualified_name": data["qualified_name"],
        "column_count": data["column_count"],
        "column_type": data["column_type"],
        "rows_inserted": data["rows_inserted"],
        # How the file was read. On a text file the delimiter is a guess, and a wrong guess makes
        # a table whose columns look plausible — this line is where the operator sees it.
        "source_format": _describe_source(data),
        # `row_count` is one of the renderer's own top-level placeholders, not just a
        # `result_*` one, so the generic `{row_count}` in any template still fills.
        "row_count": data["rows_inserted"],
    }


def execute_sql_to_xlsx_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    args: list[str],
    source_id: str,
) -> dict[str, Any]:
    """Run a read-only SELECT on the target (arg 1) using ``sql_text`` (arg 2), write the first
    result set to an .xlsx, and queue it back as a Telegram document. The target is a server_id
    or a ``<db_type> <ip> [port]`` spec (see :func:`db_ops.common.sql_run.resolve_sqlserver_target`).

    The connection is never committed: :mod:`db_ops.common.sql_run` rolls back and reports
    ``affected_rows``, it does not reject them. A rollback does not undo everything (KILL,
    RECONFIGURE, xp_cmdshell, an explicit COMMIT, DDL on Oracle/MySQL), which is why the shipped
    catalogue puts this action at level 50 (:data:`ACTION_LEVEL_RECOMMENDED`, review 0.25.0, F8.1). A known failure raises
    :class:`TelegramCommandError` so the reply template echoes it to the user; the document is
    queued only on success.
    """
    from db_ops.telegram import sql_commands
    from db_ops.lib import result_format
    from db_ops.lib.task_output import FILE_OUTPUT_FORMATS
    from db_ops.lib.xlsx_export import MAX_CELL_TEXT

    config = dict(command.action_config or {})
    # Which file the operator gets. A command may fix it in action_config (that is what keeps
    # /spbot_sql_to_xlsx producing exactly what its name promises) or declare a `format`
    # PARAMETER, in which case it is an argument the caller supplies. Read from the declared
    # parameters rather than a hard-coded index, so the config stays the one place that says
    # which argument is which.
    format_position = _format_argument_position(config)
    if format_position:
        index = format_position - 1
        export_format = str(args[index]).strip() if len(args) > index else ""
        args = list(args[:index]) + list(args[index + 1:])
    else:
        export_format = str(config.get("format") or "xlsx")
    try:
        export_format = result_format.normalize_format(export_format)
    except result_format.ResultFormatError as exc:
        raise TelegramCommandError(str(exc), exit_code=2) from exc
    if export_format not in FILE_OUTPUT_FORMATS:
        # `raw` and `json` render fine but are not what someone asking for a document wants:
        # raw exists to be piped in a shell, and neither opens in anything an operator has.
        raise TelegramCommandError(
            f"format must be one of {', '.join(FILE_OUTPUT_FORMATS)}; got {export_format!r}.",
            exit_code=2,
        )

    # arg 1 is the target: a server_id, or a "<db_type> <ip> [port]" spec delivered as one
    # message via the conversation prompt. Inline, only the single-token server_id form works
    # (a multi-word spec would collide with the consume_rest SQL text).
    target = str(args[0]).strip() if len(args) >= 1 else ""
    # sql_text is a consume_rest parameter: an inline command splits the SQL across tokens, while
    # the conversation flow delivers the whole pasted message as a single arg. Joining args[1:]
    # reconstructs both.
    sql_text = " ".join(str(part) for part in args[1:]).strip()
    if not target:
        raise TelegramCommandError("target (server_id, or '<db_type> <ip> [port]') is required.", exit_code=2)
    if not sql_text:
        raise TelegramCommandError("sql_text is required.", exit_code=2)

    max_rows = int(config.get("max_rows") or sql_commands.DEFAULT_SQL_TO_XLSX_MAX_ROWS)
    timeout_seconds = int(config.get("connect_timeout_seconds") or sql_commands.DEFAULT_CONNECT_TIMEOUT_SECONDS)
    try:
        result = sql_commands.run_sql_to_xlsx(
            target=target,
            sql_text=sql_text,
            # Optional: pin the database the SQL runs in, so a query does not have to open with
            # "USE <db>;". Unset = the target instance's own database.
            database=str(config.get("database") or config.get("database_name") or ""),
            # Optional: run as a named login from users.json instead of the instance default
            # (which is often a DBA account). Set it to a read-only credential where one exists.
            credential_name=str(config.get("credential_name") or ""),
            max_rows=max_rows,
            timeout_seconds=timeout_seconds,
        )
    except sql_commands.SqlToXlsxError as exc:
        # A known, user-facing failure (unknown server_id / non-SELECT / connect / SQL error):
        # report it verbatim so the reply template can show it.
        raise TelegramCommandError(safe_error_summary(exc), exit_code=1) from exc

    timestamp = file_stamp()
    output_dir = resolve_result_output_dir(
        str(config.get("output_dir") or "runtime/output/telegram/sql_to_xlsx")
    ).resolve()
    if not is_relative_to(output_dir, TOOL_ROOT):
        raise TelegramCommandError(
            f"Refusing to create result folder outside tools/db_ops: {output_dir}", exit_code=2
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = safe_output_file_name(
        render_template(
            str(config.get("file_name_template") or "sql_to_xlsx_{server_id}_{timestamp}"),
            {"server_id": result["server_id"], "timestamp": timestamp},
        )
    )
    if not file_name.lower().endswith(f".{export_format}"):
        file_name += f".{export_format}"
    file_path = (output_dir / file_name).resolve()
    if not is_relative_to(file_path, TOOL_ROOT):
        raise TelegramCommandError(
            f"Refusing to write result file outside tools/db_ops: {file_path}", exit_code=2
        )

    # One call whatever the format: db_ops.lib.result_format owns which formats write
    # themselves and which return text, so this handler and the sql_tasks exporter cannot drift
    # into two files that look different for the same query.
    written = result_format.write_result(
        {"ok": True, "columns": result["columns"], "rows": result["rows"],
         "row_count": result["row_count"]},
        fmt=export_format,
        path=file_path,
        sheet_name="Result",
    )

    notes = []
    if result["truncated"]:
        notes.append(" (truncated to the row limit)")
    if written.get("truncated_cells"):
        # Excel drops a string over 32,767 chars and calls the file damaged, so the writer cuts
        # it. Say so: a silently shortened query_plan / query_sql_text is worse than a warning.
        notes.append(
            f" ({written['truncated_cells']} cell(s) cut to Excel's {MAX_CELL_TEXT:,}-character limit)"
        )
    caption = render_template(
        str(
            config.get("caption")
            or "SQL result for {server_id} ({database}): {row_count} row(s){truncated_note}."
        ),
        {
            "server_id": result["server_id"],
            "database": result["database"],
            "row_count": result["row_count"],
            "truncated_note": "".join(notes),
        },
    )
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
    return {
        "server_id": result["server_id"],
        "database": result["database"],
        # Available to reply templates as {result_username} / {result_credential_name}.
        "credential_name": result.get("credential_name", ""),
        "username": result.get("username", ""),
        "row_count": result["row_count"],
        "affected_rows": result["affected_rows"],
        "truncated": result["truncated"],
        "column_count": len(result["columns"]),
        # 0 for every format but xlsx: only Excel has a per-cell length limit to hit.
        "truncated_cells": written.get("truncated_cells", 0),
        "file_path": str(file_path),
        "file_name": file_path.name,
        "_queued_reply_count": 1,
    }


def _message_document(message: Any) -> dict[str, Any] | None:
    """Return the inbound Telegram document dict (file_id, ...) from the message row, if any."""
    try:
        raw = message["raw_json"]
    except (KeyError, IndexError, TypeError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    document = data.get("document") if isinstance(data, dict) else None
    if isinstance(document, dict) and document.get("file_id"):
        return document
    return None


def _max_file_bytes(parameter: dict[str, Any]) -> int | None:
    """The parameter's ``max_file_bytes``, or ``None`` - a value that is not a positive number is
    refused rather than read as "no limit"."""
    raw = parameter.get("max_file_bytes")
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise errors.InvalidConfig(f"max_file_bytes must be a positive number of bytes, got {raw!r}") from None
    if value <= 0:
        raise errors.InvalidConfig(f"max_file_bytes must be a positive number of bytes, got {raw!r}")
    return value


def _download_document_text(document: dict[str, Any], *, config_path: str | Path,
                            max_bytes: int | None = None) -> str:
    """Download an attached document and decode it as text (utf-8, BOM tolerant)."""
    from db_ops.lib.config import load_config
    from db_ops.telegram import api

    config = load_config(config_path)
    data = api.get_file_bytes(
        bot_token=config.telegram.resolved_bot_token,
        file_id=str(document["file_id"]),
        api_url=config.telegram.api_url,
        max_bytes=max_bytes,
    )
    text = data.decode("utf-8-sig").strip()
    if not text:
        raise errors.InvalidRequest("Attached file is empty.")
    return text


def _download_document_base64(document: dict[str, Any], *, config_path: str | Path,
                              max_bytes: int | None = None) -> str:
    """Download an attached document and return it base64-encoded.

    For a binary attachment — a workbook, an archive — where decoding as text would either raise
    or, worse, succeed against a zip's bytes and hand the action something that is no longer the
    file. base64 is what a JSON request can carry, so the value goes straight into the
    `common` CLI payload with nothing else to agree on.
    """
    import base64

    from db_ops.lib.config import load_config
    from db_ops.telegram import api

    config = load_config(config_path)
    data = api.get_file_bytes(
        bot_token=config.telegram.resolved_bot_token,
        file_id=str(document["file_id"]),
        api_url=config.telegram.api_url,
        max_bytes=max_bytes,
    )
    if not data:
        raise errors.InvalidRequest("Attached file is empty.")
    return base64.b64encode(data).decode("ascii")


_ADD_SQL_NONE_TOKENS = {"", "-", "none", "null", "skip", "na", "n/a"}

# The schedule answer that means "never run this on a timer"; it becomes
# time_window.repeat_interval = MANUAL_ONLY (-1), the convention every scheduler shares.
MANUAL_SCHEDULE_WORD = "manual"


def _add_sql_optional(value: str) -> str | None:
    text = str(value or "").strip()
    return None if text.lower() in _ADD_SQL_NONE_TOKENS else text


def parse_add_sql_time_window(spec: str) -> dict[str, int] | None:
    """Parse a Telegram time-window step into a config_admin time_window dict.

    Accepts ``default``/empty (→ engine defaults), ``manual`` (→ ``repeat_interval = -1``, the
    shared MANUAL_ONLY convention: never scheduled, forced runs only), or up to four
    space/comma separated integers ``from_hour to_hour repeat_interval timeout``.
    """
    text = str(spec or "").strip()
    if text.lower() == MANUAL_SCHEDULE_WORD:
        return {"repeat_interval": MANUAL_ONLY}
    if text.lower() in {"", "default", "-", "none"}:
        return None
    parts = [p for p in re.split(r"[\s,]+", text) if p]
    keys = ("from_hour", "to_hour", "repeat_interval", "timeout")
    window: dict[str, int] = {}
    for key, part in zip(keys, parts):
        try:
            window[key] = int(part)
        except ValueError as exc:
            raise ValueError(f"time_window value for {key} must be an integer, got {part!r}") from exc
    return window or None


def execute_add_sql_task_command(*, command: SupportCommand, args: list[str]) -> dict[str, Any]:
    """Register + enable a new SQL task from the collected conversation parameters.

    Parameter order (from action_config.parameters): server_id, display_name, schedule, output,
    sql_text. **db_type, instance and target database are not asked for** — they are already
    recorded against the server in ``db_instances.json``, so asking made the conversation four
    messages longer and let the operator enter values that do not resolve (that is how
    SQLSERVER-017 got a null instance and a task that could never find its database).

    All file and config mutation goes through ``python -m db_ops.common.cli add-sql`` — the same
    JSON object, the same atomic writes and the same validation an operator gets at a shell.
    Two things are still decided here, and both are *reads*, not writes: the schedule word is
    Telegram's own vocabulary (``manual`` / ``default`` / four integers), and the target's
    db_type, instance and credential come from ``data_sources``, the one reader of the data
    folder — the operator was never asked for them, so they have to be looked up before the
    request can be built.

    Always returns a result dict (never raises) so the reply template can echo the outcome;
    ``result_status`` is ``OK`` or ``FAILED``.
    """
    from db_ops.lib import data_sources
    from db_ops.lib import task_output
    from db_ops.transport import common_cli

    def arg(position: int) -> str:
        return str(args[position - 1]).strip() if len(args) >= position else ""

    empty_result = {"status": "FAILED", "sql_id": "", "sql_code": "", "script_path": "",
                    "server_id": arg(1), "schedule": "", "output": ""}
    try:
        window = parse_add_sql_time_window(arg(3))
        output = task_output.normalize_output(arg(4))
        resolved = data_sources.resolve_sql_target_fields(arg(1))
        result = common_cli.run("add-sql", {
            "db_type": resolved["db_type"],
            "server_id": resolved["server_id"],
            "service_name": resolved["service_name"],
            "instance_name": resolved["instance_name"],
            "credential_name": resolved["credential_name"],
            "display_name": arg(2),
            "sql_text": arg(5),
            "output": output,
            # The window's four keys are the command's own flags, so they travel as fields rather
            # than as a nested object — `add-sql` has exactly one parser and this is what it reads.
            **(window or {}),
        })
    except (common_cli.CommonCliError, data_sources.TargetResolveError, ValueError) as exc:
        return {**empty_result, "error": str(exc), "_queued_reply_count": 0}
    return {
        "status": "OK",
        "sql_id": result["sql_id"],
        "sql_code": result["sql_code"],
        "script_path": result["script_path"],
        "server_id": result["server_id"],
        "schedule": MANUAL_SCHEDULE_WORD if result["manual_only"] else "scheduled",
        "output": result["output"],
        "error": "",
        "_queued_reply_count": 0,
    }
