from __future__ import annotations

import pytest

from db_ops.sre.docker_db.models import (
    ENGINE_META,
    HA_SUPPORTED_ENGINES,
    VALID_ENGINES,
    DockerDbSpec,
)


def _spec(**over):
    base = dict(name="pg_lab_01", engine="postgres", version="16", mode="single",
                host_port=5433, password_env="POSTGRES_PASSWORD")
    base.update(over)
    return DockerDbSpec(**base)


def test_valid_single_spec_passes():
    _spec().validate()


def test_engine_meta_covers_all_engines():
    assert set(ENGINE_META) == set(VALID_ENGINES)
    assert HA_SUPPORTED_ENGINES == ("postgres", "mysql", "mssql", "oracle")


@pytest.mark.parametrize("name", ["bad name", "bad!", "", "a/b", "a.b"])
def test_invalid_name_rejected(name):
    with pytest.raises(ValueError, match="name"):
        _spec(name=name).validate()


@pytest.mark.parametrize("name", ["pg_lab_01", "PG-Lab", "a", "x_1-2"])
def test_valid_names_accepted(name):
    _spec(name=name).validate()


def test_invalid_engine_rejected():
    with pytest.raises(ValueError, match="engine"):
        _spec(engine="mariadb").validate()


def test_replicas_only_valid_with_ha():
    # Explicit --replicas on a single instance is rejected (rule 4).
    with pytest.raises(ValueError, match="replicas is only valid"):
        _spec(mode="single").validate(replicas_explicit=True)
    # But a single instance with the default (implicit) replicas is fine.
    _spec(mode="single").validate(replicas_explicit=False)


def test_every_engine_supports_ha_lab():
    """"ha-lab" is each engine's own replication, not one product: PostgreSQL streaming
    replication, MySQL async primary/replica, SQL Server an Always On availability group with
    CLUSTER_TYPE = NONE (manual failover, no listener — no cluster manager exists in a lab)."""
    for engine, version in (("postgres", "18"), ("mysql", "8.4"), ("mssql", "2022-latest")):
        _spec(name=f"lab_{engine}", engine=engine, version=version, mode="ha-lab").validate()


def test_password_env_required_and_validated():
    with pytest.raises(ValueError, match="password-env"):
        _spec(password_env="").validate()
    with pytest.raises(ValueError, match="password-env"):
        _spec(password_env="1bad name").validate()


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_host_port_range(port):
    with pytest.raises(ValueError, match="host-port"):
        _spec(host_port=port).validate()


def test_missing_version_rejected():
    with pytest.raises(ValueError, match="version"):
        _spec(version="  ").validate()


def test_node_names_single():
    assert _spec().node_names() == ["pg_lab_01"]


def test_node_names_ha():
    spec = _spec(mode="ha-lab", replicas=2)
    assert spec.node_names() == ["pg_lab_01-primary", "pg_lab_01-standby-1", "pg_lab_01-standby-2"]


def test_ha_replicas_min():
    with pytest.raises(ValueError, match="replicas"):
        _spec(mode="ha-lab", replicas=0).validate(replicas_explicit=True)
