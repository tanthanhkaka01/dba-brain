"""Every backup file a restore applied is recorded, in the order it went in (0.39.0).

The operator, 2026-11-13 (translated): *dbabrain must keep the full log - during a restore, which
file was used ... keep the full chain of files, so it can be shown which file was used for a
restore, which restore.*

The catalog already held what a restore **copied** (``restore_run_files``). That is not what it
**applied**: a restore in place copies nothing, a staged folder holds more than one chain, and
nothing said which file was the full, which the differential, in what order the logs went in, or
which one carried the STOPAT. And a file decrypted from the repository was recorded with no tie to
the backup file it is, so the backup's own row never said a restore had used it.

``restore_chain_files`` is that record: one row per file, numbered in the order applied, with the
database it holds, the name it was restored as, the step that applied it and what came of it. It is
written ``planned`` **before the first step runs** - a restore that dies half way leaves the chain
it meant to apply - then each step marks its files ``applied``, or ``failed`` with the step's error.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from conftest import landing_of

from dbabrain.backup_restore import restore_by_id, restore_script
from dbabrain.db.backup_catalog import CHAIN_RESULTS, CHAIN_STEPS, TABLES, BackupCatalogError, BackupCatalogStore
from dbabrain.lib import restore_space
from dbabrain.lib.integrity import RestoreIntegrity

# Its restore targets are lab machines the tests do not list in an inventory; a target whose
# environment nothing states is refused since 0.39.0 (D-39.6) - see tests/conftest.py.
pytestmark = pytest.mark.usefixtures("restore_targets_are_lab")

SHA = "a" * 64


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _backup_file(catalog, name, file_type, **over):
    values = dict(server_id="SRC", database_name="APP", engine="sqlserver", file_name=name, file_type=file_type,
                  file_path=f"C:\\SQLBK\\APP\\{file_type}\\{name}", checksum_after_backup=SHA, verdict="ok",
                  size_bytes=1024, backup_ended_at="2026-11-12T04:35:50Z")
    values.update(over)
    return catalog.record_backup_file(**values)


def _run(catalog, **over):
    values = dict(action="restore", restore_id="APP_TO_LAB", source_server_id="SRC", target_server_id="TGT",
                  copy_max_retry=3)
    values.update(over)
    return catalog.start_restore_run(**values)


def _chain_file(name, step, **over):
    values = dict(source_server_id="SRC", target_server_id="TGT", database_name="APP", restored_as="APP",
                  step=step, file_name=name, file_path=f"/stage/APP/{name}")
    values.update(over)
    return values


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #
def test_the_table_is_part_of_the_catalog_with_its_keys(catalog):
    assert "restore_chain_files" in TABLES
    with sqlite3.connect(catalog.sqlite_path) as conn:
        parents = {row[2] for row in conn.execute("PRAGMA foreign_key_list(restore_chain_files)")}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(restore_chain_files)")}
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(restore_runs)")}
    assert parents == {"restore_runs", "backup_files"}
    assert {"position", "database_name", "restored_as", "step", "file_id", "file_name", "file_path",
            "backup_ended_at", "with_recovery", "stopat", "result", "error_text", "applied_at"} <= columns
    assert "landed_json" in run_columns


def test_a_catalog_made_before_it_gains_the_table_and_the_column(tmp_path):
    """A store of schema 7 - the worker's until 0.39.0 is deployed - is brought up additively."""
    path = tmp_path / "old.sqlite"
    store = BackupCatalogStore(path)
    store.initialize()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE restore_chain_files")
        conn.execute("ALTER TABLE restore_runs DROP COLUMN landed_json")

    BackupCatalogStore(path).initialize(force=True)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM restore_chain_files").fetchone()[0] == 0
        assert "landed_json" in {row[1] for row in conn.execute("PRAGMA table_info(restore_runs)")}


