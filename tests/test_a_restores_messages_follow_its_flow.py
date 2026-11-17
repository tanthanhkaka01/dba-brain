"""A restore's messages say each of its steps in words a person reads (0.39.0, 1.370; 0.40.0, 1.375).

The operator, 2026-11-13: *restore is like backup - it must message every step of its flow the same
way; restore has restore full -> diff -> log as well, a message for each.* And 2026-11-15: *step by step,
concrete* - eleven steps, confirmed 2026-11-16. These are their words (``restore_messages``, read by the
workflow alone); which step is said when, and to whom, is held in
``test_a_restores_messages_follow_its_steps_and_its_notify.py``.
"""

from __future__ import annotations

from dbabrain.backup_restore import events, restore_messages as M


def _text(fields, level="logging"):
    return events._format_telegram_message(level=level, message="the older words", metadata={
        "command": "restore-workflow", "restore_id": "R", **fields})


# --------------------------------------------------------------------------- #
# The formatter
# --------------------------------------------------------------------------- #
def test_a_restore_message_that_states_its_flow_is_rendered_as_one():
    text = _text(M.check_step("APP_TO_DRILL", event="done", checked=1, failed=0))

    first, *lines = text.splitlines()
    assert first.split("|", 2)[0] == "LOGGING" and first.endswith("APP_TO_DRILL · 8. Check done")
    assert lines == ["✅ 1 database(s) checked, 0 unusable"]
    assert "restore_mode=" not in text and "the older words" not in text


def test_a_restore_message_that_states_none_keeps_the_older_shape():
    """An event built elsewhere that states no flow reads as before."""
    text = events._format_telegram_message(level="logging", message="Restore R started.", metadata={
        "command": "restore-workflow", "phase": "START", "restore_id": "R"})

    assert "restore_id=R" in text and "restore_mode=" in text


# --------------------------------------------------------------------------- #
# Each step's words
# --------------------------------------------------------------------------- #
def test_the_start_says_from_where_to_where_to_which_point_and_the_steps_it_runs():
    plan = M.plan_line(target="DRILL-01", remote=True, from_repository=True, copy_only=False)
    fields = M.process_started("R", source="SRC-01", target="DRILL-01", moment="", plan=plan, from_repository=True)

    assert fields == {"flow_title": "R · 1. Restore process started",
                      "flow_lines": ["SRC-01 -> DRILL-01 · from the repository · to the newest backup",
                                     "steps: 2 copy -> DRILL-01 (decrypted from the repository), 3 metadata before, "
                                     "4 certificate, 5 full, 6 diff, 7 log, 8 check, 9 metadata after, "
                                     "10 plain files cleared, 11 done"]}
    assert "to 2026-11-12 08:30:00" in M.process_started("R", source="S", target="T", moment="2026-11-12 08:30:00",
                                                         plan="p")["flow_lines"][0]
    only = M.plan_line(target="T", remote=True, from_repository=False, copy_only=True)
    assert only == "steps: 2 copy -> T, 3-10 off (copy only - nothing restored), 11 done"


def test_the_copy_says_how_much_went_where_and_how_it_was_proven():
    done = M.copy_step("R", target="DRILL-01", event="done", transferred={
        "copied": 48, "bytes_copied": 109_576_192, "skipped": 3, "encrypted": True, "removed_absent_at_source": 2,
        "written_after_the_plan": ["x.trn"]})

    assert done["flow_title"] == "R · 2. Copy to DRILL-01 done"
    assert done["flow_lines"] == [
        "✅ 48 file(s) · 104.5 MB · decrypted from the repository's encrypted copies · bytes + SHA-256 proven · "
        "3 already there and proven · 2 removed (gone at the source)",
        "⚠ 1 file(s) written at the source after the plan were left there"]
    assert M.copy_step("R", target="T", event="start", from_repository=True, source="SRC")["flow_lines"] == [
        "from the repository: the encrypted copies of SRC, decrypted on the way"]


def test_each_full_and_diff_is_a_start_and_a_done_named_by_its_database():
    facts = {"database": "APPDB", "landing": "APPDB_COPY", "files": ["F.bak"], "finished": ["2026-11-12 04:35:48"]}

    assert M.chain_step("R", number="5.1", kind="full", event="start", facts=facts) == {
        "flow_title": "R · 5.1 Restore full APPDB_COPY started",
        "flow_lines": ["APPDB as APPDB_COPY: F.bak (backup finished 2026-11-12 04:35:48) - left restoring for the next step"]}
    assert M.chain_step("R", number="6.2", kind="diff", event="done", facts=facts, seconds=41)["flow_title"] == \
        "R · 6.2 Restore diff APPDB_COPY done"


def test_a_step_that_does_not_apply_is_said_with_why():
    assert M.none("R", 6, "no differential after the full") == {
        "flow_title": "R · 6. Restore diff: none", "flow_lines": ["- no differential after the full"]}


def test_the_metadata_and_the_certificate_are_said_either_way():
    assert M.metadata_step("R", number=3, report={"status": "OFF", "reason": "server_metadata is off"}) == {
        "flow_title": "R · 3. Instance metadata before the restore: not restored",
        "flow_lines": ["- server_metadata is off"]}
    failed = M.metadata_step("R", number=9, report={"ok": False, "error": "login failed"})
    assert failed["flow_title"] == "R · 9. Instance metadata after the restore FAILED"
    assert M.certificate_step("R", result={"status": "none"})["flow_lines"] == [
        "- none needed - the backup is not encrypted"]


