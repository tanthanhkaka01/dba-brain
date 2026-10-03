"""An uploaded file is kept as its content, and its name is kept beside it.

A command that takes a workbook receives it as a base64 argument, and that argument is all the
conversation state used to hold. `/spbot_list_my_commands` rebuilds a command from that state, so
the only thing it could show for the file was the file: one entry ran to 111 messages. The name is
what a person recognises, and the processor now records it (`arg_files`) when the file arrives.
"""

from __future__ import annotations

import base64
import json
import sqlite3

from db_ops.db import telegram_command_history as history
from db_ops.db import DbOpsStore
from db_ops.telegram import command_processor

from conftest import patch_telegram

COMMAND_TEXT = "spbot_demo_upload"
USER = "100"
WORKBOOK = base64.b64encode(bytes(range(256)) * 8).decode("ascii")

COMMAND = {
    "command_id": 901, "command_text": COMMAND_TEXT, "command_type": 1, "is_group": 1,
    "is_private": 1, "need_file": 0, "reply_default": 0, "reply_text": "",
    "action_type": "cli_execute",
    "action_config": {
        "command_argv": ["{python}", "-c", "print('ok')"],
        "parameters": [
            {"name": "table", "source": "arg", "position": 1, "required": True,
             "prompt_text": "Table name?"},
            {"name": "file_base64", "source": "arg", "position": 2, "required": True,
             "accept_file": True, "file_encoding": "base64", "prompt_text": "Attach the file."},
        ],
    },
}


def _write(path, root_key, rows):
    path.write_text(json.dumps({root_key: rows}), encoding="utf-8")


def test_the_file_name_is_recorded_and_the_listing_shows_it(tmp_path, monkeypatch):
    commands_path = tmp_path / "telegram_support_commands.json"
    _write(commands_path, "telegram_support_commands", [COMMAND])
    _write(tmp_path / "telegram_users.json", "telegram_users",
           [{"user_id": USER, "user_type": 2, "status": "active"}])
    _write(tmp_path / "telegram_groups.json", "telegram_groups", [])
    sqlite_path = tmp_path / "runtime.sqlite"
    store = DbOpsStore(sqlite_path)
    # `max_bytes` is the parameter's own size cap (review 0.25.0, F8.3): the processor always passes
    # it, and a stand-in that does not take it fails the download instead of answering.
    patch_telegram(monkeypatch, "_download_document_base64",
                   lambda document, *, config_path, max_bytes=None: WORKBOOK)

    def post(message_id, text, raw):
        store.upsert_telegram_messages([{
            "update_id": message_id, "message_id": message_id,
            "message_date": 1_779_478_400 + message_id, "chat_id": USER, "chat_type": "private",
            "user_id": USER, "text": text, "raw": raw,
        }])

    sender = {"from": {"id": int(USER), "username": "op"}}
    post(11, f"/{COMMAND_TEXT}", sender)
    store.sync_telegram_command_messages(command_prefix="/spbot")
    with sqlite3.connect(sqlite_path) as connection:
        command_message_id = connection.execute(
            "SELECT max(telegram_command_message_id) FROM telegram_command_messages").fetchone()[0]
    command_processor.process_one_command_message(
        sqlite_path=sqlite_path, telegram_command_message_id=int(command_message_id),
        commands_path=commands_path)
    post(12, "Maintenance", sender)
    command_processor.process_pending_conversation_messages(
        sqlite_path=sqlite_path, commands_path=commands_path)
    post(13, "", {**sender, "document": {"file_id": "F1", "file_name": "Maintenance.xlsx"}})
    command_processor.process_pending_conversation_messages(
        sqlite_path=sqlite_path, commands_path=commands_path)

    with sqlite3.connect(sqlite_path) as connection:
        state_json = connection.execute(
            "SELECT state_json FROM telegram_conversation_states "
            "ORDER BY state_id DESC LIMIT 1").fetchone()[0]
    state = json.loads(state_json)
    assert state["args"][1] == WORKBOOK
    assert state["arg_files"] == {"2": "Maintenance.xlsx"}

    listing = history.render(history.collect(store, user_id=USER))
    assert "<uploaded file: Maintenance.xlsx, 2.0 KB>" in listing
    assert WORKBOOK not in listing
