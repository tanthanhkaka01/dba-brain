"""The severity emoji every outgoing Telegram message leads with.

A group full of alerts is read at a glance, so the symbol has to be right for the traffic
that actually flows through the queue. These cases are real headers taken from
`telegram_send_messages`: backup/restore events, metrics reports, SLA, the jobs daemon and
command replies. The two rules worth protecting are that a header stating nothing about
severity is left alone, and that tagging never stacks on a producer that tagged itself.
"""

import pytest

from db_ops.telegram.api import send_message
from db_ops.telegram.severity import SEVERITY_EMOJI, classify_message, decorate_message


# ---------------------------------------------------------------------------
# Real headers -> the symbol an operator expects
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "header,severity",
    [
        ("LOGGING|host01|Restore workflow started.", "started"),
        ("LOGGING|host01|backup_restore.backup START: backup_id=CLOUD_MSSQL_DIFF started.", "started"),
        ("LOGGING|host01|Restore workflow finished. status=done", "success"),
        ("LOGGING|host01|backup_restore.backup END: Backup finished: done (exit 0)", "success"),
        ("ERROR|host01|Restore workflow FAILED.", "failed"),
        ("ERROR|host01|Stale running workflow detected on startup.", "failed"),
        ("CRITICAL|host01|Restore aborted.", "critical"),
        ("WARNING|host01|Restore workflow finished with warnings.", "warning"),
        ("[Metrics Warning Report]", "warning"),
        ("[Metrics Critical Report]", "critical"),
        ("[SQL] SQL task running", "running"),
        ("[SQL] SQL task done", "success"),
        ("Restore still running (45 min)", "running"),
    ],
)
def test_header_severity(header, severity):
    assert classify_message(header) == severity
    assert decorate_message(header).startswith(SEVERITY_EMOJI[severity])


def test_the_six_levels_have_the_agreed_symbols():
    assert SEVERITY_EMOJI == {
        "critical": "🚨",
        "failed": "❌",
        "warning": "⚠️",
        "started": "▶️",
        "running": "⏳",
        "success": "✅",
    }


# ---------------------------------------------------------------------------
# Silence beats a wrong symbol
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Server targets - server_id | db_type ip:port | instance",
        "SQL result for ACME-192-0-2-250 (master): 6043 row(s).",
        "Paste the SELECT statement now (single message), or attach a .sql file.",
        "[Daily Metrics Logging Summary]\nRun: 2026-07-31 07:31:31",
    ],
)
def test_a_header_without_a_verdict_is_left_alone(text):
    assert classify_message(text) == ""
    assert decorate_message(text) == text


def test_severity_is_read_from_the_header_not_the_body():
    """A JSON payload full of "error"/"running" must not decide the symbol."""
    message = (
        "LOGGING|host01|backup_restore.backup START: backup_id=X started.\n"
        "backup_id=X\n"
        '{"phase": "START", "stderr_tail": "", "error_tail": "", "session_status": "running"}'
    )

    assert classify_message(message) == "started"


def test_a_continuation_chunk_is_not_guessed_at():
    """`[part 2/2]` starts mid-body; the first chunk carries the report's own header."""
    first = "[part 1/2]\n[Metrics Warning Report]\nRun: 2026-07-31 06:20:59"
    second = "[part 2/2]\n- LOCK_TRANSACTION_HOLDERS ... session_status=running, command=SELECT"

    assert classify_message(first) == "warning"
    assert decorate_message(first).startswith("⚠️ [part 1/2]")
    assert decorate_message(second) == second


# ---------------------------------------------------------------------------
# Idempotence: a producer that tags itself keeps its own tag
# ---------------------------------------------------------------------------
def test_a_message_that_already_leads_with_a_status_emoji_is_untouched():
    sla = "❌ SLA/SLO compliance: FAILED\nWindow end: 2026-07-30T23:45:59Z"

    assert decorate_message(sla) == sla
    assert decorate_message(decorate_message("ERROR|host01|Restore workflow FAILED.")) == (
        decorate_message("ERROR|host01|Restore workflow FAILED.")
    )


# ---------------------------------------------------------------------------
# The send path applies it, and still respects Telegram's 4096-char body limit
# ---------------------------------------------------------------------------
def test_send_message_tags_the_body_it_posts(monkeypatch):
    posted = {}

    def fake_call(*, bot_token, method_name, payload, api_url, timeout_seconds):
        posted.update(payload)
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr("db_ops.telegram.api.call_telegram_api", fake_call)

    send_message(bot_token="t", chat_id="-100", text="ERROR|host01|Restore workflow FAILED.")

    assert posted["text"].startswith("❌ ")


def test_an_over_long_message_is_split_and_the_tag_leads_the_first_part(monkeypatch):
    """An over-long body used to be clipped to 4096 with "…(truncated)"; it is now sent as
    several messages so nothing is dropped. The severity tag still belongs to the *first* part —
    it is a header, and repeating it on every part would read as several separate alerts."""
    posted = []

    def fake_call(*, bot_token, method_name, payload, api_url, timeout_seconds):
        posted.append(payload)
        return {"ok": True, "result": {"message_id": len(posted)}}

    monkeypatch.setattr("db_ops.telegram.api.call_telegram_api", fake_call)

    send_message(
        bot_token="t",
        chat_id="-100",
        text="CRITICAL|host01|Restore aborted.\n" + ("x" * 5000),
    )

    assert len(posted) > 1
    assert posted[0]["text"].startswith("🚨 ")
    assert all(len(part["text"]) <= 4096 for part in posted)
    # Every x survives the split — that is the whole point of splitting instead of clipping.
    assert sum(part["text"].count("x") for part in posted) == 5000
    assert not any(part["text"].startswith("🚨 ") for part in posted[1:])


