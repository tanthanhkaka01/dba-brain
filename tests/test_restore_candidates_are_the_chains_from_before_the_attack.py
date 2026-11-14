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


# --------------------------------------------------------------------------- #
# The new flow (0.39.0, 1.358): three entries per server, files released to the repository
# --------------------------------------------------------------------------- #
def _new_flow(catalog, *, full_id_link_lost=False):
    """FULL, DIFF and LOG as three entries of one server, as the new flow writes them."""
    full, diff, log = (receipts._job(backup_id=f"ACME_250_APPDB_{level.upper()}", job=level)
                       for level in ("full", "diff", "log"))
    backup_module.record_in_catalog(catalog, full, receipts._result(full, [receipts._line("APPDB", "FULL", "/b/f.bak")]),
                                    job_run_id=1, started_at="2026-11-10T01:00:00Z", finished_at="2026-11-10T01:10:00Z")
    backup_module.record_in_catalog(catalog, diff, receipts._result(diff, [receipts._line("APPDB", "DIFF", "/b/d.bak")]),
                                    job_run_id=2, started_at="2026-11-11T20:00:00Z", finished_at="2026-11-11T20:01:00Z")
    backup_module.record_in_catalog(catalog, log, receipts._result(
        log, [receipts._line("APPDB", "LOG", "/b/l.trn", sha=receipts.SHA_B)]),
        job_run_id=3, started_at="2026-11-11T20:15:00Z", finished_at="2026-11-11T20:15:05Z")
    rows = {r["file_name"]: r for r in catalog.chain_files(server_id="ACME-192-0-2-250")}
    if full_id_link_lost:
        # A file written after its full left the server named no full (fixed for new rows in 0.38.0).
        for name in ("d.bak", "l.trn"):
            with catalog.connect() as conn:
                conn.execute("UPDATE backup_files SET chain_parent_file_id = NULL WHERE file_id = ?",
                             (rows[name]["file_id"],))
    return rows


def test_a_chain_is_read_across_a_servers_full_diff_and_log_entries(monkeypatch, catalog):
    _new_flow(catalog)
    monkeypatch.setattr(backup_module.BackupCatalogStore, "from_config", classmethod(lambda cls, c: catalog))
    answer = backup_module.restore_candidates(app_config=None, backup_id="ACME_250_APPDB_FULL")
    assert answer["backup_ids"] == ["ACME_250_APPDB_DIFF", "ACME_250_APPDB_FULL", "ACME_250_APPDB_LOG"]
    (chain,) = answer["chains"]
    assert (chain["full"], chain["files"], chain["candidate"]) == ("f.bak", 3, True)


def test_a_chain_released_to_the_repository_is_still_a_candidate(monkeypatch, catalog):
    rows = _new_flow(catalog)
    for row in rows.values():
        catalog.set_backup_file(row["file_id"], state="pruned", on_disk=0, offload_state="verified",
                                offload_server_id="ACME-192-0-2-118-HOST", offload_path=f"/r/{row['file_name']}",
                                offload_run_id=1, offload_verified_at="2026-11-12T00:00:00Z")
    monkeypatch.setattr(backup_module.BackupCatalogStore, "from_config", classmethod(lambda cls, c: catalog))
    (chain,) = backup_module.restore_candidates(app_config=None, backup_id="ACME_250_APPDB_LOG")["chains"]
    assert chain["verified"] is True and chain["candidate"] is True
    assert (chain["offloaded"], chain["on_the_server"]) == (3, 0)


def test_a_chain_is_read_by_time_when_its_link_to_the_full_is_lost(monkeypatch, catalog):
    _new_flow(catalog, full_id_link_lost=True)
    monkeypatch.setattr(backup_module.BackupCatalogStore, "from_config", classmethod(lambda cls, c: catalog))
    (chain,) = backup_module.restore_candidates(app_config=None, backup_id="ACME_250_APPDB_DIFF")["chains"]
    assert chain["files"] == 3, "the diff and the log belong to the full before them"
