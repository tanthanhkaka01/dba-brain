"""Why Telegram's own rate limit must not be able to eat half a report in silence.

On 2026-09-09 the hourly warning report went out as a dozen-plus messages in a burst. Telegram
answered HTTP 429 — "too many requests, wait n seconds" — and the parts that met it ended at
``send_status = -1``. The queue was not wedged and nothing retried for ever, so every pass
condition read clean; the failure appeared in **no log file at all**, the only record being
``metadata_json`` on the row.

Two things were wrong and they compound. A 429 arrived as an ordinary error, so the queue's retry
loop — immediate, three times — spent every attempt inside the same second and then marked the row
failed, over a limit that had asked for a short pause. And nothing paced the burst that caused it
in the first place.

A warning nobody received is a warning that did not happen. So: the pause is honoured, the row
comes back to the queue rather than to a terminal failure, and either way it is written where an
operator reads failures.
"""

import json
import logging

import pytest

from db_ops.telegram import api


def test_a_429_carries_the_wait_telegram_asked_for(monkeypatch):
    """`parameters.retry_after` is the whole difference between a retry that works and three that
    cannot. It used to arrive as the text of a RuntimeError and nothing read it."""
    monkeypatch.setattr(api.request, "urlopen", _raise_http(429, {
        "ok": False, "error_code": 429, "description": "Too Many Requests: retry after 7",
        "parameters": {"retry_after": 7}}))

    with pytest.raises(api.TelegramRateLimited) as caught:
        api.call_telegram_api(bot_token="t", method_name="sendMessage", payload={})

    assert caught.value.retry_after == 7


def test_a_429_with_an_unreadable_body_still_waits(monkeypatch):
    """Telegram always sends the interval in practice. When it does not, "wait a little" beats
    "retry immediately", which is the behaviour that lost the message."""
    monkeypatch.setattr(api.request, "urlopen", _raise_http(429, "not json at all"))

    with pytest.raises(api.TelegramRateLimited) as caught:
        api.call_telegram_api(bot_token="t", method_name="sendMessage", payload={})

    assert caught.value.retry_after == api.DEFAULT_RATE_LIMIT_WAIT_SECONDS


def test_another_http_error_is_not_mistaken_for_a_rate_limit(monkeypatch):
    """A 400 is a refusal: the body is wrong and waiting changes nothing."""
    monkeypatch.setattr(api.request, "urlopen", _raise_http(400, {"description": "message is too long"}))

    with pytest.raises(RuntimeError) as caught:
        api.call_telegram_api(bot_token="t", method_name="sendMessage", payload={})

    assert not isinstance(caught.value, api.TelegramRateLimited)


