"""A backup file's restore column says what happened to THAT file, and how many restores were done and failed (0.39.0).

The operator, 2026-11-14, reading the backup page: a LOG of 100.197 showed ``error`` beside its last
restore. That run had stopped at its first file - a lockdown refused the full's REPLACE - and never
reached the log, whose copy was proven; the page printed the run's bare phase as the file's. Asked
what to show instead: *restore done, fail with its reason ...* - and *how many done, how many failed*.

So the column reads the file's own line of its last restore's chain (``restore_chain_files``) and the
run's reason: ``restore done``; ``restore fail`` saying whether the file failed itself, went in before
the run failed, or was never reached; ``skipped`` with the engine's reason - and under it
``used N× · restore done D · fail F``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.reports import backup_catalog_sections as sections

SHA = "a" * 64
SERVER, TARGET = "ACME-192-0-2-197", "LAB-192-0-2-136"
LOCKDOWN = ("step 1 of 80 failed - APP: full APP_FULL_1.bak: restore-full failed: [APP] exists on this server "
            "(state ONLINE); a full restore would replace it. Nothing was restored.")
SHARED_JS = Path(__file__).resolve().parents[1] / "dbabrain" / "reports" / "templates" / "backup_files.js"
NODE = shutil.which("node")


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _backup(catalog, name, file_type="LOG"):
    return catalog.record_backup_file(
        server_id=SERVER, database_name="Tpro_Stag", engine="sqlserver", file_name=name, file_type=file_type,
        file_path=f"/b/{name}", checksum_after_backup=SHA, size_bytes=40960, verdict="ok",
        backup_ended_at="2026-11-14T02:42:10Z")


def _restore(catalog, file_id, name, *, phase, chain=None, error_text="", started):
    """One restore that used the file: its copy proven, its chain line (``chain`` = the file's result,
    or None for a run with no chain), and how the run ended."""
    run = catalog.start_restore_run(action="restore", restore_id="TPRO_TO_LAB", source_server_id=SERVER,
                                    target_server_id=TARGET, copy_max_retry=3, started_at=started)
    catalog.add_run_file(restore_run_id=run, file_id=file_id, source_server_id=SERVER, target_server_id=TARGET,
                         file_name=name, file_type="LOG", source_path=f"/b/{name}", target_path=f"/t/{name}",
                         copy_max_retry=3, checksum_after_backup=SHA, checksum_source_before_copy=SHA,
                         checksum_target_after_copy=SHA, size_source_bytes=40960, size_target_bytes=40960,
                         verdict="match")
    if chain is not None:
        catalog.plan_chain(run, [
            {"source_server_id": SERVER, "target_server_id": TARGET, "database_name": "APP", "restored_as": "APP",
             "step": "restore-full", "file_name": "APP_FULL_1.bak", "file_path": "/t/APP_FULL_1.bak"},
            {"source_server_id": SERVER, "target_server_id": TARGET, "database_name": "Tpro_Stag",
             "restored_as": "Tpro_Stag", "step": "restore-log", "file_id": file_id, "file_name": name,
             "file_path": f"/t/{name}"}])
        if chain == "not reached":
            catalog.set_chain_step(run, [1], result="failed", error_text=LOCKDOWN)
        elif chain in ("applied", "skipped", "failed"):
            catalog.set_chain_step(run, [1], result="applied")
            catalog.set_chain_step(run, [2], result=chain,
                                   error_text={"skipped": "Msg 4326: the log ends before the restore point",
                                               "failed": "Msg 3013: RESTORE LOG is terminating abnormally"}.get(chain, ""))
    catalog.set_restore_run(run, phase=phase, ended_at=started, **({"error_text": error_text} if error_text else {}))
    return run


def _file(catalog, name="Tpro_Stag_LOG_1.trn"):
    rows = sections.build_files(catalog.server_backup_files(SERVER), catalog.server_restore_files(SERVER),
                                server_id=SERVER)["files"]
    return next(f for f in rows if f["name"] == name)


# --------------------------------------------------------------------------- the case of the page
def test_a_file_a_failed_run_never_reached_says_restore_fail_and_that_it_was_not_reached(catalog):
    file_id = _backup(catalog, "Tpro_Stag_LOG_1.trn")
    for started in ("2026-11-14T04:51:49Z", "2026-11-14T05:01:17Z", "2026-11-14T05:10:21Z"):
        _restore(catalog, file_id, "Tpro_Stag_LOG_1.trn", phase="done", chain="applied", started=started)
    _restore(catalog, file_id, "Tpro_Stag_LOG_1.trn", phase="error", chain="not reached",
             error_text=f"RestoreByIdError: TPRO_TO_LAB: {LOCKDOWN}", started="2026-11-14T05:13:36Z")

    f = _file(catalog)

    outcome = f["mark"]["outcome"]
    assert (outcome["state"], outcome["label"]) == ("fail", "restore fail")
    assert outcome["reason"].startswith("the restore stopped before this file - RestoreByIdError")
    assert "step 1 of 80 failed" in outcome["reason"]
    assert (f["usedCount"], f["restoreDone"], f["restoreFail"]) == (4, 3, 1)


def test_the_next_restore_that_worked_says_restore_done_and_counts_it(catalog):
    file_id = _backup(catalog, "Tpro_Stag_LOG_1.trn")
    _restore(catalog, file_id, "Tpro_Stag_LOG_1.trn", phase="error", chain="not reached",
             error_text=LOCKDOWN, started="2026-11-14T05:13:36Z")
    _restore(catalog, file_id, "Tpro_Stag_LOG_1.trn", phase="done", chain="applied", started="2026-11-14T06:46:24Z")

    f = _file(catalog)

    assert f["mark"]["outcome"] == {"state": "done", "label": "restore done", "reason": ""}
    assert (f["restoreDone"], f["restoreFail"]) == (1, 1)


@pytest.mark.parametrize("chain, phase, state, label, reason_starts", [
    ("failed", "error", "fail", "restore fail", "Msg 3013"),                       # the file failed itself
    ("applied", "error", "fail", "restore fail", "this file went in; the restore failed after it"),
    ("skipped", "done", "skipped", "skipped", "Msg 4326"),                          # passed over, the restore ended
    (None, "error", "fail", "restore fail", "RestoreByIdError: the run's own reason"),  # a run with no chain
    (None, "done", "done", "restore done", ""),
])
def test_where_the_file_stood_in_its_last_restore_is_the_reason(catalog, chain, phase, state, label, reason_starts):
    file_id = _backup(catalog, "Tpro_Stag_LOG_1.trn")
    _restore(catalog, file_id, "Tpro_Stag_LOG_1.trn", phase=phase, chain=chain,
             error_text="RestoreByIdError: the run's own reason" if phase == "error" else "",
             started="2026-11-14T05:13:36Z")

    outcome = _file(catalog)["mark"]["outcome"]

    assert (outcome["state"], outcome["label"]) == (state, label)
    assert outcome["reason"].startswith(reason_starts)


@pytest.mark.parametrize("action, phase, label", [
    ("copy_only", "copied", "copy done"), ("copy_only", "error", "copy fail"),
    ("restore", "restoring", "restore running"), ("restore", "refused", "restore fail")])
def test_a_copy_and_a_run_still_going_are_said_as_such(action, phase, label):
    assert sections.restore_outcome(action=action, phase=phase)["label"] == label


def test_a_fail_with_no_recorded_reason_says_so_and_a_long_one_is_cut():
    assert sections.restore_outcome(action="restore", phase="error")["reason"] == "no reason was recorded"
    long = sections.restore_outcome(action="restore", phase="error", run_error="x " * 1000)["reason"]
    assert len(long) == sections.REASON_LIMIT and long.endswith("...")


def test_a_restore_onto_this_server_is_said_the_same_way(catalog):
    file_id = _backup(catalog, "Tpro_Stag_LOG_1.trn")
    _restore(catalog, file_id, "Tpro_Stag_LOG_1.trn", phase="error", chain="not reached",
             error_text=LOCKDOWN, started="2026-11-14T05:13:36Z")

    rows = sections.build_files(catalog.server_backup_files(TARGET), catalog.server_restore_files(TARGET),
                                server_id=TARGET)["files"]

    [here] = [f for f in rows if f["kind"] == "restored_here"]
    assert here["mark"]["outcome"]["label"] == "restore fail"
    assert here["mark"]["outcome"]["reason"].startswith("the restore stopped before this file")
    assert (here["restoreDone"], here["restoreFail"]) == (0, 1)


# --------------------------------------------------------------------------- as the browser draws it
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_page_says_restore_fail_its_reason_and_the_tally_never_the_bare_phase(catalog):
    file_id = _backup(catalog, "Tpro_Stag_LOG_1.trn")
    _restore(catalog, file_id, "Tpro_Stag_LOG_1.trn", phase="done", chain="applied", started="2026-11-14T04:51:49Z")
    _restore(catalog, file_id, "Tpro_Stag_LOG_1.trn", phase="error", chain="not reached",
             error_text=LOCKDOWN, started="2026-11-14T05:13:36Z")
    section = sections.build_files(catalog.server_backup_files(SERVER), catalog.server_restore_files(SERVER),
                                   server_id=SERVER)
    script = (
        'const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;"}[c]));\n'
        'const when = v => v ? String(v) : "";\n'
        + SHARED_JS.read_text(encoding="utf-8")
        + f"\nprocess.stdout.write(backupFilesBlock({json.dumps(section)}, {{fleet: false}}));\n")
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    page = done.stdout

    assert '<span class="sev crit">restore fail</span>' in page
    assert "the restore stopped before this file" in page
    assert "used 2× · restore done 1 · fail 1" in page
    assert ">error<" not in page, "the run's bare phase is not the file's word"
