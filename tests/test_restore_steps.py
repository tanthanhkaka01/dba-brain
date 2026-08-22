"""Applying one named backup, and the ways each engine's version of that differs.

`restore-database` decides a whole chain. These are the level below it: the caller already chose
the file and asks for exactly that, so a recovery can watch each step land before deciding the
next. The three engines do not mean the same thing by it, and the tests here pin the differences
rather than a pretence that they match.

Two rules carry the most weight, both because getting them wrong is silent:

* `with_recovery` defaults to FALSE. A database recovered early cannot take the remaining logs at
  all - the only fix is to start the whole restore again - so the safe answer is the one a caller
  gets without asking.
* `STOPAT` on anything but a log is refused. SQL Server accepts it on a full or a differential and
  ignores it, which reads as a point-in-time restore that never happened.
"""

from __future__ import annotations

import pytest

from db_ops.common.restorestep import RestoreStepError, backup_paths, restore_step
from db_ops.common.restorestep import sqlserver as mssql


def _mssql(**over):
    return dict({"db_type": "sqlserver", "database": "SALESDB_STG", "dry_run": True,
                 "backup_path": "/b/a.bak", "target": {"host": "h"}}, **over)


# --------------------------------------------------------------------------- #
# The request.
# --------------------------------------------------------------------------- #

def test_one_file_or_several_but_not_both():
    with pytest.raises(RestoreStepError, match="not both"):
        backup_paths({"backup_path": "/b/a.bak", "backup_paths": ["/b/b.bak"]})


def test_a_step_with_no_file_named_is_refused():
    with pytest.raises(RestoreStepError, match="backup_path"):
        backup_paths({})


def test_a_bare_string_where_an_array_belongs_is_refused():
    with pytest.raises(RestoreStepError, match="must be an array"):
        backup_paths({"backup_paths": "/b/a.bak"})


def test_an_unknown_engine_is_refused_by_name():
    with pytest.raises(RestoreStepError, match="db_type must be"):
        restore_step("full", {"db_type": "mysql", "backup_path": "/b/a"})


# --------------------------------------------------------------------------- #
# SQL Server: the recovery flag and STOPAT.
# --------------------------------------------------------------------------- #

def test_recovery_is_off_unless_asked_for():
    """The safe answer is the default: a chain that recovers early has to be started again."""
    statements = mssql.build_statements("full", _mssql(), ["/b/a.bak"])
    assert "NORECOVERY" in statements[0] and "WITH RECOVERY" not in statements[0]


def test_only_the_last_file_recovers_when_recovery_is_asked_for():
    statements = mssql.build_statements(
        "log", _mssql(with_recovery=True), ["/b/1.trn", "/b/2.trn", "/b/3.trn"])
    assert all("NORECOVERY" in s for s in statements[:-1])
    assert "NORECOVERY" not in statements[-1]


def test_stopat_on_a_full_is_refused_rather_than_ignored():
    """SQL Server accepts it there and silently does nothing, which reads as a point-in-time
    restore that never happened."""
    with pytest.raises(RestoreStepError, match="log restore only"):
        mssql.build_statements("full", _mssql(stopat="2026-08-07 01:40:00"), ["/b/a.bak"])


def test_stopat_lands_on_the_last_log_only():
    statements = mssql.build_statements(
        "log", _mssql(stopat="2026-08-07 01:40:00"), ["/b/1.trn", "/b/2.trn"])
    assert "STOPAT" not in statements[0]
    assert "STOPAT = N'2026-08-07 01:40:00'" in statements[1]


def test_replace_appears_on_the_full_and_nowhere_else():
    """REPLACE means "overwrite the existing database" - a statement about starting a chain."""
    full = mssql.build_statements("full", _mssql(), ["/b/a.bak"])
    logs = mssql.build_statements("log", _mssql(), ["/b/1.trn"])
    assert "REPLACE" in full[0]
    assert "REPLACE" not in logs[0]


def test_move_is_rejected_on_a_log():
    with pytest.raises(RestoreStepError, match="full restore only"):
        mssql.build_statements("log", _mssql(move={"A": "/d/a.mdf"}), ["/b/1.trn"])


def test_a_log_uses_restore_log_not_restore_database():
    assert mssql.build_statements("log", _mssql(), ["/b/1.trn"])[0].startswith("RESTORE LOG")


def test_identifiers_and_paths_are_quoted():
    weird_path = "o" + chr(39) + "brien.bak"
    statements = mssql.build_statements("full", _mssql(database="we[ird]"), [weird_path])
    assert "[we[ird]]]" in statements[0]
    assert "N" + chr(39) + "o" + chr(39) * 2 + "brien.bak" + chr(39) in statements[0]


