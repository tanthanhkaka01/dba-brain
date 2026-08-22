""""Which database?", then "which schema?" — asked once, in four engines' vocabularies.

Every caller that loads a file, runs an ad-hoc query or prompts an operator has to answer these
two questions first, and each was answering them with its own `SELECT name FROM sys.databases`.
One place, four dialects.

The tests run offline: `_query` is replaced with canned rows, so what is under test is the part
that is actually easy to get wrong — deciding what counts as a system object per engine, folding
Oracle's upper-cased column names so a caller can read `row["name"]` on every engine, and
choosing between Oracle's two shapes.

That last one matters most. `v$containers` exists on 12c+ and not on a non-CDB or anything older,
and its absence is **not** a failure to report — it is the other shape of the same answer. An
8i host reached over the legacy bridge has to come back looking like everything else.
"""

from __future__ import annotations

import pytest

from db_ops.common import db_catalog


def _stub(monkeypatch, *, db_type: str, rows, resolved_extra=None):
    """Answer every query with ``rows``; resolve the target without touching config."""
    resolved = {"server_id": "TEST-1", "db_type": db_type, "ip": "10.0.0.1", "port": 1433,
                "credential_name": "c", "username": "u", "database_name": "DB1"}
    resolved.update(resolved_extra or {})
    monkeypatch.setattr(db_catalog, "_resolve", lambda parsed: resolved)

    calls: list[str] = []

    def fake_query(parsed, sql, *, database=""):
        calls.append(sql)
        answer = rows(sql) if callable(rows) else rows
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(db_catalog, "_query", fake_query)
    return calls


# --------------------------------------------------------------------------- #
# Databases
# --------------------------------------------------------------------------- #

def test_sql_server_system_databases_are_hidden_unless_asked_for(monkeypatch):
    """A caller asking "where do I load this spreadsheet" never means master, and offering it
    invites picking it."""
    rows = [
        {"name": "master", "state": "ONLINE", "is_system": 1},
        {"name": "msdb", "state": "ONLINE", "is_system": 1},
        {"name": "APPDB", "state": "ONLINE", "is_system": 0},
    ]
    _stub(monkeypatch, db_type="sqlserver", rows=rows)

    hidden = db_catalog.list_databases({"target": "TEST-1"})
    shown = db_catalog.list_databases({"target": "TEST-1", "include_system": True})

    assert [db["name"] for db in hidden["databases"]] == ["APPDB"]
    assert hidden["system_hidden"] == 2
    assert len(shown["databases"]) == 3 and shown["system_hidden"] == 0


def test_the_state_each_engine_reports_is_carried_through_not_flattened(monkeypatch):
    """`state_desc` is what a SQL Server operator reads; renaming it to a common vocabulary
    would mean translating back before it is useful."""
    _stub(monkeypatch, db_type="sqlserver",
          rows=[{"name": "APPDB", "state": "RESTORING", "recovery_model": "FULL",
                 "is_system": 0}])

    data = db_catalog.list_databases({"target": "TEST-1"})

    assert data["databases"][0]["state"] == "RESTORING"
    assert data["databases"][0]["recovery_model"] == "FULL"


def test_postgres_templates_count_as_system(monkeypatch):
    _stub(monkeypatch, db_type="postgresql", rows=[
        {"name": "template0", "state": "NO_CONNECT", "is_template": True},
        {"name": "template1", "state": "ONLINE", "is_template": True},
        {"name": "postgres", "state": "ONLINE", "is_template": False},
        {"name": "app", "state": "ONLINE", "is_template": False},
    ])

    data = db_catalog.list_databases({"target": "TEST-1"})

    assert [db["name"] for db in data["databases"]] == ["app"]


def test_mysql_says_that_its_databases_are_also_its_schemas(monkeypatch):
    """There is no layer between the two. Silently returning the same list twice from two
    commands, with no explanation, reads as a bug."""
    _stub(monkeypatch, db_type="mysql", rows=[{"name": "app", "state": "ONLINE"}])

    data = db_catalog.list_databases({"target": "TEST-1"})

    assert "no layer between server and schema" in data["note"]


