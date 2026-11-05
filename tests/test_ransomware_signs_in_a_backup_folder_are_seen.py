"""What ransomware leaves behind is looked for after every backup run (0.35.0, 1.219).

The plans of 2026-10-29 (A3.4) and 2026-10-30: the C3RB3R wave left `read-me3.txt` in every folder
and renamed files to `*.L0CK3D`. No engine writes such a file, so one in a backup folder is an
attack, not a backup:

* the backup folder is listed by those names only (``file-checksum``'s folder form with an include
  list), so a clean folder costs one listing; a hit is CRITICAL, named, and engages the lockdown;
* a file far from its own history - three times bigger or a third the size of the median of the
  same database's and type's earlier backups (BACKUP_SIZE_ANOMALY) - is a WARNING: encrypted data
  does not compress;
* with fewer than five earlier files there is no history to judge by.
"""

from __future__ import annotations

import pytest

import tests.test_every_backup_run_records_its_files_and_their_checksums as receipts
from dbabrain.backup_restore import backup as backup_module
from dbabrain.lib import integrity as I


@pytest.mark.parametrize("size, history, flagged", [
    (300, [100] * 6, False), (301 * 1, [100] * 6, True), (33, [100] * 6, True), (34, [100] * 6, False),
    (10_000, [100] * 4, False),
])
def test_a_size_far_from_its_history_is_named(size, history, flagged):
    assert bool(I.size_anomaly(size, history)) is flagged


def test_the_markers_name_the_c3rb3r_wave_and_the_generic_notes():
    for pattern in ("read-me*.txt", "*.L0CK3D", "*DECRYPT*", "*.locked", "*.encrypted"):
        assert pattern in I.RANSOM_MARKERS


def test_a_marker_in_the_folder_is_critical_and_engages_the_lockdown(monkeypatch, catalog):
    engaged = []
    monkeypatch.setattr(backup_module, "_ransom_markers",
                        lambda item, **k: ["read-me3.txt", "APPDB_FULL_1.bak.L0CK3D"])
    monkeypatch.setattr(backup_module, "_engage_lockdown",
                        lambda app_config, metadata, *, reason, by: engaged.append(reason) or " - LOCKDOWN ON")
    row = receipts._scheduled(monkeypatch, catalog, receipts._job(), [receipts._line("A", "FULL", "/b/a.bak")])
    assert row["level"] == "critical" and "RANSOM MARKERS" in row["message"]
    assert "read-me3.txt" in row["message"] and "LOCKDOWN ON" in row["message"]
    assert engaged and "read-me3.txt" in engaged[0]


def test_a_clean_folder_says_nothing(monkeypatch, catalog):
    monkeypatch.setattr(backup_module, "_ransom_markers", lambda item, **k: [])
    row = receipts._scheduled(monkeypatch, catalog, receipts._job(), [receipts._line("A", "FULL", "/b/a.bak")])
    assert "RANSOM" not in row["message"]


def test_a_full_three_times_its_history_is_a_warning(monkeypatch, catalog):
    monkeypatch.setattr(backup_module, "_ransom_markers", lambda item, **k: [])
    job = receipts._job()
    for n in range(6):      # the history: six earlier FULLs of about the same size, one a day
        backup_module.record_in_catalog(
            catalog, job, receipts._result(job, [receipts._line("APPDB", "FULL", f"/b/f{n}.bak", size=1000)]),
            job_run_id=n + 1, started_at=f"2026-10-2{n}T01:00:00Z", finished_at=f"2026-10-2{n}T01:10:00Z")
    row = receipts._scheduled(monkeypatch, catalog, receipts._job(),
                              [receipts._line("APPDB", "FULL", "/b/big.bak", size=5000)])
    assert row["level"] == "warning", row
    assert "SIZE far from its history: big.bak" in row["message"]


@pytest.fixture
def catalog(tmp_path):
    from dbabrain.db.backup_catalog import BackupCatalogStore

    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store
