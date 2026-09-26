"""A PostgreSQL restore replays its WAL, and runs on a machine whose sudo asks for a password.

The 2026-09-24 lab drill restored a PostgreSQL lab on 192.0.2.249 onto one on 192.0.2.250 - both
reached as a user in the docker group whose sudo wants a password - and it failed, or worse,
succeeded wrongly, five ways:

* planning connected over SSH with neither a key nor a password: the host block never carried one;
* every docker call was `sudo docker`, which cannot answer a password prompt;
* the data swap ran as root on the host, against Docker's volume folders;
* then it reported DONE with only the rows of the last base/incremental backup: the recovery
  configuration was written by a step AFTER the server had started on the combined data, so no WAL
  was replayed - and `printf %s\\n`, unquoted, ended the file with `'latest'n`;
* and on the backup side the lab's bind mount belonged to the SSH user, so postgres (uid 999) could
  not create its folders - the fault 1.18 had fixed for SQL Server only. `run-sql`'s autocommit did
  not reach PostgreSQL either: CREATE DATABASE refused "inside a transaction block".

Fixed and proven on the labs: rows on the target equal the source, WAL replayed to a new timeline.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from db_ops.backup_restore import restore_by_id
from db_ops.lib.shell import docker_cli

SCRIPTS = Path(__file__).resolve().parents[1] / "db_ops" / "common" / "backup_scripts"


# --------------------------------------------------------------------------- #
# 1.28 - the recovery configuration is in place before the first start
# --------------------------------------------------------------------------- #
@pytest.fixture
def container_run(monkeypatch):
    """Drive the real container path of the PostgreSQL restore step, recording every command."""
    import db_ops.common.restorestep.postgresql as pg

    calls: list[str] = []
    mounts = [{"Source": "/var/lib/docker/volumes/v/_data", "Destination": "/var/lib/postgresql"}]
    failing = {"rebuild": False}

    def fake_run(host, command, **kw):
        calls.append(command)
        if "pg_combinebackup" in command and failing["rebuild"]:
            return {"exit_code": 1, "stdout": "", "stderr": "pg_combinebackup: boom"}
        if "inspect -f" in command:
            return {"exit_code": 0, "stdout": "postgres:18\n", "stderr": ""}
        if "pg_is_in_recovery" in command:
            return {"exit_code": 0, "stdout": "f\n", "stderr": ""}
        return {"exit_code": 0, "stdout": json.dumps([{"Mounts": mounts}]), "stderr": ""}

    monkeypatch.setattr(pg, "run", fake_run)

    def run(fail=False, **extra):
        failing["rebuild"] = fail
        from db_ops.common.restorestep import restore_step
        result = restore_step("diff", {
            "db_type": "postgresql", "data_dir": "/var/lib/postgresql/18/docker",
            "staging_dir": "/var/lib/postgresql/dbops_staging",
            "backup_paths": ["/in/base/a_FULL", "/in/base/b_INCR"],
            "host": {"runtime": "docker", "host": "h", "container": "c", "sudo": True}, **extra})
        return result, calls
    run.calls = calls
    return run


def test_the_recovery_configuration_is_written_before_the_first_start(container_run):
    result, calls = container_run(wal_dir="/in/wal")

    combine = next(c for c in calls if "pg_combinebackup" in c)
    start = next(i for i, c in enumerate(calls) if c.endswith(" start c"))
    assert "/var/lib/postgresql/dbops_staging/recovery.signal" in combine
    assert "restore_command" in combine and "cp /var/lib/postgresql/dbops_wal/%f %p" in combine
    assert calls.index(combine) < start
    assert result["recovery"] == "recovered"


def test_the_wal_is_copied_into_the_data_volume_with_the_chain(container_run):
    """The server runs restore_command after the start, so the WAL must be where IT can read it -
    a copy in the data volume, made by the same helper that builds the cluster. Not `docker cp`,
    which needs the target running."""
    result, calls = container_run(wal_dir="/in/wal")

    combine = next(c for c in calls if "pg_combinebackup" in c)
    assert "-v /in/wal:/in/wal:ro" in combine
    assert "cp -a /in/wal/. /var/lib/postgresql/dbops_wal/" in combine
    assert not [c for c in calls if "docker cp" in c]
    assert result["wal_copy"] == "/var/lib/postgresql/dbops_wal"


# --------------------------------------------------------------------------- #
# 1.39 - a target a failed drill left crash-looping can still be restored
# --------------------------------------------------------------------------- #
def test_the_target_is_stopped_before_anything_is_written(container_run):
    """Measured 2026-09-24: the .250 lab was restarting in a loop after a bad restore, and the next
    drill died on "Container ... is restarting, wait until the container is running" before
    touching anything. Nothing here enters the target, so its state does not matter."""
    _, calls = container_run(wal_dir="/in/wal")

    stop = next(i for i, c in enumerate(calls) if c.endswith(" stop c"))
    combine = next(i for i, c in enumerate(calls) if "pg_combinebackup" in c)
    assert stop < combine
    assert not [c for c in calls if " exec " in c and "pg_is_in_recovery" not in c]


def test_the_target_is_started_again_when_the_rebuild_fails(container_run):
    """Down is a second incident on top of the failed restore; the error already says what broke."""
    from db_ops.common.restorestep import RestoreStepError

    with pytest.raises(RestoreStepError, match="boom"):
        container_run(fail=True, wal_dir="/in/wal")

    assert container_run.calls[-1].endswith(" start c")
    assert not [c for c in container_run.calls if "mv -t" in c], "nothing swapped after a failure"


def test_the_step_waits_until_recovery_has_ended(container_run):
    _, calls = container_run(wal_dir="/in/wal")

    assert "pg_is_in_recovery" in calls[-1]


def test_a_planned_restore_into_a_container_has_no_separate_log_step(monkeypatch):
    monkeypatch.setattr(restore_by_id, "_list_backup_files", lambda request: {
        "files": [{"path": "/in/base/a_FULL"}] if "full" in request["kinds"] else [],
        "newest_finished_at": "2026-09-24T10:00:00Z"})
    job = SimpleNamespace(restore_id="R", target_backup_dir="/in", target_visible_dir="", backup_dir="/o", env={})

    steps = restore_by_id._plan_postgresql(job, {}, point_in_time="", data_dir=None,
                                           host={"runtime": "docker", "host": "h", "container": "c"})

    assert [s["op"] for s in steps] == ["restore-full", "verify-restore"]
    assert steps[0]["request"]["wal_dir"] == "/in/wal"


@pytest.mark.skipif(not shutil.which("sh"), reason="no POSIX shell here")
def test_the_configuration_file_ends_where_it_should(tmp_path):
    """Run the generated shell for real: unquoted, `printf %s\\n` left a literal `n` behind."""
    from db_ops.common.restorestep.postgresql import _recovery_config

    directory = tmp_path.as_posix()
    subprocess.run(["sh", "-c", _recovery_config(directory, "/w", "")], check=True)

    text = (tmp_path / "postgresql.auto.conf").read_text(encoding="utf-8")
    assert text == "restore_command = 'cp /w/%f %p'\nrecovery_target_timeline = 'latest'\n"
    assert (tmp_path / "recovery.signal").exists()


# --------------------------------------------------------------------------- #
# 1.27 - no root on the host, no sudo that has to be typed
# --------------------------------------------------------------------------- #
def test_the_swap_runs_in_a_helper_container_not_as_root_on_the_host(container_run):
    _, calls = container_run(wal_dir="/in/wal")

    swap = next(c for c in calls if "--volumes-from" in c and "mv -t" in c)
    assert f"{docker_cli(True)} run --rm -u postgres --volumes-from c " in swap
    assert "--entrypoint sh postgres:18" in swap
    assert "/var/lib/docker/volumes" not in swap, "the container's own paths, not the host's"


def test_asked_for_sudo_is_a_fallback_everywhere(container_run):
    _, calls = container_run(wal_dir="/in/wal")

    assert not any(c.startswith("sudo ") for c in calls), [c for c in calls if c.startswith("sudo ")]


# --------------------------------------------------------------------------- #
# 1.26 - a password-login target gets its password
# --------------------------------------------------------------------------- #
def test_a_password_login_target_is_given_its_password(monkeypatch):
    from db_ops.backup_restore import backup

    target = SimpleNamespace(host="192.0.2.250", port=22, username="labuser", key_file=None,
                             password_ref="LAB_SSH")
    monkeypatch.setattr(backup, "resolve_ssh_target", lambda *a, **k: target)
    job = SimpleNamespace(target_server_id="T", server_id="S", label="L", target_container="c")

    block = restore_by_id._host_block(job, data_dir=None, load_secrets=lambda: {"LAB_SSH": "pw"})

    assert block["password"] == "pw"


def test_a_key_login_target_is_not_asked_for_secrets(monkeypatch, tmp_path):
    from db_ops.backup_restore import backup
    import db_ops.lib.data_sources as data_sources

    target = SimpleNamespace(host="h", port=22, username="u", key_file="k.key", password_ref="X")
    monkeypatch.setattr(backup, "resolve_ssh_target", lambda *a, **k: target)
    monkeypatch.setattr(data_sources, "resolve_ssh_key", lambda name, d=None: tmp_path / name)
    job = SimpleNamespace(target_server_id="T", server_id="S", label="L", target_container="")

    block = restore_by_id._host_block(job, data_dir=None, load_secrets=lambda: pytest.fail("asked"))

    assert "password" not in block


# --------------------------------------------------------------------------- #
# 1.25 - autocommit reaches PostgreSQL
# --------------------------------------------------------------------------- #
def test_autocommit_is_on_before_the_first_statement(monkeypatch):
    import sys
    import types

    from db_ops.common import db_connect

    events: list[str] = []

    class _Cursor:
        def execute(self, sql):
            events.append("autocommit=%s %s" % (conn.autocommit, sql.split("(")[0]))

        def close(self):
            pass

    class _Conn:
        autocommit = False

        def cursor(self):
            return _Cursor()

    conn = _Conn()
    fake = types.ModuleType("pg8000")
    fake.dbapi = types.SimpleNamespace(connect=lambda **kw: conn)
    monkeypatch.setitem(sys.modules, "pg8000", fake)

    db_connect._connect_postgresql(host="h", port=5432, database="postgres", username="u", password="p",
                                   connect_timeout=5, statement_timeout=30, autocommit=True)

    assert events == ["autocommit=True SELECT set_config"]


# --------------------------------------------------------------------------- #
# 1.24 - the PostgreSQL and Oracle backup jobs take their folder over
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script,leaf", [
    ("postgresql/pg_basebackup_database.sh", "${base_dir}"),
    ("postgresql/pg_archive_wal.sh", "${wal_dir}"),
    ("oracle/oracle_rman_database.sh", "${backup_dir}"),
    ("oracle/oracle_rman_archivelog.sh", "${backup_dir}"),
])
def test_a_backup_job_takes_over_the_folder_it_writes(script, leaf):
    text = (SCRIPTS / script).read_text(encoding="utf-8")

    assert f"if ! run_db \"mkdir -p '{leaf}' && test -w '{leaf}'\"" in text
    assert "run_root() { $DOCKER exec -u 0" in text and "run_root() { sudo -n" in text
    assert "chown -R ${engine_uid}:${engine_gid} '${backup_dir}'" in text
    # Before the job's own mkdir, or that mkdir is what fails.
    assert text.index("run_root \"mkdir -p") < text.index(f"run_db \"mkdir -p '{leaf}'\" ")


# --------------------------------------------------------------------------- #
# 1.29 - Oracle reads the backup location inside the container
# --------------------------------------------------------------------------- #
def test_an_oracle_duplicate_stages_its_backup_location_into_the_container(monkeypatch):
    """The copy lands on the target HOST; RMAN reads inside the container. Served by no volume, the
    location was invisible to it: "RMAN-05579: CONTROLFILE backup not found" (2026-09-24)."""
    import db_ops.common.restorestep.oracle as ora
    import db_ops.common.restorestep.postgresql as pg

    calls: list[str] = []

    def fake_run(host, command, **kw):
        calls.append(command)
        return {"exit_code": 0, "stdout": "Finished Duplicate Db", "stderr": ""}

    monkeypatch.setattr(pg, "run", fake_run)
    monkeypatch.setattr(ora, "run", fake_run)
    monkeypatch.setattr(pg, "_container_mounts", lambda *a, **k: [])

    result = ora.apply("full", {"db_type": "oracle", "mode": "duplicate", "oracle_sid": "FREE",
                                "backup_location": "/in/ora",
                                "host": {"runtime": "docker", "host": "h", "container": "c"}}, [])

    assert result["staged_into_container"] == ["/in/ora"]
    cp = next(i for i, c in enumerate(calls) if " cp /in/ora c:/in/ora" in c)
    duplicate = next(i for i, c in enumerate(calls) if "rman auxiliary" in c)
    assert cp < duplicate
