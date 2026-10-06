"""A retention under a day is applied - by each engine's own means - and judged on the right clock.

The lab backups of the soak declare ``cleanup_retention: 7200`` - two hours, because a cycle of
full, differential, logs and a restore runs every hour. Asked on 2026-10-02 whether that retention
did its job, the answer read off the previous soak was no:

* **nothing applied it.** A backup script takes ``RETENTION_DAYS``, whole days; under a day it was
  handed nothing and kept its own default of fourteen. One lab host held 77 GB after 31 hours.
* **the one command that speaks seconds judged on the wrong clock.** ``finished_at`` is what the
  server printed and carries no zone; the cutoff was this node's wall clock. A node at +08 over a
  container on UTC read a backup one minute old as eight hours old.
* **and that command could not delete a PostgreSQL backup at all**, which is a directory: it
  planned, then failed on every path.

The owner's ruling the same day: *Oracle and PostgreSQL follow Oracle's and PostgreSQL's own way -
Oracle uses RMAN* - and Oracle keeps to the two hours too. So the window travels to the script as
``RETENTION_SECONDS`` beside ``RETENTION_DAYS``, and each engine applies it its own way:

* SQL Server - file age in minutes, never past the newest full;
* PostgreSQL - whole chains on the UTC stamps in their names; the WAL archive follows through
  ``pg_archivecleanup``;
* Oracle - RMAN's ``DELETE BACKUP COMPLETED BEFORE``, after a level 0 that succeeded in the same
  run and never reaching into that run. RMAN's recovery window is whole days, so the policy is one
  day and the sweep is what makes it two hours.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from db_ops.backup_restore import prune, spec_builder
from db_ops.backup_restore.backup import BackupJob
from db_ops.common import cli_backup_files
from db_ops.common.backupfiles import age_seconds, list_backup_files
from db_ops.lib.backupfiles_retention import AGE, KEEP, OBSOLETE, RECOVERY_WINDOW, plan_retention
from db_ops.lib.time_window import parse_time_window_config

SCRIPTS = Path(spec_builder.__file__).resolve().parents[1] / "common" / "backup_scripts"

#: The node's wall clock in the lab: 17:43 at +08 is 09:43 UTC.
NODE_NOW = datetime(2026, 10, 2, 17, 43, 0, tzinfo=timezone(timedelta(hours=8)))


def _job(seconds, db_type="sqlserver"):
    return BackupJob(backup_id="LAB_FULL", job="full", db_type=db_type, server_id="LAB-SQL",
                     script="assets/backup/sqlserver/mssql_backup_database.sh",
                     backup_dir="/opt/db_ops/backup/LAB", cleanup_retention=seconds,
                     time_window=parse_time_window_config({}, context="test").time_window,
                     env_secrets={"MSSQL_PASSWORD": "LAB_SA"})


def _row(kind, finished, *, age=None, path=None, database=None):
    row = {"path": path or f"/b/{kind}_{finished[11:].replace(':', '')}.bak", "kind": kind,
           "database_name": database, "size_bytes": 10, "finished_at": finished}
    if age is not None:
        row["age_seconds"] = age
    return row


def _verdicts(plan):
    return {row["path"]: row["verdict"] for row in plan["obsolete"] + plan["keep"]}


# --------------------------------------------------------------------------- #
# The window reaches the script
# --------------------------------------------------------------------------- #
def test_a_window_under_a_day_is_one_day_in_days_and_itself_in_seconds():
    """Never 0 days, which a script reads as delete everything, and no longer nothing, which was 14."""
    assert (_job(7200).retention_days, _job(7200).retention_seconds) == (1, 7200)
    assert (_job(86399).retention_days, _job(86399).retention_seconds) == (1, 86399)


def test_a_window_in_days_is_handed_over_exactly_as_it_always_was():
    assert (_job(86400).retention_days, _job(86400).retention_seconds) == (1, None)
    assert (_job(691200).retention_days, _job(691200).retention_seconds) == (8, None)


def test_no_age_gate_is_not_a_window():
    assert (_job(0).retention_days, _job(0).retention_seconds) == (None, None)


@pytest.mark.parametrize("seconds, days, exact", [(7200, "1", "7200"), (691200, "8", None), (0, None, None)])
def test_the_request_carries_the_seconds_beside_the_days(monkeypatch, tmp_path, seconds, days, exact):
    script = tmp_path / "backup.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(spec_builder, "_script_path", lambda *args, **kwargs: script)
    target = SimpleNamespace(container_name="LAB", host="192.0.2.10", port=22, username="u",
                             key_file=None, password_ref="", platform="linux", access="ssh", ssl=False)
    env = spec_builder.backup_request_from_job(_job(seconds), target=target, secrets={"LAB_SA": "x"})["env"]
    assert env.get("RETENTION_DAYS") == days
    assert env.get("RETENTION_SECONDS") == exact


# --------------------------------------------------------------------------- #
# Each engine applies it its own way
# --------------------------------------------------------------------------- #
def _script(relative):
    return (SCRIPTS / relative).read_text(encoding="utf-8")


def test_sqlserver_counts_the_files_own_age_in_minutes_and_stops_at_the_newest_full():
    text = _script("sqlserver/mssql_backup_database.sh")
    assert 'retention_seconds="${RETENTION_SECONDS:-}"' in text
    assert 'age_test="-mmin +$(( retention_seconds / 60 ))"' in text
    assert "${age_test} ! -newer '${newest_full}' -delete" in text
    # the day rule is the default it always was
    assert 'age_test="-mtime +${retention_days}"' in text


def test_sqlserver_on_windows_follows_the_same_rule():
    text = _script("sqlserver/mssql_backup_database.ps1")
    assert "$retentionSeconds = $env:RETENTION_SECONDS" in text
    assert "(Get-Date).AddSeconds(-1 * [int]$retentionSeconds)" in text
    assert "$_.LastWriteTime -lt $newestFull.LastWriteTime" in text


def test_sqlserver_says_what_its_window_removed_on_both_platforms():
    """PostgreSQL prints `retention: removing <dir>` and Oracle `Deleted n objects`; the SQL Server
    job deleted silently, so only the host could show the window had been applied (0.27.0 item
    1.91). A file is counted once it is gone: `-delete -print`, and on Windows a file still there
    after Remove-Item is not counted."""
    linux = _script("sqlserver/mssql_backup_database.sh")
    assert "! -newer '${newest_full}' -delete -print 2>/dev/null | wc -l" in linux
    assert "retention: %s removed %s file(s) past the window (%s), below its newest full %s" in linux
    windows = _script("sqlserver/mssql_backup_database.ps1")
    assert "if (-not (Test-Path -LiteralPath $file.FullName)) { $removed++ }" in windows
    assert ('"retention: $db removed $removed file(s) past the window ($retentionWindow), below its '
            'newest full $($newestFull.Name)"') in windows


def test_postgresql_drops_whole_chains_on_a_nearer_cutoff_not_single_backups():
    text = _script("postgresql/pg_basebackup_database.sh")
    assert 'retention_window="${retention_seconds} seconds ago"' in text
    assert 'date -u -d "${retention_window}"' in text
    # still the chain sweep: a chain goes only when its NEWEST member is past the cutoff
    assert 'if [ "\\$newest" \\< "\\$cutoff" ]; then' in text


def test_the_postgresql_wal_archive_is_trimmed_by_pg_archivecleanup_not_by_age():
    text = _script("postgresql/pg_archive_wal.sh")
    assert "pg_archivecleanup '${wal_dir}' '${start_wal}'" in text
    # age is only the fallback before the first base backup exists - and it reads the seconds too
    assert "find '${wal_dir}' -type f ${age_test} -delete" in text
    assert 'age_test="-mmin +$(( retention_seconds / 60 ))"' in text


def test_the_backup_history_files_go_with_the_wal_they_describe():
    """Each base backup leaves `<segment>.<offset>.backup`, and pg_archivecleanup keeps those unless
    told - 129 on the lab after three days, and `wal_files_kept=` read as WAL growing when only they
    were (0.27.0 item 1.90). Read on the lab before this was written: `-n -b` and the name-ordered
    cut below choose the same 129 files and no segment, and keep the retained chains' five."""
    text = _script("postgresql/pg_archive_wal.sh")
    # PostgreSQL 17 and later: the tool does it, when it says it can
    assert "pg_archivecleanup --help 2>&1 | grep -q -- --clean-backup-history" in text
    assert "pg_archivecleanup -b '${wal_dir}' '${start_wal}'" in text
    # before 17: the same cut by hand, on the name without its timeline, as the tool compares
    assert r"substr(\$0, 9, 16) < substr(cut, 9, 16)" in text
    # and the run says what went
    assert "backup_history_removed=%s backup_history_kept=%s" in text


