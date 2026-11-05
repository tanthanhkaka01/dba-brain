"""The pages print each backup entry's retention and point-in-time reach (0.35.0, 1.233 - T3).

"Retention" to an operator means how far back a restore can reach, and with windows per file type
that is the shorter of the base and the log window - a number to print, not to infer. The reports
never read the backup app's configuration, so every backup run records it in the catalog
(``backup_runs.retention_json``, ``pitr_reach_seconds``), and the backup & restore page and each
server page read the newest run's.
"""

from __future__ import annotations

import tests.test_every_backup_run_records_its_files_and_their_checksums as receipts
from dbabrain.backup_restore import backup as backup_module
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import cleanup_retention as R
from dbabrain.reports import backup_catalog_sections, backup_restore_report

DAY = 86400


def _catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _job_with(value, **over):
    import dataclasses

    job = receipts._job(**over)
    raw = {R.FIELD: value}
    return dataclasses.replace(job, cleanup_retention=R.parse(raw, context="t"),
                               retention=R.parse_per_type(raw, context="t", engine=job.db_type))


def _run(catalog, job, n):
    backup_module.record_in_catalog(catalog, job, receipts._result(job, [receipts._line("APPDB", "FULL", f"/b/f{n}.bak")]),
                                    job_run_id=n, started_at=f"2026-10-2{n}T01:00:00Z", finished_at=f"2026-10-2{n}T01:10:00Z")


def test_each_run_records_its_retention_and_the_newest_is_what_is_shown(tmp_path):
    catalog = _catalog(tmp_path)
    _run(catalog, _job_with(8 * DAY), 1)
    _run(catalog, _job_with({"FULL": 14 * DAY, "LOG": 7 * DAY, "default": 14 * DAY}), 2)
    (row,) = catalog.retention_by_entry()
    assert row["pitr_reach_seconds"] == 7 * DAY
    (shown,) = backup_catalog_sections.build_retention([row])
    assert "LOG 604800s (7.0 days)" in shown["windows"] and shown["reach"] == "604800s (7.0 days)"


def test_a_kept_reach_reads_as_no_limit(tmp_path):
    catalog = _catalog(tmp_path)
    _run(catalog, _job_with({"FULL": "keep", "LOG": "keep", "default": "keep"}), 1)
    (shown,) = backup_catalog_sections.build_retention(catalog.retention_by_entry(server_id="ACME-192-0-2-250"))
    assert shown["reach"] == "kept, no limit by age"


def test_the_backup_and_restore_page_has_the_table(tmp_path):
    catalog = _catalog(tmp_path)
    _run(catalog, _job_with({"FULL": 14 * DAY, "LOG": 7 * DAY, "default": 14 * DAY}), 1)
    html = backup_restore_report.render_recoverability(catalog)
    assert "Point-in-time reach" in html and "ACME_250_APPDB" in html and "604800s (7.0 days)" in html


def test_the_server_page_script_prints_the_line():
    from pathlib import Path

    script = (Path(backup_catalog_sections.__file__).parent / "templates" / "backup_files.js").read_text(encoding="utf-8")
    assert "function retentionLine(rows)" in script and "${retentionLine(b.retention)}" in script
