"""A source released after its repository copy is verified goes file by file, a folder only when empty (2026-11-11).

Why it matters: the operator, 2026-11-11 (translated) - *turn the delete at the source on: once a copy
is verified on 100.118, delete it on the server_id*; and, as decided on 2026-11-10, *every delete is one
file with its every fact; a folder with files in it is never deleted, only an empty folder*. Two things
stood in the way, and each is held here:

* a PostgreSQL base backup is a DIRECTORY (base.tar.zst, pg_wal.tar, backup_manifest). It was never
  released - the script's own sweep removed whole chains with ``rm -rf``. Now each of its files is
  deleted by the path, bytes and sha256 its offload measured there, and its folder only once empty;
* "the newest chain" was read per ENTRY, and the WAL / LOG streams are entries of their own beside the
  one that takes the fulls - read that way they had no full at all, so not one WAL file was ever
  released. The newest chain is now the database's on its server, across its entries.

The newest chain itself always stays: the next incremental chains onto it, and a restore from the
server still has its latest point.
"""

from __future__ import annotations

import pytest

from dbabrain.backup_restore import offload as O
from tests.test_every_backup_is_copied_off_its_server_before_it_is_released import (
    HOST_DIR, REPO, SRC, _entry, _hosts, _Net, _offload, _sha)

STAMP = "20261101T000000Z_FULL"
FOLDER = f"{HOST_DIR}/base/{STAMP}"
REPO_FOLDER = f"/repository/{SRC}/cluster/base/{STAMP}"
MEMBERS = {"base.tar.zst": b"the cluster's files", "pg_wal.tar": b"the wal of its window",
           "backup_manifest": b"the manifest"}


@pytest.fixture
def net(monkeypatch):
    fake = _Net()
    monkeypatch.setattr(O.common_cli, "run", fake.run)
    monkeypatch.setattr(O.common_cli, "run_allowing_failure", fake.run_allowing_failure)
    return fake


class _Catalog:
    """What release_sources reads and writes for one base backup directory."""

    def __init__(self, members, newest="2026-11-10T00:00:00Z"):
        self.members, self.newest, self.set = members, newest, []

    def newest_full_per_database(self, **_k):
        return [{"database_name": "cluster", "at": self.newest}]

    def offload_members(self, file_id):
        return [dict(m) for m in self.members]

    def set_backup_file(self, file_id, **values):
        self.set.append((file_id, values))

    def check_backup_file(self, *_a, **_k):
        pytest.fail("a directory is held to its members' proofs, not re-checked as one file")


def _directory(net, *, ended="2026-11-01T00:30:00Z"):
    """A base backup on the server and its proven copy on the repository, and the offload's member rows."""
    members = []
    for name, data in MEMBERS.items():
        net.source[f"{FOLDER}/{name}"] = data
        net.repo[f"{REPO_FOLDER}/{name}"] = data
        members.append({"source_path": f"/var/opt/mssql/backup/base/{STAMP}/{name}",
                        "source_host_path": f"{FOLDER}/{name}", "target_path": f"{REPO_FOLDER}/{name}",
                        "size_source_bytes": len(data), "size_target_bytes": len(data),
                        "checksum_source_before_copy": _sha(data), "checksum_target_after_copy": _sha(data),
                        "verdict": "copied"})
    row = {"file_id": 9, "file_name": STAMP, "file_type": "FULL_BASE", "database_name": "cluster",
           "file_path": f"/var/opt/mssql/backup/base/{STAMP}", "host_path": FOLDER,
           "offload_path": REPO_FOLDER, "backup_ended_at": ended, "size_bytes": None,
           "checksum_after_backup": _sha(MEMBERS["backup_manifest"])}
    return row, members


def _release(net, row, members, **kw):
    entry = _entry(db_type="postgresql", offload=_offload(delete_source=True))
    catalog = kw.pop("catalog", None) or _Catalog(members)
    answer = O.release_sources(entry, catalog=catalog, hosts=_hosts(entry), rows=[row], **kw)
    return answer, catalog


