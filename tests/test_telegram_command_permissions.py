import json
import sqlite3

from db_ops.db import DbOpsStore
from db_ops.telegram.command_processor import process_one_command_message


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
                "reply_text": "OK",
                "command_type": 1,
                "is_group": 1,
                "is_private": 1,
                "need_file": 0,
            }
        ],
    )


def insert_command_message(sqlite_path, *, chat_id, chat_type, user_id, message_id=10):
    store = DbOpsStore(sqlite_path)
    store.upsert_telegram_messages(
        [
            {
                "update_id": message_id,
                "message_id": message_id,
                "message_date": 1_779_478_400,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "user_id": user_id,
                "text": "/spbot_status",
                "raw": {},
            }
        ]
    )
    store.sync_telegram_command_messages(command_prefix="/spbot")
    with sqlite3.connect(sqlite_path) as conn:
        row = conn.execute(
            "SELECT telegram_command_message_id FROM telegram_command_messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
    return int(row[0])


def fetch_one_send_message(sqlite_path):
    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT tlgchat_id, message_text, reply_message_id, source_type, source_id, send_status
            FROM telegram_send_messages
            ORDER BY send_tlgmsg_id DESC
            LIMIT 1
            """
        ).fetchone()


def test_private_command_permission_denied_queues_reply(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_commands(commands_path)
    write_json(
        tmp_path / "telegram_users.json",
        "telegram_users",
        [{"user_id": "100", "user_type": 0, "status": "active"}],
    )
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])
    command_message_id = insert_command_message(
        sqlite_path,
        chat_id="100",
        chat_type="private",
        user_id="100",
    )

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )

    reply = fetch_one_send_message(sqlite_path)
    assert result["status"] == "permission_denied"
    assert result["queued_reply"] == 1
    assert reply["tlgchat_id"] == "100"
    assert reply["reply_message_id"] == 10
    assert "Permission denied for /spbot_status" in reply["message_text"]
    assert "private chat" in reply["message_text"]
    assert reply["source_type"] == "telegram_command_messages"
    assert reply["source_id"] == str(command_message_id)


def test_group_command_permission_denied_queues_reply_when_group_not_allowed(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_commands(commands_path)
    write_json(
        tmp_path / "telegram_users.json",
        "telegram_users",
        [{"user_id": "100", "user_type": 2, "status": "active"}],
    )
    write_json(
        tmp_path / "telegram_groups.json",
        "telegram_groups",
        [{"group_id": "-200", "allow_command": 0, "status": "active"}],
    )
    command_message_id = insert_command_message(
        sqlite_path,
        chat_id="-200",
        chat_type="group",
        user_id="100",
        message_id=11,
    )

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )

    reply = fetch_one_send_message(sqlite_path)
    assert result["status"] == "permission_denied"
    assert result["queued_reply"] == 1
    assert reply["tlgchat_id"] == "-200"
    assert reply["reply_message_id"] == 11
    assert "Permission denied for /spbot_status" in reply["message_text"]
    assert "this group" in reply["message_text"]


def write_private_only_command(path):
    """A command whose *chat type* is the restriction, not the user's level: is_group = 0."""
    write_json(
        path,
        "telegram_support_commands",
        [
            {
                "command_id": 11,
                "command_text": "spbot_status",
                "reply_default": 1,
                "reply_text": "OK",
                "command_type": 1,
                "is_group": 0,
                "is_private": 1,
                "need_file": 0,
            }
        ],
    )


def test_a_chat_type_refusal_says_it_is_the_chat_type(tmp_path):
    """The bug this pins down: a command blocked because is_group=0 replied "the user or chat
    permission is not enough", so the operator went looking at group_type and user_type — which
    were never the blocker. The reply must name the rule that actually refused."""
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_private_only_command(commands_path)
    write_json(tmp_path / "telegram_users.json", "telegram_users",
               [{"user_id": "1", "user_type": 2, "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups",
               [{"group_id": "-200", "allow_command": 2, "status": "active"}])
    command_message_id = insert_command_message(
        sqlite_path, chat_id="-200", chat_type="group", user_id="1", message_id=12)

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )

    reply = fetch_one_send_message(sqlite_path)
    assert result["status"] == "permission_denied"
    assert "is_group=0" in reply["message_text"]
    assert "private chat" in reply["message_text"]        # ... and what to do instead
    # The user is level 2 in a group allowed at level 2: neither is why it was refused.
    assert "user_type" not in reply["message_text"]
