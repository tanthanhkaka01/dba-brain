"""After an attack, the restore point is chosen from the chains that ended before its first sign
(0.35.0, 1.244 / 1.225 - the plan of 2026-10-29, A3.6).

Dwell time: an attacker sits in the network before the encryption starts, so the newest chain may
already be theirs. ``restore-candidates --backup-id`` lists each chain - a full and the files that
need it - newest first, with:

* ``verified``: every member on disk, hashed ok when written, unchanged when last checked;
* ``before_first_signal``: ended before the first stored file found changed or gone, or the lockdown;
* ``candidate``: both. Read-only - it chooses nothing.
"""

from __future__ import annotations

import pytest

import tests.test_every_backup_run_records_its_files_and_their_checksums as receipts
from dbabrain.backup_restore import backup as backup_module
from dbabrain.db.backup_catalog import BackupCatalogStore


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _chain(catalog, n, day):
    full, log = receipts._job(job="full"), receipts._job(job="log")
    backup_module.record_in_catalog(catalog, full, receipts._result(full, [receipts._line("APPDB", "FULL", f"/b/f{n}.bak")]),
                                    job_run_id=10 * n, started_at=f"2026-10-{day}T01:00:00Z",
                                    finished_at=f"2026-10-{day}T01:10:00Z")
    backup_module.record_in_catalog(catalog, log, receipts._result(
        log, [receipts._line("APPDB", "LOG", f"/b/l{n}.trn", sha=receipts.SHA_B)]),
        job_run_id=10 * n + 1, started_at=f"2026-10-{day}T02:00:00Z", finished_at=f"2026-10-{day}T02:00:05Z")


def test_only_chains_from_before_the_first_sign_are_candidates(monkeypatch, catalog):
    _chain(catalog, 1, 20)
    _chain(catalog, 2, 27)
    # The newest chain's log is found changed on the 28th: the first sign.
    changed = catalog.stored_files(server_id="ACME-192-0-2-250", file_names=["l2.trn"])["l2.trn"]
    catalog.check_backup_file(changed["file_id"], checksum_now="0" * 64, checked_at="2026-10-28T03:00:00Z")
    monkeypatch.setattr(backup_module.BackupCatalogStore, "from_config", classmethod(lambda cls, c: catalog))

    answer = backup_module.restore_candidates(app_config=None, backup_id="ACME_250_APPDB")
    assert answer["first_signal"] == "2026-10-28T03:00:00Z"
    newest, older = answer["chains"]
    assert (newest["full"], newest["verified"], newest["candidate"]) == ("f2.bak", False, False)
    assert newest["unverified"] == ["l2.trn"]
    assert (older["full"], older["verified"], older["before_first_signal"], older["candidate"]) == \
        ("f1.bak", True, True, True)
    assert answer["candidates"] == 1


def test_with_no_sign_every_verified_chain_is_a_candidate(monkeypatch, catalog):
    _chain(catalog, 1, 20)
    monkeypatch.setattr(backup_module.BackupCatalogStore, "from_config", classmethod(lambda cls, c: catalog))
    answer = backup_module.restore_candidates(app_config=None, backup_id="ACME_250_APPDB")
    assert answer["first_signal"] is None and answer["candidates"] == 1
