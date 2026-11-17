"""An Oracle restore from the repository copies the RMAN chain its source named at backup time (0.40.0, 1.380).

An RMAN folder is flat and its names are RMAN's, so the chain cannot be read from the names on the
repository as PostgreSQL's and SQL Server's are; from the server the copy asks the source's RMAN, and a
restore from the repository is the one that may run when the source no longer answers. The operator,
2026-11-16: **the chain is recorded after each backup** of an entry with ``record_chain`` (a field each
Oracle entry states), **a point in time is built too**, and **a backup whose chain could not be asked
stands** (step 3 says why, as a warning) while **a restore that finds no recorded chain refuses** before
it copies anything - never the whole folder instead (review 0.25.0, G2.9).

What would go wrong unseen: a chain from the wrong level 0 restores to an older point than asked (RMAN
duplicates whatever the pieces allow, and says it succeeded); a piece the repository no longer holds
fails the duplicate halfway; a backup failed by a question about its chain loses a good backup.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import backup as B
from dbabrain.backup_restore import backup_messages as BM
from dbabrain.backup_restore import restore_script as R
from dbabrain.backup_restore import workflow
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import restore_space
from dbabrain.lib.restore import oracle_chain as C
from dbabrain.lib.restore import stated

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(run_id, level, ended, *, pieces=None, error="", files=()):
    return C.ChainRun(backup_run_id=run_id, level=level, ended_at=ended, pieces=pieces, error=error,
                      files=tuple(files))


# A level 0 at 01:00, archived logs at 02:00 and 03:00, a new level 0 at 05:00, logs at 06:00.
L0_A = _run(1, "full", "2026-11-16T01:00:00Z", pieces=("l0a", "ctl_a"), files=("l0a", "ctl_a"))
LOG_2 = _run(2, "log", "2026-11-16T02:00:00Z", pieces=("l0a", "ctl_a", "arc2"), files=("arc2",))
LOG_3 = _run(3, "log", "2026-11-16T03:00:00Z", error="RuntimeError: ssh timed out", files=("arc3",))
L0_B = _run(4, "full", "2026-11-16T05:00:00Z", pieces=("l0b", "ctl_b"), files=("l0b", "ctl_b"))
LOG_6 = _run(5, "log", "2026-11-16T06:00:00Z", pieces=("l0b", "ctl_b", "arc6"), files=("arc6",))
ALL = [L0_A, LOG_2, LOG_3, L0_B, LOG_6]


# --------------------------------------------------------------------------- #
# Which pieces: the newest
# --------------------------------------------------------------------------- #
def test_the_newest_is_the_chain_its_newest_run_recorded():
    chosen = C.choose(ALL)
    assert set(chosen.pieces) == {"l0b", "ctl_b", "arc6"}
    assert (chosen.level0_run_id, chosen.recorded_run_id, chosen.reaches_run_id) == (4, 5, 5)


def test_a_run_whose_chain_was_not_asked_still_reaches_the_restore_through_an_earlier_record():
    chosen = C.choose([L0_A, LOG_2, LOG_3])
    assert chosen.pieces == ("l0a", "ctl_a", "arc2", "arc3"), "run 3's log follows run 2's recorded chain"
    assert chosen.recorded_run_id == 2 and chosen.reaches_run_id == 3
    assert "1 file(s) of 1 later run(s)" in chosen.said


def test_a_level_0_whose_chain_nothing_recorded_refuses_the_restore():
    unrecorded = _run(4, "full", "2026-11-16T05:00:00Z", error="no chain recorded", files=("l0b",))
    with pytest.raises(C.ChainNotRecorded, match="never copies the whole folder"):
        C.choose([L0_A, LOG_2, unrecorded], server_id="ORA-SRC")


def test_a_source_with_no_finished_run_refuses():
    with pytest.raises(C.ChainNotRecorded, match="no finished Oracle backup run"):
        C.choose([], server_id="ORA-SRC")


# --------------------------------------------------------------------------- #
# Which pieces: a point in time
# --------------------------------------------------------------------------- #
def test_a_moment_takes_the_level_0_before_it_and_the_runs_up_to_the_first_that_passes_it():
    chosen = C.choose(ALL, point_in_time=datetime(2026, 11, 16, 2, 30))
    assert chosen.pieces == ("l0a", "ctl_a", "arc2", "arc3")
    assert chosen.level0_run_id == 1 and chosen.reaches_run_id == 3


def test_a_moment_just_before_a_new_level_0_keeps_the_older_chain():
    # The first run past 04:00 is the new level 0: its recorded chain starts at itself, so the chain
    # of the level 0 before the moment is the one recorded by run 2, with the files up to run 4.
    chosen = C.choose(ALL, point_in_time=datetime(2026, 11, 16, 4, 0))
    assert chosen.level0_run_id == 1 and chosen.recorded_run_id == 2 and chosen.reaches_run_id == 4
    assert {"l0a", "arc2", "arc3"} <= set(chosen.pieces)


def test_a_moment_no_backup_has_passed_yet_is_refused():
    with pytest.raises(C.ChainNotRecorded, match="not backed up yet"):
        C.choose(ALL, point_in_time=datetime(2026, 11, 16, 7, 0))


def test_a_moment_before_the_first_level_0_is_refused():
    with pytest.raises(C.ChainNotRecorded, match="no level 0"):
        C.choose(ALL, point_in_time=datetime(2026, 11, 16, 0, 30))


def test_the_catalogs_rows_read_as_runs():
    runs = C.runs_from_rows(
        [{"backup_run_id": 7, "level": "full", "ended_at": "t", "rman_chain_json": json.dumps({"pieces": ["a"]})},
         {"backup_run_id": 8, "level": "log", "ended_at": "t", "rman_chain_json": json.dumps({"error": "boom"})},
         {"backup_run_id": 9, "level": "log", "ended_at": "t", "rman_chain_json": None}],
        [{"backup_run_id": 7, "file_name": "a"}, {"backup_run_id": 8, "file_name": "b"}])
    assert [(r.pieces, r.error, r.files) for r in runs] == [
        (("a",), "", ("a",)), (None, "boom", ("b",)), (None, "no chain recorded", ())]


# --------------------------------------------------------------------------- #
# The catalog keeps the answer on the run
# --------------------------------------------------------------------------- #
@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _backup(catalog, level, ended, names, *, file_type):
    return catalog.record_backup(
        run={"backup_id": "ORA_DB", "server_id": "ORA-SRC", "engine": "oracle", "level": level,
             "started_at": ended, "ended_at": ended, "status": "DONE"},
        files=[{"server_id": "ORA-SRC", "engine": "oracle", "file_name": name, "file_type": file_type,
                "file_path": f"/backup/{name}", "verdict": "ok", "database_name": "FREE",
                "size_bytes": 9, "checksum_after_backup": "0" * 64} for name in names])


def test_a_recorded_chain_and_a_failed_question_are_both_kept_and_read_back(catalog):
    first = _backup(catalog, "full", "2026-11-16T01:00:00Z", ["l0a", "ctl_a"], file_type="RMAN_L0")
    second = _backup(catalog, "log", "2026-11-16T02:00:00Z", ["arc2"], file_type="ARCHIVELOG")
    catalog.record_rman_chain(first, pieces=["ctl_a", "l0a", "l0a"])
    catalog.record_rman_chain(second, error="RuntimeError:   ssh\n timed out")
    runs = C.runs_from_rows(*catalog.oracle_chain_rows(server_id="ORA-SRC"))
    assert [(r.backup_run_id, r.pieces, r.error, r.files) for r in runs] == [
        (first, ("ctl_a", "l0a"), "", ("l0a", "ctl_a")),
        (second, None, "RuntimeError: ssh timed out", ("arc2",))]
    assert C.choose(runs).pieces == ("ctl_a", "l0a", "arc2")


# --------------------------------------------------------------------------- #
# Asked at backup time, said in step 3
# --------------------------------------------------------------------------- #
def _item(**overrides):
    values = dict(db_type="oracle", record_chain=True, server_id="ORA-SRC", backup_dir="/opt/oracle/backup",
                  label="ORA_DB.database", backup_id="ORA_DB")
    values.update(overrides)
    return SimpleNamespace(**values)


class _Catalog:
    def __init__(self, rows=()):
        self.rows, self.recorded = list(rows), []

    def backup_run_rows(self, run_id):
        return self.rows

    def record_rman_chain(self, run_id, **values):
        self.recorded.append((run_id, values))


@pytest.fixture
def asked(monkeypatch):
    calls = []
    monkeypatch.setattr(B, "resolve_ssh_target", lambda *a, **k: SimpleNamespace(container_name="ORA_C"))
    monkeypatch.setattr(R, "_ssh_login", lambda target, **_k: {"host": "src"})

    def run(command, request, **_k):
        calls.append((command, request))
        return {"include": ["l0a", "ctl_a"], "files": []}

    from dbabrain.backup_restore import source_clock
    from dbabrain.transport import common_cli
    monkeypatch.setattr(common_cli, "run", run)
    # The source's clock, as its database would answer it (1.385) - never the node's own inventory.
    monkeypatch.setattr(source_clock, "ask", lambda server_id, **_k: (420, ""))
    return calls


def _ctx(catalog):
    return SimpleNamespace(catalog=catalog, data_dir=None, key=None, key_base64=None)


def test_after_an_oracle_backup_its_sources_rman_is_asked_and_the_answer_kept(asked):
    catalog = _Catalog([{"host_path": "/srv/ora/backup/l0a"}, {"host_path": "/srv/ora/backup/ctl_a"}])
    assert workflow._record_rman_chain(_item(), run_id=7, ctx=_ctx(catalog)) == {"pieces": 2, "clock_offset_minutes": 420}
    command, request = asked[0]
    assert command == "prepare-staging" and request["plan_only"] is True and request["make_readable"] is False
    assert request["source_dir"] == "/srv/ora/backup", "the folder as the server itself names it"
    assert request["chain"] == {"db_type": "oracle", "backup_dir": "/opt/oracle/backup", "container": "ORA_C"}
    assert catalog.recorded == [(7, {"pieces": ["l0a", "ctl_a"], "clock_offset_minutes": 420})]


@pytest.mark.parametrize("item", [_item(record_chain=False), _item(db_type="postgresql")])
def test_an_entry_that_does_not_record_its_chain_asks_nothing(asked, item):
    assert workflow._record_rman_chain(item, run_id=7, ctx=_ctx(_Catalog())) is None
    assert asked == []


def test_a_question_that_fails_leaves_the_backup_standing_and_keeps_why(monkeypatch, asked):
    from dbabrain.transport import common_cli

    def fails(command, request, **_k):
        raise RuntimeError("RMAN-06004: catalog not available")

    monkeypatch.setattr(common_cli, "run", fails)
    catalog = _Catalog()
    answer = workflow._record_rman_chain(_item(), run_id=7, ctx=_ctx(catalog))
    assert answer == {"error": "RuntimeError: RMAN-06004: catalog not available"}
    assert catalog.recorded == [(7, {"error": "RuntimeError: RMAN-06004: catalog not available"})]


def _step3(chain):
    return BM.backup_done(_item(), result=SimpleNamespace(status="done", error_text=""),
                          started_at="2026-11-16T01:00:00Z", finished_at="2026-11-16T01:01:00Z",
                          catalog_summary={"files": 2, "verified": 2}, scrub=None, anomalies=[], markers=[],
                          server_metadata=None, chain=chain)


def test_step_3_says_the_chain_was_kept_or_why_it_was_not():
    assert "✅ RMAN chain recorded: 2 piece(s)" in "\n".join(_step3({"pieces": 2})["flow_lines"])
    lines = _step3({"error": "RuntimeError: boom"})["flow_lines"]
    assert any(line.startswith("⚠ RMAN chain not recorded - RuntimeError: boom") for line in lines)
    assert "done" in _step3({"error": "x"})["flow_title"], "the backup itself stands"
    assert not any("RMAN chain" in line for line in _step3(None)["flow_lines"])


# --------------------------------------------------------------------------- #
# The field: stated on every Oracle entry, Oracle's only
# --------------------------------------------------------------------------- #
def _entry(**fields):
    return {"backup_id": "ORA_DB", "db_type": "oracle", "server_id": "ORA-SRC", "backup_dir": "/b",
            "offload_enabled": True, **fields}


def test_an_oracle_entry_that_leaves_it_out_is_unstated_and_upgrade_config_writes_its_offload():
    assert "record_chain" in stated.unstated(_entry(), kind=stated.BACKUP_ENTRY)
    field = next(f for f in stated.BACKUP_ENTRY_FIELDS if f.name == "record_chain")
    assert field.value_for(_entry()) is True
    assert field.value_for(_entry(offload_enabled=False)) is False
    assert "record_chain" not in stated.unstated(_entry(db_type="postgresql"), kind=stated.BACKUP_ENTRY)


def _load(tmp_path, **fields):
    document = json.loads((REPO_ROOT / "data" / "restore_config.example.json").read_text(encoding="utf-8"))
    entry = next(e for e in document["backup_restore"]["backups"] if e["backup_id"] == "ACME_SALESDB_BASEBACKUP")
    entry.update(fields)
    document["backup_restore"]["backups"] = [entry]
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return B.load_backup_jobs(path)


def test_an_oracle_entry_loads_what_it_states(tmp_path):
    assert {job.record_chain for job in _load(tmp_path, db_type="oracle", record_chain=True)} == {True}
    assert {job.record_chain for job in _load(tmp_path, db_type="oracle", record_chain=False)} == {False}


@pytest.mark.parametrize("fields, words", [
    ({"record_chain": True}, "record_chain is Oracle's"),
    ({"db_type": "oracle", "record_chain": "yes"}, "must be true or false"),
])
def test_a_record_chain_that_cannot_apply_is_refused_when_the_entry_loads(tmp_path, fields, words):
    with pytest.raises(ValueError, match=words):
        _load(tmp_path, **fields)


# --------------------------------------------------------------------------- #
# The copy reads the recorded chain on the repository
# --------------------------------------------------------------------------- #
@pytest.fixture
def repository(monkeypatch):
    calls, listed = [], ["l0a.age", "ctl_a.age", "arc2.age"]

    def run(command, request, **_k):
        calls.append((command, request))
        return {"files": [{"path": p, "size_bytes": 9} for p in listed], "include": request.get("include")}

    monkeypatch.setattr(R.common_cli, "run", run)
    monkeypatch.setattr(R, "_oracle_recorded_pieces",
                        lambda job, **_k: (["l0a", "ctl_a", "arc2"], "the RMAN chain recorded after run 2"))
    return SimpleNamespace(calls=calls, listed=listed)


def test_a_recorded_piece_is_answered_by_its_sealed_copy_and_only_by_it(repository):
    plan = {"files": [{"path": p} for p in repository.listed]}
    assert R._pieces_missing(["l0a", "ctl_a", "arc2"], plan) == []
    assert R._pieces_missing(["l0a", "arc9"], plan) == ["arc9"], "a .age answers its plain name, nothing else"


def test_without_a_catalog_the_oracle_restore_from_the_repository_refuses():
    job = SimpleNamespace(label="ORA_TO_LAB", server_id="ORA-SRC")
    with pytest.raises(R.CopyNotProvenError, match="no catalog is open"):
        R._oracle_recorded_pieces(job, catalog=None, point_in_time="")


def test_a_chain_never_recorded_refuses_the_restore_by_its_reason(catalog):
    _backup(catalog, "full", "2026-11-16T01:00:00Z", ["l0a"], file_type="RMAN_L0")
    job = SimpleNamespace(label="ORA_TO_LAB", server_id="ORA-SRC")
    with pytest.raises(R.CopyNotProvenError, match="ORA_TO_LAB: no RMAN chain from ORA-SRC's level 0"):
        R._oracle_recorded_pieces(job, catalog=catalog, point_in_time="")


def _job(**overrides):
    values = dict(label="ORA_TO_LAB", server_id="ORA-SRC", db_type="oracle", restore_from="repository",
                  source_backup_host_dir="/srv/ora/backup", backup_dir="/opt/oracle/backup", copy_selection="chain",
                  copy_recent_hours=0, target_server_id="LAB", target_backup_dir="/stage",
                  space_check=restore_space.parse_space_check({"space_check": {"enabled": True, "factor": 2.0}}),
                  staging_marker="", databases=[], database_mappings=[], restore_all_databases=True,
                  cleanup_retention=0)
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def from_repository(monkeypatch, repository):
    monkeypatch.setattr(R, "_ssh_login", lambda target, **_k: {"host": str(target.server_id)})
    monkeypatch.setattr(R, "_repository_source", lambda job, **_k: (
        SimpleNamespace(server_id="REPO", container_name=""), "/mnt/nas/ORA-SRC/database", {}))
    # The sealed copies' proofs: none here - never the node's own store.
    from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore

    monkeypatch.setattr("dbabrain.lib.config.load_config", lambda *a, **k: None)
    monkeypatch.setattr(RepositoryEncryptionStore, "from_config",
                        classmethod(lambda cls, _c: SimpleNamespace(proven_under=lambda **_k: {})))
    return repository


def test_the_restores_plan_asks_the_repository_for_the_recorded_pieces_not_for_a_chain(from_repository):
    plan = R.plan_transfer(_job(), source=SimpleNamespace(server_id="ORA-SRC", container_name="ORA_C"),
                           target=SimpleNamespace(server_id="LAB"), point_in_time="", catalog=object())
    asked = from_repository.calls[0][1]
    assert asked["include"] == ["l0a", "ctl_a", "arc2"] and "chain" not in asked
    assert asked["source_dir"] == "/mnt/nas/ORA-SRC/database"
    assert plan["chain_said"] == "the RMAN chain recorded after run 2"
    assert not any("recorded RMAN chain not on the repository" in why for why in plan["would_refuse"])


def test_a_recorded_piece_the_repository_lost_is_named_and_refused_before_anything_is_copied(from_repository):
    from_repository.listed.remove("arc2.age")
    plan = R.plan_transfer(_job(), source=SimpleNamespace(server_id="ORA-SRC", container_name="ORA_C"),
                           target=SimpleNamespace(server_id="LAB"), point_in_time="", catalog=object())
    assert any("1 piece(s) of the recorded RMAN chain not on the repository - arc2" in why
               for why in plan["would_refuse"])
    with pytest.raises(R.CopyNotProvenError, match="arc2. Nothing was copied"):
        R.transfer_backup_to_target(_job(), source=SimpleNamespace(server_id="ORA-SRC", container_name="ORA_C"),
                                    target=SimpleNamespace(server_id="LAB"), catalog=object())
    assert all(request.get("plan_only") or request.get("dry_run") for _command, request in from_repository.calls),         "only plans and the target's read-only measure were asked: nothing copied"


# --------------------------------------------------------------------------- #
# The SID an Oracle entry backs up (1.387)
# --------------------------------------------------------------------------- #
def test_an_oracle_entry_states_its_sid_and_nothing_is_guessed_for_it():
    # The operator, 2026-11-16: *Oracle must have its SID, missing is wrong* - stated once on the entry.
    assert "oracle_sid" in stated.unstated(_entry(), kind=stated.BACKUP_ENTRY)
    assert "oracle_sid" not in stated.unstated(_entry(oracle_sid="FREE"), kind=stated.BACKUP_ENTRY)
    assert "oracle_sid" not in stated.unstated(_entry(db_type="postgresql"), kind=stated.BACKUP_ENTRY)
    field = next(f for f in stated.BACKUP_ENTRY_FIELDS if f.name == "oracle_sid")
    assert field.value_for(_entry()) is stated._UNKNOWN, "upgrade-config writes no SID for it"


def test_the_entrys_sid_reaches_every_job_and_is_stored_only_on_the_entry(tmp_path):
    jobs = _load(tmp_path, db_type="oracle", record_chain=False, oracle_sid="FREE")
    assert jobs and all(job.env["ORACLE_SID"] == "FREE" for job in jobs)


@pytest.mark.parametrize("fields, words", [
    ({"db_type": "oracle", "oracle_sid": "FREE; rm -rf /"}, "must be an Oracle SID"),
    ({"db_type": "oracle", "oracle_sid": "1FREE"}, "must be an Oracle SID"),
    ({"oracle_sid": "FREE"}, "oracle_sid is Oracle's"),
])
def test_a_sid_that_cannot_be_one_is_refused_when_the_entry_loads(tmp_path, fields, words):
    with pytest.raises(ValueError, match=words):
        _load(tmp_path, record_chain=False, **fields) if "db_type" in fields else _load(tmp_path, **fields)


def test_a_job_naming_another_sid_than_its_entry_is_refused(tmp_path):
    document = json.loads((REPO_ROOT / "data" / "restore_config.example.json").read_text(encoding="utf-8"))
    entry = next(e for e in document["backup_restore"]["backups"] if e["backup_id"] == "ACME_SALESDB_BASEBACKUP")
    entry.update(db_type="oracle", record_chain=False, oracle_sid="FREE")
    entry["jobs"][0]["env"] = {**(entry["jobs"][0].get("env") or {}), "ORACLE_SID": "ORCL"}
    document["backup_restore"]["backups"] = [entry]
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="one entry backs up one instance"):
        B.load_backup_jobs(path)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("script", ["oracle/oracle_rman_database.sh", "oracle/oracle_rman_archivelog.sh"])
def test_each_rman_script_needs_its_sid_and_holds_it_to_the_instance_that_answers(script):
    text = (REPO_ROOT / "dbabrain" / "common" / "backup_scripts" / script).read_text(encoding="utf-8")
    assert '[ -n "$oracle_sid" ] || die "ORACLE_SID is not set' in text
    assert "select instance_name from v$instance" in text and "is not the instance that answers here" in text
    assert text.index("is not the instance that answers here") < text.index('rman_out="$(printf')


# --------------------------------------------------------------------------- #
# A server folder copied to two repository folders (1.402)
# --------------------------------------------------------------------------- #
# Before 0.40.0 an Oracle backup named its database `database`; since its SID is stated (1.387) it names
# it by the SID, so an entry offloaded across the upgrade has copies under both. Every restore of it from
# the repository was refused ("the copies ... are in 2 places"), though each chain lies in one of them:
# a lab drill, 2026-11-17.
def _copy(name, folder):
    return {"host_path": f"/srv/ora/backup/{name}", "offload_path": f"/mnt/nas/ORA-SRC/{folder}/{name}",
            "offload_server_id": "REPO"}


ACROSS_THE_UPGRADE = [_copy("l0a", "database"), _copy("arc2", "database"),
                      _copy("l0b", "FREE"), _copy("arc6", "FREE")]


def test_the_folder_holding_the_chain_is_read_and_the_other_left_alone():
    from dbabrain.backup_restore import offload as O

    assert O.repository_source(ACROSS_THE_UPGRADE, "/srv/ora/backup", names={"l0b", "arc6"}) == (
        "REPO", "/mnt/nas/ORA-SRC/FREE")
    assert O.repository_source(ACROSS_THE_UPGRADE, "/srv/ora/backup", names={"l0a", "arc2"}) == (
        "REPO", "/mnt/nas/ORA-SRC/database"), "a moment before the upgrade reads the older folder"


def test_without_the_names_two_folders_are_still_refused_and_a_chain_split_across_them_too():
    from dbabrain.backup_restore import offload as O

    with pytest.raises(O.OffloadRefused, match="are in 2 places"):
        O.repository_source(ACROSS_THE_UPGRADE, "/srv/ora/backup")
    with pytest.raises(O.OffloadRefused, match=r"the 2 file\(s\) this restore reads from /srv/ora/backup are in 2"):
        O.repository_source(ACROSS_THE_UPGRADE, "/srv/ora/backup", names={"l0a", "arc6"})
    with pytest.raises(O.OffloadRefused, match="no verified repository copy of the 1 file"):
        O.repository_source(ACROSS_THE_UPGRADE, "/srv/ora/backup", names={"arc9"})


def _source_of(monkeypatch, pieces):
    monkeypatch.setattr(R, "_oracle_recorded_pieces", lambda job, **_k: (pieces, "said"))
    monkeypatch.setattr("dbabrain.backup_restore.backup.resolve_ssh_target",
                        lambda server_id, **_k: SimpleNamespace(server_id=server_id, container_name=""))
    catalog = SimpleNamespace(repository_locations=lambda **_k: list(ACROSS_THE_UPGRADE),
                              repository_proofs=lambda **_k: {})
    return R._repository_source(_job(source_backup_host_dir="/srv/ora/backup"), catalog=catalog, data_dir=None,
                                point_in_time="2026-11-16T06:30:00Z")


def test_an_oracle_restore_reads_the_folder_of_the_chain_it_chose(monkeypatch):
    repository, folder, _proofs = _source_of(monkeypatch, ["l0b", "arc6"])
    assert (repository.server_id, folder) == ("REPO", "/mnt/nas/ORA-SRC/FREE")


def test_a_chain_begun_before_the_upgrade_is_refused_with_what_to_do(monkeypatch):
    from dbabrain.backup_restore.offload import OffloadRefused

    with pytest.raises(OffloadRefused, match="take a level 0, then restore from it"):
        _source_of(monkeypatch, ["l0a", "arc6"])
