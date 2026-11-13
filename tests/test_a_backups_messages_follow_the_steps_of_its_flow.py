"""A backup's messages follow the steps of its flow, named by its backup_id (1.306, 2026-11-11).

Why it matters: the operator, 2026-11-11 - *backup and restore are many steps now, and the messages
do not follow them*. A day's messages were a START, an END, an offload message labelled
``restore_id=<unknown>``, a "released" message on its own in the next pass, a JSON tail, and
CRITICAL on every run for a file known gone two days before. The format approved the same day:

* one flow, its messages named by its ``backup_id`` (*the backup_id is enough*);
* step 1 the backup; steps 2-5 the repository - copy, encrypt, delete at the source, check - in
  one run, one message, each step a line saying what it did, of how many, where;
* a short step one line, a long step (a FULL, a large copy) a start and a done;
* a failed step named with why, the steps it stopped "not run";
* a file already known lost said once, never again on the next runs.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

from dbabrain.backup_restore import backup as B
from dbabrain.backup_restore import events
from dbabrain.backup_restore import offload as O
from tests.test_a_repository_folder_holds_exactly_the_proven_copies import (  # noqa: F401 - fixtures
    _entry, catalog, one_run, repo)


# --------------------------------------------------------------------------- #
# Named by the backup_id
# --------------------------------------------------------------------------- #
def test_an_offload_is_named_by_its_backup_id_never_an_unknown_restore_id():
    assert events.run_id_key("offload") == "backup_id" and events.run_id_key("backup") == "backup_id"
    assert events.resolve_run_id("offload", {"backup_id": "SRV_FULL"}) == ("backup_id", "SRV_FULL")
    assert events.run_id_key("restore-latest") == "restore_id"


def test_a_flow_message_is_its_title_and_its_steps_with_no_json():
    text = events._format_telegram_message(level="logging", message="ignored", metadata={
        "command": "offload", "backup_id": "SRV_FULL", "output": {"big": "x" * 500},
        "flow_title": "SRV_FULL · 2-5. Repository done", "flow_lines": ["✅ 2. Copy 1/1", "", "✅ 3. Encrypt 1/1"]})
    head, *lines = text.split("\n")
    assert head.startswith("LOGGING|") and head.endswith("|SRV_FULL · 2-5. Repository done")
    assert lines == ["✅ 2. Copy 1/1", "✅ 3. Encrypt 1/1"] and "{" not in text


# --------------------------------------------------------------------------- #
# Steps 2-5, one line each
# --------------------------------------------------------------------------- #
def _offload_entry(**over):
    values = dict(enabled=True, server_id="REPO", encrypt=True, delete_source=True, repository_check=True)
    values.update(over)
    return SimpleNamespace(backup_id="SRV_FULL", server_id="SRV", offload=SimpleNamespace(**values))


def test_a_flow_that_reached_its_end_says_each_step_of_it():
    result = {"copy": {"files": 13, "verified": 13, "failed": []},
              "encrypt": {"rows": 13, "encrypted": 13, "failed": [], "key_id": 2},
              "release": {"deleted": 13, "kept": 0, "refused": []},
              "repository_check": {"clean": True, "files": 13}}
    flow = O.offload_flow_lines(_offload_entry(), result, error="", status="done", key_until="2027-02-09T04:19:00Z")
    assert flow["flow_title"] == "SRV_FULL · 2-5. Repository done"
    assert flow["flow_lines"] == [
        "✅ 2. Copy 13/13 -> REPO · bytes + SHA-256 = server",
        "✅ 3. Encrypt 13/13 · age key #2 (encrypts until 2027-02-09) · decrypted back = server",
        "✅ 4. Delete at the source 13 on SRV, file by file",
        "✅ 5. Repository check: clean · 13 file(s)"]


def test_a_failed_step_is_named_and_the_steps_it_stopped_are_not_run():
    result = {"copy": {"files": 13, "verified": 12, "failed": ["APP_FULL.bak: FactMismatch"]}}
    flow = O.offload_flow_lines(_offload_entry(), result, error="OffloadRefused: the copy failed", status="error")
    assert flow["flow_title"].endswith("Repository ERROR")
    assert flow["flow_lines"][0].startswith("❌ 2. Copy 12/13") and "APP_FULL.bak: FactMismatch" in flow["flow_lines"][0]
    assert flow["flow_lines"][1:4] == ["- 3. Encrypt: not run", "- 4. Delete at the source: not run",
                                       "- 5. Repository check: not run"]
    assert flow["flow_lines"][-1] == "❌ OffloadRefused: the copy failed"


def test_a_step_off_for_the_entry_or_with_nothing_to_do_says_so():
    flow = O.offload_flow_lines(_offload_entry(encrypt=False, delete_source=False),
                                {"repository_check": {"clean": True, "skipped": True}}, error="", status="done")
    assert flow["flow_lines"] == ["- 2. Copy: nothing this pass", "- 3. Encrypt: off for this entry",
                                  "- 4. Delete at the source: off for this entry",
                                  "- 5. Repository check: next pass (another offload of this server is writing there)"]


def test_a_long_copy_has_its_own_start_and_a_short_one_does_not():
    assert O.is_long_copy([{"size_bytes": 2 * 1024 ** 3, "file_type": "FULL"}])
    assert O.is_long_copy([{"size_bytes": None, "file_type": "FULL_BASE"}]), "a base directory's size is unknown"
    assert not O.is_long_copy([{"size_bytes": 16 * 1024 ** 2, "file_type": "WAL"}])


def test_the_pass_that_copies_a_backup_also_releases_it_one_message_for_steps_2_to_5(catalog, repo, one_run,
                                                                                    monkeypatch):
    entry = _entry()
    entry = dataclasses.replace(entry, offload=dataclasses.replace(entry.offload, delete_source=True))
    released = []
    from dbabrain.backup_restore import repository_encryption as E

    monkeypatch.setattr(E, "is_encrypted", lambda _store, **_k: set())
    monkeypatch.setattr(E, "sealed_rows", lambda _store, **_k: {})
    monkeypatch.setattr(O, "offload_entry", lambda e, **k: {"files": 1, "verified": 1, "failed": []})
    monkeypatch.setattr(O, "release_sources",
                        lambda e, **k: released.append(k["rows"]) or {"deleted": 1, "kept": 0, "refused": []})
    summary = {"ran": 0, "succeeded": 0, "failed": 0}

    O._run_one(entry, rows=[{"file_id": 1, "size_bytes": 3, "file_type": "LOG"}], releasable=[], store=None,
               catalog=catalog, app_config=None, logger=None, data_dir=None, key=None, key_base64=None,
               sleep=lambda _s: None, summary=summary, refresh_release=lambda: [{"file_id": 1}])

    assert released == [[{"file_id": 1}]], "released in the pass that copied it, not the next"
    [event] = one_run.events
    assert event["command"] == "offload" and event["metadata"]["flow_title"] == "SRV01_MSSQL · 2-5. Repository done"
    assert "✅ 4. Delete at the source 1 on" in "\n".join(event["metadata"]["flow_lines"])


# --------------------------------------------------------------------------- #
# Step 1
# --------------------------------------------------------------------------- #
def _job(level="full", offload=True):
    return SimpleNamespace(backup_id="SRV_FULL", job=level, env={"BACKUP_LEVEL": level}, backup_dir="F:\\BK",
                           offload=SimpleNamespace(enabled=offload, server_id="REPO"))


def test_a_full_is_a_long_step_and_a_diff_or_a_log_is_not():
    assert B.is_long_level(_job("full")) and B.is_long_level(_job("0"))
    assert not B.is_long_level(_job("diff")) and not B.is_long_level(_job("log")) and not B.is_long_level(_job("incr"))


def test_the_backups_done_says_when_how_many_and_each_finding_on_its_own_line():
    flow = B.backup_flow_lines(
        _job(), result=SimpleNamespace(status="done", error_text=None), started_at="2026-11-11T04:17:02Z",
        finished_at="2026-11-11T04:19:28Z", catalog_summary={"files": 13, "verified": 13}, scrub={"checked": 20},
        anomalies=[], markers=[], server_metadata={"ok": False, "error": "Login failed for user 'sysbackupadmin'."})
    assert flow["flow_title"] == "SRV_FULL · 1. Backup full done"
    first, warning, nxt = flow["flow_lines"]
    assert "13 file(s)" in first and "13/13 hashed and verified" in first and "->" not in first
    assert warning.startswith("⚠ server metadata export failed: Login failed")
    assert nxt.startswith("next: 2. copy to REPO")


def test_a_failed_backup_says_why_and_offers_no_next_step():
    flow = B.backup_flow_lines(
        _job(), result=SimpleNamespace(status="error", error_text="pg_basebackup failed (level=INCR)"),
        started_at="2026-11-11T04:17:02Z", finished_at="2026-11-11T04:18:00Z", catalog_summary=None, scrub=None,
        anomalies=[], markers=[], server_metadata=None)
    assert flow["flow_title"] == "SRV_FULL · 1. Backup full ERROR"
    assert flow["flow_lines"][-1] == "❌ pg_basebackup failed (level=INCR)"


# --------------------------------------------------------------------------- #
# A file known lost is said once
# --------------------------------------------------------------------------- #
def test_a_file_already_found_missing_is_not_rechecked_on_every_later_run(catalog):
    sha = "a" * 64
    ids = [catalog.record_backup_file(server_id="SRV", backup_id="SRV_FULL", engine="sqlserver", file_name=n,
                                      file_type="FULL", file_path=f"/b/{n}", verdict="ok", size_bytes=1,
                                      checksum_after_backup=sha) for n in ("A.bak", "B.bak")]
    catalog.check_backup_file(ids[0], checksum_now=None, checked_by="scrub", by_run_id="1")   # found gone
    assert [r["file_name"] for r in catalog.files_to_check(backup_id="SRV_FULL")] == ["B.bak"]
