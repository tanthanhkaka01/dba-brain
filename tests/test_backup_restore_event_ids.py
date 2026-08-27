"""Every backup/restore message names the run it belongs to.

A Telegram alert that says a certificate import finished but not *which restore* it was
part of cannot be acted on: the operator cannot find the config entry, the `job_runs` row,
or the log file. So `backup_id` (backup jobs) or `restore_id` (everything restore-side) is
required in the message text, in the queued Telegram row, and in the event metadata —
enforced at the one choke point every message goes through, `emit_backup_restore_event`.
"""

import json
import sqlite3

import pytest

from db_ops.backup_restore import events
from db_ops.backup_restore.events import emit_backup_restore_event, resolve_run_id, run_id_key


class _Config:
    def __init__(self, sqlite_path):
        self.sqlite_path = str(sqlite_path)



    @property
    def store(self):
        """SQLite store declaration matching this fake's sqlite_path (see db_ops.config)."""
        from db_ops.config import SqliteStoreConfig, StoreConfig
        from pathlib import Path as _Path

        return StoreConfig(sqlite=SqliteStoreConfig(path=_Path(str(self.sqlite_path))))

@pytest.fixture()
def queued(tmp_path, monkeypatch):
    """Emit events into a temp store with Telegram routing on; return the queued rows."""
    monkeypatch.setattr(events, "alert_chat_id", lambda level: 42, raising=False)
    monkeypatch.setattr("db_ops.lib.telegram_route.telegram_route",
                        lambda level, **_: {"enabled": True, "alert": True, "chat_id": 42})
    config = _Config(tmp_path / "runtime.sqlite")

    def _emit(**kwargs):
        emit_backup_restore_event(app_config=config, **kwargs)
        with sqlite3.connect(config.sqlite_path) as conn:
            return conn.execute(
                "SELECT message_text, source_id FROM telegram_send_messages ORDER BY send_tlgmsg_id DESC LIMIT 1"
            ).fetchone()

    return _emit


# ---------------------------------------------------------------------------
# Which id identifies which command
# ---------------------------------------------------------------------------
def test_a_backup_job_is_keyed_by_backup_id_and_everything_else_by_restore_id():
    assert run_id_key("backup") == "backup_id"
    # Sub-commands keep their parent's key: "backup.start" is still a backup.
    assert run_id_key("backup.start") == "backup_id"
    for command in ("restore-latest", "restore-latest.certificate", "restore-workflow",
                    "copy-backup", "delete-backup", "verify-restore"):
        assert run_id_key(command) == "restore_id", command


def test_the_id_is_found_however_the_caller_spelled_it():
    """Single entry, several entries, or only inside the per-entry lists a workflow carries —
    all three are ways an existing caller already passes ids."""
    assert resolve_run_id("restore-latest", {"restore_id": "R1"}) == ("restore_id", "R1")
    assert resolve_run_id("restore-workflow", {"restore_ids": ["R1", "R2"]}) == ("restore_id", "R1,R2")
    assert resolve_run_id(
        "restore-workflow",
        {"mappings": [{"restore_id": "R1"}, {"restore_id": "R2"}, {"restore_id": "R1"}]},
    ) == ("restore_id", "R1,R2")           # de-duplicated, order kept
    assert resolve_run_id("backup", {"backup_id": "B7"}) == ("backup_id", "B7")


def test_an_event_that_cannot_name_its_run_says_so_out_loud():
    """Silence is what made this a defect in the first place: a message with no id looked
    perfectly normal. `<unknown>` makes the gap visible in the alert itself."""
    assert resolve_run_id("restore-latest", {}) == ("restore_id", "<unknown>")
    assert resolve_run_id("restore-latest", {"source_id": "ACME-192-0-2-250"}) == ("restore_id", "<unknown>")


# ---------------------------------------------------------------------------
# What actually reaches Telegram
# ---------------------------------------------------------------------------
def test_the_certificate_step_names_its_restore_in_the_message_text(queued):
    """The reported case: certificate START/END arrived with source_id and target_id but no
    restore_id, so a scheduled run could not be traced from the alert."""
    text, source_id = queued(
        command="restore-latest.certificate",
        phase="START",
        level="logging",
        message="certificate import started source_id=ACME-192-0-2-250",
        metadata={
            "restore_id": "APPDB_PROD_DRILL",
            "source_id": "ACME-192-0-2-250",
            "target_id": "MSSQL2025-DOCKER-192-0-2-249",
        },
    )

    header = text.splitlines()[0]
    assert "restore_id=APPDB_PROD_DRILL" in header
    assert "restore_id=APPDB_PROD_DRILL" in text.splitlines()[1]   # own line, before the JSON
    # The queued row is traceable without parsing the text.
    assert source_id == "restore-latest.certificate:APPDB_PROD_DRILL"