def test_a_replay_that_warned_says_each_warning_not_only_its_word():
    # The 1.376 drill, 2026-11-16: "replayed onto the target · WARN" and nothing more.
    said = M.metadata_step("R", number=3, report={
        "ok": True, "status": "WARN", "target": "SQL-T",
        "warnings": ["replay.secrets: 1 secret reference(s) are not in the secret store; skipped linked_servers",
                     "replay.logins: 5 ok, 0 failed, 2 unsupported on this edition"]})
    assert said["flow_title"] == "R · 3. Instance metadata before the restore done"
    assert said["flow_lines"] == [
        "✅ replayed onto SQL-T · WARN",
        "⚠ replay.secrets: 1 secret reference(s) are not in the secret store; skipped linked_servers",
        "⚠ replay.logins: 5 ok, 0 failed, 2 unsupported on this edition"]


def test_a_check_that_found_an_unusable_database_is_not_titled_done():
    bad = M.check_step("R", event="done", checked=3, failed=1)

    assert bad["flow_title"] == "R · 8. Check FAILED"
    assert bad["flow_lines"] == ["❌ 3 database(s) checked, 1 unusable"]


def test_the_plain_files_and_the_retention_each_on_a_line():
    done = M.plain_step("R", target="DRILL-01", cleaned={
        "plain": {"plain_files": 48, "deleted": 48, "already_gone": 0, "bytes_deleted": 109_576_192},
        "pruned": {"pruned": 2, "retention_seconds": 86400}, "kept": 1})

    assert done["flow_title"] == "R · 10. Plain files cleared"
    assert done["flow_lines"] == ["✅ 48 of 48 plain file(s) deleted on DRILL-01 (104.5 MB)",
                                  "- staged backups past retention: 2 removed, 1 kept - not in the catalog"]


def test_a_plain_file_left_in_the_clear_is_a_cross_and_its_reason():
    done = M.plain_step("R", target="T", cleaned={
        "pruned": {"pruned": 0, "error": "listed in part"},
        "plain": {"plain_files": 2, "deleted": 1, "already_gone": 0, "bytes_deleted": 100,
                  "error": "1 decrypted file(s) could not be deleted"}})

    assert done["flow_title"] == "R · 10. Plain files cleared - NOT all"
    assert done["flow_lines"][0].startswith("❌ 1 of 2 plain file(s) deleted on T (100 bytes) - 1 decrypted")
    assert done["flow_lines"][1] == "❌ staged backups past retention: 0 removed - listed in part"


def test_the_end_says_what_went_in_what_the_check_found_and_where_it_stopped():
    done = M.process_done("R", status="done", seconds=75, warnings=["a piece was passed over"], restored={
        "restored": "APPDB: full F + 2 log(s)", "verified": "1 database(s) checked, 0 unusable",
        "landed": "APPDB: last backup applied ended 2026-11-12 09:00:05 (restored to the end of its chain)",
        "continued": "APPDB: continued after APPDB_LOG_07.trn", "plain_deleted": "48 of 48 decrypted file(s)"})

    assert done["flow_title"] == "R · 11. Restore process done with a warning"
    assert done["flow_lines"] == [
        "✅ restored: APPDB: full F + 2 log(s)", "✅ checked: 1 database(s) checked, 0 unusable",
        "✅ stopped: APPDB: last backup applied ended 2026-11-12 09:00:05 (restored to the end of its chain)",
        "↪ continued: APPDB: continued after APPDB_LOG_07.trn", "✅ plain files deleted: 48 of 48 decrypted file(s)",
        "⚠ a piece was passed over", "75 s"]


def test_a_failure_is_titled_failed_at_its_step_and_gives_the_reason():
    failed = M.process_done("R", status="error", failed_step="6",
                            error="R: step 2 of 3 failed - APPDB: diff D.bak: Msg 3201", seconds=9)

    assert failed == {"flow_title": "R · 11. Restore process FAILED at step 6",
                      "flow_lines": ["❌ R: step 2 of 3 failed - APPDB: diff D.bak: Msg 3201", "9 s"]}
    assert M.process_done("R", status="error", copy_only=True)["flow_title"] == "R · 11. Copy only process FAILED"


def test_a_copy_only_run_says_nothing_was_restored():
    done = M.process_done("R", status="done", copy_only=True)

    assert done["flow_title"] == "R · 11. Copy only process done"
    assert done["flow_lines"] == ["✅ the files are staged and proven on the target - nothing restored"]


def test_a_size_is_said_as_a_person_reads_it():
    assert [M.size_said(n) for n in (0, 512, 1024, 109_576_192, 5 * 1024 ** 3, None, "x")] == [
        "0 bytes", "512 bytes", "1.0 KB", "104.5 MB", "5.0 GB", "", ""]


def test_a_line_is_one_line_whatever_the_engine_wrote_in_it():
    fields = M.process_done("R", status="error", error="Msg 3201\n   cannot open\tbackup device")

    assert fields["flow_lines"] == ["❌ Msg 3201 cannot open backup device"]
