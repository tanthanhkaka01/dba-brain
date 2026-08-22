from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db_ops.db import DbOpsStore
from db_ops.lib.paths import TOOL_ROOT


DEFAULT_SQLITE_SCHEMA_DIR = TOOL_ROOT / "runtime"


def export_sqlite_schema(
    *,
    sqlite_path: str | Path,
    output_dir: str | Path = DEFAULT_SQLITE_SCHEMA_DIR,
) -> dict[str, Any]:
    store = DbOpsStore(sqlite_path)
    store.initialize()
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                name,
                type,
                sql
            FROM sqlite_master
            WHERE type IN ('table', 'index')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name;
            """
        ).fetchall()

        tables = []
        indexes = []
        for row in rows:
            if row["type"] == "table":
                tables.append(export_table(conn, row["name"], row["sql"]))
            elif row["type"] == "index":
                indexes.append(
                    {
                        "index_name": row["name"],
                        "create_sql": row["sql"],
                    }
                )

    schema = {
        "_comment": "Generated SQLite table-structure snapshot for DB Ops. When SQLite table structure changes in db_ops/db/store.py, regenerate this file so the JSON stays synchronized.",
        "sqlite_path": str(sqlite_path),
        "tables": tables,
        "indexes": indexes,
    }
    output_path = target_dir / "db_ops_sqlite_structure.json"
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(schema, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return {
        "ok": True,
        "output_path": str(output_path),
        "tables": len(tables),
        "indexes": len(indexes),
    }


def export_table(conn, table_name: str, create_sql: str) -> dict[str, Any]:
    columns = []
    for column in conn.execute(f"PRAGMA table_info({table_name});").fetchall():
        columns.append(
            {
                "column_id": column["cid"],
                "name": column["name"],
                "type": column["type"],
                "not_null": bool(column["notnull"]),
                "default_value": column["dflt_value"],
                "primary_key_position": column["pk"],
                "comment": column_comment(table_name, column["name"]),
            }
        )

    return {
        "table_name": table_name,
        "comment": table_comment(table_name),
        "create_sql": create_sql,
        "columns": columns,
    }


def table_comment(table_name: str) -> str:
    comments = {
        "schema_meta": "Tracks local SQLite schema version.",
        "job_runs": "Append-only operational job run log.",
        "report_types": "Parent lookup table for report types and their alert level.",
        "reports": "Generated report rows waiting to be pushed to delivery queues.",
        "telegram_messages": "Telegram messages captured from getUpdates. Large event table.",
        "telegram_command_messages": "Subset of telegram_messages whose text starts with command prefix such as /spbot.",
        "telegram_send_messages": "Queue of Telegram messages that should be sent by the bot.",
    }
    return comments.get(table_name, "")


def column_comment(table_name: str, column_name: str) -> str:
    comments = {
        ("telegram_messages", "raw_json"): "Full Telegram message payload. Does not download/store local files.",
        ("telegram_command_messages", "command_status"): "0=pending, 1=processed, -1=skipped, -2=command not found.",
        ("telegram_command_messages", "command_id"): "Matched support command id. Nullable until processor matches command.",
        ("telegram_command_messages", "reply_message_id"): "Telegram message_id of reply sent for this command.",
        ("telegram_command_messages", "processed_at"): "UTC timestamp when command processor handled the row.",
        ("telegram_command_messages", "command_payload"): "Message text after command_prefix, trimmed.",
        ("telegram_send_messages", "send_tlgmsg_id"): "Primary key, equivalent to Oracle SEND_TLGMSG_ID.",
        ("telegram_send_messages", "row_ins_date"): "Insert date, equivalent to Oracle ROW_INS_DATE.",
        ("telegram_send_messages", "tlgchat_id"): "Telegram chat id, equivalent to Oracle TLGCHAT_ID.",
        ("telegram_send_messages", "list_tlguser_id"): "Optional comma-separated Telegram user id list.",
        ("telegram_send_messages", "message_text"): "Outgoing message text.",
        ("telegram_send_messages", "entities"): "Optional Telegram entities JSON/text.",
        ("telegram_send_messages", "send_status"): "-1=failed, 0=pending, 1=sent, 2=processing.",
        ("telegram_send_messages", "send_date"): "Date/time when send finished.",
        ("telegram_send_messages", "message_id"): "Telegram message_id returned by sendMessage after successful send.",
        ("telegram_send_messages", "reply_message_id"): "Optional Telegram message_id to reply to.",
        ("telegram_send_messages", "source_type"): "Optional origin, for example command_processor.",
        ("telegram_send_messages", "source_id"): "Optional origin id.",
        ("job_runs", "metadata_json"): "Optional structured metadata for the run.",
        ("report_types", "report_type"): "Primary key used by reports.report_type.",
        ("report_types", "report_level"): "Routing level: logging, warning, or critical.",
        ("reports", "report_id"): "Primary key for generated reports.",
        ("reports", "report_code"): "Stable code for the generated report.",
        ("reports", "report_name"): "Human-readable report name.",
        ("reports", "report_type"): "FK to report_types.report_type.",
        ("reports", "report_level"): "Copied routing level for fast filtering.",
        ("reports", "status"): "created=pending push, pushed=queued to Telegram, skipped=not pushed, failed=push failed.",
        ("reports", "pushed_at"): "UTC timestamp when report was pushed into telegram_send_messages.",
        ("reports", "telegram_send_message_id"): "telegram_send_messages.send_tlgmsg_id created from this report.",
    }
    return comments.get((table_name, column_name), "")

def main() -> None:
    result = export_sqlite_schema(
        sqlite_path="runtime/db_ops.sqlite",
        output_dir="runtime",
    )
    print(result)


if __name__ == "__main__":
    main()