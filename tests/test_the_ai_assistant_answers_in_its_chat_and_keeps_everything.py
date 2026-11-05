"""The AI assistant's job, end to end on a SQLite store with a stand-in model (0.34.0, 1.261).

The operator, 2026-10-31: *keep it carefully - what the user wrote, what the AI answered, which
dbabrain tools it used, the tokens and what they cost; a user is off unless switched on; refuse
duplicates.* So each case reads the rows back: the question, every call to the model, every tool
run, the conversation as the model takes it, and the reply queued for the bot - and a refused
message is a row too, with why.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.db.ai_assist_store import AiAssistStore
from dbabrain.db.store import DbaBrainStore
from dbabrain.lib import ai_assist as A
from dbabrain.telegram import ai_assist as job

CHAT = "-500"
BOT = "dbabrain_test_bot"
BOT_ID = "4242"
OPERATOR = "100"
NOW = datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)


class Model:
    """Asks for one store_query, then answers."""

    def __init__(self):
        self.calls = []

    def __call__(self, params):
        self.calls.append(params)
        if not any(block.get("type") == "tool_result" for block in params["messages"][-1]["content"]):
            return {"model": "claude-sonnet-5-5", "stop_reason": "tool_use", "request_id": "req_a",
                    "content": [{"type": "tool_use", "id": f"t{len(self.calls)}", "name": "store_query",
                                 "input": {"sql": "SELECT COUNT(*) AS n FROM telegram_messages"}}],
                    "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 3000}}
        return {"model": "claude-sonnet-5-5", "stop_reason": "end_turn", "request_id": "req_b",
                "content": [{"type": "text", "text": "Mọi thứ ổn."}],
                "usage": {"input_tokens": 200, "output_tokens": 50, "cache_read_input_tokens": 3000,
                          "cache_creation_input_tokens": 0}}


def _root(tmp_path: Path, **changes) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    document = A.shipped_document()
    document.update({"enabled": True, "api_key_ref": "KEY", "cooldown_seconds": 0,
                     "require_mention": False,
                     "max_message_age_seconds": 10 ** 9, **changes})
    document["users"] = [A.shipped_user(OPERATOR, "Op", active=True), A.shipped_user("300", "Off", active=False)]
    (data / "ai_assist.json").write_text(json.dumps(document), encoding="utf-8")
    (data / "telegram_users.json").write_text(json.dumps({"telegram_users": [
        {"user_id": OPERATOR, "user_type": 100, "active": True}]}), encoding="utf-8")
    (data / "telegram_support_commands.json").write_text('{"telegram_support_commands": []}', encoding="utf-8")
    DbaBrainStore(str(tmp_path / "store.sqlite")).initialize()
    return tmp_path


def _message(root: Path, message_id: int, user_id: str, text: str, **extra) -> None:
    raw = {"message_id": message_id, "date": int(NOW.timestamp()), "chat": {"id": int(CHAT), "title": "AI"},
           "from": {"id": int(user_id), "first_name": "U" + user_id, "is_bot": extra.pop("is_bot", False)},
           "text": text, **extra}
    with sqlite3.connect(root / "store.sqlite") as conn:
        conn.execute("INSERT INTO telegram_messages (message_id, message_date, chat_id, chat_type, user_id, text, "
                     "raw_json) VALUES (?, ?, ?, 'supergroup', ?, ?, ?)",
                     (message_id, int(NOW.timestamp()), CHAT, user_id, text, json.dumps(raw)))


def _run(root: Path, model=None, now=NOW):
    settings, missing = job.load_settings(root / "data")
    assert settings is not None, missing
    config = SimpleNamespace(store=str(root / "store.sqlite"), timezone="+07:00", node_role="worker",
                             telegram=SimpleNamespace(resolved_bot_token="", api_url="", telegram_bot_id=BOT_ID,
                                                      telegram_bot_username=BOT,
                                                      level_chat_map={"ai_assist": CHAT, "sql": "-999"}))
    runner = job.Runner(config=config, config_path=root / "config.json", settings=settings, api_key="k",
                        secrets={"KEY": "sk-ant-secret-value"}, call=model or Model(), now=now)
    return runner.run()


def _rows(root: Path, sql: str):
    with sqlite3.connect(root / "store.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql).fetchall()]


def test_a_question_is_answered_and_every_step_is_kept(tmp_path):
    root = _root(tmp_path)
    _message(root, 1, OPERATOR, "Hôm nay backup thế nào?")
    model = Model()
    counts = _run(root, model)
    assert counts["answered"] == 1
    request = _rows(root, "SELECT * FROM ai_requests")[0]
    assert request["status"] == "done" and request["question"] == "Hôm nay backup thế nào?"
    assert request["answer_text"] == "Mọi thứ ổn."
    assert request["turns"] == 2 and request["tool_calls"] == 1
    assert request["total_tokens"] == 1000 + 100 + 3000 + 200 + 50 + 3000
    assert request["cost_usd"] > 0
    calls = _rows(root, "SELECT * FROM ai_api_calls ORDER BY turn_no")
    assert [call["anthropic_request_id"] for call in calls] == ["req_a", "req_b"]
    tool = _rows(root, "SELECT * FROM ai_tool_runs")[0]
    assert tool["tool_name"] == "store_query" and tool["is_error"] == 0 and tool["ai_api_call_id"] == calls[0]["ai_api_call_id"]
    assert [row["role"] for row in _rows(root, "SELECT role FROM ai_messages ORDER BY seq")] == [
        "user", "assistant", "user", "assistant"]
    sent = _rows(root, "SELECT * FROM telegram_send_messages ORDER BY send_tlgmsg_id")
    assert sent[0]["message_text"].startswith("Working on it")
    answer = sent[-1]
    assert answer["source_type"] == "ai_requests" and answer["source_id"] == str(request["ai_request_id"])
    assert answer["reply_message_id"] == 1 and "Mọi thứ ổn." in answer["message_text"]
    assert "store_query" in answer["message_text"], "the cost footer names the tools"
    # The month shown is what was spent - the question's held cap is not counted on top of it.
    assert f"month ${request['cost_usd']:.4f}/$5.00" in answer["message_text"]
    # The question carries its context; the system prompt carries no time, so it caches.
    first = model.calls[0]
    assert "<dbabrain_context>" in first["messages"][-1]["content"][-1]["text"]
    assert "2026-10-31" not in first["system"]


def test_a_message_is_answered_once_whatever_runs_it_twice(tmp_path):
    root = _root(tmp_path)
    _message(root, 1, OPERATOR, "x?")
    _run(root)
    assert _run(root)["read"] == 0
    assert len(_rows(root, "SELECT * FROM ai_requests")) == 1


def test_a_user_who_is_not_switched_on_is_refused_by_name_and_kept(tmp_path):
    root = _root(tmp_path)
    _message(root, 1, "300", "hello")
    _message(root, 2, "999", "hello")
    model = Model()
    _run(root, model)
    assert model.calls == []
    rows = _rows(root, "SELECT user_id, status, reason FROM ai_requests ORDER BY message_id")
    assert [row["status"] for row in rows] == ["blocked", "blocked"]
    replies = [row["message_text"] for row in _rows(root, "SELECT message_text FROM telegram_send_messages")]
    assert any("telegram_user_id 999" in text and '"active": true' in text for text in replies)


def test_the_same_question_again_is_a_duplicate_and_costs_nothing(tmp_path):
    root = _root(tmp_path)
    _message(root, 1, OPERATOR, "Backup status?")
    _message(root, 2, OPERATOR, "backup   STATUS?")
    model = Model()
    _run(root, model)
    rows = _rows(root, "SELECT status, reason FROM ai_requests ORDER BY message_id")
    assert rows[0]["status"] == "done"
    assert rows[1]["status"] == "blocked" and rows[1]["reason"].startswith("duplicate")
    assert len(model.calls) == 2, "only the first question reached the model"


def test_a_question_over_the_users_budget_is_refused_before_any_call(tmp_path):
    root = _root(tmp_path)
    store = AiAssistStore(str(root / "store.sqlite"))
    spent = store.claim_request(chat_id=CHAT, message_id=99, user_id=OPERATOR, kind="question", status="running",
                                started_at="2026-10-30T00:00:00Z")
    store.set_request(spent, status="done", cost_usd=5.0)
    _message(root, 1, OPERATOR, "one more?")
    model = Model()
    _run(root, model)
    row = _rows(root, "SELECT status, reason FROM ai_requests WHERE message_id = 1")[0]
    assert row["status"] == "blocked" and row["reason"].startswith("budget_month")
    assert model.calls == []


def test_a_reply_to_an_answer_continues_its_conversation_and_new_starts_over(tmp_path):
    root = _root(tmp_path)
    _message(root, 1, OPERATOR, "first?")
    _run(root)
    answer = _rows(root, "SELECT send_tlgmsg_id FROM telegram_send_messages ORDER BY send_tlgmsg_id DESC")[0]
    with sqlite3.connect(root / "store.sqlite") as conn:
        conn.execute("UPDATE telegram_send_messages SET message_id = 500 WHERE send_tlgmsg_id = ?",
                     (answer["send_tlgmsg_id"],))
    _message(root, 2, OPERATOR, "and then?", reply_to_message={"message_id": 500, "date": int(NOW.timestamp()),
                                                               "text": "Mọi thứ ổn.", "from": {"first_name": "bot"}})
    model = Model()
    _run(root, model)
    rows = _rows(root, "SELECT ai_conversation_id FROM ai_requests WHERE kind = 'question' ORDER BY message_id")
    assert rows[0]["ai_conversation_id"] == rows[1]["ai_conversation_id"]
    assert len(model.calls[0]["messages"]) == 5, "the earlier question and its answers went with it"
    assert "replied_to" in model.calls[0]["messages"][-1]["content"][-1]["text"]
    _message(root, 3, OPERATOR, "/new")
    _message(root, 4, OPERATOR, "fresh?")
    _run(root)
    rows = _rows(root, "SELECT ai_conversation_id FROM ai_requests WHERE kind = 'question' ORDER BY message_id")
    assert rows[2]["ai_conversation_id"] != rows[0]["ai_conversation_id"]


def test_usage_answers_with_every_limit(tmp_path):
    root = _root(tmp_path)
    _message(root, 1, OPERATOR, "/usage@some_bot")
    _run(root)
    text = _rows(root, "SELECT message_text FROM telegram_send_messages")[0]["message_text"]
    assert "System - month: $0.0000 of $10.00" in text and "Op - month: $0.0000 of $5.00" in text


def test_a_bot_command_and_a_service_message_are_left_to_others(tmp_path):
    root = _root(tmp_path)
    _message(root, 1, OPERATOR, "/spbot_status")
    _message(root, 2, OPERATOR, "")
    _message(root, 3, "777", "an alert", is_bot=True)
    model = Model()
    _run(root, model)
    assert model.calls == []
    assert [row["status"] for row in _rows(root, "SELECT status FROM ai_requests")] == ["stopped"] * 3
    assert _rows(root, "SELECT * FROM telegram_send_messages") == []


def test_a_secret_is_never_kept_or_sent(tmp_path):
    root = _root(tmp_path)

    class Leaky(Model):
        def __call__(self, params):
            self.calls.append(params)
            return {"model": "claude-sonnet-5-5", "stop_reason": "end_turn", "content": [
                {"type": "text", "text": "the key is sk-ant-secret-value"}], "usage": {}}

    _message(root, 1, OPERATOR, "what is the key?")
    _run(root, Leaky())
    assert "sk-ant-secret-value" not in _rows(root, "SELECT answer_text FROM ai_requests")[0]["answer_text"]
    assert all("sk-ant-secret-value" not in row["message_text"]
               for row in _rows(root, "SELECT message_text FROM telegram_send_messages"))


def test_not_configured_is_a_state_not_an_error(tmp_path):
    config = SimpleNamespace(store=str(tmp_path / "s.sqlite"))
    (tmp_path / "data").mkdir()
    assert job.run_ai_assist(config=config, config_path=tmp_path / "config.json")["skipped"] is True
    document = A.shipped_document()
    (tmp_path / "data" / "ai_assist.json").write_text(json.dumps(document), encoding="utf-8")
    answer = job.run_ai_assist(config=config, config_path=tmp_path / "config.json")
    assert answer["skipped"] is True and "enabled is false" in answer["reason"]


def test_an_answer_longer_than_one_message_is_cut_at_a_line():
    parts = job.chunks("line\n" * 2000, 3900)
    assert all(len(part) <= 3900 for part in parts) and len(parts) > 1


# --------------------------------------------------------------------------- #
# Found by the read-only review of 2026-10-31 (Gemini), each one checked and held here.
# --------------------------------------------------------------------------- #
def test_a_secret_the_user_pastes_is_redacted_before_it_is_kept_or_sent(tmp_path):
    root = _root(tmp_path)
    _message(root, 1, OPERATOR, "is sk-ant-secret-value still valid?")
    model = Model()
    _run(root, model)
    assert "sk-ant-secret-value" not in _rows(root, "SELECT question FROM ai_requests")[0]["question"]
    assert "sk-ant-secret-value" not in json.dumps(model.calls[0]["messages"])
    assert "sk-ant-secret-value" not in json.dumps(_rows(root, "SELECT content_json FROM ai_messages"))


def test_the_first_question_of_the_day_is_not_counted_against_itself(tmp_path):
    root = _root(tmp_path, requests_day=1)
    _message(root, 1, OPERATOR, "first?")
    _message(root, 2, OPERATOR, "second?")
    _run(root)
    rows = _rows(root, "SELECT status, reason FROM ai_requests ORDER BY message_id")
    assert rows[0]["status"] == "done"
    assert rows[1]["status"] == "blocked" and rows[1]["reason"].startswith("requests_day")


def test_a_message_that_fails_is_closed_and_answered_and_the_run_goes_on(tmp_path):
    root = _root(tmp_path)
    _message(root, 1, OPERATOR, "first?")
    _message(root, 2, OPERATOR, "second?")

    class Breaks(Model):
        def __call__(self, params):
            if "first?" in json.dumps(params["messages"][-1]):
                raise KeyError("boom")
            return super().__call__(params)

    original = job.Runner._conversation

    def conversation(self, **kwargs):
        if kwargs["text"] == "first?":
            raise RuntimeError("history unreadable")
        return original(self, **kwargs)

    job.Runner._conversation = conversation
    try:
        counts = _run(root, Breaks())
    finally:
        job.Runner._conversation = original
    rows = _rows(root, "SELECT message_id, status, reason FROM ai_requests ORDER BY message_id")
    assert rows[0]["status"] == "error" and "history unreadable" in rows[0]["reason"]
    assert rows[1]["status"] == "done", "the next message was still answered"
    assert counts["errors"] == 1
    replies = [row["message_text"] for row in _rows(root, "SELECT message_text FROM telegram_send_messages")]
    assert any("The assistant failed: RuntimeError: history unreadable" in text for text in replies)


# --------------------------------------------------------------------------- #
# Answer only when addressed (the operator, 2026-10-31: run the AI only when the bot is @mentioned)
# --------------------------------------------------------------------------- #
def test_a_message_without_the_bots_mention_is_neither_answered_nor_kept(tmp_path):
    root = _root(tmp_path, require_mention=True)
    _message(root, 1, OPERATOR, "anh ơi chiều nay họp không?")
    _message(root, 2, "999", "ok anh")
    model = Model()
    _run(root, model)
    assert model.calls == []
    rows = _rows(root, "SELECT status, question, reason FROM ai_requests ORDER BY message_id")
    assert [row["status"] for row in rows] == ["stopped", "stopped"]
    assert all(row["question"] == "" for row in rows), "a chat between people is not kept"
    assert "no @mention" in rows[0]["reason"]
    assert _rows(root, "SELECT * FROM telegram_send_messages") == [], "nobody is told they are not switched on"


def test_a_mention_is_answered_without_the_mention_in_the_question(tmp_path):
    root = _root(tmp_path, require_mention=True)
    _message(root, 1, OPERATOR, f"@{BOT.upper()}  backup hôm nay ổn không?")
    model = Model()
    _run(root, model)
    row = _rows(root, "SELECT status, question FROM ai_requests")[0]
    assert row["status"] == "done" and row["question"] == "backup hôm nay ổn không?"
    assert "@" not in model.calls[0]["messages"][-1]["content"][-1]["text"].split("<dbabrain_context>")[0]


def test_a_reply_to_the_assistant_needs_no_mention(tmp_path):
    root = _root(tmp_path, require_mention=True)
    _message(root, 1, OPERATOR, "and the log backups?",
             reply_to_message={"message_id": 77, "date": int(NOW.timestamp()), "text": "...",
                               "from": {"id": int(BOT_ID), "is_bot": True, "username": BOT}})
    _run(root)
    assert _rows(root, "SELECT status FROM ai_requests")[0]["status"] == "done"


def test_a_group_word_and_a_bare_mention_are_answered(tmp_path):
    root = _root(tmp_path, require_mention=True)
    _message(root, 1, OPERATOR, "/usage")
    _message(root, 2, OPERATOR, f"@{BOT}")
    _run(root)
    texts = [row["message_text"] for row in _rows(root, "SELECT message_text FROM telegram_send_messages")]
    assert any("System - month" in text for text in texts)
    assert any("@mention the bot" in text for text in texts), "a bare mention answers with the help"


def test_a_node_that_does_not_know_its_bot_says_so(tmp_path):
    root = _root(tmp_path, require_mention=True)
    settings, _ = job.load_settings(root / "data")
    config = SimpleNamespace(store=str(root / "store.sqlite"), timezone="+07:00", node_role="worker",
                             telegram=SimpleNamespace(resolved_bot_token="", api_url="", telegram_bot_id="",
                                                      telegram_bot_username=""))
    runner = job.Runner(config=config, config_path=root / "config.json", settings=settings, api_key="k",
                        secrets={}, call=Model(), now=NOW)
    answer = runner.run()
    assert answer["skipped"] is True and "telegram_bot_username" in answer["reason"]



# --------------------------------------------------------------------------- #
# Preserved thinking (2026-10-31: the first question after a deploy was refused with
# "Invalid `signature` in `thinking` block. The block is bound to a different conversation.")
# --------------------------------------------------------------------------- #
def test_every_call_asks_the_api_to_drop_a_stale_thinking_block_rather_than_refuse(monkeypatch):
    anthropic = pytest.importorskip("anthropic")  # the `ai` extra

    sent = {}

    class Beta:
        class messages:  # noqa: N801 - the SDK's shape
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)
                return SimpleNamespace(to_dict=lambda: {"content": [], "stop_reason": "end_turn"}, _request_id="r")

    class Client:
        def __init__(self, **_options):
            self.beta = Beta

    monkeypatch.setattr(anthropic, "Anthropic", Client)
    settings = A.parse_ai_assist({**A.shipped_document(), "users": []})
    job.sdk_call(settings, "k")({"model": "m", "max_tokens": 10, "messages": []})
    assert sent["betas"] == ["thinking-binding-controls-2026-08-01"]
    assert sent["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}


def test_the_system_prompt_carries_nothing_a_deploy_changes():
    """It is part of every thinking block's binding and of the cached prefix: the version, like
    the time, goes in the question's context."""
    from dbabrain.lib.version import __version__

    settings = A.parse_ai_assist({**A.shipped_document(), "users": []})
    system = job.system_prompt(settings, node={"role": "worker", "backend": "postgresql", "timezone": "+07:00"})
    assert __version__ not in system