def test_an_engine_the_command_does_not_know_is_named_in_the_refusal(monkeypatch):
    _stub(monkeypatch, db_type="host", rows=[])

    with pytest.raises(db_catalog.DbCatalogError, match="does not know engine 'host'"):
        db_catalog.list_databases({"target": "TEST-1"})


# --------------------------------------------------------------------------- #
# Oracle has two shapes and the caller must not have to care
# --------------------------------------------------------------------------- #

def test_a_cdb_reports_its_root_its_seed_and_every_pdb(monkeypatch):
    """`con_id` is what says which is which: 1 is the root, 2 is the seed, everything above is a
    real PDB. Reporting them undifferentiated would offer PDB$SEED as somewhere to put a table."""
    _stub(monkeypatch, db_type="oracle", rows=[
        {"CON_ID": 1, "NAME": "CDB$ROOT", "OPEN_MODE": "READ WRITE", "RESTRICTED": "NO"},
        {"CON_ID": 2, "NAME": "PDB$SEED", "OPEN_MODE": "READ ONLY", "RESTRICTED": "NO"},
        {"CON_ID": 3, "NAME": "FREEPDB1", "OPEN_MODE": "READ WRITE", "RESTRICTED": "NO"},
    ])

    data = db_catalog.list_databases({"target": "TEST-1", "include_system": True})

    assert data["container_type"] == "CDB"
    kinds = {db["name"]: db["kind"] for db in data["databases"]}
    assert kinds == {"CDB$ROOT": "CDB$ROOT", "PDB$SEED": "SEED", "FREEPDB1": "PDB"}


def test_the_root_and_the_seed_are_system_so_only_real_pdbs_are_offered(monkeypatch):
    _stub(monkeypatch, db_type="oracle", rows=[
        {"CON_ID": 1, "NAME": "CDB$ROOT", "OPEN_MODE": "READ WRITE"},
        {"CON_ID": 2, "NAME": "PDB$SEED", "OPEN_MODE": "READ ONLY"},
        {"CON_ID": 3, "NAME": "FREEPDB1", "OPEN_MODE": "READ WRITE"},
    ])

    data = db_catalog.list_databases({"target": "TEST-1"})

    assert [db["name"] for db in data["databases"]] == ["FREEPDB1"]
    assert data["system_hidden"] == 2


def test_a_non_cdb_falls_back_to_v_dollar_database_instead_of_failing(monkeypatch):
    """`v$containers` does not exist on a non-CDB or before 12c. That is the other shape of the
    answer, not an error — an 8i host over the legacy bridge has to come back like the rest."""
    def rows(sql):
        if "v$containers" in sql:
            return db_catalog.DbCatalogError("SQL failed: ORA-00942: table or view does not exist")
        return [{"NAME": "LEGACYDB", "OPEN_MODE": "READ WRITE", "DATABASE_ROLE": "PRIMARY",
                 "LOG_MODE": "NOARCHIVELOG"}]

    _stub(monkeypatch, db_type="oracle", rows=rows)

    data = db_catalog.list_databases({"target": "TEST-1"})

    assert data["container_type"] == "NON_CDB"
    assert [db["name"] for db in data["databases"]] == ["LEGACYDB"]
    assert data["databases"][0]["kind"] == "DATABASE"
    assert "not a CDB" in data["note"]


def test_oracle_column_names_are_folded_so_one_caller_reads_every_engine(monkeypatch):
    """Oracle upper-cases every unquoted identifier, so `row["name"]` finds nothing there and
    everything elsewhere — a caller would need a branch per engine to read one field."""
    _stub(monkeypatch, db_type="oracle", rows=[
        {"CON_ID": 3, "NAME": "FREEPDB1", "OPEN_MODE": "READ WRITE"},
    ])

    data = db_catalog.list_databases({"target": "TEST-1"})

    assert set(data["databases"][0]) >= {"name", "con_id", "kind", "open_mode"}


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

