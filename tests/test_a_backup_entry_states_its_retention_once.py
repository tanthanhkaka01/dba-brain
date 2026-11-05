"""A backup entry states ``cleanup_retention`` once, for the folder its jobs write (0.35.0, 1.232).

The window belongs to the directory, and one entry is one directory (the plan of 2026-10-29,
§1.2). While it sat on the jobs, two jobs writing one folder could state two numbers for it, and
the shortest cut every type. So:

* the loader reads the entry's value for a job that states none; a job that states its own keeps it;
* a job and its entry stating different values is a check-objects violation, and refused by backup-add;
* ``upgrade-config`` step ``retention-per-type`` folds the jobs' values onto the entry: jobs that
  agree become the integer (nothing changes); jobs that differ become the per-type object - each
  job's value on the types it writes, the longest as default (D-R2) - and the change says so;
* an entry it cannot fold (a job whose types are not known, a job without a value) is a conflict, left as it is.
"""

from __future__ import annotations

import copy
import json

import pytest

from dbabrain.backup_restore.backup import load_backup_jobs
from dbabrain.lib import cleanup_retention as R
from dbabrain.lib import shared_objects

DAY = 86400
WINDOW = {"from_hour": 1, "to_hour": 4, "repeat_interval": 72000, "retry_interval": 1800, "timeout": 7200}


def _job(name, level, retention=None, script="assets/backup/sqlserver/mssql_backup_database.sh"):
    job = {"job": name, "script": script, "env": {"BACKUP_LEVEL": level}, "databases": ["APPDB"],
           "all_databases": False, "time_window": dict(WINDOW)}
    if retention is not None:
        job[R.FIELD] = retention
    return job


def _entry(jobs, **over):
    return {"backup_id": "ACME_APPDB", "server_id": "ACME-1", "db_type": "sqlserver", "backup_dir": "/b",
            "jobs": jobs, **over}


def test_jobs_that_agree_fold_into_the_integer_and_nothing_changes():
    entry = _entry([_job("full", "full", 14 * DAY), _job("log", "log", 14 * DAY)])
    changes, conflicts = R.fold_entry(entry, where="e")
    assert conflicts == [] and entry[R.FIELD] == 14 * DAY
    assert all(R.FIELD not in job for job in entry["jobs"]) and "unchanged" in changes[0]["effect"]


def test_jobs_that_differ_become_the_object_and_the_change_is_named():
    entry = _entry([_job("full", "full", 14 * DAY), _job("diff", "diff", 14 * DAY), _job("log", "log", 7 * DAY)])
    changes, _ = R.fold_entry(entry, where="e")
    assert entry[R.FIELD] == {"FULL": 14 * DAY, "DIFF": 14 * DAY, "LOG": 7 * DAY, "default": 14 * DAY}
    assert changes[0]["effect"].startswith("CHANGES: the shortest job window")


def test_postgresql_and_oracle_jobs_give_their_chains_and_logs():
    pg = _entry([_job("database_full", "full", 14 * DAY, "assets/backup/postgresql/pg_basebackup_database.sh"),
                 _job("database", "incr", 14 * DAY, "assets/backup/postgresql/pg_basebackup_database.sh"),
                 _job("wal", "", 7 * DAY, "assets/backup/postgresql/pg_archive_wal.sh")], db_type="postgresql")
    R.fold_entry(pg, where="pg")
    assert pg[R.FIELD]["FULL_BASE"] == pg[R.FIELD]["INCR_BASE"] == 14 * DAY and pg[R.FIELD]["WAL"] == 7 * DAY
    ora = _entry([_job("database_full", "0", 14 * DAY, "assets/backup/oracle/oracle_rman_database.sh"),
                  _job("archivelog", "", 2 * DAY, "assets/backup/oracle/oracle_rman_archivelog.sh")], db_type="oracle")
    R.fold_entry(ora, where="ora")
    assert ora[R.FIELD]["RMAN_L0"] == 14 * DAY and ora[R.FIELD]["ARCHIVELOG"] == 2 * DAY


@pytest.mark.parametrize("jobs", [
    [_job("full", "full", 14 * DAY), _job("odd", "weird", 7 * DAY)],
    [_job("full", "full", 14 * DAY), _job("log", "log")],
])
def test_an_entry_it_cannot_fold_is_a_conflict_left_as_it_is(jobs):
    entry = _entry(copy.deepcopy(jobs))
    before = copy.deepcopy(entry)
    changes, conflicts = R.fold_entry(entry, where="e")
    assert changes == [] and conflicts and entry == before


def test_the_loader_reads_the_entry_for_a_job_that_states_none(tmp_path):
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"backups": [
        _entry([_job("full", "full"), _job("log", "log", 3 * DAY)], **{R.FIELD: 9 * DAY})]}}), encoding="utf-8")
    jobs = {job.job: job for job in load_backup_jobs(path)}
    assert jobs["full"].cleanup_retention == 9 * DAY and jobs["log"].cleanup_retention == 3 * DAY


def test_check_objects_names_a_job_and_an_entry_that_disagree(tmp_path):
    (tmp_path / "restore_config.json").write_text(json.dumps({"backup_restore": {"backups": [
        _entry([_job("full", "full", 9 * DAY), _job("log", "log", 3 * DAY)], **{R.FIELD: 9 * DAY})]}}),
        encoding="utf-8")
    found = shared_objects.retention_disagreements(tmp_path)
    assert len(found) == 1 and "jobs[log]" in found[0]["where"] and "two answers" in found[0]["detail"]



def _missing_retention(root):
    return [v for v in shared_objects.check_data_dir(root)["violations"]
            if v["field"] == R.FIELD and v["kind"] == "missing"]


def test_a_folded_entry_is_not_reported_missing_on_its_jobs(tmp_path):
    """Found on the worker right after the 0.35 deploy: the fold left every job without the field and
    check-objects reported 49 missing. The jobs' site inherits it from the entry (inherited_from)."""
    (tmp_path / "restore_config.json").write_text(json.dumps({"backup_restore": {"backups": [
        _entry([_job("full", "full"), _job("log", "log")], **{R.FIELD: 9 * DAY})]}}), encoding="utf-8")
    assert _missing_retention(tmp_path) == []


def test_a_job_with_no_retention_anywhere_is_still_missing_it(tmp_path):
    (tmp_path / "restore_config.json").write_text(json.dumps({"backup_restore": {"backups": [
        _entry([_job("full", "full")])]}}), encoding="utf-8")
    assert len(_missing_retention(tmp_path)) == 1
