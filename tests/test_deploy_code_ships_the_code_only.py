"""`deploy-code` ships the code and leaves the worker's own configuration where it is.

0.28.0 was soaked on the worker, and the tool had no command that ships an image alone: `copy` and
`deploy` carry the master's `data/` and `assets/`, and on 2026-10-09 the master's
`restore_config.json` had the worker's nightly restore switched off - a full deploy would have ended
it without a word (0.28.0 sheet §3.3 T1, 0.29.0 item 1.118). It was done by hand. These tests hold
the written-down run to what made the hand run safe:

- without `--apply` it reads the worker and changes nothing - not even a build;
- the compose file keeps every line that is the worker's: only the tool's image, and the names the
  0.29.0 rename moved, change - never a host folder or a docker network;
- the way back is kept before anything is written;
- the passphrase never reaches an argv, and not the one script line the remote gate echoes.
"""

from __future__ import annotations

import pytest

from dbabrain.control import deploy_code

COMPOSE = """# a worker's compose file
services:
  dbabrain:
    # the soak's candidate
    image: db_ops:2.87.37
    container_name: dbabrain
    environment:
      DB_OPS_CONFIG: config.json
      DB_OPS_NODE_ROLE: worker
      #   DB_OPS_SECRET_KEY="<passphrase>" docker compose up -d
      DB_OPS_SECRET_KEY: "${DB_OPS_SECRET_KEY:?pass the passphrase}"
    volumes:
      - ./data:/app/tools/db_ops/data
      - ./assets:/app/tools/db_ops/assets
      - /opt/db_ops/containers:/opt/db_ops/containers
  sidecar:
    image: postgres:18
networks:
  default:
    name: db_ops_default
    external: true
"""


def test_the_image_line_and_the_renamed_names_change_and_nothing_else() -> None:
    plan = deploy_code.plan_compose(COMPOSE, "dbabrain:2.88.0")

    assert plan.previous_image == "db_ops:2.87.37"
    assert "    image: dbabrain:2.88.0\n" in plan.text
    assert "DBABRAIN_NODE_ROLE: worker" in plan.text
    assert '"${DBABRAIN_SECRET_KEY:?pass the passphrase}"' in plan.text
    assert "./data:/app/tools/dbabrain/data" in plan.text
    for kept in ("/opt/db_ops/containers:/opt/db_ops/containers", "name: db_ops_default",
                 "image: postgres:18"):
        assert kept in plan.text, f"{kept} is the host's or another service's"
    assert len(plan.text.splitlines()) == len(COMPOSE.splitlines())


def test_a_second_plan_on_the_new_file_changes_only_the_image() -> None:
    once = deploy_code.plan_compose(COMPOSE, "dbabrain:2.88.0")
    again = deploy_code.plan_compose(once.text, "dbabrain:2.88.1")

    assert again.changes == [("    image: dbabrain:2.88.0", "    image: dbabrain:2.88.1")]


@pytest.mark.parametrize("image_line", ["    image: ghcr.io/acme/dbabrain:0.28.0",
                                        "    image: dbabrain:latest"])
def test_the_published_image_and_the_new_name_are_the_tools_too(image_line) -> None:
    text = COMPOSE.replace("    image: db_ops:2.87.37", image_line)
    assert deploy_code.plan_compose(text, "dbabrain:2.88.0").previous_image == image_line.split()[-1]


def test_a_file_with_no_tool_image_or_two_is_refused() -> None:
    with pytest.raises(deploy_code.DeployCodeError, match="0 image line"):
        deploy_code.plan_compose(COMPOSE.replace("image: db_ops:2.87.37", "image: nginx:1"), "x:1")
    doubled = COMPOSE.replace("image: postgres:18", "image: dbabrain:old")
    with pytest.raises(deploy_code.DeployCodeError, match="2 image line"):
        deploy_code.plan_compose(doubled, "x:1")


def test_the_service_is_read_from_the_file() -> None:
    assert deploy_code.compose_service(COMPOSE) == "dbabrain"


