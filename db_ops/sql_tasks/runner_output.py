"""A SQL task's result: formatted, trimmed for the store, written to a file, and queued to Telegram.

Split out of ``sql_tasks/runner.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``runner`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
from db_ops.lib.text_format import format_log_value, format_message_time  # noqa: F401 - one definition, see that module
from db_ops.lib.data_sources import _server_id_from_instance  # noqa: F401 - one definition, see that module
import json
from pathlib import Path
from typing import Any
from db_ops.lib.sql_task_catalog import (  # noqa: F401 - re-exported for this module's callers
    _AMBIGUOUS_CREDENTIAL, DEFAULT_INLINE_MAX_ROWS, DEFAULT_SQL_TIMEOUT_SECONDS, INPUT_TYPES,
    SQL_TARGET_NOTIFY_DEFAULTS, XLSX_MAX_ROWS, SqlCommand, SqlTarget, _opt_str, collect_sql_tasks,
    load_default_credential_names, load_input_definition, load_sql_access_by_server,
    load_sql_commands, load_sql_script_definition, load_sql_targets, resolve_sql_folder)
from db_ops.lib.notify import (
    NotifyRule,
)
from db_ops.lib.task_output import (
    FILE_OUTPUT_FORMATS,
    merge_result_sets,
)
from db_ops.lib import result_format
from db_ops.db.queue_message import queue_message, store_block_from
from db_ops.lib import sql_task_target
from db_ops.lib.timezone import file_stamp
from db_ops.db import DbOpsStore
from db_ops.logging_ops import log_event
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.sql_tasks.runner_plan import workflow_name_from_code


# What goes into sql_runs.result_json regardless of how many rows were fetched: an export must
# not turn every run row into a multi-megabyte JSON blob in the store. **Its own number, not an
# alias of the inline cap** — it used to be `= MAX_RESULT_ROWS`, so raising how much an operator
# sees in chat would have quietly multiplied the size of every stored run row too.
STORED_RESULT_MAX_ROWS = 100
#: Kept for callers that imported it; the inline default now carries the meaning.
MAX_RESULT_ROWS = DEFAULT_INLINE_MAX_ROWS

#: How many of a script's result sets are kept, for the store row and the Telegram table. Five,
#: because that is what the app's own batch reader kept before it called `run-sql` instead and
#: `sql_runs.result_json` is read against it. The rows of the sets beyond it are still *counted*
#: into `row_count` — dropping them from the total would make a run look smaller than it was.
MAX_STORED_RESULT_SETS = 5


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
