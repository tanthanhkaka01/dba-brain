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
* the check runs only on what already answered, and ``checkdb: false`` leaves it out.
"""

from __future__ import annotations

from types import SimpleNamespace

from dbabrain.common import verifyrestore


def _runs(monkeypatch, *answers):
    """Each call to the host gets the next (stdout, exit_code); the commands are kept."""
    seen: list[str] = []
    queue = list(answers)

    def run(host, command, timeout=0):
        seen.append(command)
        out, code = queue.pop(0)
        return {"stdout": out, "stderr": "", "exit_code": code}

    monkeypatch.setattr(verifyrestore, "run", run)
    monkeypatch.setattr(verifyrestore, "parse_host", lambda host: host)
    return seen


def test_postgresql_runs_pg_amcheck_on_every_database(monkeypatch):
    seen = _runs(monkeypatch, ("f\n3\n", 0), ("", 0))
    answer = verifyrestore.verify({"db_type": "postgresql", "host": {}, "consistency": True})
    assert answer["ok"] and "pg_amcheck clean" in answer["databases"][0]["detail"]
    assert "pg_amcheck -U postgres --all --install-missing --heapallindexed" in seen[1]


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