def test_the_chain_is_written_planned_in_the_order_given(catalog):
    run = _run(catalog)

    written = catalog.plan_chain(run, [
        _chain_file("APP_FULL.bak", "restore-full"),
        _chain_file("APP_DIFF.bak", "restore-diff"),
        _chain_file("APP_LOG_1.trn", "restore-log"),
        _chain_file("APP_LOG_2.trn", "restore-log", with_recovery=1, stopat="2026-11-13 08:45:22")])

    chain = catalog.chain_of(run)
    assert written == 4
    assert [(row["position"], row["step"], row["file_name"], row["result"]) for row in chain] == [
        (1, "restore-full", "APP_FULL.bak", "planned"), (2, "restore-diff", "APP_DIFF.bak", "planned"),
        (3, "restore-log", "APP_LOG_1.trn", "planned"), (4, "restore-log", "APP_LOG_2.trn", "planned")]
    assert chain[3]["stopat"] == "2026-11-13 08:45:22" and chain[3]["with_recovery"] == 1
    assert all(row["applied_at"] is None for row in chain)


def test_a_runs_chain_is_written_once(catalog):
    run = _run(catalog)
    catalog.plan_chain(run, [_chain_file("APP_FULL.bak", "restore-full")])

    assert catalog.plan_chain(run, [_chain_file("OTHER.bak", "restore-full")]) == 0
    assert [row["file_name"] for row in catalog.chain_of(run)] == ["APP_FULL.bak"]


def test_each_step_marks_its_own_files_and_what_was_never_reached_stays_planned(catalog):
    run = _run(catalog)
    catalog.plan_chain(run, [_chain_file("APP_FULL.bak", "restore-full"),
                             _chain_file("APP_LOG_1.trn", "restore-log"),
                             _chain_file("APP_LOG_2.trn", "restore-log")])

    catalog.set_chain_step(run, [1], result="applied")
    catalog.set_chain_step(run, [2], result="failed", error_text="Msg 4305: the log is too recent to apply")

    chain = catalog.chain_of(run)
    assert [row["result"] for row in chain] == ["applied", "failed", "planned"]
    assert chain[0]["applied_at"] and chain[1]["applied_at"] is None
    assert "Msg 4305" in chain[1]["error_text"]


def test_only_the_three_restore_steps_and_the_four_results_are_taken(catalog):
    run = _run(catalog)
    assert CHAIN_STEPS == ("restore-full", "restore-diff", "restore-log")
    assert CHAIN_RESULTS == ("planned", "applied", "skipped", "failed")

    with pytest.raises(BackupCatalogError, match="step"):
        catalog.plan_chain(run, [_chain_file("x.bak", "verify-restore")])
    with pytest.raises(BackupCatalogError, match="result must be one of"):
        catalog.set_chain_step(run, [1], result="ignored")
    with pytest.raises(BackupCatalogError, match="file_name"):
        catalog.plan_chain(run, [{"source_server_id": "SRC", "target_server_id": "TGT", "step": "restore-full",
                                  "file_path": "/stage/x.bak"}])


# --------------------------------------------------------------------------- #
# Which restore used a file - and which files a restore used
# --------------------------------------------------------------------------- #
def test_a_backup_file_is_known_by_its_server_and_name_whatever_its_state(catalog):
    """A file released at its source once its repository copy was proven reads ``pruned`` there - and
    it is exactly the file a restore from the repository applies."""
    held = _backup_file(catalog, "APP_FULL_20261112_043500Z.bak", "FULL")
    released = _backup_file(catalog, "APP_LOG_20261112_050000Z.trn", "LOG", state="pruned")

    facts = catalog.backup_file_facts(server_id="SRC", file_names=[
        "APP_FULL_20261112_043500Z.bak", "APP_LOG_20261112_050000Z.trn", "NOT_IN_THE_CATALOG.bak"])

    assert facts["APP_FULL_20261112_043500Z.bak"]["file_id"] == held
    assert facts["APP_LOG_20261112_050000Z.trn"]["file_id"] == released, "pruned at the source is still that file"
    assert facts["APP_LOG_20261112_050000Z.trn"]["file_type"] == "LOG"
    assert "NOT_IN_THE_CATALOG.bak" not in facts, "no row is invented for a file the catalog does not know"
    assert catalog.backup_file_facts(server_id="OTHER", file_names=["APP_FULL_20261112_043500Z.bak"]) == {}


