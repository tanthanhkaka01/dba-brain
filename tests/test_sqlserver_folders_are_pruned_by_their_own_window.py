"""Each SQL Server backup folder is pruned by its own window (0.35.0, 1.228).

FULL, DIFF and LOG of a database sit in `<DB>/FULL|DIFF|LOG`, each written by its own job, and each
job's script used to prune `*.bak` and `*.trn` together with its own window - so the LOG job, every
few minutes and usually the shortest, pruned the FULLs and DIFFs its sibling jobs meant to keep
(the plan of 2026-10-29, §0). A job whose ``cleanup_retention`` names types now hands the script
``RETENTION_FULL_SECONDS`` / ``_DIFF_`` / ``_LOG_`` (seconds or ``keep``), and each folder is cut by
its own. The rule over all of them is unchanged: never the newest FULL, nor anything after it.

These tests run the scripts' own retention blocks - cut out of the shipped files - on real files
with real ages: bash for the Linux script, Windows PowerShell for the Windows one. They also pin a
fix found while writing them: `find ! -newer <newest full>` matches the newest full itself, so in
the one-window form a database whose backups stopped for longer than the window lost its last FULL.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dbabrain.backup_restore import spec_builder
from dbabrain.backup_restore.backup import BackupJob
from dbabrain.lib import cleanup_retention
from dbabrain.lib.backup_scope import BackupScope
from dbabrain.lib.time_window import parse_time_window_config

SCRIPTS = Path(spec_builder.__file__).resolve().parents[1] / "common" / "backup_scripts" / "sqlserver"
DAY, HOUR = 86400, 3600
WINDOWS = {"RETENTION_FULL_SECONDS": str(14 * DAY), "RETENTION_DIFF_SECONDS": str(14 * DAY),
           "RETENTION_LOG_SECONDS": str(7 * DAY)}
#: Each file and its age: the newest FULL is a day old; a LOG two hours old came after it.
FILES = {"FULL/f_old.bak": 20 * DAY, "FULL/f_mid.bak": 10 * DAY, "FULL/f_new.bak": 1 * DAY,
         "DIFF/d_old.bak": 20 * DAY, "DIFF/d_mid.bak": 10 * DAY,
         "LOG/l_old.trn": 10 * DAY, "LOG/l_new.trn": 2 * HOUR}


def _tree(root: Path, files=FILES) -> Path:
    db = root / "APPDB"
    now = time.time()
    for name, age in files.items():
        path = db / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        os.utime(path, (now - age, now - age))
    return db


def _left(db: Path) -> set[str]:
    return {p.relative_to(db).as_posix() for p in db.rglob("*") if p.is_file()}


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


# --------------------------------------------------------------------------- Linux (bash)
BASH = shutil.which("bash")


def _run_sh(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    text = (SCRIPTS / "mssql_backup_database.sh").read_text(encoding="utf-8")
    harness = "\n".join([
        'exec_here() { "$@"; }', 'die() { echo "die: $*" >&2; exit 3; }',
        f"backup_dir='{root.as_posix()}'", "databases='APPDB'",
        'age_test="-mmin +1440"', 'retention_window="1d"',
        _between(text, "# Per file type (0.35.0, 1.228)", "\n# $DOCKER_CONTAINER is optional"),
        _between(text, 'for db in $databases; do\n    db_dir="${backup_dir%/}/${db}"\n    newest_full=',
                 "\n# Readable by the SSH user"),
    ])
    return subprocess.run([BASH, "-c", harness], env={**os.environ, **env}, capture_output=True, text=True)


needs_bash = pytest.mark.skipif(BASH is None, reason="no bash on this machine")


@needs_bash
def test_bash_each_folder_is_cut_by_its_own_window(tmp_path):
    db = _tree(tmp_path)
    done = _run_sh(tmp_path, WINDOWS)
    assert done.returncode == 0, done.stderr
    # The 10-day FULL and DIFF stay under their 14 days although the LOG window is 7 - the
    # shortest-job-wins case of the plan's §0, as a regression.
    assert _left(db) == {"FULL/f_mid.bak", "FULL/f_new.bak", "DIFF/d_mid.bak", "LOG/l_new.trn"}
    assert "APPDB LOG removed 1 file(s)" in done.stdout


@needs_bash
def test_bash_keep_is_never_cut(tmp_path):
    db = _tree(tmp_path)
    done = _run_sh(tmp_path, {**WINDOWS, "RETENTION_FULL_SECONDS": "keep"})
    assert done.returncode == 0, done.stderr
    assert {"FULL/f_old.bak", "FULL/f_mid.bak"} <= _left(db) and "APPDB FULL kept (keep)" in done.stdout


@needs_bash
def test_bash_refuses_a_partial_or_wrong_set(tmp_path):
    _tree(tmp_path)
    assert _run_sh(tmp_path, {"RETENTION_LOG_SECONDS": str(DAY)}).returncode == 3
    assert _run_sh(tmp_path, {**WINDOWS, "RETENTION_DIFF_SECONDS": "30"}).returncode == 3


@needs_bash
@pytest.mark.parametrize("env", [WINDOWS, {}], ids=["per-type", "one-window"])
def test_bash_the_newest_full_is_kept_however_old(tmp_path, env):
    db = _tree(tmp_path, {"FULL/only.bak": 30 * DAY})
    done = _run_sh(tmp_path, {**env, **({k: str(DAY) for k in WINDOWS} if env else {})})
    assert done.returncode == 0, done.stderr
    assert _left(db) == {"FULL/only.bak"}


# --------------------------------------------------------------------------- Windows (PowerShell)
POWERSHELL = shutil.which("powershell.exe") if sys.platform == "win32" else None


def _run_ps1(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    text = (SCRIPTS / "mssql_backup_database.ps1").read_text(encoding="utf-8")
    harness = "\n".join([
        'function Die($m) { Write-Output "die: $m"; exit 3 }',
        f"$backupDir = '{root}'", "$databases = @('APPDB')",
        "$cutoff = (Get-Date).AddDays(-1); $retentionWindow = '1d'",
        _between(text, "# Per file type (0.35.0, 1.228)", "\n# A SQL login needs both halves."),
        _between(text, 'foreach ($db in $databases) {\n    $dbDir = "$backupDir\\$db"\n    if (-not (Test-Path',
                 '"retention: $db removed $removed file(s) past the window ($retentionWindow)'),
        "}",
    ])
    script = root / "harness.ps1"
    script.write_text(harness, encoding="utf-8-sig")
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                          env={**os.environ, **env}, capture_output=True, text=True)


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell only")
def test_powershell_each_folder_is_cut_by_its_own_window_and_keep_holds(tmp_path):
    db = _tree(tmp_path)
    done = _run_ps1(tmp_path, {**WINDOWS, "RETENTION_FULL_SECONDS": "keep"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert _left(db) - {"harness.ps1"} == {"FULL/f_old.bak", "FULL/f_mid.bak", "FULL/f_new.bak",
                                           "DIFF/d_mid.bak", "LOG/l_new.trn"}


# --------------------------------------------------------------------------- what the job sends
def _job(value):
    raw = {cleanup_retention.FIELD: value}
    return BackupJob(backup_id="LAB", job="log", db_type="sqlserver", server_id="LAB-SQL",
                     script="assets/backup/sqlserver/mssql_backup_database.sh", backup_dir="/b",
                     cleanup_retention=cleanup_retention.parse(raw, context="t"),
                     retention=cleanup_retention.parse_per_type(raw, context="t", engine="sqlserver"),
                     time_window=parse_time_window_config({}, context="t").time_window,
                     scope=BackupScope(databases=(), all_databases=True))


def test_a_job_naming_types_sends_all_three_and_keeps_the_longest_for_old_scripts():
    job = _job({"FULL": 14 * DAY, "LOG": 7 * DAY, "default": 14 * DAY})
    assert spec_builder._retention_per_type_env(job) == {
        "RETENTION_FULL_SECONDS": str(14 * DAY), "RETENTION_DIFF_SECONDS": str(14 * DAY),
        "RETENTION_LOG_SECONDS": str(7 * DAY)}
    assert job.retention_days == 14


def test_the_one_window_form_sends_nothing_new():
    assert spec_builder._retention_per_type_env(_job(8 * DAY)) == {}


def test_a_zero_window_per_folder_is_refused():
    with pytest.raises(ValueError, match="at least 60"):
        spec_builder._retention_per_type_env(_job({"FULL": 0, "default": DAY}))
