"""An app's Telegram message and a shell's take one path from the request to the row.

Two doors queue a message: `db.queue_message.queue_message`, which every app calls in its own
process, and `db.cli queue-telegram-message`, for a script or a person at a shell. Until 0.25.0 each
chose the store and mapped the request's eleven fields itself, and the CLI derived the stored type a
second time to echo it - copies that only had to drift once for a script's "RESTORE FAILED" to be
stored differently from the app's. Both now run `db.telegram_queue.queue_from_request`, and these
tests hold them to one row.

Offline: the store is a SQLite file on the test's own root.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from db_ops.db import DbOpsStore
from db_ops.db import cli as db_cli
from db_ops.db.declaration import describe_store
from db_ops.db.queue_message import queue_message

#: What a row says about its message - everything but its own id and when it was written.
_COLUMNS = ("tlgchat_id", "message_text", "reply_message_id", "note", "source_type", "source_id",
            "message_type", "metadata_json", "send_status", "entities")

_REQUEST = {"chat_id": "-1001234567890", "text": "RESTORE FAILED on LAB", "level": "error",
            "phase": "END", "note": "the nightly drill", "source_type": "backup_restore",
            "source_id": "drill-1", "reply_message_id": 7, "metadata": {"run": 1}}


@pytest.fixture
def store(tmp_path) -> DbOpsStore:
    return DbOpsStore(tmp_path / "store.sqlite")


def _row(store: DbOpsStore, send_tlgmsg_id: int) -> dict:
    with sqlite3.connect(store.target.sqlite_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM telegram_send_messages WHERE send_tlgmsg_id = ?",
                                 (send_tlgmsg_id,)).fetchone()
    return {column: row[column] for column in _COLUMNS}


def _through_the_shell(capsys, request: dict) -> dict:
    capsys.readouterr()
    db_cli.main(["queue-telegram-message", json.dumps(request)])
    out = capsys.readouterr().out
    return json.loads(out[out.index("{"):])


def test_an_apps_message_and_a_shells_are_the_same_row(store, capsys):
    from_the_app = queue_message(dict(_REQUEST), fallback_store=store)
    answer = _through_the_shell(capsys, {**_REQUEST, "store": describe_store(store)})

    assert answer["success"] is True, answer
    from_the_shell = answer["data"]["send_tlgmsg_id"]
    assert from_the_app and from_the_shell and from_the_app != from_the_shell
    assert _row(store, from_the_app) == _row(store, from_the_shell)


def test_the_shell_echoes_the_type_it_stored(store, capsys):
    """A level and a phase in, the derived type out: `END` at `error` is a failure, never a success."""
    answer = _through_the_shell(capsys, {**_REQUEST, "store": describe_store(store)})
    assert answer["data"]["message_type"] == _row(store, answer["data"]["send_tlgmsg_id"])["message_type"]
    assert answer["data"]["message_type"] == "failed"


def test_a_message_with_no_chat_is_queued_by_neither_door(store, capsys):
    """The app is told on stderr and carries on - a push never fails the work it reports on."""
    assert queue_message({**_REQUEST, "chat_id": ""}, fallback_store=store) is None
    assert "chat_id and text are required" in capsys.readouterr().err

    answer = _through_the_shell(capsys, {**_REQUEST, "chat_id": "", "store": describe_store(store)})
    assert answer["success"] is False and "chat_id and text are required" in answer["error"]