def test_the_passphrase_is_on_no_argv_and_not_on_the_echoed_line() -> None:
    script = deploy_code.keyed_script("/opt/dbabrain", "Pa$$#word^1", ["docker compose up -d"])
    first, *rest = script.splitlines()

    assert first.startswith("cd ") and "Pa$$" not in first
    assert "Pa$$#word^1" not in script, "the key travels encoded, decoded by a builtin"
    assert any("printf %s" in line and "base64 -d" in line for line in rest)


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def put(self, local, remote):
        self.calls.append(("put", remote))

    def put_bytes(self, content, remote):
        self.calls.append(("write", remote))

    def run_script(self, script):
        self.calls.append(("script", script))
        return type("R", (), {"ok": True, "stdout": "", "stderr": "", "exit_code": 0})()

    def close(self):
        self.calls.append(("close", ""))


@pytest.fixture
def worker(monkeypatch):
    client = _Client()
    commands: list[str] = []
    built: list[list[str]] = []
    monkeypatch.setattr(deploy_code, "ssh_connect", lambda *a, **k: client)
    monkeypatch.setattr(deploy_code, "read_version", lambda: "2.88.0")
    monkeypatch.setattr(deploy_code, "require_docker", lambda: None)
    monkeypatch.setattr(deploy_code, "run_local", lambda cmd, cwd=None: built.append(cmd))
    monkeypatch.setattr(deploy_code, "_local_image_id", lambda tag: "sha256:abc")

    def capture(_client, command):
        commands.append(command)
        if command.startswith("cat "):
            return 0, COMPOSE, ""
        if "image inspect" in command:
            return 0, "sha256:abc\n", ""
        return 0, "0 running dbabrain:2.88.0\n", ""

    def run(_client, command, **_kwargs):
        commands.append(command)
        return 0

    monkeypatch.setattr(deploy_code, "ssh_capture", capture)
    monkeypatch.setattr(deploy_code, "ssh_run", run)
    return client, commands, built


def _deploy(**kwargs):
    return deploy_code.deploy_code(host="192.0.2.10", user="ops", password="pw", secret_key="k3y",
                                   **kwargs)


def test_without_apply_nothing_is_built_sent_or_written(worker) -> None:
    client, commands, built = worker
    answer = _deploy()

    assert answer["applied"] is False and answer["image"] == "dbabrain:2.88.0"
    assert built == []
    assert [kind for kind, _ in client.calls] == ["close"]
    assert commands == ["cat /opt/dbabrain/docker-compose.yml"]


def test_with_apply_the_way_back_is_kept_before_the_compose_file_is_written(worker) -> None:
    client, commands, built = worker
    answer = _deploy(apply=True)

    assert answer["applied"] is True
    assert built[0][:2] == ["docker", "build"] and built[1][:2] == ["docker", "save"]
    kinds = [kind for kind, _ in client.calls]
    assert kinds == ["put", "write", "script", "close"]
    backup = next(command for command in commands if "pre_deploy_2.88.0_" in command)
    assert "docker-compose.yml.before" in backup and "data.tgz data assets config.json" in backup
    assert commands.index(backup) < len(commands) - 2, "kept before the switch"


def test_the_switch_upgrades_the_configuration_in_the_new_image_before_it_starts(worker) -> None:
    client, _commands, _built = worker
    _deploy(apply=True)

    script = next(text for kind, text in client.calls if kind == "script")
    order = [line for line in script.splitlines()
             if line.startswith("docker compose") and "export" not in line]
    run = "docker compose run --rm --no-deps -T dbabrain dbabrain"
    assert order == [
        "docker compose stop -t 120",
        "docker compose rm -f",
        f"{run} common upgrade-config '{{}}'",
        f"{run} common upgrade-config '{{\"dry_run\": false}}'",
        f"{run} db --config config.json sync-config '{{}}'",
        "docker compose up -d",
    ]
    assert "k3y" not in script


def test_an_image_that_did_not_load_as_built_stops_the_run(worker, monkeypatch) -> None:
    client, _commands, _built = worker
    monkeypatch.setattr(deploy_code, "ssh_capture", lambda _c, command: (
        (0, COMPOSE, "") if command.startswith("cat ") else (0, "sha256:other\n", "")))

    with pytest.raises(deploy_code.DeployCodeError, match="not the one built"):
        _deploy(apply=True)
    assert "write" not in [kind for kind, _ in client.calls], "nothing written after a bad load"