def test_a_database_must_be_named():
    with pytest.raises(RestoreStepError, match="database is required"):
        mssql.build_statements("full", {"target": {"host": "h"}}, ["/b/a.bak"])


# --------------------------------------------------------------------------- #
# Oracle: a path is offered to RMAN, not applied.
# --------------------------------------------------------------------------- #

def _ora(level, **over):
    """In-place restore mode. Duplicate is the default, so it is stated when testing the other."""
    return restore_step(level, dict(
        {"db_type": "oracle", "dry_run": True, "backup_path": "/b/a.bkp", "mode": "restore",
         "host": {"runtime": "docker", "host": "h", "container": "c"}}, **over))


def _dup(**over):
    return restore_step("full", dict(
        {"db_type": "oracle", "dry_run": True, "backup_path": "/b/a.bkp",
         "backup_location": "/b", "oracle_sid": "FREE",
         "host": {"runtime": "docker", "host": "h", "container": "c"}}, **over))


def _script(result, name):
    """The script of one step, decoded back out of the base64 it travels as."""
    import base64
    import re

    command = result["scripts"][name]
    return base64.b64decode(re.search(r"printf %s '?([A-Za-z0-9+/=]+)'?",
                                      command).group(1)).decode()


def test_duplicate_is_the_default_because_a_drill_is_a_different_instance():
    """DUPLICATE builds a NEW database with its own DBID; RESTORE puts a database back into
    ITSELF and needs a matching DBID, so it cannot cross instances at all."""
    result = _dup()
    assert result["mode"] == "duplicate"
    assert "DUPLICATE DATABASE TO FREE" in _script(result, "duplicate")
    assert "NOFILENAMECHECK" in _script(result, "duplicate")


def test_duplicate_takes_the_instance_to_nomount_not_mount():
    """DUPLICATE builds its own controlfile and needs the instance not to have one."""
    assert "STARTUP NOMOUNT;" in _script(_dup(), "nomount")


def test_the_shutdown_is_abort_and_never_immediate():
    """Measured: on 2026-08-05 an `immediate` stopped at `alter pluggable database all close
    immediate` and never returned, and the 02:00 drill hung until morning. Nothing bounds it -
    the entry timeout marks the run row, it does not kill the shell."""
    for result, step in ((_dup(), "nomount"), (_ora("full"), "mount")):
        script = _script(result, step)
        assert "SHUTDOWN ABORT;" in script
        assert "IMMEDIATE" not in script.upper()


def test_set_decryption_is_outside_the_run_block():
    """Inside one, RMAN answers RMAN-03032. Verified on 23.26.2; wrapping both in RUN{} does not
    help."""
    script = _script(_ora("full", encryption_password="s3cret"), "restore")
    assert script.index("SET DECRYPTION") < script.index("RUN {")


def test_a_quote_in_the_passphrase_is_refused_rather_than_breaking_the_script():
    with pytest.raises(RestoreStepError, match="single quote"):
        _ora("full", encryption_password="pa" + chr(39) + "ss")


def test_duplicate_needs_a_backup_location_and_a_sid():
    with pytest.raises(RestoreStepError, match="backup_location is required"):
        restore_step("full", {"db_type": "oracle", "dry_run": True, "backup_path": "/b/a.bkp",
                              "oracle_sid": "FREE",
                              "host": {"runtime": "docker", "host": "h", "container": "c"}})
    with pytest.raises(RestoreStepError, match="oracle_sid is required"):
        restore_step("full", {"db_type": "oracle", "dry_run": True, "backup_path": "/b/a.bkp",
                              "backup_location": "/b",
                              "host": {"runtime": "docker", "host": "h", "container": "c"}})


def test_duplicate_has_no_separate_diff_or_log_step():
    """RMAN picks the level 0, the incrementals and the archived logs itself; offering a diff step
    would describe something it did not do."""
    with pytest.raises(RestoreStepError, match="no separate diff or log step"):
        restore_step("diff", {"db_type": "oracle", "dry_run": True, "backup_path": "/b/a.bkp",
                              "backup_location": "/b", "oracle_sid": "FREE",
                              "host": {"runtime": "docker", "host": "h", "container": "c"}})


def test_duplicate_connects_as_the_auxiliary():
    """`DUPLICATE ... BACKUP LOCATION` restores INTO this instance, so it is the AUXILIARY; a
    plain `target /` would be the database being duplicated FROM."""
    assert "rman auxiliary /" in _dup()["scripts"]["duplicate"]


def test_success_is_the_banner_not_the_exit_code():
    """RMAN prints error stacks and still exits 0."""
    from db_ops.common.restorestep.oracle import _failed

    assert _failed("duplicate", "RMAN-06136: ...", 0) is True
    assert _failed("duplicate", "Finished Duplicate Db at 07-AUG-26", 0) is False


