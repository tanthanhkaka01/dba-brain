"""The console shows what the AI assistant was asked, what it answered and what it cost (0.35.0, 1.266).

0.34.0 kept every question, message, API call and tool run in five store tables, readable only with
``telegram.cli ai-usage`` and ``ai-show``; the operator asked for petweb's admin page in the console
(D-AI8). These hold what that page must be:

* an admin's page, like *Users* - the transcripts are what people asked, in their words, and what the
  tools read on the estate's servers;
* the usage table adds the rows up exactly as ``ai-usage`` does, because both read
  ``AiAssistStore.usage_since`` - two sums of one table that can disagree are worse than one;
* a refused question is listed, with why: it is the row an operator most often comes looking for;
* one request opens in full - the messages as the model took them, every call, every tool run;
* a console started without the assistant's store says so instead of failing - not configured is a
  state;
* the page and its link carry the assistant's name (0.38.0, 1.326) - the ``assistant_name`` the node's
  ``ai_assist.json`` states, the field the Telegram job answers under, so the console and the chat
  call it the same thing; a node that states none is shown what the page is, without a name.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import pytest

from conftest import write_catalogued_data

from dbabrain.db import config_sync
from dbabrain.db.ai_assist_store import AiAssistStore
from dbabrain.db.config_store import ConfigStore
from dbabrain.db.web_auth_store import WebAuthStore
from dbabrain.lib import ai_assist as ai_assist_rules
from dbabrain.lib import web_auth
from dbabrain.webhost.app import Request, WebApp, WebSettings

PASSWORD = "console-password-1"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def cheap_kdf(monkeypatch):
    monkeypatch.setattr(web_auth, "PBKDF2_ITERATIONS", 1000)


def _seed(store: AiAssistStore) -> int:
    """One answered question with a tool run, one refused question, one group word."""
    conversation = store.open_conversation(chat_id="-100", user_id="7", user_name="Lan", title="disk",
                                           model="claude-sonnet-5-5")
    asked = store.claim_request(
        chat_id="-100", message_id=11, user_id="7", user_name="Lan", kind="question", status="running",
        question="Which server is low on disk?", model="claude-sonnet-5-5", effort="medium",
        tools_mode="read_only", ai_conversation_id=conversation, started_at="2026-11-01T08:00:00Z")
    store.add_messages(ai_conversation_id=conversation, ai_request_id=asked, messages=[
        {"role": "user", "content": [{"type": "text", "text": "Which server is low on disk?"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "look at the store", "signature": "sig"},
            {"type": "tool_use", "id": "tu_1", "name": "store_query", "input": {"sql": "SELECT 1"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "1 row"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "SRV-A has 4 % free on D:."}]}])
    call = store.record_api_call(ai_request_id=asked, turn_no=1, model_requested="claude-sonnet-5-5",
                                 model_served="claude-sonnet-5-5", anthropic_request_id="req_abc",
                                 stop_reason="end_turn", input_tokens=1200, output_tokens=300,
                                 cost_usd=0.0081, duration_ms=2100, started_at="2026-11-01T08:00:01Z")
    store.record_tool_run(ai_request_id=asked, ai_api_call_id=call, tool_use_id="tu_1", tool_name="store_query",
                          input_json={"sql": "SELECT 1"}, output_text="1 row", output_chars=5, sent_chars=5,
                          row_count=1, duration_ms=40)
    store.set_request(asked, status="done", total_tokens=1500, input_tokens=1200, output_tokens=300,
                      cost_usd=0.0081, turns=1, tool_calls=1, stop_reason="end_turn",
                      answer_text="SRV-A has 4 % free on D:.", finished_at="2026-11-01T08:00:05Z")
    store.claim_request(chat_id="-100", message_id=12, user_id="9", user_name="Minh", kind="question",
                        status="blocked", reason="not switched on in ai_assist.json users[]",
                        question="hello?", started_at="2026-11-01T09:00:00Z")
    store.claim_request(chat_id="-100", message_id=13, user_id="7", user_name="Lan", kind="command",
                        status="done", question="/usage", started_at="2026-11-01T10:00:00Z")
    return asked


@pytest.fixture(scope="module")
def _template(tmp_path_factory):
    root = tmp_path_factory.mktemp("ai-template")
    data = write_catalogued_data(root / "data")
    store_path = root / "template.sqlite"
    config_sync.sync(ConfigStore(store_path), data_dir=data, actor="test")
    auth = WebAuthStore(store_path)
    auth.create_user(username="admin", password=PASSWORD, level=90)
    auth.create_user(username="viewer", password=PASSWORD, level=1)
    asked = _seed(AiAssistStore(store_path))
    checkpoint = sqlite3.connect(store_path)
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint.close()
    return store_path, data, asked


def _console(tmp_path: Path, template, *, with_ai: bool = True) -> WebApp:
    store_path = tmp_path / "dbabrain.sqlite"
    shutil.copy(template[0], store_path)
    data = tmp_path / "data"
    shutil.copytree(template[1], data)
    return WebApp(auth_store=WebAuthStore(store_path), config_store=ConfigStore(store_path),
                  ai_store=AiAssistStore(store_path) if with_ai else None, data_dir=data,
                  log_dir=tmp_path, settings=WebSettings(), now=lambda: NOW)


def _sign_in(console: WebApp, username: str) -> str:
    response = console.handle(Request(
        method="POST", path="/dbabrain/login",
        body=urlencode({"username": username, "password": PASSWORD}).encode("utf-8"),
        headers={"content-type": "application/x-www-form-urlencoded"}, client_ip="10.0.0.5"))
    assert response.status == 303
    return next(v for n, v in response.headers if n == "Set-Cookie").split(";")[0]


def _get(console: WebApp, path: str, cookie: str, **query: str):
    return console.handle(Request(method="GET", path=path, query={k: [v] for k, v in query.items()},
                                  headers={"cookie": cookie}))


def test_only_an_admin_opens_the_ai_page(tmp_path, _template):
    console = _console(tmp_path, _template)
    response = _get(console, "/dbabrain/ai", _sign_in(console, "viewer"))
    assert response.status == 403
    assert "needs level" in response.body.decode("utf-8")


def test_an_admin_sees_the_link_and_the_page(tmp_path, _template):
    console = _console(tmp_path, _template)
    cookie = _sign_in(console, "admin")
    assert 'href="/dbabrain/ai"' in _get(console, "/dbabrain/", cookie).body.decode("utf-8")
    response = _get(console, "/dbabrain/ai", cookie)
    assert response.status == 200
    page = response.body.decode("utf-8")
    assert "Which server is low on disk?" in page
    assert "not switched on in ai_assist.json users[]" in page, "a refusal is listed with why"
    assert "$0.0081" in page


def test_the_usage_table_adds_up_as_the_cli_does(tmp_path, _template):
    console = _console(tmp_path, _template)
    cookie = _sign_in(console, "admin")
    usage = AiAssistStore(tmp_path / "dbabrain.sqlite").usage_since(days=30, group_by="user", now=NOW)
    page = _get(console, "/dbabrain/ai", cookie, by="user", days="30").body.decode("utf-8")
    assert [row["bucket"] for row in usage["rows"]] == ["7 Lan", "9 Minh"]
    assert usage["total_tokens"] == 1500 and usage["total_cost_usd"] == pytest.approx(0.0081)
    for row in usage["rows"]:
        assert row["bucket"] in page
    # The group word is not a question: one question and one refusal are counted.
    assert sum(int(row["questions"]) for row in usage["rows"]) == 1
    assert sum(int(row["refused"]) for row in usage["rows"]) == 1


def test_a_request_opens_in_full(tmp_path, _template):
    console = _console(tmp_path, _template)
    cookie = _sign_in(console, "admin")
    page = _get(console, f"/dbabrain/ai/{_template[2]}", cookie).body.decode("utf-8")
    assert "SRV-A has 4 % free on D:." in page
    assert "store_query" in page and "req_abc" in page
    assert "thinking (17 characters)" in page, "thinking is kept, folded away"
    assert "ai-show --id" in page


def test_an_unknown_request_is_a_404_not_a_traceback(tmp_path, _template):
    console = _console(tmp_path, _template)
    cookie = _sign_in(console, "admin")
    assert _get(console, "/dbabrain/ai/999999", cookie).status == 404
    assert _get(console, "/dbabrain/ai/not-a-number", cookie).status == 404


def test_a_bad_query_falls_back_instead_of_failing(tmp_path, _template):
    console = _console(tmp_path, _template)
    cookie = _sign_in(console, "admin")
    response = _get(console, "/dbabrain/ai", cookie, days="lots", by="'; DROP", page="-3")
    assert response.status == 200
    assert "last 30 day(s)" in response.body.decode("utf-8")


def test_the_page_is_read_only(tmp_path, _template):
    console = _console(tmp_path, _template)
    cookie = _sign_in(console, "admin")
    response = console.handle(Request(method="POST", path="/dbabrain/ai", body=b"",
                                      headers={"cookie": cookie}))
    assert response.status == 405


def test_without_the_store_the_page_says_so(tmp_path, _template):
    console = _console(tmp_path, _template, with_ai=False)
    page = _get(console, "/dbabrain/ai", _sign_in(console, "admin"))
    assert page.status == 200
    assert "not open here" in page.body.decode("utf-8")


# --------------------------------------------------------------------------- #
# The name (0.38.0, 1.326)
# --------------------------------------------------------------------------- #
def _name_the_assistant(tmp_path: Path, name: str) -> None:
    document = {**ai_assist_rules.shipped_document(), "assistant_name": name}
    (tmp_path / "data" / "ai_assist.json").write_text(json.dumps(document), encoding="utf-8")


def test_the_page_and_its_link_carry_the_name_the_node_states(tmp_path, _template):
    console = _console(tmp_path, _template)
    cookie = _sign_in(console, "admin")
    _name_the_assistant(tmp_path, "Ada")
    page = _get(console, "/dbabrain/ai", cookie).body.decode("utf-8")
    assert "<title>Ada · AI assistant — dbabrain</title>" in page
    assert "<h2>Ada · AI assistant</h2>" in page
    assert "What Ada, the Telegram AI assistant, was asked" in page
    link = '<a class="item active" href="/dbabrain/ai">'
    assert link in page and '<span class="name">Ada · AI assistant</span>' in page[page.index(link):]
    # Every page's sidebar names it, and one request's page leads back to it by name.
    assert '<span class="name">Ada · AI assistant</span>' in _get(console, "/dbabrain/", cookie).body.decode("utf-8")
    request = _get(console, f"/dbabrain/ai/{_template[2]}", cookie).body.decode("utf-8")
    assert "&larr; Ada · AI assistant</a>" in request


def test_renaming_the_assistant_is_an_edit_of_the_field_and_nothing_else(tmp_path, _template):
    """No restart and no code: the console reads the field on each request."""
    console = _console(tmp_path, _template)
    cookie = _sign_in(console, "admin")
    _name_the_assistant(tmp_path, "Ada")
    assert "<h2>Ada · AI assistant</h2>" in _get(console, "/dbabrain/ai", cookie).body.decode("utf-8")
    _name_the_assistant(tmp_path, "Bao <the second>")
    page = _get(console, "/dbabrain/ai", cookie).body.decode("utf-8")
    assert "<h2>Bao &lt;the second&gt; · AI assistant</h2>" in page and "Ada" not in page


def test_a_node_that_states_no_name_is_shown_what_the_page_is(tmp_path, _template):
    """No `ai_assist.json`, an unreadable one, or one without the field: the assistant is not
    configured here. The page says what it is - there is no name in code to say instead."""
    console = _console(tmp_path, _template)
    cookie = _sign_in(console, "admin")
    shipped = ai_assist_rules.shipped_document()["assistant_name"]
    for write in (lambda: None,
                  lambda: (tmp_path / "data" / "ai_assist.json").write_text("{not json", encoding="utf-8"),
                  lambda: (tmp_path / "data" / "ai_assist.json").write_text('{"enabled": true}', encoding="utf-8"),
                  lambda: _name_the_assistant(tmp_path, "   ")):
        write()
        page = _get(console, "/dbabrain/ai", cookie).body.decode("utf-8")
        assert "<h2>AI assistant</h2>" in page and "<title>AI assistant — dbabrain</title>" in page
        assert "What the Telegram AI assistant was asked" in page
        assert shipped not in page, "the shipped name is a value in a file, never a default on a page"


def test_the_name_is_read_for_the_one_who_is_shown_the_page(tmp_path, _template):
    console = _console(tmp_path, _template)
    _name_the_assistant(tmp_path, "Ada")
    viewer = _get(console, "/dbabrain/", _sign_in(console, "viewer")).body.decode("utf-8")
    assert "Ada" not in viewer and 'href="/dbabrain/ai"' not in viewer
    assert console.assistant_name() == "Ada"