def test_oracle_removes_backups_through_rman_only_after_a_level_zero_that_succeeded():
    text = _script("oracle/oracle_rman_database.sh")
    failed = text.index("RESULT=error backup_level=%s rman_rc=%s")
    sweep = text.index('if [ -n "$retention_seconds" ] && [ "$level" = "0" ]; then')
    assert failed < sweep  # a failed backup has already left the script
    # the configured window, never a literal - and less the time this run took, so a backup that
    # takes longer than the window keeps its own first sets
    assert ("DELETE NOPROMPT BACKUP COMPLETED BEFORE "
            "'SYSDATE-${retention_seconds}/86400-${run_elapsed}/86400';") in text
    assert "run_elapsed=$(( $(date +%s) - run_started ))" in text
    # the policy itself stays RMAN's and in days
    assert "CONFIGURE RETENTION POLICY TO RECOVERY WINDOW OF ${retention_days} DAYS;" in text
    assert "DELETE NOPROMPT OBSOLETE;" in text


def test_an_oracle_database_backup_carries_its_own_archived_logs():
    """A level 0 taken with the database open needs the redo written while it was taken, and that
    redo waited in the online log for the next archivelog job. On the 0.26.0 lab (2026-10-04) a
    restore right after a level 0 fell back to the level 0 before it, and failed while the copy
    held only the new one. The backup now archives the current log and backs up every log not yet
    backed up - after the datafiles, before the controlfile that records them."""
    text = _script("oracle/oracle_rman_database.sh")
    datafiles = text.index("BACKUP INCREMENTAL LEVEL ${level} DATABASE TAG 'DBOPS_L${level}';")
    switch = text.index("SQL 'ALTER SYSTEM ARCHIVE LOG CURRENT';")
    logs = text.index("BACKUP ARCHIVELOG ALL NOT BACKED UP 1 TIMES TAG 'DBOPS_ARCH' "
                      "FORMAT '${backup_dir}/arch_%d_%T_%U.bkp';")
    controlfile = text.index("BACKUP CURRENT CONTROLFILE TAG 'DBOPS_CTL';")
    assert datafiles < switch < logs < controlfile


