"""The backup catalog's rows are hash-chained per entry, so an edit shows (0.35.0, 1.224 - A3.7).

Every ``backup_files`` row carries ``row_hash`` = sha256 of its own fields that never change (what
the backup wrote: names, path, size, checksum, times) and the entry's previous ``row_hash``. An edit
to a past row breaks its hash and every one after it; ``catalog-verify-chain`` names the first.
Rows written before the chain existed are counted, not judged. The state, checks and offload of a
row move afterwards and are not in its hash. (The copy of the chain head off the estate, which makes
the chain hold against someone who rewrites every hash, waits for the vault - V-1, D2.)
"""

from __future__ import annotations

import sqlite3

import pytest

import tests.test_every_backup_run_records_its_files_and_their_checksums as receipts
from dbabrain.backup_restore import backup as backup_module
from dbabrain.db.backup_catalog import BackupCatalogStore


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _runs(catalog, n=3):
    job = receipts._job()
    for i in range(n):
        backup_module.record_in_catalog(catalog, job, receipts._result(job, [
            receipts._line("APPDB", "FULL", f"/b/f{i}.bak", size=1000 + i)]),
            job_run_id=i + 1, started_at=f"2026-10-2{i}T01:00:00Z", finished_at=f"2026-10-2{i}T01:10:00Z")


def test_an_untouched_catalog_holds(catalog):
    _runs(catalog)
    answer = catalog.verify_row_chain()
    assert answer["broken"] == 0 and answer["entries"][0]["chained"] == 3


def test_an_edited_row_is_named_and_breaks_what_follows(catalog, tmp_path):
    _runs(catalog)
    with sqlite3.connect(tmp_path / "catalog.sqlite") as conn:
        conn.execute("UPDATE backup_files SET file_path = '/elsewhere/f1.bak' WHERE file_name = 'f1.bak'")
    entry = catalog.verify_row_chain(backup_id="ACME_250_APPDB")["entries"][0]
    assert entry["broken_at"]["file_name"] == "f1.bak" and entry["chained"] == 1


def test_a_check_or_a_state_change_is_not_an_edit(catalog):
    _runs(catalog)
    row = catalog.stored_files(server_id="ACME-192-0-2-250", file_names=["f0.bak"])["f0.bak"]
    catalog.check_backup_file(row["file_id"], checksum_now="0" * 64)
    assert catalog.verify_row_chain()["broken"] == 0


def test_rows_from_before_the_chain_are_counted_not_judged(catalog, tmp_path):
    _runs(catalog, n=2)
    with sqlite3.connect(tmp_path / "catalog.sqlite") as conn:
        conn.execute("UPDATE backup_files SET row_hash = NULL WHERE file_name = 'f0.bak'")
    entry = catalog.verify_row_chain()["entries"][0]
    # f1's hash was taken over f0's, which is now absent: the chain from f1 is not what it was.
    assert entry["before_chain"] == 1 and entry["broken_at"]["file_name"] == "f1.bak"