# --------------------------------------------------------------------------- #
# A base backup directory
# --------------------------------------------------------------------------- #
def test_a_base_backup_directory_is_deleted_file_by_file_then_its_empty_folder(net):
    row, members = _directory(net)

    answer, catalog = _release(net, row, members)

    assert answer == {"deleted": 1, "kept": 0, "refused": []}
    assert not [p for p in net.source if p.startswith(FOLDER + "/")], "every file of it is gone"
    assert net.deleted[-1] == FOLDER + "/", "the folder goes last, once empty"
    assert sorted((r["file_name"], r["size_bytes"], r["sha256"]) for r in net.delete_requests) == sorted(
        (name, len(data), _sha(data)) for name, data in MEMBERS.items()), "each file with its every fact"
    assert all(r["must_be_under"] == HOST_DIR for r in net.delete_requests)
    assert catalog.set[0][1]["state"] == "pruned" and catalog.set[0][1]["source_deleted_at"]
    assert "delete-files" not in net.calls


def test_a_file_left_in_the_folder_keeps_the_folder_and_the_row(net):
    row, members = _directory(net)
    net.source[f"{FOLDER}/written_by_hand.txt"] = b"x"

    answer, catalog = _release(net, row, members)

    assert answer["deleted"] == 0 and "its folder is not" in answer["refused"][0]
    assert f"{FOLDER}/written_by_hand.txt" in net.source, "a folder with a file in it is never deleted"
    assert catalog.set == [], "not marked deleted while its folder is still there"


def test_a_file_of_other_bytes_at_the_source_stops_the_list_and_the_folder_stays(net):
    row, members = _directory(net)
    net.source[f"{FOLDER}/backup_manifest"] = b"changed after the backup"

    answer, catalog = _release(net, row, members)

    assert answer["deleted"] == 0 and "backup_manifest" in answer["refused"][0]
    assert f"{FOLDER}/backup_manifest" in net.source and catalog.set == []
    assert FOLDER + "/" not in net.deleted


def test_a_repository_copy_of_other_bytes_keeps_the_whole_source(net):
    row, members = _directory(net)
    net.repo[f"{REPO_FOLDER}/pg_wal.tar"] = b"tampered"

    answer, catalog = _release(net, row, members)

    assert answer["deleted"] == 0 and "pg_wal.tar" in answer["refused"][0]
    assert all(f"{FOLDER}/{name}" in net.source for name in MEMBERS) and net.delete_requests == []


def test_a_directory_with_no_proof_of_its_files_is_never_deleted(net):
    row, _members = _directory(net)
    answer, _catalog = _release(net, row, [])
    assert answer["deleted"] == 0 and "no proof of its files" in answer["refused"][0]
    assert net.delete_requests == []


@pytest.mark.parametrize("change", ["no sha256", "no size", "outside the folder"])
def test_a_member_without_every_fact_refuses_the_directory_before_the_host(net, change):
    row, members = _directory(net)
    members[0].update({"no sha256": {"checksum_source_before_copy": None}, "no size": {"size_source_bytes": None},
                       "outside the folder": {"source_host_path": f"{HOST_DIR}/elsewhere/base.tar.zst"}}[change])

    answer, _catalog = _release(net, row, members)

    assert answer["deleted"] == 0 and "nothing deleted" in answer["refused"][0]
    assert net.delete_requests == [] and all(f"{FOLDER}/{name}" in net.source for name in MEMBERS)


def test_the_newest_chains_directory_stays_on_the_server(net):
    row, members = _directory(net, ended="2026-11-10T00:30:00Z")
    answer, _catalog = _release(net, row, members)
    assert answer == {"deleted": 0, "kept": 1, "refused": []} and net.delete_requests == []


def test_an_encrypted_directory_is_released_by_its_age_files_then_file_by_file(net):
    row, members = _directory(net)
    for name, data in MEMBERS.items():
        del net.repo[f"{REPO_FOLDER}/{name}"]
        net.repo[f"{REPO_FOLDER}/{name}.age"] = b"age " + data
    sealed = {9: [{"file_id": 9, "encrypted_path": f"{REPO_FOLDER}/{name}.age", "encrypted_size_bytes": len(data) + 4,
                   "encrypted_sha256": _sha(b"age " + data)} for name, data in MEMBERS.items()]}

    answer, _catalog = _release(net, row, members, encrypted={9}, sealed=sealed)

    assert answer["deleted"] == 1 and not [p for p in net.source if p.startswith(FOLDER + "/")]