def test_every_restore_that_applied_a_file_can_be_listed(catalog):
    full = _backup_file(catalog, "APP_FULL.bak", "FULL")
    # Both starts stated: with the first left to the clock, the order depended on the hour the suite ran.
    first = _run(catalog, point_in_time="LATEST", started_at="2026-11-13T08:00:00Z")
    second = _run(catalog, restore_id="APP_TO_LAB_COPY", point_in_time="2026-11-13 08:45:22",
                  started_at="2026-11-13T09:00:00Z")
    catalog.plan_chain(first, [_chain_file("APP_FULL.bak", "restore-full", file_id=full)])
    catalog.plan_chain(second, [_chain_file("APP_FULL.bak", "restore-full", file_id=full, restored_as="APP_COPY")])
    catalog.set_chain_step(second, [1], result="applied")

    used = catalog.restores_using(full)

    assert [row["restore_run_id"] for row in used] == [second, first], "newest first"
    assert used[0]["restore_id"] == "APP_TO_LAB_COPY" and used[0]["restored_as"] == "APP_COPY"
    assert used[0]["result"] == "applied" and used[0]["point_in_time"] == "2026-11-13 08:45:22"
    assert used[1]["result"] == "planned"


def test_a_servers_restores_list_their_chains_in_order(catalog):
    run = _run(catalog)
    catalog.plan_chain(run, [_chain_file("APP_FULL.bak", "restore-full"), _chain_file("APP_LOG_1.trn", "restore-log")])

    rows = catalog.server_restore_chains("SRC")

    assert [(row["restore_run_id"], row["position"], row["file_name"]) for row in rows] == [
        (run, 1, "APP_FULL.bak"), (run, 2, "APP_LOG_1.trn")]
    assert rows[0]["restore_id"] == "APP_TO_LAB" and rows[0]["run_phase"]
    assert catalog.server_restore_chains("ANOTHER") == []


# --------------------------------------------------------------------------- #
# The chain is read off the plan
# --------------------------------------------------------------------------- #
FULL = {"path": "/s/APP/FULL/APP_FULL_20261112_043500Z.bak", "finished_at": "2026-11-12 04:35:50"}
DIFF = {"path": "/s/APP/DIFF/APP_DIFF_20261112_060000Z.bak", "finished_at": "2026-11-12 06:00:20"}
LOGS = [{"path": f"/s/APP/LOG/APP_LOG_20261112_0{hour}0000Z.trn", "finished_at": f"2026-11-12 0{hour}:00:05"}
        for hour in (7, 8)]
JOB = SimpleNamespace(server_id="SRC", target_server_id="TGT")


def _steps(stopat="2026-11-12 07:30:00"):
    return [
        {"op": "restore-key", "request": {}, "done": True},
        {"op": "restore-full", "source_database": "APP", "chain": [FULL],
         "request": {"database_name": "APP_COPY", "backup_path": FULL["path"], "with_recovery": False}},
        {"op": "restore-diff", "source_database": "APP", "chain": [DIFF],
         "request": {"database_name": "APP_COPY", "backup_path": DIFF["path"], "with_recovery": False}},
        {"op": "restore-log", "source_database": "APP", "chain": LOGS,
         "request": {"database_name": "APP_COPY", "backup_paths": [f["path"] for f in LOGS],
                     "with_recovery": True, **({"stopat": stopat} if stopat else {})}},
        {"op": "expect", "expect": {}},
        {"op": "warning", "warning": "x"},
        {"op": "verify-restore", "request": {}},
    ]


