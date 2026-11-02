"""The fleet's backup & restore page lists every server's files on one page (0.34.0).

Asked for on 2026-10-30, beside Fleet inventory, Server metrics, SLA and Index usage. Each server
page lists its own files (1.217); a reader asking "which FULLs were never restore-tested" or "what
changed on disk" across the estate had to open every server. This page is one table for the fleet:
- each server's rows, tagged with the server;
- one copy counted once;
- drawn by the server page's own code;
- offered in every banner once it exists.
"""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import page_banner, webhost_endpoints
from dbabrain.reports import backup_catalog_sections as sections
from dbabrain.reports import backup_restore_report as page

SHA = "a" * 64
NODE = shutil.which("node")


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _backup(catalog, server, name, *, file_type="FULL", ended="2026-10-29T01:00:00Z"):
    return catalog.record_backup_file(
        server_id=server, database_name="APPDB", engine="sqlserver", file_name=name, file_type=file_type,
        file_path=f"/b/APPDB/{file_type}/{name}", host_path=f"/b/APPDB/{file_type}/{name}",
        checksum_after_backup=SHA, size_bytes=10, verdict="ok", engine_verify="passed", backup_ended_at=ended)


def _copied(catalog, file_id, name, *, source, target):
    run = catalog.start_restore_run(action="restore", source_server_id=source, target_server_id=target,
                                    copy_max_retry=3, restore_id="R")
    catalog.add_run_file(restore_run_id=run, file_id=file_id, source_server_id=source, target_server_id=target,
                         file_name=name, file_type="FULL", source_path="s", target_path="t",
                         checksum_after_backup=SHA, checksum_source_before_copy=SHA, size_source_bytes=10,
                         checksum_target_after_copy=SHA, copy_max_retry=3, verdict="copied")
    catalog.set_restore_run(run, phase="done")


def _per_server(catalog):
    return {s: (catalog.server_backup_files(s), catalog.server_restore_files(s)) for s in catalog.catalogued_servers()}


def test_every_server_with_a_file_or_a_copy_is_on_the_page(catalog):
    full = _backup(catalog, "SRV-A", "APPDB_FULL_1.bak")
    _copied(catalog, full, "APPDB_FULL_1.bak", source="SRV-A", target="LAB-B")
    assert catalog.catalogued_servers() == ["LAB-B", "SRV-A"], "a copy's target is on it too"


def test_each_row_says_its_server_and_one_copy_is_one_row(catalog):
    a = _backup(catalog, "SRV-A", "APPDB_FULL_1.bak")
    _backup(catalog, "SRV-B", "APPDB_LOG_1.trn", file_type="LOG", ended="2026-10-29T02:00:00Z")
    _copied(catalog, a, "APPDB_FULL_1.bak", source="SRV-A", target="LAB-B")

    fleet = sections.build_fleet(_per_server(catalog))

    assert [(f["host"], f["name"]) for f in fleet["files"]] == [
        ("SRV-B", "APPDB_LOG_1.trn"), ("SRV-A", "APPDB_FULL_1.bak")], "newest first, across servers"
    assert fleet["servers"] == ["SRV-A", "SRV-B"] and fleet["types"] == ["FULL", "LOG"]
    assert fleet["files"][1]["mark"]["target"] == "LAB-B", "the copy is on the source's row"
    assert not any(f["kind"] == "restored_here" for f in fleet["files"]), "not listed a second time on LAB-B"
    assert (fleet["used"], fleet["restoredOk"]) == (1, 1), "counted by the same summarize as a server's table"


def test_the_page_is_written_under_its_stable_name_and_its_day(tmp_path, catalog):
    _backup(catalog, "SRV-A", "APPDB_FULL_1.bak")

    written = page.build_page(sqlite_path=catalog.sqlite_path, output_dir=tmp_path / "reports",
                              stamp="20261030T0100Z", snapshot_date="2026-10-30 08:00:00 +07")

    assert written.endswith("backup-restore.html")
    html = (tmp_path / "reports" / "backup-restore.html").read_text(encoding="utf-8")
    assert (tmp_path / "reports" / "20261030_backup-restore.html").exists(), "?date= reaches it"
    assert "__" not in "".join(t for t in html.split('"') if t.startswith("__") and t.endswith("__")), html[:80]
    assert "APPDB_FULL_1.bak" in html and "function backupFilesBlock(b, opts = {})" in html
    assert "--crit-bg" in html, "the server page's own stylesheet, one source for both"