def test_oracle_catalogs_the_piece_before_asking_rman_to_restore():
    """RMAN restores from its catalogue, never from a path handed to it."""
    script = _script(_ora("full"), "restore")
    assert "CATALOG BACKUPPIECE " in script and "/b/a.bkp" in script
    assert "RESTORE DATABASE;" in script


def test_oracle_diff_and_log_are_recovery_not_restore():
    """Applying incrementals *is* recovery to RMAN; there is no separate differential step."""
    for level in ("diff", "log"):
        assert "RECOVER DATABASE;" in _script(_ora(level), "recover")


def test_oracle_stopat_becomes_set_until_time():
    assert "SET UNTIL TIME" in _script(_ora("log", stopat="2026-08-07 01:40:00"), "recover")


def test_only_a_recovering_step_opens_the_database():
    """`with_recovery` means the same as it does for SQL Server: this is the last step."""
    assert "open" not in _ora("diff")["steps"]
    result = _ora("log", with_recovery=True)
    assert result["steps"][-1] == "open"
    assert "OPEN RESETLOGS" in _script(result, "open")


def test_scripts_travel_as_base64_never_as_shell_text():
    """`v$database` became `v` more than once while this was built: sh -c -> docker exec -> sh -lc
    is three chances for a shell to eat a `$`, and it reports as ORA-00942."""
    command = _ora("full")["scripts"]["restore"]
    assert "base64 -d" in command
    assert "CATALOG" not in command      # the SQL itself never appears on any command line


# --------------------------------------------------------------------------- #
# PostgreSQL: a directory, a combine, and a configuration.
# --------------------------------------------------------------------------- #

def _pg(level, **over):
    return restore_step(level, dict(
        {"db_type": "postgresql", "dry_run": True, "data_dir": "/var/lib/postgresql/data",
         "host": {"runtime": "docker", "host": "h", "container": "c"}}, **over))


def test_postgresql_full_copies_a_base_backup_directory_into_place():
    result = _pg("full", backup_path="/b/base/x_FULL")
    assert "cp -a /b/base/x_FULL/." in result["plan"]["combine"]


def test_a_container_restore_plans_the_whole_lifecycle_itself():
    """Nothing is left for the caller to type. The chain is put where the container can read it,
    then the combine runs while the server is up because it is the long part; only the swap needs
    it down, so the database is out for seconds rather than for the minutes pg_combinebackup
    takes."""
    plan = _pg("diff", backup_paths=["/b/base/a_FULL", "/b/base/b_INCR"])["plan"]

    assert list(plan) == ["stage", "combine", "stop", "swap", "start"]
    assert "docker exec -u postgres" in plan["combine"]
    assert "docker stop" in plan["stop"] and "docker start" in plan["start"]


def test_the_chain_is_staged_into_the_container_before_it_is_combined():
    """The chain is listed on the host and combined inside the container. When no volume serves
    that path the container cannot open it, and the combine died on whichever piece was newest
    while happily reading a previous run's leftovers for the rest."""
    plan = _pg("diff", backup_paths=["/b/base/a_FULL", "/b/base/b_INCR"])["plan"]

    assert [p for p in plan["stage"] if "a_FULL" in p] and [p for p in plan["stage"] if "b_INCR" in p]
    assert all("docker cp" in step for step in plan["stage"])


def test_staging_replaces_a_previous_copy_rather_than_copying_into_it():
    """``docker cp`` into an existing directory nests the copy, and keeping the old one pins the
    restore to whatever was staged first - pieces deleted at the source get read hours later."""
    stage = _pg("diff", backup_paths=["/b/base/a_FULL", "/b/base/b_INCR"])["plan"]["stage"][0]

    assert "rm -rf /b/base/a_FULL" in stage
    assert stage.index("rm -rf") < stage.index("docker cp")
    # Copied in as root (the staging path is outside the database user's own directories), then
    # handed back readable - the database user is what reads the pieces during the combine.
    assert "-u 0" in stage and "chmod -R a+rX" in stage


def test_the_data_directory_contents_are_replaced_not_the_directory():
    """PGDATA's parent is root-owned, so the database user can neither delete nor recreate the
    directory - but it owns what is inside. `rm -rf $PGDATA` simply fails."""
    plan = _pg("diff", backup_paths=["/b/base/a_FULL", "/b/base/b_INCR"])["plan"]

    assert "/var/lib/postgresql/data/*" in plan["swap"]
    assert "rm -rf /var/lib/postgresql/data " not in plan["swap"]


