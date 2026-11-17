"""A server's page lists the restores of or onto that server, each with the files it applied in order (0.39.0, 1.365).

The operator, 2026-11-13 (translated): *dbabrain must keep the full chain of files a restore used, so
it can be shown which file was used for which restore.* The fleet page (``backup-restore.html``)
lists every restore with its chain; a server's own page listed its backup files and which restores
used each, and never what a restore did - in what order its files went in, which one a restore
passed over, which one it failed on, and where it stopped. A DBA who opens the page of the server a
drill reads from, or of the lab it restores onto, had to go to the fleet page and search.

Which restores: those that read this server's backups or restored onto it, by the run's own source
and target (``restore_history(server_id=...)``). Not by its chain files: a run that failed before its
first file has none, and it is exactly the one to see. The table is the fleet page's own drawing
(``reports/restores_table``), carried in the page's JSON as escaped HTML - a second drawing in the
page's script would drift from the first. What a run did not record reads *not recorded*.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.db.store import DbaBrainStore
from dbabrain.reports import backup_catalog_sections, backup_restore_report, restores_table
from dbabrain.reports.server_report import build_payload, build_server_pages, series_file_name

SERVER, LAB, OTHER = "ACME-192-0-2-250", "LAB-192-0-2-40", "ACME-192-0-2-251"
TEMPLATE = Path(__file__).resolve().parents[1] / "dbabrain" / "reports" / "templates" / "server_report.html"
M4326 = "Msg 4326: The log in this backup set terminates at LSN 51000000041600001, which is too early to apply"
M4305 = "Msg 4305: The log in this backup set begins at LSN 51000000044000001, which is too recent to apply"


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _run(catalog, started, *, source=SERVER, target=LAB, restore_id="APP_TO_LAB", action="restore",
         phase="done", chain=(), steps=(), **more):
    run_id = catalog.start_restore_run(action=action, restore_id=restore_id, source_server_id=source,
                                       target_server_id=target, copy_max_retry=3, started_at=started)
    if chain:
        catalog.plan_chain(run_id, [dict(source_server_id=source, target_server_id=target, **row) for row in chain])
        for positions, result, error in steps:
            catalog.set_chain_step(run_id, positions, result=result, error_text=error)
    catalog.set_restore_run(run_id, phase=phase, **more)
    return run_id


def _file(name, step, ended, **more):
    return dict(database_name="APPDB", restored_as="APPDB_LAB", step=step, file_name=name,
                file_path=f"D:/stage/APPDB/{name}", backup_ended_at=ended, **more)


CHAIN = (_file("APPDB_FULL_20261112.bak", "restore-full", "2026-11-12 23:35:49"),
         _file("APPDB_DIFF_20261113.bak", "restore-diff", "2026-11-13 00:41:12"),
         _file("APPDB_LOG_20261113_0100.trn", "restore-log", "2026-11-13 01:00:01"),
         _file("APPDB_LOG_20261113_0115.trn", "restore-log", "2026-11-13 01:15:01"),
         _file("APPDB_LOG_20261113_0130.trn", "restore-log", "2026-11-13 01:30:01", with_recovery=1,
               stopat="2026-11-13 01:20:00"))
LANDED = [{"database_name": "APPDB_LAB", "source_database": "APPDB", "state": "ONLINE", "by_stopat": True,
           "stop_at": "2026-11-13T01:20:00", "last_backup_finished": "2026-11-13T01:30:01",
           "verdict": "stopped at the moment asked"}]


@pytest.fixture
def estate(catalog):
    """A drill from this server that passed over one log, one that failed on a log, one refused before
    its first file, a restore onto this server from another, and the rows that are not its."""
    _run(catalog, "2026-11-13T07:00:00Z", chain=CHAIN, landed_json=json.dumps(LANDED),
         point_in_time="2026-11-13 01:20:00", ended_at="2026-11-13T07:05:00Z",
         steps=[([1, 2, 3], "applied", ""), ([4], "skipped", M4326), ([5], "applied", "")])
    _run(catalog, "2026-11-12T07:00:00Z", chain=CHAIN[:4], phase="error", ended_at="2026-11-12T07:03:00Z",
         error_text="APPDB: a log is missing before APPDB_LOG_20261113_0115.trn",
         steps=[([1, 2, 3], "applied", ""), ([4], "failed", M4305)])
    _run(catalog, "2026-11-11T07:00:00Z", phase="refused", ended_at="2026-11-11T07:00:02Z",
         error_text="no full backup for <APPDB>")
    _run(catalog, "2026-11-10T07:00:00Z", source=OTHER, target=SERVER, restore_id="HR_TO_APP_HOST",
         ended_at="2026-11-10T07:30:00Z", chain=[dict(_file("HRDB_FULL.bak", "restore-full", None),
                                                      database_name="HRDB", restored_as="HRDB")],
         steps=[([1], "applied", "")])
    # Not this server's: another pair's restore, and a copy-only run, which restores nothing.
    _run(catalog, "2026-11-13T08:00:00Z", source=OTHER, target=LAB, restore_id="HR_TO_LAB")
    _run(catalog, "2026-11-13T09:00:00Z", action="copy_only", phase="copied", restore_id="COPY")
    return catalog


def _section(catalog, server=SERVER) -> dict:
    return build_payload([], [], server_id=server,
                         restore_runs=catalog.restore_history(server_id=server))["restores"]


# --------------------------------------------------------------------------- #
# Which restores are the server's
# --------------------------------------------------------------------------- #
def test_the_restores_of_a_server_are_those_from_its_backups_and_onto_it_newest_first(estate):
    runs = estate.restore_history(server_id=SERVER)

    assert [run["restore_id"] for run in runs] == ["APP_TO_LAB", "APP_TO_LAB", "APP_TO_LAB", "HR_TO_APP_HOST"]
    assert [run["started_at"] for run in runs] == sorted((run["started_at"] for run in runs), reverse=True)


def test_a_restore_refused_before_its_first_file_is_still_the_servers(estate):
    """By the run's own source and target: the chain-file reader lists it nowhere, having no file."""
    refused = [run for run in estate.restore_history(server_id=SERVER) if run["phase"] == "refused"]

    assert len(refused) == 1 and refused[0]["chain"] == []
    assert all(row["restore_run_id"] != refused[0]["restore_run_id"]
               for row in estate.server_restore_chains(SERVER))


