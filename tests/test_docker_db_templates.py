from __future__ import annotations

import pytest
import yaml

from db_ops.sre.docker_db import compose as compose_mod
from db_ops.sre.docker_db import provisioner, templates
from db_ops.sre.docker_db.models import DockerDbSpec

ALL_CASES = [
    ("pg", "postgres", "16", "single", 2, 5433),
    ("my", "mysql", "8.4", "single", 2, 3307),
    ("ms", "mssql", "2022-latest", "single", 2, 14333),
    ("pgha", "postgres", "16", "ha-lab", 2, 5433),
    ("myha", "mysql", "8.4", "ha-lab", 3, 3307),
]


def _spec(name, engine, version, mode, replicas, port):
    return DockerDbSpec(name=name, engine=engine, version=version, mode=mode,
                        replicas=replicas, host_port=port, password_env="PW")


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #
def test_render_scalar_and_dotted():
    out = templates.render("a={{ x }} b={{ o.k }}", {"x": 1, "o": {"k": "v"}})
    assert out == "a=1 b=v"


def test_render_for_loop_with_loop_index():
    out = templates.render(
        "{% for n in items %}{{ loop.index }}:{{ n.k }};{% endfor %}",
        {"items": [{"k": "a"}, {"k": "b"}]},
    )
    assert out == "1:a;2:b;"


def test_render_missing_var_is_empty():
    assert templates.render("x={{ nope }}", {}) == "x="


# --------------------------------------------------------------------------- #
# Compose generation for every engine/mode
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,engine,version,mode,replicas,port", ALL_CASES)
def test_templates_render_valid_yaml(name, engine, version, mode, replicas, port):
    spec = _spec(name, engine, version, mode, replicas, port)
    text = templates.render(templates.load_template(engine, mode), templates.build_context(spec))
    doc = yaml.safe_load(text)
    assert "services" in doc and doc["services"]


@pytest.mark.parametrize("name,engine,version,mode,replicas,port", ALL_CASES)
def test_password_never_inlined(name, engine, version, mode, replicas, port):
    spec = _spec(name, engine, version, mode, replicas, port)
    text = templates.render(templates.load_template(engine, mode), templates.build_context(spec))
    # The password is only referenced via ${DB_PASSWORD}; no literal secret appears.
    assert "${DB_PASSWORD}" in text
    assert "PW" not in text  # the password_env name must not leak into the compose


def test_ha_services_and_ports_increment():
    spec = _spec("pgha", "postgres", "16", "ha-lab", 2, 5433)
    doc = yaml.safe_load(templates.render(templates.load_template("postgres", "ha-lab"),
                                          templates.build_context(spec)))
    names = list(doc["services"])
    assert names == ["pgha-primary", "pgha-standby-1", "pgha-standby-2"]
    ports = [doc["services"][n]["ports"][0] for n in names]
    assert ports == ["5433:5432", "5434:5432", "5435:5432"]


def test_postgres_ha_uses_official_image_and_init_script():
    spec = _spec("pgha", "postgres", "18", "ha-lab", 2, 5433)
    doc = yaml.safe_load(templates.render(templates.load_template("postgres", "ha-lab"),
                                          templates.build_context(spec)))
    # Official image (no third-party registry that may 404 on new majors).
    assert doc["services"]["pgha-primary"]["image"] == "postgres:18"
    assert all(v["image"] == "postgres:18" for v in doc["services"].values())
    # Standbys wait for the primary to be healthy before seeding.
    assert "pgha-primary" in doc["services"]["pgha-standby-1"]["depends_on"]
    # The replication bootstrap script travels with the instance.
    plan = compose_mod.build_plan(spec, containers_dir="/opt/db_ops/containers", password="x")
    assert "initdb/10-replication.sh" in [f.relpath for f in plan.files]


