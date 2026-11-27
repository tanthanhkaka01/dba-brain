"""A message outlives Telegram being unreachable for a moment.

Read on the worker on 2026-11-26: in 14 days the bot's run failed 835 times because the road to Telegram failed -
its gateway answered HTTP 502 (484), a read timed out (184), a connection was reset (95), a TLS handshake timed out
(61). Each failure was the whole run's error, so the daemon waited its retry interval before the bot listened again;
and in 30 days **69 queued messages** - alerts and replies - were spent on three immediate retries against the same
broken road and marked failed for good. Two of those failures were not even wrapped: a read timing out and a
connection closed without a response came out of ``response.read()`` as bare errors.

Now the road failing is ``TelegramUnreachable``: the row goes back to the queue with what it had delivered, the pass
stops starting rows, the updates not fetched are fetched next pass, and the run ends as a state, not an error. A
refusal (a 400) is still a refusal.
"""

from __future__ import annotations

import http.client
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from urllib import error as urllib_error

import pytest

from dbabrain.db import DbaBrainStore
from dbabrain.lib import errors
from dbabrain.telegram import api, send_queue, workflow


class _Body:
    def __init__(self, text: str) -> None:
        self._text = text.encode("utf-8")

    def read(self, *args):
        return self._text

    def close(self):
        pass


def _http(code: int, body: str = '{"ok": false}'):
    def fail(*args, **kwargs):
        raise urllib_error.HTTPError("https://api.telegram.org", code, "err", {}, _Body(body))
    return fail


def _raises(exc: BaseException):
    def fail(*args, **kwargs):
        raise exc
    return fail


