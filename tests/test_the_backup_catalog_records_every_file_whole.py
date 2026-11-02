"""The backup catalog holds every file whole, and an entry always states its ``integrity`` (0.34.0, 1.211).

Why it matters: the catalog is the evidence a person reads after a bad night - which file, on which
server, with which checksum, copied how many times. The operator, 2026-10-29: *the DB must be
complete - source and target server_id, file name, file type, the checksum before and after the
copy; the same for every backup without the target half; nullable virus-scan columns*. So the store
itself refuses what would make that evidence wrong: a "proven" copy whose checksum is not the
source's, a file type nobody can group by, a backup checksum rewritten after the fact.

And ``copy_max_retry`` is *mandatory, default 3, declared in full in the shared object json*: the
reference requires it on every restore entry, ``upgrade-config`` writes it where it is missing,
``restore-add`` writes it into every new entry, and the parser refuses a block without it.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from dbabrain.backup_restore import registration
from dbabrain.common import config_upgrade
from dbabrain.db.backup_catalog import TABLES, BackupCatalogError, BackupCatalogStore
from dbabrain.lib import integrity as I
from dbabrain.lib import shared_objects

from tests.test_registering_a_backup_a_restore_and_an_os_login import KEY, _restore

SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _raw(catalog):
    return sqlite3.connect(catalog.sqlite_path)


def _file(**over):
    values = dict(server_id="ACME-192-0-2-250", database_name="APPDB_PROD", engine="sqlserver",
                  file_name="APPDB_PROD_FULL_20261026_0100.bak", file_type="FULL",
                  file_path=r"D:\SQLBK\APPDB_PROD\FULL\APPDB_PROD_FULL_20261026_0100.bak",
                  checksum_after_backup=SHA_A, verdict="ok", size_bytes=61203447808)
    values.update(over)
    return values


# --------------------------------------------------------------------------- #
# The tables
# --------------------------------------------------------------------------- #
def test_all_six_tables_are_created_with_their_keys(catalog):
    with _raw(catalog) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert set(TABLES) <= names
        fks = {table: {row[2] for row in conn.execute(f"PRAGMA foreign_key_list({table})")}
               for table in TABLES}
    assert fks["backup_files"] == {"backup_runs", "backup_files"}
    assert fks["backup_file_checks"] == {"backup_files"}
    assert fks["restore_run_files"] == {"restore_runs", "backup_files"}
    assert fks["copy_attempts"] == {"restore_run_files"}
    # job_runs ages out into its archive; a real key into it blocked that sweep once (run_requests.py).
    assert all("job_runs" not in parents for parents in fks.values())


def test_a_backup_run_and_its_files_read_back_whole(catalog):
    run = catalog.start_backup_run(backup_id="ACME_250_APPDB_LOG", server_id="ACME-192-0-2-250",
                                   engine="sqlserver", level="log", job_run_id=4242)
    full = catalog.record_backup_file(**_file(backup_run_id=run, backup_id="ACME_250_APPDB_LOG"))
    catalog.record_backup_file(**_file(backup_run_id=run, file_type="LOG", chain_parent_file_id=full,
                                       file_name="APPDB_PROD_LOG_20261029_0315.trn",
                                       file_path=r"D:\SQLBK\APPDB_PROD\LOG\APPDB_PROD_LOG_20261029_0315.trn",
                                       checksum_after_backup=SHA_B, engine_verify="passed"))
    catalog.finish_backup_run(run, status="DONE", files_count=2, engine_verify="passed")

    rows = catalog.backup_history(server_id="ACME-192-0-2-250")
    assert [(r["file_type"], r["file_name"], r["checksum_after_backup"]) for r in rows] == [
        ("FULL", "APPDB_PROD_FULL_20261026_0100.bak", SHA_A),
        ("LOG", "APPDB_PROD_LOG_20261029_0315.trn", SHA_B)]
    assert rows[0]["size_bytes"] == 61203447808, "a 60 GB FULL does not overflow the column"
    assert rows[1]["chain_parent_file_id"] == full
    assert all(r[col] is None for r in rows for col in I.AV_SCAN_COLUMNS), "never scanned is NULL"


def test_a_file_type_outside_the_one_list_is_refused(catalog):
    with pytest.raises(BackupCatalogError, match="file_type must be one of"):
        catalog.record_backup_file(**_file(file_type="TRN"))
    with _raw(catalog) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO backup_files (server_id, engine, file_name, file_type, file_path, "
                     "checksum_after_backup, verdict) VALUES ('S', 'sqlserver', 'x', 'log', '/x', ?, 'ok')",
                     (SHA_A,))


def test_a_backup_file_has_its_checksum_unless_it_is_recorded_as_unhashed(catalog):
    with _raw(catalog) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO backup_files (server_id, engine, file_name, file_type, file_path, verdict) "
                     "VALUES ('S', 'sqlserver', 'x.bak', 'FULL', '/x.bak', 'ok')")
    file_id = catalog.record_backup_file(**_file(checksum_after_backup=None, verdict="hash_failed"))
    assert file_id > 0
    with pytest.raises(sqlite3.IntegrityError):
        catalog.record_backup_file(**_file(file_path="/y.bak", checksum_after_backup="short"))


def test_the_checksum_a_backup_recorded_is_never_rewritten(catalog):
    file_id = catalog.record_backup_file(**_file())
    with pytest.raises(BackupCatalogError, match="never rewritten"):
        catalog.set_backup_file(file_id, checksum_after_backup=SHA_B)


def test_a_pruned_files_row_stays_and_does_not_block_a_new_file_at_its_path(catalog):
    first = catalog.record_backup_file(**_file())
    with pytest.raises(sqlite3.IntegrityError):
        catalog.record_backup_file(**_file())
    catalog.set_backup_file(first, state="pruned")
    second = catalog.record_backup_file(**_file(checksum_after_backup=SHA_B))
    assert second != first
    assert len(catalog.backup_history(server_id="ACME-192-0-2-250")) == 2


def test_a_later_check_is_kept_and_a_mismatch_marks_the_file(catalog):
    file_id = catalog.record_backup_file(**_file())
    common = dict(file_id=file_id, server_id="ACME-192-0-2-250", database_name="APPDB_PROD",
                  file_name="APPDB_PROD_FULL_20261026_0100.bak", file_type="FULL",
                  file_path=r"D:\SQLBK\APPDB_PROD\FULL\APPDB_PROD_FULL_20261026_0100.bak",
                  checksum_after_backup=SHA_A)
    catalog.record_file_check(**common, checked_by="scrub", checksum_now=SHA_A, result="match",
                              checked_at="2026-10-28T01:00:00Z")
    catalog.record_file_check(**common, checked_by="scrub", checksum_now=SHA_B, result="mismatch",
                              checked_at="2026-10-29T01:00:00Z")
    row = catalog.backup_history(server_id="ACME-192-0-2-250")[0]
    assert row["state"] == "mismatch" and row["last_check_result"] == "mismatch"
    assert row["checksum_last_check"] == SHA_B and row["checksum_after_backup"] == SHA_A
    with _raw(catalog) as conn:
        assert conn.execute("SELECT count(*) FROM backup_file_checks").fetchone()[0] == 2


def test_a_virus_scan_is_kept_as_a_check_and_on_the_file(catalog):
    file_id = catalog.record_backup_file(**_file())
    catalog.record_file_check(file_id=file_id, server_id="ACME-192-0-2-250", file_name="x.bak",
                              file_type="FULL", file_path="/x.bak", checked_by="av_scan",
                              av_scan_status="skipped", av_scan_tool="Microsoft Defender",
                              av_scan_tool_version="1.1.24090.11", av_scan_signature_version="1.419.112.0",
                              av_scan_server_id="ACME-192-0-2-250",
                              av_scan_result_text="encrypted - contents not inspectable")
    row = catalog.backup_history(server_id="ACME-192-0-2-250")[0]
    assert row["av_scan_status"] == "skipped" and row["av_scan_server_id"] == "ACME-192-0-2-250"
    assert row["state"] == "present", "a scan is not a hash check"
    with pytest.raises(BackupCatalogError, match="av_scan_status must be one of"):
        catalog.set_backup_file(file_id, av_scan_status="ok")


def test_a_proven_copy_must_carry_the_sources_checksum_the_store_holds_it(catalog):
    run = catalog.start_restore_run(action="restore", source_server_id="S", target_server_id="T",
                                    copy_max_retry=3)
    base = dict(restore_run_id=run, source_server_id="S", target_server_id="T", file_name="a.trn",
                file_type="LOG", source_path="/s/a.trn", target_path="/t/a.trn", copy_max_retry=3,
                checksum_source_before_copy=SHA_A)
    with pytest.raises(sqlite3.IntegrityError):
        catalog.add_run_file(**base, checksum_target_after_copy=SHA_B, verdict="copied")
    with pytest.raises(sqlite3.IntegrityError):
        catalog.add_run_file(**{**base, "source_path": "/s/b.trn"}, verdict="copied")
    run_file = catalog.add_run_file(**{**base, "source_path": "/s/c.trn"}, verdict="copying")
    with pytest.raises(sqlite3.IntegrityError):
        catalog.set_run_file(run_file, copy_attempts=5)  # more than copy_max_retry + 1
    with pytest.raises(sqlite3.IntegrityError):
        catalog.record_copy_attempt(run_file_id=run_file, attempt_no=1, source_server_id="S",
                                    target_server_id="T", file_name="c.trn", file_type="LOG",
                                    source_path="/s/c.trn", target_path="/t/c.trn",
                                    checksum_source_before_copy=SHA_A, checksum_target_after_copy=SHA_B,
                                    result="match")


def test_a_run_is_never_written_without_its_copy_max_retry(catalog):
    with pytest.raises(BackupCatalogError, match="copy_max_retry is required"):
        catalog.start_restore_run(action="copy_only", source_server_id="S", target_server_id="T")
    with pytest.raises(sqlite3.IntegrityError):
        catalog.start_restore_run(action="copy_only", source_server_id="S", target_server_id="T",
                                  copy_max_retry=11)


def test_a_misspelled_column_is_refused_rather_than_dropped(catalog):
    with pytest.raises(BackupCatalogError, match="has no column"):
        catalog.record_backup_file(**_file(checksum="x"))


# --------------------------------------------------------------------------- #
# The integrity object
# --------------------------------------------------------------------------- #
def test_the_stated_block_is_copy_max_retry_3_and_reads_back():
    stated = I.stated_restore_integrity()
    assert stated["copy_max_retry"] == 3
    assert I.parse_restore_integrity({"integrity": stated}) == I.RestoreIntegrity()


def test_an_entry_without_the_block_is_none_and_the_copying_code_refuses_it():
    assert I.parse_restore_integrity({"restore_id": "R"}) is None
    with pytest.raises(I.IntegrityConfigError, match="states no integrity block"):
        I.require_restore_integrity({"restore_id": "R"}, label="R")


@pytest.mark.parametrize("block, message", [
    ({"copy_retry_interval_seconds": 30}, "copy_max_retry is required"),
    ({"copy_max_retry": -1}, "must be 0 to 10"),
    ({"copy_max_retry": 11}, "must be 0 to 10"),
    ({"copy_max_retry": "3"}, "must be a whole number"),
    ({"copy_max_retry": True}, "must be a whole number"),
    ({"copy_max_retry": 3, "copy_max_retries": 5}, "unknown integrity field"),
    ({"copy_max_retry": 3, "verify_source_before_copy": "yes"}, "must be true or false"),
    ({"copy_max_retry": 3, "uncatalogued": "ignore"}, "must be one of"),
    ({"copy_max_retry": 3, "algorithm": "md5"}, "must be one of"),
])
def test_a_block_that_says_something_invalid_is_refused_by_name(block, message):
    with pytest.raises(I.IntegrityConfigError, match=message):
        I.parse_restore_integrity({"integrity": block}, label="R")


def test_the_reference_requires_the_block_and_copy_max_retry_inside_it():
    reference = shared_objects.load()
    entries = {item["object"]: item for item in reference}
    integrity_field = next(f for f in entries["restore_entry"]["fields"] if f["field"] == "integrity")
    assert integrity_field["required"] is True
    retry = next(f for f in entries["integrity"]["fields"] if f["field"] == "copy_max_retry")
    assert retry["required"] is True and retry["constraint"]["max"] == I.MAX_COPY_MAX_RETRY
    assert {f["field"] for f in entries["integrity"]["fields"]} == set(I.INTEGRITY_FIELDS)

    findings = shared_objects.check_record("restore_entry", {"restore_id": "R"}, where="R",
                                           reference=reference)
    assert any(f["field"] == "integrity" and f["kind"] == "missing" for f in findings)
    findings = shared_objects.check_record("integrity", {"copy_retry_interval_seconds": 30}, where="R",
                                           reference=reference)
    assert any(f["field"] == "copy_max_retry" and f["kind"] == "missing" for f in findings)


# --------------------------------------------------------------------------- #
# Where the block comes from
# --------------------------------------------------------------------------- #
def test_upgrade_config_states_the_block_where_it_is_missing_and_keeps_one_already_there(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "restore_config.json").write_text(json.dumps({"backup_restore": {"backups": [], "restores": [
        {"restore_id": "NO_BLOCK"},
        {"restore_id": "OWN_BLOCK", "integrity": {"copy_max_retry": 7}},
    ]}}, indent=4), encoding="utf-8")

    _message, data_out = config_upgrade.upgrade(
        {"data_dir": str(data), "steps": ["stated-integrity"], "dry_run": False})

    restores = json.loads((data / "restore_config.json").read_text(encoding="utf-8"))["backup_restore"]["restores"]
    assert restores[0]["integrity"] == I.stated_restore_integrity()
    assert restores[1]["integrity"] == {"copy_max_retry": 7}, "a stated block is never overridden"
    assert [r["action"] for r in restores] == ["restore", "restore"], "the action is stated on both (1.216)"
    assert data_out["records_changed"] == 2, "records, not fields: NO_BLOCK got two, OWN_BLOCK one"
    assert "stated-integrity" in config_upgrade.STEPS


def test_restore_add_writes_the_block_with_the_operators_values_and_the_rest_stated(tmp_path):
    registration.add_restore(_restore(integrity={"copy_max_retry": 5}), data_dir=tmp_path, key=KEY)
    entry = json.loads((tmp_path / "restore_config.json").read_text(encoding="utf-8"))[
        "backup_restore"]["restores"][0]
    assert entry["integrity"] == {**I.stated_restore_integrity(), "copy_max_retry": 5}


def test_restore_add_without_a_block_still_writes_one(tmp_path):
    registration.add_restore(_restore(), data_dir=tmp_path, key=KEY)
    entry = json.loads((tmp_path / "restore_config.json").read_text(encoding="utf-8"))[
        "backup_restore"]["restores"][0]
    assert entry["integrity"]["copy_max_retry"] == 3
    assert entry["action"] == "restore", "the action is stated too (1.216) - what an entry always did"


def test_restore_add_refuses_a_block_the_parser_refuses(tmp_path):
    with pytest.raises(registration.RegistrationError, match="must be 0 to 10"):
        registration.add_restore(_restore(integrity={"copy_max_retry": 50}), data_dir=tmp_path, key=KEY)


# --------------------------------------------------------------------------- #
# The first checksum is never updated (the operator, 2026-10-29)
# --------------------------------------------------------------------------- #
def _run_file(catalog, **over):
    run = catalog.start_restore_run(action="restore", source_server_id="S", target_server_id="T",
                                    copy_max_retry=3)
    values = dict(restore_run_id=run, source_server_id="S", target_server_id="T", file_name="a.trn",
                  file_type="LOG", source_path="/s/a.trn", target_path="/t/a.trn", copy_max_retry=3,
                  checksum_after_backup=SHA_A, checksum_source_before_copy=SHA_A, verdict="copying")
    values.update(over)
    return catalog.add_run_file(**values)


@pytest.mark.parametrize("column", ["checksum_after_backup", "checksum_source_before_copy",
                                    "checksum_target_before_copy", "checksum_target_after_copy"])
def test_a_copied_files_first_checksum_is_never_rewritten(catalog, column):
    run_file = _run_file(catalog, checksum_target_before_copy=SHA_B)
    if column == "checksum_target_after_copy":
        catalog.set_run_file(run_file, checksum_target_after_copy=SHA_A)   # the first write: allowed
    catalog.set_run_file(run_file, **{column: catalog.run_files(1)[0][column]})  # the same again: harmless

    with pytest.raises(BackupCatalogError, match="is never rewritten"):
        catalog.set_run_file(run_file, **{column: "c" * 64})


def test_an_empty_first_checksum_may_be_written_once(catalog):
    run_file = _run_file(catalog)
    catalog.set_run_file(run_file, checksum_target_after_copy=SHA_A, verdict="copied")
    assert catalog.run_files(1)[0]["checksum_target_after_copy"] == SHA_A
    with pytest.raises(BackupCatalogError, match="is never rewritten"):
        catalog.set_run_file(run_file, checksum_target_after_copy=SHA_B)


def test_a_backup_files_first_checksum_is_never_rewritten_by_any_route(catalog):
    file_id = catalog.record_backup_file(**_file())
    with pytest.raises(BackupCatalogError, match="never rewritten"):
        catalog.set_backup_file(file_id, checksum_after_backup=SHA_B)
    # A later check that finds other bytes is recorded as a check, and the first checksum stays.
    catalog.check_backup_file(file_id, checksum_now=SHA_B)
    row = catalog.backup_history(server_id="ACME-192-0-2-250")[0]
    assert row["checksum_after_backup"] == SHA_A and row["checksum_last_check"] == SHA_B


def test_a_later_check_of_a_copy_never_touches_its_first_checksums(catalog):
    run_file = _run_file(catalog, checksum_target_after_copy=SHA_A, verdict="copied")
    catalog.check_run_file(run_file, side="target", checksum_now=SHA_B)
    row = catalog.run_files(1)[0]
    assert (row["checksum_source_before_copy"], row["checksum_target_after_copy"]) == (SHA_A, SHA_A)
    assert row["target_checksum_matches"] == 0


# --------------------------------------------------------------------------- #
# ... and the database holds it too, against any writer
# --------------------------------------------------------------------------- #
def test_the_database_refuses_a_hand_written_update_of_a_backups_first_checksum(catalog):
    catalog.record_backup_file(**_file())
    with _raw(catalog) as conn:
        for value in ("'" + SHA_B + "'", "NULL"):
            with pytest.raises(sqlite3.DatabaseError, match="a first checksum is never updated"):
                conn.execute(f"UPDATE backup_files SET checksum_after_backup = {value}")
        conn.execute("UPDATE backup_files SET state = 'pruned'")  # every other column still moves
        assert conn.execute("SELECT checksum_after_backup, state FROM backup_files").fetchone() == (SHA_A, "pruned")


@pytest.mark.parametrize("column", ["checksum_after_backup", "checksum_source_before_copy",
                                    "checksum_target_before_copy", "checksum_target_after_copy"])
def test_the_database_refuses_a_hand_written_update_of_a_copys_first_checksum(catalog, column):
    _run_file(catalog, checksum_target_before_copy=SHA_B, checksum_target_after_copy=SHA_A,
              verdict="copied")
    with _raw(catalog) as conn, pytest.raises(sqlite3.DatabaseError, match="a first checksum is never updated"):
        conn.execute(f"UPDATE restore_run_files SET {column} = '{'c' * 64}'")


def test_the_database_lets_an_empty_first_checksum_be_written_once(catalog):
    _run_file(catalog)
    with _raw(catalog) as conn:
        conn.execute(f"UPDATE restore_run_files SET checksum_target_after_copy = '{SHA_A}'")
        with pytest.raises(sqlite3.DatabaseError, match="a first checksum is never updated"):
            conn.execute(f"UPDATE restore_run_files SET checksum_target_after_copy = '{SHA_B}'")


def test_the_postgresql_guard_names_every_first_checksum():
    from dbabrain.db.backup_catalog import FIRST_CHECKSUM_FUNCTION_POSTGRES, FIRST_CHECKSUMS

    for columns in FIRST_CHECKSUMS.values():
        for column in columns:
            assert f"OLD.{column} IS NOT NULL" in FIRST_CHECKSUM_FUNCTION_POSTGRES, column
    assert "RAISE EXCEPTION" in FIRST_CHECKSUM_FUNCTION_POSTGRES
    assert ";" not in FIRST_CHECKSUM_FUNCTION_POSTGRES.split("$guard$")[0], \
        "the function travels as one statement; nothing before its body may end it"


# --------------------------------------------------------------------------- #
# Every file's size, in bytes, recorded once (the operator, 2026-10-29)
# --------------------------------------------------------------------------- #
def test_an_ok_file_is_never_stored_without_its_size_in_bytes(catalog):
    with pytest.raises(sqlite3.IntegrityError, match="ck_backup_files_sized"):
        catalog.record_backup_file(**_file(size_bytes=None))
    # One whose size could not be read is kept, and says so.
    catalog.record_backup_file(**_file(size_bytes=None, verdict="size_failed"))
    assert catalog.backup_history(server_id="ACME-192-0-2-250")[0]["verdict"] == "size_failed"


def test_the_size_a_backup_recorded_is_never_rewritten_by_any_route(catalog):
    file_id = catalog.record_backup_file(**_file())
    with pytest.raises(BackupCatalogError, match="never rewritten"):
        catalog.set_backup_file(file_id, size_bytes=1)
    with _raw(catalog) as conn:
        for value in ("1", "NULL"):
            with pytest.raises(sqlite3.DatabaseError, match="nor a first size"):
                conn.execute(f"UPDATE backup_files SET size_bytes = {value}")
    # A later check's size is a reading of its own beside it.
    catalog.check_backup_file(file_id, checksum_now=SHA_A, size_now=5)
    row = catalog.backup_history(server_id="ACME-192-0-2-250")[0]
    assert row["size_bytes"] == 61203447808


def test_the_size_a_copy_read_at_its_source_is_never_rewritten(catalog):
    run_file = _run_file(catalog, size_source_bytes=4096)
    with pytest.raises(BackupCatalogError, match="never rewritten"):
        catalog.set_run_file(run_file, size_source_bytes=1)
    with _raw(catalog) as conn, pytest.raises(sqlite3.DatabaseError, match="nor a first size"):
        conn.execute("UPDATE restore_run_files SET size_source_bytes = 1")
