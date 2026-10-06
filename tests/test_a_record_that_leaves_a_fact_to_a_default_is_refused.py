"""A record that leaves a which-thing fact to a default is refused where it is used (rules R50).

The owner's no-fallback rule (review notes G, 2026-10-01): every fact that says which thing is
acted on is stated. Records written before it left some to a default - SSH `auth_type` became
`key`, a missing port the engine's, the platform a guess from `os` or the transport, a PostgreSQL
database the service label, a script restore's login `sa`, a certificate API's token a built-in ref.
Phase 1 (0.26.0) reported each as a `fallback` notice of `check-objects`, so a node could be
completed first; phase 2 (0.27.0, the operator, 2026-10-06, after the worker and the master read 0
such records) refuses them: `check-objects` counts each as a violation, and the record is refused
where it is used, naming the field - one record alone, the rest of a scan runs (R26). The
operator's words for it, the same day: *add the full configuration - code that guesses for itself
gets it wrong easily.*
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from db_ops.backup_restore import config as restore_config_module
from db_ops.backup_restore import restore_by_id, restore_script
from db_ops.common import instance_admin, remote_credential_admin
from db_ops.lib import errors, shared_objects
from db_ops.lib.data_sources import collection_targets, request_fill
from db_ops.lib.stated_facts import FALLBACK_KIND, FactNotStated, fallbacks
from db_ops.metrics import executor

SSH = {"enabled": True, "method": "ssh", "host": "192.0.2.10", "port": 22, "credential_name": "os"}
COMPLETE = {"server_id": "PG-1", "db_type": "postgresql", "ip": "192.0.2.10", "port": 5432,
            "database_name": "app", "platform": "linux", "active": True,
            "cmd_access": {**SSH, "auth_type": "key"}}


def _write(root, **files):
    for name, payload in files.items():
        (root / name).write_text(json.dumps(payload), encoding="utf-8")
    return root


def _fields(root) -> set[str]:
    return {item["field"] for item in fallbacks(root)}


# --------------------------------------------------------------------------- #
# What counts as a fact left to a default
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("record, field", [
    ({"server_id": "A", "db_type": "sqlserver", "port": 1433, "cmd_access": SSH,
      "os": "linux"}, "platform"),
    ({"server_id": "A", "db_type": "sqlserver", "port": 1433, "platform": "linux",
      "cmd_access": SSH}, "cmd_access.auth_type"),
    ({"server_id": "A", "db_type": "postgresql", "service_name": "pg01"}, "port"),
    ({"server_id": "A", "db_type": "postgresql", "port": 5432, "service_name": "pg01"}, "database_name"),
    ({"server_id": "A", "db_type": "oracle", "port": 1521, "instance_name": "ORCL"}, "service_name"),
])
def test_each_default_an_instance_relies_on_is_named(tmp_path, record, field):
    _write(tmp_path, **{"db_instances.json": {"db_instances": [record]}})

    assert field in _fields(tmp_path)


def test_a_record_that_states_everything_is_not_reported(tmp_path):
    _write(tmp_path, **{"db_instances.json": {"db_instances": [COMPLETE]}})

    assert fallbacks(tmp_path) == []


def test_an_inactive_record_is_not_reported(tmp_path):
    _write(tmp_path, **{"db_instances.json": {"db_instances": [
        {"server_id": "A", "db_type": "oracle", "active": False}]}})

    assert fallbacks(tmp_path) == []


def test_a_restore_s_defaults_are_named(tmp_path):
    _write(tmp_path, **{"restore_config.json": {"backup_restore": {"restores": [
        {"restore_id": "SCRIPT", "script": "sqlserver", "db_type": "sqlserver", "env": {}},
        {"restore_id": "SMB", "target": {"sql_container": "mssql"},
         "source": {"certificate_api_url": "https://vault.example/cer"}, "restore_all_databases": True},
    ]}}})

    assert _fields(tmp_path) == {"env.MSSQL_USER", "target.sqlcmd_path", "source.certificate_api_token_ref"}


def test_a_restore_fact_stated_on_the_block_counts(tmp_path):
    """As the loader reads an entry: the block's keys and blocks under the entry's own."""
    _write(tmp_path, **{"restore_config.json": {"backup_restore": {
        "sqlcmd_path": "/opt/mssql-tools18/bin/sqlcmd",
        "source": {"certificate_api_token_ref": "VAULT_TOKEN"},
        "restores": [{"restore_id": "SMB", "target": {"sql_container": "mssql"},
                      "source": {"certificate_api_url": "https://vault.example/cer"},
                      "database_mappings": [{"source_database": "APPDB"}]}]}}})

    assert fallbacks(tmp_path) == []


# --------------------------------------------------------------------------- #
# check-objects: a violation since 0.27.0
# --------------------------------------------------------------------------- #
def test_check_objects_counts_them_as_violations(tmp_path):
    _write(tmp_path, **{"db_instances.json": {"db_instances": [
        {"server_id": "A", "db_type": "oracle", "port": 1521}]}})

    result = shared_objects.check_data_dir(tmp_path)

    found = [item for item in result["violations"] if item["kind"] == FALLBACK_KIND]
    assert found and found[0]["field"] == "service_name" and "R50" in found[0]["detail"]
    assert result["ok"] is False
    assert not any(item["kind"] == FALLBACK_KIND for item in result["notices"])


# --------------------------------------------------------------------------- #
# Refused where it is used - one record at a time
# --------------------------------------------------------------------------- #
def test_a_connection_is_never_built_on_a_default_port():
    with pytest.raises(FactNotStated, match=r"PG-1: port is not stated"):
        request_fill.connection_from({**COMPLETE, "port": None}, {"username": "u"}, "pw")


def test_a_connection_is_never_built_on_a_label_taken_for_a_database():
    record = {key: value for key, value in COMPLETE.items() if key != "database_name"}

    with pytest.raises(FactNotStated, match="database_name is not stated"):
        request_fill.connection_from({**record, "service_name": "PG-PROD"}, {"username": "u"}, "pw")


def test_a_complete_record_still_connects():
    assert request_fill.connection_from(COMPLETE, {"username": "u"}, "pw")["port"] == 5432


def test_an_app_asking_for_a_connection_is_told_the_field(tmp_path):
    _write(tmp_path, **{"db_instances.json": {"db_instances": [{**COMPLETE, "port": None}]}})

    with pytest.raises(request_fill.RequestFillError, match="port is not stated"):
        request_fill.sql_connection("PG-1", data_dir=tmp_path)


@pytest.mark.parametrize("change, field", [
    ({"platform": None}, "platform"),
    ({"cmd_access": SSH}, "cmd_access.auth_type"),
])
def test_a_host_is_never_reached_on_a_guess(tmp_path, change, field):
    record = {key: value for key, value in {**COMPLETE, **change}.items() if value is not None}
    _write(tmp_path, **{"db_instances.json": {"db_instances": [record]}})

    with pytest.raises(request_fill.RequestFillError, match=f"{field} is not stated"):
        request_fill.host_access("PG-1", data_dir=tmp_path)


def test_one_metric_target_with_a_gap_fails_alone(tmp_path):
    """R26: the rest of the scan runs. The gapped target is kept, and each of its SQL metrics
    fails before anything is sent, naming the field."""
    gapped = {**COMPLETE, "server_id": "PG-2", "ip": "192.0.2.11", "port": None, "cmd_access": None}
    _write(tmp_path, **{"db_instances.json": {"db_instances": [
        {**COMPLETE, "cmd_access": None}, gapped]},
        "users.json": {"database_credentials": []}})

    targets = {target.server_id: target for target in
               collection_targets.load_metric_targets(data_dir=tmp_path)}

    assert set(targets) == {"PG-1", "PG-2"}
    assert "error" not in targets["PG-1"].connection_info
    with pytest.raises(errors.InvalidConfig, match="PG-2: port is not stated"):
        executor.prepare_sql(target=targets["PG-2"], sql_text="select 1", secrets={})


def test_a_metric_target_reaching_its_host_on_a_guess_fails_its_host_metrics(tmp_path):
    gapped = {**COMPLETE, "platform": None}
    _write(tmp_path, **{"db_instances.json": {"db_instances": [
        {key: value for key, value in gapped.items() if value is not None}]},
        "users.json": {"database_credentials": []}})

    (target,) = collection_targets.load_metric_targets(data_dir=tmp_path)

    assert "platform is not stated" in target.cmd_access["error"]


# --------------------------------------------------------------------------- #
# The registrars write no such record
# --------------------------------------------------------------------------- #
def _root(tmp_path):
    _write(tmp_path, **{"db_instances.json": {"db_instances": []},
                        "users.json": {"database_credentials": [], "remote_credentials": []}})
    return tmp_path


def test_instance_add_refuses_an_oracle_target_with_no_service(tmp_path):
    root = _root(tmp_path)

    with pytest.raises(instance_admin.InstanceAdminError, match="service_name is not stated"):
        instance_admin.add_instance({"server_id": "ORA-1", "db_type": "oracle", "ip": "192.0.2.12"},
                                    data_dir=root)
    assert json.loads((root / "db_instances.json").read_text(encoding="utf-8"))["db_instances"] == []


def test_instance_add_refuses_a_host_block_with_no_platform(tmp_path):
    root = _root(tmp_path)

    with pytest.raises(instance_admin.InstanceAdminError, match="platform is not stated"):
        instance_admin.add_instance({"server_id": "H-1", "db_type": "host", "ip": "192.0.2.13",
                                     "cmd_access": {**SSH, "auth_type": "key"}}, data_dir=root)


def test_instance_add_writes_an_inactive_record_to_be_completed_later(tmp_path):
    root = _root(tmp_path)

    instance_admin.add_instance({"server_id": "ORA-1", "db_type": "oracle", "ip": "192.0.2.12",
                                 "active": False}, data_dir=root)

    assert json.loads((root / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]


def test_remote_credential_add_refuses_to_wire_a_host_with_no_platform(tmp_path):
    root = _root(tmp_path)
    _write(root, **{"db_instances.json": {"db_instances": [
        {"server_id": "H-1", "db_type": "host", "ip": "192.0.2.13", "active": True}]}})
    before = (root / "users.json").read_text(encoding="utf-8")

    with pytest.raises(instance_admin.InstanceAdminError, match="states no platform"):
        remote_credential_admin.add_remote_credential(
            {"server_id": "H-1", "username": "ops", "method": "ssh", "auth_type": "key"}, data_dir=root)
    assert (root / "users.json").read_text(encoding="utf-8") == before, "nothing was written"


# --------------------------------------------------------------------------- #
# Restore entries
# --------------------------------------------------------------------------- #
@pytest.fixture
def no_estate_defaults(tmp_path, monkeypatch):
    absent = tmp_path / "no_default_restore_config.json"
    monkeypatch.setattr(restore_config_module, "DEFAULT_RESTORE_CONFIG_PATH", absent)
    monkeypatch.setattr(restore_script, "DEFAULT_RESTORE_CONFIG_PATH", absent, raising=False)


def _smb(restore_id: str, **fields) -> dict:
    entry = {"restore_id": restore_id, "server_id": "SRC", "target_server_id": "DST",
             "cleanup_retention": 86400, "source": {"backup_share": "//192.0.2.10/SQLBK"},
             "target": {"vm_platform": "linux", "vm_import_linux_path": f"/opt/restore/import/{restore_id}",
                        "restore_data_dir": "/var/opt/mssql/data", "sql_container": "MSSQL_DRILL"},
             "database_mappings": [{"source_database": "APPDB", "target_database": "APPDB"}]}
    entry.update(fields)
    return entry


def _restore_file(tmp_path, *entries, **block):
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {**block, "restores": list(entries)}}), encoding="utf-8")
    return str(path)


def test_an_active_smb_entry_with_a_container_and_no_sqlcmd_path_is_refused(tmp_path, no_estate_defaults):
    with pytest.raises(ValueError, match="target.sqlcmd_path is not stated"):
        restore_config_module.load_restore_configs(_restore_file(tmp_path, _smb("DRILL")))


def test_the_parser_s_own_sqlcmd_default_does_not_answer_for_the_entry(tmp_path, monkeypatch):
    """The daemon runs with `--config config.json`, and the loader falls back to the node's own
    restore file with the parser's defaults merged in - `sqlcmd_path: "sqlcmd"` among them. The
    rule reads the file as written, or that default would answer for every entry."""
    restore_file = tmp_path / "restore_config.json"
    restore_file.write_text(json.dumps({"backup_restore": {"restores": [_smb("DRILL")]}}), encoding="utf-8")
    monkeypatch.setattr(restore_config_module, "DEFAULT_RESTORE_CONFIG_PATH", restore_file)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="target.sqlcmd_path is not stated"):
        restore_config_module.load_restore_configs(str(tmp_path / "config.json"))


def test_an_inactive_one_is_kept_out_with_its_reason(tmp_path, no_estate_defaults):
    configs = restore_config_module.load_restore_configs(_restore_file(
        tmp_path, _smb("OLD", active=False),
        _smb("LIVE", target={**_smb("LIVE")["target"], "sqlcmd_path": "/opt/mssql-tools18/bin/sqlcmd"})))

    assert [config.restore_id for config in configs] == ["LIVE"]
    assert "target.sqlcmd_path is not stated" in configs.unusable["OLD"]


def test_a_certificate_api_names_its_token_and_no_built_in_ref_answers_for_it(tmp_path, no_estate_defaults):
    stated = {**_smb("DRILL")["target"], "sqlcmd_path": "/opt/mssql-tools18/bin/sqlcmd"}
    entry = _smb("DRILL", target=stated,
                 source={"backup_share": "//192.0.2.10/SQLBK", "certificate_api_url": "https://vault.example/cer"})

    with pytest.raises(ValueError, match="source.certificate_api_token_ref is not stated"):
        restore_config_module.load_restore_configs(_restore_file(tmp_path, entry))
    assert restore_config_module.RESTORE_PARSER_DEFAULTS["certificate_api_token_ref"] == ""


def test_a_script_sql_server_restore_names_its_login(tmp_path, no_estate_defaults):
    entry = {"restore_id": "MSSQL_DRILL", "db_type": "sqlserver", "server_id": "SRC",
             "target_server_id": "DST", "backup_dir": "/backup", "script": "restore.sh", "env": {}}

    with pytest.raises(ValueError, match="env.MSSQL_USER is not stated"):
        restore_script.load_script_restores(_restore_file(tmp_path, entry))


def test_a_restore_plan_never_logs_in_as_sa_on_its_own():
    job = SimpleNamespace(restore_id="MSSQL_DRILL", env={}, env_secrets={})

    with pytest.raises(restore_by_id.RestoreByIdError, match="env.MSSQL_USER is not stated"):
        restore_by_id._plan_sqlserver(job, {}, point_in_time="", host={"host": "h"}, data_dir=None)


@pytest.mark.parametrize("example, live_name", [
    ("db_instances.example.json", "db_instances.json"),
    ("restore_config.example.json", "restore_config.json"),
])
def test_the_shipped_examples_state_every_fact(tmp_path, example, live_name):
    """An install starts from these. Phase 1 read only a node's own data/, and both examples carried
    an active record it would have reported - copied as shipped, 0.27.0 would refuse them."""
    from db_ops.lib.paths import resolve_tool_path

    (tmp_path / live_name).write_bytes(resolve_tool_path(f"data/{example}").read_bytes())

    assert fallbacks(tmp_path) == []


# --------------------------------------------------------------------------- #
# Which databases a SQL Server restore takes is stated (0.27.0 1.101)
# --------------------------------------------------------------------------- #
def test_an_smb_entry_that_names_no_database_and_not_all_of_them_is_refused(tmp_path, no_estate_defaults):
    """An empty list restored whatever FULL was on the share, and the check after it had no name to
    ask about - the worker's nightly restore of 13 databases, never verified (the operator,
    2026-10-06: SQL Server only; PostgreSQL and Oracle restore the instance)."""
    stated = {**_smb("ALL")["target"], "sqlcmd_path": "/opt/mssql-tools18/bin/sqlcmd"}

    with pytest.raises(ValueError, match="database_mappings is not stated"):
        restore_config_module.load_restore_configs(
            _restore_file(tmp_path, _smb("ALL", target=stated, database_mappings=[])))


def test_restore_all_databases_states_every_one_under_its_backup_s_name(tmp_path, no_estate_defaults):
    stated = {**_smb("ALL")["target"], "sqlcmd_path": "/opt/mssql-tools18/bin/sqlcmd"}

    (config,) = restore_config_module.load_restore_configs(_restore_file(
        tmp_path, _smb("ALL", target=stated, database_mappings=[], restore_all_databases=True)))

    assert config.restore_all_databases is True and config.databases == ()


@pytest.mark.parametrize("fields, words", [
    ({"restore_all_databases": True}, "states both database_mappings and restore_all_databases"),
    ({"restore_all_databases": True, "database_mappings": [], "restore_database_name": "ONE"},
     "under that single name"),
    ({"restore_all_databases": "yes", "database_mappings": []}, "must be true or false"),
])
def test_restore_all_databases_means_one_thing(tmp_path, no_estate_defaults, fields, words):
    stated = {**_smb("ALL")["target"], "sqlcmd_path": "/opt/mssql-tools18/bin/sqlcmd"}

    with pytest.raises(ValueError, match=words):
        restore_config_module.load_restore_configs(_restore_file(tmp_path, _smb("ALL", target=stated, **fields)))


def test_a_script_driven_restore_names_no_database(tmp_path, no_estate_defaults):
    """PostgreSQL and Oracle restore the instance: no list, and no flag, is asked of them."""
    entry = {"restore_id": "PG_DRILL", "db_type": "postgresql", "server_id": "SRC", "target_server_id": "DST",
             "backup_dir": "/backup", "script": "restore.sh", "cleanup_retention": 86400,
             "target_backup_dir": "/opt/db_ops/restore_staging/PG_DRILL", "source_backup_host_dir": "/opt/backup/pg"}

    assert [job.restore_id for job in restore_script.load_script_restores(_restore_file(tmp_path, entry))] == ["PG_DRILL"]