def test_the_chain_is_the_plans_own_files_in_the_order_they_go_in():
    rows, positions = restore_by_id._chain_rows(JOB, _steps())

    assert [(row["step"], row["file_name"]) for row in rows] == [
        ("restore-full", "APP_FULL_20261112_043500Z.bak"), ("restore-diff", "APP_DIFF_20261112_060000Z.bak"),
        ("restore-log", "APP_LOG_20261112_070000Z.trn"), ("restore-log", "APP_LOG_20261112_080000Z.trn")]
    assert positions == {1: [1], 2: [2], 3: [3, 4]}, "each step knows the files it carries"
    assert {row["database_name"] for row in rows} == {"APP"} and {row["restored_as"] for row in rows} == {"APP_COPY"}
    assert rows[0]["backup_ended_at"] == "2026-11-12 04:35:50"
    assert (rows[0]["source_server_id"], rows[0]["target_server_id"]) == ("SRC", "TGT")


def test_the_stopat_and_the_recovery_sit_on_the_last_file_of_their_step():
    rows, _ = restore_by_id._chain_rows(JOB, _steps())

    assert [row["stopat"] for row in rows] == [None, None, None, "2026-11-12 07:30:00"]
    assert [row["with_recovery"] for row in rows] == [0, 0, 0, 1]


def test_a_whole_instance_restore_records_its_base_backups_and_then_the_wal_folder():
    steps = [{"op": "restore-diff", "request": {"backup_paths": ["/in/base/a_FULL", "/in/base/b_INCR"]},
              "chain": [{"path": "/in/base/a_FULL", "finished_at": "2026-11-12 01:00:00"},
                        {"path": "/in/base/b_INCR", "finished_at": "2026-11-12 13:00:00"},
                        {"path": "/in/wal/", "step": "restore-log", "finished_at": "2026-11-12 14:10:00"}]}]

    rows, positions = restore_by_id._chain_rows(JOB, steps)

    assert [(row["step"], row["file_name"], row["database_name"]) for row in rows] == [
        ("restore-diff", "a_FULL", ""), ("restore-diff", "b_INCR", ""), ("restore-log", "wal", "")]
    assert positions == {0: [1, 2, 3]}


def test_each_file_is_tied_to_the_backup_file_it_is(catalog):
    full = _backup_file(catalog, "APP_FULL_20261112_043500Z.bak", "FULL")
    log = _backup_file(catalog, "APP_LOG_20261112_070000Z.trn", "LOG", state="pruned")
    run = _run(catalog)

    positions, warning = restore_by_id._record_chain(catalog, run, JOB, _steps())

    chain = catalog.chain_of(run)
    assert warning == "" and positions == {1: [1], 2: [2], 3: [3, 4]}
    assert [row["file_id"] for row in chain] == [full, None, log, None], "a file the catalog does not know has none"
    assert chain[0]["file_type"] == "FULL" and chain[2]["file_type"] == "LOG"


@pytest.mark.parametrize("catalog_there, run_there, said", [
    (False, True, "no catalog was open"), (True, False, "the run has no catalog row")])
def test_a_chain_that_cannot_be_recorded_is_said_not_silent(catalog, catalog_there, run_there, said):
    positions, warning = restore_by_id._record_chain(catalog if catalog_there else None,
                                                     _run(catalog) if run_there else None, JOB, _steps())

    assert positions == {} and said in warning


def test_a_store_that_fails_is_a_warning_on_the_restore_not_its_failure():
    broken = SimpleNamespace(backup_file_facts=lambda **k: (_ for _ in ()).throw(RuntimeError("store is down")))

    positions, warning = restore_by_id._record_chain(broken, 7, JOB, _steps())

    assert positions == {} and "could not be recorded - RuntimeError: store is down" in warning