def test_a_failed_oracle_sweep_is_a_warning_and_not_the_backups_verdict():
    text = _script("oracle/oracle_rman_database.sh")
    sweep = text[text.index('if [ -n "$retention_seconds" ] && [ "$level" = "0" ]; then'):]
    sweep = sweep[:sweep.index("\nfi\n")]
    assert "warning: the retention sweep" in sweep
    assert "exit 1" not in sweep and "die " not in sweep


def test_oracle_archived_logs_leave_the_disk_on_the_window_and_their_backups_wait_for_the_level_zero():
    text = _script("oracle/oracle_rman_archivelog.sh")
    assert '[ -n "$retention_seconds" ] && log_age="${retention_seconds}/86400"' in text
    assert "DELETE NOPROMPT ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-${log_age}';" in text
    # never ahead of the level 0 that makes them unnecessary: this line stays in days
    assert "DELETE NOPROMPT BACKUP OF ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-${retention_days}';" in text


# --------------------------------------------------------------------------- #
# The age of a file, on one clock
# --------------------------------------------------------------------------- #
def test_an_age_is_the_difference_of_two_instants_read_off_one_clock():
    assert age_seconds("2026-10-02 09:42:33", "2026-10-02 09:43:03") == 30
    assert age_seconds(datetime(2026, 10, 2, 9, 0, 0), datetime(2026, 10, 2, 11, 0, 0)) == 7200
    assert age_seconds("2026-10-02T09:42:33.250000", "2026-10-02 09:42:40") == 7


def test_an_unknown_instant_gives_no_age_and_a_clock_that_stepped_back_gives_zero():
    """An unknown age is not an old one, and a negative one would read as "not yet written"."""
    assert age_seconds(None, "2026-10-02 09:43:03") is None
    assert age_seconds("2026-10-02 09:42:33", "") is None
    assert age_seconds("06-AUG-26", "2026-10-02 09:43:03") is None
    assert age_seconds("2026-10-02 09:43:03", "2026-10-02 09:42:33") == 0


def test_a_backup_a_minute_old_on_a_utc_host_is_inside_two_hours_on_a_plus_eight_node():
    """The lab, 2026-10-02: the cutoff came out as 15:43 and the file, stamped 09:42, went."""
    files = [_row("full", "2026-10-02 09:42:00", age=60)]
    plan = plan_retention(files, retention_seconds=7200, now=NODE_NOW)
    assert plan["cutoff"] == "2026-10-02 07:43:00"
    assert _verdicts(plan) == {files[0]["path"]: KEEP}
    assert "inside the 7200-second window" in plan["keep"][0]["reason"]


def test_a_backup_three_hours_old_goes_whatever_zone_the_node_shows():
    files = [_row("full", "2026-10-02 06:40:00", age=3 * 3600 + 120),
             _row("full", "2026-10-02 09:42:00", age=60)]
    plan = plan_retention(files, retention_seconds=7200, now=NODE_NOW)
    assert _verdicts(plan) == {files[0]["path"]: OBSOLETE, files[1]["path"]: KEEP}


