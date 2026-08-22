"""The conversation flow must pick up a *document-only* reply (a file upload with empty text),
so accept_file parameters (spbot_add_sql, spbot_sql_to_xlsx) work when the user attaches a .sql
file instead of pasting text. Regression: the fetch query filtered out empty-text messages and
was not null-safe on the command filter, so a file upload never advanced the state (bot silent).
"""

from db_ops.db import DbOpsStore


def _msg(message_id, *, text="", raw=None):
    return {
        "update_id": message_id,
        "message_id": message_id,
        "message_date": message_id,
        "chat_id": "5",
        "chat_type": "private",
        "user_id": "5",
        "text": text,
        "raw": raw if raw is not None else ({"text": text} if text else {}),
    }


def test_fetch_next_message_accepts_document_only(tmp_path):
    store = DbOpsStore(tmp_path / "r.sqlite")
    store.upsert_telegram_messages(
        [
            _msg(10, text="/spbot_sql_to_xlsx"),
            _msg(11, raw={"document": {"file_id": "AAA", "file_name": "q.sql"}}),
        ]
    )
    row = store.fetch_next_telegram_message_for_state(chat_id="5", user_id="5", after_message_id=10)
    assert row is not None
    assert int(row["message_id"]) == 11


def test_fetch_next_message_skips_empty_non_document(tmp_path):
    store = DbOpsStore(tmp_path / "r.sqlite")
    store.upsert_telegram_messages([_msg(11, raw={})])  # empty text, no document
    assert store.fetch_next_telegram_message_for_state(chat_id="5", user_id="5", after_message_id=10) is None


def test_fetch_next_message_skips_command_message(tmp_path):
    store = DbOpsStore(tmp_path / "r.sqlite")
    store.upsert_telegram_messages([_msg(11, text="/spbot_status")])
    assert store.fetch_next_telegram_message_for_state(chat_id="5", user_id="5", after_message_id=10) is None


def test_fetch_next_message_returns_plain_text(tmp_path):
    store = DbOpsStore(tmp_path / "r.sqlite")
    store.upsert_telegram_messages([_msg(11, text="SELECT 1")])
    row = store.fetch_next_telegram_message_for_state(chat_id="5", user_id="5", after_message_id=10)
    assert row is not None and str(row["text"]) == "SELECT 1"
