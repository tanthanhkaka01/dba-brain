from __future__ import annotations

import json

import pytest

from db_ops.sre.docker_db import provisioner, register_config
from db_ops.sre.docker_db.models import DockerDbSpec


def _spec(**over):
    base = dict(name="pg_lab_01", engine="postgres", version="16", mode="single",
                host_port=5433, password_env="POSTGRES_PASSWORD")
    base.update(over)
    return DockerDbSpec(**base)


# --------------------------------------------------------------------------- #
# Connection registry
# --------------------------------------------------------------------------- #
def test_build_connection_entry_shape():
    entry = register_config.build_connection_entry(
        _spec(), host="10.0.0.1", compose_path="/opt/db_ops/containers/pg_lab_01/docker-compose.yml",
        worker_host="10.0.0.1",
    )
    assert entry["id"] == "PG_LAB_01"
    assert entry["engine"] == "postgres"
    assert entry["port"] == 5433
    assert entry["username"] == "postgres"
    assert entry["password_env"] == "POSTGRES_PASSWORD"
    assert entry["docker"]["instance_name"] == "pg_lab_01"
    assert entry["created_by"] == "db_ops.sre.create-db-docker"
    # No password value anywhere in the entry.
    assert "password" not in json.dumps(entry).replace("password_env", "")


def test_ha_entry_records_replicas():
    entry = register_config.build_connection_entry(
        _spec(mode="ha-lab", replicas=3), host="h", compose_path="c",
    )
    assert entry["docker"]["replicas"] == 3
    assert entry["docker"]["mode"] == "ha-lab"


def test_register_add_then_update_is_idempotent(tmp_path):
    path = tmp_path / "docker_db_connections.json"
    e1 = register_config.build_connection_entry(_spec(), host="10.0.0.1", compose_path="c")
    assert register_config.register_connection(path, e1) == "added"

    e2 = register_config.build_connection_entry(_spec(host_port=5999), host="10.0.0.2", compose_path="c")
    assert register_config.register_connection(path, e2) == "updated"

    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data["docker_db_connections"]
    assert len(entries) == 1  # same id → replaced, not duplicated
    assert entries[0]["host"] == "10.0.0.2"
    assert entries[0]["port"] == 5999


def test_register_two_distinct_ids(tmp_path):
    path = tmp_path / "docker_db_connections.json"
    register_config.register_connection(path, register_config.build_connection_entry(
        _spec(name="pg_a"), host="h", compose_path="c"))
    register_config.register_connection(path, register_config.build_connection_entry(
        _spec(name="pg_b"), host="h", compose_path="c"))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {e["id"] for e in data["docker_db_connections"]} == {"PG_A", "PG_B"}


def test_load_registry_rejects_bad_shape(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"wrong_key": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="registry"):
        register_config.load_registry(path)


# --------------------------------------------------------------------------- #
# Provisioner guards
# --------------------------------------------------------------------------- #
def test_resolve_password_from_env(monkeypatch):
    monkeypatch.setenv("MY_PW", "hunter2")
    value, source = provisioner.resolve_password_value("MY_PW")
    assert value == "hunter2"
    assert source == "env:MY_PW"


def test_resolve_password_missing_allow(monkeypatch):
    monkeypatch.delenv("MY_PW", raising=False)
    monkeypatch.delenv("DB_OPS_SECRET_KEY", raising=False)
    value, _ = provisioner.resolve_password_value("MY_PW", allow_missing=True)
    assert value is None


def test_resolve_password_missing_raises(monkeypatch):
    monkeypatch.delenv("MY_PW", raising=False)
    monkeypatch.delenv("DB_OPS_SECRET_KEY", raising=False)
    with pytest.raises(provisioner.ProvisionError, match="Password not found"):
        provisioner.resolve_password_value("MY_PW", allow_missing=False)


def test_check_ports_free_detects_clash():
    def fake_runner(cmd, **kw):
        class R:
            stdout = "0.0.0.0:5433->5432/tcp, :::5433->5432/tcp"
            returncode = 0
        return R()

    with pytest.raises(provisioner.ProvisionError, match="5433"):
        provisioner.check_ports_free([5433], fake_runner)


def test_check_ports_free_passes_when_clear():
    def fake_runner(cmd, **kw):
        class R:
            stdout = "0.0.0.0:9999->5432/tcp"
            returncode = 0
        return R()

    provisioner.check_ports_free([5433, 5434], fake_runner)  # no raise


def test_instance_ports_ha():
    ports = provisioner._instance_ports(_spec(mode="ha-lab", replicas=2))
    assert ports == [5433, 5434, 5435]