def test_a_listing_that_states_no_age_is_still_judged_on_the_operators_clock():
    """A share listed by something that cannot ask the machine the time: the rule it always had."""
    files = [_row("full", "2026-10-02 09:42:00"), _row("full", "2026-10-02 17:42:00")]
    plan = plan_retention(files, retention_seconds=7200, now=NODE_NOW)
    assert plan["cutoff"] == "2026-10-02 15:43:00"
    assert _verdicts(plan) == {files[0]["path"]: OBSOLETE, files[1]["path"]: KEEP}


def test_a_window_in_days_is_measured_on_the_files_own_clock_too():
    """Eight hours in fourteen days went unnoticed; it was the same error."""
    files = [_row("full", "2026-09-18 09:50:00", age=14 * 86400 - 420),
             _row("full", "2026-10-02 09:42:00", age=60)]
    plan = plan_retention(files, retention_days=14, now=NODE_NOW)
    assert plan["cutoff"] == "2026-09-18 09:43:00"
    assert _verdicts(plan)[files[0]["path"]] == KEEP


def test_the_two_rules_still_differ_only_at_the_full_the_window_reaches_back_to():
    files = [
        _row("full", "2026-10-02 06:00:00", age=13380, database="LABTEST"),
        _row("full", "2026-10-02 07:00:00", age=9780, database="LABTEST"),
        _row("diff", "2026-10-02 07:10:00", age=9180, database="LABTEST"),
        _row("full", "2026-10-02 08:00:00", age=6180, database="LABTEST"),
        _row("full", "2026-10-02 09:00:00", age=2580, database="LABTEST"),
    ]
    window = _verdicts(plan_retention(files, retention_seconds=7200, mode=RECOVERY_WINDOW, now=NODE_NOW))
    assert [window[row["path"]] for row in files] == [OBSOLETE, KEEP, KEEP, KEEP, KEEP]
    age = _verdicts(plan_retention(files, retention_seconds=7200, mode=AGE, now=NODE_NOW))
    assert [age[row["path"]] for row in files] == [OBSOLETE, OBSOLETE, OBSOLETE, KEEP, KEEP]


# --------------------------------------------------------------------------- #
# Each listing asks the machine what time it is
# --------------------------------------------------------------------------- #
def test_postgresql_reads_the_hosts_clock_in_the_same_listing(monkeypatch):
    listing = (
        "/b/base/20261002T094214Z_FULL|4096|2026-10-02 09:42:14.281890803 +0000\n"
        "/b/wal|4096|2026-10-02 09:42:17.050897987 +0000\n"
        "__HOST_NOW__|2026-10-02 09:43:14\n"
    )
    seen = {}

    def fake_run(host, command, **kwargs):
        seen["command"] = command
        return {"stdout": listing}

    monkeypatch.setattr("db_ops.common.backupfiles.postgresql.run", fake_run)
    files = list_backup_files({"db_type": "postgresql", "path": "/b",
                               "host": {"runtime": "docker", "container": "pg"}})["files"]
    assert "date '+%Y-%m-%d %H:%M:%S'" in seen["command"]
    assert [(row["kind"], row["age_seconds"]) for row in files] == [("full", 60), ("log", 57)]


def test_oracle_reads_the_clock_of_the_shell_rman_ran_in(monkeypatch):
    listing = (
        "BS Key  Type LV Size       Device Type Elapsed Time Completion Time\n"
        "------- ---- -- ---------- ----------- ------------ -------------------\n"
        "7       Incr 0  1.20G      DISK        00:00:45     2026-10-02 09:42:33\n"
        "        BP Key: 7   Status: AVAILABLE  Compressed: NO  Tag: L0\n"
        "        Piece Name: /b/FREE_L0_7.bkp\n"
        "__HOST_NOW__ 2026-10-02 09:43:03\n"
    )
    seen = {}

    def fake_run(host, command, **kwargs):
        seen["command"] = command
        return {"stdout": listing}

    monkeypatch.setattr("db_ops.common.backupfiles.oracle.run", fake_run)
    files = list_backup_files({"db_type": "oracle", "path": "/b",
                               "host": {"runtime": "docker", "container": "ora"}})["files"]
    assert "__HOST_NOW__ $(date" in seen["command"]
    assert [(row["path"], row["age_seconds"]) for row in files] == [("/b/FREE_L0_7.bkp", 30)]


