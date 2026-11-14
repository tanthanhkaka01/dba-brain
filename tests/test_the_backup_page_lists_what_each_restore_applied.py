"""The backup & restore page lists each restore: the files it applied, in order, and where each database stopped (0.39.0, 1.365).

The operator, 2026-11-13 (translated): *dbabrain must keep the full chain of files a restore used,
so it can be shown which file was used for which restore* - and of ``backup-restore.html``: *this
report may have to be fixed when 0.39 is done (its restore and backup parts).*

The chain has been in the catalog since 1.367 (``restore_chain_files``) and where each database
stopped since 1.362 (``restore_runs.landed_json``) - and no page read either. The *Restores*
section does: the newest restores, how each ended, the moment asked, the target's own word on where
each database stopped, and under each the files that went in, in order. What a run did not record
- a restore from before 0.39.0, one that failed before its first file - reads *not recorded*, never
a blank that looks like *none*.
"""

from __future__ import annotations

import json

import pytest

from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.reports import backup_catalog_sections, backup_restore_report


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _run(catalog, started, *, restore_id="APP_TO_DRILL", action="restore", phase="done", chain=(), **more):
    run_id = catalog.start_restore_run(action=action, restore_id=restore_id, source_server_id="SRC-01",
                                       target_server_id="DRILL-01", copy_max_retry=3, started_at=started)
    if chain:
        catalog.plan_chain(run_id, [dict(source_server_id="SRC-01", target_server_id="DRILL-01", **row) for row in chain])
    catalog.set_restore_run(run_id, phase=phase, **more)
    return run_id


def _file(name, step, **more):
    return dict(database_name="APPDB", restored_as="APPDB_COPY", step=step, file_name=name,
                file_path=f"/stage/APPDB/{name}", **more)


CHAIN = (_file("APPDB_FULL.bak", "restore-full", backup_ended_at="2026-11-12 11:35:49"),
         _file("APPDB_DIFF.bak", "restore-diff", backup_ended_at="2026-11-13 03:00:55"),
         _file("APPDB_LOG_1.trn", "restore-log", backup_ended_at="2026-11-13 03:08:29"),
         _file("APPDB_LOG_2.trn", "restore-log", backup_ended_at="2026-11-13 03:23:01", with_recovery=1,
               stopat="2026-11-13 03:20:00"))
LANDED = [{"database_name": "APPDB_COPY", "source_database": "APPDB", "state": "ONLINE", "by_stopat": True,
           "stop_at": "2026-11-13T03:20:00", "last_backup_finished": "2026-11-13T03:23:01",
           "verdict": "stopped at the moment asked"}]


# --------------------------------------------------------------------------- #
# The catalog's reader
# --------------------------------------------------------------------------- #
def test_the_newest_restores_come_first_each_with_its_chain_in_order(catalog):
    old = _run(catalog, "2026-11-12T01:00:00Z")
    new = _run(catalog, "2026-11-13T07:00:00Z", chain=CHAIN, landed_json=json.dumps(LANDED),
               point_in_time="2026-11-13 03:20:00")

    runs = catalog.restore_history()

    assert [run["restore_run_id"] for run in runs] == [new, old]
    assert [row["file_name"] for row in runs[0]["chain"]] == [row["file_name"] for row in CHAIN]
    assert [row["position"] for row in runs[0]["chain"]] == [1, 2, 3, 4]
    assert runs[1]["chain"] == [] and runs[0]["point_in_time"] == "2026-11-13 03:20:00"


def test_a_copy_only_run_restored_nothing_and_is_not_listed(catalog):
    _run(catalog, "2026-11-13T07:00:00Z", action="copy_only", phase="copied")
    restore = _run(catalog, "2026-11-13T06:00:00Z")

    assert [run["restore_run_id"] for run in catalog.restore_history()] == [restore]


def test_the_page_takes_the_newest_few_and_a_reader_may_ask_since_when(catalog):
    for hour in range(5):
        _run(catalog, f"2026-11-13T0{hour}:00:00Z")

    assert len(catalog.restore_history(limit=3)) == 3
    assert [run["started_at"] for run in catalog.restore_history(since="2026-11-13T03:00:00Z")] == [
        "2026-11-13T04:00:00Z", "2026-11-13T03:00:00Z"]