class _Response:
    """What urlopen hands back - whose read fails the way the road does."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        raise self._exc


@pytest.mark.parametrize("opener", [
    _http(502, '{"ok":false,"error_code":502,"description":"Bad Gateway"}'),
    _http(503),
    _raises(urllib_error.URLError("timed out")),
    lambda *a, **k: _Response(TimeoutError("The read operation timed out")),
    lambda *a, **k: _Response(http.client.RemoteDisconnected("Remote end closed connection without response")),
    lambda *a, **k: _Response(ConnectionResetError(104, "Connection reset by peer")),
], ids=["502", "503", "url-error", "read-timeout", "remote-closed", "reset"])
def test_the_road_failing_is_telegram_unreachable(monkeypatch, opener):
    monkeypatch.setattr(api.request, "urlopen", opener)
    with pytest.raises(api.TelegramUnreachable) as caught:
        api.call_telegram_api(bot_token="t", method_name="sendMessage", payload={})
    assert isinstance(caught.value, errors.Unreachable)


def test_a_refusal_is_not_mistaken_for_the_road(monkeypatch):
    monkeypatch.setattr(api.request, "urlopen", _http(400, '{"description": "message is too long"}'))
    with pytest.raises(errors.OperationFailed) as caught:
        api.call_telegram_api(bot_token="t", method_name="sendMessage", payload={})
    assert not isinstance(caught.value, api.TelegramUnreachable)


def test_a_rate_limit_is_still_a_rate_limit(monkeypatch):
    monkeypatch.setattr(api.request, "urlopen", _http(429, '{"parameters": {"retry_after": 4}}'))
    with pytest.raises(api.TelegramRateLimited):
        api.call_telegram_api(bot_token="t", method_name="sendMessage", payload={})


def _queue_one(tmp_path) -> tuple[DbaBrainStore, int]:
    store = DbaBrainStore(tmp_path / "store.sqlite3")
    store.initialize()
    with store.connect() as conn:
        conn.execute("INSERT INTO telegram_send_messages (tlgchat_id, message_text, send_status, metadata_json) "
                     "VALUES (?, ?, 0, ?)", ("-100", "CRITICAL|host01|disk at 99%", json.dumps({"level": "critical"})))
        row_id = conn.execute("SELECT max(send_tlgmsg_id) FROM telegram_send_messages").fetchone()[0]
    return store, int(row_id)


def test_an_unreachable_telegram_puts_the_row_back_instead_of_failing_it(tmp_path, monkeypatch, caplog):
    store, row_id = _queue_one(tmp_path)
    calls = []

    def unreachable(**kwargs):
        calls.append(kwargs)
        raise api.TelegramUnreachable("Telegram HTTP 502: Bad Gateway")

    monkeypatch.setattr(send_queue, "send_message", unreachable)
    with caplog.at_level(logging.WARNING, logger="telegram"):
        result = send_queue.send_one_message(sqlite_path=tmp_path / "store.sqlite3", send_tlgmsg_id=row_id,
                                             bot_token="t", retry_count=3)

    assert result["status"] == "unreachable" and result["failed"] == 0
    assert len(calls) == 1                     # not three attempts against the same broken road
    row = DbaBrainStore(tmp_path / "store.sqlite3").fetch_telegram_send_message(send_tlgmsg_id=row_id)
    assert int(row["send_status"]) == 0        # pending again, never the terminal -1
    metadata = json.loads(row["metadata_json"])
    assert metadata["level"] == "critical" and "502" in metadata["last_fail_text"]
    assert "unreachable" in caplog.text and "requeued" in caplog.text


def test_a_long_message_cut_off_by_the_road_resumes_after_its_delivered_parts(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda seconds: None)
    sent = []

    def call(**kwargs):
        if sent:
            raise api.TelegramUnreachable("Telegram request failed: The read operation timed out")
        sent.append(kwargs["payload"]["text"])
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(api, "call_telegram_api", call)
    with pytest.raises(api.TelegramUnreachable) as caught:
        api.send_message(bot_token="t", chat_id="-100", text="line\n" * 2000)
    assert caught.value.parts_delivered == 1


class _Queue:
    def __init__(self, rows):
        self.rows = [{"send_tlgmsg_id": number, "tlgchat_id": chat} for chat, number in rows]

    def requeue_stale_telegram_send_messages(self, *, older_than_seconds):
        return 0

    def fetch_pending_telegram_send_messages(self, limit, *, per_chat=None):
        return self.rows[:limit]


def test_the_pass_stops_starting_rows_once_the_road_has_failed(monkeypatch):
    rows = [("-1001", 1), ("-1001", 2), ("-1002", 3), ("-1002", 4)]
    monkeypatch.setattr(send_queue, "DbaBrainStore", lambda path: _Queue(rows))
    tried = []

    def send_one(**kwargs):
        tried.append(kwargs["send_tlgmsg_id"])
        return {"send_tlgmsg_id": kwargs["send_tlgmsg_id"], "sent": 0, "failed": 0, "status": "unreachable"}

    monkeypatch.setattr(send_queue, "send_one_message", send_one)
    counts = send_queue.send_pending_messages(sqlite_path="x", bot_token="t", send_threads=1)

    assert tried == [1]
    assert counts["unreachable"] == 1 and counts["deferred"] == 3 and counts["failed"] == 0


def test_updates_not_fetched_leave_the_rest_of_the_pass_running(monkeypatch):
    done, offsets = [], []
    monkeypatch.setattr(workflow, "fetch_and_save_updates",
                        _raises(api.TelegramUnreachable("Telegram HTTP 502: Bad Gateway")))
    for name in ("save_command_messages_from_messages", "process_pending_command_messages",
                 "process_pending_conversation_messages", "check_cli_background_tasks", "send_pending_messages"):
        monkeypatch.setattr(workflow, name, lambda _name=name, **kwargs: done.append(_name) or {})

    result = workflow.run_bot_workflow(sqlite_path="x", bot_token="t", save_offset=offsets.append)

    assert "send_pending_messages" in done and "process_pending_command_messages" in done
    assert offsets == [None]                   # the offset is not moved: the next pass fetches these updates
    assert "502" in result["steps"]["get_updates_insert_messages"]["unreachable"]


def _cli(monkeypatch, raised):
    from dbabrain.telegram import cli

    config = SimpleNamespace(log_dir=Path("logs"), telegram=SimpleNamespace(update_offset=0))
    monkeypatch.setattr(cli, "resolve_config_path", lambda app, path: "config.json")
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "patch_stdout", lambda *a, **k: None)
    monkeypatch.setattr(cli, "setup_app_logger", lambda *a, **k: logging.getLogger("telegram-test"))
    monkeypatch.setattr(cli, "log_function_call", lambda *a, **k: None)
    monkeypatch.setattr(cli, "log_function_error", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_missing_bot_token", lambda config: None)
    monkeypatch.setattr(cli, "call_telegram_function", _raises(raised))
    return cli


@pytest.mark.parametrize("raised", [api.TelegramUnreachable("Telegram HTTP 502: Bad Gateway"),
                                    api.TelegramRateLimited("Telegram HTTP 429", 5)], ids=["502", "429"])
def test_the_polling_run_ends_as_a_state_not_an_error(monkeypatch, capsys, raised):
    cli = _cli(monkeypatch, raised)
    assert cli.main(["run-workflow"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["skipped"] is True and "not reached" in answer["reason"]
