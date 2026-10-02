"""A send pass sends to several chats at once, and each chat's messages still arrive in order.

On 2026-09-29 a node met a backlog in six chats. One chat at a time, a pass sent 30 rows at about
1.3 s each - a fresh HTTPS call to Telegram and three store round trips - and took about 40 s. The
bot reads its commands once per pass, so the reply to `/spbot_self_status` took a minute. The
operator: send the chats side by side, ten at a time (`send_threads`), and drop the pass's own 180 s
budget. That budget came back, measured rather than fixed: half of the Telegram workflow's own
`time_window.timeout`, which the daemon states in `DB_OPS_APP_TIMEOUT_SECONDS` - without a margin the
daemon killed the pass mid-row and the row stayed in flight for good.

What must not change with it: a chat's rows go one after another, oldest first (a conversation read
out of order is a different conversation), each row on its own call (rules R29), and a 429 pauses
only the chat it was given for.
"""

from __future__ import annotations

import json
import threading

import pytest

from db_ops.lib.config import load_config
from db_ops.telegram import send_queue


class _Queue:
    """The pass's own read: each chat's rows, oldest first, chats interleaved as the query returns them."""

    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self.rows = [{"send_tlgmsg_id": number, "tlgchat_id": chat} for chat, number in rows]
        self.requeued_after: list[int] = []

    def requeue_stale_telegram_send_messages(self, *, older_than_seconds: int) -> int:
        self.requeued_after.append(older_than_seconds)
        return 0

    def fetch_pending_telegram_send_messages(self, limit: int, *, per_chat: int | None = None):
        return self.rows[:limit]


def _interleaved(chats: int, per_chat: int) -> list[tuple[str, int]]:
    return [(f"-100{chat}", chat * 100 + row) for row in range(per_chat) for chat in range(chats)]


@pytest.fixture()
def recorder(monkeypatch):
    sent: list[tuple[str, int]] = []
    lock = threading.Lock()

    def install(rows, send_one=None):
        monkeypatch.setattr(send_queue, "DbOpsStore", lambda path: _Queue(rows))
        chat_of = {number: chat for chat, number in rows}

        def default(**kwargs):
            number = kwargs["send_tlgmsg_id"]
            with lock:
                sent.append((chat_of[number], number))
            return {"send_tlgmsg_id": number, "sent": 1, "failed": 0, "status": "sent"}

        monkeypatch.setattr(send_queue, "send_one_message", send_one or default)
        return chat_of

    install.sent = sent
    install.lock = lock
    return install


def test_every_chat_s_rows_arrive_in_the_order_they_were_written(recorder):
    recorder(_interleaved(chats=6, per_chat=5))

    counts = send_queue.send_pending_messages(sqlite_path="x", bot_token="t", send_threads=10)

    assert counts == {"read": 30, "sent": 30, "failed": 0, "deferred": 0, "paused_chats": 0,
                      "requeued": 0, "errors": 0}
    for chat in range(6):
        own = [number for chat_id, number in recorder.sent if chat_id == f"-100{chat}"]
        assert own == sorted(own) and len(own) == 5


def test_chats_are_sent_at_the_same_time(recorder):
    """Each chat's first send waits for the other two to start: one at a time, that never happens."""
    arrived = threading.Barrier(3, timeout=5)
    rows = _interleaved(chats=3, per_chat=2)
    chat_of = {number: chat for chat, number in rows}
    first_seen: set[str] = set()

    def send_one(**kwargs):
        number = kwargs["send_tlgmsg_id"]
        chat = chat_of[number]
        with recorder.lock:
            is_first = chat not in first_seen
            first_seen.add(chat)
        if is_first:
            arrived.wait()
        with recorder.lock:
            recorder.sent.append((chat, number))
        return {"send_tlgmsg_id": number, "sent": 1, "failed": 0, "status": "sent"}

    recorder(rows, send_one)

    counts = send_queue.send_pending_messages(sqlite_path="x", bot_token="t", send_threads=3)

    assert counts["sent"] == 6 and not arrived.broken


def test_one_thread_sends_one_chat_after_another(recorder):
    recorder(_interleaved(chats=3, per_chat=2))

    send_queue.send_pending_messages(sqlite_path="x", bot_token="t", send_threads=1)

    chats_in_turn = [chat for chat, _ in recorder.sent]
    assert chats_in_turn == ["-1000", "-1000", "-1001", "-1001", "-1002", "-1002"]


def test_a_rate_limit_stops_only_its_own_chat(recorder, tmp_path):
    rows = _interleaved(chats=3, per_chat=3)
    chat_of = {number: chat for chat, number in rows}

    def send_one(**kwargs):
        number = kwargs["send_tlgmsg_id"]
        if number == 101:  # the second row of chat -1001
            return {"send_tlgmsg_id": number, "sent": 0, "failed": 0, "status": "rate_limited",
                    "retry_after": 30}
        with recorder.lock:
            recorder.sent.append((chat_of[number], number))
        return {"send_tlgmsg_id": number, "sent": 1, "failed": 0, "status": "sent"}

    recorder(rows, send_one)
    pauses = tmp_path / "pauses.json"

    counts = send_queue.send_pending_messages(sqlite_path="x", bot_token="t", pauses_path=pauses,
                                              now=lambda: 1000.0)

    assert counts == {"read": 9, "sent": 7, "failed": 0, "deferred": 2, "paused_chats": 1,
                      "requeued": 0, "errors": 0}
    assert [n for c, n in recorder.sent if c == "-1001"] == [100]
    assert json.loads(pauses.read_text(encoding="utf-8")) == {"-1001": 1030.0}