# --------------------------------------------------------------------------- #
# The rows the page draws
# --------------------------------------------------------------------------- #
def test_a_restore_is_listed_with_its_counts_its_chain_and_where_each_database_stopped(catalog):
    run_id = _run(catalog, "2026-11-13T07:00:00Z", chain=CHAIN, landed_json=json.dumps(LANDED))
    catalog.set_chain_step(run_id, [1, 2, 3], result="applied")
    catalog.set_chain_step(run_id, [4], result="failed", error_text="Msg 3201: cannot open backup device")

    [item] = backup_catalog_sections.build_restores(catalog.restore_history())

    assert item["files"] == 4 and item["steps"] == {"full": 1, "diff": 1, "log": 2}
    assert item["results"] == {"applied": 3, "failed": 1}
    assert [row["step"] for row in item["chain"]] == ["full", "diff", "log", "log"]
    last = item["chain"][-1]
    assert last["stopat"] == "2026-11-13 03:20:00" and last["recovers"] is True and "Msg 3201" in last["error"]
    assert last["restoredAs"] == "APPDB_COPY" and last["inCatalog"] is False
    assert item["landed"] == [{"database": "APPDB", "restoredAs": "APPDB_COPY", "state": "ONLINE",
                               "stopAt": "2026-11-13T03:20:00", "lastBackupFinished": "2026-11-13T03:23:01",
                               "byStopat": True, "verdict": "stopped at the moment asked"}]


def test_a_run_that_recorded_neither_says_so_and_fails_nothing(catalog):
    _run(catalog, "2026-11-13T07:00:00Z", phase="error", error_text="no full backup for APPDB",
         landed_json="not json at all")

    [item] = backup_catalog_sections.build_restores(catalog.restore_history())

    assert item["chain"] == [] and item["landed"] == [] and item["files"] == 0
    assert item["phase"] == "error" and item["error"] == "no full backup for APPDB" and item["moment"] == "LATEST"


# --------------------------------------------------------------------------- #
# The section
# --------------------------------------------------------------------------- #
@pytest.fixture
def section(catalog):
    done = _run(catalog, "2026-11-13T07:00:00Z", chain=CHAIN, landed_json=json.dumps(LANDED),
                point_in_time="2026-11-13 03:20:00", ended_at="2026-11-13T07:01:00Z")
    catalog.set_chain_step(done, [1, 2, 3, 4], result="applied")
    _run(catalog, "2026-11-12T07:00:00Z", phase="error", error_text="no full backup for <APPDB>",
         ended_at="2026-11-12T07:00:05Z")
    return backup_restore_report.render_restores(catalog)


def test_each_restore_is_a_row_and_its_chain_a_row_of_its_own_across_the_table(section):
    """Inside the last column the file names wrapped letter by letter at 1366 px (seen in a browser)."""
    assert section.count("<tr class='chain-row'><td colspan='7'>") == 1, "only the run that recorded a chain"
    assert "4 file(s) - 4 applied" in section and "1 full, 1 diff, 2 log" in section
    assert "<td class='file'>APPDB_FULL.bak" in section
    order = [section.index(name) for name in ("APPDB_FULL.bak", "APPDB_DIFF.bak", "APPDB_LOG_1.trn", "APPDB_LOG_2.trn")]
    assert order == sorted(order), "in the order they went in"
    assert "STOPAT 2026-11-13 03:20:00" in section


def test_the_stop_point_is_the_targets_own_word_and_its_verdict(section):
    assert "APPDB as APPDB_COPY: stopped at 2026-11-13 03:20:00" in section
    assert "(stopped at the moment asked)" in section
    assert "in the source server's clock" in section


def test_a_failed_restore_shows_why_and_what_it_did_not_record(section):
    assert "<td class='st-failed'>failed" in section
    assert "no full backup for &lt;APPDB&gt;" in section, "the error is text, never markup"
    assert section.count("<span class='rs-none'>not recorded</span>") == 2, "its stop points and its files"


def test_a_file_the_backup_catalog_does_not_know_is_marked(section):
    assert "(not in the backup catalog)" in section


def test_the_section_uses_class_names_of_its_own(section):
    """The page shares the server page's stylesheet: `verdict` there is the big verdict block, and
    each stop point came out with two blank lines under it (seen in a browser, 2026-11-13). Its rules
    are in that shared stylesheet since a server page folds the same table (1.365)."""
    styles = backup_restore_report.report_css()

    assert "class='verdict'" not in section and "class='rs-note'" in section
    assert "section.restores .rs-note" in styles and "section.restores .verdict" not in styles


def test_a_catalog_with_no_restore_says_so(catalog):
    assert "The catalog holds no restore yet." in backup_restore_report.render_restores(catalog)


def test_the_page_carries_the_section_under_recoverability(section):
    html = backup_restore_report.render_page(files={"files": [], "summary": {}, "servers": []},
                                             snapshot_date="2026-11-13", recoverability="<section>R</section>",
                                             restores=section)

    assert "__RESTORES__" not in html
    assert html.index("<section>R</section>") < html.index("<h2>Restores</h2>") < html.index('<div id="body">')