def test_a_schema_listing_without_a_database_is_refused_not_defaulted(monkeypatch):
    """Defaulting would answer about master (SQL Server) or the login's default (PostgreSQL) and
    look exactly like a correct answer about the database the operator meant."""
    _stub(monkeypatch, db_type="sqlserver", rows=[])

    with pytest.raises(db_catalog.DbCatalogError, match="Run list-databases first"):
        db_catalog.list_schemas({"target": "TEST-1"})


def test_the_named_database_is_the_one_the_query_runs_in(monkeypatch):
    """Schemas are per database. Connecting elsewhere and listing its schemas is a wrong answer
    that raises no error at all."""
    seen: dict[str, str] = {}
    monkeypatch.setattr(db_catalog, "_resolve", lambda parsed: {
        "server_id": "TEST-1", "db_type": "sqlserver", "database_name": "APPDB",
        "credential_name": "c", "username": "u"})

    def fake_query(parsed, sql, *, database=""):
        seen["database"] = database
        return [{"name": "dbo", "owner": "dbo"}, {"name": "sys", "owner": "sys"},
                {"name": "staging", "owner": "dbo"}]

    monkeypatch.setattr(db_catalog, "_query", fake_query)

    data = db_catalog.list_schemas({"target": "TEST-1", "database": "APPDB"})

    assert seen["database"] == "APPDB"
    assert data["database"] == "APPDB"
    # `sys` is machinery and hidden; `dbo` is not — it is where user tables live by default and
    # the schema a load is most often aimed at. Hiding it would empty the list on most databases.
    assert [s["name"] for s in data["schemas"]] == ["dbo", "staging"]


def test_oracle_system_schemas_are_hidden_so_the_list_is_the_applications(monkeypatch):
    """An Oracle instance has dozens of shipped schemas. Showing SYS next to LTR makes the one
    the operator wants hard to find, and picking the wrong one is a real mistake."""
    _stub(monkeypatch, db_type="oracle", rows=[
        {"NAME": "SYS", "OWNER": "SYS"},
        {"NAME": "SYSTEM", "OWNER": "SYSTEM"},
        {"NAME": "XDB", "OWNER": "XDB"},
        {"NAME": "LTR", "OWNER": "LTR"},
    ], resolved_extra={"database_name": "LEGACYAPP"})

    data = db_catalog.list_schemas({"target": "TEST-1", "database": "LEGACYAPP"})

    assert [s["name"] for s in data["schemas"]] == ["LTR"]
    assert data["system_hidden"] == 3


def test_postgres_internal_schemas_are_matched_by_prefix_not_by_a_list(monkeypatch):
    """`pg_toast`, `pg_temp_3`, `pg_toast_temp_3` — the set is not fixed, so a literal list goes
    stale and starts showing internals."""
    _stub(monkeypatch, db_type="postgresql", rows=[
        {"name": "pg_catalog", "owner": "postgres"},
        {"name": "pg_toast_temp_7", "owner": "postgres"},
        {"name": "information_schema", "owner": "postgres"},
        {"name": "app", "owner": "app_owner"},
    ], resolved_extra={"database_name": "appdb"})

    data = db_catalog.list_schemas({"target": "TEST-1", "database": "appdb"})

    assert [s["name"] for s in data["schemas"]] == ["app"]


def test_a_request_with_no_target_is_refused_by_both_commands():
    for runner in (db_catalog.list_databases, db_catalog.list_schemas):
        with pytest.raises(db_catalog.DbCatalogError, match='needs a "target"'):
            runner({})


def test_a_request_that_is_not_an_object_is_refused():
    with pytest.raises(db_catalog.DbCatalogError, match="must be a JSON object"):
        db_catalog.list_databases(["ACME-1"])
