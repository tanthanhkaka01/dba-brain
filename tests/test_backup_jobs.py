import json
from datetime import datetime, timedelta, timezone

import pytest

from db_ops.backup_restore.backup import (
    BackupJob,
    load_backup_jobs,
    select_due_backup_jobs,
)
from db_ops.lib.time_window import TimeWindow
from conftest import shipped_config


def _config(tmp_path, jobs=None, *, active=True):
    payload = {
        "backup_restore": {
            "backups": [
                {
                    "backup_id": "ORA_PRIMARY",
                    "active": active,
                    "db_type": "oracle",
                    "server_id": "CLOUD-ORA-1521",
                    "backup_dir": "/opt/oracle/backup/dbops",
                    "jobs": jobs
                    or [
                        {
                            "job": "database",
                            "script": "assets/backup/oracle/oracle_rman_database.sh",
                            "retention_days": 14,
                            "time_window": {"repeat_interval": 86400, "timeout": 7200},
                        },
                        {
                            "job": "archivelog",
                            "script": "assets/backup/oracle/oracle_rman_archivelog.sh",
                            "retention_days": 7,
                            "time_window": {"repeat_interval": 900, "timeout": 1800},
                        },
                    ],
                }
            ]
        }
    }
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class _Row(dict):
    """Stands in for the sqlite3.Row returned by fetch_latest_job_runs_by_job_code."""


