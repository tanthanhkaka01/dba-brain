"""Reaching a database, on whichever engine the target names.

The engine-specific connect code used to live inside the metrics app, so `common.sql_run` — the
engine behind `/spbot_sql_to_xlsx` and `python -m db_ops.common.cli run-sql` — knew only SQL
Server and answered "run this SELECT on that PostgreSQL box" with
*"only sqlserver is supported"*. Two implementations of one thing, disagreeing on which engines
exist. These tests pin the shared one: every engine reachable, sensible per-engine defaults, the
timeout enforced *inside* the server, and a missing driver reported as an operator message
rather than an ImportError traceback.
"""

from __future__ import annotations

import sys
import types

import pytest

from db_ops.lib.target_profile import TargetProfile
from db_ops.common import db_connect, sql_run


class _Cursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, *args):
        self.log.append(sql)

    def close(self):
        pass


class _Conn:
    def __init__(self, log):
        self.log = log
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return _Cursor(self.log)

    def close(self):
        self.closed = True


# --------------------------------------------------------------------------- #
# Engine coverage
# --------------------------------------------------------------------------- #
def test_every_engine_db_ops_monitors_can_be_reached():
    """If an engine appears in db_instances.json it must be connectable here — this tuple is
    what sql_run checks a target against before refusing it."""
    assert set(db_connect.SUPPORTED_DB_TYPES) == {"sqlserver", "postgresql", "mysql", "oracle"}


@pytest.mark.parametrize("spelling,expected", [
    ("mssql", "sqlserver"), ("SQL_Server", "sqlserver"), ("sqlserver", "sqlserver"),
    ("postgres", "postgresql"), ("PG", "postgresql"), ("postgresql", "postgresql"),
    ("mariadb", "mysql"), ("MySQL", "mysql"),
    ("oracle", "oracle"),
])
def test_the_spellings_that_appear_in_config_all_resolve(spelling, expected):
    """db_instances.json and the target specs people type use several names for the same engine;
    a target refused because it said 'mssql' would be a confusing way to fail."""
    assert db_connect.normalize_db_type(spelling) == expected


def test_an_unknown_engine_is_refused_by_name():
    with pytest.raises(db_connect.DbConnectError, match="Unsupported db_type"):
        db_connect.connect_engine(db_type="cassandra", host="h", username="u", password="p")


# --------------------------------------------------------------------------- #
# Per-engine behaviour
# --------------------------------------------------------------------------- #
def test_postgres_gets_its_statement_timeout_set_inside_the_server(monkeypatch):
    """A connect timeout does not cover the case this exists for: the socket stays healthy while
    a relation is locked. Only statement_timeout bounds that."""
    log = []
    conn = _Conn(log)
    package = types.ModuleType("pg8000")
    package.dbapi = types.SimpleNamespace(connect=lambda **kw: log.append(kw) or conn)
    monkeypatch.setitem(sys.modules, "pg8000", package)

    returned = db_connect.connect_engine(
        db_type="postgresql", host="10.0.0.1", username="u", password="p",
        statement_timeout_seconds=9)

    assert returned is conn
    assert "SELECT set_config('statement_timeout', '9000', false)" in log
    # Inlined, not bound: pg8000's paramstyle is a module-level global the runtime store pins to
    # 'qmark', so a '%s' here fails with `syntax error at or near "%"` once both share a process.
    assert not any("%s" in str(entry) for entry in log if isinstance(entry, str))


def test_oracle_builds_a_dsn_from_the_service_and_sets_call_timeout(monkeypatch):
    captured = {}

    class OraConn:
        call_timeout = None

        def close(self):
            pass

    conn = OraConn()
    monkeypatch.setitem(sys.modules, "oracledb", types.SimpleNamespace(
        makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}",
        connect=lambda **kw: captured.update(kw) or conn))

    db_connect.connect_engine(db_type="oracle", host="10.0.0.2", username="u", password="p",
                              service_name="FREEPDB1", statement_timeout_seconds=4)

    assert captured["dsn"] == "10.0.0.2:1521/FREEPDB1"
    assert conn.call_timeout == 4000


