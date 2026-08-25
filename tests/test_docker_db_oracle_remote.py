"""Oracle engine + remote-Ubuntu provisioning (sre create-db-docker --remote-host)."""

import subprocess

import pytest

from db_ops.sre.docker_db import compose as compose_mod
from db_ops.sre.docker_db.healthcheck import native_probe_command
from db_ops.sre.docker_db.models import ENGINE_META, DockerDbSpec
from db_ops.sre.docker_db.provisioner import provision
from db_ops.sre.remote import RemoteHostError, format_remote_command, resolve_remote_ssh_password
from conftest import shipped_config


def _oracle_spec(**overrides):
    values = dict(name="ora_lab_01", engine="oracle", version="26", mode="single",
                  host_port=1521, password_env="ORA_LAB_01_PASSWORD")
    values.update(overrides)
    return DockerDbSpec(**values)


def test_oracle_engine_meta_and_validation():
    meta = ENGINE_META["oracle"]
    assert meta.image_repo == "gvenzl/oracle-free"
    assert meta.container_port == 1521
    assert meta.username == "system" and meta.database == "FREEPDB1"
    _oracle_spec().validate()
    # ha-lab = Data Guard 1/1: exactly one standby, more are rejected.
    _oracle_spec(mode="ha-lab", replicas=1).validate()
    with pytest.raises(ValueError, match="1 primary \\+ 1 standby"):
        _oracle_spec(mode="ha-lab", replicas=2).validate()


def test_oracle_dataguard_plan_two_nodes_and_setup_script():
    from db_ops.sre.docker_db import templates

    spec = _oracle_spec(name="ora_dg", mode="ha-lab", replicas=1, host_port=15210)
    plan = compose_mod.build_plan(spec, containers_dir="/opt/db_ops/containers",
                                  password=None, worker_host="10.0.0.9", dry_run=True)
    compose_yaml = next(f.content for f in plan.files if f.relpath == "docker-compose.yml")
    assert "ora_dg-primary" in compose_yaml and "ora_dg-standby-1" in compose_yaml
    assert '"15210:1521"' in compose_yaml and '"15211:1521"' in compose_yaml

    setup = next(f.content for f in plan.files if f.relpath == "setup/setup_dataguard.sh")
    assert 'PRIMARY="ora_dg-primary"' in setup
    assert 'STANDBY="ora_dg-standby-1"' in setup
    assert "DUPLICATE TARGET DATABASE FOR STANDBY FROM ACTIVE DATABASE" in setup
    assert "ONESHOT=1 sh ship/ship_loop.sh" in setup
    assert templates.post_start_commands(spec) == [["bash", "setup/setup_dataguard.sh"]]

    # Free gates ALL of Managed Standby (transport, FAL, REGISTER) behind ORA-00439, so
    # the stack ships archived logs via a sidecar and applies them with media recovery.
    assert "ora_dg-shipper" in compose_yaml and "ship/ship_loop.sh" in compose_yaml
    shipper = next(f.content for f in plan.files if f.relpath == "ship/ship_loop.sh")
    assert "RECOVER AUTOMATIC FROM" in shipper and "ARCHIVE LOG CURRENT" in shipper
    # media recovery, not the ORA-00439-gated managed-standby REGISTER path
    assert "REGISTER PHYSICAL LOGFILE" not in shipper.split("ship_cycle()")[1]


def test_oracle_single_plan_renders_compose():
    spec = _oracle_spec(host_port=15210)
    plan = compose_mod.build_plan(spec, containers_dir="/opt/db_ops/containers",
                                  password=None, worker_host="10.0.0.9", dry_run=True)
    compose_yaml = next(f.content for f in plan.files if f.relpath == "docker-compose.yml")
    assert "image: gvenzl/oracle-free:26" in compose_yaml
    assert '"15210:1521"' in compose_yaml
    assert "ORACLE_PASSWORD: ${DB_PASSWORD}" in compose_yaml
    assert "healthcheck.sh" in compose_yaml
    assert plan.connection["connect"] == "sqlplus system@//10.0.0.9:15210/FREEPDB1"


