"""A record that leaves a which-thing fact to a default is reported now, and refused next release.

The owner's no-fallback rule (review notes G, 2026-10-01): every fact that says which thing is
acted on is stated. Records written before it leave some to a default - SSH `auth_type` becomes
`key`, a missing port the engine's, the platform a guess from `os` or the transport, a PostgreSQL
database the service label, a script restore's login `sa`. Refusing them at once would stop a node
the moment it upgrades, so phase 1 reports each as a `fallback` notice of `check-objects`, naming
the field and what is assumed until then. Active records only: an inactive one runs nothing.
"""

from __future__ import annotations

import json

import pytest

from db_ops.lib import shared_objects
from db_ops.lib.stated_facts import FALLBACK_KIND, fallbacks

SSH = {"enabled": True, "method": "ssh", "host": "192.0.2.10", "port": 22, "credential_name": "os"}


def _write(root, **files):
    for name, payload in files.items():
        (root / name).write_text(json.dumps(payload), encoding="utf-8")
    return root


def _fields(root) -> set[str]:
    return {item["field"] for item in fallbacks(root)}


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
    _write(tmp_path, **{"db_instances.json": {"db_instances": [{
        "server_id": "A", "db_type": "postgresql", "port": 5432, "database_name": "app",
        "platform": "linux", "cmd_access": {**SSH, "auth_type": "password"}}]}})

    assert fallbacks(tmp_path) == []


def test_an_inactive_record_is_not_reported(tmp_path):
    _write(tmp_path, **{"db_instances.json": {"db_instances": [
        {"server_id": "A", "db_type": "oracle", "active": False}]}})

    assert fallbacks(tmp_path) == []


def test_a_restore_s_defaults_are_named(tmp_path):
    _write(tmp_path, **{"restore_config.json": {"backup_restore": {"restores": [
        {"restore_id": "SCRIPT", "script": "sqlserver", "db_type": "sqlserver", "env": {}},
        {"restore_id": "SMB", "target": {"sql_container": "mssql"},
         "source": {"certificate_api_url": "https://vault.example/cer"}},
    ]}}})

    assert _fields(tmp_path) == {"env.MSSQL_USER", "target.sqlcmd_path", "source.certificate_api_token_ref"}


def test_a_restore_fact_stated_on_the_block_counts(tmp_path):
    """As the loader reads an entry: the block's keys and blocks under the entry's own."""
    _write(tmp_path, **{"restore_config.json": {"backup_restore": {
        "sqlcmd_path": "/opt/mssql-tools18/bin/sqlcmd",
        "source": {"certificate_api_token_ref": "VAULT_TOKEN"},
        "restores": [{"restore_id": "SMB", "target": {"sql_container": "mssql"},
                      "source": {"certificate_api_url": "https://vault.example/cer"}}]}}})

    assert fallbacks(tmp_path) == []


def test_check_objects_reports_them_as_notices_not_violations(tmp_path):
    _write(tmp_path, **{"db_instances.json": {"db_instances": [
        {"server_id": "A", "db_type": "oracle", "port": 1521}]}})

    result = shared_objects.check_data_dir(tmp_path)

    notices = [item for item in result["notices"] if item["kind"] == FALLBACK_KIND]
    assert notices and notices[0]["field"] == "service_name" and "next release" in notices[0]["detail"]
    assert not any(item["kind"] == FALLBACK_KIND for item in result["violations"])
