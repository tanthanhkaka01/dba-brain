"""Running a PostgreSQL metric against every database, not just the one it happened to connect to.

PostgreSQL cannot read another database's catalog from one connection. `_metric_database` sends a
PostgreSQL metric to `connection_info.database`, falling back to `postgres` — and no target in
this estate sets it. So every per-database PostgreSQL metric was describing `postgres`, which on
the PGLAB cluster holds no user tables at all, while the real data sat in `db_ops` where nothing
looked. Four metrics reported OK from an empty database
- four metrics reporting OK from a database with nothing in it.

SQL Server never had this problem: it iterates databases inside the SQL, with a cursor and USE.
PostgreSQL needs a connection each, so the loop moved into the collector and is opted into per
variant — because iterating a *cluster-wide* metric would store the same rows once per database.

What these tests pin is mostly the failure behaviour. A loop that gives up on the first bad
database, or that silently visits a subset, would reintroduce the exact fault being fixed: a
partial answer that reads like a complete one.
"""

import dataclasses

import pytest

from db_ops.metrics import executor
from db_ops.metrics.executor import MetricConnectionError, MetricExecutionError
from db_ops.metrics.models import MetricTarget


def _target(db_type="postgresql", database=None):
    info = {"database": database} if database else {}
    return MetricTarget(
        target_id="t/postgresql/db", server_id="PGLAB", ip="10.0.0.1", db_type=db_type,
        db_name="", credential_name="cred", port=5432, connection_info=info,
        credential={"username": "u", "password_ref": "R"},
    )


@pytest.fixture
def visited(monkeypatch):
    """Record which database each _execute call was pointed at."""
    seen: list[str] = []

    def fake_execute(*, target, sql_text, password, sql_timeout_seconds, max_rows=0):
        name = target.connection_info.get("database", "")
        seen.append(name)
        if name == "broken":
            raise MetricExecutionError("relation does not exist")
        if name == "locked":
            raise MetricConnectionError("permission denied for database")
        return [{"metric_item": f"{name}.idx", "metric_value": "1", "metric_unit": "idx_scan",
                 "status": "OK", "message": f"db={name}"}]

    monkeypatch.setattr(executor, "_execute", fake_execute)
    monkeypatch.setattr(executor, "resolve_password", lambda *_a, **_k: "pw")
    return seen


def _databases(monkeypatch, names):
    monkeypatch.setattr(executor, "_list_databases", lambda **_: list(names))


def test_the_sql_runs_once_per_database(monkeypatch, visited):
    _databases(monkeypatch, ["postgres", "db_ops", "test01"])

    rows = executor.execute_metric_sql(
        target=_target(), sql_text="SELECT 1", secrets={}, per_database=True)

    assert visited == ["postgres", "db_ops", "test01"]
    assert [row["metric_item"] for row in rows] == ["postgres.idx", "db_ops.idx", "test01.idx"]


def test_without_the_flag_nothing_changes(monkeypatch, visited):
    """The default path must stay exactly as it was — one connection, the resolved database."""
    _databases(monkeypatch, ["postgres", "db_ops"])

    rows = executor.execute_metric_sql(
        target=_target(database="db_ops"), sql_text="SELECT 1", secrets={})

    assert visited == ["db_ops"]
    assert len(rows) == 1


def test_a_non_postgres_target_never_iterates(monkeypatch, visited):
    """SQL Server reaches every database from one connection; looping would store each row twice."""
    _databases(monkeypatch, ["a", "b", "c"])

    executor.execute_metric_sql(
        target=_target(db_type="sqlserver"), sql_text="SELECT 1", secrets={}, per_database=True)

    assert len(visited) == 1


def test_one_unreadable_database_costs_only_its_own_rows(monkeypatch, visited):
    """The same rule `load_metric_targets` applies to a broken cmd_access. A database dropped
    mid-run, or one this login cannot enter, must not take the rest of the cluster with it."""
    _databases(monkeypatch, ["db_ops", "broken", "test01"])

    rows = executor.execute_metric_sql(
        target=_target(), sql_text="SELECT 1", secrets={}, per_database=True)

    assert visited == ["db_ops", "broken", "test01"]
    assert [row["metric_item"] for row in rows] == [
        "db_ops.idx", "broken :: collection failed", "test01.idx"]


def test_a_failed_database_is_reported_not_skipped(monkeypatch, visited):
    """Silently dropping it is the failure this whole change exists to remove: a partial
    inventory that reads exactly like a complete one."""
    _databases(monkeypatch, ["locked"])

    rows = executor.execute_metric_sql(
        target=_target(), sql_text="SELECT 1", secrets={}, per_database=True)

    assert rows[0]["status"] == "WARNING"
    assert "permission denied" in rows[0]["message"]


def test_passing_the_database_cap_is_stated_rather_than_swallowed(monkeypatch, visited):
    _databases(monkeypatch, [f"db{n}" for n in range(executor.MAX_DATABASES_PER_METRIC + 3)])

    rows = executor.execute_metric_sql(
        target=_target(), sql_text="SELECT 1", secrets={}, per_database=True)

    assert len(visited) == executor.MAX_DATABASES_PER_METRIC
    truncated = [row for row in rows if row["metric_item"] == "databases :: truncated"]
    assert len(truncated) == 1
    assert truncated[0]["status"] == "WARNING"
    assert "db52" in truncated[0]["message"]


def test_the_caller_s_target_is_not_left_pointing_at_the_last_database(monkeypatch, visited):
    """A mutation here would silently redirect every metric collected after this one."""
    _databases(monkeypatch, ["db_ops", "test01"])
    target = _target(database="postgres")

    executor.execute_metric_sql(
        target=target, sql_text="SELECT 1", secrets={}, per_database=True)

    assert target.connection_info["database"] == "postgres"


def test_with_database_returns_a_copy():
    target = _target(database="postgres")
    scoped = executor._with_database(target, "db_ops")

    assert scoped.connection_info["database"] == "db_ops"
    assert target.connection_info["database"] == "postgres"
    assert dataclasses.replace(scoped, connection_info={}) != target


def test_per_database_is_refused_on_any_engine_but_postgresql(tmp_path):
    """Every other engine reaches its databases from one connection, so iterating would collect
    the same rows N times and store them as N databases' worth of findings. The catalog refuses
    the flag rather than the executor quietly ignoring it, so the mistake is caught at load."""
    import json

    from db_ops.metrics.definitions import load_metric_definitions

    sql_dir = tmp_path / "sql" / "sqlserver"
    sql_dir.mkdir(parents=True)
    (sql_dir / "x.sql").write_text("SELECT 1", encoding="utf-8")
    path = tmp_path / "metric_definitions.json"
    path.write_text(json.dumps({"metrics": [{
        "metric_code": "X", "db_type": "sqlserver", "category": "maintenance",
        "default_importance": 1, "active": True, "collector_type": "sql",
        "variants": [{"name": "mssql", "db_type": "sqlserver", "supported": True,
                      "file": "sqlserver/x.sql", "per_database": True}],
    }]}), encoding="utf-8")

    with pytest.raises(Exception) as err:
        load_metric_definitions(path, sql_dir=tmp_path / "sql")

    assert "per_database is PostgreSQL-only" in str(err.value)