def test_a_rate_limited_part_is_retried_after_the_wait_rather_than_lost(monkeypatch):
    slept, posted = _record_sleep(monkeypatch), []
    calls = {"n": 0}

    def fake_call(*, bot_token, method_name, payload, api_url, timeout_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            raise api.TelegramRateLimited("Telegram HTTP 429", 3)
        posted.append(payload)
        return {"ok": True, "result": {"message_id": calls["n"]}}

    monkeypatch.setattr(api, "call_telegram_api", fake_call)

    api.send_message(bot_token="t", chat_id="-100", text="WARNING|host01|disk at 91%")

    assert [part["text"] for part in posted] == ["⚠️ WARNING|host01|disk at 91%"]
    assert 3 in slept


def test_the_parts_of_one_body_are_paced_apart(monkeypatch):
    """The limit is roughly 20 messages a minute to one group. A dozen parts with no gap is over
    it before the first is read — which is how the 429 was provoked, not merely met."""
    slept = _record_sleep(monkeypatch)
    monkeypatch.setattr(api, "call_telegram_api",
                        lambda **kwargs: {"ok": True, "result": {"message_id": 1}})

    api.send_message(bot_token="t", chat_id="-100", text="header\n" + ("x" * 9000))

    assert slept.count(api.PART_PAUSE_SECONDS) >= 2      # three parts, two gaps


def test_a_wait_longer_than_the_cap_goes_back_to_the_queue_and_is_logged(monkeypatch, caplog):
    """A chat throttled for minutes is a queue problem. A send layer asleep longer than the
    daemon's own cycle is indistinguishable from a hang."""
    _record_sleep(monkeypatch)
    monkeypatch.setattr(api, "call_telegram_api", _raises(
        api.TelegramRateLimited("Telegram HTTP 429", api.MAX_RATE_LIMIT_WAIT_SECONDS + 120)))

    with caplog.at_level(logging.ERROR, logger="telegram"):
        with pytest.raises(api.TelegramRateLimited):
            api.send_message(bot_token="t", chat_id="-100", text="WARNING|host01|disk at 91%")

    assert "NOT delivered" in caplog.text


# ---------------------------------------------------------------------------
# The queue: a deferral is not a failure
# ---------------------------------------------------------------------------
def test_a_rate_limited_row_comes_back_to_the_queue_instead_of_dying(tmp_path, monkeypatch, caplog):
    from db_ops.db import DbOpsStore
    from db_ops.telegram import send_queue

    store, send_id = _queue_one(tmp_path)
    _record_sleep(monkeypatch, module=send_queue)
    monkeypatch.setattr(send_queue, "send_message",
                        _raises(api.TelegramRateLimited("Telegram HTTP 429", 2)))

    with caplog.at_level(logging.WARNING, logger="telegram"):
        result = send_queue.send_one_message(
            sqlite_path=tmp_path / "db_ops.sqlite", send_tlgmsg_id=send_id, bot_token="t")

    assert result["status"] == "rate_limited"
    row = DbOpsStore(tmp_path / "db_ops.sqlite").fetch_telegram_send_message(send_tlgmsg_id=send_id)
    assert int(row["send_status"]) == 0                   # pending again, not a terminal -1
    assert "rate limited" in caplog.text and "requeued" in caplog.text


def test_a_requeued_row_comes_back_as_itself(tmp_path, monkeypatch):
    """`reset_telegram_send_message_pending` replaced the whole metadata object with its own
    failure text. It had no caller until now, so nothing had met it: a re-queued document row
    would have lost `document_path` and gone out next cycle as a plain message."""
    from db_ops.db import DbOpsStore
    from db_ops.telegram import send_queue

    store, send_id = _queue_one(tmp_path, metadata={"document_path": "report.xlsx"})
    _record_sleep(monkeypatch, module=send_queue)
    monkeypatch.setattr(send_queue, "send_document",
                        _raises(api.TelegramRateLimited("Telegram HTTP 429", 2)))

    send_queue.send_one_message(
        sqlite_path=tmp_path / "db_ops.sqlite", send_tlgmsg_id=send_id, bot_token="t")

    row = DbOpsStore(tmp_path / "db_ops.sqlite").fetch_telegram_send_message(send_tlgmsg_id=send_id)
    metadata = json.loads(str(row["metadata_json"]))
    assert metadata["document_path"] == "report.xlsx"
    assert metadata["last_fail_text"]


def test_a_refusal_is_still_terminal_and_now_says_so_in_the_log(tmp_path, monkeypatch, caplog):
    """Nothing about an ordinary failure changed except that it is visible. A 400 is the body
    being wrong; retrying it for ever would be the other way to lose a report."""
    from db_ops.db import DbOpsStore
    from db_ops.telegram import send_queue

    store, send_id = _queue_one(tmp_path)
    monkeypatch.setattr(send_queue, "send_message",
                        _raises(RuntimeError("Telegram HTTP 400: message is too long")))

    with caplog.at_level(logging.ERROR, logger="telegram"):
        result = send_queue.send_one_message(
            sqlite_path=tmp_path / "db_ops.sqlite", send_tlgmsg_id=send_id, bot_token="t",
            retry_count=1)

    assert result["status"] == "failed"
    row = DbOpsStore(tmp_path / "db_ops.sqlite").fetch_telegram_send_message(send_tlgmsg_id=send_id)
    assert int(row["send_status"]) == -1
    assert "delivery FAILED" in caplog.text


# ---------------------------------------------------------------------------
def _queue_one(tmp_path, *, metadata=None):
    from db_ops.db import DbOpsStore

    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    send_id = store.insert_telegram_send_message(
        tlgchat_id="-100", message_text="WARNING|host01|disk at 91%", metadata=metadata)
    return store, send_id


def _record_sleep(monkeypatch, *, module=api):
    """Every wait in this file is recorded rather than taken — the suite is offline and fast."""
    slept: list[float] = []
    monkeypatch.setattr(module.time, "sleep", slept.append)
    return slept


def _raises(error):
    def fail(**kwargs):
        raise error
    return fail


def _raise_http(code, body):
    from urllib import error as urllib_error

    payload = body if isinstance(body, str) else json.dumps(body)

    def fail(*args, **kwargs):
        raise urllib_error.HTTPError(
            "https://api.telegram.org", code, "err", {}, _Body(payload))
    return fail


class _Body:
    """The file-like `fp` an HTTPError reads its body from."""

    def __init__(self, text):
        self._text = text.encode("utf-8")

    def read(self, *args):
        return self._text

    def close(self):
        return None
