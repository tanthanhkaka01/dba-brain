"""A restored database is held to its engine's consistency check, not only to "open and answers"
(0.35.0, 1.251 - gap G11).

The share-driven SQL Server restore has run ``DBCC CHECKDB`` since 0.26.0. A script-driven drill -
a SQL Server container, PostgreSQL, Oracle - checked that the database opened and answered a query,
which a database with corrupt pages does. ``verify-restore`` now takes ``consistency: true``, and
``restore-by-id`` sets it from the entry's ``checkdb`` (default true):

* PostgreSQL: ``pg_amcheck --all --install-missing --heapallindexed``; a cluster without the
  program is *not checked*, which is not clean;
* Oracle: RMAN ``VALIDATE CHECK LOGICAL DATABASE``, then ``v$database_block_corruption`` must be empty;
* SQL Server: ``DBCC CHECKDB ... WITH NO_INFOMSGS`` per database;
* the check runs only on what already answered, and ``checkdb: false`` leaves it out;
* how long it may run and on how many connections are the entry's ``checkdb_timeout_seconds`` and
  ``checkdb_jobs`` (0.37.0) - the store's 60 GB drill ran past the old fixed hour on one connection
  and finished in 47 minutes on four; the limit is kept on the host, so a check never outlives the call.
"""

from __future__ import annotations

from types import SimpleNamespace

from dbabrain.common import verifyrestore


def _runs(monkeypatch, *answers):
    """Each call to the host gets the next (stdout, exit_code); the commands are kept."""
    seen: list[str] = []
    timeouts: list[int] = []
    queue = list(answers)

    def run(host, command, timeout=0):
        seen.append(command)
        timeouts.append(timeout)
        out, code = queue.pop(0)
        return {"stdout": out, "stderr": "", "exit_code": code}

    monkeypatch.setattr(verifyrestore, "run", run)
    monkeypatch.setattr(verifyrestore, "parse_host", lambda host: host)
    seen_timeouts.clear()
    seen_timeouts.extend([timeouts])
    return seen


seen_timeouts: list[list[int]] = []


def _timeouts() -> list[int]:
    return seen_timeouts[0]


def test_postgresql_runs_pg_amcheck_on_every_database(monkeypatch):
    seen = _runs(monkeypatch, ("f\n3\n", 0), ("", 0))
    answer = verifyrestore.verify({"db_type": "postgresql", "host": {}, "consistency": True})
    assert answer["ok"] and "pg_amcheck clean" in answer["databases"][0]["detail"]
    assert '"$AM" -U postgres --all --install-missing --heapallindexed' in seen[1]
    assert "--jobs" not in seen[1], "one connection unless the entry says more"
    # Found off PATH too: the PGDG packages give pg_amcheck no /usr/bin wrapper (0.37.0).
    assert "/usr/lib/postgresql/*/bin/pg_amcheck" in seen[1]


def test_postgresql_corruption_fails_the_drill(monkeypatch):
    _runs(monkeypatch, ("f\n3\n", 0), ("heap table \"public.t\", block 7: invalid line pointer", 2))
    row = verifyrestore.verify({"db_type": "postgresql", "host": {}, "consistency": True})["databases"][0]
    assert not row["ok"] and "invalid line pointer" in row["detail"]


def test_postgresql_without_pg_amcheck_is_not_called_clean(monkeypatch):
    _runs(monkeypatch, ("f\n3\n", 0), ("PG_AMCHECK_MISSING", 3))
    row = verifyrestore.verify({"db_type": "postgresql", "host": {}, "consistency": True})["databases"][0]
    assert not row["ok"] and "not checked" in row["detail"]


def test_oracle_validates_and_reads_the_corruption_view(monkeypatch):
    seen = _runs(monkeypatch, ("READ WRITE 1", 0), ("Finished validate", 0), ("CORRUPT_BLOCKS=0", 0))
    answer = verifyrestore.verify({"db_type": "oracle", "host": {}, "oracle_sid": "FREE", "consistency": True})
    assert answer["ok"] and "no corrupt block" in answer["databases"][0]["detail"]
    assert "VALIDATE CHECK LOGICAL DATABASE" in seen[1] and "ORACLE_SID=FREE" in seen[1]


def test_oracle_corrupt_blocks_fail_the_drill(monkeypatch):
    _runs(monkeypatch, ("READ WRITE 1", 0), ("Finished validate", 0), ("CORRUPT_BLOCKS=4", 0))
    row = verifyrestore.verify({"db_type": "oracle", "host": {}, "consistency": True})["databases"][0]
    assert not row["ok"] and "CORRUPT_BLOCKS=4" in row["detail"]


