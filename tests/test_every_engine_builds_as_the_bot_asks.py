"""Every engine `create-db-docker` knows can be built from the bot, and comes up as promised.

The 2026-09-25 pass built one lab per engine through `/spbot_create_db_docker` on the soaked
0.23.0 wheel (the lab VMs emptied first). SQL Server, Oracle Free, MySQL and the PostgreSQL ha-lab
came up as promised; four things did not:

* the bot refused `oracle-xe` - its engine question listed four of the provisioner's five engines;
* an Oracle XE lab with a backup mount came up NOARCHIVELOG: the first-start ARCHIVELOG script
  (1.31) was Oracle Free's only, and XE's template never mounted the script directory;
* the MySQL ha-lab could not be built at all - `bitnami/mysql` has left Docker Hub - and the image
  check passed anyway, because it asked only about the single-mode image;
* every generated compose file pointed its reader at `db_ops/sre/docker_db/models.py`, gone since
  the provisioner moved to `common`.

Two older bot behaviours went with them: a failed build left its password in the store, and a
`{worker_host}` nothing filled in would have been recorded as a lab's host.
"""

from __future__ import annotations

import json
import types

import pytest

from conftest import shipped_config
from db_ops.common.docker_db import compose as compose_mod
from db_ops.common.docker_db import provisioner, templates
from db_ops.lib.docker_db_spec import VALID_ENGINES, DockerDbSpec


def _plan(**over):
    values = dict(name="lab01", engine="postgres", version="18", mode="single", host_port=5432,
                  password_env="LAB01_PASSWORD")
    values.update(over)
    return compose_mod.build_plan(DockerDbSpec(**values), containers_dir="/c", password=None,
                                  worker_host="192.0.2.10", dry_run=True)


def _file(plan, relpath):
    return next((f.content for f in plan.files if f.relpath == relpath), None)


# --------------------------------------------------------------------------- #
# The bot offers what the provisioner builds
# --------------------------------------------------------------------------- #
def test_the_bots_engine_question_offers_every_engine_the_provisioner_builds():
    doc = json.loads(shipped_config("telegram_support_commands.json").read_text(encoding="utf-8-sig"))
    command = next(c for c in doc["telegram_support_commands"] if c["command_text"] == "spbot_create_db_docker")
    engine = next(p for p in command["action_config"]["parameters"] if p["name"] == "engine")

    assert sorted(o["value"] for o in engine["options"]) == sorted(VALID_ENGINES)
    assert sorted(engine["pattern"].split("|")) == sorted(VALID_ENGINES)


# --------------------------------------------------------------------------- #
# Oracle XE is backup-ready as built
# --------------------------------------------------------------------------- #
def test_an_oracle_xe_lab_with_a_backup_mount_turns_archivelog_on_at_its_first_start():
    plan = _plan(engine="oracle-xe", version="11", host_port=1521, backup_mount="/opt/db_ops/backup")
    script = _file(plan, "initdb/10-archivelog.sql")

    assert script and "ALTER DATABASE ARCHIVELOG" in script
    assert "PLUGGABLE" not in script, "XE 11 is a non-CDB"
    assert "./initdb:/container-entrypoint-initdb.d:ro" in _file(plan, "docker-compose.yml")


def test_an_oracle_xe_lab_without_a_mount_is_left_as_the_image_makes_it():
    plan = _plan(engine="oracle-xe", version="11", host_port=1521, backup_mount="")

    assert _file(plan, "initdb/10-archivelog.sql") is None
    assert "container-entrypoint-initdb.d" not in _file(plan, "docker-compose.yml")


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
def test_the_mysql_ha_lab_runs_an_image_that_still_exists():
    compose = _file(_plan(engine="mysql", version="8.4", mode="ha-lab", host_port=3306), "docker-compose.yml")

    assert "image: bitnamilegacy/mysql:8.4" in compose
    assert "image: bitnami/mysql" not in compose


def test_the_image_check_asks_about_every_image_the_compose_file_pulls():
    assert provisioner.images_in(_plan(engine="mysql", version="8.4", mode="ha-lab", host_port=3306)) == [
        "bitnamilegacy/mysql:8.4"]
    oracle_ha = provisioner.images_in(_plan(engine="oracle", version="23.26.3", mode="ha-lab",
                                            replicas=1, host_port=1521))
    assert "gvenzl/oracle-free:23.26.3" in oracle_ha and len(oracle_ha) >= 2, oracle_ha


def test_a_template_image_that_left_the_registry_is_named_as_such():
    spec = DockerDbSpec(name="lab01", engine="mysql", version="8.4", mode="ha-lab", host_port=3306,
                        password_env="LAB01_PASSWORD")

    def runner(argv, **kwargs):
        missing = argv[-1].startswith("bitnami/")
        return types.SimpleNamespace(returncode=1 if missing else 0, stdout="",
                                     stderr="manifest unknown" if missing else "")

    with pytest.raises(provisioner.ProvisionError, match="ha-lab mysql template runs"):
        provisioner.check_image_exists(spec, runner, images=["mysql:8.4", "bitnami/mysql:8.4"])


def test_no_generated_file_points_at_a_module_that_is_gone():
    for engine, version, mode, port in (("postgres", "18", "single", 5432), ("mssql", "2025-latest", "ha-lab", 1433),
                                        ("oracle-xe", "11", "single", 1521), ("mysql", "8.4", "single", 3306)):
        for pf in _plan(engine=engine, version=version, mode=mode, host_port=port).files:
            assert "sre/docker_db/models.py" not in pf.content, (engine, mode, pf.relpath)
    assert templates is not None


# --------------------------------------------------------------------------- #
# What sre does around the build
# --------------------------------------------------------------------------- #
def test_an_unfilled_worker_host_placeholder_is_never_recorded():
    from db_ops.sre.cli import _worker_host

    assert _worker_host("{worker_host}") == ""
    assert _worker_host("192.0.2.10") == "192.0.2.10"


@pytest.fixture
def config(tmp_path):
    from pathlib import Path

    path = tmp_path / "config.json"
    path.write_text((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8-sig"),
                    encoding="utf-8")
    return path


def _create(config, monkeypatch, *, run, stored):
    from db_ops.lib import common_cli
    from db_ops.sre import cli as sre_cli

    monkeypatch.setattr(common_cli, "run", run)
    monkeypatch.setattr(sre_cli, "_assert_password_can_be_stored", lambda *a, **k: None)
    monkeypatch.setattr(sre_cli, "_store_password", lambda *a, **k: stored.append(True))
    monkeypatch.setenv("NEW_PW", "Lab_Pw_2026")
    return sre_cli.main(["--config", str(config), "create-db-docker", "--name", "LAB_X", "--engine",
                         "postgres", "--version", "99", "--password-text-env", "NEW_PW", "--no-register"])


def test_a_failed_build_stores_no_password(config, monkeypatch):
    from db_ops.lib import common_cli

    def run(*a, **k):
        raise common_cli.CommonCliError("create-db-docker failed: Image not found: postgres:99")

    stored: list = []
    assert _create(config, monkeypatch, run=run, stored=stored) == 1
    assert stored == [], "the password of a lab that does not exist"


def test_a_build_that_worked_stores_its_password(config, monkeypatch):
    stored: list = []
    code = _create(config, monkeypatch, stored=stored,
                   run=lambda *a, **k: {"compose_path": "/c/LAB_X/docker-compose.yml", "summary": "", "status": "running"})

    assert code == 0 and stored == [True]
