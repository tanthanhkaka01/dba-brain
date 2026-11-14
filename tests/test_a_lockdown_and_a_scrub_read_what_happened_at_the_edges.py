"""Two findings of the restore tests on the lab host (2026-11-14), both at an edge of a rule that holds.

**A scrub at the window's edge.** A lockdown had been on since 2026-11-13 12:08Z: the scrub of
a SQL Server LOG entry found five LOG files of 2026-10-30 12:07 gone. They were the
script's own: its 14-day window cuts a file by its write time on the server, and cut them at 12:07:39
- while the scrub held their age to the END of their run (12:07:46) against the moment its pass began,
which put them a few seconds inside the window: "gone", an attack's signal, a lockdown. A file is
written between its backup's start and its end, so held to the START (the backup's own, else its
run's), every file the script may cut is the window's, and one gone well inside the window is still the
signal.

**A replace under a lockdown.** The lockdown holds every replace (1.220), so an entry stating
`overwrite_existing: true` sent its RESTORE without REPLACE and the server refused it - after the whole
copy (80 files), in words that told the entry to state the true it states. A lockdown is now asked
before the copy, and named.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import landing_of

from dbabrain.backup_restore import backup, restore_by_id
from dbabrain.lib import cleanup_retention as R

FOURTEEN_DAYS = R.Retention(default=1209600, uniform=True)


# --------------------------------------------------------------------------- the scrub
def test_a_file_the_script_cut_on_the_windows_last_seconds_is_the_windows_not_an_attack():
    """The five files of the lockdown, as the catalog holds them: no backup start of their own (older
    rows), their run started 12:06:54 and ended 12:07:46; the scrub's pass began 12:07:40."""
    row = {"file_type": "LOG", "backup_started_at": None, "run_started_at": "2026-10-30T12:06:54Z",
           "backup_ended_at": "2026-10-30T12:07:46Z", "created_at": "2026-10-30T12:07:46Z"}
    now = "2026-11-13T12:07:40Z"

    assert R.removed_by_retention(FOURTEEN_DAYS, "LOG", row["backup_ended_at"], now_utc=now) == "", \
        "the old reading: inside the window by six seconds - gone, a lockdown"
    assert R.removed_by_retention(FOURTEEN_DAYS, "LOG", backup._written_from(row), now_utc=now).startswith(
        "retention: past its LOG window")


def test_a_file_gone_well_inside_its_window_is_still_the_signal():
    row = {"backup_started_at": "2026-11-10T12:00:00Z", "run_started_at": "2026-11-10T11:59:00Z",
           "backup_ended_at": "2026-11-10T12:00:30Z"}
    assert R.removed_by_retention(FOURTEEN_DAYS, "LOG", backup._written_from(row), now_utc="2026-11-13T12:07:40Z") == ""


@pytest.mark.parametrize("row, expected", [
    ({"backup_started_at": "A", "run_started_at": "B", "backup_ended_at": "C", "created_at": "D"}, "A"),
    ({"backup_started_at": None, "run_started_at": "B", "backup_ended_at": "C", "created_at": "D"}, "B"),
    ({"backup_started_at": None, "run_started_at": None, "backup_ended_at": "C", "created_at": "D"}, "C"),
    ({"created_at": "D"}, "D"),
])
def test_the_earliest_moment_the_catalog_holds_is_the_one_read(row, expected):
    assert backup._written_from(row) == expected


def test_the_scrubs_rows_carry_their_runs_start(tmp_path):
    from dbabrain.db.backup_catalog import BackupCatalogStore

    catalog = BackupCatalogStore(tmp_path / "c.sqlite")
    catalog.initialize()
    run = catalog.start_backup_run(backup_id="E", server_id="S", engine="sqlserver", level="log",
                                   started_at="2026-10-30T12:06:54Z")
    catalog.record_backup_file(server_id="S", backup_id="E", backup_run_id=run, database_name="APP",
                               engine="sqlserver", file_name="APP_LOG_1.trn", file_type="LOG",
                               file_path="C:\\B\\APP_LOG_1.trn", checksum_after_backup="a" * 64, verdict="ok",
                               size_bytes=10, backup_ended_at="2026-10-30T12:07:46Z")

    [row] = catalog.files_to_check(backup_id="E")

    assert row["run_started_at"] == "2026-10-30T12:06:54Z" and row["file_name"] == "APP_LOG_1.trn"


# --------------------------------------------------------------------------- the replace
def _job(**over):
    values = dict(restore_id="R", db_type="sqlserver", landing=landing_of("APP"), overwrite_existing=True)
    values.update(over)
    return SimpleNamespace(**values)


def test_a_replace_under_a_lockdown_is_refused_before_the_copy_and_says_why(monkeypatch):
    monkeypatch.setattr(restore_by_id, "_locked", lambda: True)
    monkeypatch.setattr(restore_by_id, "_where_it_lands", lambda *a, **k: [
        {"lands_as": "APP", "refused": "APP exists on the target (ONLINE) and overwrite_existing is false"}])

    with pytest.raises(restore_by_id.RestoreByIdError) as refused:
        restore_by_id._refuse_a_name_already_held(_job(), {}, host={"host": "h"}, data_dir=None)

    said = str(refused.value)
    assert "APP exist(s) on the target and the entry states overwrite_existing true, but a lockdown is on" in said
    assert "Nothing was copied or restored" in said and "State overwrite_existing: true" not in said


def test_a_replace_with_no_lockdown_asks_nothing_as_before(monkeypatch):
    monkeypatch.setattr(restore_by_id, "_locked", lambda: False)
    monkeypatch.setattr(restore_by_id, "_where_it_lands", lambda *a, **k: pytest.fail("nothing is asked"))

    assert restore_by_id._refuse_a_name_already_held(_job(), {}, host={"host": "h"}, data_dir=None) == {}


def test_a_replace_under_a_lockdown_onto_a_free_name_goes_on(monkeypatch):
    monkeypatch.setattr(restore_by_id, "_locked", lambda: True)
    monkeypatch.setattr(restore_by_id, "_where_it_lands", lambda *a, **k: [{"lands_as": "APP"}])

    assert restore_by_id._refuse_a_name_already_held(_job(), {}, host={"host": "h"}, data_dir=None) == {}


@pytest.mark.parametrize("db_type", ["postgresql", "oracle"])
def test_a_whole_instance_replace_under_a_lockdown_waits_for_it_by_name(monkeypatch, db_type):
    monkeypatch.setattr(restore_by_id, "_locked", lambda: True)

    with pytest.raises(restore_by_id.RestoreByIdError, match="a lockdown is on: nothing is replaced"):
        restore_by_id._refuse_a_whole_instance_replace_under_a_lockdown(
            _job(db_type=db_type, overwrite_existing=True), restore_id="R")


@pytest.mark.parametrize("db_type, overwrite, locked", [
    ("postgresql", False, True), ("postgresql", True, False), ("sqlserver", True, True)])
def test_otherwise_nothing_is_refused_here(monkeypatch, db_type, overwrite, locked):
    """false is the restore step's to judge against what the target holds; SQL Server is asked by name."""
    monkeypatch.setattr(restore_by_id, "_locked", lambda: locked)

    assert restore_by_id._refuse_a_whole_instance_replace_under_a_lockdown(
        _job(db_type=db_type, overwrite_existing=overwrite), restore_id="R") is None
