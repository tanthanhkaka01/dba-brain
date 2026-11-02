"""Every backup entry states where its backups go, and every restore entry where it reads them (0.34.0).

The operator, 2026-10-30: the backup flow copies each run's files to a repository (a shared folder
or another VM), checks checksum and bytes there as a restore does, may scan them, and only then
deletes them on the database server; a restore may read from the repository. *Every field is
mandatory - an entry that lacks one gets it added*, and the flows that run today keep running.

So the ``offload_*`` fields and ``restore_from`` follow ``integrity`` and ``action``:
- stated on every entry, written by ``upgrade-config`` and the registrars as the values that keep
  the entry's flow (offload off, restore from the server);
- refused by name when half-written or contradictory;
- refused at run time when unstated (R50: never a code default).
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from conftest import present_copies

from dbabrain.backup_restore import backup as backup_module
from dbabrain.backup_restore import registration
from dbabrain.lib import offload as O
from tests.test_registering_a_backup_a_restore_and_an_os_login import KEY, _backup, _restore

STATED = dict(O.OFFLOAD_FIELDS)
ON = {**STATED, "offload_enabled": True, "offload_server_id": "REPO-192-0-2-60", "offload_folder": "/repo/appdb"}


# --------------------------------------------------------------------------- #
# The fields
# --------------------------------------------------------------------------- #
def test_an_entry_from_before_states_none_and_a_stated_one_is_off_as_it_ran():
    assert O.parse_backup_offload({}, label="B") is None
    stated = O.parse_backup_offload(STATED, label="B")
    assert (stated.enabled, stated.delete_source, stated.scan, stated.copy_max_retry) == (False, False, "none", 3)


def test_a_half_written_block_is_refused_naming_what_is_missing():
    partial = {"offload_enabled": False, "offload_server_id": ""}
    with pytest.raises(O.OffloadConfigError, match="offload_folder"):
        O.parse_backup_offload(partial, label="B")


@pytest.mark.parametrize("change, message", [
    ({"offload_enabled": True}, "offload_server_id and offload_folder must name"),
    ({**ON, "offload_source_share": "//192.0.2.10/SQLBK"}, "needs offload_source_username and offload_source_password_ref"),
    ({"offload_delete_source": True}, "only after its copy on the repository is proven"),
    ({"offload_scan": "avast"}, "offload_scan must be one of none, clamav"),
    ({"offload_copy_max_retry": 11}, "0 to 10"),
    ({"offload_enabled": "yes"}, "must be true or false"),
])
def test_a_contradictory_or_mistyped_value_is_refused_by_name(change, message):
    with pytest.raises(O.OffloadConfigError, match=message):
        O.parse_backup_offload({**STATED, **change}, label="B")


def test_an_oracle_entry_never_deletes_its_source_by_file():
    with pytest.raises(O.OffloadConfigError, match="RMAN owns its pieces"):
        O.parse_backup_offload({**ON, "offload_delete_source": True}, label="B", db_type="oracle")
    assert O.parse_backup_offload({**ON, "offload_delete_source": True}, label="B", db_type="sqlserver").delete_source


@pytest.mark.parametrize("stated, read", [(None, None), ("SERVER", "server"), ("repository", "repository")])
def test_restore_from_is_read_as_stated(stated, read):
    assert O.parse_restore_from({} if stated is None else {"restore_from": stated}) == read


def test_restore_from_outside_the_two_is_refused():
    with pytest.raises(O.OffloadConfigError, match="restore_from must be one of server, repository"):
        O.parse_restore_from({"restore_from": "nas"})


# --------------------------------------------------------------------------- #
# upgrade-config and the registrars write them
# --------------------------------------------------------------------------- #
def test_upgrade_config_states_every_missing_field_and_keeps_what_is_stated():
    document = {"backup_restore": {
        "backups": [{"backup_id": "OLD"}, {"backup_id": "ON", **ON}],
        "restores": [{"restore_id": "R"}, {"restore_id": "R2", "restore_from": "repository"}]}}
    before = copy.deepcopy(document["backup_restore"]["backups"][1])

    changes = O.state_offload(document)

    backups, restores = document["backup_restore"]["backups"], document["backup_restore"]["restores"]
    assert {k: backups[0][k] for k in O.OFFLOAD_FIELDS} == STATED, "off - the flow it ran"
    assert backups[1] == before, "a stated block is never overridden"
    assert [r["restore_from"] for r in restores] == ["server", "repository"]
    assert len(changes) == 2


def test_backup_add_writes_every_offload_field_with_the_operators_values(tmp_path):
    registration.add_backup(_backup(offload_scan="clamav"), data_dir=tmp_path, key=KEY)
    entry = json.loads((tmp_path / "restore_config.json").read_text(encoding="utf-8"))["backup_restore"]["backups"][0]
    assert {k: entry[k] for k in O.OFFLOAD_FIELDS} == {**STATED, "offload_scan": "clamav"}


def test_backup_add_refuses_an_offload_that_names_no_repository(tmp_path):
    with pytest.raises(registration.RegistrationError, match="must name the repository"):
        registration.add_backup(_backup(offload_enabled=True), data_dir=tmp_path, key=KEY)


def test_restore_add_states_restore_from(tmp_path):
    registration.add_restore(_restore(), data_dir=tmp_path, key=KEY)
    entry = json.loads((tmp_path / "restore_config.json").read_text(encoding="utf-8"))["backup_restore"]["restores"][0]
    assert entry["restore_from"] == "server"


# --------------------------------------------------------------------------- #
# The readers
# --------------------------------------------------------------------------- #
def test_every_job_of_an_entry_carries_its_offload(tmp_path):
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"backups": [{
        "backup_id": "B", "server_id": "S", "db_type": "sqlserver", "backup_dir": "/b", **ON,
        "jobs": [{"job": "full", "script": "x.sh", "cleanup_retention": 86400, "time_window": {"repeat_interval": 3600}},
                 {"job": "log", "script": "x.sh", "cleanup_retention": 86400, "time_window": {"repeat_interval": 900}}]}]}}),
        encoding="utf-8")
    jobs = backup_module.load_backup_jobs(path)
    assert [(j.job, j.offload.server_id, j.offload.folder) for j in jobs] == [
        ("full", "REPO-192-0-2-60", "/repo/appdb"), ("log", "REPO-192-0-2-60", "/repo/appdb")]


@pytest.mark.parametrize("restore_from, message", [
    (None, "states no restore_from"),
    # A share-driven entry (no ScriptRestore): the repository is an SSH host, that path reads a share.
    ("repository", "restore_from repository is built for the script-driven"),
])
def test_a_restore_entry_unstated_or_from_the_repository_fails_its_own_run(restore_from, message):
    from dbabrain.backup_restore.workflow import _entry_action

    entry = SimpleNamespace(restore_id="R", action="restore", restore_from=restore_from)
    action, error = _entry_action(entry, copy_only=False)
    assert action is None and message in str(error)
    assert _entry_action(SimpleNamespace(restore_id="R", action="restore", restore_from="server"),
                         copy_only=False) == ("restore", None)


def test_the_master_data_states_every_field_on_every_entry():
    from pathlib import Path

    for name in present_copies("data/restore_config.json", "data/restore_config.example.json"):
        section = json.loads(Path(name).read_text(encoding="utf-8"))["backup_restore"]
        for entry in section.get("backups") or []:
            assert set(O.OFFLOAD_FIELDS) <= set(entry), (name, entry.get("backup_id"))
            O.parse_backup_offload(entry, label=entry["backup_id"], db_type=entry.get("db_type", ""))
        for entry in section.get("restores") or []:
            assert entry.get("restore_from") in O.RESTORE_FROM, (name, entry.get("restore_id"))


# --------------------------------------------------------------------------- #
# Which databases a backup job takes - a list, stated on every job
# --------------------------------------------------------------------------- #
from dbabrain.lib import backup_scope as S  # noqa: E402


def test_a_job_names_its_databases_as_a_list_or_takes_every_user_database():
    named = S.parse_backup_databases({"databases": ["HR", "SALESDB", "HR"], "all_databases": False}, label="J")
    assert named == S.BackupScope(databases=("HR", "SALESDB"), all_databases=False)
    assert S.script_env(named) == {"MSSQL_DATABASES": "HR,SALESDB"}, "the script's own input, built by the app"
    every = S.parse_backup_databases({"databases": [], "all_databases": True}, label="J")
    assert S.script_env(every) == {}, "nothing named: the script takes every user database"
    assert S.parse_backup_databases({}, label="J") is None


@pytest.mark.parametrize("job, message", [
    ({"databases": ["HR"]}, "without all_databases"),
    ({"databases": "HR,SALESDB", "all_databases": False}, "must be a list of database names"),
    ({"databases": ["HR"], "all_databases": True}, "one answer"),
    ({"databases": [], "all_databases": False}, "name the databases"),
    ({"databases": ["HR"], "all_databases": False, "env": {"MSSQL_DATABASES": "HR"}}, "as well as in databases"),
])
def test_a_job_whose_databases_are_half_stated_mistyped_or_stated_twice_is_refused(job, message):
    with pytest.raises(S.BackupScopeError, match=message):
        S.parse_backup_databases(job, label="J")


def test_a_whole_cluster_engine_cannot_name_databases():
    with pytest.raises(S.BackupScopeError, match="takes the whole cluster"):
        S.parse_backup_databases({"databases": ["appdb"], "all_databases": False}, label="J", db_type="postgresql")


def test_upgrade_config_moves_the_env_string_into_the_list():
    document = {"backup_restore": {"backups": [{"backup_id": "B", "jobs": [
        {"job": "full", "env": {"MSSQL_USER": "u", "MSSQL_DATABASES": "HR, SALESDB"}},
        {"job": "log", "env": {}},
        {"job": "stated", "databases": ["X"], "all_databases": False}]}]}}

    changes = S.state_backup_databases(document)

    full, log, stated = document["backup_restore"]["backups"][0]["jobs"]
    assert (full["databases"], full["all_databases"], full["env"]) == (["HR", "SALESDB"], False, {"MSSQL_USER": "u"})
    assert (log["databases"], log["all_databases"]) == ([], True), "none named: every user database, as it ran"
    assert stated == {"job": "stated", "databases": ["X"], "all_databases": False}
    assert len(changes) == 2


def test_the_request_carries_the_list_and_a_job_with_none_stated_is_refused():
    from dbabrain.backup_restore import spec_builder
    from tests.test_backup_jobs import _job_and_target

    job, target = _job_and_target(scope=S.BackupScope(databases=("HR", "SALESDB"), all_databases=False))
    assert spec_builder.backup_request_from_job(job, target=target, secrets={})["env"]["MSSQL_DATABASES"] == "HR,SALESDB"
    job, target = _job_and_target(scope=None)
    with pytest.raises(S.BackupScopeError, match="states no databases"):
        spec_builder.backup_request_from_job(job, target=target, secrets={})


def test_every_master_job_states_its_databases_as_a_list():
    from pathlib import Path

    for name in present_copies("data/restore_config.json", "data/restore_config.example.json"):
        for entry in json.loads(Path(name).read_text(encoding="utf-8"))["backup_restore"].get("backups") or []:
            for job in entry["jobs"]:
                label = f"{name}:{entry['backup_id']}.{job['job']}"
                assert S.parse_backup_databases(job, label=label, db_type=entry.get("db_type", "")) is not None, label


# --------------------------------------------------------------------------- #
# How long the repository keeps each copy - by the level of its file type
# --------------------------------------------------------------------------- #
def test_the_repository_keeps_each_copy_by_its_level_and_zero_keeps_it_for_good():
    kept = O.parse_backup_offload({**ON, "offload_retention_full": 2419200, "offload_retention_diff": 1209600,
                                   "offload_retention_log": 604800}, label="B")
    assert [kept.retention_of(t) for t in ("FULL", "FULL_BASE", "RMAN_L0", "CERT", "CONTROLFILE")] == [2419200] * 5, \
        "a certificate or a control file belongs to the full it travels with"
    assert [kept.retention_of(t) for t in ("DIFF", "INCR_BASE", "RMAN_L1")] == [1209600] * 3
    assert [kept.retention_of(t) for t in ("LOG", "WAL", "ARCHIVELOG")] == [604800] * 3
    assert O.parse_backup_offload(STATED, label="B").retention_of("FULL") == 0, "written as 0: never deleted"


@pytest.mark.parametrize("value", [-1, "604800", True, 1.5])
def test_a_retention_that_is_not_whole_seconds_is_refused(value):
    with pytest.raises(O.OffloadConfigError, match="must be whole seconds"):
        O.parse_backup_offload({**STATED, "offload_retention_log": value}, label="B")
