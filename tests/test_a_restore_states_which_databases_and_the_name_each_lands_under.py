"""A SQL Server restore states which databases it takes and the name each is restored as (0.39.0, 1.362).

The operator, 2026-11-13 (translated): *restore must be guarded very carefully - into an existing DB,
into a new DB, under another name*; and the same day: *these fields must be required; in backup
restore most fields must be required - a mistake can ruin the whole production.*

Until then a script-driven SQL Server entry - every restore of the repository flow - named its
databases as a comma-separated string in ``env.MSSQL_DATABASES``; with none named it restored
**whatever FULL the folder held**, and each landed under its backup's own name, the only name there
was. A share-driven entry has had to say since 0.27.0 (1.101). Now both say it the same way:
``database_mappings`` - each ``source_database`` with the ``target_database`` it is restored AS - or
``restore_all_databases: true``. An entry stating neither is refused inside its own run; the plan
lists a database by the name its backup carries and restores it under the name the entry states;
and ``upgrade-config`` moves an existing ``env.MSSQL_DATABASES`` over, each name mapped to itself.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from conftest import landing_of
from conftest import stated as stated_fields

from dbabrain.backup_restore import restore_by_id, workflow
from dbabrain.backup_restore.restore_script import load_script_restores
from dbabrain.lib.restore import landing as L


def _maps(*pairs):
    return [{"source_database": source, "target_database": target} for source, target in pairs]


# --------------------------------------------------------------------------- #
# The statement, read once
# --------------------------------------------------------------------------- #
def test_each_database_is_named_with_the_name_it_lands_under():
    landing = L.parse_landing({"database_mappings": _maps(("APPDB", "APPDB"), ("HR", "HR_COPY"))},
                              db_type="sqlserver")

    assert landing.names == (("APPDB", "APPDB"), ("HR", "HR_COPY")) and landing.all_databases is False
    assert landing.sources == ("APPDB", "HR")
    assert landing.renamed == (("HR", "HR_COPY"),), "the same name twice is the plain restore"
    assert L.said(list(landing.names)) == "APPDB, HR as HR_COPY"


def test_every_database_of_the_folder_is_the_other_answer():
    landing = L.parse_landing({"restore_all_databases": True}, db_type="sqlserver")

    assert landing.all_databases is True and landing.names == ()
    assert L.landing_names(landing, ["APPDB", "HR"]) == [("APPDB", "APPDB"), ("HR", "HR")]


def test_an_entry_that_states_neither_reads_as_nothing_stated():
    assert L.parse_landing({"env": {"MSSQL_USER": "sa"}}, db_type="sqlserver") is None
    with pytest.raises(L.LandingError, match="restore_id=X states no database_mappings and no restore_all_databases"):
        L.require_landing(None, label="restore_id=X")


@pytest.mark.parametrize("entry, said", [
    ({"database_mappings": _maps(("HR", "HR")), "restore_all_databases": True}, "one answer"),
    ({"database_mappings": [], "restore_all_databases": False}, "name the databases"),
    ({"database_mappings": []}, "name the databases"),
    ({"restore_all_databases": "yes"}, "must be true or false"),
    ({"database_mappings": "HR,SALES"}, "must be a list"),
    ({"database_mappings": ["HR"]}, "a database mapping is an object"),
    ({"database_mappings": [{"source_database": "HR"}]}, "needs target_database"),
    ({"database_mappings": [{"target_database": "HR"}]}, "needs source_database"),
    ({"database_mappings": _maps(("HR", "A"), ("hr", "B"))}, "source_database 'hr' is named twice"),
    ({"database_mappings": _maps(("HR", "SAME"), ("SALES", "same"))}, "two databases cannot land under one name"),
    ({"database_mappings": _maps(("HR", "x" * 129))}, "not a database name SQL Server takes"),
    ({"database_mappings": _maps(("HR", "HR")), "env": {"MSSQL_DATABASES": "HR"}}, "state them once"),
])
def test_what_says_two_things_or_nothing_is_refused_by_name(entry, said):
    with pytest.raises(L.LandingError, match=said):
        L.parse_landing(entry, label="restore[X]", db_type="sqlserver")


@pytest.mark.parametrize("engine", ["postgresql", "oracle"])
def test_a_whole_instance_engine_has_nothing_to_name(engine):
    """A base backup and an RMAN duplicate bring a whole cluster / instance: its databases come under
    the names they had, so the entry states neither field - and a mapping there is refused, never
    read and ignored."""
    assert L.parse_landing({"db_type": engine}, db_type=engine) is None
    with pytest.raises(L.LandingError, match="lays down the whole cluster / instance"):
        L.parse_landing({"database_mappings": _maps(("HR", "HR_COPY"))}, db_type=engine)
    with pytest.raises(L.LandingError, match="lays down the whole cluster / instance"):
        L.parse_landing({"restore_all_databases": True}, db_type=engine)


# --------------------------------------------------------------------------- #
# upgrade-config: what the entry had been doing, stated
# --------------------------------------------------------------------------- #
def test_upgrade_config_moves_the_env_list_and_states_all_where_none_was_named():
    document = {"backup_restore": {"restores": [
        {"restore_id": "NAMED", "db_type": "sqlserver", "script": "s",
         "env": {"MSSQL_USER": "sa", "MSSQL_DATABASES": "APPDB, HR ,APPDB"}},
        {"restore_id": "SILENT", "db_type": "sqlserver", "script": "s", "env": {"MSSQL_USER": "sa"}},
        {"restore_id": "STATED", "db_type": "sqlserver", "script": "s", "restore_all_databases": True,
         "env": {"MSSQL_DATABASES": "LEFT_ALONE"}},
        {"restore_id": "PG", "db_type": "postgresql", "script": "s", "env": {}},
        {"restore_id": "SHARE", "source": {}, "target": {}},
    ]}}

    changes = L.state_restore_landing(document)
    entries = {entry["restore_id"]: entry for entry in document["backup_restore"]["restores"]}

    assert entries["NAMED"]["database_mappings"] == _maps(("APPDB", "APPDB"), ("HR", "HR"))
    assert "MSSQL_DATABASES" not in entries["NAMED"]["env"], "moved, not copied: one place says it"
    assert entries["SILENT"]["restore_all_databases"] is True and "database_mappings" not in entries["SILENT"]
    assert entries["STATED"]["env"] == {"MSSQL_DATABASES": "LEFT_ALONE"}, "an entry that states it is the operator's"
    assert "database_mappings" not in entries["PG"] and "restore_all_databases" not in entries["PG"]
    assert "database_mappings" not in entries["SHARE"], "a share-driven entry has its own rule (1.101)"
    assert [(c["where"], c["field"]) for c in changes] == [
        ("backup_restore.restores[NAMED]", "database_mappings"),
        ("backup_restore.restores[SILENT]", "restore_all_databases")]
    assert L.state_restore_landing(document) == [], "run again, it changes nothing"


def test_the_upgraded_entry_loads_as_what_it_ran_as():
    entry = {"restore_id": "NAMED", "db_type": "sqlserver", "script": "s",
             "env": {"MSSQL_USER": "sa", "MSSQL_DATABASES": "APPDB,HR"}}
    L.state_restore_landing({"backup_restore": {"restores": [entry]}})

    assert L.parse_landing(entry, db_type="sqlserver").names == (("APPDB", "APPDB"), ("HR", "HR"))


# --------------------------------------------------------------------------- #
# The loader and the run
# --------------------------------------------------------------------------- #
def _entry(**over) -> dict:
    entry = {"restore_id": "LAB_TO_LAB", "db_type": "sqlserver", "server_id": "SRC",
             "target_server_id": "TGT", "backup_dir": "/out", "script": "sqlserver",
             "target_backup_dir": "/opt/db_ops/staging/lab", "source_backup_host_dir": "/opt/out",
             "cleanup_retention": 86400, "time_window": {"from_hour": 0, "to_hour": 23},
             "env": {"MSSQL_USER": "sa"}, "overwrite_existing": False, "action": "restore",
             "restore_from": "server",
             # The four fields of 1.366, stated like every entry: this file is about the databases.
             "allow_production_target": False, "checkdb": True, "copy_selection": "chain",
             "space_check": {"enabled": True, "factor": 2.0, "on_unknown": "refuse"}}
    entry.update(over)
    # And every other field that decides its run (1.366, the operator's second word), as upgrade-config
    # writes them.
    return stated_fields(entry)


def _load(tmp_path, entry: dict):
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"restores": [entry]}}), encoding="utf-8")
    return load_script_restores(path)


def test_the_loader_reads_it_and_an_unstated_entry_still_loads(tmp_path):
    stated = _load(tmp_path, _entry(database_mappings=_maps(("APPDB", "APPDB_COPY"))))[0]
    assert stated.landing.names == (("APPDB", "APPDB_COPY"),)
    # Loaded, so one such entry cannot stop every other restore of the file; refused when it runs.
    assert _load(tmp_path, _entry())[0].landing is None


def test_a_mistyped_statement_on_an_inactive_entry_is_kept_out_with_its_reason(tmp_path):
    jobs = _load(tmp_path, _entry(active=False, database_mappings=_maps(("HR", "X"), ("SALES", "x"))))

    assert not jobs
    assert "two databases cannot land under one name" in jobs.unusable["LAB_TO_LAB"]


def test_the_workflow_refuses_an_unstated_entry_inside_its_own_run(tmp_path):
    silent = _load(tmp_path, _entry())[0]
    action, refused = workflow._entry_action(silent, copy_only=False)
    assert action is None and "states no database_mappings and no restore_all_databases" in str(refused)

    named = _load(tmp_path, _entry(restore_all_databases=True))[0]
    assert workflow._entry_action(named, copy_only=False) == ("restore", None)


def test_restore_by_id_refuses_it_before_anything_is_copied(monkeypatch, tmp_path):
    from dbabrain.backup_restore import restore_script

    silent = _load(tmp_path, _entry())[0]
    monkeypatch.setattr(restore_script, "load_script_restores", lambda _path=None: [silent])
    monkeypatch.setattr(restore_script, "transfer_backup_to_target",
                        lambda *a, **k: pytest.fail("copied for an entry that states no databases"))

    with pytest.raises(restore_by_id.RestoreByIdError, match="states no database_mappings"):
        restore_by_id.restore_by_id({"restore_id": "LAB_TO_LAB"})


def test_the_old_script_still_gets_its_list_built_from_the_statement(tmp_path):
    from dbabrain.backup_restore.restore_script import _script_env

    job = _load(tmp_path, _entry(database_mappings=_maps(("APPDB", "APPDB"), ("HR", "HR"))))[0]
    source = SimpleNamespace(container_name="src")

    assert _script_env(job, source)["MSSQL_DATABASES"] == "APPDB,HR"


# --------------------------------------------------------------------------- #
# The plan: listed by the backup's name, restored under the entry's
# --------------------------------------------------------------------------- #
FULL = {"path": "/s/HR/FULL/HR_FULL_20261112_010000Z.bak", "database_name": "HR", "kind": "full",
        "finished_at": "2026-11-12 01:00:40"}
LOGS = [{"path": f"/s/HR/LOG/HR_LOG_20261112_0{hour}0000Z.trn", "database_name": "HR", "kind": "log",
         "finished_at": f"2026-11-12 0{hour}:00:05"} for hour in (2, 3)]


def _plan(monkeypatch, landing, *, moment="", overwrite=False):
    asked = []

    def listed(request):
        asked.append(request)
        kind = request["kinds"][0]
        files = [FULL] if kind == "full" else LOGS if kind == "log" else []
        return {"files": files, "newest_finished_at": "2026-11-12 01:00:40" if files else None}

    monkeypatch.setattr(restore_by_id, "_list_backup_files", listed)
    monkeypatch.setattr(restore_by_id, "_visible_dir", lambda job: "/s")
    monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda job, data_dir=None: 1433)
    job = SimpleNamespace(restore_id="HR_TO_LAB", env={"MSSQL_USER": "dba"}, env_secrets={},
                          landing=landing, overwrite_existing=overwrite)
    steps = restore_by_id._plan_sqlserver(job, {}, point_in_time=moment, host={"host": "192.0.2.40"}, data_dir=None)
    return steps, asked


def test_another_name_reaches_every_step_and_the_check(monkeypatch):
    landing = L.Landing(names=(("HR", "HR_COPY"),), all_databases=False)
    steps, asked = _plan(monkeypatch, landing)

    # The backup knows the database as HR: that is the name it is listed by.
    assert {request["database_name"] for request in asked} == {"HR"}
    restoring = [step for step in steps if step["op"] in ("restore-full", "restore-log")]
    assert {step["request"]["database_name"] for step in restoring} == {"HR_COPY"}
    assert [step["op"] for step in restoring][:2] == ["restore-full", "restore-log"]
    assert all(step["source_database"] == "HR" for step in restoring)
    check = next(step for step in steps if step["op"] == "verify-restore")
    assert check["request"]["database_names"] == ["HR_COPY"], "the check opens the name it landed under"


def test_the_guard_is_asked_about_the_name_it_lands_under(monkeypatch):
    from dbabrain.common.restorestep import sqlserver as mssql

    steps, _ = _plan(monkeypatch, L.Landing(names=(("HR", "HR_COPY"),), all_databases=False))
    full = next(step for step in steps if step["op"] == "restore-full")["request"]

    text = mssql.build_statements("full", full, [full["backup_path"]])[0]
    assert "IF DB_ID(N'HR_COPY') IS NOT NULL" in text and "DB_ID(N'HR')" not in text
    # Its files too: each as <the name it lands under>_<its own file name>, so a copy beside the
    # original never asks for the original's files.
    assert "N'HR_COPY_'" in text


def test_the_summary_says_the_database_and_the_name_it_lands_under(monkeypatch):
    renamed, _ = _plan(monkeypatch, L.Landing(names=(("HR", "HR_COPY"),), all_databases=False))
    plain, _ = _plan(monkeypatch, landing_of("HR"))

    assert restore_by_id._plan_summary(renamed).startswith("HR as HR_COPY: full ")
    assert restore_by_id._plan_summary(plain).startswith("HR: full ")


def test_every_database_of_the_folder_is_restored_under_its_own_name(monkeypatch):
    steps, _ = _plan(monkeypatch, landing_of())

    full = next(step for step in steps if step["op"] == "restore-full")
    assert full["request"]["database_name"] == "HR" and full["source_database"] == "HR"


def test_a_job_that_states_nothing_is_refused_by_the_plan_too(monkeypatch):
    with pytest.raises(restore_by_id.RestoreByIdError, match="states no database_mappings"):
        _plan(monkeypatch, None)


def test_the_reference_says_a_script_entry_states_it_too():
    from pathlib import Path

    reference = json.loads((Path(__file__).resolve().parents[1] / "dbabrain/common/catalogue/shared_config_objects.json")
                           .read_text(encoding="utf-8-sig"))["shared_config_objects"]
    entry = next(item for item in reference if item["object"] == "restore_entry")
    mappings = next(field for field in entry["fields"] if field["field"] == "database_mappings")

    assert "script-driven SQL Server entry" in mappings["purpose"]
    assert "refused inside its own run" in mappings["rule"]


# --------------------------------------------------------------------------- #
# The dry run says where it lands, and what is there now
# --------------------------------------------------------------------------- #
def _lands(monkeypatch, *, landing, overwrite=False, states=None, files=(), engine="sqlserver", fails=False):
    job = SimpleNamespace(restore_id="R", db_type=engine, env={"MSSQL_USER": "dba"}, env_secrets={},
                          landing=landing, overwrite_existing=overwrite)
    asked = []

    def answer(op, request, **_kw):
        asked.append((op, request))
        if fails:
            return False, {}, "login timeout expired"
        # As the transport answers: a database that is not there makes the verdict "not usable".
        rows = [{"database_name": name, "state": state} for name, state in (states or {}).items()]
        return all(row["state"] == "ONLINE" for row in rows), {"databases": rows}, ""

    monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda job, data_dir=None: 1433)
    monkeypatch.setattr(restore_by_id.common_cli, "run_allowing_failure", answer)
    return restore_by_id._where_it_lands(job, {}, host={"host": "h"}, data_dir=None, files=list(files)), asked


def test_the_dry_run_asks_the_target_about_each_name_and_changes_nothing(monkeypatch):
    landing = L.Landing(names=(("APP", "APP"), ("HR", "HR_COPY")), all_databases=False)
    lands, asked = _lands(monkeypatch, landing=landing, states={"APP": "ONLINE", "HR_COPY": "ABSENT"})

    op, request = asked[0]
    assert op == "verify-restore" and request["consistency"] is False, "read only: no CHECKDB, nothing written"
    assert request["database_names"] == ["APP", "HR_COPY"], "asked about the names they land under"
    assert [(item["source_database"], item["lands_as"], item["on_target"], item["will"].split(" - ")[0])
            for item in lands] == [("APP", "APP", "ONLINE", "REFUSED"), ("HR", "HR_COPY", "ABSENT", "created")]
    assert "APP exists on the target (ONLINE) and overwrite_existing is false" in lands[0]["refused"]
    assert "refused" not in lands[1]


@pytest.mark.parametrize("state", ["ONLINE", "RESTORING", "OFFLINE", "RECOVERY_PENDING"])
def test_a_name_the_target_holds_in_any_state_is_said_as_the_restores_own_refusal(monkeypatch, state):
    lands, _ = _lands(monkeypatch, landing=landing_of("APP"), states={"APP": state})

    assert lands[0]["will"] == "REFUSED" and f"({state})" in lands[0]["refused"]


def test_with_the_entrys_word_it_says_what_it_will_replace(monkeypatch):
    lands, _ = _lands(monkeypatch, landing=landing_of("APP"), overwrite=True, states={"APP": "ONLINE"})

    assert lands[0]["will"] == "REPLACES the database there now (ONLINE) - overwrite_existing is true"
    assert "refused" not in lands[0]


def test_every_database_of_the_folder_is_read_from_the_files_the_copy_would_take(monkeypatch):
    files = [{"path": "APP/FULL/a.bak"}, {"path": "APP/LOG/b.trn"}, {"path": "HR/FULL/c.bak"}]
    lands, asked = _lands(monkeypatch, landing=landing_of(), states={"APP": "ABSENT", "HR": "ABSENT"}, files=files)

    assert asked[0][1]["database_names"] == ["APP", "HR"]
    assert [item["on_target"] for item in lands] == ["ABSENT", "ABSENT"]
    assert all(item["will"].startswith("created") for item in lands), "not there is the answer, not a failure to ask"


def test_a_target_that_cannot_be_asked_is_said_not_guessed(monkeypatch):
    lands, _ = _lands(monkeypatch, landing=landing_of("APP"), fails=True)

    assert lands[0]["on_target"] == "not read" and "login timeout expired" in lands[0]["will"]
    assert "refused" not in lands[0], "not known is not a refusal the restore would make"


@pytest.mark.parametrize("overwrite, said", [(False, "refused by its restore step when the target instance holds user data"),
                                             (True, "REPLACES whatever the target instance holds")])
def test_a_whole_instance_engine_says_it_lands_as_the_whole_instance(monkeypatch, overwrite, said):
    lands, asked = _lands(monkeypatch, landing=None, overwrite=overwrite, engine="postgresql")

    assert asked == [] and lands[0]["lands"] == "the whole instance of the target"
    assert said in lands[0]["will"]


def test_the_sentence_a_chat_reads_names_where_it_lands():
    said = workflow.dry_run_said({
        "would_refuse": ["APP exists on the target (ONLINE) and overwrite_existing is false - the restore stops "
                         "before it restores anything"],
        "lands": [{"source_database": "APP", "lands_as": "APP", "on_target": "ONLINE", "will": "REFUSED"},
                  {"source_database": "HR", "lands_as": "HR_COPY", "on_target": "ABSENT",
                   "will": "created - the target holds no database of that name"}],
        "steps": ["restore-full per database - x", "verify-restore - y"]})

    assert said.startswith("WOULD BE REFUSED - APP exists on the target (ONLINE)")
    assert "lands: APP - REFUSED; HR as HR_COPY - created - the target holds no database of that name" in said
    assert said.endswith("then restore-full per database, verify-restore - nothing changed")


# --------------------------------------------------------------------------- #
# A name the target holds stops the restore before its copy
# --------------------------------------------------------------------------- #
def _held(monkeypatch, *, landing, overwrite=False, states=None, fails=False, engine="sqlserver"):
    job = SimpleNamespace(restore_id="R", db_type=engine, env={"MSSQL_USER": "dba"}, env_secrets={},
                          landing=landing, overwrite_existing=overwrite)
    asked = []

    def answer(op, request, **_kw):
        asked.append(op)
        if fails:
            return False, {}, "login timeout expired"
        rows = [{"database_name": name, "state": state} for name, state in (states or {}).items()]
        return all(row["state"] == "ONLINE" for row in rows), {"databases": rows}, ""

    monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda job, data_dir=None: 1433)
    monkeypatch.setattr(restore_by_id.common_cli, "run_allowing_failure", answer)
    restore_by_id._refuse_a_name_already_held(job, {}, host={"host": "h"}, data_dir=None)
    return asked


def test_a_name_the_target_holds_is_refused_before_a_file_is_copied(monkeypatch):
    with pytest.raises(restore_by_id.RestoreByIdError,
                       match=r"R: APP exists on the target \(RESTORING\) and overwrite_existing is false .* "
                             "Nothing was copied or restored"):
        _held(monkeypatch, landing=landing_of("APP"), states={"APP": "RESTORING"})


def test_a_name_the_target_does_not_hold_goes_on(monkeypatch):
    landing = L.Landing(names=(("APP", "APP_COPY"),), all_databases=False)

    assert _held(monkeypatch, landing=landing, states={"APP_COPY": "ABSENT"}) == ["verify-restore"]


@pytest.mark.parametrize("case", ["overwrite", "all_databases", "postgresql", "unreachable"])
def test_it_asks_or_refuses_nothing_where_the_step_decides(monkeypatch, case):
    """The entry's word to replace, every database of the folder, a whole-instance engine - and a
    target that cannot be asked, which the restore step's own guard still decides."""
    asked = _held(monkeypatch,
                  landing=landing_of() if case == "all_databases" else None if case == "postgresql" else landing_of("APP"),
                  overwrite=case == "overwrite", states={"APP": "ONLINE"}, fails=case == "unreachable",
                  engine="postgresql" if case == "postgresql" else "sqlserver")

    assert asked == (["verify-restore"] if case == "unreachable" else [])
