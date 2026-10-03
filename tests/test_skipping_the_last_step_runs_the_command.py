"""Skipping a command's last, optional step runs the command - it does not ask the same question again.

Found on the 0.26 soak node's bot, 2026-10-02 (1.87): ``/spbot_backup LAB251_PG_WAL`` with no level
asked *Level? full / diff / log - or Skip to let the schedule decide*; the operator typed ``skip``,
the answer was consumed - and the bot asked the same question again. The level is the backup's
last parameter, a choice of three with Skip allowed, and Skip stores ``-``, which the backup reads as
"let the schedule decide". With the level typed in the command the backup ran.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import patch_telegram
from db_ops.db import DbOpsStore
from db_ops.telegram.command_processor import (
    process_one_command_message,
    process_pending_conversation_messages,
)

USER = "100"

#: /spbot_backup's two parameters as shipped (db_ops/telegram/catalogue/telegram_support_commands.json).
COMMAND = {
    "command_id": 16, "command_text": "spbot_backup", "command_type": 1, "is_group": 1,
    "is_private": 1, "need_file": 0, "reply_default": 0, "reply_text": "",
    "action_type": "cli_execute",
    "action_config": {
        "command_argv": ["{python}", "-c", "print('replaced in these tests')", "{backup_id}"],
        "conditional_args": [{"parameter": "backup_type", "not_equals": "-",
                              "argv": ["--backup-type", "{backup_type}"]}],
        "parameters": [
            {"name": "backup_id", "source": "arg", "position": 1, "required": True,
             "validator": "regex", "pattern": "[A-Za-z][A-Za-z0-9_-]{1,63}",
             "prompt_text": "Which backup_id?"},
            {"name": "backup_type", "source": "arg", "position": 2, "required": False,
             "validator": "regex", "pattern": "full|diff|log|-",
             "validation_error": "backup_type must be full, diff, log, or - to let the schedule decide",
             "prompt_text": "Level? full / diff / log - or Skip to let the schedule decide",
             "input_type": "choice", "allow_text_input": False, "allow_skip": True,
             "options": [{"label": "full", "value": "full"}, {"label": "diff", "value": "diff"},
                         {"label": "log", "value": "log"}]},
        ],
    },
}


class _Chat:
    def __init__(self, tmp_path, monkeypatch):
        self.sqlite_path = tmp_path / "runtime.sqlite"
        self.commands_path = tmp_path / "telegram_support_commands.json"
        self.commands_path.write_text(json.dumps({"telegram_support_commands": [COMMAND]}), encoding="utf-8")
        (tmp_path / "telegram_users.json").write_text(json.dumps(
            {"telegram_users": [{"user_id": USER, "user_type": 2, "status": "active"}]}), encoding="utf-8")
        (tmp_path / "telegram_groups.json").write_text(json.dumps({"telegram_groups": []}), encoding="utf-8")
        self.store = DbOpsStore(self.sqlite_path)
        self.next_id = 10
        self.ran: list[list[str]] = []

        def run(*, command, args, **_kw):
            self.ran.append(list(args))
            return {"status": "success", "reply": "started"}

        patch_telegram(monkeypatch, "execute_command_action", run)

    def _post(self, text):
        self.next_id += 1
        self.store.upsert_telegram_messages([{
            "update_id": self.next_id, "message_id": self.next_id,
            "message_date": 1_779_478_400 + self.next_id, "chat_id": USER, "chat_type": "private",
            "user_id": USER, "text": text, "raw": {"from": {"id": int(USER), "username": "op"}},
        }])

    def start(self, text):
        self._post(text)
        self.store.sync_telegram_command_messages(command_prefix="/spbot")
        with sqlite3.connect(self.sqlite_path) as connection:
            row = connection.execute("SELECT MAX(telegram_command_message_id) FROM telegram_command_messages").fetchone()
        process_one_command_message(sqlite_path=self.sqlite_path, telegram_command_message_id=int(row[0]),
                                    commands_path=self.commands_path)

    def say(self, text):
        self._post(text)
        process_pending_conversation_messages(sqlite_path=self.sqlite_path, commands_path=self.commands_path,
                                              delete_message=lambda *_a: None)

    def asked(self, question: str) -> int:
        with sqlite3.connect(self.sqlite_path) as connection:
            return sum(question in str(row[0]) for row in connection.execute(
                "SELECT message_text FROM telegram_send_messages"))


@pytest.fixture()
def chat(tmp_path, monkeypatch):
    return _Chat(tmp_path, monkeypatch)


@pytest.mark.parametrize("answer", ["skip", "Skip", "⏭️ Skip"])
def test_skip_on_the_level_runs_the_backup_and_lets_the_schedule_decide(chat, answer):
    chat.start("/spbot_backup LAB251_PG_WAL")
    assert chat.asked("Level?") == 1

    chat.say(answer)

    assert chat.asked("Level?") == 1, "the same question was asked again"
    assert chat.ran and chat.ran[-1][:2] == ["LAB251_PG_WAL", "-"]


def test_a_level_given_in_the_command_is_not_asked(chat):
    chat.start("/spbot_backup LAB251_MSSQL_LOG log")

    assert chat.asked("Level?") == 0
    assert chat.ran and chat.ran[-1][:2] == ["LAB251_MSSQL_LOG", "log"]


def test_a_level_chosen_from_the_buttons_runs_with_it(chat):
    chat.start("/spbot_backup LAB251_PG_WAL")
    chat.say("full")

    assert chat.ran and chat.ran[-1][:2] == ["LAB251_PG_WAL", "full"]