def test_a_database_that_did_not_open_is_not_validated(monkeypatch):
    seen = _runs(monkeypatch, ("MOUNTED", 0))
    assert not verifyrestore.verify({"db_type": "oracle", "host": {}, "consistency": True})["ok"]
    assert len(seen) == 1


def test_without_consistency_nothing_more_runs(monkeypatch):
    seen = _runs(monkeypatch, ("f\n3\n", 0))
    assert verifyrestore.verify({"db_type": "postgresql", "host": {}})["ok"] and len(seen) == 1


def test_sql_server_runs_checkdb_on_each_database(monkeypatch):
    from dbabrain.common import db_catalog, sql_run

    asked: list[str] = []

    def query_rows(cursor, sql):
        asked.append(sql)
        if sql.startswith("DBCC CHECKDB([BROKEN])"):
            raise RuntimeError("Msg 8928: Object ID 5, page (1:153) could not be processed")
        return [{"n": 4}]

    connection = SimpleNamespace(cursor=lambda: None, close=lambda: None)
    monkeypatch.setattr("dbabrain.common.db_connect.connect_engine", lambda **k: connection)
    monkeypatch.setattr(db_catalog, "databases", lambda cursor, engine: [
        {"name": "SALESDB", "state": "ONLINE"}, {"name": "BROKEN", "state": "ONLINE"}])
    monkeypatch.setattr(sql_run, "query_rows", query_rows)

    answer = verifyrestore.verify({"db_type": "sqlserver", "target": {"host": "192.0.2.40"}, "consistency": True})
    rows = {r["database_name"]: r for r in answer["databases"]}
    assert rows["SALESDB"]["ok"] and "DBCC CHECKDB clean" in rows["SALESDB"]["detail"]
    assert not rows["BROKEN"]["ok"] and "Msg 8928" in rows["BROKEN"]["detail"]
    assert "DBCC CHECKDB([SALESDB]) WITH NO_INFOMSGS" in asked


def test_restore_by_id_asks_for_it_from_the_entry(monkeypatch):
    from dbabrain.backup_restore import restore_by_id

    monkeypatch.setattr(restore_by_id, "_visible_dir", lambda job: "/b")
    for checkdb in (True, False):
        job = SimpleNamespace(restore_id="ORA", env={"ORACLE_SID": "FREE"}, env_secrets={}, checkdb=checkdb)
        steps = restore_by_id._plan_oracle(job, {}, point_in_time="", host={}, data_dir=None)
        assert steps[-1]["request"]["consistency"] is checkdb


# --------------------------------------------------------------------------- #
# How long, and on how many connections (0.37.0)
# --------------------------------------------------------------------------- #
def test_postgresql_checks_on_the_stated_jobs_within_the_stated_limit_kept_on_the_host(monkeypatch):
    seen = _runs(monkeypatch, ("f\n3\n", 0), ("", 0))
    answer = verifyrestore.verify({"db_type": "postgresql", "host": {}, "consistency": True,
                                   "consistency_timeout_seconds": 7200, "consistency_jobs": 4})
    assert answer["ok"]
    assert "--jobs 4 --all" in seen[1]
    # The host stops the check at the limit; the call waits a little longer, so it never outlives it.
    assert "T=$(command -v timeout); ${T:+$T 7200}" in seen[1]
    assert _timeouts()[1] == 7200 + 60


def test_without_a_stated_limit_the_check_keeps_the_hour_it_always_had(monkeypatch):
    seen = _runs(monkeypatch, ("f\n3\n", 0), ("", 0))
    verifyrestore.verify({"db_type": "postgresql", "host": {}, "consistency": True})
    assert "${T:+$T 3600}" in seen[1] and _timeouts()[1] == 3660


def test_a_check_stopped_at_its_limit_is_not_checked_and_says_which_field_to_raise(monkeypatch):
    _runs(monkeypatch, ("f\n3\n", 0), ("PG_AMCHECK_TIMED_OUT", 124))
    row = verifyrestore.verify({"db_type": "postgresql", "host": {}, "consistency": True,
                                "consistency_timeout_seconds": 600})["databases"][0]
    assert not row["ok"] and "within 600 s" in row["detail"] and "checkdb_timeout_seconds" in row["detail"]