# --------------------------------------------------------------------------- #
# The chat is a level, as every app's telegram_chat is (the operator, 2026-10-31)
# --------------------------------------------------------------------------- #
def test_the_assistant_answers_in_the_chat_its_level_resolves_to_and_nowhere_else(tmp_path):
    root = _root(tmp_path)
    _message(root, 1, OPERATOR, "in the assistant's chat?")
    with sqlite3.connect(root / "store.sqlite") as conn:
        raw = {"message_id": 5, "date": int(NOW.timestamp()), "chat": {"id": -999, "title": "SQL"},
               "from": {"id": int(OPERATOR), "first_name": "Op"}, "text": "in the sql chat"}
        conn.execute("INSERT INTO telegram_messages (message_id, message_date, chat_id, chat_type, user_id, text, "
                     "raw_json) VALUES (5, ?, '-999', 'supergroup', ?, 'in the sql chat', ?)",
                     (int(NOW.timestamp()), OPERATOR, json.dumps(raw)))
    answer = _run(root)
    assert answer["chat_ids"] == [CHAT]
    assert [row["chat_id"] for row in _rows(root, "SELECT chat_id FROM ai_requests")] == [CHAT]


def test_a_level_with_no_chat_is_a_state_that_names_the_command(tmp_path):
    root = _root(tmp_path, telegram_chat="nobody_has_this")
    answer = _run(root)
    assert answer["skipped"] is True
    assert "group-level" in answer["reason"] and "nobody_has_this" in answer["reason"]