# --------------------------------------------------------------------------- #
# The restore itself writes it
# --------------------------------------------------------------------------- #
@pytest.fixture
def restore(monkeypatch, catalog):
    def run(fail_at: str = ""):
        job = SimpleNamespace(restore_id="R", db_type="sqlserver", server_id="SRC", target_server_id="TGT",
                              env_secrets={}, is_remote=False, label="R (sqlserver)", landing=landing_of("APP"),
                              overwrite_existing=False, integrity=None, tier=None, target_backup_dir="/s",
                              server_metadata=SimpleNamespace(enabled=False))
        monkeypatch.setattr(restore_script, "load_script_restores", lambda _p=None: [job])
        monkeypatch.setattr(restore_by_id, "_host_block", lambda j, **_: {"host": "h"})
        monkeypatch.setattr(restore_by_id, "assert_target_is_not_source", lambda *a, **k: None)
        monkeypatch.setitem(restore_by_id._PLANNERS, "sqlserver", lambda *a, **k: _steps(stopat=""))
        seen = []

        def execute(op, request):
            seen.append((op, [row["result"] for row in catalog.chain_of(1)]))
            if op == fail_at:
                raise restore_by_id.RestoreByIdError("Msg 3201: cannot open backup device")
            return {"ok": True, "checked": 1, "failed": 0, "databases": []}

        monkeypatch.setattr(restore_by_id, "_execute", execute)
        return seen

    return run


def test_the_whole_chain_is_on_record_before_the_first_file_goes_in(restore, catalog):
    seen = restore()
    restore_by_id.restore_by_id({"restore_id": "R"}, catalog=catalog)

    assert seen[0] == ("restore-full", ["planned", "planned", "planned", "planned"])
    assert seen[1] == ("restore-diff", ["applied", "planned", "planned", "planned"])
    assert [row["result"] for row in catalog.chain_of(1)] == ["applied"] * 4


def test_a_step_that_fails_marks_its_files_and_leaves_the_rest_planned(restore, catalog):
    restore(fail_at="restore-diff")

    with pytest.raises(restore_by_id.RestoreByIdError, match="Msg 3201"):
        restore_by_id.restore_by_id({"restore_id": "R"}, catalog=catalog)

    chain = catalog.chain_of(1)
    assert [row["result"] for row in chain] == ["applied", "failed", "planned", "planned"]
    assert "Msg 3201" in chain[1]["error_text"]


def test_the_answer_names_the_run_the_chain_is_under(restore, catalog):
    restore()
    outcome = restore_by_id.restore_by_id({"restore_id": "R"}, catalog=catalog)

    # The plan's own warning, and none about the record: the chain was written.
    assert outcome["restore_run_id"] == 1 and outcome["warnings"] == ["x"]


# --------------------------------------------------------------------------- #
# A file decrypted from the repository is tied to its backup file
# --------------------------------------------------------------------------- #
def test_a_decrypted_staged_file_is_tied_to_the_backup_file_it_is(catalog):
    """The plain name is the backup's own, so the SOURCE server and that name find its row - and with
    the tie, the backup's row says a restore used it (``last_restore_run_id``)."""
    full = _backup_file(catalog, "APP_FULL_20261112_043500Z.bak", "FULL", state="pruned")
    job = SimpleNamespace(restore_id="APP_TO_LAB", label="APP_TO_LAB (sqlserver)", server_id="SRC",
                          target_server_id="TGT", db_type="sqlserver", action="restore", tier=None,
                          integrity=RestoreIntegrity(copy_retry_interval_seconds=0),
                          space_check=restore_space.SpaceCheck())

    run = restore_script._record_decrypted(
        job, catalog, source_server_id="REPO", source_dir="/mnt/nas/SRC", target_dir="/stage",
        files=[{"path": "/stage/APP/FULL/APP_FULL_20261112_043500Z.bak", "size_bytes": 1024, "sha256": SHA},
               {"path": "/stage/APP/LOG/UNKNOWN_TO_THE_CATALOG.trn", "size_bytes": 10, "sha256": SHA}])

    files = catalog.run_files(run)
    assert [(row["file_id"], row["database_name"]) for row in files] == [(full, "APP"), (None, "")]
    assert files[0]["backup_taken_at"] == "2026-11-12T04:35:50Z" and files[0]["file_type"] == "FULL"
    with catalog.connect() as conn:
        marked = conn.execute("SELECT last_restore_run_id, last_restore_target_server_id FROM backup_files "
                              "WHERE file_id = ?", (full,)).fetchone()
    assert (marked["last_restore_run_id"], marked["last_restore_target_server_id"]) == (run, "TGT")
