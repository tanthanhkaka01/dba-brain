"""A backup that lies about succeeding is worse than one that fails.

The failure is only discovered by the restore that needed it, which is the worst possible moment.
That is the whole reason this layer judges a run the way it does, and most of what these tests pin.

The rest is the split: ``backup_restore`` reads ``restore_config.json``, asks ``db_instances.json``
for the host behind a ``server_id`` and decrypts the passphrase; ``common.backup`` is handed the
answers and runs exactly what it was given. A lookup inside the API would make its behaviour depend
on files the caller cannot see — two callers passing the same request getting different backups
because one machine's config is a week older, and nothing in the request saying so.
"""

from __future__ import annotations

import pytest

from db_ops.common import backup as backup_api
from db_ops.common.backup import parse_backup_spec, plan_backup, run_backup
from db_ops.common.backup.spec import RECEIPT, BackupSpecError, backup_level_for

SCRIPT = "assets/backup/oracle/oracle_rman_database.sh"


def _request(**over):
    request = {
        "db_type": "oracle",
        "label": "CLOUD_ORA_DB/database",
        "script_path": SCRIPT,
        "env": {"DOCKER_CONTAINER": "ora_dg_lab-primary", "BACKUP_DIR": "/opt/oracle/backup/dbops"},
        "host": {"runtime": "linux", "host": "203.0.113.188", "username": "ubuntu"},
        "timeout": 7200,
    }
    request.update(over)
    return request


@pytest.fixture
def ran(monkeypatch):
    """Capture what would have been shipped, and script the host's answer."""
    captured = {}

    def fake_run_script(host, script, *, env=None, timeout=None):
        captured.update({"host": host, "script": script, "env": env, "timeout": timeout})
        return captured.get("answer", {"exit_code": 0, "stdout": f"{RECEIPT} backup_level=0\n",
                                       "stderr": ""})

    monkeypatch.setattr(backup_api, "run_script", fake_run_script)
    return captured


# --------------------------------------------------------------------------- #
# Judging the run
# --------------------------------------------------------------------------- #
def test_success_is_the_receipt_not_the_exit_code(ran):
    """Exit 0 says the shell finished, not that a backup happened."""
    result = run_backup(parse_backup_spec(_request()))

    assert result["status"] == "done"
    assert result["receipt"] is True


def test_exit_zero_without_the_receipt_is_an_error_that_says_so(ran):
    """This is not hypothetical: a `docker exec -i` inside a script read the rest of the script as
    its own stdin, so the shell ran out of work and reported success having backed up nothing."""
    ran["answer"] = {"exit_code": 0, "stdout": "", "stderr": ""}

    result = run_backup(parse_backup_spec(_request()))

    assert result["status"] == "error"
    assert result["receipt"] is False
    assert "did not run to completion" in result["error"]


def test_a_nonzero_exit_reports_what_the_engine_said(ran):
    """RMAN's own words, not "exit 1" — the operator has to act on this line."""
    ran["answer"] = {"exit_code": 1, "stdout": "",
                     "stderr": "RMAN-06059: expected archived log not found"}

    result = run_backup(parse_backup_spec(_request()))

    assert result["status"] == "error"
    assert "RMAN-06059" in result["error"]


def test_a_host_that_cannot_be_reached_is_distinct_from_a_backup_that_failed(ran, monkeypatch):
    """The fix is somewhere else entirely, so it must not read as "the backup failed"."""
    from db_ops.common.hostcmd import HostCommandError

    def refuse(*args, **kwargs):
        raise HostCommandError("could not connect to ubuntu@203.0.113.188: timed out")

    monkeypatch.setattr(backup_api, "run_script", refuse)

    result = run_backup(parse_backup_spec(_request()))

    assert result["status"] == "error"
    assert result["exit_code"] is None
    assert "could not connect" in result["error"]


# --------------------------------------------------------------------------- #
# The spec: complete, or refused
# --------------------------------------------------------------------------- #
def test_one_word_becomes_each_engines_own_level(ran):
    """`full` is one word for the operator and three values for the engines. Translated here, in
    the one place that knows the engine, so the CLI and the app cannot disagree about it."""
    run_backup(parse_backup_spec(_request(level="full")))

    assert ran["env"]["BACKUP_LEVEL"] == "0"          # RMAN counts levels
    assert backup_level_for("postgresql", "full") == "full"
    assert backup_level_for("sqlserver", "diff") == "diff"


def test_an_engine_without_that_level_is_refused_by_name():
    """Oracle and PostgreSQL take archive/WAL backups through a separate script with its own
    schedule. Passing `log` into their script would have it read as something else entirely."""
    with pytest.raises(BackupSpecError, match="no 'log' level"):
        parse_backup_spec(_request(level="log"))


def test_an_empty_env_value_is_refused(ran):
    """An empty value is how a missing secret arrives, and a backup script reading it as "no
    passphrase" writes an unencrypted set nobody notices until a restore needs it."""
    with pytest.raises(BackupSpecError, match="env\\[BACKUP_ENCRYPTION_PASSWORD\\] is empty"):
        parse_backup_spec(_request(env={"BACKUP_ENCRYPTION_PASSWORD": ""}))


def test_a_script_path_that_is_not_there_is_named(ran):
    """A relative path that missed and a path that is right but unreadable used to arrive as the
    same empty script — which then ran, exited 0, and printed no receipt."""
    with pytest.raises(BackupSpecError, match="script_path not found"):
        parse_backup_spec(_request(script_path="assets/backup/oracle/nope.sh"))