# --------------------------------------------------------------------------- #
# The newest chain is the database's on its server, across its entries
# --------------------------------------------------------------------------- #
@pytest.fixture
def catalog(tmp_path):
    from dbabrain.db.backup_catalog import BackupCatalogStore

    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _run(catalog, backup_id, file_type, name, ended):
    catalog.record_backup(run={"backup_id": backup_id, "server_id": SRC, "engine": "postgresql",
                               "started_at": ended, "status": "DONE"},
                          files=[{"backup_id": backup_id, "server_id": SRC, "database_name": "cluster",
                                  "engine": "postgresql", "file_name": name, "file_type": file_type,
                                  "file_path": f"/b/{name}", "backup_ended_at": ended, "size_bytes": 4,
                                  "checksum_after_backup": _sha(name.encode()), "verdict": "ok",
                                  "on_disk": 1, "checksum_matches": 1}])
    with catalog.connect() as conn:
        file_id = conn.execute("SELECT file_id FROM backup_files WHERE file_name = ?", (name,)).fetchone()[0]
    catalog.set_backup_file(file_id, offload_state="verified", offload_server_id=REPO,
                            offload_path=f"/repository/{name}", offload_run_id=1, offload_verified_at=ended)
    return file_id


def test_a_wal_entry_beside_the_full_entry_releases_what_is_older_than_the_newest_full(catalog):
    _run(catalog, "STORE", "FULL_BASE", "FULL_1", "2026-11-01T00:00:00Z")
    _run(catalog, "STORE_WAL", "WAL", "WAL_A", "2026-11-02T00:00:00Z")
    _run(catalog, "STORE", "FULL_BASE", "FULL_2", "2026-11-05T00:00:00Z")
    _run(catalog, "STORE_WAL", "WAL", "WAL_B", "2026-11-06T00:00:00Z")

    assert [r["file_name"] for r in catalog.files_to_release(backup_id="STORE_WAL")] == ["WAL_A"], \
        "older than the server's newest full - the WAL entry has no full of its own"
    assert [r["file_name"] for r in catalog.files_to_release(backup_id="STORE")] == ["FULL_1"]
    assert catalog.newest_full_per_database(backup_id="STORE_WAL") == [
        {"database_name": "cluster", "at": "2026-11-05T00:00:00Z"}]


def test_another_servers_full_never_moves_this_servers_floor(catalog):
    _run(catalog, "STORE_WAL", "WAL", "WAL_A", "2026-11-02T00:00:00Z")
    catalog.record_backup(run={"backup_id": "OTHER", "server_id": "A1A-OTHER-01", "engine": "postgresql",
                               "started_at": "2026-11-05T00:00:00Z", "status": "DONE"},
                          files=[{"backup_id": "OTHER", "server_id": "A1A-OTHER-01", "database_name": "cluster",
                                  "engine": "postgresql", "file_name": "OTHER_FULL", "file_type": "FULL_BASE",
                                  "file_path": "/b/OTHER_FULL", "backup_ended_at": "2026-11-05T00:00:00Z",
                                  "size_bytes": 4, "checksum_after_backup": _sha(b"o"), "verdict": "ok",
                                  "on_disk": 1, "checksum_matches": 1}])
    assert catalog.files_to_release(backup_id="STORE_WAL") == [], "no full of ITS server: nothing is older"


# --------------------------------------------------------------------------- #
# offload_source_keeps_newest_chain false: nothing stays on the server but the newest manifest
# (the operator, 2026-11-11: *a running server never restores - no backup needs to stay on the server_id*)
# --------------------------------------------------------------------------- #
class _NewestCatalog(_Catalog):
    def __init__(self, members, newest_dir):
        super().__init__(members, newest="2026-11-01T00:00:00Z")
        self.newest_dir = newest_dir

    def newest_base_directory(self, **_k):
        return self.newest_dir


def _release_all(net, row, members, *, newest_dir):
    entry = _entry(db_type="postgresql", offload=_offload(delete_source=True, source_keeps_newest_chain=False))
    catalog = _NewestCatalog(members, newest_dir)
    return O.release_sources(entry, catalog=catalog, hosts=_hosts(entry), rows=[row]), catalog


def test_the_newest_base_keeps_only_its_manifest_and_its_folder(net):
    row, members = _directory(net, ended="2026-11-10T00:30:00Z")

    answer, catalog = _release_all(net, row, members, newest_dir=9)

    assert answer == {"deleted": 1, "kept": 0, "refused": []}
    left = sorted(p for p in net.source if p.startswith(FOLDER + "/"))
    assert left == [f"{FOLDER}/backup_manifest"], "the next incremental chains onto it"
    assert FOLDER + "/" not in net.deleted, "a folder with a file in it is never deleted"
    assert sorted(r["file_name"] for r in net.delete_requests) == ["base.tar.zst", "pg_wal.tar"]
    assert catalog.set == [(9, {"pruned_by": O.MANIFEST_KEPT})], "still present: its manifest is on the server"


