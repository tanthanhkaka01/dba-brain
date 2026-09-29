"""One chat's backlog must never hold another chat's messages - least of all the bot's own replies.

On 2026-09-28 the soak node's lab drills failed every ~10 s and each sent two messages to the lab
SQL chat: ~3,600 an hour into a group Telegram drains at ~1,250 an hour. The send pass took the
oldest 50 rows of the whole queue, so every chat queued behind that one - and when the operator
typed `/spbot_self_status` in a private chat, the reply was created and then waited behind ~46,000
lab alerts. Each pass also slept out Telegram's 429s, so it grew from 11.5 s to 143.5 s on average,
and the bot read its commands once every two minutes and more.

So (0.25.0, the operator's design): the pass runs every second and takes the oldest **five of each
chat**, every chat in turn. A 429 pauses that chat for the seconds Telegram asked - kept for the
next passes - and the pass carries on with the others instead of sleeping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.db import DbOpsStore
from db_ops.telegram import api, send_queue

FLOODED = "-1001"
OTHER_GROUP = "-1002"
PRIVATE = "851"


def _store(tmp_path: Path, rows: list[tuple[str, str]]) -> tuple[Path, DbOpsStore, list[int]]:
    """A real store with ``rows`` (chat, text) queued in that order."""
    path = tmp_path / "db_ops.sqlite"
    store = DbOpsStore(path)
    store.initialize()
    ids = [store.insert_telegram_send_message(tlgchat_id=chat, message_text=text) for chat, text in rows]
    return path, store, ids


def _flood_then_others(flood: int = 12) -> list[tuple[str, str]]:
    return ([(FLOODED, f"ERROR|lab|drill {n} failed") for n in range(flood)]
            + [(OTHER_GROUP, "WARNING|host01|disk at 91%"), (OTHER_GROUP, "WARNING|host02|disk at 92%")]
            + [(PRIVATE, "0.25.0 on DB-THANH")])


def _pending_ids(store: DbOpsStore) -> set[int]:
    return {int(row["send_tlgmsg_id"]) for row in store.fetch_pending_telegram_send_messages(limit=10_000)}


class _Telegram:
    """Records every send; a chat in ``limited`` answers 429 with ``retry_after``."""

    def __init__(self, limited: dict[str, float] | None = None) -> None:
        self.limited = dict(limited or {})
        self.calls: list[tuple[str, str, bool]] = []

    def send_message(self, *, chat_id, text, wait_before_first_part=True, **_kwargs):
        self.calls.append((str(chat_id), text, wait_before_first_part))
        if str(chat_id) in self.limited:
            raise api.TelegramRateLimited("Telegram HTTP 429", self.limited[str(chat_id)])
        return {"ok": True, "result": {"message_id": len(self.calls)}}

    def chats(self) -> list[str]:
        return [chat for chat, _text, _wait in self.calls]


@pytest.fixture()
def telegram(monkeypatch):
    fake = _Telegram()
    monkeypatch.setattr(send_queue, "send_message", fake.send_message)
    # A pass that sleeps is the defect this file exists for: any wait is a failure here.
    monkeypatch.setattr(send_queue.time, "sleep", lambda seconds: pytest.fail(f"the pass slept {seconds}s"))
    return fake


def test_a_pass_takes_the_oldest_five_of_each_chat_every_chat_in_turn(tmp_path):
    _path, store, ids = _store(tmp_path, _flood_then_others())

    rows = store.fetch_pending_telegram_send_messages(limit=50, per_chat=5)

    assert [int(row["send_tlgmsg_id"]) for row in rows] == [
        ids[0], ids[12], ids[14],     # every chat's oldest first
        ids[1], ids[13],
        ids[2], ids[3], ids[4],       # the flooded chat's next, up to five
    ]


def test_without_per_chat_the_query_is_the_old_one(tmp_path):
    """`send-queue` and every other caller that does not ask keeps the whole queue's order."""
    _path, store, ids = _store(tmp_path, _flood_then_others())

    rows = store.fetch_pending_telegram_send_messages(limit=3)

    assert [int(row["send_tlgmsg_id"]) for row in rows] == ids[:3]


def test_the_bots_reply_goes_out_in_the_first_pass_behind_a_flood(tmp_path, telegram):
    path, store, ids = _store(tmp_path, _flood_then_others(flood=200))

    counts = send_queue.send_pending_messages(sqlite_path=path, bot_token="t")

    assert PRIVATE in telegram.chats()
    assert telegram.chats().count(FLOODED) == 5
    assert telegram.chats().count(OTHER_GROUP) == 2
    assert counts["sent"] == 8 and counts["failed"] == 0
    assert ids[-1] not in _pending_ids(store), "the reply was sent"


