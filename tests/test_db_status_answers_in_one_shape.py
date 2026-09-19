"""One command answers "what state is this server in", for every engine and at three depths.

Asked for on 2026-09-16, after a restore reported `done` over a database nobody could open: *the
DB status query must be a common CLI, JSON object in, JSON object out, so it can be reused; and it
should cover instance status, each database, and each schema, for SQL Server, PostgreSQL and
Oracle.*

The question was already being asked in several places — the restore workflow, the SLA grader, the
fleet page, `/spbot_*` commands — and each answered it its own way, which is how three callers grow
three notions of "online" and disagree about the same server on the same afternoon.

Two properties are the whole point and are asserted here rather than left to the engines:

* **The instance is checked at every depth.** A verdict about a database on an instance nobody
  could reach is a guess. When the instance does not answer, nothing deeper is attempted — asking
  anyway produces a second, less useful copy of the same failure.
* **A state column is never the whole answer.** A real statement is run at the depth asked for,
  because a database can read ONLINE and still refuse a query while it finishes an upgrade step.
"""

from __future__ import annotations

import pytest

from db_ops.common import db_catalog, dbstatus


@pytest.fixture
def server(monkeypatch):
    """A fake SQL Server: one reachable instance, three databases, one of them still RESTORING."""
    state = {
        "instance": [{"version": "16.0.1000", "edition": "Developer", "state": "ONLINE"}],
        "databases": {"SALES": "ONLINE", "ORDERS": "ONLINE", "STAGING": "RESTORING"},
        "answers": True,
        "queries": [],
    }

    monkeypatch.setattr(db_catalog, "_resolve",
                        lambda parsed: {"db_type": "sqlserver", "server_id": "ACME-SQL01"})

    def fake_query(parsed, sql, *, database=""):
        state["queries"].append((sql, database))
        if "SERVERPROPERTY" in sql:
            if state["instance"] is None:
                raise db_catalog.DbCatalogError("connect failed: host unreachable")
            return state["instance"]
        if not state["answers"]:
            raise db_catalog.DbCatalogError("Msg 942: database cannot be opened")
        return [{"n": 7}]

    monkeypatch.setattr(db_catalog, "_query", fake_query)
    monkeypatch.setattr(db_catalog, "list_databases", lambda request: {
        "databases": [{"name": n, "state": s} for n, s in state["databases"].items()]})
    monkeypatch.setattr(db_catalog, "list_schemas", lambda request: {
        "schemas": [{"name": "dbo"}, {"name": "sales"}]})
    return state


def test_the_default_depth_is_the_instance(server) -> None:
    answer = dbstatus.status({"target": "ACME-SQL01"})

    assert answer["depth"] == "instance"
    assert answer["ok"] is True
    assert answer["instance"]["state"] == "ONLINE"
    assert answer["instance"]["version"] == "16.0.1000"
    assert answer["items"] == []


def test_an_unreachable_instance_is_a_result_not_an_error(server) -> None:
    """The caller asked what state the server is in, and "unreachable" is the state. Raising would
    make every caller write the same try/except to learn the same thing."""
    server["instance"] = None

    answer = dbstatus.status({"target": "ACME-SQL01"})

    assert answer["ok"] is False
    assert answer["instance"]["state"] == "UNREACHABLE"
    assert "unreachable" in answer["instance"]["detail"]


def test_nothing_deeper_is_attempted_when_the_instance_is_down(server) -> None:
    server["instance"] = None

    answer = dbstatus.status({"target": "ACME-SQL01", "depth": "database"})

    assert answer["items"] == []
    assert answer["ok"] is False
    assert [sql for sql, _db in server["queries"] if "sys.tables" in sql] == []


def test_a_database_still_restoring_is_not_usable(server) -> None:
    """The state this project keeps meeting: the restore command returned, the chain was never
    recovered, and the database is not there."""
    answer = dbstatus.status({"target": "ACME-SQL01", "depth": "database"})

    by_name = {item["name"]: item for item in answer["items"]}
    assert by_name["STAGING"]["ok"] is False
    assert by_name["STAGING"]["state"] == "RESTORING"
    assert by_name["SALES"]["ok"] is True
    assert answer["ok"] is False
    assert answer["failed"] == 1


def test_a_real_query_is_run_and_not_just_the_state_column(server) -> None:
    dbstatus.status({"target": "ACME-SQL01", "depth": "database", "databases": ["SALES"]})

    assert ("SELECT COUNT(*) AS n FROM sys.tables", "SALES") in server["queries"]


def test_a_database_that_reads_online_and_refuses_a_query_fails(server) -> None:
    """The reason the state column is not enough. ONLINE and unusable is a real state, and the
    only way to tell is to ask it something."""
    server["answers"] = False

    answer = dbstatus.status({"target": "ACME-SQL01", "depth": "database",
                              "databases": ["SALES"]})

    assert answer["items"][0]["ok"] is False
    assert "cannot be opened" in answer["items"][0]["detail"]