def test_sqlserver_asks_the_instance_for_its_own_now(monkeypatch):
    """BackupFinishDate and GETDATE() are the same clock, and neither carries a zone."""
    from db_ops.common.backupfiles import sqlserver

    asked = []

    def fake_rows(cursor, statement):
        asked.append(statement)
        if "dm_os_enumerate_filesystem" in statement:
            return [{"path": "/b/LABTEST/FULL/x.bak", "size": 100}]
        if "GETDATE()" in statement:
            return [{"server_now": datetime(2026, 10, 2, 9, 44, 12)}]
        return [{"BackupType": 1, "DatabaseName": "LABTEST", "FirstLSN": 1, "LastLSN": 2,
                 "BackupFinishDate": datetime(2026, 10, 2, 9, 42, 12)}]

    connection = SimpleNamespace(cursor=lambda: object(), close=lambda: None)
    monkeypatch.setattr("db_ops.common.db_connect.connect_engine", lambda **kwargs: connection)
    monkeypatch.setattr(sqlserver.sql_run, "query_rows", fake_rows)
    rows = sqlserver.list_files({"path": "/b", "target": {"host": "h", "username": "sa", "password": "p"}})
    assert any("GETDATE()" in statement for statement in asked)
    assert [(row["database_name"], row["age_seconds"]) for row in rows] == [("LABTEST", 120)]


# --------------------------------------------------------------------------- #
# The report command: SQL Server by the job's own login, PostgreSQL never deleted
# --------------------------------------------------------------------------- #
@pytest.fixture
def lab(tmp_path):
    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": "LAB-SQL", "db_type": "sqlserver", "ip": "192.0.2.10", "port": 1433}]}), encoding="utf-8")
    return tmp_path


def test_a_sqlserver_job_is_listed_through_the_instance_with_its_own_login(lab):
    job = _job(7200)
    login = prune.sql_login(job, secrets={"LAB_SA": "sa-pw"}, data_dir=lab)
    assert login == {"host": "192.0.2.10", "port": 1433, "username": "sa", "password": "sa-pw"}
    named = BackupJob(**{**job.__dict__, "env": {"MSSQL_USER": "backup_op"}})
    assert prune.sql_login(named, secrets={"LAB_SA": "sa-pw"}, data_dir=lab)["username"] == "backup_op"
    assert prune.not_listable(job, secrets={"LAB_SA": "sa-pw"}, data_dir=lab) == ""


def test_a_sqlserver_job_that_states_no_login_is_named_not_half_attempted(lab):
    job = BackupJob(**{**_job(7200).__dict__, "env_secrets": {}})
    assert prune.sql_login(job, secrets={}, data_dir=lab) is None
    assert "states no login" in prune.not_listable(job, secrets={}, data_dir=lab)


def test_the_request_for_a_sqlserver_job_carries_the_login_and_no_other_engine_does(lab, monkeypatch):
    monkeypatch.setattr(spec_builder, "_resolved_key_file", lambda *args, **kwargs: "")
    target = SimpleNamespace(container_name="LAB", host="192.0.2.10", port=22, username="u",
                             key_file=None, password_ref="")
    request = prune.prune_job_request(_job(7200), target=target, secrets={"LAB_SA": "sa-pw"}, data_dir=lab)
    assert request["target"]["username"] == "sa" and request["cleanup_retention"] == 7200
    other = prune.prune_job_request(_job(7200, db_type="oracle"), target=target, secrets={}, data_dir=lab)
    assert "target" not in other


def test_the_common_command_refuses_to_delete_a_postgresql_backup_by_name(monkeypatch, capsys):
    """It planned and then failed on every path: a base backup is a directory."""
    listed = {"files": [
        {"path": "/b/base/20261002T060000Z_FULL", "kind": "full", "database_name": None,
         "finished_at": "2026-10-02 06:00:00", "age_seconds": 13380},
        {"path": "/b/base/20261002T090000Z_FULL", "kind": "full", "database_name": None,
         "finished_at": "2026-10-02 09:00:00", "age_seconds": 2580},
    ]}
    monkeypatch.setattr("db_ops.common.backupfiles.list_backup_files", lambda request: listed)
    monkeypatch.setattr("db_ops.common.deletefiles.delete_files",
                        lambda request: pytest.fail("nothing may be deleted"))
    code = cli_backup_files._prune({"db_type": "postgresql", "path": "/b", "cleanup_retention": 7200,
                                    "delete": True, "host": {}})
    answer = json.loads(capsys.readouterr().out)
    assert code != 0 and answer["success"] is False
    assert "delete is not available for postgresql" in answer["message"]
    assert answer["data"]["counts"]["obsolete"] == 1 and answer["data"]["deleted"] is None