def test_a_rate_limited_chat_is_paused_and_the_pass_carries_on_without_sleeping(tmp_path, telegram):
    path, store, ids = _store(tmp_path, _flood_then_others())
    telegram.limited[FLOODED] = 30
    pauses = tmp_path / "runtime" / send_queue.PAUSES_FILE_NAME

    counts = send_queue.send_pending_messages(sqlite_path=path, bot_token="t", pauses_path=pauses,
                                              now=lambda: 1_000.0)

    assert telegram.chats().count(FLOODED) == 1, "one refused call, then the chat waits"
    assert telegram.calls[0][2] is False, "the pass asks the send layer not to wait either"
    assert {OTHER_GROUP, PRIVATE} <= set(telegram.chats())
    assert set(ids[:12]) <= _pending_ids(store), "nothing of the paused chat is lost or failed"
    assert counts["paused_chats"] == 1 and counts["deferred"] == 5
    assert json.loads(pauses.read_text(encoding="utf-8")) == {FLOODED: 1_030.0}


def test_the_next_pass_leaves_a_paused_chat_alone_until_its_pause_is_over(tmp_path, telegram):
    path, _store_, _ids = _store(tmp_path, _flood_then_others())
    pauses = tmp_path / send_queue.PAUSES_FILE_NAME
    pauses.write_text(json.dumps({FLOODED: 1_030.0}), encoding="utf-8")

    send_queue.send_pending_messages(sqlite_path=path, bot_token="t", pauses_path=pauses, now=lambda: 1_010.0)
    assert FLOODED not in telegram.chats()

    send_queue.send_pending_messages(sqlite_path=path, bot_token="t", pauses_path=pauses, now=lambda: 1_031.0)
    assert telegram.chats().count(FLOODED) == 5
    assert not pauses.exists(), "a pause that is over is not kept"


def test_an_unreadable_pause_file_is_no_pause(tmp_path, telegram):
    path, _store_, _ids = _store(tmp_path, _flood_then_others())
    pauses = tmp_path / send_queue.PAUSES_FILE_NAME
    pauses.write_text("{not json", encoding="utf-8")

    send_queue.send_pending_messages(sqlite_path=path, bot_token="t", pauses_path=pauses, now=lambda: 1.0)

    assert telegram.chats().count(FLOODED) == 5


def test_send_one_still_waits_out_a_rate_limit_by_default(tmp_path, monkeypatch):
    """`send-one` is a person sending one row: waiting for it is what they asked for."""
    path, store, ids = _store(tmp_path, [(OTHER_GROUP, "WARNING|host01|disk at 91%")])
    calls = {"n": 0}

    def once_limited(**kwargs):
        calls["n"] += 1
        assert kwargs["wait_before_first_part"] is True
        if calls["n"] == 1:
            raise api.TelegramRateLimited("Telegram HTTP 429", 2)
        return {"ok": True, "result": {"message_id": 9}}

    slept: list[float] = []
    monkeypatch.setattr(send_queue, "send_message", once_limited)
    monkeypatch.setattr(send_queue.time, "sleep", slept.append)

    result = send_queue.send_one_message(sqlite_path=path, send_tlgmsg_id=ids[0], bot_token="t")

    assert result["status"] == "sent" and slept == [2]


def test_the_first_part_can_be_handed_back_without_a_wait(monkeypatch):
    """Nothing has been sent when the first part meets a 429, so handing it back duplicates
    nothing; a later part still waits, because part of the body has landed."""
    monkeypatch.setattr(api.time, "sleep", lambda seconds: pytest.fail("waited on the first part"))
    monkeypatch.setattr(api, "call_telegram_api", _raises(api.TelegramRateLimited("Telegram HTTP 429", 30)))

    with pytest.raises(api.TelegramRateLimited):
        api.send_message(bot_token="t", chat_id=FLOODED, text="ERROR|lab|drill failed",
                         wait_before_first_part=False)


def test_a_documents_rate_limit_is_a_pause_not_a_failure(tmp_path, monkeypatch):
    """A report file met a 429 as a plain error: three immediate retries, then the row failed."""
    import io
    from urllib import error

    report = tmp_path / "r.xlsx"
    report.write_bytes(b"x")

    def refuse(*_args, **_kwargs):
        body = json.dumps({"ok": False, "error_code": 429, "parameters": {"retry_after": 11}})
        raise error.HTTPError("https://api.telegram.org", 429, "Too Many Requests", {},
                              io.BytesIO(body.encode("utf-8")))

    monkeypatch.setattr(api.request, "urlopen", refuse)

    with pytest.raises(api.TelegramRateLimited) as caught:
        api.call_telegram_multipart_api(bot_token="t", method_name="sendDocument", payload={"chat_id": "1"},
                                        file_field="document", file_path=report)

    assert caught.value.retry_after == 11


@pytest.mark.parametrize(("configured", "expected"), [(None, 5), (3, 3), ("8", 8), (0, 5), ("five", 5)])
def test_send_per_chat_is_read_from_telegram_config(tmp_path, configured, expected):
    from db_ops.common import scaffold
    from db_ops.lib.config import load_config

    scaffold.initialise(tmp_path, app_name="dbabrain", force=False)
    telegram_config = tmp_path / "data" / "telegram_config.json"
    document = json.loads(telegram_config.read_text(encoding="utf-8"))
    if configured is None:
        document.pop("send_per_chat", None)
    else:
        document["send_per_chat"] = configured
    telegram_config.write_text(json.dumps(document), encoding="utf-8")

    assert load_config(tmp_path / "config.json").telegram.send_per_chat == expected


def _raises(exc):
    def raise_it(*_args, **_kwargs):
        raise exc
    return raise_it