def test_a_queued_row_keeps_its_declared_message_type_when_it_is_read_back(tmp_path):
    """The producer's `message_type` only decides anything if the SELECT that feeds the sender
    actually returns it.

    `insert_telegram_send_message` wrote the column, but neither
    `fetch_telegram_send_message` nor `fetch_pending_telegram_send_messages` listed it, so every
    row reached `send_message` declaring nothing and fell back to guessing from the header. The
    visible symptom: the /spbot_add_sql schedule prompt contains the word "timeout", so an
    ordinary question went out as "❌ Schedule? ...".
    """
    from db_ops.db import DbOpsStore
    from db_ops.telegram.send_queue import row_value

    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    prompt = ("Schedule? 'manual' = only when you run it with /spbot_run_sql_task, "
              "'default' = every 300s all day, or: from_hour to_hour repeat_interval timeout")
    send_id = store.insert_telegram_send_message(
        tlgchat_id="-100", message_text=prompt, message_type="plain")

    one = store.fetch_telegram_send_message(send_tlgmsg_id=send_id)
    pending = store.fetch_pending_telegram_send_messages()

    assert row_value(one, "message_type") == "plain"
    assert [row_value(row, "message_type") for row in pending] == ["plain"]

    # ... and that is what keeps the emoji off a message that merely mentions a timeout.
    assert classify_message(prompt) == "failed"          # the header guess still says failure
    assert decorate_message(prompt, row_value(one, "message_type")) == prompt


# ---------------------------------------------------------------------------
# Splitting an over-long body instead of clipping it
# ---------------------------------------------------------------------------
def test_a_body_inside_the_limit_is_sent_unchanged_as_one_message():
    """The common case must stay exactly as it was: one message, no part footer. A footer on
    every short alert would be noise on the overwhelming majority of what the bot sends."""
    from db_ops.lib.telegram_text import split_telegram_message

    assert split_telegram_message("short body") == ["short body"]


def test_a_split_breaks_on_line_boundaries_so_a_table_row_is_never_cut_in_half():
    """The body this exists for is a markdown table. Cutting mid-row would produce a line with
    the wrong number of pipes, which renders as a broken table rather than as data."""
    from db_ops.lib.telegram_text import split_telegram_message

    row = "| " + "c" * 60 + " |"
    parts = split_telegram_message("\n".join([row] * 200))

    assert len(parts) > 1
    for part in parts:
        body = [line for line in part.split("\n") if not line.startswith("[part ")]
        assert all(line == row for line in body)


def test_every_line_survives_the_split():
    """Splitting exists so that nothing is dropped; a split that loses a line is worse than the
    clip it replaced, because it looks complete."""
    from db_ops.lib.telegram_text import split_telegram_message

    lines = [f"row {n}" for n in range(1500)]
    parts = split_telegram_message("\n".join(lines))
    rejoined = [line for part in parts for line in part.split("\n")
                if not line.startswith("[part ")]

    assert rejoined == lines


def test_one_line_longer_than_the_whole_limit_is_hard_split_rather_than_dropped():
    """A single 10k-character line has no boundary to break on — a stdout tail printed without
    newlines, say. It still has to arrive."""
    from db_ops.lib.telegram_text import split_telegram_message

    parts = split_telegram_message("y" * 10000)

    assert len(parts) > 1
    assert all(len(part) <= 3900 for part in parts)
    assert sum(part.count("y") for part in parts) == 10000


def test_each_part_is_numbered_so_the_reader_knows_more_is_coming():
    from db_ops.lib.telegram_text import split_telegram_message

    parts = split_telegram_message("\n".join(f"row {n}" for n in range(1500)))

    assert parts[0].splitlines()[0] == f"[part 1/{len(parts)}]"
    assert parts[-1].splitlines()[0] == f"[part {len(parts)}/{len(parts)}]"


def test_the_reply_quote_goes_on_the_first_part_and_buttons_on_the_last(monkeypatch):
    """Quoting on every part would quote the original once per message; buttons belong where the
    reader ends up, which is the bottom."""
    from db_ops.telegram.api import send_message

    posted = []

    def fake_call(*, bot_token, method_name, payload, api_url, timeout_seconds):
        posted.append(payload)
        return {"ok": True, "result": {"message_id": len(posted)}}

    monkeypatch.setattr("db_ops.telegram.api.call_telegram_api", fake_call)

    send_message(bot_token="t", chat_id="-100", text="\n".join(f"row {n}" for n in range(1500)),
                 reply_to_message_id=77, reply_markup={"force_reply": True})

    assert len(posted) > 1
    assert [p for p in posted if "reply_to_message_id" in p] == [posted[0]]
    assert [p for p in posted if "reply_markup" in p] == [posted[-1]]


def test_the_recorded_message_id_is_the_first_part_not_the_last(monkeypatch):
    """The queue row stores this id and replies quote it. Pointing it at the tail would quote
    the end of the output instead of its beginning."""
    from db_ops.telegram.api import send_message

    posted = []

    def fake_call(*, bot_token, method_name, payload, api_url, timeout_seconds):
        posted.append(payload)
        return {"ok": True, "result": {"message_id": len(posted)}}

    monkeypatch.setattr("db_ops.telegram.api.call_telegram_api", fake_call)

    result = send_message(bot_token="t", chat_id="-100",
                          text="\n".join(f"row {n}" for n in range(1500)))

    assert len(posted) > 1
    assert result["result"]["message_id"] == 1