def test_nothing_is_written_as_root():
    """Writing as root (which is what plain `docker exec` does) leaves a data directory PostgreSQL
    refuses to start on. The fix is not to chown afterwards - it is never to write as root."""
    plan = _pg("diff", backup_paths=["/b/base/a_FULL", "/b/base/b_INCR"])["plan"]

    assert "-u postgres" in plan["combine"] and "-u postgres" in plan["swap"]
    assert "chown" not in plan["combine"] and "chown" not in plan["swap"]


def test_a_path_a_volume_already_serves_is_never_copied_or_deleted():
    """Host and container are looking at one directory, so there is nothing to copy - and the
    "previous copy" the staging would delete first would be the source backups themselves."""
    from db_ops.common.restorestep.postgresql import _served_by_a_mount

    mounts = [{"Source": "/opt/stage", "Destination": "/var/lib/postgresql/backup"}]

    assert _served_by_a_mount(mounts, "/var/lib/postgresql/backup/base/a_FULL") is True
    assert _served_by_a_mount(mounts, "/opt/db_ops/pg_restore_stage/base/a_FULL") is False
    # Not a prefix match on the string: /var/lib/postgresql/backupXYZ is a different directory.
    assert _served_by_a_mount(mounts, "/var/lib/postgresql/backupXYZ") is False


def test_the_wal_directory_is_staged_too_because_the_server_reads_it():
    """``restore_command`` is run by the server, inside the container. Pointing it at a host path
    the server cannot see produces a cluster that starts and silently replays nothing."""
    import db_ops.common.restorestep.postgresql as pg

    calls = []

    def fake_run(host, command, **kw):
        calls.append(command)
        return {"exit_code": 0, "stdout": "[]", "stderr": ""}

    original_mounts = pg._container_mounts
    pg._container_mounts = lambda *a, **k: []
    pg_run = pg.run
    pg.run = fake_run
    try:
        result = restore_step("log", {
            "db_type": "postgresql", "data_dir": "/var/lib/postgresql/data",
            "wal_dir": "/opt/db_ops/pg_restore_stage/wal",
            "host": {"runtime": "docker", "host": "h", "container": "c"}})
    finally:
        pg._container_mounts = original_mounts
        pg.run = pg_run

    assert result["staged_into_container"] == ["/opt/db_ops/pg_restore_stage/wal"]
    assert any("docker cp /opt/db_ops/pg_restore_stage/wal" in c for c in calls)


def test_postgresql_diff_needs_the_whole_chain_not_the_newest():
    """pg_combinebackup reads the full and every incremental together. Given only the newest, the
    result is a data directory missing everything in between - and it starts."""
    with pytest.raises(RestoreStepError, match="every incremental"):
        _pg("diff", backup_path="/b/base/x_INCR")


def test_postgresql_diff_combines_the_chain_in_order():
    combine = _pg("diff", backup_paths=["/b/base/a_FULL", "/b/base/b_INCR",
                                        "/b/base/c_INCR"])["plan"]["combine"]
    assert "pg_combinebackup" in combine
    assert combine.index("a_FULL") < combine.index("c_INCR")


def test_postgresql_log_writes_a_configuration_rather_than_applying_anything():
    """WAL is replayed by the server at startup; there is nothing to apply here, and saying
    otherwise would have an operator believe a step ran that did not."""
    result = _pg("log", backup_path="/b/wal", stopat="2026-08-07 01:40:00")
    assert "recovery_target_time = " in result["command"]
    assert "recovery.signal" in result["command"]


def test_postgresql_needs_a_data_dir():
    with pytest.raises(RestoreStepError, match="data_dir is required"):
        restore_step("full", {"db_type": "postgresql", "backup_path": "/b/x_FULL",
                              "dry_run": True,
                              "host": {"runtime": "docker", "host": "h", "container": "c"}})


def test_pg_combinebackup_is_resolved_not_assumed_to_be_on_path():
    """Measured on pg_ha_cloud2-primary (PostgreSQL 18.4): /usr/bin carries Debian wrappers for
    psql and pg_basebackup but NOT pg_combinebackup, which lives in /usr/lib/postgresql/18/bin.
    Calling it bare fails with "command not found" on an instance where it is plainly installed."""
    combine = _pg("diff", backup_paths=["/b/base/a_FULL", "/b/base/b_INCR"])["plan"]["combine"]

    assert "/usr/lib/postgresql/*/bin/pg_combinebackup" in combine
    assert "command -v pg_combinebackup" in combine


def test_an_explicit_binary_path_wins_over_the_search():
    """A caller who knows where it is should not have the search run anyway."""
    combine = _pg("diff", backup_paths=["/b/base/a_FULL", "/b/base/b_INCR"],
                  bin_dir="/opt/pg/bin")["plan"]["combine"]

    assert "/opt/pg/bin/pg_combinebackup" in combine
    assert "command -v" not in combine
