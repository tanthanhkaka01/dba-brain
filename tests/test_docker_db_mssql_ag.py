"""SQL Server Always On lab (mode=ha-lab).

Compose cannot build an availability group and the SQL Server image has no init hook: the AG
spans the containers and its steps are ordered (enable Always On -> certificate -> mirroring
endpoints -> create the group -> join -> seed a database). So the compose file only starts the
nodes, and a generated script builds the group after they are healthy.

It is an AG with CLUSTER_TYPE = NONE — there is no WSFC or Pacemaker in a container lab — so
failover is manual and there is no listener. These tests pin that down, and that no password
is ever written into a file or an argument.
"""

import subprocess
import types

import pytest
import yaml

from db_ops.sre.docker_db import compose as compose_mod
from db_ops.sre.docker_db import provisioner, templates
from db_ops.sre.docker_db.models import DockerDbSpec
from db_ops.sre.docker_db.provisioner import ProvisionError


def _spec(name="mssql_ag_lab", replicas=2, host_port=15433):
    return DockerDbSpec(name=name, engine="mssql", version="2022-latest", mode="ha-lab",
                        replicas=replicas, host_port=host_port, password_env="MSSQL_AG_LAB_PASSWORD")


def _ag_script(spec):
    files = dict(templates.extra_files(spec))
    return files["setup/setup_ag.sh"]


def test_compose_starts_every_node_with_always_on_enabled_and_its_own_port():
    spec = _spec()
    doc = yaml.safe_load(templates.render(templates.load_template("mssql", "ha-lab"),
                                          templates.build_context(spec)))
    services = doc["services"]

    assert list(services) == ["mssql_ag_lab-primary", "mssql_ag_lab-standby-1", "mssql_ag_lab-standby-2"]
    ports = [svc["ports"][0] for svc in services.values()]
    assert ports == ["15433:1433", "15434:1433", "15435:1433"]  # all reachable on one worker
    for service in services.values():
        # Without this the engine starts without the feature and CREATE AVAILABILITY GROUP fails.
        assert service["environment"]["MSSQL_ENABLE_HADR"] == "1"
        assert service["environment"]["MSSQL_SA_PASSWORD"] == "${DB_PASSWORD}"  # from .env, not inline


def test_the_ag_script_creates_a_cluster_type_none_group_over_the_real_nodes():
    script = _ag_script(_spec())

    assert 'NODES=("mssql_ag_lab-primary" "mssql_ag_lab-standby-1" "mssql_ag_lab-standby-2")' in script
    assert 'SECONDARIES=("mssql_ag_lab-standby-1" "mssql_ag_lab-standby-2")' in script
    assert "CREATE AVAILABILITY GROUP [$AG_NAME]" in script
    assert "WITH (CLUSTER_TYPE = NONE)" in script
    assert "JOIN WITH (CLUSTER_TYPE = NONE)" in script
    assert "SEEDING_MODE = AUTOMATIC" in script          # secondaries seeded by the engine
    assert "GRANT CREATE ANY DATABASE" in script         # which automatic seeding requires
    assert "FAILOVER_MODE = MANUAL" in script            # no cluster manager -> no auto failover
    assert "hadr_endpoint" in script and "5022" in script


def test_the_ag_script_never_handles_the_password_itself():
    """The password stays in the container's own MSSQL_SA_PASSWORD (compose put it there from
    .env). sqlcmd reads it to log in, and substitutes $(MSSQL_SA_PASSWORD) where the SQL needs
    it — so it is in no argv, no log line and no generated file."""
    script = _ag_script(_spec())

    assert "$(MSSQL_SA_PASSWORD)" in script      # sqlcmd substitution, resolved inside the node
    assert "DB_PASSWORD" not in script           # that name only exists in .env / compose
    # SQL goes in on stdin: as a -Q argument the inner `bash -lc` would run $(...) as a command.
    assert "printf '%s\\nGO\\n' \"$sql\" | docker exec -i" in script


def test_the_provisioner_builds_the_group_after_the_nodes_are_healthy(monkeypatch, tmp_path):
    spec = _spec()
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(list(command))
        # check_ports_free parses `docker ps` output, so a runner must look like a real one.
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provisioner.healthcheck, "wait_healthy",
                        lambda services, engine, **kw: {svc: "healthy" for svc in services})
    monkeypatch.setattr(provisioner, "resolve_password_value", lambda *a, **kw: ("pw", "env:test"))
    monkeypatch.setattr(provisioner.register_config, "register_connection", lambda *a, **kw: "added")

    rc = provisioner.provision(spec, containers_dir=str(tmp_path), data_dir=str(tmp_path),
                               register=False, runner=fake_runner)

    assert rc == 0
    assert ["bash", "setup/setup_ag.sh"] in calls
    # ... and only after the stack is up: the group cannot be created against a dead node.
    up = next(index for index, call in enumerate(calls) if call[:2] == ["docker", "compose"] and "up" in call)
    assert calls.index(["bash", "setup/setup_ag.sh"]) > up