def test_once_a_newer_base_is_there_the_manifest_goes_then_the_empty_folder(net):
    row, members = _directory(net, ended="2026-11-10T00:30:00Z")
    for name in ("base.tar.zst", "pg_wal.tar"):
        del net.source[f"{FOLDER}/{name}"]          # released while it was the newest

    answer, catalog = _release_all(net, row, members, newest_dir=10)

    assert answer["deleted"] == 1 and not [p for p in net.source if p.startswith(FOLDER + "/")]
    assert net.deleted[-1] == FOLDER + "/"
    assert catalog.set[0][1]["state"] == "pruned" and catalog.set[0][1]["source_deleted_at"]


def test_the_newest_chains_files_are_released_when_the_server_keeps_no_chain(net):
    row = {"file_id": 5, "file_name": "000000010000000000000001", "file_type": "WAL", "database_name": "cluster",
           "file_path": "/var/opt/mssql/backup/wal/000000010000000000000001",
           "host_path": f"{HOST_DIR}/wal/000000010000000000000001",
           "offload_path": f"/repository/{SRC}/cluster/wal/000000010000000000000001",
           "backup_ended_at": "2026-11-10T01:00:00Z", "size_bytes": 3, "checksum_after_backup": _sha(b"wal")}
    net.source[row["host_path"]] = b"wal"
    net.repo[row["offload_path"]] = b"wal"

    answer, _catalog = _release_all(net, row, [], newest_dir=None)

    assert answer["deleted"] == 1 and row["host_path"] not in net.source, "inside the newest chain, and released"


def test_by_default_the_newest_chain_stays_as_it_did(net):
    row, members = _directory(net, ended="2026-11-10T00:30:00Z")
    answer, _catalog = _release(net, row, members)
    assert answer == {"deleted": 0, "kept": 1, "refused": []}


def test_the_catalog_offers_the_newest_chain_and_holds_back_a_manifest_left_for_the_next_incremental(catalog):
    full = _run(catalog, "STORE", "FULL_BASE", "FULL_1", "2026-11-01T00:00:00Z")
    _run(catalog, "STORE_WAL", "WAL", "WAL_A", "2026-11-02T00:00:00Z")

    assert [r["file_name"] for r in catalog.files_to_release(backup_id="STORE")] == []
    assert [r["file_name"] for r in catalog.files_to_release(backup_id="STORE", keep_newest_chain=False)] == ["FULL_1"]
    assert [r["file_name"] for r in catalog.files_to_release(backup_id="STORE_WAL", keep_newest_chain=False)] == ["WAL_A"]

    catalog.set_backup_file(full, pruned_by=O.MANIFEST_KEPT)
    assert catalog.files_to_release(backup_id="STORE", keep_newest_chain=False) == [], "it waits for a newer base"
    assert catalog.newest_base_directory(server_id=SRC, database_name="cluster") == full

    newer = _run(catalog, "STORE", "INCR_BASE", "INCR_1", "2026-11-03T00:00:00Z")
    assert [r["file_name"] for r in catalog.files_to_release(backup_id="STORE", keep_newest_chain=False)] == [
        "FULL_1", "INCR_1"]
    assert catalog.newest_base_directory(server_id=SRC, database_name="cluster") == newer


def test_a_file_written_before_files_since_is_never_offered_to_the_repository(catalog):
    for name, ended in (("OLD_LOG", "2026-11-10T00:00:00Z"), ("NEW_FULL", "2026-11-11T04:31:00Z"),
                        ("NEW_LOG", "2026-11-11T04:40:00Z")):
        catalog.record_backup(run={"backup_id": "MSSQL", "server_id": SRC, "engine": "sqlserver",
                                   "started_at": ended, "status": "DONE"},
                              files=[{"backup_id": "MSSQL", "server_id": SRC, "database_name": "APPDB",
                                      "engine": "sqlserver", "file_name": name, "file_type": "LOG",
                                      "file_path": f"/b/{name}", "backup_ended_at": ended, "size_bytes": 4,
                                      "checksum_after_backup": _sha(name.encode()), "verdict": "ok",
                                      "on_disk": 1, "checksum_matches": 1}])
    every = [r["file_name"] for r in catalog.files_to_offload(backup_id="MSSQL")]
    since = [r["file_name"] for r in catalog.files_to_offload(backup_id="MSSQL", since="2026-11-11T04:30:00Z")]
    assert every == ["OLD_LOG", "NEW_FULL", "NEW_LOG"] and since == ["NEW_FULL", "NEW_LOG"]
