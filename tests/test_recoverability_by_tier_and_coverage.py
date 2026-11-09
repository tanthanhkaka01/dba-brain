"""Each restore drill is judged by its tier, and each database by what a restore of it needs
(0.35.0, 1.248 / 1.252 / 1.253 - gaps G8, G13, G14 of 2026-10-30).

* A restore entry states ``tier`` - daily, weekly, monthly - and every run records it
  (``restore_runs.tier``, catalog schema 4, added to an older catalog where missing). The schedule
  stays the ``time_window``'s: monthly is ``from_day 1, to_day 1``.
* A drill is OK within its tier's deadline after its last success, OVERDUE past it, NEVER when it
  has not succeeded; a failed check (``databases_failed``) is no success.
* A database is *complete* with a full, log or WAL backups, a copy off the host and a drill; what is
  missing is named.
* The backup & restore page carries both, from the catalog - plus the configured entries, read
  through ``lib.data_sources`` (never the backup app), so a tiered drill with no run is listed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import drill_tiers
from dbabrain.reports import backup_catalog_sections as S
from dbabrain.reports import backup_restore_report

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def _at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("value, parsed", [("daily", "daily"), ("Weekly", "weekly"), (None, None), ("", None)])
def test_a_tier_is_one_of_three_or_none(value, parsed):
    assert drill_tiers.parse_tier(value) == parsed


def test_an_unknown_tier_is_refused():
    with pytest.raises(drill_tiers.TierError, match="daily, weekly, monthly"):
        drill_tiers.parse_tier("hourly")


@pytest.mark.parametrize("tier, hours_ago, state", [
    ("daily", 25, "OK"), ("daily", 27, "OVERDUE"), ("weekly", 24 * 7, "OK"), ("weekly", 24 * 8, "OVERDUE"),
    ("monthly", 24 * 31, "OK"), ("monthly", 24 * 33, "OVERDUE"), (None, 1, "UNTIERED"),
])
def test_a_drill_is_judged_by_its_tiers_deadline(tier, hours_ago, state):
    assert drill_tiers.drill_state(tier, NOW - timedelta(hours=hours_ago), NOW) == state


def test_a_tiered_drill_that_never_succeeded_is_never():
    assert drill_tiers.drill_state("daily", None, NOW) == "NEVER"


def test_drills_count_successes_and_a_failed_check_is_not_one():
    runs = [
        {"restore_id": "SALES_DAILY", "tier": "daily", "phase": "done", "databases_failed": 1,
         "started_at": _at(2), "ended_at": _at(1.5)},
        {"restore_id": "SALES_DAILY", "tier": "daily", "phase": "done", "databases_failed": 0,
         "started_at": _at(30), "ended_at": _at(29)},
        {"restore_id": "SALES_DAILY", "tier": "daily", "phase": "error", "started_at": _at(24 * 10)},
    ]
    row = S.build_drills(runs, now=NOW)[0]
    assert row["state"] == "OVERDUE", "the newest run's check failed, and the last success is 29 h old"
    assert (row["ok_7"], row["runs_7"], row["ok_30"], row["runs_30"]) == (1, 2, 1, 3)


def test_coverage_names_what_is_missing():
    rows = [
        {"server_id": "S1", "database_name": "SALESDB", "engine": "sqlserver", "file_type": "FULL", "files": 2,
         "newest": _at(5), "offloaded": 1, "last_restored": _at(6)},
        {"server_id": "S1", "database_name": "SALESDB", "engine": "sqlserver", "file_type": "LOG", "files": 40,
         "newest": _at(1), "offloaded": 0, "last_restored": None},
        {"server_id": "S1", "database_name": "HRDB", "engine": "sqlserver", "file_type": "FULL", "files": 1,
         "newest": _at(5), "offloaded": 0, "last_restored": None},
    ]
    found = {r["database_name"]: r for r in S.build_coverage(rows)}
    assert found["SALESDB"]["status"] == "complete" and found["SALESDB"]["levels"] == ["full", "log"]
    assert found["HRDB"]["missing"] == ["log / WAL backups", "a copy off the host", "a restore drill"]


def test_the_catalog_records_the_tier_and_the_page_shows_it(tmp_path):
    catalog = BackupCatalogStore(tmp_path / "catalog.sqlite")
    catalog.initialize()
    run = catalog.start_restore_run(action="restore", restore_id="SALES_WEEKLY", source_server_id="S1",
                                    target_server_id="LAB", copy_max_retry=3, tier="weekly",
                                    started_at=_at(48), phase="restoring")
    catalog.set_restore_run(run, phase="done", databases_ok=1, databases_failed=0, ended_at=_at(47))
    runs = catalog.drill_runs(since=_at(24 * 30))
    assert runs[0]["tier"] == "weekly"
    html = backup_restore_report.render_recoverability(catalog, now=NOW)
    assert "SALES_WEEKLY" in html and "weekly" in html and "st-OK" in html
    assert "The catalog holds no backup yet." in html


def test_an_older_catalog_gets_the_tier_column(tmp_path):
    path = tmp_path / "old.sqlite"
    catalog = BackupCatalogStore(path)
    catalog.initialize()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE restore_runs DROP COLUMN tier")
        conn.execute("DELETE FROM dbabrain_schema_versions WHERE component = 'BackupCatalogStore'") \
            if conn.execute("SELECT name FROM sqlite_master WHERE name = 'dbabrain_schema_versions'").fetchone() else None
    BackupCatalogStore(path).initialize(force=True)
    with sqlite3.connect(path) as conn:
        assert "tier" in {row[1] for row in conn.execute("PRAGMA table_info(restore_runs)")}


# --------------------------------------------------------------------------- audit 2026-11-05, C1 / C3


def _full_backup(catalog):
    return catalog.record_backup_file(server_id="S1", backup_id="SALES_FULL", engine="sqlserver",
                                         file_name="SALES_FULL.bak", file_type="FULL", database_name="SALES",
                                         file_path="/b/SALES_FULL.bak", verdict="ok", size_bytes=10,
                                         checksum_after_backup="0" * 64, backup_ended_at=_at(30))


def _file_used_by(catalog, file_id, *, action, phase, hours_ago, verdict="copied"):
    run = catalog.start_restore_run(action=action, restore_id="SALES_WEEKLY", source_server_id="S1",
                                    target_server_id="LAB", copy_max_retry=3, started_at=_at(hours_ago + 1), phase="copying")
    catalog.add_run_file(restore_run_id=run, file_id=file_id, source_server_id="S1", target_server_id="LAB",
                         file_name="SALES_FULL.bak", file_type="FULL", source_path="/b/SALES_FULL.bak",
                         target_path="/r/SALES_FULL.bak", copy_max_retry=3, verdict=verdict,
                         checksum_after_backup="0" * 64, checksum_source_before_copy="0" * 64,
                         checksum_target_after_copy="0" * 64, size_source_bytes=10, size_target_bytes=10,
                         copy_attempts=1)
    catalog.set_restore_run(run, phase=phase, ended_at=_at(hours_ago))


def test_a_copy_only_or_failed_run_is_not_a_restore_on_the_coverage_table(tmp_path):
    catalog = BackupCatalogStore(tmp_path / "catalog.sqlite")
    catalog.initialize()
    file_id = _full_backup(catalog)
    _file_used_by(catalog, file_id, action="copy_only", phase="done", hours_ago=10)
    _file_used_by(catalog, file_id, action="restore", phase="error", hours_ago=8)
    (row,) = [r for r in catalog.coverage() if r["file_type"] == "FULL"]
    assert row["last_restored"] is None

    _file_used_by(catalog, file_id, action="restore", phase="done", hours_ago=19)
    (row,) = [r for r in catalog.coverage() if r["file_type"] == "FULL"]
    assert row["last_restored"] == _at(19)


def test_a_monthly_drill_that_succeeded_31_days_ago_is_ok_not_never(tmp_path):
    catalog = BackupCatalogStore(tmp_path / "catalog.sqlite")
    catalog.initialize()
    ok = catalog.start_restore_run(action="restore", restore_id="SALES_MONTHLY", source_server_id="S1",
                                   target_server_id="LAB", copy_max_retry=3, tier="monthly",
                                   started_at=_at(24 * 31 + 1), phase="restoring")
    catalog.set_restore_run(ok, phase="done", databases_ok=1, databases_failed=0, ended_at=_at(24 * 31))
    html = backup_restore_report.render_recoverability(catalog, now=NOW)
    assert "SALES_MONTHLY" in html and "st-OK" in html and "st-NEVER" not in html


def test_the_sla_reads_the_catalogs_drills_beside_the_share_restores_history(tmp_path):
    """D3: RESTORE_DRILL_RTO_30D read only backup_restore_history, which script-driven drills never
    write - so the SLA said "never drilled" for drills this page showed OK."""
    from dbabrain.db.backup_restore_history import catalog_drill_rows

    catalog = BackupCatalogStore(tmp_path / "catalog.sqlite")
    catalog.initialize()
    file_id = _full_backup(catalog)
    _file_used_by(catalog, file_id, action="restore", phase="done", hours_ago=19)
    _file_used_by(catalog, file_id, action="copy_only", phase="done", hours_ago=10)
    with catalog.connect() as conn:
        rows = catalog_drill_rows(conn)
    assert rows == [{"database_name": "SALES", "server_id": "S1", "status": "SUCCESS", "restore_start": _at(20),
                     "restore_end": _at(19), "duration_seconds": 3600.0}]


def test_a_store_without_the_catalog_has_no_catalog_drills(tmp_path):
    from dbabrain.db.backup_restore_history import catalog_drill_rows

    with sqlite3.connect(tmp_path / "empty.sqlite") as conn:
        assert catalog_drill_rows(conn) == []


def test_a_configured_tiered_drill_with_no_run_in_the_window_is_listed_overdue():
    """C3 rest (audit 2026-11-05): such a drill was absent from the table, as if nothing were due."""
    entries = [{"restore_id": "SALES_MONTHLY", "tier": "monthly", "active": True, "server_id": "S1",
                "target_server_id": "LAB"},
               {"restore_id": "OFF_WEEKLY", "tier": "weekly", "active": False},
               {"restore_id": "UNTIERED", "active": True}]
    rows = S.build_drills([], now=NOW, entries=entries)
    assert [(row["restore_id"], row["state"]) for row in rows] == [("SALES_MONTHLY", drill_tiers.OVERDUE)]


def test_two_servers_with_one_database_name_are_two_drills():
    """D3 rest: drills were keyed by database name alone, so one server's success covered another's
    failure."""
    from dbabrain.db import restore_drill

    rows = [{"database_name": "SALES", "server_id": "S1", "status": "SUCCESS", "restore_start": _at(10)},
            {"database_name": "SALES", "server_id": "S2", "status": "FAILED", "restore_start": _at(5)}]
    verdicts = {v["server_id"]: v["status"] for v in restore_drill.evaluate(rows, policy={}, now=NOW)}
    assert verdicts == {"S1": "OK", "S2": "CRITICAL"}
