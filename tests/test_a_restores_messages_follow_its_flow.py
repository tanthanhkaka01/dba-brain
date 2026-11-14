"""A restore's messages follow its flow, in the format a backup's have (0.39.0, 1.370).

The operator, 2026-11-11: *backup and restore are many steps now and the messages do not follow
them* - the backup's format was approved that day (1.306). On 2026-11-13: *restore is like backup -
it must message every step of its flow the same way; restore has restore full -> diff -> log as
well, a message for each.*

A script-driven restore's Telegram messages were still a fixed header and ``key=value`` lines. They
state their flow now - the restore_id and the step on the first line, one line per step, no JSON -
and the formatter renders a message that states a flow as one, whatever command it belongs to.
"""

from __future__ import annotations

import pytest

from dbabrain.backup_restore import events, restore_messages as M


def _text(fields, level="logging"):
    return events._format_telegram_message(level=level, message="the older words", metadata={
        "command": "restore-workflow", "restore_id": "R", **fields})


# --------------------------------------------------------------------------- #
# The formatter
# --------------------------------------------------------------------------- #
def test_a_restore_message_that_states_its_flow_is_rendered_as_one():
    text = _text(M.check_done("APP_TO_DRILL", checked=1, failed=0))

    first, *lines = text.splitlines()
    assert first.split("|", 2)[0] == "LOGGING" and first.endswith("APP_TO_DRILL · 3. Check done")
    assert lines == ["✅ 3. Check 1 database(s) checked, 0 unusable"]
    assert "restore_mode=" not in text and "the older words" not in text


def test_a_restore_message_that_states_none_keeps_the_older_shape():
    """The share-driven path's events are built elsewhere and state no flow: they read as before."""
    text = events._format_telegram_message(level="logging", message="Restore R started.", metadata={
        "command": "restore-workflow", "phase": "RESTORE_START", "restore_id": "R"})

    assert "restore_id=R" in text and "restore_mode=" in text


# --------------------------------------------------------------------------- #
# Each step's words
# --------------------------------------------------------------------------- #
def test_the_start_says_from_where_to_where_to_which_point_and_what_comes_next():
    fields = M.started("R", source="SRC-01", target="DRILL-01", moment="", from_repository=True)

    assert fields == {"flow_title": "R · Restore started",
                      "flow_lines": ["SRC-01 -> DRILL-01 · from the repository · to the newest backup",
                                     "next: 1. copy, 2. restore full -> diff -> log, 3. check, 4. clean up"]}
    assert "to 2026-11-12 08:30:00" in M.started("R", source="S", target="T", moment="2026-11-12 08:30:00")["flow_lines"][0]
    only = M.started("R", source="S", target="", moment="", copy_only=True)
    assert only["flow_title"] == "R · Copy only started" and only["flow_lines"] == [
        "S -> S · to the newest backup", "next: 1. copy - nothing is restored"]


def test_the_copy_says_how_much_went_where_and_how_it_was_proven():
    done = M.copy_done("R", {"copied": 48, "bytes_copied": 109_576_192, "skipped": 3, "encrypted": True,
                             "removed_absent_at_source": 2, "written_after_the_plan": ["x.trn"]}, target="DRILL-01")

    assert done["flow_title"] == "R · 1. Copy done"
    assert done["flow_lines"] == [
        "✅ 1. Copy 48 file(s) · 104.5 MB -> DRILL-01 · decrypted from the repository's encrypted copies · "
        "bytes + SHA-256 proven · 3 already there and proven · 2 removed (gone at the source)",
        "⚠ 1 file(s) written at the source after the plan were left there"]
    plain = M.copy_done("R", {"copied": 2, "bytes_copied": 512, "skipped": 0}, target="T")
    assert plain["flow_lines"] == ["✅ 1. Copy 2 file(s) · 512 bytes -> T · bytes + SHA-256 proven"]
    assert M.copy_started("R", source="SRC", target="T", from_repository=True)["flow_lines"] == [
        "the repository's copies of SRC -> T"]


@pytest.mark.parametrize("op, name", [("restore-full", "full"), ("restore-diff", "diff"), ("restore-log", "log")])
def test_each_step_of_the_chain_is_titled_by_what_it_is_and_its_place(op, name):
    fields = M.step_done("R", op=op, number=2, total=3, said="APPDB as APPDB_COPY: the backup", seconds=41)

    assert fields == {"flow_title": f"R · 2. Restore {name} done (2 of 3)",
                      "flow_lines": ["✅ APPDB as APPDB_COPY: the backup · 41 s"]}


def test_the_restores_start_names_the_plan_and_what_it_takes_up():
    fields = M.restore_started("R", plan="APPDB: 2 log(s)", moment="to the newest backup",
                               continued="APPDB: continued after APPDB_LOG_07.trn")

    assert fields["flow_title"] == "R · 2. Restore started"
    assert fields["flow_lines"] == ["APPDB: 2 log(s) · to the newest backup",
                                    "↪ APPDB: continued after APPDB_LOG_07.trn"]
    assert M.restore_started("R", plan="", moment="to the newest backup")["flow_lines"] == [
        "nothing more to apply · to the newest backup"]


