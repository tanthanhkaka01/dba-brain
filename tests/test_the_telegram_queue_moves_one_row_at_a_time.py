"""The Telegram queue marks, sends and updates one row per call - never a batch (rules R29).

A batch fails as a batch: send ten messages, then update ten statuses, and a crash between the
two leaves ten rows that were delivered and still read as pending - the next pass sends all ten
again. One row at a time bounds that to the one message in flight, and a row being sent is visibly
`processing`, so no second sender takes it. `docs/07_telegram_app.md` says the rule; this holds the
send queue to it by watching the store at the moment each message goes out.
"""

from __future__ import annotations

from db_ops.db import DbOpsStore
from db_ops.telegram import send_queue

PENDING, SENT, PROCESSING = 0, 1, 2


def test_each_message_is_marked_sent_before_the_next_one_is_touched(tmp_path, monkeypatch):
    path = tmp_path / "db_ops.sqlite"
    store = DbOpsStore(path)
    store.initialize()
    ids = [store.insert_telegram_send_message(tlgchat_id="-100", message_text=f"LOGGING|host|part {n}")
           for n in range(3)]
    seen: list[dict[int, int]] = []

    def fake_send(**kwargs):
        seen.append({send_id: int(DbOpsStore(path).fetch_telegram_send_message(
            send_tlgmsg_id=send_id)["send_status"]) for send_id in ids})
        return {"ok": True, "result": {"message_id": len(seen)}}

    monkeypatch.setattr(send_queue, "send_message", fake_send)
    counts = send_queue.send_pending_messages(sqlite_path=path, bot_token="t")

    assert counts["sent"] == 3
    for index, states in enumerate(seen):
        assert [states[send_id] for send_id in ids[:index]] == [SENT] * index, (
            "an earlier message was not finished before this one was sent")
        assert states[ids[index]] == PROCESSING, "the message in flight is marked, and only it"
        assert [states[send_id] for send_id in ids[index + 1:]] == [PENDING] * (len(ids) - index - 1), (
            "a later message was touched before its turn - that is a batch")