def test_the_pass_has_no_fixed_time_budget_of_its_own():
    """The budget is a share of the app's timeout (SEND_BUDGET_RATIO), never a number of seconds."""
    assert not hasattr(send_queue, "SEND_BUDGET_SECONDS")
    assert send_queue.SEND_BUDGET_RATIO == 0.5


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_a_spent_budget_starts_no_further_row_and_leaves_the_rest_queued(recorder):
    rows = _interleaved(chats=1, per_chat=4)
    clock = _Clock()

    def send_one(**kwargs):
        with recorder.lock:
            recorder.sent.append(("-1000", kwargs["send_tlgmsg_id"]))
        clock.value += 60.0          # each row takes a minute
        return {"send_tlgmsg_id": kwargs["send_tlgmsg_id"], "sent": 1, "failed": 0, "status": "sent"}

    recorder(rows, send_one)

    # Timeout 300 -> budget 150: rows start at 0 and 60, the one at 120 too; 180 is past it.
    counts = send_queue.send_pending_messages(sqlite_path="x", bot_token="t", budget_seconds=150.0,
                                              budget_started_at=0.0, clock=clock)

    assert [number for _, number in recorder.sent] == [0, 1, 2]
    assert counts["sent"] == 3 and counts["deferred"] == 1


def test_the_budget_counts_from_the_process_start_not_the_pass(recorder):
    recorder(_interleaved(chats=1, per_chat=2))
    clock = _Clock()
    clock.value = 200.0              # the workflow spent 200 s on commands before sending

    counts = send_queue.send_pending_messages(sqlite_path="x", bot_token="t", budget_seconds=150.0,
                                              budget_started_at=0.0, clock=clock)

    assert recorder.sent == [] and counts["deferred"] == 2


def test_without_a_budget_every_row_is_sent(recorder):
    recorder(_interleaved(chats=1, per_chat=3))
    clock = _Clock()
    clock.value = 10_000.0

    counts = send_queue.send_pending_messages(sqlite_path="x", bot_token="t", budget_seconds=None,
                                              clock=clock)

    assert counts["sent"] == 3


def test_the_budget_is_half_of_the_timeout_the_daemon_states(monkeypatch):
    from db_ops.lib import app_timeout

    monkeypatch.setenv(app_timeout.APP_TIMEOUT_ENV_VAR, "300")
    assert app_timeout.budget_seconds(send_queue.SEND_BUDGET_RATIO) == 150.0
    monkeypatch.delenv(app_timeout.APP_TIMEOUT_ENV_VAR)
    assert app_timeout.budget_seconds(send_queue.SEND_BUDGET_RATIO) is None


def test_one_chat_failing_keeps_the_others_counts_and_the_pauses(recorder, tmp_path):
    rows = _interleaved(chats=3, per_chat=1)

    def send_one(**kwargs):
        number = kwargs["send_tlgmsg_id"]
        if number == 100:
            raise RuntimeError("store went away for this chat")
        if number == 200:
            return {"send_tlgmsg_id": number, "sent": 0, "failed": 0, "status": "rate_limited",
                    "retry_after": 30}
        return {"send_tlgmsg_id": number, "sent": 1, "failed": 0, "status": "sent"}

    recorder(rows, send_one)
    pauses = tmp_path / "pauses.json"

    counts = send_queue.send_pending_messages(sqlite_path="x", bot_token="t", pauses_path=pauses,
                                              now=lambda: 1000.0)

    assert counts["sent"] == 1 and counts["errors"] == 1 and counts["deferred"] == 1
    assert json.loads(pauses.read_text(encoding="utf-8")) == {"-1002": 1030.0}


def test_rows_left_in_flight_are_put_back_before_the_pass_reads(monkeypatch):
    queue = _Queue([])
    monkeypatch.setattr(send_queue, "DbOpsStore", lambda path: queue)
    monkeypatch.delenv("DB_OPS_APP_TIMEOUT_SECONDS", raising=False)

    send_queue.send_pending_messages(sqlite_path="x", bot_token="t")
    monkeypatch.setenv("DB_OPS_APP_TIMEOUT_SECONDS", "1800")
    send_queue.send_pending_messages(sqlite_path="x", bot_token="t")

    # The floor, then twice a timeout long enough to exceed it.
    assert queue.requeued_after == [send_queue.STALE_IN_FLIGHT_SECONDS, 3600]


@pytest.mark.parametrize("configured, expected", [(None, 10), (4, 4), (0, 10), ("x", 10)])
def test_send_threads_is_read_from_telegram_config(tmp_path, configured, expected):
    data = tmp_path / "data"
    data.mkdir()
    document = {"enabled": False}
    if configured is not None:
        document["send_threads"] = configured
    (data / "telegram_config.json").write_text(json.dumps(document), encoding="utf-8")
    (tmp_path / "config.json").write_text(json.dumps({"telegram_config_file": "data/telegram_config.json"}),
                                          encoding="utf-8")

    assert load_config(tmp_path / "config.json").telegram.send_threads == expected
