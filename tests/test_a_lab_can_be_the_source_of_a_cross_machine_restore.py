"""A lab built by create-db-docker can be backed up, and can be the source of a cross-machine restore.

The 2026-09-24 drill backed a SQL Server lab up on 192.0.2.249 and restored it onto 192.0.2.250.
Before that worked, the lab side failed four ways:

* SQL Server (uid 10001 in the image) could not create its backup folder in the bind mount, which
  install_docker hands to the SSH user - every backup failed "mkdir: Permission denied";
* the pieces it did write stayed 0660, unreadable by the SSH user who copies them to the other
  machine - the Oracle and PostgreSQL scripts have relaxed theirs after every run for months;
* a stopped container was reported as "no sqlcmd found; install mssql-tools" - it was not running;
* and only two templates rendered --backup-mount: PostgreSQL, Oracle Free, MySQL single and every
  ha-lab dropped it without a word.

Also pinned: a second lab created on the same host must not take the first lab's backups away from
its engine (see test_install_docker_prepares_every_folder_a_lab_writes.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_ops.common.docker_db import templates as t
from db_ops.lib.docker_db_spec import DockerDbSpec

SCRIPTS = Path(__file__).resolve().parents[1] / "db_ops" / "common" / "backup_scripts"
MSSQL = SCRIPTS / "sqlserver" / "mssql_backup_database.sh"


def _spec(engine, mode="single", **overrides):
    values = dict(name="LAB", engine=engine, version="18", mode=mode, host_port=5432,
                  password_env="LAB_PASSWORD")
    values.update(overrides)
    return DockerDbSpec(**values)


# --------------------------------------------------------------------------- #
# The backup scripts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", sorted(SCRIPTS.glob("*/*.sh")), ids=lambda p: p.name)
def test_every_backup_script_says_a_stopped_container_is_stopped(script):
    text = script.read_text(encoding="utf-8")

    assert "{{.State.Running}}" in text
    assert "is not running - start it" in text
    # Checked before the first exec into it, or that exec's failure is what gets reported.
    assert text.index("{{.State.Running}}") < text.index('exec_here() {' if "exec_here() {" in text else "run_db()")


def test_the_sqlserver_script_makes_its_backup_folder_writable_by_the_engine():
    text = MSSQL.read_text(encoding="utf-8")

    assert 'exec_here test -w "$backup_dir"' in text
    assert 'exec_as_root() { $DOCKER exec -u 0' in text
    # Before the first thing that writes there - the certificate export.
    assert text.index("backup_dir_writable \\") < text.index('cert_dir="${backup_dir%/}/_cert"')


def test_the_sqlserver_script_opens_its_pieces_to_the_copy_after_every_run():
    text = MSSQL.read_text(encoding="utf-8")

    assert 'exec_here chmod -R a+rX "$backup_dir"' in text
    assert text.index('exec_here chmod -R a+rX "$backup_dir"') < text.index("printf 'RESULT=ok level=")


# --------------------------------------------------------------------------- #
# The templates
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("engine,version", [("postgres", "18"), ("oracle", "23.26.3"), ("mysql", "8.4")])
def test_a_single_lab_mounts_the_backup_folder_it_was_given(engine, version):
    spec = _spec(engine, version=version, backup_mount="/opt/db_ops/backup")

    compose = t.render(t.load_template(spec.engine, spec.mode), t.build_context(spec))

    assert '- "/opt/db_ops/backup:/opt/db_ops/backup"' in compose


def test_an_ha_lab_takes_a_backup_mount_now():
    """It was refused, when no HA template rendered one; they all do since 1.30 - see
    test_a_lab_with_a_backup_mount_is_backup_ready.py."""
    _spec("postgres", mode="ha-lab", replicas=2, backup_mount="/opt/db_ops/backup").validate()


def test_an_ha_lab_that_names_no_mount_is_still_built():
    _spec("postgres", mode="ha-lab", replicas=2).validate()
    _spec("mssql", mode="ha-lab", version="2025-latest", replicas=2).validate()
