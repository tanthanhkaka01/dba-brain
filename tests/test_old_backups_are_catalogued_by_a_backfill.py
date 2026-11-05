"""The backups taken before the catalog are hashed where they lie and recorded (0.34.0, 1.215).

Why it matters:
- A restore's gate holds every copied file to the checksum its backup recorded (1.213).
- Every backup taken before 0.34.0 has none. The gate can then only compare the copy with the
  source, and an entry that says ``uncatalogued: refuse`` cannot restore it at all.
- ``catalog-backfill`` hashes each such file on its own server and records it with
  ``baseline='backfill'``. That proves the file has not changed since then - not that it was good
  when written - and the row says so: ``engine_verify='not_run'``.
- It is a dry run unless asked. It never touches a file the catalog already holds, nor one written
  minutes ago (a backup running now).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import backup as backup_module
from dbabrain.common import filechecksum
from dbabrain.db.backup_catalog import BackupCatalogStore


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# file-checksum, the folder form
# --------------------------------------------------------------------------- #
def test_a_folder_is_listed_and_only_the_matching_files_hashed(tmp_path):
    (tmp_path / "HR" / "FULL").mkdir(parents=True)
    (tmp_path / "HR" / "LOG").mkdir()
    (tmp_path / "HR" / "FULL" / "HR_FULL_1.BAK").write_bytes(b"full")
    (tmp_path / "HR" / "LOG" / "HR_LOG_1.trn").write_bytes(b"log")
    (tmp_path / "notes.txt").write_bytes(b"not a backup")

    answer = filechecksum.checksum({"folder": str(tmp_path), "include": ["*.bak", "*.trn"]})

    assert answer["folder_found"] is True and answer["listed"] == 2 and answer["truncated"] is False
    assert sorted(f["sha256"] for f in answer["files"]) == sorted([sha(b"full"), sha(b"log")]), \
        "patterns match whatever the case of the name"


def test_a_missing_folder_is_an_answer_and_too_many_files_are_said(tmp_path):
    assert filechecksum.checksum({"folder": str(tmp_path / "gone")})["folder_found"] is False
    for index in range(3):
        (tmp_path / f"f{index}.bak").write_bytes(b"x")
    answer = filechecksum.checksum({"folder": str(tmp_path), "max_files": 2})
    assert answer["file_count"] == 2 and answer["listed"] == 3 and answer["truncated"] is True


def test_paths_and_folder_are_one_or_the_other():
    with pytest.raises(filechecksum.FileChecksumError, match="paths or folder, not both"):
        filechecksum.checksum({"folder": "/b", "paths": ["/b/x.bak"]})


def test_a_windows_folder_is_listed_in_one_powershell_2_command(monkeypatch):
    sent: list = []

    def run(host, script, timeout=None, session=None):
        sent.append(script)
        return {"exit_code": 0, "stdout": "FILE|D:\\SQLBK\\HR\\FULL\\HR_FULL_1.bak\r\nnoise\r\n", "stderr": ""}

    monkeypatch.setattr(filechecksum, "run", run)
    host = filechecksum.parse_host({"runtime": "windows", "access": "winrm", "host": "192.0.2.245"})
    assert filechecksum._list_folder(host, "D:\\SQLBK", timeout=60) == ["D:\\SQLBK\\HR\\FULL\\HR_FULL_1.bak"]
    assert "Get-ChildItem" in sent[0] and "PSIsContainer" in sent[0], "PowerShell 2.0 has no -File"


def test_a_containers_folder_is_read_through_its_mounts_on_the_host(monkeypatch):
    from dbabrain.common.restorestep import postgresql

    monkeypatch.setattr(postgresql, "_container_mounts", lambda host, container, sudo: [
        {"Destination": "/var/opt/mssql", "Source": "/srv/mssql"},
        {"Destination": "/var/opt/mssql/backup", "Source": "/data/mssql_backup"}])
    host = filechecksum.parse_host({"runtime": "linux", "host": "192.0.2.40"})
    assert filechecksum._folder_on_the_host(host, "/var/opt/mssql/backup", "mssql") == "/data/mssql_backup", \
        "the longest mount wins"


# --------------------------------------------------------------------------- #
# catalog-backfill
# --------------------------------------------------------------------------- #
NOW = dt.datetime(2026, 10, 29, 12, 0, tzinfo=dt.timezone.utc)
OLD = "2026-10-20T01:00:00Z"


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _backfill(monkeypatch, catalog, files, *, apply, engine="sqlserver", folder="D:\\SQLBK_DBOPS", env=None):
    from dbabrain.backup_restore import spec_builder

    job = SimpleNamespace(backup_id="ACME_MSSQL_SALES_FULL", job="full", db_type=engine,
                          server_id="ACME-192-0-2-245", backup_dir=folder, env=env or {})
    monkeypatch.setattr(backup_module, "load_backup_jobs", lambda _p: [job])
    monkeypatch.setattr(backup_module.BackupCatalogStore, "from_config", classmethod(lambda cls, c: catalog))
    monkeypatch.setattr(backup_module, "resolve_backup_target", lambda *a, **k: SimpleNamespace(
        key_file="", password_ref="", container_name=""))
    monkeypatch.setattr(spec_builder, "host_block", lambda *a, **k: {"runtime": "windows", "host": "192.0.2.245"})
    asked: list = []
    monkeypatch.setattr(backup_module.common_cli, "run", lambda command, request: asked.append(request) or {
        "folder": request["folder"], "host_folder": folder, "folder_found": True,
        "files": files, "truncated": False})
    summary = backup_module.catalog_backfill(app_config=None, config_path="x", backup_id=job.backup_id,
                                             apply=apply, now=NOW)
    return summary, asked


def _file(rel, body, mtime=OLD):
    return {"path": "D:\\SQLBK_DBOPS\\" + rel.replace("/", "\\"), "sha256": sha(body), "size_bytes": len(body),
            "mtime_utc": mtime}


FILES = [_file("HR/FULL/HR_FULL_20261020.bak", b"full"), _file("HR/LOG/HR_LOG_20261020.trn", b"log"),
         _file("_cert/dbabrain_backup_cert.cer", b"cert"),
         _file("HR/LOG/HR_LOG_20261029_1158.trn", b"new", mtime="2026-10-29T11:58:00Z")]


def test_a_dry_run_says_what_it_would_write_and_writes_nothing(monkeypatch, catalog):
    summary, asked = _backfill(monkeypatch, catalog, FILES, apply=False)

    assert asked[0]["include"] == ["*.bak", "*.trn", "*.cer", "*.pvk"] and asked[0]["folder"] == "D:\\SQLBK_DBOPS"
    assert (summary["would_write"], summary["written"], summary["too_new"]) == (3, 0, 1)
    assert catalog.stored_files(server_id="ACME-192-0-2-245", file_names=["HR_FULL_20261020.bak"]) == {}


def test_applied_each_old_file_is_recorded_as_a_backfill_typed_by_its_place(monkeypatch, catalog):
    summary, _ = _backfill(monkeypatch, catalog, FILES, apply=True)

    assert summary["written"] == 3 and summary["too_new"] == 1, "a file written two minutes ago is left"
    rows = catalog.stored_files(server_id="ACME-192-0-2-245",
                                file_names=["HR_FULL_20261020.bak", "HR_LOG_20261020.trn", "dbabrain_backup_cert.cer"])
    full = rows["HR_FULL_20261020.bak"]
    assert (full["file_type"], full["database_name"], full["baseline"], full["engine_verify"]) == \
        ("FULL", "HR", "backfill", "not_run")
    assert full["checksum_after_backup"] == sha(b"full") and full["size_bytes"] == 4
    assert full["file_path"] == full["host_path"] == "D:\\SQLBK_DBOPS\\HR\\FULL\\HR_FULL_20261020.bak"
    assert rows["HR_LOG_20261020.trn"]["file_type"] == "LOG"
    assert (rows["dbabrain_backup_cert.cer"]["file_type"], rows["dbabrain_backup_cert.cer"]["database_name"]) == ("CERT", "")


def test_a_second_pass_leaves_what_the_first_catalogued(monkeypatch, catalog):
    _backfill(monkeypatch, catalog, FILES, apply=True)
    summary, _ = _backfill(monkeypatch, catalog, FILES, apply=True)

    assert (summary["written"], summary["catalogued_already"]) == (0, 3)


def test_an_engine_without_a_known_layout_is_refused_by_name(monkeypatch, catalog):
    with pytest.raises(ValueError, match="a mysql entry has none it knows"):
        _backfill(monkeypatch, catalog, FILES, apply=False, engine="mysql")


# --------------------------------------------------------------------------- #
# PostgreSQL and Oracle (0.35.0, the rest of 1.215)
# --------------------------------------------------------------------------- #
PG = "/opt/backup/pg"


def _linux(root, rel, body, mtime=OLD):
    return {"path": f"{root}/{rel}", "sha256": sha(body), "size_bytes": len(body), "mtime_utc": mtime}


PG_FILES = [_linux(PG, "base/20261020T010000Z_FULL/backup_manifest", b"manifest-full"),
            _linux(PG, "base/20261021T010000Z_INCR/backup_manifest", b"manifest-incr"),
            _linux(PG, "wal/000000010000000000000042", b"segment"),
            _linux(PG, "wal/000000010000000000000043.partial", b"partial"),
            _linux(PG, "wal/00000002.history", b"history"),
            _linux(PG, "base/20261020T010000Z_FULL/pg_wal/000000010000000000000041", b"inside")]


def test_a_postgresql_base_backup_is_catalogued_as_its_directory_by_its_manifest(monkeypatch, catalog):
    """As the receipt does: one row per base backup, its directory, the sha256 of its manifest."""
    summary, asked = _backfill(monkeypatch, catalog, PG_FILES, apply=True, engine="postgresql", folder=PG)

    assert asked[0]["include"] == ["backup_manifest", "?" * 24]
    rows = catalog.stored_files(server_id="ACME-192-0-2-245", file_names=[
        "20261020T010000Z_FULL", "20261021T010000Z_INCR", "000000010000000000000042"])
    full = rows["20261020T010000Z_FULL"]
    assert (full["file_type"], full["database_name"], full["checksum_after_backup"]) == \
        ("FULL_BASE", "cluster", sha(b"manifest-full"))
    assert full["file_path"] == full["host_path"] == f"{PG}/base/20261020T010000Z_FULL"
    assert (full["size_bytes"], full["verdict"]) == (None, "size_failed"), "the manifest's size is not the directory's"
    assert rows["20261021T010000Z_INCR"]["file_type"] == "INCR_BASE"
    wal = rows["000000010000000000000042"]
    assert (wal["file_type"], wal["size_bytes"]) == ("WAL", 7)
    # A .partial or .history file, and a segment inside a base backup's own pg_wal, are not catalogued.
    assert summary["written"] == 3 and summary["not_a_backup"] == 3


ORA = "/opt/oracle/backup"
ORA_FILES = [_linux(ORA, "FREE_L0_20261020_0a1b2c3d_1_1.bkp", b"l0"),
             _linux(ORA, "FREE_L1_20261021_0e4f5a6b_1_1.bkp", b"l1"),
             _linux(ORA, "arch_FREE_20261021_1a2b3c4d_1_1.bkp", b"arch"),
             _linux(ORA, "ctl_FREE_20261021_2b3c4d5e_1_1.bkp", b"ctl"),
             _linux(ORA, "spfile_FREE_20261021_3c4d5e6f_1_1.bkp", b"spfile"),
             _linux(ORA, "autobackup_c-1234567890-20261021-00", b"auto")]


def test_rman_pieces_are_typed_by_the_names_the_oracle_scripts_give_them(monkeypatch, catalog):
    summary, asked = _backfill(monkeypatch, catalog, ORA_FILES, apply=True, engine="oracle", folder=ORA,
                               env={"ORACLE_SID": "FREE"})

    assert asked[0]["include"] == ["*.bkp", "autobackup_*", "c-*"] and summary["written"] == 6
    rows = catalog.stored_files(server_id="ACME-192-0-2-245", file_names=[f["path"].rsplit("/", 1)[-1] for f in ORA_FILES])
    assert {name: row["file_type"] for name, row in rows.items()} == {
        "FREE_L0_20261020_0a1b2c3d_1_1.bkp": "RMAN_L0", "FREE_L1_20261021_0e4f5a6b_1_1.bkp": "RMAN_L1",
        "arch_FREE_20261021_1a2b3c4d_1_1.bkp": "ARCHIVELOG", "ctl_FREE_20261021_2b3c4d5e_1_1.bkp": "CONTROLFILE",
        "spfile_FREE_20261021_3c4d5e6f_1_1.bkp": "SPFILE", "autobackup_c-1234567890-20261021-00": "CONTROLFILE"}
    assert {row["database_name"] for row in rows.values()} == {"FREE"}
    assert "typed by its piece name" in rows["FREE_L0_20261020_0a1b2c3d_1_1.bkp"]["engine_verify_text"]