def test_a_check_that_found_an_unusable_database_is_not_titled_done():
    bad = M.check_done("R", checked=3, failed=1)

    assert bad["flow_title"] == "R · 3. Check FAILED"
    assert bad["flow_lines"] == ["❌ 3. Check 3 database(s) checked, 1 unusable"]


def test_the_clean_up_says_the_plain_files_and_the_retention_each_on_a_line():
    done = M.cleanup_done("R", target="DRILL-01",
                          plain={"plain_files": 48, "deleted": 48, "already_gone": 0, "bytes_deleted": 109_576_192},
                          pruned={"pruned": 2, "retention_seconds": 86400}, kept=1)

    assert done["flow_title"] == "R · 4. Clean up done"
    assert done["flow_lines"] == ["✅ 4. Clean up: plain files 48/48 deleted on DRILL-01 (104.5 MB)",
                                  "- staged backups past retention: 2 removed, 1 kept - not in the catalog"]


def test_a_plain_file_left_in_the_clear_is_a_cross_and_its_reason():
    done = M.cleanup_done("R", target="T", pruned={"pruned": 0, "error": "listed in part"},
                          plain={"plain_files": 2, "deleted": 1, "already_gone": 0, "bytes_deleted": 100,
                                 "error": "1 decrypted file(s) could not be deleted"})

    assert done["flow_lines"][0].startswith("❌ 4. Clean up: plain files 1/2 deleted on T (100 bytes) - 1 decrypted")
    assert done["flow_lines"][1] == "❌ staged backups past retention: 0 removed - listed in part"


def test_a_restore_that_decrypted_nothing_says_so():
    done = M.cleanup_done("R", target="T", plain=None, pruned={"pruned": 0, "skipped": "retention disabled"})

    assert done["flow_lines"] == ["- 4. Clean up: nothing was decrypted for this restore",
                                  "- staged backups past retention: 0 removed (retention disabled)"]


def test_the_end_says_what_went_in_what_the_check_found_and_where_it_stopped():
    done = M.finished("R", status="done", seconds=75, warnings=["a piece was passed over"], restored={
        "restored": "APPDB: full F + 2 log(s)", "verified": "1 database(s) checked, 0 unusable",
        "landed": "APPDB: last backup applied ended 2026-11-12 09:00:05 (restored to the end of its chain)",
        "continued": "APPDB: continued after APPDB_LOG_07.trn", "plain_deleted": "48 of 48 decrypted file(s)"})

    assert done["flow_title"] == "R · Restore done with a warning"
    assert done["flow_lines"] == [
        "✅ restored: APPDB: full F + 2 log(s)", "✅ checked: 1 database(s) checked, 0 unusable",
        "✅ stopped: APPDB: last backup applied ended 2026-11-12 09:00:05 (restored to the end of its chain)",
        "↪ continued: APPDB: continued after APPDB_LOG_07.trn", "✅ plain files deleted: 48 of 48 decrypted file(s)",
        "⚠ a piece was passed over", "75 s"]


def test_a_failure_is_titled_failed_and_gives_the_reason():
    failed = M.finished("R", status="error", error="R: step 2 of 3 failed - APPDB: diff D.bak: Msg 3201", seconds=9)

    assert failed == {"flow_title": "R · Restore FAILED",
                      "flow_lines": ["❌ R: step 2 of 3 failed - APPDB: diff D.bak: Msg 3201", "9 s"]}
    assert M.finished("R", status="error", copy_only=True)["flow_title"] == "R · Copy only FAILED"


def test_a_copy_only_run_says_nothing_was_restored():
    done = M.finished("R", status="done", copy_only=True)

    assert done["flow_title"] == "R · Copy only done"
    assert done["flow_lines"] == ["✅ the files are staged and proven on the target - nothing restored"]


# --------------------------------------------------------------------------- #
# A short step one line, a long step a start and a done
# --------------------------------------------------------------------------- #
def test_only_the_closing_line_of_the_restore_and_the_clean_ups_start_stay_off_telegram():
    off = {name: fields.get("telegram") is False for name, fields in {
        "copy start": M.copy_started("R", source="S", target="T", from_repository=False),
        "restore start": M.restore_started("R", plan="p", moment="m"),
        "restore closing": M.restore_done("R", steps=3, seconds=9),
        "check start": M.check_started("R"),
        "clean-up start": M.cleanup_started("R"),
        "a step": M.step_done("R", op="restore-full", number=1, total=1, said="s", seconds=1)}.items()}

    assert off == {"copy start": False, "restore start": False, "restore closing": True, "check start": False,
                   "clean-up start": True, "a step": False}


def test_a_size_is_said_as_a_person_reads_it():
    assert [M.size_said(n) for n in (0, 512, 1024, 109_576_192, 5 * 1024 ** 3, None, "x")] == [
        "0 bytes", "512 bytes", "1.0 KB", "104.5 MB", "5.0 GB", "", ""]


def test_a_line_is_one_line_whatever_the_engine_wrote_in_it():
    fields = M.finished("R", status="error", error="Msg 3201\n   cannot open\tbackup device")

    assert fields["flow_lines"] == ["❌ Msg 3201 cannot open backup device"]