def test_oracle_without_a_service_says_so(monkeypatch):
    """Oracle connects to a service, not a database name. Failing with a DSN error would send an
    operator looking at the network."""
    monkeypatch.setitem(sys.modules, "oracledb", types.SimpleNamespace(
        makedsn=lambda **_kw: "", connect=lambda **_kw: None))
    with pytest.raises(db_connect.DbConnectError, match="service_name"):
        db_connect.connect_engine(db_type="oracle", host="h", username="u", password="p")


def test_each_engine_supplies_its_own_default_port_and_database(monkeypatch):
    """A caller that names neither must still land somewhere sensible — and SQL Server's
    'master' is not a sensible default for PostgreSQL."""
    seen = {}
    package = types.ModuleType("pg8000")
    package.dbapi = types.SimpleNamespace(connect=lambda **kw: seen.update(kw) or _Conn([]))
    monkeypatch.setitem(sys.modules, "pg8000", package)

    db_connect.connect_engine(db_type="postgresql", host="h", username="u", password="p",
                              statement_timeout_seconds=None)

    assert seen["port"] == 5432
    assert seen["database"] == "postgres"


def test_a_missing_driver_is_an_operator_message_not_an_import_traceback(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymysql", None)
    monkeypatch.setitem(sys.modules, "mysql", None)
    monkeypatch.setitem(sys.modules, "mysql.connector", None)

    with pytest.raises(db_connect.DbConnectError, match="pymysql"):
        db_connect.connect_engine(db_type="mysql", host="h", username="u", password="p")


# --------------------------------------------------------------------------- #
# What sql_run does with it
# --------------------------------------------------------------------------- #
def test_sql_run_no_longer_refuses_a_non_sqlserver_target(monkeypatch, tmp_path):
    """The regression this whole change exists for: `/spbot_sql_to_xlsx` against a PostgreSQL
    target used to fail with 'only sqlserver is supported'."""
    conn = _Conn([])
    monkeypatch.setattr(sql_run, "resolve_sqlserver_target",
                        lambda spec, data_dir=None, database="", credential_name="", sql_access=None,
                        profile=None, driver="", oracle_client_mode="": {
                            "server_id": "PGLAB", "db_type": "postgresql",
                            "database_name": "postgres", "credential_name": "pg_cred",
                            "username": "postgres", "password": "x", "ip": "10.0.0.9",
                            "port": 5432, "service_name": "",
                            "sql_access": sql_access or {"method": "direct"},
                            "profile": TargetProfile(db_type="postgresql"),
                            "tool": {"tool": "postgresql", "chosen_by": "default", "reason": ""},
                        })
    monkeypatch.setattr(
        sql_run, "connect_target",
        lambda target, timeout_seconds, connect_timeout_seconds=0,  # noqa: ARG005
        autocommit=False: conn)
    monkeypatch.setattr(sql_run, "execute_capture", lambda cursor, sql, **_kwargs: (
        [{"columns": ["n"], "rows": [[1]], "row_count": 1, "truncated": False}], 0, False))

    result = sql_run.run_sql({"target": "PGLAB", "sql": "SELECT 1 AS n"})

    assert result["ok"] is True and result["rows"] == [[1]]
    assert conn.closed is True


def test_oracle_loses_the_trailing_semicolon_every_other_engine_tolerates():
    """`SELECT 1;` raises ORA-00911 on Oracle and is fine everywhere else. Stripping it for all
    engines would break a MySQL/PostgreSQL batch of several semicolon-separated statements."""
    assert sql_run.split_batches_for("SELECT 1 FROM dual;", "oracle") == ["SELECT 1 FROM dual"]
    assert sql_run.split_batches_for("SELECT 1;", "postgresql") == ["SELECT 1;"]
    assert sql_run.split_batches_for("SELECT 1;", "sqlserver") == ["SELECT 1;"]


def test_go_still_separates_batches_for_sql_server():
    batches = sql_run.split_batches_for("SELECT 1\nGO\nSELECT 2", "sqlserver")
    assert batches == ["SELECT 1", "SELECT 2"]


# --------------------------------------------------------------------------- #
# Which database metric collection connects to
# --------------------------------------------------------------------------- #
def test_sqlserver_metrics_connect_to_the_instance_not_the_service_label():
    """Regression, 2026-08-01: unifying the four per-engine execute functions replaced SQL
    Server's hard-coded `master` with the target's `db_name`. On SQL Server `db_name` is the
    *service* label from the inventory ("APPDB-DEV", "SALESDB-PROD"), not a database that exists, so
    every SQL Server target failed at once with `Cannot open database "APPDB-DEV" requested by
    the login`. Collection connects to the instance; metric SQL does its own USE."""
    from db_ops.metrics.executor import _metric_database
    from db_ops.metrics.models import MetricTarget

    target = MetricTarget(
        target_id="t", server_id="s", ip="10.0.0.1", db_type="sqlserver",
        db_name="APPDB-DEV", credential_name="c", port=1433,
        connection_info={}, credential={"username": "u"},
    )
    assert _metric_database(target) == "", "must not name a database; db_connect supplies master"


def test_the_other_engines_still_take_the_database_the_inventory_names():
    """They have no instance-level catalog to sit in, so the opposite rule applies."""
    from db_ops.metrics.executor import _metric_database
    from db_ops.metrics.models import MetricTarget

    def _t(db_type, db_name, info=None):
        return MetricTarget(target_id="t", server_id="s", ip="10.0.0.1", db_type=db_type,
                            db_name=db_name, credential_name="c", port=None,
                            connection_info=info or {}, credential={"username": "u"})

    assert _metric_database(_t("postgresql", "appdb")) == "appdb"
    assert _metric_database(_t("mysql", "", {"database": "shop"})) == "shop"
    # Nothing named: db_connect falls back to that engine's neutral default.
    assert _metric_database(_t("postgresql", "")) == ""


# --------------------------------------------------------------------------- #
# Which database run-sql / spbot_sql_to_xlsx connect to
# --------------------------------------------------------------------------- #
def _instances(monkeypatch, entry):
    """Point target resolution at one fabricated inventory entry."""
    from db_ops.common import data_sources
    from db_ops.common import data_sources as target_resolve
    monkeypatch.setattr(target_resolve, "resolve_target_instance",
                        lambda spec, data_dir=None: dict(entry))
    monkeypatch.setattr(data_sources, "find_database_credential",
                        lambda *a, **k: {"credential_name": "c", "username": "u",
                                         "password_ref": "R"})
    monkeypatch.setattr(data_sources, "load_credentials", lambda *a, **k: [])
    monkeypatch.setattr(data_sources, "load_secret_text", lambda *a, **k: {"R": "pw"})


def test_sqlserver_connects_to_master_even_if_the_inventory_names_something_else(monkeypatch):
    """The inventory's `database` is unreliable on SQL Server: mostly empty, sometimes a copy of
    'master', and nothing stops a service label (`APPDB-PROD`) being written there — which is not
    a database, so the login fails with 4060. Metric collection hit exactly that. master is
    always openable and a script that needs another database says USE."""
    _instances(monkeypatch, {"server_id": "ACME-x", "db_type": "sqlserver", "ip": "10.0.0.1",
                             "port": 1433, "database": "APPDB-PROD"})

    resolved = sql_run.resolve_target("ACME-x")

    assert resolved["database_name"] == "master"


def test_an_explicit_database_in_the_request_still_wins(monkeypatch):
    """Pinning the default must not remove the documented `"database": "SALESDB"` option — that is
    how a caller queries a user database without writing USE."""
    _instances(monkeypatch, {"server_id": "ACME-x", "db_type": "sqlserver", "ip": "10.0.0.1",
                             "port": 1433, "database": "APPDB-PROD"})

    resolved = sql_run.resolve_target("ACME-x", database="SALESDB")

    assert resolved["database_name"] == "SALESDB"


def test_the_other_engines_still_take_the_inventory_database(monkeypatch):
    """PostgreSQL/MySQL have no instance-level catalog to sit in, so the opposite rule holds."""
    _instances(monkeypatch, {"server_id": "PG", "db_type": "postgresql", "ip": "10.0.0.2",
                             "port": 5432, "database": "appdb"})

    assert sql_run.resolve_target("PG")["database_name"] == "appdb"