def test_the_id_has_its_own_line_ahead_of_a_huge_payload(queued):
    """The id used to be reachable only inside the JSON payload, which was clipped at 3900
    characters — so it was cut off exactly on the noisiest, most urgent events.

    The clip is gone (`db_ops.telegram.api` splits an over-long body across messages instead of
    dropping its tail), so the payload now survives whole. The dedicated id line stays anyway:
    a line near the top lands in the first part, while an id buried in a long JSON blob lands in
    whichever part it happens to fall into — and nobody scrolls to part 3 for it."""
    text, _ = queued(
        command="restore-latest",
        phase="ERROR",
        level="critical",
        message="failed",
        metadata={"restore_id": "R1", "stdout_tail": "x" * 8000},
    )

    assert "restore_id=R1" in text.splitlines()[1]
    # Nothing is thrown away any more: the tail the operator needs is still in the body.
    #
    # The run of 8000, not a count of the letter. Counting made the assertion depend on the
    # machine: every event line carries the hostname, and CI's runner is `runnervmgx7h7`, which
    # contributes one `x` of its own. 8001 != 8000, on a build where nothing was wrong.
    assert "x" * 8000 in text


def test_a_backup_event_carries_its_backup_id(queued):
    text, source_id = queued(
        command="backup",
        phase="END",
        level="logging",
        message="Backup ACME_PG_LAB01_PRIMARY/wal finished: done (exit 0)",
        metadata={"backup_id": "ACME_PG_LAB01_PRIMARY", "job": "wal"},
    )

    assert "backup_id=ACME_PG_LAB01_PRIMARY" in text.splitlines()[0]
    assert source_id == "backup:ACME_PG_LAB01_PRIMARY"


def test_a_restore_workflow_message_leads_with_the_id_before_any_detail(queued):
    text, _ = queued(
        command="restore-workflow",
        phase="END",
        level="logging",
        message="finished",
        metadata={
            "restore_id": "APPDB_PROD_DRILL",
            "restore_mode": "LATEST",
            "mappings": [{"restore_id": "APPDB_PROD_DRILL", "source_id": "S", "target_id": "T"}],
            "status": "SUCCESS",
        },
    )

    lines = text.splitlines()
    assert lines[1] == "restore_id=APPDB_PROD_DRILL"        # first thing after the header
    assert lines[2].startswith("restore_mode=")


def test_a_multi_entry_workflow_lists_every_id_it_touched(queued):
    text, source_id = queued(
        command="restore-workflow",
        phase="START",
        level="logging",
        message="started restore_count=2",
        metadata={"restore_ids": ["R1", "R2"], "restore_mode": "LATEST", "restore_count": 2},
    )

    assert "restore_id=R1,R2" in text
    assert source_id == "restore-workflow:R1,R2"


def test_the_id_reaches_the_job_runs_row_too(tmp_path, monkeypatch):
    """The sqlite row is what a report or a later investigation reads; it must not depend on
    the caller having remembered to put the id in metadata."""
    monkeypatch.setattr("db_ops.lib.telegram_route.telegram_route",
                        lambda level, **_: {"enabled": True, "alert": False, "chat_id": ""})
    config = _Config(tmp_path / "runtime.sqlite")

    emit_backup_restore_event(
        app_config=config, command="restore-latest.certificate", phase="END", level="logging",
        message="certificate import finished", metadata={"restore_id": "R9", "source_id": "S"},
    )

    with sqlite3.connect(config.sqlite_path) as conn:
        message, metadata = conn.execute(
            "SELECT message, metadata_json FROM job_runs ORDER BY log_id DESC LIMIT 1"
        ).fetchone()

    assert "restore_id=R9" in message
    assert json.loads(metadata)["restore_id"] == "R9"


def test_the_id_is_not_repeated_when_the_message_already_names_it(queued):
    text, _ = queued(
        command="restore-latest",
        phase="START",
        level="logging",
        message="started restore_id=R1",
        metadata={"restore_id": "R1"},
    )

    assert text.splitlines()[0].count("restore_id=R1") == 1