def test_a_database_that_is_not_there_is_named_rather_than_skipped(server) -> None:
    """Silence here is how an empty check passes for a database a restore never created."""
    answer = dbstatus.status({"target": "ACME-SQL01", "depth": "database",
                              "databases": ["NEVER_RESTORED"]})

    assert answer["items"][0]["state"] == "ABSENT"
    assert answer["items"][0]["ok"] is False
    assert answer["failed"] == 1


def test_asking_for_nothing_in_particular_checks_them_all(server) -> None:
    answer = dbstatus.status({"target": "ACME-SQL01", "depth": "database"})

    assert {item["name"] for item in answer["items"]} == {"SALES", "ORDERS", "STAGING"}


def test_a_schema_that_the_login_cannot_see_is_reported(server) -> None:
    answer = dbstatus.status({"target": "ACME-SQL01", "depth": "schema",
                              "database": "APPDB", "schemas": ["sales", "hidden"]})

    by_name = {item["name"]: item for item in answer["items"]}
    assert by_name["sales"]["ok"] is True
    assert by_name["hidden"]["ok"] is False
    assert by_name["hidden"]["state"] == "ABSENT"


def test_a_schema_depth_without_a_database_is_refused_on_sqlserver(server) -> None:
    """A schema lives inside a database. Without one, the answer would describe whatever the
    login's default happens to be — a different server's answer to a different question."""
    with pytest.raises(dbstatus.DbStatusError, match="database"):
        dbstatus.status({"target": "ACME-SQL01", "depth": "schema"})


def test_an_unknown_depth_is_refused_by_name(server) -> None:
    with pytest.raises(dbstatus.DbStatusError, match="depth must be one of"):
        dbstatus.status({"target": "ACME-SQL01", "depth": "table"})


def test_the_answer_has_the_same_shape_whichever_engine_replied(server, monkeypatch) -> None:
    """A caller should not need to know which engine it is talking to. Every engine answers with
    the same keys; only the values differ."""
    shape = set(dbstatus.status({"target": "ACME-SQL01"}))

    monkeypatch.setattr(db_catalog, "_resolve",
                        lambda parsed: {"db_type": "postgresql", "server_id": "ACME-PG"})
    monkeypatch.setattr(db_catalog, "_query",
                        lambda parsed, sql, *, database="": [{"version": "PostgreSQL 18",
                                                              "state": "ACCEPTING"}])
    postgres = dbstatus.status({"target": "ACME-PG"})

    assert set(postgres) == shape
    assert postgres["ok"] is True
    assert postgres["db_type"] == "postgresql"


def test_a_postgresql_cluster_still_in_recovery_is_not_usable(server, monkeypatch) -> None:
    """Its version of "restored but not usable": the server is up, accepts a connection, and
    refuses every write."""
    monkeypatch.setattr(db_catalog, "_resolve",
                        lambda parsed: {"db_type": "postgresql", "server_id": "ACME-PG"})
    monkeypatch.setattr(db_catalog, "_query",
                        lambda parsed, sql, *, database="": [{"version": "PostgreSQL 18",
                                                              "state": "IN RECOVERY"}])

    answer = dbstatus.status({"target": "ACME-PG"})

    assert answer["ok"] is False
    assert answer["instance"]["state"] == "IN RECOVERY"


def test_an_oracle_instance_open_with_the_database_only_mounted_is_not_usable(
        server, monkeypatch) -> None:
    """RMAN finishes, the instance is OPEN, and the database is MOUNTED. Reading the instance
    alone answers "up" about a database nobody can query — so both are read, and the database's
    open_mode is what decides."""
    monkeypatch.setattr(db_catalog, "_resolve",
                        lambda parsed: {"db_type": "oracle", "server_id": "ACME-ORA"})
    monkeypatch.setattr(db_catalog, "_query",
                        lambda parsed, sql, *, database="": [{"version": "19.0.0", "status": "OPEN",
                                                              "state": "MOUNTED"}])

    answer = dbstatus.status({"target": "ACME-ORA"})

    assert answer["ok"] is False
    assert answer["instance"]["state"] == "MOUNTED"
    assert answer["instance"]["instance_status"] == "OPEN"


def test_an_engine_this_command_cannot_answer_for_is_refused_by_name(server, monkeypatch) -> None:
    monkeypatch.setattr(db_catalog, "_resolve",
                        lambda parsed: {"db_type": "mysql", "server_id": "ACME-MY"})

    with pytest.raises(dbstatus.DbStatusError, match="mysql"):
        dbstatus.status({"target": "ACME-MY"})


def test_a_request_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(dbstatus.DbStatusError, match="JSON object"):
        dbstatus.status(["target"])
