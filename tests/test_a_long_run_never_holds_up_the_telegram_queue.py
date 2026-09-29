"""A restore's message carries its result, not its work, and one long message never holds up the queue.

On 2026-09-26, eleven minutes into a soak, the Telegram workflow was killed at its 300 s timeout.
The first message in the queue was the END event of a 13-database ``restore-latest``: the event put
the command's whole output in the text - each database's statement three times, the skipped files at
three levels - 181,174 characters, sent as 49 parts. Telegram rate-limited the chat, the waits
added up past 300 s, and every message queued behind it waited for the next pass.

Two changes, each enough on its own for that run: the message summarises the output (the full output
stays on the run's ``job_runs`` row), and a sending pass stops starting messages well before the
daemon's timeout, leaving the rest queued.
"""

from __future__ import annotations

import json

from db_ops.backup_restore import events
from db_ops.telegram import send_queue


def _restore_latest_end(databases: int = 13) -> dict:
    statement = "RESTORE DATABASE [APPDB] FROM DISK = N'/import/APPDB/FULL/APPDB_FULL_1.bak' " * 40
    names = [f"DB{index:02d}" for index in range(databases)]
    skipped = [{"backup_file": f"/import/{name}/FULL/old_{n}.bak", "reason": "not_latest_full"}
               for name in names for n in range(12)]
    result = {name: "SUCCESS" for name in names}
    source = {
        "source_id": "ACME-192-0-2-10", "target_id": "ACME-192-0-2-40", "status": "SUCCESS",
        "overall_status": "SUCCESS", "databases_considered": databases,
        "per_database_restore_status": result, "per_database_error": {},
        "skipped_backups": skipped,
        "results": [{"database_name": name, "sql": statement, "command": [statement],
                     "steps": [{"step": "restore-full", "sql": statement}], "skipped_backups": skipped[:12]}
                    for name in names],
    }
    output = {"status": "SUCCESS", "overall_status": "SUCCESS", "sources_considered": 1,
              "per_database_restore_status": result, "skipped_backups": skipped,
              "selected_full_backup": [f"/import/{name}/FULL/new.bak" for name in names],
              "selected_log_backups": [], "sources": [source]}
    return {"command": "restore-latest", "phase": "END", "restore_id": "ACME_TO_DRILL",
            "restore_success": databases, "restore_total": databases, "output": output}


def test_the_end_of_a_thirteen_database_restore_fits_in_a_message_or_two():
    metadata = _restore_latest_end()
    assert len(json.dumps(metadata)) > 100_000, "the fixture is the size of the run that broke it"

    text = events._format_telegram_message(level="logging", message="finished status=SUCCESS", metadata=metadata)

    assert len(text) < 4_000
    assert "RESTORE DATABASE" not in text
    payload = json.loads(text.split("\n", 2)[2])
    assert payload["output"]["per_database_restore_status"]["DB12"] == "SUCCESS"
    assert payload["output"]["skipped_backups_count"] == 13 * 12
    assert payload["output"]["sources"][0]["per_database_restore_status"]["DB00"] == "SUCCESS"
    assert "results" not in payload["output"]["sources"][0]


def test_any_other_output_that_grows_is_cut_at_a_bound_and_says_where_the_rest_is():
    metadata = {"command": "backup", "phase": "END", "backup_id": "B1", "output": {"log": "x" * 50_000}}
    text = events._format_telegram_message(level="logging", message="done", metadata=metadata)
    assert len(text) < events.MAX_TELEGRAM_PAYLOAD_CHARS + 500
    assert "the whole output is on the run's job_runs row" in text


def test_the_queued_row_keeps_the_summary_and_the_run_keeps_the_whole_output():
    metadata = _restore_latest_end()
    queued = events.telegram_metadata(metadata)
    assert "sources" in queued["output"] and "results" not in queued["output"]["sources"][0]
    assert "results" in metadata["output"]["sources"][0], "the caller's copy - the job_runs row's - is untouched"


class _Queue:
    def __init__(self, rows: int) -> None:
        self.rows = [{"send_tlgmsg_id": number, "tlgchat_id": f"-100{number}"}
                     for number in range(1, rows + 1)]

    def fetch_pending_telegram_send_messages(self, limit: int, *, per_chat: int | None = None):
        return self.rows[:limit]


def test_a_pass_stops_starting_messages_before_the_daemon_would_kill_it(monkeypatch):
    now = [0.0]
    sent: list[int] = []

    def send_one(**kwargs):
        sent.append(kwargs["send_tlgmsg_id"])
        now[0] += 100  # one throttled multi-part message
        return {"send_tlgmsg_id": kwargs["send_tlgmsg_id"], "sent": 1, "failed": 0, "status": "sent"}

    monkeypatch.setattr(send_queue, "DbOpsStore", lambda path: _Queue(rows=5))
    monkeypatch.setattr(send_queue, "send_one_message", send_one)

    counts = send_queue.send_pending_messages(sqlite_path="x", bot_token="t", clock=lambda: now[0])

    assert sent == [1, 2]
    assert counts == {"read": 5, "sent": 2, "failed": 0, "deferred": 3, "paused_chats": 0}
    assert send_queue.SEND_BUDGET_SECONDS < 300
