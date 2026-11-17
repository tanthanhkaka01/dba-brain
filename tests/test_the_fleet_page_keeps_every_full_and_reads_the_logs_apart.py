"""The fleet's backup & restore page holds every full, and each server's logs apart (0.41.0, 1.392-1.395).

The operator, 2026-11-17, reading the page: it was 11.8 MB and grew every day, and it showed only each
server's newest 500 files - on a server with a log every 15 minutes the last hours' logs, so the full
every restore of them needs was not on the page. Asked how the files should be laid out: *the logs in
a block of their own on the page, hidden by default, shown when clicked*.

What would go wrong unseen: a full pushed off the page again by its own logs; a server's logs left
in the page's embedded data (the size comes back); a copy read from the repository listed as a file of
the repository's own, a row per file per restore; a field the page never reads written for every file.
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
from dbabrain.reports import backup_restore_report as page

SHA = "a" * 64
NODE = shutil.which("node")
TEMPLATES = Path(page.__file__).resolve().parent / "templates"


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _backup(catalog, server, name, *, file_type, ended):
    return catalog.record_backup_file(
        server_id=server, database_name="APPDB", engine="sqlserver", file_name=name, file_type=file_type,
        file_path=f"/b/APPDB/{file_type}/{name}", host_path=f"/b/APPDB/{file_type}/{name}",
        checksum_after_backup=SHA, size_bytes=10, verdict="ok", engine_verify="passed", backup_ended_at=ended)


def _a_full_and_more_logs_than_the_page_held(catalog, server="SRV-A", logs=sections.ROW_LIMIT + 20):
    _backup(catalog, server, "APPDB_FULL_20261101_000000Z.bak", file_type="FULL", ended="2026-11-01T00:00:00Z")
    for n in range(logs):
        _backup(catalog, server, f"APPDB_LOG_{n:05d}.trn", file_type="LOG",
                ended=f"2026-11-02T{n // 60 % 24:02d}:{n % 60:02d}:00Z")


def _files_of(html: str) -> dict:
    line = next(line for line in html.splitlines() if line.startswith("const FILES = "))
    return json.loads(line[len("const FILES = "):].rstrip().rstrip(";"))


# --------------------------------------------------------------------------- #
# The catalog's two reads
# --------------------------------------------------------------------------- #
def test_the_catalog_reads_a_servers_logs_or_everything_else(catalog):
    _a_full_and_more_logs_than_the_page_held(catalog, logs=3)
    _backup(catalog, "SRV-A", "APPDB_DIFF_1.bak", file_type="DIFF", ended="2026-11-03T00:00:00Z")

    assert {r["file_type"] for r in catalog.server_backup_files("SRV-A", logs=True)} == {"LOG"}
    assert {r["file_type"] for r in catalog.server_backup_files("SRV-A", logs=False)} == {"FULL", "DIFF"}
    assert len(catalog.server_backup_files("SRV-A")) == 5, "neither: every file, as the server page reads"


def test_wal_and_archivelog_are_logs_too():
    assert all(sections.is_log({"file_type": t}) for t in ("LOG", "WAL", "ARCHIVELOG", "log"))
    assert not any(sections.is_log({"file_type": t}) for t in ("FULL", "DIFF", "FULL_BASE", "RMAN_L0", "CONTROLFILE", None))


# --------------------------------------------------------------------------- #
# The page and its log files
# --------------------------------------------------------------------------- #
def test_a_full_older_than_a_servers_newest_500_logs_is_on_the_page(tmp_path, catalog):
    _a_full_and_more_logs_than_the_page_held(catalog)

    page.build_page(sqlite_path=catalog.sqlite_path, output_dir=tmp_path, stamp="20261117T0100Z",
                    snapshot_date="2026-11-17")
    files = _files_of((tmp_path / "backup-restore.html").read_text(encoding="utf-8"))

    assert [f["type"] for f in files["files"]] == ["FULL"], "the full is there, and no log is embedded"
    assert files["logs"] == [{"server": "SRV-A", "file": "backup-restore-logs_srv-a.json",
                              "count": sections.ROW_LIMIT + 20}]


def test_each_servers_logs_are_a_file_of_their_own_beside_the_page_and_its_day(tmp_path, catalog):
    _a_full_and_more_logs_than_the_page_held(catalog, logs=4)
    _backup(catalog, "SRV-B", "APPDB_FULL_1.bak", file_type="FULL", ended="2026-11-01T00:00:00Z")

    page.build_page(sqlite_path=catalog.sqlite_path, output_dir=tmp_path, stamp="20261117T0100Z",
                    snapshot_date="2026-11-17")
    logs = json.loads((tmp_path / "backup-restore-logs_srv-a.json").read_text(encoding="utf-8"))

    assert [f["type"] for f in logs["files"]] == ["LOG"] * 4 and logs["servers"] == ["SRV-A"]
    assert (tmp_path / "20261117_backup-restore-logs_srv-a.json").exists(), "?date= reads that day's logs"
    assert not (tmp_path / "backup-restore-logs_srv-b.json").exists(), "a server with no log has no file"


def test_a_servers_log_file_holds_its_newest_thousand(tmp_path, catalog):
    _a_full_and_more_logs_than_the_page_held(catalog, logs=sections.LOG_ROW_LIMIT + 5)

    page.build_page(sqlite_path=catalog.sqlite_path, output_dir=tmp_path, stamp="s", snapshot_date="d")
    logs = json.loads((tmp_path / "backup-restore-logs_srv-a.json").read_text(encoding="utf-8"))

    assert logs["shown"] == sections.LOG_ROW_LIMIT == 1000
    assert sum(1 for f in logs["files"] if f.get("chain")) == sections.LOG_ROW_LIMIT, \
        "every log of the file carries its chain, not only the first 500"


# --------------------------------------------------------------------------- #
# What a row carries
# --------------------------------------------------------------------------- #
def test_what_the_pages_script_never_reads_is_not_written():
    row = {"repo": {"files": [{"name": "f", "serverPath": "C:\\x", "repoPath": "/r/x", "sha256": SHA}]},
           "lastRestore": {"at": "t", "proven": True, "sourcePath": "s", "targetPath": "t", "testedOk": True,
                           "pointInTime": "LATEST", "sha256SourceBefore": SHA, "sha256TargetAfter": SHA}}
    slim = sections.for_the_page({"files": [row], "shown": 1})["files"][0]

    assert slim["repo"]["files"] == [{"name": "f", "sha256": SHA}]
    assert slim["lastRestore"] == {"at": "t", "proven": True, "sha256TargetAfter": SHA}
    script = (TEMPLATES / "backup_files.js").read_text(encoding="utf-8")
    for unread in sections._UNREAD_REPO_MEMBER + sections._UNREAD_RESTORE:
        assert not re.search(rf"\.{unread}\b", script), f"the page reads {unread} - take it off the list"


def test_a_copy_read_from_the_repository_is_its_backups_and_not_a_row_of_its_own(catalog):
    full = _backup(catalog, "SRV-A", "APPDB_FULL_1.bak", file_type="FULL", ended="2026-11-01T00:00:00Z")
    run = catalog.start_restore_run(action="restore", source_server_id="REPO", target_server_id="LAB",
                                    copy_max_retry=3, restore_id="R")
    catalog.add_run_file(restore_run_id=run, file_id=full, source_server_id="REPO", target_server_id="LAB",
                         file_name="APPDB_FULL_1.bak", file_type="FULL", source_path="s", target_path="t",
                         checksum_after_backup=SHA, checksum_source_before_copy=SHA, size_source_bytes=10,
                         checksum_target_after_copy=SHA, copy_max_retry=3, verdict="copied")
    catalog.set_restore_run(run, phase="done")
    per_server = {s: (catalog.server_backup_files(s), catalog.server_restore_files(s))
                  for s in catalog.catalogued_servers()}

    fleet = sections.build_fleet(per_server)

    assert [(f["host"], f["kind"]) for f in fleet["files"]] == [("SRV-A", "backup")], \
        "no 'copied out' row under the repository"
    assert fleet["files"][0]["mark"]["target"] == "LAB", "the backup's own row says where it went"


# --------------------------------------------------------------------------- #
# The page's log block
# --------------------------------------------------------------------------- #
def test_the_log_block_is_closed_until_opened_and_reads_the_chosen_servers_file():
    html = (TEMPLATES / "backup_restore_report.html").read_text(encoding="utf-8")
    block = html[html.index("function logsBlock"):]
    assert '<details class="fold" id="logBackups">' in block, "closed: no `open`"
    assert "fetch(withDate(file" in block, "the chosen server's file, that day's when the page is dated"
    assert 'backupFilesBlock(data, {fleet: true, inner: true, idp: "lg"})' in block
    assert 'bindFileFilters(data.files, "lg")' in block, "its own ids: the page's table keeps its filters"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_two_tables_on_one_page_keep_their_own_ids_and_the_log_one_names_no_server_column():
    script = (TEMPLATES / "backup_files.js").read_text(encoding="utf-8")
    row = {"kind": "backup", "at": "2026-11-17T00:00:00Z", "host": "SRV-A", "database": "APPDB", "type": "LOG",
           "name": "x.trn", "sizeBytes": 1, "verdict": "ok", "engineVerify": "passed", "usedCount": 0,
           "restoredOk": 0, "files": None, "repo": None, "mark": None, "lastRestore": None, "virus": None}
    data = {"files": [row], "shown": 1, "types": ["LOG"], "servers": ["SRV-A"], "notOk": 0, "changed": 0,
            "missing": 0, "used": 0, "restoredOk": 0, "notProven": 0, "infected": 0}
    program = ('const esc = s => String(s ?? "");\nconst when = v => v ? String(v) : "";\n' + script
               + f"\nconst d = {json.dumps(data)};\n"
               + "process.stdout.write(JSON.stringify({main: backupFilesBlock(d, {fleet: true}),"
               + " logs: backupFilesBlock(d, {fleet: true, inner: true, idp: 'lg'})}));")
    done = subprocess.run([NODE, "-"], input=program, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    drawn = json.loads(done.stdout)
    main, logs = drawn["main"], drawn["logs"]
    assert 'id="bf-rows"' in main and "<th>Server</th>" in main and 'data-key="server"' in main
    assert 'id="lg-rows"' in logs and 'data-bf="lg"' in logs and 'id="lg-more-0"' in logs
    assert "<th>Server</th>" not in logs and 'data-key="server"' not in logs, "one server's logs"
    assert 'id="bf-' not in logs, "no id of the page's own table twice on the page"