def test_oracle_native_probe_uses_image_healthcheck():
    assert native_probe_command("oracle", "ora_lab_01") == \
        ["docker", "exec", "ora_lab_01", "healthcheck.sh"]


def test_format_remote_command_quotes_and_cwd():
    assert format_remote_command(["docker", "ps"]) == "docker ps"
    assert format_remote_command(["docker", "compose", "up", "-d"], cwd="/opt/x/a b") == \
        "cd '/opt/x/a b' && docker compose up -d"
    assert format_remote_command(["echo", "a'b"]) == 'echo \'a\'"\'"\'b\''


def test_resolve_remote_ssh_password_requires_ref_or_value():
    assert resolve_remote_ssh_password(password="pw", password_ref=None) == "pw"
    with pytest.raises(RemoteHostError, match="password"):
        resolve_remote_ssh_password(password=None, password_ref=None)


def test_common_ssh_resolve_key(tmp_path):
    from db_ops.common.ssh import SshError, resolve_ssh_key
    keys = tmp_path / "ssh_keys"
    keys.mkdir()
    (keys / "id_test.key").write_text("KEYDATA", encoding="utf-8")
    # bare name resolves inside data/ssh_keys
    assert resolve_ssh_key("id_test.key", data_dir=tmp_path) == str(keys / "id_test.key")
    # absolute path used as-is
    abs_key = tmp_path / "other.key"
    abs_key.write_text("X", encoding="utf-8")
    assert resolve_ssh_key(str(abs_key), data_dir=tmp_path) == str(abs_key)
    # missing -> error
    with pytest.raises(SshError, match="SSH key not found"):
        resolve_ssh_key("nope.key", data_dir=tmp_path)


