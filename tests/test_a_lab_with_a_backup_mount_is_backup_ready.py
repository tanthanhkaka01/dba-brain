"""A lab built with a backup mount can be backed up as built - HA labs included.

The 2026-09-24 drills needed a DBA's hands before the first backup: the PostgreSQL lab had no WAL
archive (`archive_mode is 'off'`), the Oracle lab ran NOARCHIVELOG (an online RMAN backup refuses
that), and an ha-lab had no host-visible backup folder at all, so it could never be the source of a
cross-machine restore - `--backup-mount` was refused for it. Each is now part of the lab:

* every lab's backups go under `<mount>/<lab name>`, which `create-db-docker` prints;
* PostgreSQL (single, and the HA primary) makes that folder as root and archives WAL into its `wal/`,
  with WAL summaries for incrementals;
* Oracle single runs a first-start script that turns ARCHIVELOG on (the image has no switch for it -
  read from its entrypoint on 23.26.3); the Data Guard lab already runs ARCHIVELOG;
* the HA primaries mount the folder (SQL Server: every replica, since a backup may run on any).
"""

from __future__ import annotations

import pytest
import yaml

from db_ops.common.docker_db import templates as t
from db_ops.lib.docker_db_spec import DockerDbSpec

MOUNT = "/opt/db_ops/backup"


def _compose(engine, mode="single", version="18", replicas=None, **kw):
    values = dict(name="LAB", engine=engine, version=version, mode=mode, host_port=5432,
                  password_env="LAB_PW", backup_mount=MOUNT)
    values.update(kw)
    if replicas:
        values["replicas"] = replicas
    spec = DockerDbSpec(**values)
    spec.validate(replicas_explicit=bool(replicas))
    return spec, yaml.safe_load(t.render(t.load_template(engine, mode), t.build_context(spec)))


def test_the_backup_folder_is_one_per_lab_under_the_mount():
    spec, _ = _compose("postgres")
    assert t.backup_folder(spec) == f"{MOUNT}/LAB"
    spec, _ = _compose("postgres", backup_mount="")
    assert t.backup_folder(spec) == ""


@pytest.mark.parametrize("mode,replicas,service", [("single", None, "LAB"), ("ha-lab", 2, "LAB-primary")])
def test_a_postgresql_lab_archives_its_wal_into_the_backup_folder(mode, replicas, service):
    _, doc = _compose("postgres", mode=mode, replicas=replicas)
    node = doc["services"][service]

    assert f'"{MOUNT}:{MOUNT}"' in str(node["volumes"]) or f"{MOUNT}:{MOUNT}" in node["volumes"]
    assert "archive_mode=on" in node["command"] and "summarize_wal=on" in node["command"]
    assert f"archive_command=test ! -f {MOUNT}/LAB/wal/%f && cp %p {MOUNT}/LAB/wal/%f" in node["command"]
    # Made by root in the image's own entrypoint, then handed to postgres; "$$@" is compose's "$@".
    assert node["entrypoint"][:2] == ["bash", "-c"]
    assert f'mkdir -p "{MOUNT}/LAB/wal" && chown -R postgres:postgres "{MOUNT}/LAB"' in node["entrypoint"][2]
    assert 'exec docker-entrypoint.sh "$$@"' in node["entrypoint"][2]


def test_only_the_postgresql_primary_archives():
    _, doc = _compose("postgres", mode="ha-lab", replicas=2)
    for name, node in doc["services"].items():
        if name != "LAB-primary":
            assert "archive_mode=on" not in str(node.get("command")), name


def test_an_oracle_lab_turns_archivelog_on_at_its_first_start():
    spec, doc = _compose("oracle", version="23.26.3")
    files = dict(t.extra_files(spec))

    assert "ALTER DATABASE ARCHIVELOG;" in files["initdb/10-archivelog.sql"]
    assert "./initdb:/container-entrypoint-initdb.d:ro" in doc["services"]["LAB"]["volumes"]


@pytest.mark.parametrize("engine,version,replicas,expected", [
    ("oracle", "23.26.3", 1, {"LAB-primary"}),
    ("mssql", "2025-latest", 2, {"LAB-primary", "LAB-standby-1", "LAB-standby-2"}),
    ("postgres", "18", 2, {"LAB-primary"}),
])
def test_an_ha_lab_mounts_the_backup_folder(engine, version, replicas, expected):
    _, doc = _compose(engine, mode="ha-lab", version=version, replicas=replicas)

    mounted = {name for name, node in doc["services"].items()
               if any(f"{MOUNT}:{MOUNT}" in str(v) for v in node.get("volumes", []))}
    assert mounted == expected


@pytest.mark.parametrize("engine,version,mode,replicas", [
    ("postgres", "18", "single", None), ("postgres", "18", "ha-lab", 2),
    ("oracle", "23.26.3", "single", None), ("mssql", "2025-latest", "ha-lab", 2),
])
def test_without_a_mount_nothing_of_this_is_rendered(engine, version, mode, replicas):
    spec, doc = _compose(engine, mode=mode, version=version, replicas=replicas, backup_mount="")
    text = str(doc)

    assert MOUNT not in text and "archive_mode" not in text and "container-entrypoint-initdb" not in text
    assert "initdb/10-archivelog.sql" not in dict(t.extra_files(spec))


def test_the_summary_names_the_backup_folder():
    from db_ops.common.docker_db import compose, provisioner

    spec, _ = _compose("postgres")
    plan = compose.build_plan(spec, containers_dir="/opt/db_ops/containers", password="x", worker_host="192.0.2.249")
    summary = provisioner.format_summary(plan, {}, status_label="running")

    assert f"Backup folder: {MOUNT}/LAB" in summary
    assert "WAL is archived into its wal/" in summary