def test_an_unhealthy_stack_does_not_get_an_availability_group(monkeypatch, tmp_path):
    spec = _spec()
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(list(command))
        # check_ports_free parses `docker ps` output, so a runner must look like a real one.
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provisioner.healthcheck, "wait_healthy",
                        lambda services, engine, **kw: {svc: "timeout" for svc in services})
    monkeypatch.setattr(provisioner, "resolve_password_value", lambda *a, **kw: ("pw", "env:test"))

    with pytest.raises(ProvisionError, match="never became healthy"):
        provisioner.provision(spec, containers_dir=str(tmp_path), data_dir=str(tmp_path),
                              register=False, runner=fake_runner)
    assert ["bash", "setup/setup_ag.sh"] not in calls


def test_single_mode_mssql_has_no_post_start_step():
    single = DockerDbSpec(name="mssql_lab", engine="mssql", version="2022-latest", mode="single",
                          host_port=14333, password_env="MSSQL_LAB_PASSWORD")
    assert templates.post_start_commands(single) == []
    assert templates.extra_files(single) == []


# --------------------------------------------------------------------------- #
# A wrong --version, which is what actually happened: "mssql:2025" was pulled for minutes and
# then failed — Microsoft publishes 2025-latest, not a bare 2025.
# --------------------------------------------------------------------------- #
def test_a_tag_that_does_not_exist_fails_before_anything_is_created(monkeypatch, tmp_path):
    spec = DockerDbSpec(name="mssql_lab_ha_01", engine="mssql", version="2025", mode="ha-lab",
                        replicas=2, host_port=15433, password_env="X_PASSWORD")
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(list(command))
        if command[:3] == ["docker", "manifest", "inspect"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="manifest unknown")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provisioner, "resolve_password_value", lambda *a, **kw: ("pw", "env:test"))

    with pytest.raises(ProvisionError) as exc:
        provisioner.provision(spec, containers_dir=str(tmp_path), data_dir=str(tmp_path),
                              register=False, runner=fake_runner)

    assert "Image not found: mcr.microsoft.com/mssql/server:2025" in str(exc.value)
    assert "2025-latest" in str(exc.value)                      # ... and what to use instead
    assert not (tmp_path / "mssql_lab_ha_01").exists()          # nothing written
    assert not any("up" in call for call in calls)              # and nothing pulled


def test_a_failed_start_removes_what_this_run_created(monkeypatch, tmp_path):
    """Otherwise the retry — with the tag fixed — is refused with "instance folder already
    exists, pass --force", after a failure that created nothing that works."""
    spec = _spec()

    def fake_runner(command, **kwargs):
        if command[:2] == ["docker", "compose"] and "up" in command:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="pull failed")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provisioner, "resolve_password_value", lambda *a, **kw: ("pw", "env:test"))

    with pytest.raises(ProvisionError, match="docker compose up"):
        provisioner.provision(spec, containers_dir=str(tmp_path), data_dir=str(tmp_path),
                              register=False, runner=fake_runner)

    assert not (tmp_path / "mssql_ag_lab").exists()


def test_an_instance_that_already_existed_is_never_removed_by_a_failure(monkeypatch, tmp_path):
    """--force on an existing instance that then fails to start must not delete it: rollback
    only undoes what this run created."""
    spec = _spec()
    existing = tmp_path / "mssql_ag_lab"
    existing.mkdir()
    (existing / "docker-compose.yml").write_text("# the instance that was already here\n", encoding="utf-8")

    def fake_runner(command, **kwargs):
        if command[:2] == ["docker", "compose"] and "up" in command:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provisioner, "resolve_password_value", lambda *a, **kw: ("pw", "env:test"))

    with pytest.raises(ProvisionError):
        provisioner.provision(spec, containers_dir=str(tmp_path), data_dir=str(tmp_path),
                              register=False, force=True, runner=fake_runner)

    assert existing.exists()


def test_the_healthcheck_uses_the_env_var_the_container_actually_has():
    """What broke the first real run: the healthcheck logged in with -P "${DB_PASSWORD}", but
    DB_PASSWORD exists only in the .env file compose interpolates from — inside the container
    there is only MSSQL_SA_PASSWORD. So sqlcmd sent an empty password, SQL Server answered
    "Password did not match" every 15 seconds, and the node never became healthy."""
    for mode in ("single", "ha-lab"):
        spec = _spec() if mode == "ha-lab" else DockerDbSpec(
            name="mssql_lab", engine="mssql", version="2022-latest", mode="single",
            host_port=14333, password_env="MSSQL_LAB_PASSWORD")
        doc = yaml.safe_load(templates.render(templates.load_template("mssql", mode),
                                              templates.build_context(spec)))
        for service in doc["services"].values():
            test = service["healthcheck"]["test"][1]
            assert "${MSSQL_SA_PASSWORD}" in test, mode
            assert "DB_PASSWORD" not in test, mode      # the name the container does not have
            # ... and it is the same variable the image itself reads:
            assert service["environment"]["MSSQL_SA_PASSWORD"] == "${DB_PASSWORD}"