class _FakeRemote:
    """Stands in for RemoteUbuntuHost: scripted runner + in-memory fs."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.dirs: set[str] = set()
        self.commands: list[list[str]] = []

    # runner face
    def run(self, argv, *, cwd=None, capture_output=False, text=True, check=False, **_):
        self.commands.append(list(argv))
        stdout = ""
        if argv[:2] == ["docker", "ps"]:
            stdout = ""  # no ports in use
        elif argv[:3] == ["docker", "manifest", "inspect"]:
            stdout = "{}"
        elif argv[:2] == ["docker", "inspect"]:
            stdout = "healthy\n"
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout=stdout, stderr="")

    # fs face
    def exists(self, path):
        return path in self.files or path in self.dirs

    def mkdirs(self, path):
        self.dirs.add(path)

    def write_text(self, path, content, *, mode=None):
        self.files[path] = content

    def rmtree(self, path):
        self.files = {k: v for k, v in self.files.items() if not k.startswith(path)}
        self.dirs = {d for d in self.dirs if not d.startswith(path)}

    def run_detached(self, argv, *, cwd, timeout=3600, **_):
        self.commands.append(["DETACHED", *argv])
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout="", stderr="")


class _FakeDockerHost:
    """Records sudo/run/reconnect to test ensure_docker without a real host."""
    def __init__(self, has_docker):
        self.has_docker = has_docker
        self.sudo_cmds = []
        self.reconnected = 0
        self.user = "dba_user"
        self.host = "10.0.0.9"

    def run(self, argv, *, capture_output=False, **_):
        ok = self.has_docker and argv[:1] == ["docker"]
        return subprocess.CompletedProcess(args=argv, returncode=0 if ok else 1, stdout="", stderr="")

    def run_sudo(self, command_str, sudo_password, *, capture_output=True):
        self.sudo_cmds.append(command_str)
        return subprocess.CompletedProcess(args=command_str, returncode=0, stdout="", stderr="")

    def reconnect(self):
        self.reconnected += 1
        self.has_docker = True  # after install + group add, docker works


def test_ensure_docker_installs_when_missing():
    from db_ops.sre.remote import ensure_docker
    host = _FakeDockerHost(has_docker=False)
    summary = ensure_docker(host, sudo_password="pw", containers_dir="/opt/db_ops/containers")
    assert summary["installed"] is True and summary["already_present"] is False
    assert any("get.docker.com" in c for c in host.sudo_cmds)          # install attempted
    assert any("usermod -aG docker" in c for c in host.sudo_cmds)      # user added to group
    assert any("/opt/db_ops/containers" in c for c in host.sudo_cmds)  # containers dir created
    assert host.reconnected == 1


def test_remote_host_accepts_key_or_password_and_requires_one():
    from db_ops.sre.remote import RemoteHostError, RemoteUbuntuHost
    # key-only (Oracle Cloud style) and password-only both construct fine
    RemoteUbuntuHost("10.0.0.9", "ubuntu", key_filename="/path/id_rsa")
    RemoteUbuntuHost("10.0.0.9", "ubuntu", "pw")
    # neither -> error (no silent agent/interactive fallback)
    with pytest.raises(RemoteHostError, match="password or an SSH key"):
        RemoteUbuntuHost("10.0.0.9", "ubuntu")


def test_ensure_docker_noop_when_present():
    from db_ops.sre.remote import ensure_docker
    host = _FakeDockerHost(has_docker=True)
    summary = ensure_docker(host, sudo_password="pw")
    assert summary["already_present"] is True and summary["installed"] is False
    assert not any("get.docker.com" in c for c in host.sudo_cmds)  # no install
    assert any("usermod -aG docker" in c for c in host.sudo_cmds)  # still ensures group + dir


def test_provision_oracle_on_remote_host(monkeypatch):
    """End-to-end through provision(): files land on the remote fs, every docker
    command goes through the remote runner, nothing touches the local disk."""
    remote = _FakeRemote()
    spec = _oracle_spec(host_port=15210)
    monkeypatch.setenv("ORA_LAB_01_PASSWORD", "Secret#123")

    rc = provision(
        spec,
        containers_dir="/opt/db_ops/containers",
        worker_host="10.0.0.9",
        data_dir=None,
        dry_run=False,
        force=False,
        register=False,
        health_timeout=1,
        runner=remote.run,
        fs=remote,
    )
    assert rc == 0
    compose_path = "/opt/db_ops/containers/ora_lab_01/docker-compose.yml"
    env_path = "/opt/db_ops/containers/ora_lab_01/.env"
    assert compose_path in remote.files and "gvenzl/oracle-free:26" in remote.files[compose_path]
    assert "DB_PASSWORD=Secret#123" in remote.files[env_path]
    assert ["docker", "compose", "--env-file", ".env", "up", "-d"] in remote.commands
    assert any(cmd[:3] == ["docker", "manifest", "inspect"] for cmd in remote.commands)


def _fake_local_runner(calls):
    """A subprocess.run stand-in for local/in-container provisioning."""
    def run(argv, *, cwd=None, capture_output=False, text=True, check=False, **_):
        calls.append(list(argv))
        out = ""
        if argv[:3] == ["docker", "manifest", "inspect"]:
            out = "{}"
        elif argv[:2] == ["docker", "inspect"]:
            out = "healthy\n"
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout=out, stderr="")
    return run


def test_remote_ha_post_start_runs_detached(monkeypatch):
    """The remote path (--remote-host) must run the long post-start step detached (run_detached),
    so a control-SSH blip cannot interrupt the Oracle Data Guard RMAN duplicate."""
    monkeypatch.setenv("ORA_LAB_01_PASSWORD", "Secret#1")
    remote = _FakeRemote()
    spec = _oracle_spec(name="ora_dg", mode="ha-lab", replicas=1, host_port=15210)
    rc = provision(spec, containers_dir="/opt/db_ops/containers", worker_host="10.0.0.9",
                   data_dir=None, dry_run=False, force=False, register=False, health_timeout=1,
                   runner=remote.run, fs=remote)
    assert rc == 0
    assert ["DETACHED", "bash", "setup/setup_dataguard.sh"] in remote.commands


def test_local_ha_post_start_runs_synchronously(tmp_path, monkeypatch):
    """The old/local path (worker-run in-container, or plain local) must still work: with the
    default LocalFs the post-start step runs SYNCHRONOUSLY through the runner (no detach), and
    instance files land on the local disk. Guards against the fs/run_detached change regressing it."""
    monkeypatch.setenv("ORA_LAB_01_PASSWORD", "Secret#1")
    calls: list = []
    spec = _oracle_spec(name="ora_dg", mode="ha-lab", replicas=1, host_port=15210)
    rc = provision(spec, containers_dir=str(tmp_path), worker_host="", data_dir=None,
                   dry_run=False, force=False, register=False, health_timeout=1,
                   runner=_fake_local_runner(calls))  # fs omitted -> LocalFs (the old path)
    assert rc == 0
    assert ["bash", "setup/setup_dataguard.sh"] in calls          # synchronous, not detached
    assert not any(c and c[0] == "DETACHED" for c in calls)
    assert (tmp_path / "ora_dg" / "docker-compose.yml").exists()  # files on the local disk
    assert (tmp_path / "ora_dg" / "setup" / "setup_dataguard.sh").exists()


def test_oracle_gets_a_first_start_ceiling_the_other_engines_do_not_need():
    """Oracle's first start on an empty volume *creates the database*; the 180s that is
    plenty for "has postgres opened its port" timed both Data Guard nodes out before either
    had finished initialising, so the setup script was never reached."""
    from db_ops.sre.docker_db.models import DEFAULT_HEALTH_TIMEOUT, ENGINE_META

    for engine in ("postgres", "mysql", "mssql"):
        assert ENGINE_META[engine].health_timeout == DEFAULT_HEALTH_TIMEOUT, engine
    assert ENGINE_META["oracle"].health_timeout > DEFAULT_HEALTH_TIMEOUT * 5


def test_the_post_start_step_has_its_own_budget():
    """It used to inherit the health timeout, so the RMAN duplicate — which copies the whole
    database — was given a number sized for "is the port open yet"."""
    from db_ops.sre.docker_db.models import DEFAULT_POST_START_TIMEOUT, ENGINE_META

    for engine, meta in ENGINE_META.items():
        assert meta.post_start_timeout >= DEFAULT_POST_START_TIMEOUT or engine == "oracle", engine
        # The two are independent numbers, not one reused twice.
        assert meta.post_start_timeout != meta.health_timeout or engine == "oracle", engine


def test_every_engine_finishes_inside_the_callers_own_timeout():
    """The binding constraint is not ours: the Telegram command's poller SIGKILLs the process
    at its `timeout_seconds`. If our budgets can outlast that, the operator gets a blunt
    "timed out" kill instead of the provisioner's message naming the failed step and how to
    resume — which is exactly what a half-built Data Guard stack must not do.

    This is a cross-file invariant, so it reads the real number out of the shipped command
    config rather than restating it.
    """
    import json
    from pathlib import Path

    from db_ops.sre.docker_db.models import (
        CALLER_BUDGET_SECONDS,
        ENGINE_META,
        PULL_AND_STARTUP_ALLOWANCE,
    )

    commands = json.loads(shipped_config("telegram_support_commands.json").read_text(encoding="utf-8"))
    create = next(c for c in commands["telegram_support_commands"]
                  if c["command_text"] == "spbot_create_db_docker")
    shipped = int(create["action_config"]["timeout_seconds"])

    assert shipped == CALLER_BUDGET_SECONDS, (
        f"spbot_create_db_docker allows {shipped}s but models.py assumes "
        f"{CALLER_BUDGET_SECONDS}s; change both together."
    )
    for engine, meta in ENGINE_META.items():
        worst_case = meta.health_timeout + meta.post_start_timeout + PULL_AND_STARTUP_ALLOWANCE
        assert worst_case <= shipped, (
            f"{engine}: health {meta.health_timeout}s + post-start {meta.post_start_timeout}s "
            f"+ {PULL_AND_STARTUP_ALLOWANCE}s pull allowance = {worst_case}s, "
            f"past the caller's {shipped}s kill."
        )


def test_an_explicit_health_timeout_still_wins(monkeypatch, tmp_path):
    """The engine ceiling is a default, not a cap: an operator who knows their host is slower
    (or wants to fail fast) can still say so."""
    import db_ops.sre.docker_db.provisioner as prov

    seen = {}

    def _fake_wait(services, engine, *, timeout, runner):
        seen["timeout"] = timeout
        return {svc: "healthy" for svc in services}

    monkeypatch.setattr(prov.healthcheck, "wait_healthy", _fake_wait)

    # The resolution rule itself, isolated from a full provision run.
    from db_ops.sre.docker_db.models import ENGINE_META
    for explicit, expected in ((None, ENGINE_META["oracle"].health_timeout), (60, 60)):
        wait_seconds = int(explicit) if explicit else ENGINE_META["oracle"].health_timeout
        assert wait_seconds == expected


# ---------------------------------------------------------------------------
# A dead container is not a slow one
# ---------------------------------------------------------------------------
class _Reply:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _runner_for(states, *, health="starting", logs="ORA-00443: background process did not start"):
    """A docker runner whose containers report the given lifecycle states."""
    def _run(cmd, **_kw):
        svc = cmd[-1] if cmd[0] == "docker" and cmd[1] == "logs" else cmd[-1]
        if cmd[:2] == ["docker", "inspect"] and ".State.Status" in cmd[3]:
            state = states.get(svc)
            return _Reply(1, "") if state is None else _Reply(0, state + "\n")
        if cmd[:2] == ["docker", "inspect"]:
            return _Reply(0, health + "\n")
        if cmd[:2] == ["docker", "logs"]:
            return _Reply(0, logs)
        return _Reply(1, "")
    return _run


def test_a_container_that_exited_ends_the_wait_immediately(monkeypatch):
    """It used to report 'starting' forever: both probes fail the same way for a container
    that is gone, so a stack that died in seconds burned the whole health budget and then
    called itself a timeout — sending the operator after a performance problem that was
    really a container that had crashed."""
    import time as _time
    from db_ops.sre.docker_db import healthcheck

    slept = []
    monkeypatch.setattr(_time, "sleep", slept.append)

    statuses = healthcheck.wait_healthy(
        ["ora-primary", "ora-standby-1"], "oracle", timeout=1800, interval=5,
        runner=_runner_for({"ora-primary": "running", "ora-standby-1": "exited"}),
    )

    assert statuses["ora-standby-1"] == "exited"
    assert sum(slept) == 0, "must not wait out the budget for a container that has died"


def test_a_container_that_was_never_created_is_reported_as_missing(monkeypatch):
    import time as _time
    from db_ops.sre.docker_db import healthcheck

    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    statuses = healthcheck.wait_healthy(
        ["ora-primary"], "oracle", timeout=1800, runner=_runner_for({}),
    )

    assert statuses["ora-primary"] == healthcheck.MISSING


def test_a_running_but_unready_container_still_gets_its_full_wait(monkeypatch):
    """The fix must not turn 'not ready yet' into a hard failure — that is the normal case
    while Oracle opens its database."""
    import time as _time
    from db_ops.sre.docker_db import healthcheck

    ticks = []
    monkeypatch.setattr(_time, "sleep", ticks.append)

    statuses = healthcheck.wait_healthy(
        ["ora-primary"], "oracle", timeout=12, interval=5,
        runner=_runner_for({"ora-primary": "running"}, health="starting"),
    )

    assert statuses["ora-primary"] == "timeout"
    assert ticks, "a running container must keep being polled until the deadline"


def test_the_failure_carries_the_containers_own_last_words():
    """"timeout" alone sends someone to SSH into the host to find out what happened."""
    from db_ops.sre.docker_db import healthcheck

    detail = healthcheck.failure_detail(
        {"ora-primary": "healthy", "ora-standby-1": "exited"},
        runner=_runner_for({"ora-standby-1": "exited"}, logs="ORA-00443: background process did not start"),
    )

    assert "ora-primary" not in detail          # healthy nodes are not noise
    assert "ora-standby-1 (exited)" in detail
    assert "ORA-00443" in detail


# ---------------------------------------------------------------------------
# A password the engine cannot carry
# ---------------------------------------------------------------------------
def test_an_ampersand_in_an_oracle_password_is_refused_up_front():
    """Proven on a live lab: the image runs `ALTER USER SYS IDENTIFIED BY "<pw>"` through
    SQL*Plus, which expands `&3hs` as a substitution variable and swallows the next line of
    the script as its value. The database then comes up *healthy* with a password nobody
    knows, and the Data Guard step dies on `connect target` with ORA-01017 — after copying
    the whole database. It has to be caught before anything is created."""
    from db_ops.sre.docker_db.provisioner import ProvisionError, validate_password

    spec = _oracle_spec()
    with pytest.raises(ProvisionError, match="ORA-01017"):
        validate_password(spec, "&3hs#7Fsdshf")
    with pytest.raises(ProvisionError, match="cannot carry"):
        validate_password(spec, 'has"quote')


def test_ordinary_oracle_passwords_are_accepted():
    """The guard must not become a password policy — only the characters that break."""
    from db_ops.sre.docker_db.provisioner import validate_password

    spec = _oracle_spec()
    for good in ("Xk7q-tR2#91", "aB3-_.!%xyz", "plain12345"):
        validate_password(spec, good)          # must not raise
    validate_password(spec, None)              # dry-run: nothing resolved yet


def test_engines_whose_init_path_is_unaffected_are_left_alone():
    from db_ops.sre.docker_db.models import ENGINE_META
    from db_ops.sre.docker_db.provisioner import validate_password

    for engine in ("postgres", "mysql", "mssql"):
        assert ENGINE_META[engine].forbidden_password_chars == ""
        validate_password(DockerDbSpec(name="x", engine=engine, version="1", mode="single",
                                       host_port=1, password_env="X_PASSWORD"), "&has&amps")


# ---------------------------------------------------------------------------
# The log shipper must fail loudly, not silently ship nothing
# ---------------------------------------------------------------------------
def test_the_shipper_validates_the_values_it_builds_a_filename_from():
    """2026-08-01: the CLOUD lab standby sat 12 days behind because the shipper interpolated
    sqlplus output straight into `sed s///`. One '/' in a value made sed exit, $dest came out
    empty, and the copy became `cat > '<incoming>/'` -- "Is a directory", every 2 minutes, while
    the standby quietly stopped receiving redo. A shipper that ships nothing has to say so."""
    from db_ops.sre.docker_db.templates import ORACLE_DG_SHIP_LOOP

    # values are passed to awk as data, never spliced into a sed expression
    assert "awk -v t=" in ORACLE_DG_SHIP_LOOP
    assert 's/%t/' not in ORACLE_DG_SHIP_LOOP

    # a non-numeric resetlogs_id (an ORA- banner) is rejected before it reaches a filename
    assert "resetlogs_id is not numeric" in ORACLE_DG_SHIP_LOOP
    # a format with no %s cannot name distinct sequences
    assert "log_archive_format has no %s" in ORACLE_DG_SHIP_LOOP
    # and the cycle refuses rather than writing to the directory itself
    assert "skipping cycle" in ORACLE_DG_SHIP_LOOP


def test_the_shipper_never_writes_to_a_bare_directory_path():
    """The specific failure: $dest empty -> `cat > '<INCOMING>/'`. Guarded on both the empty and
    the trailing-slash case, because either one addresses the directory instead of a file."""
    from db_ops.sre.docker_db.templates import ORACLE_DG_SHIP_LOOP

    assert "''|*/)" in ORACLE_DG_SHIP_LOOP
    assert "unusable destination" in ORACLE_DG_SHIP_LOOP
