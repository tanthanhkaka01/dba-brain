import json
import sqlite3

from db_ops.db import DbOpsStore
from db_ops.telegram.command_processor import parse_command_message, process_one_command_message


def write_json(path, root_key, rows):
    path.write_text(json.dumps({root_key: rows}, ensure_ascii=False), encoding="utf-8")


def write_commands(path):
    write_json(
        path,
        "telegram_support_commands",
        [
            {
                "command_id": 1,
                "command_text": "spbot_status",
                "reply_default": 1,
                "reply_text": "I still working hard right now",
                "command_type": 1,
                "is_group": 1,
                "is_private": 1,
                "need_file": 0,
            }
        ],
    )


def prepare_store(tmp_path, text):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_commands(commands_path)
    write_json(
        tmp_path / "telegram_users.json",
        "telegram_users",
        [{"user_id": "100", "user_type": 1, "status": "active"}],
    )
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])

    store = DbOpsStore(sqlite_path)
    store.upsert_telegram_messages(
        [
            {
                "update_id": 10,
                "message_id": 10,
                "message_date": 1_779_478_400,
                "chat_id": "100",
                "chat_type": "private",
                "user_id": "100",
                "text": text,
                "raw": {"from": {"id": 100, "username": "admin"}},
            }
        ]
    )
    saved = store.sync_telegram_command_messages(command_prefix="/spbot")
    return sqlite_path, commands_path, saved


def fetch_command_message_id(sqlite_path):
    with sqlite3.connect(sqlite_path) as conn:
        row = conn.execute(
            "SELECT telegram_command_message_id FROM telegram_command_messages WHERE chat_id = ? AND message_id = ?",
            ("100", 10),
        ).fetchone()
    return int(row[0]) if row else None


def fetch_send_messages(sqlite_path):
    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        return list(
            conn.execute(
                """
                SELECT message_text, reply_message_id, source_type, metadata_json
                FROM telegram_send_messages
                ORDER BY send_tlgmsg_id ASC
                """
            )
        )


def process_text(tmp_path, text):
    sqlite_path, commands_path, saved = prepare_store(tmp_path, text)
    command_message_id = fetch_command_message_id(sqlite_path)
    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )
    return sqlite_path, saved, result


def test_valid_status_command_queues_existing_reply(tmp_path):
    sqlite_path, saved, result = process_text(tmp_path, "/spbot_status")

    messages = fetch_send_messages(sqlite_path)
    assert saved == 1
    assert result["status"] == "processed"
    assert result["queued_reply"] == 1
    assert messages[-1]["message_text"] == "I still working hard right now"


def test_valid_status_command_with_bot_suffix_queues_existing_reply(tmp_path):
    sqlite_path, saved, result = process_text(tmp_path, "/spbot_status@it_dev_code_sp_bot")

    messages = fetch_send_messages(sqlite_path)
    assert parse_command_message("/spbot_status@it_dev_code_sp_bot")["command_key"] == "spbot_status"
    assert saved == 1
    assert result["status"] == "processed"
    assert result["queued_reply"] == 1
    assert messages[-1]["message_text"] == "I still working hard right now"


def test_unknown_spbot_command_queues_validation_reply(tmp_path):
    sqlite_path, saved, result = process_text(tmp_path, "/spbot_report_hourly_metrics_wrong")

    messages = fetch_send_messages(sqlite_path)
    assert saved == 1
    assert result["status"] == "command_not_found"
    assert result["queued_reply"] == 1
    assert messages[-1]["message_text"] == (
        "Unknown support command: /spbot_report_hourly_metrics_wrong\n\n"
        "Please check the command name or use /spbot_status to verify the bot is running."
    )


def test_unknown_spbot_command_with_bot_suffix_queues_validation_reply(tmp_path):
    sqlite_path, saved, result = process_text(tmp_path, "/spbot_report_hourly_metrics_wrong@it_dev_code_sp_bot")

    messages = fetch_send_messages(sqlite_path)
    metadata = json.loads(messages[-1]["metadata_json"])
    assert parse_command_message("/spbot_report_hourly_metrics_wrong@it_dev_code_sp_bot")["command_key"] == "spbot_report_hourly_metrics_wrong"
    assert saved == 1
    assert result["status"] == "command_not_found"
    assert result["queued_reply"] == 1
    assert messages[-1]["message_text"].startswith("Unknown support command: /spbot_report_hourly_metrics_wrong")
    assert metadata["command_key"] == "spbot_report_hourly_metrics_wrong"


def test_normal_text_still_does_not_enter_support_command_flow(tmp_path):
    sqlite_path, _commands_path, saved = prepare_store(tmp_path, "hello bot")

    assert saved == 0
    assert fetch_command_message_id(sqlite_path) is None
    assert fetch_send_messages(sqlite_path) == []