def test_the_answer_is_in_the_questions_language_unless_one_is_configured():
    """2026-10-31: a question in English was answered in Vietnamese - the rule said *usually
    Vietnamese*, and the conversation before it was Vietnamese."""
    node = {"role": "worker", "backend": "postgresql", "timezone": "+07:00"}
    auto = A.parse_ai_assist({**A.shipped_document(), "users": []})
    system = job.system_prompt(auto, node=node)
    assert "LATEST question" in system and "usually Vietnamese" not in system
    fixed = A.parse_ai_assist({**A.shipped_document(), "users": [], "reply_language": "English"})
    assert "always answer in English" in job.system_prompt(fixed, node=node)



def test_the_language_is_also_said_next_to_the_question(tmp_path):
    """The system prompt's rule alone lost to a long Vietnamese conversation on the real API; the
    same rule in the question's own context block won."""
    root = _root(tmp_path)
    _message(root, 1, OPERATOR, "sql tasks ok?")
    model = Model()
    _run(root, model)
    question = model.calls[0]["messages"][-1]["content"][-1]["text"]
    assert "answer_language: the language of the question above" in question



# --------------------------------------------------------------------------- #
# One level or several, and the users' private chats (the operator, 2026-11-04: *telegram_chat sets
# only one group - let it take one or many, and support private chat for the users switched on*)
# --------------------------------------------------------------------------- #
def _in_chat(root: Path, chat_id: str, message_id: int, user_id: str, text: str, *, chat_type: str) -> None:
    raw = {"message_id": message_id, "date": int(NOW.timestamp()), "chat": {"id": int(chat_id), "type": chat_type},
           "from": {"id": int(user_id), "first_name": "U" + user_id, "is_bot": False}, "text": text}
    with sqlite3.connect(root / "store.sqlite") as conn:
        conn.execute("INSERT INTO telegram_messages (message_id, message_date, chat_id, chat_type, user_id, text, "
                     "raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (message_id, int(NOW.timestamp()), chat_id, chat_type, user_id, text, json.dumps(raw)))


def test_the_assistant_answers_in_every_group_its_levels_resolve_to(tmp_path):
    root = _root(tmp_path, telegram_chat=["ai_assist", "SQL", "ai_assist"])
    _message(root, 1, OPERATOR, "in the assistant's chat?")
    _in_chat(root, "-999", 5, OPERATOR, "in the sql chat?", chat_type="supergroup")
    answer = _run(root)
    assert answer["chat_ids"] == [CHAT, "-999"], "each level once, lower case, in the order written"
    done = _rows(root, "SELECT chat_id FROM ai_requests WHERE status = 'done'")
    assert sorted(row["chat_id"] for row in done) == ["-500", "-999"]
    replies = _rows(root, "SELECT tlgchat_id FROM telegram_send_messages")
    assert {row["tlgchat_id"] for row in replies} == {"-500", "-999"}, "each answer goes back to its own chat"


def test_a_level_without_a_chat_is_named_and_the_others_still_answer(tmp_path):
    root = _root(tmp_path, telegram_chat=["nobody_has_this", "ai_assist"])
    _message(root, 1, OPERATOR, "still answered?")
    answer = _run(root)
    assert answer["levels_without_chat"] == ["nobody_has_this"]
    assert answer["answered"] == 1


def test_an_active_users_private_chat_is_answered_without_a_mention(tmp_path):
    root = _root(tmp_path, require_mention=True, private_chat_enabled=True)
    _in_chat(root, OPERATOR, 1, OPERATOR, "backup hôm nay ổn không?", chat_type="private")
    _in_chat(root, OPERATOR, 2, OPERATOR, "/help", chat_type="private")
    answer = _run(root)
    assert answer["private_chats"] == 1 and answer["answered"] == 1
    rows = _rows(root, "SELECT chat_id, status FROM ai_requests ORDER BY message_id")
    assert [(row["chat_id"], row["status"]) for row in rows] == [(OPERATOR, "done"), (OPERATOR, "done")]
    texts = [row["message_text"] for row in _rows(root, "SELECT message_text FROM telegram_send_messages")]
    assert any("write a question here" in text for text in texts), "the help is the private chat's"


def test_a_private_chat_is_not_read_while_private_chat_is_off(tmp_path):
    root = _root(tmp_path)
    _in_chat(root, OPERATOR, 1, OPERATOR, "hello?", chat_type="private")
    model = Model()
    answer = _run(root, model)
    assert answer["read"] == 0 and answer["private_chats"] == 0 and model.calls == []


def test_nobody_elses_private_chat_is_read(tmp_path):
    """A user who is off, or a stranger, writing to the bot privately is the bot's business - the
    assistant neither answers nor refuses nor keeps it."""
    root = _root(tmp_path, private_chat_enabled=True)
    _in_chat(root, "300", 1, "300", "hello?", chat_type="private")
    _in_chat(root, "999", 2, "999", "hello?", chat_type="private")
    model = Model()
    answer = _run(root, model)
    assert answer["read"] == 0 and model.calls == []
    assert _rows(root, "SELECT * FROM ai_requests") == []
    assert _rows(root, "SELECT * FROM telegram_send_messages") == []


@pytest.mark.parametrize("held", ["waiting", "consumed", "step"])
def test_an_answer_to_a_bot_command_never_reaches_the_model(tmp_path, held):
    """A bot command asking for a value - a password, often - takes the user's next message. In a
    private chat every message is the assistant's, so that one must be recognised and left alone."""
    root = _root(tmp_path, private_chat_enabled=True)
    _in_chat(root, OPERATOR, 10, OPERATOR, "S3cr3t!pass", chat_type="private")
    telegram_message_id = _rows(root, "SELECT telegram_message_id FROM telegram_messages")[0]["telegram_message_id"]
    with sqlite3.connect(root / "store.sqlite") as conn:
        if held == "step":
            conn.execute("INSERT INTO telegram_workflow_steps (run_key, chat_id, user_id, command_id, command_text, "
                         "step_no, parameter_name, parameter_position, status, answer_telegram_message_id) "
                         "VALUES ('r1', ?, ?, 1, '/spbot_x', 1, 'password', 1, 'answered', ?)",
                         (OPERATOR, OPERATOR, telegram_message_id))
        else:
            conn.execute("INSERT INTO telegram_conversation_states (chat_id, user_id, command_id, command_text, "
                         "state_key, status, wait_after_message_id, consumed_telegram_message_id) "
                         "VALUES (?, ?, 1, '/spbot_x', 'password', ?, 9, ?)",
                         (OPERATOR, OPERATOR, "waiting" if held == "waiting" else "done",
                          None if held == "waiting" else telegram_message_id))
    model = Model()
    _run(root, model)
    assert model.calls == []
    row = _rows(root, "SELECT status, question, reason FROM ai_requests")[0]
    assert row["status"] == "stopped" and row["question"] == "", "the answer's text is not kept"
    assert "bot command that is asking" in row["reason"]
    assert _rows(root, "SELECT * FROM telegram_send_messages") == []


def test_private_chats_alone_are_a_configuration_and_nothing_at_all_is_not(tmp_path):
    root = _root(tmp_path, telegram_chat=[], private_chat_enabled=True)
    _in_chat(root, OPERATOR, 1, OPERATOR, "x?", chat_type="private")
    assert _run(root)["answered"] == 1
    data = tmp_path / "off"
    data.mkdir()
    document = {**A.shipped_document(), "enabled": True, "api_key_ref": "K", "telegram_chat": []}
    (data / "ai_assist.json").write_text(json.dumps(document), encoding="utf-8")
    settings, missing = job.load_settings(data)
    assert settings is None and "private_chat_enabled is false" in missing


def test_telegram_chat_takes_a_level_or_a_list_of_levels_and_nothing_else():
    def parsed(value):
        return A.parse_ai_assist({**A.shipped_document(), "users": [], "telegram_chat": value})

    assert parsed("ai_assist").chat_levels == ("ai_assist",)
    assert parsed([" AI_Assist ", "sql", "ai_assist", ""]).chat_levels == ("ai_assist", "sql")
    for wrong in (5, ["ai_assist", 5], {"level": "ai_assist"}):
        with pytest.raises(A.AiAssistConfigError, match="a level or a list of levels"):
            parsed(wrong)


def test_check_references_follows_every_level_of_a_list(tmp_path):
    from dbabrain.lib import config_references

    data = tmp_path / "data"
    data.mkdir()
    document = {**A.shipped_document(), "enabled": True, "telegram_chat": ["ai_assist", "nobody_has_this"]}
    (data / "ai_assist.json").write_text(json.dumps(document), encoding="utf-8")
    (data / "telegram_groups.json").write_text(json.dumps({"telegram_groups": [
        {"group_id": "-500", "notify_level": "ai_assist", "active": True}]}), encoding="utf-8")
    (data / "telegram_config.json").write_text(json.dumps({"level_chat_map": {}}), encoding="utf-8")
    found = [item for item in config_references.check(data)["dangling"] if item["where"].startswith("ai_assist.json")]
    assert [item["value"] for item in found] == ["nobody_has_this"]


def test_the_reference_takes_a_list_of_levels_and_holds_each_to_a_string():
    from dbabrain.lib.shared_objects import check_value

    constraint = {"kind": "string", "nullable": False, "allow_empty": True, "also_accepts": ["array"]}
    assert check_value(constraint, ["ai_assist", "sql"], where="ai_assist.json", field="telegram_chat") == []
    assert check_value(constraint, "ai_assist", where="ai_assist.json", field="telegram_chat") == []
    problems = check_value(constraint, ["ai_assist", {"x": 1}], where="ai_assist.json", field="telegram_chat")
    assert [item["field"] for item in problems] == ["telegram_chat[1]"]


def test_a_step_a_command_left_half_answered_does_not_hold_the_chat_for_ever(tmp_path):
    """2026-11-04, the first private question on the worker: two workflow steps abandoned in
    October were still 'active', no conversation state was waiting, and every message in that
    chat was taken for an answer to them - the question was never asked."""
    root = _root(tmp_path, private_chat_enabled=True)
    _in_chat(root, OPERATOR, 10, OPERATOR, "server 100.250 ổn không?", chat_type="private")
    with sqlite3.connect(root / "store.sqlite") as conn:
        conn.execute("INSERT INTO telegram_workflow_steps (run_key, chat_id, user_id, command_id, command_text, "
                     "step_no, parameter_name, parameter_position, status) "
                     "VALUES ('r1', ?, ?, 1, '/spbot_x', 3, 'confirm', 3, 'active')", (OPERATOR, OPERATOR))
        conn.execute("INSERT INTO telegram_conversation_states (chat_id, user_id, command_id, command_text, "
                     "state_key, status, wait_after_message_id) VALUES (?, ?, 1, '/spbot_x', 'confirm', 'done', 9)",
                     (OPERATOR, OPERATOR))
    assert _run(root)["answered"] == 1