def test_postgres_uses_named_volume_at_parent_dir():
    # Data must be a Docker NAMED volume (not a ./bind mount, which inherits the host
    # uid and breaks postgres uid 999) mounted at the PARENT dir (postgres 18 rejects
    # a mount at .../data). Guard both single and HA.
    single = yaml.safe_load(templates.render(templates.load_template("postgres", "single"),
                                             templates.build_context(_spec("pg", "postgres", "18", "single", 2, 5433))))
    vols = single["services"]["pg"]["volumes"]
    assert "pg_data:/var/lib/postgresql" in vols
    assert not any(v.startswith("./") and "postgresql" in v for v in vols)  # no host bind mount for data
    assert "pg_data" in (single.get("volumes") or {})

    ha = yaml.safe_load(templates.render(templates.load_template("postgres", "ha-lab"),
                                         templates.build_context(_spec("pgha", "postgres", "18", "ha-lab", 2, 5433))))
    for name, svc in ha["services"].items():
        data_mounts = [v for v in svc["volumes"] if v.endswith(":/var/lib/postgresql")]
        assert data_mounts and not data_mounts[0].startswith("./")
        assert not any(v.endswith(":/var/lib/postgresql/data") for v in svc["volumes"])
    # Every node's named volume is declared at the top level.
    assert set(ha.get("volumes") or {}) == {f"{n}_data" for n in ["pgha-primary", "pgha-standby-1", "pgha-standby-2"]}


def test_compose_runtime_vars_are_escaped():
    # Runtime shell vars must reach the container as $VAR, so the template must
    # write $$VAR (compose collapses $$ -> $). A bare $VAR would be eaten by compose.
    spec = _spec("pgha", "postgres", "18", "ha-lab", 2, 5433)
    text = templates.render(templates.load_template("postgres", "ha-lab"),
                            templates.build_context(spec))
    assert "$$PGDATA" in text and "$$PRIMARY_HOST" in text


def test_image_uses_version_tag():
    spec = _spec("pg", "postgres", "16", "single", 2, 5433)
    doc = yaml.safe_load(templates.render(templates.load_template("postgres", "single"),
                                          templates.build_context(spec)))
    assert doc["services"]["pg"]["image"] == "postgres:16"


# --------------------------------------------------------------------------- #
# Plan / .env
# --------------------------------------------------------------------------- #
def test_dry_run_env_masks_password():
    spec = _spec("pg", "postgres", "16", "single", 2, 5433)
    plan = compose_mod.build_plan(spec, containers_dir="/opt/db_ops/containers",
                                  password=None, dry_run=True)
    env = next(f for f in plan.files if f.relpath == ".env")
    assert "DB_PASSWORD=***" in env.content
    assert env.secret is False


def test_real_env_carries_password_and_is_marked_secret():
    spec = _spec("pg", "postgres", "16", "single", 2, 5433)
    plan = compose_mod.build_plan(spec, containers_dir="/opt/db_ops/containers",
                                  password="s3cret", dry_run=False)
    env = next(f for f in plan.files if f.relpath == ".env")
    assert "DB_PASSWORD=s3cret" in env.content
    assert env.secret is True


def test_plan_paths_and_connection():
    spec = _spec("pg_lab_01", "postgres", "16", "single", 2, 5433)
    plan = compose_mod.build_plan(spec, containers_dir="/opt/db_ops/containers",
                                  password="x", worker_host="10.0.0.1")
    assert plan.instance_dir == "/opt/db_ops/containers/pg_lab_01"
    assert plan.compose_path.endswith("/pg_lab_01/docker-compose.yml")
    assert plan.connection["connect"] == "psql -h 10.0.0.1 -p 5433 -U postgres -d postgres"
    assert plan.up_command == ["docker", "compose", "--env-file", ".env", "up", "-d"]


def test_ha_summary_includes_warning():
    spec = _spec("pgha", "postgres", "16", "ha-lab", 2, 5433)
    plan = compose_mod.build_plan(spec, containers_dir="/opt/db_ops/containers", password="x")
    summary = provisioner.format_summary(plan, {"pgha-primary": "healthy"}, status_label="running")
    assert "WARNING: ha-lab mode is only HA simulation" in summary
