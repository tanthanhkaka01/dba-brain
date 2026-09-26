"""`control worker-create-db-docker` builds through `common` and registers here (rules R42, R10).

Until 0.24.0 it ran `sre.cli create-db-docker` inside the worker container: one app driving
another's CLI, with the secret-store passphrase on that command line, and the record written into
the worker's `data/` for `--pull-config` to fetch back. The operator chose to rewrite it rather than
remove it (2026-09-26). It now asks `common.cli create-db-docker` to build on the worker's host over
SSH - the login and the database password on stdin - and the password and the record land on this
node, which is where configuration starts; the worker has them after the next deploy.
"""

from __future__ import annotations

import json

import pytest

from db_ops.control import worker_data
from db_ops.lib import docker_db_registry as registry
from db_ops.lib.secret_text import set_secret_text
from db_ops.transport import common_cli

KEY = "a-throwaway-passphrase-for-this-test-only"


@pytest.fixture()
def built(monkeypatch):
    """Record every request; answer like `common` does for a successful build."""
    sent: list[tuple[str, dict]] = []

    def fake_run(command, request, **_kwargs):
        sent.append((command, request))
        return {"worker_host": request["remote"]["host"], "compose_path":
                f"/opt/db_ops/containers/{request['name']}/docker-compose.yml",
                "status": "running", "summary": "built", "plan_text": "plan"}

    monkeypatch.setattr(common_cli, "run", fake_run)
    monkeypatch.setenv("DB_OPS_SECRET_KEY", KEY)
    _isolate_the_password_variable(monkeypatch)
    return sent


def _isolate_the_password_variable(monkeypatch):
    """`store_password` also sets the ref in the process environment (the provisioner reads it
    back from there), and the environment wins over the store - so one test's password would be
    the next test's. Setting then deleting it makes monkeypatch restore it to absent afterwards."""
    monkeypatch.setenv("PG_LAB_01_PASSWORD", "")
    monkeypatch.delenv("PG_LAB_01_PASSWORD")


def _create(tmp_path, **overrides):
    kwargs = dict(host="192.0.2.10", user="ops", password="ssh-pw", name="pg_lab_01",
                  engine="postgres", version="16", data_dir=tmp_path)
    return worker_data.create_db_docker_on_worker(**{**kwargs, **overrides})


def test_the_build_is_common_s_on_the_worker_s_host_with_the_login_in_the_request(tmp_path, built):
    assert _create(tmp_path, password_text="db-pw") == 0
    [(command, request)] = built
    assert command == "create-db-docker"
    assert request["remote"] == {"host": "192.0.2.10", "port": 22, "username": "ops",
                                 "password": "ssh-pw", "key_file": ""}
    assert request["password"] == "db-pw" and request["password_ref"] == "PG_LAB_01_PASSWORD"


def test_the_record_is_written_here_and_says_control_made_it(tmp_path, built):
    _create(tmp_path, password_text="db-pw")
    entries = registry.load_registry(registry.default_registry_path(tmp_path))[registry.REGISTRY_ROOT_KEY]
    [entry] = entries
    assert entry["id"] == "PG_LAB_01" and entry["host"] == "192.0.2.10"
    assert entry["created_by"] == worker_data.CREATED_BY_WORKER_COMMAND


def test_a_new_password_is_stored_here_after_the_build(tmp_path, built):
    _create(tmp_path, password_text="db-pw")
    from db_ops.lib import data_sources

    assert data_sources.load_secret_text(tmp_path, key=KEY)["PG_LAB_01_PASSWORD"] == "db-pw"


def test_a_failed_build_stores_no_password_and_registers_nothing(tmp_path, monkeypatch):
    """A wrong image tag used to leave its password in the store under a ref no lab uses."""
    monkeypatch.setenv("DB_OPS_SECRET_KEY", KEY)
    _isolate_the_password_variable(monkeypatch)

    def fail(command, request, **_kwargs):
        raise common_cli.CommonCliError("create-db-docker failed: manifest unknown")

    monkeypatch.setattr(common_cli, "run", fail)
    assert _create(tmp_path, password_text="db-pw") == 1
    assert not registry.default_registry_path(tmp_path).exists()
    from db_ops.lib import data_sources

    assert "PG_LAB_01_PASSWORD" not in (data_sources.load_secret_text(tmp_path, key=KEY) or {})


def test_a_stored_password_is_reused_when_none_is_given(tmp_path, built):
    set_secret_text(tmp_path, "PG_LAB_01_PASSWORD", "stored-pw", key=KEY)
    _create(tmp_path)
    assert built[0][1]["password"] == "stored-pw"


def test_a_different_password_under_the_ref_is_refused_before_anything_is_built(tmp_path, built):
    set_secret_text(tmp_path, "PG_LAB_01_PASSWORD", "stored-pw", key=KEY)
    assert _create(tmp_path, password_text="another-pw") == 1
    assert built == []


def test_a_dry_run_registers_nothing(tmp_path, built, capsys):
    assert _create(tmp_path, dry_run=True) == 0
    assert built[0][1]["dry_run"] is True
    assert not registry.default_registry_path(tmp_path).exists()
    printed = capsys.readouterr().out
    assert json.loads(printed[printed.index("{"):printed.rindex("}") + 1])[registry.REGISTRY_ROOT_KEY][0]["id"] == "PG_LAB_01"


def test_worker_host_overrides_the_recorded_address(tmp_path, built):
    _create(tmp_path, password_text="db-pw", worker_host="lab.example.test")
    [entry] = registry.load_registry(registry.default_registry_path(tmp_path))[registry.REGISTRY_ROOT_KEY]
    assert entry["host"] == "lab.example.test"


def test_nothing_in_control_runs_sre_s_cli():
    from pathlib import Path

    source = Path(worker_data.__file__).read_text(encoding="utf-8")
    assert "db_ops.sre" not in source