def test_a_catalog_that_cannot_be_opened_writes_no_page_so_no_banner_offers_one(tmp_path):
    assert page.build_page(sqlite_path=tmp_path / "no-such-dir" / "x" / "catalog.sqlite\0",
                           output_dir=tmp_path / "reports", stamp="s", snapshot_date="d") == ""
    assert not (tmp_path / "reports" / "backup-restore.html").exists()


def test_the_banner_offers_it_beside_the_others_only_once_it_is_published():
    labels = [label for label, _href in page_banner.SIBLING_PAGES]
    assert labels[-1] == "Backup & restore" and labels[:3] == ["Fleet inventory", "Server metrics", "SLA"]
    assert ("Backup & restore", "backup-restore.html") not in page_banner.siblings_present(lambda name: name != "backup-restore.html")
    assert webhost_endpoints.STABLE_PAGES["backup_restore"] == "backup-restore.html"


def test_the_fleet_page_is_built_before_the_pages_that_offer_it():
    from dbabrain.reports import inventory_report

    source = inspect.getsource(inventory_report)
    assert source.index("backup_restore_report.build_page(") < source.index("server_pages = build_server_pages("), \
        "the server page's banner looks for it on disk in the same run"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_fleet_table_has_a_server_column_and_filter_and_the_server_page_does_not(catalog):
    _backup(catalog, "SRV-A", "APPDB_FULL_1.bak")
    fleet = sections.build_fleet(_per_server(catalog))
    one = sections.build_files(catalog.server_backup_files("SRV-A"), catalog.server_restore_files("SRV-A"),
                               server_id="SRV-A")
    shared = (Path(page.BACKUP_FILES_JS)).read_text(encoding="utf-8")

    def draw(table, fleet_mode):
        script = ('const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;"}[c]));\n'
                  'const when = v => v ? String(v) : "";\n' + shared
                  + f"\nprocess.stdout.write(backupFilesBlock({json.dumps(table)}, {{fleet: {str(fleet_mode).lower()}}}));")
        done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert done.returncode == 0, done.stderr
        return done.stdout

    fleet_html, server_html = draw(fleet, True), draw(one, False)
    assert "<th>Server</th>" in fleet_html and 'data-key="server"' in fleet_html and 'data-key="bad"' in fleet_html
    assert 'data-server="SRV-A"' in fleet_html and fleet_html.startswith('<section class="files" id="backupFiles">')
    assert "<th>Server</th>" not in server_html and server_html.startswith('<details class="fold" id="backupFiles"')
    # A Database filter on both pages (the operator, 2026-10-30), its choices the databases on the page.
    for html in (fleet_html, server_html):
        assert 'data-key="db"' in html and "<option>APPDB</option>" in html and 'data-db="APPDB"' in html


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_each_backup_file_says_whether_it_is_encrypted_and_unknown_is_not_no(catalog):
    """The operator, 2026-10-30: show whether each backup is encrypted. A file whose script said
    nothing (written before the scripts did, or catalogued by catalog-backfill) is "not recorded" -
    reading it as "no" would claim what nobody checked."""
    for name, flag in (("A_FULL_1.bak", 1), ("A_FULL_2.bak", 0), ("A_FULL_3.bak", None)):
        catalog.record_backup_file(
            server_id="SRV-A", database_name="APPDB", engine="sqlserver", file_name=name, file_type="FULL",
            file_path=f"/b/{name}", checksum_after_backup=SHA, size_bytes=10, verdict="ok", encrypted=flag)
    table = sections.build_files(catalog.server_backup_files("SRV-A"), [], server_id="SRV-A")
    assert {f["name"]: f["encrypted"] for f in table["files"]} == {
        "A_FULL_1.bak": True, "A_FULL_2.bak": False, "A_FULL_3.bak": None}
    shared = Path(page.BACKUP_FILES_JS).read_text(encoding="utf-8")
    script = ('const esc = s => String(s ?? "");\nconst when = v => v ? String(v) : "";\n' + shared
              + f"\nprocess.stdout.write(backupFilesBlock({json.dumps(table)}, {{}}));")
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    html = done.stdout
    assert "<th>Encrypted</th>" in html
    assert 'sev ok">yes<' in html and 'sev warn">no<' in html and 'sev">not recorded<' in html


def test_the_server_page_header_goes_to_its_files():
    html = Path(page.SERVER_TEMPLATE).read_text(encoding="utf-8")
    assert 'id="backupFilesLink" href="#backupFiles"' in html and "showBackupFilesLink(d.backupFiles)" in html
    assert "__BACKUP_FILES_JS__" in html, "the table's code is the shared file's, injected"