def test_the_lab_page_lists_every_restore_onto_it_and_the_fleet_reader_is_unchanged(estate):
    assert {run["restore_id"] for run in estate.restore_history(server_id=LAB)} == {"APP_TO_LAB", "HR_TO_LAB"}
    assert len(estate.restore_history()) == 5, "every restore, a copy-only run left out"


# --------------------------------------------------------------------------- #
# What the section says
# --------------------------------------------------------------------------- #
def test_the_section_counts_its_restores_and_the_ones_that_did_not_end_done(estate):
    section = _section(estate)

    assert (section["shown"], section["failed"], section["refused"]) == (4, 1, 1)
    assert section["limit"] == restores_table.RESTORE_ROWS


def test_each_file_is_listed_in_the_order_it_went_in_with_its_step_database_and_name_restored_as(estate):
    html = _section(estate)["html"]
    first = html[:html.index("run 2")]

    order = [first.index(row["file_name"]) for row in CHAIN]
    assert order == sorted(order), "in the order they went in"
    assert "<td>1</td><td>full</td><td class='db'>APPDB as APPDB_LAB</td>" in first
    assert "<td>2</td><td>diff</td>" in first and "<td>3</td><td>log</td>" in first
    assert "2026-11-12 23:35:49" in first, "the backup's end, in the source's clock, as recorded"


def test_a_skipped_and_a_failed_file_say_why_and_have_no_applied_time(estate):
    html = _section(estate)["html"]

    assert f"<td class='st-skipped'>skipped - {M4326}</td><td>-</td>" in html
    assert f"<td class='st-failed'>failed - {M4305}</td><td>-</td>" in html
    assert "5 file(s) - 4 applied, 1 skipped" in html and "4 file(s) - 3 applied, 1 failed" in html


def test_the_last_file_carries_its_stopat_and_where_the_database_stopped_is_the_targets_word(estate):
    html = _section(estate)["html"]

    assert "<td>STOPAT 2026-11-13 01:20:00</td></tr>" in html
    assert "APPDB as APPDB_LAB: stopped at 2026-11-13 01:20:00" in html


def test_what_a_run_did_not_record_reads_not_recorded_never_blank(estate):
    html = _section(estate)["html"]
    onto = html[html.index("HR_TO_APP_HOST"):]

    assert "<td class='file'>HRDB_FULL.bak <span class='rs-none'>(not in the backup catalog)</span></td>" \
           "<td><span class='rs-none'>not recorded</span></td>" in onto, "its backup's end was not recorded"
    refused = html[html.index("refused"):html.index("HR_TO_APP_HOST")]
    assert refused.count("<span class='rs-none'>not recorded</span>") == 2, "its stop points and its files"
    assert "<td></td>" not in html


