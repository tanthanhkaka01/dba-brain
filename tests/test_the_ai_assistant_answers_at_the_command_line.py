"""The AI assistant answers at the node's command line - `dbabrain telegram ai-ask` (1.267).

The guide tells the assistant how to walk a new install through its setup. Until 2026-11-06 it could
be asked only in Telegram, and Telegram is step 6 of that walk. The operator's test was: *a fresh
install, the AI key added, then ask the CLI what to set up next*. So the command line needs nothing
of Telegram, no users[] row and no chat level. It still keeps the question, every call and every
tool run as a chat question does, holds the system's limits, and continues the conversation.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

from tests.test_the_ai_assistant_answers_in_its_chat_and_keeps_everything import NOW, Model

from dbabrain.db.store import DbaBrainStore
from dbabrain.lib import ai_assist as A
from dbabrain.telegram import ai_assist as job


def _root(tmp_path, **changes):
    data = tmp_path / "data"
    data.mkdir()
    # A fresh install's file: no users, no chat level, no private chats - only switched on.
    document = A.shipped_document()
    document.update({"enabled": True, "api_key_ref": "KEY", "cooldown_seconds": 0, "telegram_chat": [],
                     "private_chat_enabled": False, **changes})
    document["users"] = []
    (data / "ai_assist.json").write_text(json.dumps(document), encoding="utf-8")
    DbaBrainStore(str(tmp_path / "store.sqlite")).initialize()
    return tmp_path


def _ask(root, question, *, model=None, now=NOW, new=False):
    printed: list[str] = []
    settings, missing = job.load_settings(root / "data", need_chat=False)
    assert settings is not None, missing
    config = SimpleNamespace(store=str(root / "store.sqlite"), timezone="+07:00", node_role="master",
                             telegram=SimpleNamespace(resolved_bot_token="", api_url=""))
    runner = job.Runner(config=config, config_path=root / "config.json", settings=settings, api_key="k",
                        secrets={"KEY": "sk-ant-secret-value"}, call=model or Model(), now=now,
                        deliver=printed.append)
    return runner.ask(question, asker="installer", new_conversation=new), printed


def _rows(root, sql):
    with sqlite3.connect(root / "store.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql).fetchall()]


def test_a_new_install_with_no_chat_is_answered_at_the_command_line(tmp_path):
    root = _root(tmp_path)
    # The job still has nothing to answer in; the command line does.
    assert job.load_settings(root / "data")[0] is None
    result, printed = _ask(root, "What do I set up next?")

    assert result["ok"] and result["ai_request_id"]
    assert printed and printed[0].startswith("Mọi thứ ổn.")      # the answer, then its cost footer
    request = _rows(root, "SELECT * FROM ai_requests")[0]
    assert request["chat_id"] == job.CLI_CHAT_ID and request["status"] == "done"
    assert "command line of this node" in request["context_text"]
    assert len(_rows(root, "SELECT * FROM ai_api_calls")) == 2
    assert len(_rows(root, "SELECT * FROM ai_tool_runs")) == 1
    # Nothing was queued for Telegram.
    assert _rows(root, "SELECT COUNT(*) AS n FROM telegram_send_messages")[0]["n"] == 0


def test_the_next_question_continues_the_conversation_unless_new_is_asked(tmp_path):
    root = _root(tmp_path)
    _ask(root, "Step one?")
    _ask(root, "And then?", now=NOW + timedelta(minutes=5))
    _ask(root, "Something else", now=NOW + timedelta(minutes=10), new=True)
    requests = _rows(root, "SELECT message_id, ai_conversation_id FROM ai_requests ORDER BY ai_request_id")
    assert [row["message_id"] for row in requests] == [1, 2, 3]
    assert requests[0]["ai_conversation_id"] == requests[1]["ai_conversation_id"]
    assert requests[2]["ai_conversation_id"] != requests[1]["ai_conversation_id"]


def test_the_system_budget_still_holds_at_the_command_line(tmp_path):
    root = _root(tmp_path, budget_usd_month=0)
    result, printed = _ask(root, "What next?")
    assert _rows(root, "SELECT status FROM ai_requests")[0]["status"] == "blocked"
    assert printed and "limit" in printed[0]


def test_an_unconfigured_node_says_what_is_missing(tmp_path):
    (tmp_path / "data").mkdir()
    result = job.ask_from_cli(config=SimpleNamespace(), config_path=tmp_path / "config.json", question="hi")
    assert result["skipped"] and "not configured" in result["reason"]
