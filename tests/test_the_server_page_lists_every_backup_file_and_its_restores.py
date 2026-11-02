"""The server page lists every backup file and every restore that used it, in one section (0.34.0, 1.217).

Why it matters: the operator asked, on 2026-10-29, for the server page to answer, file by file, what a
backup wrote and whether it can be trusted. That means its size in bytes, its checksum, the engine's
verification, whether it is still on disk and unchanged, and the virus scan. It also means whether
any restore has ever used the file, and with what result: when, onto which target, and whether it
was restored OK. Backup and restore go in one report, not two, filterable by file type and by "used
for a restore".

A restore marks each file it used, on the file's own row, and the mark follows the run to its end.
A file "restored OK" is one a restore copied intact, restored, and verified. A copy-only run proves a
copy, never a restore.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.reports import backup_catalog_sections as sections
from dbabrain.reports.server_report import build_payload

SHA_A, SHA_B = "a" * 64, "b" * 64
SERVER, TARGET = "ACME-192-0-2-250", "LAB-192-0-2-40"
TEMPLATE = Path(__file__).resolve().parents[1] / "dbabrain" / "reports" / "templates" / "server_report.html"


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _backup(catalog, name, *, file_type="FULL", ended="2026-10-29T01:00:00Z", sha=SHA_A, size=4096):
    return catalog.record_backup_file(
        server_id=SERVER, database_name="APPDB", engine="sqlserver", file_name=name, file_type=file_type,
        file_path=f"/b/APPDB/{file_type}/{name}", host_path=f"/opt/b/APPDB/{file_type}/{name}",
        checksum_after_backup=sha, size_bytes=size, verdict="ok", engine_verify="passed",
        backup_ended_at=ended)


def _use(catalog, file_id, name, *, action="restore", phase="done", verdict="copied", started=None,
         target=TARGET, file_type="FULL"):
    run = catalog.start_restore_run(action=action, source_server_id=SERVER, target_server_id=target,
                                    copy_max_retry=3, restore_id="APPDB_TO_LAB",
                                    **({"started_at": started} if started else {}))
    catalog.add_run_file(restore_run_id=run, file_id=file_id, source_server_id=SERVER, target_server_id=target,
                         file_name=name, file_type=file_type, source_path=f"/b/{name}", target_path=f"/t/{name}",
                         copy_max_retry=3, checksum_after_backup=SHA_A, checksum_source_before_copy=SHA_A,
                         size_source_bytes=4096, verdict="copying")
    run_file = catalog.run_files(run)[0]["run_file_id"]
    if verdict != "copying":
        catalog.set_run_file(run_file, verdict=verdict, copy_attempts=1,
                             **({"checksum_target_after_copy": SHA_A} if verdict in ("copied", "match", "recopied")
                                else {}))
    catalog.set_restore_run(run, phase=phase, ended_at="2026-10-29T03:00:00Z")
    return run


def _row(catalog, name):
    return next(r for r in catalog.server_backup_files(SERVER) if r["file_name"] == name)


# --------------------------------------------------------------------------- #
# Each restore marks the files it used, on their own rows
# --------------------------------------------------------------------------- #
def test_a_restore_marks_every_file_it_used_with_when_where_and_its_result(catalog):
    file_id = _backup(catalog, "APPDB_FULL_1.bak")
    assert _row(catalog, "APPDB_FULL_1.bak")["last_restore_run_id"] is None, "never used: no mark"

    run = _use(catalog, file_id, "APPDB_FULL_1.bak", started="2026-10-29T02:00:00Z")

    row = _row(catalog, "APPDB_FULL_1.bak")
    assert (row["last_restore_run_id"], row["last_restore_at"], row["last_restore_action"],
            row["last_restore_target_server_id"], row["last_restore_result"], row["last_restore_verdict"]) == (
        run, "2026-10-29T02:00:00Z", "restore", TARGET, "done", "copied")
    assert row["server_id"] == SERVER, "the source of the use is the file's own server"


def test_the_mark_follows_the_run_to_its_end(catalog):
    file_id = _backup(catalog, "APPDB_FULL_1.bak")
    run = catalog.start_restore_run(action="restore", source_server_id=SERVER, target_server_id=TARGET,
                                    copy_max_retry=3, phase="copying")
    catalog.add_run_file(restore_run_id=run, file_id=file_id, source_server_id=SERVER, target_server_id=TARGET,
                         file_name="APPDB_FULL_1.bak", file_type="FULL", source_path="/b/x", target_path="/t/x",
                         copy_max_retry=3, checksum_source_before_copy=SHA_A, verdict="copying")
    assert (_row(catalog, "APPDB_FULL_1.bak")["last_restore_result"],
            _row(catalog, "APPDB_FULL_1.bak")["last_restore_verdict"]) == ("copying", "copying"), \
        "marked the moment the run takes the file, not only when it ends"

    catalog.set_restore_run(run, phase="error", error_text="RESTORE failed")
    assert _row(catalog, "APPDB_FULL_1.bak")["last_restore_result"] == "error"


def test_an_older_run_finishing_late_never_covers_a_newer_runs_mark(catalog):
    file_id = _backup(catalog, "APPDB_FULL_1.bak")
    older = catalog.start_restore_run(action="restore", source_server_id=SERVER, target_server_id=TARGET,
                                      copy_max_retry=3, phase="copying")
    catalog.add_run_file(restore_run_id=older, file_id=file_id, source_server_id=SERVER, target_server_id=TARGET,
                         file_name="APPDB_FULL_1.bak", file_type="FULL", source_path="/b/x", target_path="/t/x",
                         copy_max_retry=3, checksum_source_before_copy=SHA_A, verdict="copying")
    newer = _use(catalog, file_id, "APPDB_FULL_1.bak", target="LAB-192-0-2-41")

    catalog.set_restore_run(older, phase="error")

    row = _row(catalog, "APPDB_FULL_1.bak")
    assert (row["last_restore_run_id"], row["last_restore_target_server_id"]) == (newer, "LAB-192-0-2-41")


def test_a_caller_cannot_write_the_mark_itself(catalog):
    from dbabrain.db.backup_catalog import BackupCatalogError

    file_id = _backup(catalog, "APPDB_FULL_1.bak")
    with pytest.raises(BackupCatalogError):
        catalog.set_backup_file(file_id, last_restore_result="done")


@pytest.mark.parametrize("action, phase, verdict, used, restored_ok", [
    ("restore", "done", "copied", 1, 1),
    ("restore", "done", "match", 1, 1),
    ("restore", "error", "copied", 1, 0),            # copied intact, but the RESTORE failed
    ("restore", "refused", "copy_failed_after_retries", 1, 0),
    ("copy_only", "done", "copied", 1, 0),            # a copy is proven, never a restore
])
def test_a_file_is_restored_ok_only_by_a_restore_that_finished(catalog, action, phase, verdict, used, restored_ok):
    file_id = _backup(catalog, "APPDB_FULL_1.bak")
    _use(catalog, file_id, "APPDB_FULL_1.bak", action=action, phase=phase, verdict=verdict)
    row = _row(catalog, "APPDB_FULL_1.bak")
    assert (row["used_count"], row["restored_ok_count"]) == (used, restored_ok)


# --------------------------------------------------------------------------- #
# One section: backups, their restores, and restores onto this server
# --------------------------------------------------------------------------- #
def _section(catalog, server=SERVER):
    return sections.build_files(catalog.server_backup_files(server), catalog.server_restore_files(server),
                                server_id=server)


def test_one_list_newest_first_with_every_files_size_in_bytes_checksum_and_restore(catalog):
    full = _backup(catalog, "APPDB_FULL_1.bak", ended="2026-10-28T01:00:00Z", size=61203447808)
    _backup(catalog, "APPDB_LOG_1.trn", file_type="LOG", ended="2026-10-29T01:15:00Z", sha=SHA_B, size=512)
    _use(catalog, full, "APPDB_FULL_1.bak")

    s = _section(catalog)

    assert [f["name"] for f in s["files"]] == ["APPDB_LOG_1.trn", "APPDB_FULL_1.bak"], "newest first"
    full_row = s["files"][1]
    assert full_row["sizeBytes"] == 61203447808 and full_row["sha256"] == SHA_A
    assert full_row["engineVerify"] == "passed" and full_row["path"] == "/opt/b/APPDB/FULL/APPDB_FULL_1.bak"
    assert full_row["mark"]["target"] == TARGET and full_row["mark"]["result"] == "done"
    assert full_row["lastRestore"]["testedOk"] and full_row["lastRestore"]["sha256TargetAfter"] == SHA_A
    assert (full_row["usedCount"], full_row["restoredOk"]) == (1, 1)
    assert s["files"][0]["mark"] is None and s["files"][0]["usedCount"] == 0
    assert s["types"] == ["FULL", "LOG"] and (s["used"], s["restoredOk"]) == (1, 1)
    assert not any(f["kind"] != "backup" for f in s["files"]), "a used backup is one row, not two"


def test_a_restore_onto_this_server_is_a_row_of_its_page_too(catalog):
    full = _backup(catalog, "APPDB_FULL_1.bak")
    _use(catalog, full, "APPDB_FULL_1.bak")

    s = _section(catalog, server=TARGET)

    assert [(f["kind"], f["name"], f["server"]) for f in s["files"]] == [
        ("restored_here", "APPDB_FULL_1.bak", SERVER)]
    assert s["files"][0]["restoredOk"] == 1 and s["files"][0]["sizeBytes"] == 4096


def test_a_copy_of_a_file_no_backup_catalogued_is_kept_on_its_source_page(catalog):
    _use(catalog, None, "APPDB_FULL_0.bak", verdict="copy_failed_after_retries", phase="refused")
    s = _section(catalog)
    assert [(f["kind"], f["name"]) for f in s["files"]] == [("restore_out", "APPDB_FULL_0.bak")]
    assert s["notProven"] == 1


def test_the_page_payload_carries_the_one_section(catalog):
    _backup(catalog, "APPDB_FULL_1.bak")
    payload = build_payload([], [], server_id=SERVER, backup_file_rows=catalog.server_backup_files(SERVER),
                            restore_file_rows=catalog.server_restore_files(SERVER))
    assert payload["backupFiles"]["shown"] == 1 and "restoreFiles" not in payload


# --------------------------------------------------------------------------- #
# The section as the browser draws it
# --------------------------------------------------------------------------- #
NODE = shutil.which("node")


#: The section's code, shared by the server page and the fleet page since backup-restore.html.
SHARED_JS = TEMPLATE.parent / "backup_files.js"


def _render(section: dict, *, fleet: bool = False) -> str:
    script = (
        'const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;"}[c]));\n'
        'const when = v => v ? String(v) : "";\n'
        + SHARED_JS.read_text(encoding="utf-8")
        + f"\nprocess.stdout.write(backupFilesBlock({json.dumps(section)}, {{fleet: {str(fleet).lower()}}}));\n")
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_section_draws_sizes_in_bytes_and_both_filters(catalog):
    full = _backup(catalog, "APPDB_FULL_1.bak", size=61203447808)
    _backup(catalog, "APPDB_LOG_1.trn", file_type="LOG", ended="2026-10-29T02:00:00Z", size=512)
    _use(catalog, full, "APPDB_FULL_1.bak")

    page = _render(_section(catalog))

    assert "61,203,447,808 B" in page and "512 B" in page, "sizes are exact bytes"
    assert 'data-key="type"' in page and "<option>FULL</option>" in page and "<option>LOG</option>" in page
    assert 'data-key="use"' in page and 'value="ok"' in page and 'value="never"' in page
    assert re.search(r'data-type="FULL" data-use="ok"', page) and re.search(r'data-type="LOG" data-use="never"', page)
    assert "RESTORE OK" in page and TARGET in page
    assert "Backup &amp; restore files" in page and page.count("<table") == 1, "one report, not two"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_an_empty_catalog_draws_nothing(catalog):
    assert _render(_section(catalog)) == ""