def test_a_value_is_text_never_markup(estate):
    html = _section(estate)["html"]

    assert "no full backup for &lt;APPDB&gt;" in html and "<APPDB>" not in html


def test_a_server_no_restore_touched_has_no_section(estate):
    assert _section(estate, server="ACME-192-0-2-99") == {}
    assert build_payload([], [], server_id=SERVER)["restores"] == {}


# --------------------------------------------------------------------------- #
# One drawing for both pages
# --------------------------------------------------------------------------- #
def test_the_server_page_carries_the_fleet_pages_own_table(catalog):
    _run(catalog, "2026-11-13T07:00:00Z", chain=CHAIN, landed_json=json.dumps(LANDED),
         steps=[([1, 2, 3, 4, 5], "applied", "")])
    table = restores_table.restores_table(backup_catalog_sections.build_restores(catalog.restore_history()),
                                          empty="The catalog holds no restore yet.")

    assert table in _section(catalog)["html"]
    assert table in backup_restore_report.render_restores(catalog)


def test_the_tables_rules_are_in_the_stylesheet_both_pages_read(catalog):
    """The fleet page reads the server page's stylesheet; the rules moved there with the table."""
    sheet = backup_restore_report.report_css()

    assert "section.restores table.chain td.file" in sheet and "section.recover table" in sheet
    assert "section.restores" not in backup_restore_report.TEMPLATE_HTML.read_text(encoding="utf-8").split("</style>")[0]
    assert "section.restores .st-failed,section.restores .st-refused" in sheet


def test_a_long_file_name_wraps_instead_of_widening_the_page(catalog):
    """At 1366 px a chain of long file names, every cell kept on one line, pushed the table and its
    Files applied column past the screen (looked at in a browser, 2026-11-14)."""
    sheet = backup_restore_report.report_css()

    assert re.search(r"section\.restores table\.chain td\.file\{[^}]*overflow-wrap:anywhere[^}]*min-width:180px", sheet)
    assert "section.restores table.chain td.db{white-space:normal}" in sheet
    assert "section.restores{max-width:100%; overflow-x:auto}" in sheet


# --------------------------------------------------------------------------- #
# The page as built, and as the browser folds it
# --------------------------------------------------------------------------- #
def test_the_built_server_file_carries_the_section(tmp_path):
    path = tmp_path / "runtime.sqlite"
    DbaBrainStore(path).initialize()          # the metric tables the rest of the page reads
    store = BackupCatalogStore(path)
    store.initialize()
    _run(store, "2026-11-13T07:00:00Z", chain=CHAIN, steps=[([1, 2, 3, 4, 5], "applied", "")])
    out = tmp_path / "reports"

    build_server_pages(sqlite_path=path, models=[{"server_id": SERVER, "ip": "192.0.2.250"}], output_dir=out,
                       stamp="20261114_090000", snapshot_date="2026-11-14", days=7,
                       inventory_href="database-inventory.html")

    payload = json.loads((out / series_file_name(SERVER)).read_text(encoding="utf-8"))
    assert payload["restores"]["shown"] == 1 and "APPDB_LOG_20261113_0130.trn" in payload["restores"]["html"]


NODE = shutil.which("node")


def _fold(section: dict) -> str:
    script = TEMPLATE.read_text(encoding="utf-8")
    block = re.search(r"\nfunction restoresBlock\(r\)\{.*?\n\}\n", script, flags=re.S).group(0)
    program = ('const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'
               '\'"\':"&quot;"}[c]));\n' + block + f"\nprocess.stdout.write(restoresBlock({json.dumps(section)}));\n")
    done = subprocess.run([NODE, "-"], input=program, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_page_folds_the_table_under_its_backup_files_and_opens_it_on_a_failure(estate):
    page = _fold(_section(estate))

    assert page.startswith('<details class="fold" id="restores" open>')
    assert "Restores — 4 restore(s) of or onto this server · 1 failed · 1 refused" in page
    assert _section(estate)["html"] in page, "the table as Python drew it"
    script = TEMPLATE.read_text(encoding="utf-8")
    assert script.index("+ backupFilesBlock(d.backupFiles)") < script.index("+ restoresBlock(d.restores)")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_section_with_every_restore_done_stays_folded_and_none_draws_nothing(catalog):
    _run(catalog, "2026-11-13T07:00:00Z", chain=CHAIN, steps=[([1, 2, 3, 4, 5], "applied", "")])

    assert _fold(_section(catalog)).startswith('<details class="fold" id="restores">')
    assert _fold({}) == ""
