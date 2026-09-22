"""A backup's name says its time zone, and "Sunday" is Sunday where the node is.

Two readings of one clock disagreed on a node configured for +07:

* the SQL Server backup scripts stamped names in UTC with nothing marking it -
  `APPDB_FULL_20260919_072256.bak` read as 07:22 local, seven hours wrong;
* the PostgreSQL and Oracle scripts took their weekly full when `date +%u` said Sunday, which is
  the HOST's clock. On a UTC host the +07 Sunday's full started seven hours late, and a Saturday
  evening run could take it early.

Now the stamp ends in `Z`, and db_ops passes the weekday in the node's configured zone as
`DB_OPS_WEEKDAY`, which the scripts read before falling back to the host clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db_ops.backup_restore import spec_builder
from db_ops.lib.paths import resolve_tool_path

from test_backup_jobs import _job_and_target

PLUS_7 = timezone(timedelta(hours=7))


def _script(relative: str) -> str:
    return resolve_tool_path(relative).read_text(encoding="utf-8")


def test_the_weekday_passed_to_the_script_is_the_nodes_not_utcs(monkeypatch):
    # 00:30 on Sunday at +07 is still Saturday 17:30 in UTC.
    monkeypatch.setattr(spec_builder, "display_now",
                        lambda: datetime(2026, 9, 20, 0, 30, tzinfo=PLUS_7))
    job, target = _job_and_target(env={"BACKUP_LEVEL": "full"})

    request = spec_builder.backup_request_from_job(job, target=target, secrets={})

    assert request["env"]["DB_OPS_WEEKDAY"] == "7"


def test_a_job_can_still_pin_the_weekday(monkeypatch):
    monkeypatch.setattr(spec_builder, "display_now",
                        lambda: datetime(2026, 9, 20, 0, 30, tzinfo=PLUS_7))
    job, target = _job_and_target(env={"DB_OPS_WEEKDAY": "3"})

    assert spec_builder.backup_request_from_job(
        job, target=target, secrets={})["env"]["DB_OPS_WEEKDAY"] == "3"


def test_no_backup_script_decides_the_weekday_for_itself():
    """The level is the scheduler's decision, and the weekday is config.

    These two scripts used to choose FULL against ``$DB_OPS_WEEKDAY``, falling back to ``date +%u``
    on the container host. db_ops only began passing that variable in 0.20.0, so every older node
    took the fallback — and on a host set to UTC that is a different day from the one the scheduler
    read the same job's ``from_hour`` on. One backup, two clocks: on 2026-09-19 the host said
    Saturday while the node's own +07 said Sunday, so five incrementals ran and failed where the
    weekly full was due.

    The day is now ``time_window.weekdays``, evaluated on the node's configured clock beside every
    other bound. A comparison against a weekday literal anywhere in these scripts would put the
    second clock back.
    """
    for script in ("assets/backup/postgresql/pg_basebackup_database.sh",
                   "assets/backup/oracle/oracle_rman_database.sh"):
        # Comments may still explain what was removed and why; code may not do it.
        code = "\n".join(line for line in _script(script).splitlines()
                         if not line.lstrip().startswith("#"))

        assert "date +%u" not in code, script
        assert "DB_OPS_WEEKDAY" not in code, script
        assert 'BACKUP_LEVEL' in code, script


def test_a_sql_server_backup_name_marks_its_stamp_as_utc():
    assert 'stamp="$(date -u +%Y%m%d_%H%M%SZ)"' in _script(
        "assets/backup/sqlserver/mssql_backup_database.sh")
    assert "ToString('yyyyMMdd_HHmmss') + 'Z'" in _script(
        "assets/backup/sqlserver/mssql_backup_database.ps1")


def test_the_restore_accepts_both_names_and_compares_the_stamp_without_the_marker():
    text = _script("assets/restore/sqlserver/mssql_restore.sh")

    assert '[0-9]{8}_[0-9]{6}Z?\\.$3' in text
    assert text.count("Z\\{0,1\\}\\.") == 4        # FULL, DIFF, the DIFF base, and LOG stamps


def test_a_marked_name_is_read_as_utc_and_an_unmarked_one_as_before():
    """db_ops' own names carry the Z and are UTC. A name written by another tool on the source
    server (a maintenance plan, an Ola Hallengren job) carries none and stamps local time, so it
    keeps the reading it always had rather than moving by the server's offset."""
    from db_ops.backup_restore.shell_quoting import backup_time_from_name

    marked = backup_time_from_name("APPDB_FULL_20260919_072256Z.bak")
    unmarked = backup_time_from_name("SRV_APPDB_FULL_20260919_072256.bak")

    assert marked == datetime(2026, 9, 19, 7, 22, 56, tzinfo=timezone.utc).timestamp()
    assert unmarked == datetime(2026, 9, 19, 7, 22, 56).timestamp()
    assert backup_time_from_name("APPDB_FULL_01.bak") is None