def test_the_command_sent_turns_a_stopped_check_into_its_own_word(monkeypatch, tmp_path):
    """The command itself, run by a real shell with a pg_amcheck that is stopped (exit 124, what
    `timeout` answers) and with one that is clean: only the first prints the token."""
    import os
    import shutil
    import subprocess

    import pytest

    bash = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("sh")
    if not bash or not os.path.exists(bash):
        pytest.skip("no POSIX shell here")
    seen = _runs(monkeypatch, ("f\n3\n", 0), ("", 0))
    verifyrestore.verify({"db_type": "postgresql", "host": {}, "consistency": True})
    tools = tmp_path / "bin"
    tools.mkdir()
    for code, expected in ((124, "PG_AMCHECK_TIMED_OUT"), (0, "")):
        (tools / "pg_amcheck").write_text(f"#!/bin/sh\nexit {code}\n", encoding="utf-8", newline="\n")
        (tools / "timeout").write_text('#!/bin/sh\nshift\nexec "$@"\n', encoding="utf-8", newline="\n")
        command = f'export PATH="{_posix(tools)}:$PATH"; chmod +x "{_posix(tools)}"/*; ' + seen[1]
        done = subprocess.run([bash, "-c", command], capture_output=True, text=True)
        assert done.stdout.strip() == expected and done.returncode == code


def _posix(path) -> str:
    import os
    text = str(path)
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


def test_oracle_validate_runs_within_the_stated_limit(monkeypatch):
    _runs(monkeypatch, ("READ WRITE 1", 0), ("Finished validate", 0), ("CORRUPT_BLOCKS=0", 0))
    verifyrestore.verify({"db_type": "oracle", "host": {}, "consistency": True,
                          "consistency_timeout_seconds": 5400})
    assert _timeouts()[1] == 5400


def test_a_malformed_limit_is_refused_before_anything_is_reached(monkeypatch):
    import pytest

    def run(*_a, **_k):
        raise AssertionError("reached")
    monkeypatch.setattr(verifyrestore, "run", run)
    for bad in ({"consistency_timeout_seconds": 59}, {"consistency_timeout_seconds": "3600"},
                {"consistency_timeout_seconds": True}, {"consistency_jobs": 0}, {"consistency_jobs": 17},
                {"consistency_jobs": 2.5}):
        with pytest.raises(verifyrestore.VerifyError):
            verifyrestore.verify({"db_type": "postgresql", "host": {}, "consistency": True, **bad})


def test_an_entry_states_its_limits_and_absent_keeps_the_old_ones():
    import pytest

    from dbabrain.lib import consistency_check as C

    assert C.parse_timeout(None) == 3600 and C.parse_jobs(None) == 1
    assert C.parse_timeout(7200) == 7200 and C.parse_jobs(4) == 4
    for bad in (0, 59, 86401, "7200", True, 1.5):
        with pytest.raises(C.ConsistencyCheckError, match="checkdb_timeout_seconds"):
            C.parse_timeout(bad, where="restores[0]")
    for bad in (0, 17, "4", False):
        with pytest.raises(C.ConsistencyCheckError, match="checkdb_jobs"):
            C.parse_jobs(bad)


def test_restore_by_id_passes_the_entrys_limits_on_every_engine(monkeypatch):
    from dbabrain.backup_restore import restore_by_id

    monkeypatch.setattr(restore_by_id, "_visible_dir", lambda job: "/b")
    job = SimpleNamespace(restore_id="ORA", env={"ORACLE_SID": "FREE"}, env_secrets={}, checkdb=True,
                          checkdb_timeout_seconds=7200, checkdb_jobs=4)
    request = restore_by_id._plan_oracle(job, {}, point_in_time="", host={}, data_dir=None)[-1]["request"]
    assert request["consistency_timeout_seconds"] == 7200 and request["consistency_jobs"] == 4


def test_a_script_restore_entry_reads_its_limits_and_refuses_a_bad_one(tmp_path):
    import json

    import pytest

    from dbabrain.backup_restore.restore_script import load_script_restores

    def load(**over):
        entry = {"restore_id": "STORE_DRILL", "db_type": "postgresql", "server_id": "SRC",
                 "target_server_id": "TGT", "backup_dir": "/out", "script": "postgresql",
                 "target_backup_dir": "/opt/db_ops/restore_staging/STORE_DRILL", "source_backup_host_dir": "/opt/out",
                 "cleanup_retention": 86400, "time_window": {"from_hour": 0, "to_hour": 23},
                 "env": {"PG_OS_USER": "postgres"}, **over}
        path = tmp_path / "restore_config.json"
        path.write_text(json.dumps({"backup_restore": {"restores": [entry]}}), encoding="utf-8")
        return load_script_restores(path)

    job = load()[0]
    assert (job.checkdb_timeout_seconds, job.checkdb_jobs) == (3600, 1)
    job = load(checkdb_timeout_seconds=7200, checkdb_jobs=4)[0]
    assert (job.checkdb_timeout_seconds, job.checkdb_jobs) == (7200, 4)
    with pytest.raises(Exception, match="checkdb_jobs"):  # noqa: B017 - the loader's own error type
        load(checkdb_jobs=40)
