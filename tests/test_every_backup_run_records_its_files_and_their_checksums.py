"""Every backup run records each file it wrote, with its checksum, and is verified (0.34.0, 1.212).

Why it matters: the operator, 2026-10-29 - *every backup run must be saved into the db: file name,
server, checksum - so every run is verified*. Until now a run left a ``job_runs`` row and one
``RESULT=ok`` line; which files it wrote, how big, with what hash, was nowhere. The scripts now
print one ``FILE`` line per file - its sha256 taken where the file lies and the engine's own check
(VERIFYONLY WITH CHECKSUM, pg_verifybackup, RMAN VALIDATE) - and the run writes them to the catalog.

Two outcomes are new and are the point: a file the script could not hash, or whose engine check
failed, turns a "successful" run into an error; and a catalog that cannot be written does too. A
green run that is neither verified nor recorded is the failure this exists to end.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import backup as backup_module
from dbabrain.backup_restore.server_metadata import ServerMetadataPlan
from dbabrain.common import backup as common_backup
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.db.store import RunAlreadyClaimed
from dbabrain.lib import integrity as I
from dbabrain.lib.time_window import TimeWindow

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "dbabrain" / "common" / "backup_scripts"
SHA_A, SHA_B = "a" * 64, "b" * 64
# The bash found on PATH, by its full path: on Windows a bare "bash" starts WSL (System32 is searched
# first), which cannot see the test's folder.
BASH = shutil.which("bash")


def _line(db, file_type, path, sha=SHA_A, verify="passed", size=1024, note=""):
    extra = f"\tnote={note}" if note else ""
    return f"FILE\tdb={db}\ttype={file_type}\tsize={size}\tsha256={sha}\tverify={verify}{extra}\tpath={path}"


# --------------------------------------------------------------------------- #
# The receipt line
# --------------------------------------------------------------------------- #
def test_a_receipt_line_is_read_into_the_catalogs_own_words():
    out = "\n".join([
        "-- APPDB full -> /b/APPDB/FULL/APPDB_FULL_20261029_010000Z.bak",
        _line("APPDB", "FULL", "/b/APPDB/FULL/APPDB_FULL_20261029_010000Z.bak"),
        _line("HR DB", "LOG", r"D:\SQL BK\HR DB\LOG\HR DB_LOG_1.trn", sha=SHA_B.upper()) + "\r",
        "RESULT=ok level=full databases=APPDB encrypted=no",
    ])
    files = I.parse_file_lines(out)

    assert [(f["database_name"], f["file_type"], f["file_name"]) for f in files] == [
        ("APPDB", "FULL", "APPDB_FULL_20261029_010000Z.bak"), ("HR DB", "LOG", "HR DB_LOG_1.trn")]
    assert files[0]["file_folder"] == "/b/APPDB/FULL" and files[0]["size_bytes"] == 1024
    assert files[1]["path"] == r"D:\SQL BK\HR DB\LOG\HR DB_LOG_1.trn", "a space in the path needs no quoting"
    assert files[1]["sha256"] == SHA_B, "upper-case hex is read as the same hash"


def test_a_file_the_script_could_not_hash_is_kept_as_unverified_not_dropped():
    files = I.parse_file_lines(_line("APPDB", "FULL", "/b/x.bak", sha="") + "\n"
                               + _line("APPDB", "FULL", "/b/y.bak", sha="not-a-hash"))
    assert [f["sha256"] for f in files] == [None, None]
    assert [I.file_verdict(f) for f in files] == ["hash_failed", "hash_failed"]
    assert I.file_verdict({"sha256": SHA_A, "size_bytes": 10, "engine_verify": "failed"}) == "verify_failed"
    assert I.file_verdict({"sha256": SHA_A, "size_bytes": 10, "engine_verify": "not_run"}) == "ok"


def test_a_file_whose_size_could_not_be_read_is_not_ok():
    """Every file records its size in bytes (the operator, 2026-10-29). A script whose ``stat`` failed
    prints ``size=`` empty: the file is kept, hashed, and called ``size_failed`` - never ``ok``."""
    files = I.parse_file_lines(_line("APPDB", "FULL", "/b/x.bak", size=""))
    assert files[0]["size_bytes"] is None and files[0]["sha256"] == SHA_A
    assert I.file_verdict(files[0]) == "size_failed"
    assert I.file_verdict({"sha256": SHA_A, "size_bytes": 0}) == "ok", "an empty file has a size: 0"


def test_backup_database_answers_the_files_its_script_printed(monkeypatch):
    out = _line("APPDB", "FULL", "/b/a.bak") + "\nRESULT=ok level=full\n"
    monkeypatch.setattr(common_backup, "run_script",
                        lambda host, script, **k: {"stdout": out, "stderr": "", "exit_code": 0})
    spec = common_backup.parse_backup_spec({"db_type": "sqlserver", "script": "echo", "level": "full"})

    result = common_backup.run_backup(spec)

    assert result["status"] == "done"
    assert [f["file_name"] for f in result["files"]] == ["a.bak"]


# --------------------------------------------------------------------------- #
# Into the catalog
# --------------------------------------------------------------------------- #
@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _job(backup_id="ACME_250_APPDB", db_type="sqlserver", job="full"):
    return backup_module.BackupJob(
        backup_id=backup_id, job=job, db_type=db_type, server_id="ACME-192-0-2-250",
        script="x.sh", backup_dir="/b", cleanup_retention=691200, time_window=TimeWindow(),
        server_metadata=ServerMetadataPlan())


def _result(job, lines, status="done"):
    out = "\n".join(lines)
    return backup_module.BackupRunResult(job=job, status=status, exit_code=0, duration_ms=5,
                                         stdout=out, stderr="", files=I.parse_file_lines(out))


def test_a_run_and_its_files_are_written_whole_and_a_log_finds_its_full(catalog):
    full_job, log_job = _job(job="full"), _job(job="log")
    first = backup_module.record_in_catalog(
        catalog, full_job, _result(full_job, [_line("APPDB_PROD", "FULL", "/b/APPDB_PROD/FULL/f.bak")]),
        job_run_id=10, started_at="2026-10-29T01:00:00Z", finished_at="2026-10-29T01:10:00Z")
    second = backup_module.record_in_catalog(
        catalog, log_job, _result(log_job, [_line("APPDB_PROD", "LOG", "/b/APPDB_PROD/LOG/l.trn", sha=SHA_B)]),
        job_run_id=11, started_at="2026-10-29T01:15:00Z", finished_at="2026-10-29T01:15:05Z")

    assert first["level"] == "full" and second["level"] == "log"
    assert first["verified"] == 1 and first["unverified"] == []
    rows = catalog.backup_history(server_id="ACME-192-0-2-250")
    assert [(r["file_type"], r["file_name"], r["checksum_after_backup"], r["verdict"]) for r in rows] == [
        ("FULL", "f.bak", SHA_A, "ok"), ("LOG", "l.trn", SHA_B, "ok")]
    assert rows[1]["chain_parent_file_id"] == rows[0]["file_id"], "the log names the full it needs"
    assert rows[0]["job_run_id"] == 10 and rows[0]["backup_id"] == "ACME_250_APPDB"
    with catalog.connect() as conn:
        runs = [dict(r) for r in conn.execute("SELECT * FROM backup_runs ORDER BY backup_run_id")]
    assert [(r["status"], r["level"], r["files_count"], r["engine_verify"]) for r in runs] == [
        ("DONE", "full", 1, "passed"), ("DONE", "log", 1, "passed")]


def test_a_run_that_wrote_nothing_is_recorded_with_no_level(catalog):
    job = _job()
    summary = backup_module.record_in_catalog(catalog, job, _result(job, ["no database to back up"]),
                                              job_run_id=1, started_at="2026-10-29T01:00:00Z",
                                              finished_at="2026-10-29T01:00:01Z")
    assert summary["files"] == 0 and summary["level"] is None


def test_an_unhashed_or_unverified_file_makes_the_catalogued_run_an_error(catalog):
    job = _job()
    summary = backup_module.record_in_catalog(
        catalog, job, _result(job, [_line("A", "FULL", "/b/a.bak", sha=""),
                                    _line("B", "FULL", "/b/b.bak", verify="failed")]),
        job_run_id=1, started_at="2026-10-29T01:00:00Z", finished_at="2026-10-29T01:10:00Z")
    assert summary["unverified"] == ["a.bak (hash_failed)", "b.bak (verify_failed)"]
    with catalog.connect() as conn:
        run = dict(conn.execute("SELECT * FROM backup_runs").fetchone())
    assert run["status"] == "ERROR" and run["engine_verify"] == "failed"


# --------------------------------------------------------------------------- #
# The scheduled run
# --------------------------------------------------------------------------- #
class _Store:
    """job_runs as the scheduler sees it, keeping what the last update said."""

    rows: list[dict] = []

    @classmethod
    def from_config(cls, config, **kwargs):
        return cls()

    def fetch_latest_job_runs_by_job_code(self):
        return {}

    def fetch_running_job_runs(self, _prefix=""):
        return []

    def job_run_started_since(self, job_code, since):
        return False

    def insert_job_run(self, run):
        if any(r["status"] == "RUNNING" and r["claim_key"] == run.claim_key for r in _Store.rows):
            raise RunAlreadyClaimed(run.claim_key)
        _Store.rows.append({"claim_key": run.claim_key, "status": "RUNNING"})
        return len(_Store.rows)

    def update_job_run(self, *, log_id, status, message, error_text=None, metadata=None, **kwargs):
        _Store.rows[log_id - 1].update(status=status, message=message, error_text=error_text,
                                       metadata=metadata)


def _scheduled(monkeypatch, catalog, job, lines, *, seen_env=None):
    _Store.rows = []
    monkeypatch.setattr(backup_module, "DbaBrainStore", _Store)
    monkeypatch.setattr(backup_module, "BackupCatalogStore",
                        SimpleNamespace(from_config=lambda config: catalog))
    monkeypatch.setattr(backup_module, "emit_backup_restore_event", lambda **k: None)
    monkeypatch.setattr(backup_module.schedule, "reap_stale_runs", lambda **k: [])
    monkeypatch.setattr(backup_module, "resolve_backup_target", lambda *a, **k: None)
    monkeypatch.setattr(backup_module, "load_backup_jobs", lambda _p: [job])
    monkeypatch.setattr(backup_module, "export_for_backup", lambda *a, **k: {"ok": True})

    def execute(item, **kwargs):
        if seen_env is not None:
            seen_env.update(kwargs.get("env_overrides") or {})
        return _result(item, lines + ["RESULT=ok"])

    monkeypatch.setattr(backup_module, "execute_backup_job", execute)
    backup_module.run_backup(app_config=SimpleNamespace(), config_path="x", force=True)
    return _Store.rows[0]


def test_a_verified_run_says_so_in_its_message(monkeypatch, catalog):
    row = _scheduled(monkeypatch, catalog, _job(), [_line("A", "FULL", "/b/a.bak"),
                                                    _line("B", "FULL", "/b/b.bak", sha=SHA_B)])
    assert row["status"] == "DONE"
    assert "2/2 file(s) hashed and verified" in row["message"]
    assert row["metadata"]["catalog"]["files"] == 2


def test_a_run_with_an_unverified_file_ends_as_an_error_that_names_it(monkeypatch, catalog):
    row = _scheduled(monkeypatch, catalog, _job(), [_line("A", "FULL", "/b/a.bak", sha="")])
    assert row["status"] == "ERROR"
    assert "written but not verified: a.bak (hash_failed)" in row["error_text"]
    assert "not verified" in row["message"]


def test_a_catalog_that_cannot_be_written_ends_the_run_as_an_error(monkeypatch, catalog):
    def refuse(**_kwargs):
        raise OSError("the store is unreachable")

    monkeypatch.setattr(catalog, "record_backup", refuse)
    row = _scheduled(monkeypatch, catalog, _job(), [_line("A", "FULL", "/b/a.bak")])
    assert row["status"] == "ERROR"
    assert "the backup catalog could not be written: OSError: the store is unreachable" in row["error_text"]


def test_a_postgresql_job_is_told_where_the_wal_catalog_left_off(monkeypatch, catalog):
    job = _job(backup_id="A1A_STORE_WAL", db_type="postgresql", job="wal")
    _scheduled(monkeypatch, catalog, job, [
        _line("cluster", "WAL", "/b/wal/000000010000000000000007", verify="not_run"),
        _line("cluster", "WAL", "/b/wal/000000010000000000000008", verify="not_run", sha=SHA_B)])
    seen: dict = {}
    _scheduled(monkeypatch, catalog, job, [], seen_env=seen)
    assert seen["CATALOG_AFTER"] == "000000010000000000000008"


def test_a_wal_job_whose_catalog_holds_no_segment_yet_is_given_none_rather_than_an_empty_one(
        monkeypatch, catalog):
    """The first run after the catalog exists. Sent empty, the spec refused it - an empty env value is
    how a missing secret arrives - and the store's own WAL job failed its first run on the worker
    (2.87.51, 2026-10-29). Absent, the script starts from the first segment."""
    seen: dict = {}
    _scheduled(monkeypatch, catalog, _job(backup_id="A1A_STORE_WAL", db_type="postgresql", job="wal"), [],
               seen_env=seen)
    assert "CATALOG_AFTER" not in seen


def test_a_sql_server_job_is_given_no_wal_position(monkeypatch, catalog):
    seen: dict = {}
    _scheduled(monkeypatch, catalog, _job(), [], seen_env=seen)
    assert "CATALOG_AFTER" not in seen


# --------------------------------------------------------------------------- #
# The scripts print the receipt
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [
    "sqlserver/mssql_backup_database.sh", "sqlserver/mssql_backup_database.ps1",
    "postgresql/pg_basebackup_database.sh", "postgresql/pg_archive_wal.sh",
    "oracle/oracle_rman_database.sh", "oracle/oracle_rman_archivelog.sh",
])
def test_every_backup_script_prints_the_one_receipt_line(script):
    text = (SCRIPTS / script).read_text(encoding="utf-8")
    sep = "`t" if script.endswith(".ps1") else r"\t"
    assert f"FILE{sep}db=" in text
    for key in ("type=", "size=", "sha256=", "verify="):
        assert f"{sep}{key}" in text, f"{script}: no {key}"
    assert f"{sep}path=" in text, f"{script}: the path is last, so a space in it needs no quoting"


def test_sql_server_verifyonly_checks_the_page_checksums_too():
    for script in ("sqlserver/mssql_backup_database.sh", "sqlserver/mssql_backup_database.ps1"):
        text = (SCRIPTS / script).read_text(encoding="utf-8")
        assert "RESTORE VERIFYONLY" in text and "WITH CHECKSUM;" in text, script


def _bash_block(script, start, end):
    lines = (SCRIPTS / script).read_text(encoding="utf-8").splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith(start))
    last = next(i for i, line in enumerate(lines) if i > first and line.startswith(end))
    return "\n".join(lines[first:last])


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_the_wal_receipt_lists_only_full_segments_after_the_catalogs_last(tmp_path):
    wal = tmp_path / "wal"
    wal.mkdir()
    for name, body in (("000000010000000000000001", b"one"), ("000000010000000000000002", b"two"),
                       ("000000010000000000000003", b"three"), ("00000002.history", b"h"),
                       ("000000010000000000000004.partial", b"p")):
        (wal / name).write_bytes(body)
    block = _bash_block("postgresql/pg_archive_wal.sh", "# The file's path on this server, not inside the container (0.34.0)", "# --- 3.")
    harness = "\n".join([
        'die() { echo "DIE $1"; exit 1; }',
        'run_db() { bash -c "$1" </dev/null; }',
        f"wal_dir='{wal.as_posix()}'", "container=''",
        "CATALOG_AFTER=000000010000000000000001", "CATALOG_MAX_FILES=200", block])
    out = subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60).stdout
    files = I.parse_file_lines(out)

    assert [f["file_name"] for f in files] == ["000000010000000000000002", "000000010000000000000003"]
    assert files[0]["sha256"] == hashlib.sha256(b"two").hexdigest()
    assert {f["file_type"] for f in files} == {"WAL"}


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_segment_names_are_compared_as_text_never_as_numbers(tmp_path):
    """The worker's case (2026-10-29, 10:52Z): the catalog's newest segment was all digits, so awk
    compared numerically - an older ``...81000000E3`` read as 1e17 and was listed again (a duplicate
    key, every WAL run failing), while the new ``...8400000068`` equalled ``...67`` in double
    precision and was missed."""
    wal = tmp_path / "wal"
    wal.mkdir()
    for name in ("0000000100000081000000E3", "000000010000008400000067", "000000010000008400000068",
                 "0000000100000084000000A0"):
        (wal / name).write_bytes(name.encode())
    block = _bash_block("postgresql/pg_archive_wal.sh", "# The file's path on this server, not inside the container (0.34.0)", "# --- 3.")
    harness = "\n".join([
        'die() { echo "DIE $1"; exit 1; }',
        'run_db() { bash -c "$1" </dev/null; }',
        f"wal_dir='{wal.as_posix()}'", "container=''",
        "CATALOG_AFTER=000000010000008400000067", "CATALOG_MAX_FILES=200", block])
    out = subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60).stdout

    assert [f["file_name"] for f in I.parse_file_lines(out)] == [
        "000000010000008400000068", "0000000100000084000000A0"]


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_rman_pieces_are_typed_by_their_tag_hashed_and_validated(tmp_path):
    (tmp_path / "ORCL_L0_a.bkp").write_bytes(b"level 0")
    (tmp_path / "arch_ORCL_b.bkp").write_bytes(b"archivelog")
    block = _bash_block("oracle/oracle_rman_database.sh", "# The file's path on this server, not inside the container (0.34.0)", "RMAN_IN=")
    rman_out = (f"piece handle={tmp_path.as_posix()}/ORCL_L0_a.bkp tag=DBOPS_L0 comment=NONE\n"
                f"piece handle={tmp_path.as_posix()}/arch_ORCL_b.bkp tag=DBOPS_ARCH comment=NONE")
    harness = "\n".join([
        'die() { echo "DIE $1"; exit 1; }',
        'run_db() { bash -c "$1" </dev/null; }',
        # A fake RMAN: the summary lists two sets of this run, the validation is clean.
        "run_rman() { input=$(cat); case \"$input\" in "
        "LIST*) printf '12      B  0  A DISK  29-OCT-26  1  1  NO  DBOPS_L0\\n13      B  A  A DISK  29-OCT-26  1  1  NO  DBOPS_ARCH\\n';; "
        "VALIDATE*) printf 'Finished validate\\n';; esac; }",
        "run_started=$(date +%s); sid_export=''; oracle_sid=ORCL; container=''", block,
        f"emit_pieces '{rman_out}'"])
    out = subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60).stdout
    files = {f["file_name"]: f for f in I.parse_file_lines(out)}

    assert files["ORCL_L0_a.bkp"]["file_type"] == "RMAN_L0"
    assert files["arch_ORCL_b.bkp"]["file_type"] == "ARCHIVELOG"
    assert files["ORCL_L0_a.bkp"]["sha256"] == hashlib.sha256(b"level 0").hexdigest()
    assert files["ORCL_L0_a.bkp"]["engine_verify"] == "passed"
    assert "VALIDATE BACKUPSET 12,13" in files["ORCL_L0_a.bkp"]["engine_verify_text"]


# --------------------------------------------------------------------------- #
# Where the file is on the server, and whether it still is (the operator, 2026-10-29)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_a_file_in_a_container_is_given_its_path_on_the_server_through_the_mount():
    block = _bash_block("sqlserver/mssql_backup_database.sh",
                        "# The file's path on this server, not inside the container (0.34.0)", "}")
    harness = "\n".join([
        # Two mounts, one inside the other: the longest that holds the file is the one it is in.
        "fake_docker() { printf '/var/opt/mssql|/srv/mssql\\n/var/opt/mssql/backup|/opt/db_backups/mssql\\n'; }",
        "DOCKER=fake_docker; container=mssql", block + "\n}",
        'echo "[$(host_path_of /var/opt/mssql/backup/APPDB/FULL/a.bak)]"',
        'echo "[$(host_path_of /var/opt/mssql/data/x.mdf)]"',
        'echo "[$(host_path_of /tmp/orphan.bak)]"',
        "container=''", 'echo "[$(host_path_of /opt/b/a.bak)]"'])
    out = subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60).stdout.split()
    assert out == ["[/opt/db_backups/mssql/APPDB/FULL/a.bak]", "[/srv/mssql/data/x.mdf]", "[]",
                   "[/opt/b/a.bak]"], "no mount holds the third: it lives in the container alone"


def test_the_receipt_carries_the_host_path_and_the_container():
    line = ("FILE\tdb=APPDB\ttype=FULL\tsize=1\tsha256=" + SHA_A + "\tverify=passed"
            "\thost_path=/opt/db_backups/mssql/APPDB/FULL/a.bak\tcontainer=mssql"
            "\tpath=/var/opt/mssql/backup/APPDB/FULL/a.bak")
    item = I.parse_file_lines(line)[0]
    assert item["host_path"] == "/opt/db_backups/mssql/APPDB/FULL/a.bak"
    assert item["container_name"] == "mssql"
    plain = I.parse_file_lines(_line("APPDB", "FULL", r"D:\SQLBK\a.bak"))[0]
    assert plain["host_path"] == r"D:\SQLBK\a.bak", "without a container the engine's path is the server's"


def test_a_written_file_is_on_disk_and_matches_its_stored_checksum(catalog):
    job = _job()
    line = ("FILE\tdb=APPDB\ttype=FULL\tsize=1\tsha256=" + SHA_A + "\tverify=passed"
            "\thost_path=/opt/bk/a.bak\tcontainer=mssql\tpath=/var/opt/mssql/backup/a.bak")
    backup_module.record_in_catalog(catalog, job, _result(job, [line]), job_run_id=1,
                                    started_at="2026-10-29T01:00:00Z", finished_at="2026-10-29T01:10:00Z")
    row = catalog.backup_history(server_id="ACME-192-0-2-250")[0]
    assert (row["host_path"], row["container_name"]) == ("/opt/bk/a.bak", "mssql")
    assert (row["on_disk"], row["checksum_matches"]) == (1, 1)


def test_a_later_check_is_held_to_the_checksum_the_catalog_stored(catalog):
    file_id = catalog.record_backup_file(
        server_id="S", engine="sqlserver", file_name="a.bak", file_type="FULL", file_path="/b/a.bak",
        checksum_after_backup=SHA_A, size_bytes=1024, verdict="ok")

    assert catalog.check_backup_file(file_id, checksum_now=SHA_A) == "match"
    assert catalog.check_backup_file(file_id, checksum_now=SHA_B) == "mismatch"
    row = catalog.backup_history(server_id="S")[0]
    assert (row["on_disk"], row["checksum_matches"], row["state"]) == (1, 0, "mismatch")
    assert row["checksum_after_backup"] == SHA_A, "what the backup wrote is never rewritten"

    assert catalog.check_backup_file(file_id, checksum_now=None) == "missing"
    row = catalog.backup_history(server_id="S")[0]
    assert (row["on_disk"], row["checksum_matches"], row["state"]) == (0, None, "missing")

    assert catalog.check_backup_file(file_id, checksum_now=SHA_A) == "match"
    row = catalog.backup_history(server_id="S")[0]
    assert (row["on_disk"], row["checksum_matches"], row["state"]) == (1, 1, "present")


def test_a_copied_file_is_held_to_the_stored_checksum_on_both_sides(catalog):
    run = catalog.start_restore_run(action="copy_only", source_server_id="S", target_server_id="T",
                                    copy_max_retry=3)
    run_file = catalog.add_run_file(
        restore_run_id=run, source_server_id="S", target_server_id="T", file_name="a.trn",
        file_type="LOG", source_path="/s/a.trn", target_path="/t/a.trn", copy_max_retry=3,
        source_host_path="/srv/s/a.trn", target_host_path="/srv/t/a.trn",
        checksum_after_backup=SHA_A, checksum_source_before_copy=SHA_A, verdict="copying")

    assert catalog.check_run_file(run_file, side="source", checksum_now=SHA_A) == "match"
    assert catalog.check_run_file(run_file, side="target", checksum_now=SHA_B) == "mismatch"
    row = catalog.run_files(run)[0]
    assert (row["source_on_disk"], row["source_checksum_matches"]) == (1, 1)
    assert (row["target_on_disk"], row["target_checksum_matches"]) == (1, 0)
    assert (row["source_host_path"], row["target_host_path"]) == ("/srv/s/a.trn", "/srv/t/a.trn")

    assert catalog.check_run_file(run_file, side="target", checksum_now=None) == "missing"
    row = catalog.run_files(run)[0]
    assert (row["target_on_disk"], row["target_checksum_matches"]) == (0, None)


def test_the_copy_gate_fills_both_sides_as_it_copies(catalog):
    from dbabrain.backup_restore.copy_gate import FileToCopy, copy_verified

    run = catalog.start_restore_run(action="copy_only", source_server_id="S", target_server_id="T",
                                    copy_max_retry=3)
    good = hashlib.sha256(b"bytes").hexdigest()
    target: dict = {}

    def copy(_item):
        target["sha"] = good
        return 5

    copy_verified(
        FileToCopy(source_server_id="S", target_server_id="T", file_name="a.trn", file_type="LOG",
                   source_path="/s/a.trn", target_path="/t/a.trn", checksum_after_backup=good,
                   source_host_path="/srv/s/a.trn", target_host_path="/srv/t/a.trn"),
        integrity=I.RestoreIntegrity(), hash_source=lambda p: (good, 5),
        hash_target=lambda p: (target["sha"], 5) if target else None, copy=copy,
        catalog=catalog, restore_run_id=run, sleep=lambda s: None)
    row = catalog.run_files(run)[0]
    assert (row["source_on_disk"], row["source_checksum_matches"]) == (1, 1)
    assert (row["target_on_disk"], row["target_checksum_matches"]) == (1, 1)
    assert row["source_host_path"] == "/srv/s/a.trn" and row["target_host_path"] == "/srv/t/a.trn"
    assert row["source_checked_at"] and row["target_checked_at"]
    attempt = catalog.attempts(row["run_file_id"])[0]
    assert (attempt["source_host_path"], attempt["target_host_path"]) == ("/srv/s/a.trn", "/srv/t/a.trn")


def _receipt(db, file_type, sha, host_path, path):
    return (f"FILE\tdb={db}\ttype={file_type}\tsize=1\tsha256={sha}\tverify=passed"
            f"\thost_path={host_path}\tcontainer=m\tpath={path}")


def test_verify_backup_files_rehashes_on_the_server_and_names_what_changed(monkeypatch, catalog):
    job = _job()
    lines = [_receipt("A", "FULL", SHA_A, "/srv/a.bak", "/c/a.bak"),
             _receipt("B", "FULL", SHA_B, "/srv/b.bak", "/c/b.bak"),
             _receipt("C", "FULL", SHA_A, "/srv/c.bak", "/c/c.bak"),
             _receipt("cluster", "FULL_BASE", SHA_B, "/srv/base/X_FULL", "/c/base/X_FULL"),
             _receipt("D", "FULL", SHA_A, "", "/c/d.bak")]
    backup_module.record_in_catalog(catalog, job, _result(job, lines), job_run_id=1,
                                    started_at="2026-10-29T01:00:00Z", finished_at="2026-10-29T01:10:00Z")
    asked: dict = {}

    def file_checksum(command, request, **_kw):
        asked.update(request)
        # a.bak intact; b.bak changed; c.bak gone; the base backup's manifest intact.
        return {"files": [{"path": "/srv/a.bak", "sha256": SHA_A, "size_bytes": 1},
                          {"path": "/srv/b.bak", "sha256": "c" * 64, "size_bytes": 1},
                          {"path": "/srv/base/X_FULL/backup_manifest", "sha256": SHA_B, "size_bytes": 1}],
                "missing": ["/srv/c.bak"]}

    target = SimpleNamespace(host="192.0.2.10", port=22, username="u", key_file="", password_ref="",
                             platform="linux", access="ssh", ssl=False)
    monkeypatch.setattr(backup_module, "BackupCatalogStore", SimpleNamespace(from_config=lambda c: catalog))
    monkeypatch.setattr(backup_module, "load_backup_jobs", lambda _p: [job])
    monkeypatch.setattr(backup_module, "resolve_backup_target", lambda *a, **k: target)
    monkeypatch.setattr(backup_module.common_cli, "run", file_checksum)

    summary = backup_module.verify_backup_files(app_config=SimpleNamespace(), config_path="x")

    assert "/srv/base/X_FULL/backup_manifest" in asked["paths"], "a base backup is held by its manifest"
    assert asked["host"]["host"] == "192.0.2.10" and asked["host"]["runtime"] == "linux"
    assert summary["checked"] == 4 and summary["match"] == 2
    assert [m["file"] for m in summary["mismatch"]] == ["b.bak"]
    assert [m["file"] for m in summary["missing"]] == ["c.bak"]
    assert summary["skipped"] == [{"file": "d.bak", "why": "in no mount of its container - no path on the server"}]
    rows = {r["file_name"]: r for r in catalog.backup_history(server_id="ACME-192-0-2-250")}
    assert (rows["b.bak"]["on_disk"], rows["b.bak"]["checksum_matches"]) == (1, 0)
    assert (rows["c.bak"]["on_disk"], rows["c.bak"]["checksum_matches"]) == (0, None)


def test_every_file_says_whether_its_script_wrote_it_encrypted(catalog):
    """The operator, 2026-10-30: the page shows whether each backup file is encrypted. The answer is
    the script's own - it wrote the file WITH ENCRYPTION (SQL Server) or SET ENCRYPTION ON (RMAN)
    exactly when a passphrase was given - and a line that does not say stays unknown, never "no"."""
    job = _job()
    lines = [_line("APPDB", "FULL", "/b/a.bak").replace("\tpath=", "\tencrypted=yes\tpath="),
             _line("APPDB", "FULL", "/b/b.bak").replace("\tpath=", "\tencrypted=no\tpath="),
             _line("APPDB", "FULL", "/b/c.bak")]
    assert [item["encrypted"] for item in I.parse_file_lines("\n".join(lines))] == [1, 0, None]
    backup_module.record_in_catalog(catalog, job, _result(job, lines), job_run_id=1,
                                    started_at="2026-10-29T01:00:00Z", finished_at="2026-10-29T01:10:00Z")
    rows = {r["file_name"]: r["encrypted"] for r in catalog.backup_history(server_id="ACME-192-0-2-250")}
    assert rows == {"a.bak": 1, "b.bak": 0, "c.bak": None}


@pytest.mark.parametrize("script, says", [
    ("sqlserver/mssql_backup_database.sh", 'encrypt_clause" ] && f_encrypted=yes'),
    ("sqlserver/mssql_backup_database.ps1", "$fileEncrypted = if ($encPassword)"),
    ("oracle/oracle_rman_database.sh", 'enc_line" ] && p_encrypted=yes'),
    ("oracle/oracle_rman_archivelog.sh", 'enc_line" ] && p_encrypted=yes'),
    ("postgresql/pg_basebackup_database.sh", r"\tencrypted=no\t"),
    ("postgresql/pg_archive_wal.sh", r"\tencrypted=no\t"),
])
def test_every_backup_script_states_encrypted_on_each_file_line(script, says):
    text = (SCRIPTS / script).read_text(encoding="utf-8")
    assert says in text
    assert "encrypted=" in text[text.index("FILE"):], script
