"""A PostgreSQL base backup is catalogued file by file, each its own row (0.40.0, 1.386).

The operator, 2026-11-16: *the catalog records every file*, and *the design as 3NF - no two fields
holding the same value*. A base backup was ONE row for its directory, its manifest's sha256 standing
for every file in it: a restore read from the server then found ``base.tar.zst`` in no row, and an
entry that refuses an uncatalogued file could never restore PostgreSQL from its server (the 1.385 drill).
Now each file - ``base.tar.zst``, ``pg_wal.tar``, each tablespace's tar, ``backup_manifest`` - is a row
of its backup's run, of the backup's level, named from its directory on so a name is still one file
(``20261116T093216Z_FULL/base.tar.zst``); the WAL job records the ``.backup`` history file too.

What must hold, and would go wrong unseen otherwise: a name is one file on its server (two backups'
``base.tar.zst`` never tied to each other); the newest base's manifest stays at its source, the next
incremental chains onto it; a folder emptied at its source goes, as a directory always did; a base
catalogued before as its directory stays readable in that shape until its retention removes it.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from dbabrain.backup_restore import offload as O
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import integrity as I
from tests.test_every_backup_is_copied_off_its_server_before_it_is_released import (
    HOST_DIR, SRC, _entry, _hosts, _Net, _offload, _sha)
from tests.test_every_backup_run_records_its_files_and_their_checksums import _bash_block

BASH = shutil.which("bash")
STAMP = "20261101T000000Z_FULL"
FOLDER = f"{HOST_DIR}/base/{STAMP}"
REPO_FOLDER = f"/repository/{SRC}/cluster/base/{STAMP}"
MEMBERS = {"base.tar.zst": b"the cluster's files", "pg_wal.tar": b"the wal of its window",
           "backup_manifest": b"the manifest"}


# --------------------------------------------------------------------------- #
# The name a file is catalogued under
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path, name", [
    ("/b/base/20261116T093216Z_FULL/base.tar.zst", "20261116T093216Z_FULL/base.tar.zst"),
    ("/b/base/20261116T093216Z_INCR/backup_manifest", "20261116T093216Z_INCR/backup_manifest"),
    ("/b/base/20261116T093216Z_FULL/global/pg_control", "20261116T093216Z_FULL/global/pg_control"),
    ("/b/base/20261116T093216Z_FULL", "20261116T093216Z_FULL"),
    ("/b/wal/000000010000000000000003.00000028.backup", "000000010000000000000003.00000028.backup"),
    ("F:\\BK\\APP\\FULL\\APP_FULL_20261116_010000Z.bak", "APP_FULL_20261116_010000Z.bak"),
])
def test_a_name_is_one_file_on_its_server(path, name):
    assert I.catalog_name(path) == name


def test_a_directory_row_is_only_one_named_as_a_base_directory():
    assert I.is_base_directory("FULL_BASE", "20261116T093216Z_FULL")
    assert not I.is_base_directory("FULL_BASE", "20261116T093216Z_FULL/base.tar.zst")
    assert not I.is_base_directory("FULL", "20261116T093216Z_FULL")
    assert I.directory_member("INCR_BASE", "20261116T093216Z_INCR") == "backup_manifest"
    assert I.directory_member("FULL_BASE", "20261116T093216Z_FULL/backup_manifest") is None
    assert I.hashed_path("FULL_BASE", "/b/base/20261116T093216Z_FULL") == "/b/base/20261116T093216Z_FULL/backup_manifest"
    assert I.hashed_path("FULL_BASE", "/b/base/20261116T093216Z_FULL/base.tar.zst").endswith("/base.tar.zst")


# --------------------------------------------------------------------------- #
# The receipt: one FILE line per file, each its own size and sha256
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_the_receipt_says_every_file_of_the_backup_each_with_its_own_facts(tmp_path):
    target = tmp_path / "base" / STAMP
    target.mkdir(parents=True)
    for name, data in MEMBERS.items():
        (target / name).write_bytes(data)
    block = _bash_block("postgresql/pg_basebackup_database.sh", "# One FILE line per file of the backup",
                        '[ "$verify" = "failed" ]')
    harness = "\n".join([
        'die() { echo "DIE $1"; exit 1; }', 'run_db() { bash -c "$1" </dev/null; }',
        'host_path_of() { printf "/host%s" "$1"; }',
        f"target='{target.as_posix()}'", "file_type=FULL_BASE", "verify=passed", "verify_note='checked'",
        "started=2026-11-01T00:00:00Z", "container=''", block])
    out = subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60).stdout
    files = {f["file_name"]: f for f in I.parse_file_lines(out)}

    assert sorted(files) == sorted(f"{STAMP}/{name}" for name in MEMBERS)
    for name, data in MEMBERS.items():
        row = files[f"{STAMP}/{name}"]
        assert (row["file_type"], row["size_bytes"], row["sha256"]) == ("FULL_BASE", len(data), _sha(data))
        assert row["host_path"].endswith(f"/{STAMP}/{name}") and row["engine_verify"] == "passed"
    assert files[f"{STAMP}/backup_manifest"]["file_type"] == "FULL_BASE", "the manifest is of its backup's level"


def test_the_wal_job_takes_the_backup_history_file_after_its_segment():
    text = open("dbabrain/common/backup_scripts/postgresql/pg_archive_wal.sh", encoding="utf-8").read()
    assert "grep -E '^[0-9A-F]{24}(\\.[0-9A-F]{8}\\.backup)?$'" in text
    assert I.WAL_HISTORY_NAME.match("000000010000000000000003.00000028.BACKUP")
    assert not I.WAL_SEGMENT_NAME.match("000000010000000000000003.00000028.BACKUP"), "not a segment to replay"


# --------------------------------------------------------------------------- #
# The catalog: rows of the run, their level, their history
# --------------------------------------------------------------------------- #
@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _base_run(catalog, stamp, ended, *, level="full", sizes=None):
    file_type = "FULL_BASE" if level == "full" else "INCR_BASE"
    folder = f"{HOST_DIR}/base/{stamp}"
    files = [{"backup_id": "SRC_PG", "server_id": SRC, "engine": "postgresql", "database_name": "cluster",
              "file_type": file_type,
              "file_name": f"{stamp}/{name}", "file_path": f"{folder}/{name}", "host_path": f"{folder}/{name}",
              "verdict": "ok", "size_bytes": (sizes or {}).get(name, len(data)), "checksum_after_backup": _sha(data),
              "backup_ended_at": ended}
             for name, data in MEMBERS.items()]
    return catalog.record_backup(run={"backup_id": "SRC_PG", "server_id": SRC, "engine": "postgresql",
                                      "level": level, "started_at": ended, "ended_at": ended, "status": "DONE"},
                                 files=files)


def test_a_restore_from_the_server_finds_each_file_by_its_catalogued_name(catalog):
    _base_run(catalog, "20261101T000000Z_FULL", "2026-11-01T00:30:00Z")
    _base_run(catalog, "20261102T000000Z_FULL", "2026-11-02T00:30:00Z")
    rows = catalog.stored_files(server_id=SRC, file_names=[
        I.catalog_name(f"base/{stamp}/base.tar.zst") for stamp in ("20261101T000000Z_FULL", "20261102T000000Z_FULL")])
    assert {row["file_path"] for row in rows.values()} == {
        f"{HOST_DIR}/base/20261101T000000Z_FULL/base.tar.zst", f"{HOST_DIR}/base/20261102T000000Z_FULL/base.tar.zst"}, \
        "each backup's base.tar.zst is its own row, never the newest's"


def test_a_files_size_is_held_to_the_same_part_of_earlier_backups(catalog):
    first = _base_run(catalog, "20261101T000000Z_FULL", "2026-11-01T00:30:00Z")
    _base_run(catalog, "20261102T000000Z_FULL", "2026-11-02T00:30:00Z")
    assert catalog.earlier_sizes(backup_id="SRC_PG", database_name="cluster", file_type="FULL_BASE",
                                 before_run=first + 1, file_name="20261102T000000Z_FULL/backup_manifest") == [
        len(MEMBERS["backup_manifest"])], "a manifest beside manifests, never beside a base.tar.zst"


def test_the_newest_base_manifest_is_known_and_never_listed_to_release(catalog):
    _base_run(catalog, "20261101T000000Z_FULL", "2026-11-01T00:30:00Z")
    _base_run(catalog, "20261102T000000Z_INCR", "2026-11-02T00:30:00Z", level="diff")
    with catalog.connect() as conn:
        conn.execute("UPDATE backup_files SET offload_state = 'verified', offload_server_id = 'REPO', "
                     "offload_path = file_path, offload_run_id = 1, offload_verified_at = '2026-11-02T01:00:00Z'")
    newest = catalog.newest_base_manifest(server_id=SRC, database_name="cluster")
    listed = {row["file_name"] for row in catalog.files_to_release(backup_id="SRC_PG", keep_newest_chain=False)}
    assert newest is not None and "20261102T000000Z_INCR/backup_manifest" not in listed
    assert {"20261101T000000Z_FULL/backup_manifest", "20261102T000000Z_INCR/base.tar.zst"} <= listed
    assert catalog.base_files_present(server_id=SRC, directory="20261101T000000Z_FULL") == 3


# --------------------------------------------------------------------------- #
# Released at the source: file by file, the newest manifest kept, the emptied folder gone
# --------------------------------------------------------------------------- #
@pytest.fixture
def net(monkeypatch):
    fake = _Net()
    monkeypatch.setattr(O.common_cli, "run", fake.run)
    monkeypatch.setattr(O.common_cli, "run_allowing_failure", fake.run_allowing_failure)
    return fake


class _Catalog:
    def __init__(self, rows, *, newest_manifest=None):
        self.rows, self.newest_manifest, self.set = rows, newest_manifest, []

    def newest_full_per_database(self, **_k):
        return [{"database_name": "cluster", "at": "2026-11-10T00:00:00Z"}]

    def newest_base_manifest(self, **_k):
        return self.newest_manifest

    def base_files_present(self, *, server_id, directory):
        gone = {file_id for file_id, values in self.set if values.get("state") == "pruned"}
        return sum(1 for row in self.rows if row["file_name"].startswith(directory + "/") and row["file_id"] not in gone)

    def set_backup_file(self, file_id, **values):
        self.set.append((file_id, values))


def _rows(net):
    rows = []
    for number, (name, data) in enumerate(MEMBERS.items(), start=11):
        net.source[f"{FOLDER}/{name}"] = data
        net.repo[f"{REPO_FOLDER}/{name}"] = data
        rows.append({"file_id": number, "file_name": f"{STAMP}/{name}", "file_type": "FULL_BASE",
                     "database_name": "cluster", "server_id": SRC,
                     "file_path": f"/var/opt/mssql/backup/base/{STAMP}/{name}", "host_path": f"{FOLDER}/{name}",
                     "offload_path": f"{REPO_FOLDER}/{name}", "backup_ended_at": "2026-11-01T00:30:00Z",
                     "size_bytes": len(data), "checksum_after_backup": _sha(data)})
    return rows


def _release(rows, catalog, *, keep_chain):
    entry = _entry(db_type="postgresql", offload=_offload(delete_source=True, source_keeps_newest_chain=keep_chain))
    return O.release_sources(entry, catalog=catalog, hosts=_hosts(entry), rows=rows)


def test_each_file_goes_by_its_own_facts_and_then_its_emptied_folder(net):
    rows = _rows(net)
    answer = _release(rows, _Catalog(rows), keep_chain=False)

    assert answer["deleted"] == 3 and answer["refused"] == []
    assert not [path for path in net.source if path.startswith(FOLDER + "/")]
    assert sorted(r["file_name"] for r in net.delete_requests) == sorted(MEMBERS), \
        "the command is told each file's own last name"
    assert net.deleted[-1] == FOLDER + "/", "the folder goes once nothing of it is left"


def test_the_newest_bases_manifest_stays_and_so_does_its_folder(net):
    rows = _rows(net)
    manifest = next(row for row in rows if row["file_name"].endswith("/backup_manifest"))
    answer = _release(rows, _Catalog(rows, newest_manifest=manifest["file_id"]), keep_chain=False)

    assert answer["deleted"] == 2 and answer["kept"] == 1
    assert f"{FOLDER}/backup_manifest" in net.source, "the next incremental chains onto it"
    assert FOLDER + "/" not in net.deleted


def test_a_catalog_name_that_is_not_the_files_is_never_sent_to_delete(net):
    rows = _rows(net)
    rows[0]["file_name"] = "20261101T000000Z_FULL/something_else"
    answer = _release(rows[:1], _Catalog(rows[:1]), keep_chain=False)
    assert answer["deleted"] == 0 and "is not the name" in answer["refused"][0] and net.delete_requests == []
