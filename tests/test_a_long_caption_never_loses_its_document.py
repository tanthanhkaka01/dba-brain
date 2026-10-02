"""A document whose caption is longer than Telegram allows is still delivered, and so is the text.

`sendDocument` takes a caption of 1024 characters - a quarter of a message's 4096. The send queue
handed the row's whole text to the document as its caption, Telegram answered HTTP 400, the row was
retried three times and failed, and the file was never delivered. A SQL task's result workbook
carries its whole status block as the caption, error text included, so the run that most needed
its file was the one that lost it (review 0.25.0, B1.1).

Now the caption is the leading lines, cut inside the limit with a note that the rest follows, and
the whole text follows as an ordinary message.
"""

from __future__ import annotations

from pathlib import Path

from db_ops.db import DbOpsStore
from db_ops.lib.telegram_text import CAPTION_CONTINUED, TELEGRAM_CAPTION_LIMIT, document_caption
from db_ops.telegram import send_queue

LONG = "\n".join(f"line {n:03d}: " + "x" * 40 for n in range(60))   # ~3000 characters


def test_a_caption_that_fits_is_left_alone():
    assert document_caption("WARNING|SQL037 done") == ("WARNING|SQL037 done", "")


def test_a_long_caption_is_cut_at_a_line_inside_the_limit_and_the_whole_text_follows():
    caption, follow_up = document_caption(LONG)

    assert len(caption) <= TELEGRAM_CAPTION_LIMIT
    assert caption.endswith(CAPTION_CONTINUED)
    assert caption[: -len(CAPTION_CONTINUED)].endswith("x" * 40)   # a whole line, not half a word
    assert follow_up == LONG


def test_the_queue_delivers_the_document_and_then_the_whole_text(tmp_path: Path, monkeypatch):
    report = tmp_path / "result.xlsx"
    report.write_bytes(b"xlsx")
    path = tmp_path / "db_ops.sqlite"
    store = DbOpsStore(path)
    store.initialize()
    row_id = store.insert_telegram_send_message(tlgchat_id="-100", message_text=LONG,
                                               metadata={"document_path": str(report)})
    documents: list[str] = []
    messages: list[str] = []
    monkeypatch.setattr(send_queue, "send_document",
                        lambda **kw: documents.append(kw["caption"]) or {"result": {"message_id": 7}})
    monkeypatch.setattr(send_queue, "send_message",
                        lambda **kw: messages.append(kw["text"]) or {"result": {"message_id": 8}})

    answer = send_queue.send_one_message(sqlite_path=path, send_tlgmsg_id=row_id, bot_token="t")

    assert answer["sent"] == 1
    assert len(documents) == 1 and len(documents[0]) <= TELEGRAM_CAPTION_LIMIT
    assert messages == [LONG]


# --------------------------------------------------------------------------- #
# The text after the document is a second call - and it failing never sends the file again
# --------------------------------------------------------------------------- #
def _queued_document(tmp_path: Path) -> tuple[Path, int]:
    report = tmp_path / "result.xlsx"
    report.write_bytes(b"xlsx")
    path = tmp_path / "db_ops.sqlite"
    store = DbOpsStore(path)
    store.initialize()
    return path, store.insert_telegram_send_message(
        tlgchat_id="-100", message_text=LONG, metadata={"document_path": str(report)})


def _deliveries(monkeypatch, follow_up) -> tuple[list[str], list[dict]]:
    documents: list[str] = []
    texts: list[dict] = []
    monkeypatch.setattr(send_queue, "send_document",
                        lambda **kw: documents.append(kw["caption"]) or {"result": {"message_id": 7}})

    def send_message(**kw):
        texts.append(kw)
        return follow_up(len(texts))

    monkeypatch.setattr(send_queue, "send_message", send_message)
    monkeypatch.setattr(send_queue.time, "sleep", lambda _seconds: None)
    return documents, texts


def test_a_rate_limit_on_the_text_hands_the_row_back_and_the_next_pass_sends_only_the_text(
        tmp_path: Path, monkeypatch):
    """The fix for B1.1 made a document two calls, and the second one failing sent the row round
    again from the top: under a rate limit - a busy chat - the same workbook arrived once per pass."""
    path, row_id = _queued_document(tmp_path)

    def follow_up(call: int):
        if call == 1:
            raise send_queue.TelegramRateLimited("Telegram HTTP 429", 3)
        return {"result": {"message_id": 8}}

    documents, texts = _deliveries(monkeypatch, follow_up)

    first = send_queue.send_one_message(sqlite_path=path, send_tlgmsg_id=row_id, bot_token="t",
                                        wait_on_rate_limit=False)
    second = send_queue.send_one_message(sqlite_path=path, send_tlgmsg_id=row_id, bot_token="t",
                                         wait_on_rate_limit=False)

    assert first["status"] == "rate_limited" and second["status"] == "sent"
    assert len(documents) == 1, "the file went out once"
    assert [kw["text"] for kw in texts] == [LONG, LONG]
    assert texts[0]["wait_before_first_part"] is False, "the send pass is not held by one chat's limit"
    row = DbOpsStore(path).fetch_telegram_send_message(send_tlgmsg_id=row_id)
    assert int(row["message_id"]) == 7, "the row's message is the document"


def test_a_text_that_fails_outright_is_retried_without_the_file(tmp_path: Path, monkeypatch):
    path, row_id = _queued_document(tmp_path)

    def follow_up(call: int):
        raise RuntimeError("Telegram HTTP 400: can't parse entities")

    documents, texts = _deliveries(monkeypatch, follow_up)

    answer = send_queue.send_one_message(sqlite_path=path, send_tlgmsg_id=row_id, bot_token="t")

    assert answer["status"] == "failed"
    assert len(documents) == 1 and len(texts) == 3, "three attempts at the text, one file"
    row = DbOpsStore(path).fetch_telegram_send_message(send_tlgmsg_id=row_id)
    assert "the document was delivered" in str(row["metadata_json"])


def test_a_long_text_resumes_at_the_part_the_limit_stopped(tmp_path: Path, monkeypatch):
    path, row_id = _queued_document(tmp_path)

    def follow_up(call: int):
        if call == 1:
            limited = send_queue.TelegramRateLimited("Telegram HTTP 429", 3)
            limited.parts_delivered = 2
            raise limited
        return {"result": {"message_id": 8}}

    documents, texts = _deliveries(monkeypatch, follow_up)

    send_queue.send_one_message(sqlite_path=path, send_tlgmsg_id=row_id, bot_token="t",
                                wait_on_rate_limit=False)
    send_queue.send_one_message(sqlite_path=path, send_tlgmsg_id=row_id, bot_token="t",
                                wait_on_rate_limit=False)

    assert len(documents) == 1
    assert [kw["start_part"] for kw in texts] == [0, 2]
