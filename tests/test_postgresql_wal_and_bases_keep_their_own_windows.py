"""PostgreSQL's base chains and its WAL archive keep their own windows (0.35.0, 1.229).

The plan of 2026-10-29 §1.4: the base chains go by ``FULL_BASE``'s window (an ``INCR_BASE`` goes with
its chain - 1.227's chain rule), and the WAL archive by ``WAL``'s, sent as
``RETENTION_FULL_BASE_SECONDS`` and ``RETENTION_WAL_SECONDS``:

* WAL kept shorter than the bases: the cut line is the START WAL of the oldest full base that began
  inside the WAL window - else the newest - so point-in-time reaches back the WAL window and never
  less than from the newest base. An older base still restores to its own end: ``-X stream`` put
  the WAL it needs inside it;
* ``keep`` on WAL: no cleanup at all; ``keep`` on FULL_BASE: no sweep;
* nothing stated: exactly as before - the oldest base's START WAL, the sweep by ``RETENTION_DAYS``.

These run the scripts' own blocks, cut out of the shipped files, on real directories.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dbabrain.backup_restore import spec_builder
from dbabrain.backup_restore.backup import BackupJob
from dbabrain.lib import cleanup_retention
from dbabrain.lib.backup_scope import BackupScope
from dbabrain.lib.time_window import parse_time_window_config

SCRIPTS = Path(spec_builder.__file__).resolve().parents[1] / "common" / "backup_scripts" / "postgresql"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="no bash on this machine")
DAY = 86400


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _stamp(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%dT%H%M%SZ")


def _bases(root: Path, ages: dict[str, float]) -> Path:
    base = root / "base"
    for suffix, days in ages.items():
        (base / f"{_stamp(days)}_{suffix.split(':')[0]}").mkdir(parents=True)
    return base


def _run(harness: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    script = "\n".join(['run_db() { sh -c "$1"; }', 'die() { echo "die: $*" >&2; exit 3; }', *harness])
    return subprocess.run([BASH, "-c", script], env={**os.environ, **env}, capture_output=True, text=True)


def _floor(root: Path, env: dict[str, str]) -> str:
    text = (SCRIPTS / "pg_archive_wal.sh").read_text(encoding="utf-8")
    done = _run([f"base_dir='{(root / 'base').as_posix()}'", 'retention_seconds=""', 'age_test="-mtime +7"',
                 _between(text, "# Per file type (0.35.0, 1.229)", "\n# $DOCKER_CONTAINER is optional"),
                 'oldest_base="$(run_db "ls -1d ${base_dir}/*_FULL 2>/dev/null | sort | head -1" | tr -d \'\\r\')"',
                 _between(text, "# >>> wal floor", "# <<< wal floor"),
                 'printf "%s\\n" "${oldest_base##*/}"'], env)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_wal_shorter_than_the_bases_cuts_at_the_oldest_full_inside_its_window(tmp_path):
    _bases(tmp_path, {"FULL": 13, "FULL:b": 6, "FULL:c": 1})
    floor = _floor(tmp_path, {"RETENTION_WAL_SECONDS": str(7 * DAY)})
    assert floor.endswith("_FULL") and floor.startswith(_stamp(6)[:8])


def test_a_wal_window_shorter_than_every_base_still_keeps_point_in_time_from_the_newest(tmp_path):
    _bases(tmp_path, {"FULL": 13, "FULL:b": 6})
    assert _floor(tmp_path, {"RETENTION_WAL_SECONDS": str(DAY)}).startswith(_stamp(6)[:8])


def test_nothing_stated_cuts_at_the_oldest_base_as_before(tmp_path):
    _bases(tmp_path, {"FULL": 13, "FULL:b": 6})
    assert _floor(tmp_path, {}).startswith(_stamp(13)[:8])


def test_a_wrong_wal_window_stops_the_script(tmp_path):
    _bases(tmp_path, {"FULL": 1})
    text = (SCRIPTS / "pg_archive_wal.sh").read_text(encoding="utf-8")
    block = _between(text, "# Per file type (0.35.0, 1.229)", "\n# $DOCKER_CONTAINER is optional")
    assert _run([block], {"RETENTION_WAL_SECONDS": "30"}).returncode == 3


def _sweep(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    text = (SCRIPTS / "pg_basebackup_database.sh").read_text(encoding="utf-8")
    return _run([f"base_dir='{(root / 'base').as_posix()}'", 'retention_window="14 days ago"',
                 _between(text, "# Per file type (0.35.0, 1.229)", "\n# Unset is the normal case"),
                 _between(text, "# --- Retention: drop whole chains", "\n# Make the pieces readable")], env)


def test_the_base_chains_go_by_full_bases_window(tmp_path):
    base = _bases(tmp_path, {"FULL": 10, "INCR": 9, "FULL:b": 2, "INCR:b": 1})
    done = _sweep(tmp_path, {"RETENTION_FULL_BASE_SECONDS": str(5 * DAY)})
    assert done.returncode == 0, done.stderr
    # The whole older chain went - its INCR with it - and the newer one stayed.
    assert sorted(p.name.split("_", 1)[1] for p in base.iterdir()) == ["FULL", "INCR"]


def test_keep_on_full_base_sweeps_nothing(tmp_path):
    base = _bases(tmp_path, {"FULL": 30, "FULL:b": 20})
    done = _sweep(tmp_path, {"RETENTION_FULL_BASE_SECONDS": "keep"})
    assert done.returncode == 0 and len(list(base.iterdir())) == 2 and "kept (keep)" in done.stdout


def test_a_postgresql_job_sends_its_two_windows():
    raw = {cleanup_retention.FIELD: {"FULL_BASE": 14 * DAY, "WAL": 7 * DAY, "default": 14 * DAY}}
    job = BackupJob(backup_id="PG", job="wal", db_type="postgresql", server_id="PG-1",
                    script="assets/backup/postgresql/pg_archive_wal.sh", backup_dir="/b",
                    cleanup_retention=cleanup_retention.parse(raw, context="t"),
                    retention=cleanup_retention.parse_per_type(raw, context="t", engine="postgresql"),
                    time_window=parse_time_window_config({}, context="t").time_window,
                    scope=BackupScope(databases=(), all_databases=True))
    assert spec_builder._retention_per_type_env(job) == {
        "RETENTION_FULL_BASE_SECONDS": str(14 * DAY), "RETENTION_WAL_SECONDS": str(7 * DAY)}