def test_giving_both_a_script_and_a_path_is_refused():
    """Two sources for one thing is two ways to disagree about what ran."""
    with pytest.raises(BackupSpecError, match="not both"):
        parse_backup_spec(_request(script="echo hi"))


def test_an_unsupported_engine_is_refused_rather_than_defaulted():
    with pytest.raises(BackupSpecError, match="db_type must be one of"):
        parse_backup_spec(_request(db_type="db2"))


def test_the_script_is_shipped_whole(ran):
    """Fed on stdin rather than passed as an argument: a backup script is a hundred lines of shell
    with its own quoting, and as an argument every character has to survive two shells."""
    run_backup(parse_backup_spec(_request()))

    assert ran["script"].startswith("#!/bin/bash")
    assert RECEIPT in ran["script"], "the script itself is what prints the receipt"


def test_the_timeout_reaches_the_transport(ran):
    """An RMAN level 0 runs for hours; a default timeout would kill it halfway and leave a partial
    set behind."""
    run_backup(parse_backup_spec(_request(timeout=7200)))

    assert ran["timeout"] == 7200


# --------------------------------------------------------------------------- #
# The plan, and what it must not contain
# --------------------------------------------------------------------------- #
def test_the_plan_names_the_env_but_never_its_values():
    """The plan exists to be shown to somebody, and half of these are passphrases."""
    spec = parse_backup_spec(_request(
        env={"DOCKER_CONTAINER": "c", "BACKUP_ENCRYPTION_PASSWORD": "s3cret-passphrase"}))

    planned = plan_backup(spec)

    assert planned["env_names"] == ["BACKUP_ENCRYPTION_PASSWORD", "DOCKER_CONTAINER"]
    assert "s3cret-passphrase" not in str(planned)


def test_a_dry_run_ships_nothing(ran):
    """Asserted on the transport, not on a flag: nothing was sent anywhere."""
    spec = parse_backup_spec(_request(dry_run=True))

    assert spec.dry_run is True
    assert ran == {}, "parsing a dry-run spec must not reach the host"


# --------------------------------------------------------------------------- #
# The instance metadata that a SQL Server backup does NOT contain
# --------------------------------------------------------------------------- #
def test_server_metadata_is_refused_for_engines_that_do_not_need_it():
    """RMAN DUPLICATE and pg_basebackup are physical and whole-instance, so that state is inside
    the data. Refused rather than ignored: a caller that set it believes it is getting something."""
    with pytest.raises(BackupSpecError, match="SQL Server only"):
        parse_backup_spec(_request(server_metadata={"target": "X", "output_dir": "/b"}))


def test_server_metadata_requires_somewhere_to_put_it():
    """The bundle belongs beside the backup it describes. A default would put it somewhere the
    restore does not look, which is the same as not having it."""
    with pytest.raises(BackupSpecError, match="output_dir is required"):
        parse_backup_spec(_request(db_type="sqlserver", level="full",
                                   server_metadata={"target": "ACME-192-0-2-115"}))


def test_metadata_is_exported_after_a_backup_that_completed(ran, monkeypatch):
    exported = {}

    def fake_export(request, **kwargs):
        exported.update(request)
        return {"ok": True, "artifacts": ["logins", "agent_jobs"]}

    from db_ops.common import sqlserver_instance
    monkeypatch.setattr(sqlserver_instance, "export_instance", fake_export)

    result = run_backup(parse_backup_spec(_request(
        db_type="sqlserver", level="full",
        script_path="assets/backup/sqlserver/mssql_backup_database.sh",
        server_metadata={"target": "ACME-192-0-2-115", "output_dir": "/b/_instance"})))

    assert exported == {"target": "ACME-192-0-2-115", "output_dir": "/b/_instance"}
    assert result["server_metadata_result"]["ok"] is True


def test_metadata_is_not_exported_when_the_backup_failed(ran, monkeypatch):
    """A bundle describing an instance whose backup did not complete is a set of files that look
    like a matched pair and are not."""
    from db_ops.common import sqlserver_instance
    monkeypatch.setattr(sqlserver_instance, "export_instance",
                        lambda *a, **k: pytest.fail("must not export after a failed backup"))
    ran["answer"] = {"exit_code": 1, "stdout": "", "stderr": "BACKUP failed"}

    result = run_backup(parse_backup_spec(_request(
        db_type="sqlserver", level="full",
        script_path="assets/backup/sqlserver/mssql_backup_database.sh",
        server_metadata={"target": "T", "output_dir": "/b/_instance"})))

    assert result["server_metadata_result"]["skipped"] is True


def test_a_metadata_failure_does_not_fail_the_backup(ran, monkeypatch):
    """The data is the thing that must not be lost. Losing a completed backup to a metadata step
    that could not read sys.credentials would be absurd."""
    from db_ops.common import sqlserver_instance
    monkeypatch.setattr(sqlserver_instance, "export_instance",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("login failed")))

    result = run_backup(parse_backup_spec(_request(
        db_type="sqlserver", level="full",
        script_path="assets/backup/sqlserver/mssql_backup_database.sh",
        server_metadata={"target": "T", "output_dir": "/b/_instance"})))

    assert result["status"] == "done"
    assert "login failed" in result["server_metadata_result"]["error"]
