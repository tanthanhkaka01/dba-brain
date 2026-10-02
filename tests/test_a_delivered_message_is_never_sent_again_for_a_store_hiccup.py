"""Telegram has a message the moment it answers - recording that is not a reason to send it again.

The send and the store write used to share one retry loop, so a store write that failed after
Telegram had accepted the row (SQLite's `database is locked`, likelier with 0.25.0's ten send
threads) went round the loop and sent the message a second time. And a row a killed pass left in
flight (`send_status = 2`) stayed there for good: nothing ever moved it back.
"""

from __future__ import annotations

import sqlite3

from db_ops.db.store import DbOpsStore
from db_ops.telegram import send_queue


def _store_with_one_row(tmp_path) -> tuple[DbOpsStore, int]:
    store = DbOpsStore(tmp_path / "store.sqlite3")
    store.initialize()
    with store.connect() as conn:
        conn.execute("INSERT INTO telegram_send_messages (tlgchat_id, message_text, send_status) "
                     "VALUES (?, ?, 0)", ("-100", "hello"))
        row_id = conn.execute("SELECT max(send_tlgmsg_id) FROM telegram_send_messages").fetchone()[0]
    return store, int(row_id)


def _status(store: DbOpsStore, row_id: int) -> tuple[int, str | None]:
    with store.connect() as conn:
        row = conn.execute("SELECT send_status, send_date FROM telegram_send_messages "
                           "WHERE send_tlgmsg_id = ?", (row_id,)).fetchone()
    return int(row["send_status"]), row["send_date"]


def test_a_store_write_that_fails_once_does_not_send_the_message_twice(tmp_path, monkeypatch):
    store, row_id = _store_with_one_row(tmp_path)
    sent: list[str] = []
    monkeypatch.setattr(send_queue, "send_message",
                        lambda **kw: sent.append(kw["text"]) or {"result": {"message_id": 7}})
    monkeypatch.setattr(send_queue, "RECORD_SENT_PAUSE_SECONDS", 0.0)
    real = DbOpsStore.mark_telegram_send_message_sent
    calls = {"n": 0}

    def flaky(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(self, **kwargs)

    monkeypatch.setattr(DbOpsStore, "mark_telegram_send_message_sent", flaky)

    result = send_queue.send_one_message(sqlite_path=tmp_path / "store.sqlite3",
                                         send_tlgmsg_id=row_id, bot_token="t")

    assert result["status"] == "sent"
    assert sent == ["hello"]
    assert _status(store, row_id)[0] == 1


def test_a_store_that_never_records_it_still_does_not_resend(tmp_path, monkeypatch):
    store, row_id = _store_with_one_row(tmp_path)
    sent: list[str] = []
    monkeypatch.setattr(send_queue, "send_message",
                        lambda **kw: sent.append(kw["text"]) or {"result": {"message_id": 7}})
    monkeypatch.setattr(send_queue, "RECORD_SENT_PAUSE_SECONDS", 0.0)

    def always_locked(self, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(DbOpsStore, "mark_telegram_send_message_sent", always_locked)

    result = send_queue.send_one_message(sqlite_path=tmp_path / "store.sqlite3",
                                         send_tlgmsg_id=row_id, bot_token="t", retry_count=3)

    assert result["status"] == "sent"
    assert sent == ["hello"]
    assert _status(store, row_id)[0] == 2        # in flight, for the stale re-queue to judge later


def test_a_row_another_sender_took_is_not_sent(tmp_path, monkeypatch):
    store, row_id = _store_with_one_row(tmp_path)
    assert store.mark_telegram_send_message_processing(send_tlgmsg_id=row_id) is True
    assert store.mark_telegram_send_message_processing(send_tlgmsg_id=row_id) is False


def test_a_row_left_in_flight_goes_back_to_the_queue_once_stale(tmp_path):
    store, row_id = _store_with_one_row(tmp_path)
    store.mark_telegram_send_message_processing(send_tlgmsg_id=row_id)
    assert _status(store, row_id)[1]              # when it went in flight

    assert store.requeue_stale_telegram_send_messages(older_than_seconds=900) == 0
    assert _status(store, row_id)[0] == 2

    with store.connect() as conn:
        conn.execute("UPDATE telegram_send_messages SET send_date = '2020-01-01T00:00:00Z' "
                     "WHERE send_tlgmsg_id = ?", (row_id,))
    assert store.requeue_stale_telegram_send_messages(older_than_seconds=900) == 1
    assert _status(store, row_id) == (0, None)


def test_a_row_put_in_flight_before_the_stamp_existed_is_stale(tmp_path):
    store, row_id = _store_with_one_row(tmp_path)
    with store.connect() as conn:
        conn.execute("UPDATE telegram_send_messages SET send_status = 2, send_date = NULL "
                     "WHERE send_tlgmsg_id = ?", (row_id,))

    assert store.requeue_stale_telegram_send_messages(older_than_seconds=900) == 1


def test_sent_and_failed_rows_are_never_requeued(tmp_path):
    store, row_id = _store_with_one_row(tmp_path)
    store.mark_telegram_send_message_sent(send_tlgmsg_id=row_id, message_id=1)

    assert store.requeue_stale_telegram_send_messages(older_than_seconds=0) == 0
    assert _status(store, row_id)[0] == 1