def _run_row(*, status, minutes_ago):
    started = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return _Row(status=status, started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _job(job="archivelog", *, repeat=900, timeout=1800, active=True, window=None):
    return BackupJob(
        backup_id="ORA_PRIMARY",
        job=job,
        db_type="oracle",
        server_id="CLOUD-ORA-1521",
        script="assets/backup/oracle/oracle_rman_archivelog.sh",
        backup_dir="/opt/oracle/backup/dbops",
        retention_days=7,
        time_window=window or TimeWindow(repeat_interval=repeat, timeout=timeout),
        active=active,
    )


def test_load_backup_jobs_expands_one_job_per_entry(tmp_path):
    jobs = load_backup_jobs(_config(tmp_path))

    assert [item.label for item in jobs] == ["ORA_PRIMARY/database", "ORA_PRIMARY/archivelog"]
    database, archivelog = jobs
    assert database.retention_days == 14
    assert archivelog.retention_days == 7
    assert database.backup_dir == "/opt/oracle/backup/dbops"
    # The job code is what carries the schedule state between runs.
    assert database.job_code == "backup_restore.backup_job.ORA_PRIMARY.database"


def test_load_backup_jobs_entry_active_false_disables_every_job(tmp_path):
    jobs = load_backup_jobs(_config(tmp_path, active=False))

    assert jobs and all(not item.active for item in jobs)


def test_load_backup_jobs_rejects_duplicate_job_names(tmp_path):
    duplicate = [
        {"job": "database", "script": "a.sh", "time_window": {"repeat_interval": 60}},
        {"job": "database", "script": "b.sh", "time_window": {"repeat_interval": 60}},
    ]
    with pytest.raises(ValueError, match="Duplicate backup job"):
        load_backup_jobs(_config(tmp_path, duplicate))


def test_load_backup_jobs_requires_backup_dir(tmp_path):
    payload = {
        "backup_restore": {
            "backups": [
                {
                    "backup_id": "ORA",
                    "server_id": "S1",
                    "jobs": [{"job": "database", "script": "a.sh"}],
                }
            ]
        }
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="requires backup_dir"):
        load_backup_jobs(path)


def test_a_config_without_backups_falls_back_to_the_canonical_file(tmp_path, monkeypatch):
    """The scheduled command runs with --config config.json, which carries app settings but no
    backup entries; without the fallback the daemon would silently back nothing up."""
    import db_ops.backup_restore.backup as backup_module

    canonical = tmp_path / "restore_config.json"
    canonical.write_text(
        json.dumps(
            {
                "backup_restore": {
                    "backups": [
                        {
                            "backup_id": "FROM_CANONICAL",
                            "server_id": "S1",
                            "backup_dir": "/backup",
                            "jobs": [{"job": "database", "script": "a.sh",
                                      "time_window": {"repeat_interval": 60}}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(backup_module, "DEFAULT_RESTORE_CONFIG_PATH", canonical)

    app_config = tmp_path / "config.json"
    app_config.write_text(json.dumps({"app_name": "db_ops"}), encoding="utf-8")

    assert [item.backup_id for item in load_backup_jobs(app_config)] == ["FROM_CANONICAL"]


def test_an_explicit_backups_list_is_not_replaced_by_the_fallback(tmp_path, monkeypatch):
    import db_ops.backup_restore.backup as backup_module

    canonical = tmp_path / "canonical_restore_config.json"
    canonical.write_text(
        json.dumps({"backup_restore": {"backups": [
            {"backup_id": "CANONICAL", "server_id": "S", "backup_dir": "/b",
             "jobs": [{"job": "database", "script": "a.sh", "time_window": {"repeat_interval": 60}}]}
        ]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(backup_module, "DEFAULT_RESTORE_CONFIG_PATH", canonical)

    assert {item.backup_id for item in load_backup_jobs(_config(tmp_path))} == {"ORA_PRIMARY"}


def test_an_empty_backups_array_means_none_configured(tmp_path, monkeypatch):
    """An explicit empty list is a decision ("nothing to back up here"), not a missing section."""
    import db_ops.backup_restore.backup as backup_module

    monkeypatch.setattr(backup_module, "DEFAULT_RESTORE_CONFIG_PATH", tmp_path / "missing.json")
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"backup_restore": {"backups": []}}), encoding="utf-8")

    assert load_backup_jobs(path) == []


def test_due_when_never_run():
    assert [item.label for item in select_due_backup_jobs(jobs=[_job()], latest_runs={})] == [
        "ORA_PRIMARY/archivelog"
    ]


def test_not_due_before_the_repeat_interval_elapses():
    job = _job(repeat=900)
    latest = {job.job_code: _run_row(status="DONE", minutes_ago=5)}

    assert select_due_backup_jobs(jobs=[job], latest_runs=latest) == []


def test_due_again_after_the_repeat_interval():
    job = _job(repeat=900)
    latest = {job.job_code: _run_row(status="DONE", minutes_ago=20)}

    assert select_due_backup_jobs(jobs=[job], latest_runs=latest) == [job]


def test_a_running_job_is_not_started_a_second_time():
    # Two RMAN sessions against the same database is the failure this prevents.
    job = _job(repeat=900, timeout=1800)
    latest = {job.job_code: _run_row(status="RUNNING", minutes_ago=2)}

    assert select_due_backup_jobs(jobs=[job], latest_runs=latest) == []


def test_a_stale_running_job_is_recovered_after_its_timeout():
    job = _job(repeat=900, timeout=1800)
    latest = {job.job_code: _run_row(status="RUNNING", minutes_ago=60)}

    assert select_due_backup_jobs(jobs=[job], latest_runs=latest) == [job]


def test_a_failed_job_backs_off_on_retry_interval():
    job = _job(window=TimeWindow(repeat_interval=86400, retry_interval=3600, timeout=7200))
    too_soon = {job.job_code: _run_row(status="ERROR", minutes_ago=10)}
    elapsed = {job.job_code: _run_row(status="ERROR", minutes_ago=90)}

    assert select_due_backup_jobs(jobs=[job], latest_runs=too_soon) == []
    assert select_due_backup_jobs(jobs=[job], latest_runs=elapsed) == [job]


def test_inactive_job_is_never_due():
    assert select_due_backup_jobs(jobs=[_job(active=False)], latest_runs={}) == []


def test_time_window_hours_gate_the_job():
    # A window of 01:00-05:00 must not run at 14:00 even when the interval has elapsed.
    job = _job(window=TimeWindow(from_hour=1, to_hour=5, repeat_interval=86400))
    afternoon = datetime(2026, 7, 25, 14, 0).astimezone()
    night = datetime(2026, 7, 25, 2, 0).astimezone()

    assert select_due_backup_jobs(jobs=[job], latest_runs={}, local_now=afternoon) == []
    assert select_due_backup_jobs(jobs=[job], latest_runs={}, local_now=night) == [job]


# ---------------------------------------------------------------------------
# Secrets reach a backup script without living in the committed config
# ---------------------------------------------------------------------------
#
# The resolving moved from ``backup._script_env`` to ``spec_builder.backup_request_from_job`` when
# execution went down to ``common``: reading the secret store is the app's half of the split, and
# the spec below it carries values only. Same two rules, asserted where they now live.
def _job_and_target(**over):
    from db_ops.backup_restore.backup import BackupJob, BackupTarget
    from db_ops.lib.time_window import TimeWindow

    fields = dict(
        backup_id="CLOUD_MSSQL_HA_PRIMARY", job="full", db_type="sqlserver",
        server_id="S", script="assets/backup/sqlserver/mssql_backup_database.sh",
        backup_dir="/b", retention_days=14, time_window=TimeWindow(),
    )
    fields.update(over)
    target = BackupTarget(server_id="S", host="h", username="u", container_name="c",
                          port=22, key_file=None, password_ref=None)
    return BackupJob(**fields), target


def test_env_secrets_are_resolved_from_the_store_not_the_config():
    """A SQL Server backup needs a login password and an encryption passphrase. Putting
    either in `env` would put it in a committed file, so a job names the *ref* and the value
    is fetched at run time."""
    # The app builds the *request*; parsing it into a spec is `common`'s side of the boundary,
    # which this test may cross because a test is not an app.
    from db_ops.backup_restore.spec_builder import backup_request_from_job
    from db_ops.common.backup.spec import parse_backup_spec

    job, target = _job_and_target(
        env={"BACKUP_LEVEL": "full"},
        env_secrets={"MSSQL_PASSWORD": "MSSQL_HA_CLOUD_PASSWORD"},
    )

    spec = parse_backup_spec(backup_request_from_job(
        job, target=target, secrets={"MSSQL_HA_CLOUD_PASSWORD": "s3cret"}))

    assert spec.env["MSSQL_PASSWORD"] == "s3cret"
    assert spec.env["BACKUP_LEVEL"] == "full"
    assert "MSSQL_HA_CLOUD_PASSWORD" not in spec.env.values()   # the ref name is not the value


def test_a_missing_secret_ref_fails_the_job_instead_of_running_without_it():
    """Silently omitting the passphrase would produce an *unencrypted* backup that reports
    success — the failure would only surface when someone needs the encryption."""
    from db_ops.backup_restore.spec_builder import backup_request_from_job

    job, target = _job_and_target(
        env_secrets={"BACKUP_ENCRYPTION_PASSWORD": "TOKEN_203_0_113_188_BACKUP_ENC"})

    # Refused while the request is built, before anything is handed over — a missing secret must
    # never reach the CLI as an empty value.
    with pytest.raises(ValueError, match="TOKEN_203_0_113_188_BACKUP_ENC"):
        backup_request_from_job(job, target=target, secrets={})


def test_the_ssh_password_is_read_from_the_store_even_when_the_job_has_no_env_secrets(monkeypatch):
    """The SSH password is a secret the *spec* needs, so the store is read for it too.

    The store used to be read only when the job declared ``env_secrets``, which was true while
    ``execute_over_ssh`` resolved ``password_ref`` itself, lazily. Once the transport moved to
    ``common.backup`` the password had to be resolved up front into ``spec.host.password`` — and
    every password-auth job that decrypted nothing else built its spec against an empty store and
    failed on "password_ref ... is not in the secret store". That took out every ACME_* backup at
    once while the key-auth CLOUD_* ones kept running, because a key needs nothing decrypted.
    """
    from db_ops.backup_restore import backup as backup_module
    from db_ops.backup_restore.backup import BackupTarget, execute_backup_job

    job, _ = _job_and_target(env_secrets={})
    target = BackupTarget(server_id="S", host="192.0.2.249", username="tuser",
                          container_name="c", port=22, key_file=None,
                          password_ref="REMOTE_192_0_2_249_TUSER")

    monkeypatch.setattr(backup_module, "_load_secrets",
                        lambda **_: {"REMOTE_192_0_2_249_TUSER": "ssh-pw"})
    shipped = {}

    # Stubbed at the CLI boundary since 2026-08-15: the backup itself now runs in
    # `db_ops.common.cli backup-database`, so patching `common.backup.run_backup` would patch a
    # function in this process that nothing here calls. What is asserted is unchanged — the
    # resolved SSH password has to reach the request the app hands over.
    def fake_cli(command, request):
        shipped["command"], shipped["request"] = command, request
        return True, {"status": "done", "exit_code": 0, "duration_ms": 1,
                      "stdout": "", "stderr": "", "error": None}, ""

    monkeypatch.setattr(backup_module.common_cli, "run_allowing_failure", fake_cli)

    result = execute_backup_job(job, target=target)

    assert result.status == "done"
    assert shipped["command"] == "backup-database"
    assert shipped["request"]["host"]["password"] == "ssh-pw"


# ---------------------------------------------------------------------------
# One word for the operator, three vocabularies for the engines
# ---------------------------------------------------------------------------
def test_one_backup_type_word_maps_to_each_engines_own_level():
    """RMAN counts levels, pg_basebackup names them, SQL Server has native terms. Making the
    operator remember which is which is how a "full" backup gets taken as an incremental."""
    from db_ops.backup_restore.backup import backup_level_for

    assert backup_level_for("oracle", "full") == "0"
    assert backup_level_for("oracle", "diff") == "1"
    assert backup_level_for("postgresql", "full") == "full"
    assert backup_level_for("postgresql", "diff") == "incr"
    assert backup_level_for("sqlserver", "full") == "full"
    assert backup_level_for("sqlserver", "diff") == "diff"
    assert backup_level_for("sqlserver", "log") == "log"


def test_asking_an_engine_for_a_level_it_does_not_have_is_refused():
    """Oracle/PostgreSQL take their log backups through a different script, so `log` here is a
    mistake — passing an unrecognised value into a shell script would fail far from the cause."""
    from db_ops.backup_restore.backup import backup_level_for

    with pytest.raises(ValueError, match="no 'log' level"):
        backup_level_for("oracle", "log")
    with pytest.raises(ValueError, match="no 'log' level"):
        backup_level_for("postgresql", "log")


def test_the_scheduled_config_passes_no_level_so_the_script_still_decides():
    """The daily 1-5h entries must not hardcode a level: the script picks full on Sunday and
    diff otherwise. --backup-type exists for the manual override, not for the schedule."""
    from db_ops.backup_restore.backup import load_backup_jobs

    # The ids are read from the config rather than named — see the note in the sqlserver-levels
    # test above. What identifies these entries is what they are: the once-a-day `database` job
    # of an engine whose script derives its own level.
    daily = [j for j in load_backup_jobs(shipped_config("restore_config.json"))
             if j.job == "database" and j.db_type in ("oracle", "postgresql")]
    assert daily, "no engine-script database backup in the shipped config"

    for job in daily:
        assert "BACKUP_LEVEL" not in job.env, f"{job.backup_id} should let the script decide"
        assert job.time_window.from_hour == 1 and job.time_window.to_hour == 5


def test_force_skips_the_schedule_but_not_a_run_already_in_flight():
    """Conflating the two started two copies of the same restore against one database, each
    waiting on the other. --force is about the *schedule*."""
    from datetime import datetime, timedelta, timezone
    from db_ops.backup_restore import schedule

    def row(status, minutes_ago=1):
        started = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return {"status": status, "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ")}

    latest = {"j": row("RUNNING"), "k": row("DONE"), "m": row("ERROR")}

    assert schedule.is_running("j", latest) is True
    assert schedule.is_running("k", latest) is False
    assert schedule.is_running("m", latest) is False
    assert schedule.is_running("never-run", latest) is False


def _inactive_backup_ids(config_path: str) -> list[str]:
    """Backup ids currently switched off in the config, read straight from the file."""
    import json

    with open(config_path, encoding="utf-8") as handle:
        data = json.load(handle)
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("backup_id") and node.get("active") is False:
                found.append(str(node["backup_id"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found


def test_an_empty_config_says_so_rather_than_printing_a_bare_header():
    from db_ops.backup_restore.cli import _format_backup_list

    assert "No active backup entries" in _format_backup_list([])


def test_a_config_where_everything_is_off_says_so_instead_of_looking_empty():
    """"No active entries" plus the count is the difference between "nothing is configured"
    and "everything is switched off" - two very different things to be told at 3am."""
    from db_ops.backup_restore.cli import _format_backup_list

    class _Off:
        active = False

    text = _format_backup_list([_Off(), _Off()])

    assert "No active backup entries" in text
    assert "2 inactive backups hidden" in text


def _restore_entries(path=shipped_config("restore_config.json")):
    """Both loaders' entries, split active/inactive. The listing must account for all of them."""
    from db_ops.backup_restore.config import load_restore_configs
    from db_ops.backup_restore.restore_script import load_script_restores

    smb = load_restore_configs(path)
    scripts = load_script_restores(path)
    active = [e.restore_id for e in list(smb) + list(scripts) if e.active]
    inactive = [e.restore_id for e in list(smb) + list(scripts) if not e.active]
    return smb, scripts, active, inactive


