"""Oracle's backup pieces keep their own windows (0.35.0, 1.230).

A job whose ``cleanup_retention`` names types sends ``RETENTION_RMAN_L0_SECONDS``,
``RETENTION_ARCHIVELOG_SECONDS``, ``RETENTION_CONTROLFILE_SECONDS`` and ``RETENTION_SPFILE_SECONDS``:

* RMAN_L0 is RMAN's recovery window (whole days, at least one; under a day the sweep after a level 0
  applies it exactly) - in place of ``RETENTION_DAYS``, which such a job sends as its LONGEST window:
  a ``CONTROLFILE: keep`` would otherwise make the recovery window a century;
* ARCHIVELOG is how long a backed-up log stays on DISK. The backups of the logs follow RMAN_L0: a
  level 0 is a hot backup and needs the logs written during it - cut shorter, every older level 0
  would look present and not restore. (The plan said ARCHIVELOG's window; this is why it does not.)
* CONTROLFILE / SPFILE backups go by their own window after a run that succeeded, never reaching
  into the run - the controlfile it just backed up stays;
* ``keep`` cannot be carried out for RMAN_L0 or ARCHIVELOG, and is refused when the job is built.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dbabrain.backup_restore import spec_builder
from dbabrain.backup_restore.backup import BackupJob
from dbabrain.lib import cleanup_retention
from dbabrain.lib.backup_scope import BackupScope
from dbabrain.lib.time_window import parse_time_window_config

SCRIPTS = Path(spec_builder.__file__).resolve().parents[1] / "common" / "backup_scripts" / "oracle"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="no bash on this machine")
DAY, HOUR = 86400, 3600


def _block(name: str) -> str:
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    start = text.index("# >>> retention per type")
    return text[start:text.index("# <<< retention per type", start)]


def _run(name: str, prelude: str, show: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    script = "\n".join(['die() { echo "die: $*" >&2; exit 3; }', prelude, _block(name), show])
    return subprocess.run([BASH, "-c", script], env={**os.environ, **env}, capture_output=True, text=True)


@needs_bash
@pytest.mark.parametrize("seconds, days, exact", [(14 * DAY, "14", ""), (2 * HOUR, "1", str(2 * HOUR))])
def test_the_level_0_window_is_the_recovery_window_not_the_longest(seconds, days, exact):
    done = _run("oracle_rman_database.sh", 'retention_days=36500; retention_seconds=""',
                'echo "$retention_days|$retention_seconds"', {"RETENTION_RMAN_L0_SECONDS": str(seconds)})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == f"{days}|{exact}"


@needs_bash
def test_a_wrong_controlfile_window_stops_the_database_job():
    done = _run("oracle_rman_database.sh", 'retention_days=14; retention_seconds=""', "",
                {"RETENTION_CONTROLFILE_SECONDS": "30"})
    assert done.returncode == 3


@needs_bash
def test_archivelog_is_the_logs_on_disk_and_their_backups_follow_the_level_0():
    done = _run("oracle_rman_archivelog.sh", 'retention_days=36500; retention_seconds=""; log_age=36500',
                'echo "$log_age|$retention_days"',
                {"RETENTION_ARCHIVELOG_SECONDS": str(2 * DAY), "RETENTION_RMAN_L0_SECONDS": str(14 * DAY)})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == f"{2 * DAY}/86400|14"
    text = (SCRIPTS / "oracle_rman_archivelog.sh").read_text(encoding="utf-8")
    assert "DELETE NOPROMPT BACKUP OF ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-${retention_days}';" in text


def test_the_controlfile_and_spfile_sweep_never_reaches_into_the_run():
    text = (SCRIPTS / "oracle_rman_database.sh").read_text(encoding="utf-8")
    assert ("DELETE NOPROMPT BACKUP OF ${kind} COMPLETED BEFORE "
            "'SYSDATE-${window}/86400-${run_elapsed}/86400';") in text
    # After the backup's own verdict: a run that failed exits before it.
    assert text.index("emit_pieces \"$rman_out\"") < text.index("piece_sweep=\"\"")


def _job(value):
    raw = {cleanup_retention.FIELD: value}
    return BackupJob(backup_id="ORA", job="database", db_type="oracle", server_id="ORA-1",
                     script="assets/backup/oracle/oracle_rman_database.sh", backup_dir="/b",
                     cleanup_retention=cleanup_retention.parse(raw, context="t"),
                     retention=cleanup_retention.parse_per_type(raw, context="t", engine="oracle"),
                     time_window=parse_time_window_config({}, context="t").time_window,
                     scope=BackupScope(databases=(), all_databases=True))


def test_an_oracle_job_sends_its_four_windows():
    env = spec_builder._retention_per_type_env(_job(
        {"RMAN_L0": 14 * DAY, "ARCHIVELOG": 2 * DAY, "CONTROLFILE": "keep", "default": 14 * DAY}))
    assert env == {"RETENTION_RMAN_L0_SECONDS": str(14 * DAY), "RETENTION_ARCHIVELOG_SECONDS": str(2 * DAY),
                   "RETENTION_CONTROLFILE_SECONDS": "keep", "RETENTION_SPFILE_SECONDS": str(14 * DAY)}


@pytest.mark.parametrize("kind", ["RMAN_L0", "ARCHIVELOG"])
def test_keep_is_refused_where_the_engine_cannot_carry_it_out(kind):
    with pytest.raises(ValueError, match="need a window"):
        spec_builder._retention_per_type_env(_job({kind: "keep", "default": 14 * DAY}))


def test_oracles_point_in_time_reach_is_the_level_0s_window():
    retention = cleanup_retention.parse_per_type(
        {cleanup_retention.FIELD: {"RMAN_L0": 10 * DAY, "ARCHIVELOG": 2 * DAY, "default": 10 * DAY}},
        context="t", engine="oracle")
    assert cleanup_retention.point_in_time_reach(retention, engine="oracle") == 10 * DAY
