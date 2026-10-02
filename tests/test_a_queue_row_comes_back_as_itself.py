"""A Telegram queue row that fails or is rate-limited keeps what it is, and resumes where it stopped.

Two details of the send queue (review 0.25.0, B3.2 and B1.2):

* **A failed row lost its metadata.** Marking it failed wrote ``{"fail_text": ...}`` over the whole
  object - `document_path`, `reply_markup`, the level and the SQL task id went with it, so a failed
  report row no longer said which file it had carried.
* **A rate limit on a later part re-sent the earlier ones.** A long message goes out in parts. A 429
  that outlasted the wait on part 3 put the row back in the queue, and the next pass started again
  at part 1: the chat saw parts 1 and 2 twice. The send knows exactly which part failed, so the
  row now records how many were delivered and the next pass resumes there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.db import DbOpsStore
from db_ops.telegram import api, send_queue


def _store(tmp_path: Path) -> tuple[Path, DbOpsStore]:
    path = tmp_path / "db_ops.sqlite"
    store = DbOpsStore(path)
    store.initialize()
    return path, store


def _metadata(store: DbOpsStore, row_id: int) -> dict:
    return json.loads(str(store.fetch_telegram_send_message(send_tlgmsg_id=row_id)["metadata_json"]))


def test_a_failed_row_keeps_its_document_and_says_why(tmp_path):
    _, store = _store(tmp_path)
    row_id = store.insert_telegram_send_message(
        tlgchat_id="-100", message_text="SQL037 result",
        metadata={"document_path": "/runtime/output/sql_tasks/r.xlsx", "sql_id": 37})

    store.mark_telegram_send_message_failed(send_tlgmsg_id=row_id, fail_text="Telegram HTTP 400")

    metadata = _metadata(store, row_id)
    assert metadata["document_path"] == "/runtime/output/sql_tasks/r.xlsx"
    assert metadata["sql_id"] == 37 and metadata["fail_text"] == "Telegram HTTP 400"


LONG = "\n".join(f"row {n:04d} " + "x" * 60 for n in range(180))   # several parts
PARTS = len(api.split_telegram_message(LONG))


def test_a_rate_limit_on_a_later_part_says_how_many_were_delivered(monkeypatch):
    sent: list[str] = []

    def telegram(*, payload, **_):
        if len(sent) == 1:
            raise api.TelegramRateLimited("Telegram HTTP 429", 600)   # past the wait cap: handed back
        sent.append(payload["text"])
        return {"result": {"message_id": len(sent)}}

    monkeypatch.setattr(api, "call_telegram_api", telegram)
    monkeypatch.setattr(api.time, "sleep", lambda _s: None)

    with pytest.raises(api.TelegramRateLimited) as caught:
        api.send_message(bot_token="t", chat_id="-100", text=LONG)

    assert PARTS >= 3
    assert caught.value.parts_delivered == 1


def test_resuming_sends_only_the_parts_not_yet_delivered(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(api, "call_telegram_api",
                        lambda *, payload, **_: sent.append(payload["text"]) or {"result": {"message_id": 1}})
    monkeypatch.setattr(api.time, "sleep", lambda _s: None)

    api.send_message(bot_token="t", chat_id="-100", text=LONG, start_part=1)

    assert len(sent) == PARTS - 1
    assert f"[part 2/{PARTS}]" in sent[0] and f"[part {PARTS}/{PARTS}]" in sent[-1]


def test_the_queue_records_the_delivered_parts_and_resumes_from_them(tmp_path, monkeypatch):
    path, store = _store(tmp_path)
    row_id = store.insert_telegram_send_message(tlgchat_id="-100", message_text=LONG)
    starts: list[int] = []

    def first_pass(**kw):
        starts.append(kw["start_part"])
        exc = api.TelegramRateLimited("Telegram HTTP 429", 30)
        exc.parts_delivered = 2
        raise exc

    monkeypatch.setattr(send_queue, "send_message", first_pass)
    answer = send_queue.send_one_message(sqlite_path=path, send_tlgmsg_id=row_id, bot_token="t",
                                         wait_on_rate_limit=False)
    assert answer["status"] == "rate_limited"
    assert _metadata(store, row_id)["parts_delivered"] == 2

    monkeypatch.setattr(send_queue, "send_message",
                        lambda **kw: starts.append(kw["start_part"]) or {"result": {"message_id": 9}})
    answer = send_queue.send_one_message(sqlite_path=path, send_tlgmsg_id=row_id, bot_token="t")

    assert answer["sent"] == 1
    assert starts == [0, 2]
