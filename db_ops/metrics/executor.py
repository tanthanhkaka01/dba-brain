from __future__ import annotations

from typing import Any

import dataclasses

import time

from db_ops.lib import sql_access
from db_ops.common import db_connect
from db_ops.common import oracle_bridge
from db_ops.lib.event_policy import PHASE_CONNECT, PHASE_EXECUTE
from db_ops.common.sql_execution import (
    execute_cursor_batches,
    resolve_password,
    split_sql_batches,
)
from db_ops.metrics.models import MetricTarget


DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_SQL_TIMEOUT_SECONDS = 5

POSTGRES_DB_TYPES = frozenset({"postgresql", "postgres"})

#: How many databases one per-database metric will visit on a single target.
#:
#: Each one costs a connection, so an unbounded loop turns a cluster with 200 databases into 200
#: logins per metric per run. The cap is high enough for any cluster in this estate and low enough
#: that a surprise cannot flood a target; passing it is reported as a row rather than swallowed,
#: because a silently partial inventory is the failure this whole change exists to remove.
MAX_DATABASES_PER_METRIC = 50

#: Databases every PostgreSQL cluster has and that hold nothing worth collecting. `template0` in
#: particular refuses connections outright (`datallowconn = false`), so it is excluded by the
#: query as well; naming them here keeps the intent readable next to it.
SKIP_DATABASES = frozenset({"template0", "template1"})


class MetricConnectionError(RuntimeError):
    """The collector never reached the target: connect refused, auth rejected, no credential.

    Carried as a type rather than left to be guessed from the message, because the caller grades
    it against the metric's ``connection_error_severity``. Driver messages are not a reliable
    signal — pyodbc says "Login timeout expired" for an unreachable host and oracledb says
    "ORA-12541" — so the one place that knows the connect had not returned yet says so.
    """

    failure_phase = PHASE_CONNECT


class MetricExecutionError(RuntimeError):
    """The connection was open and the metric's own SQL failed — a finding about the check."""

    failure_phase = PHASE_EXECUTE


def execute_metric_sql(
    *,
    target: MetricTarget,
    sql_text: str,
    secrets: dict[str, str],
    sql_timeout_seconds: int = DEFAULT_SQL_TIMEOUT_SECONDS,
    max_rows: int = 0,
    per_database: bool = False,
) -> list[dict[str, Any]]:
    sql_timeout_seconds = max(1, int(sql_timeout_seconds))
    # Per-target transport override: "api"/"subprocess" run the SQL through the legacy Oracle
    # tool instead of a database connection, which is the only way to reach an 8i host.
    if sql_access.is_legacy(target.sql_access):
        # No connect/execute split to carry here: the bridge connects and runs in one call and
        # reports a single error. Its failures are classified from the message
        # (event_policy.resolve_failure_phase), which is all the transport can tell us.
        return oracle_bridge.run_bridge_query(
            sql=sql_text,
            sql_access=target.sql_access,
            secrets=secrets,
            # The connect string is built from this target's own credential, exactly as the
            # direct path below resolves it — the bridge's `connect_ref` secret was a duplicate
            # of a password already in the store and was deleted (audits/20260801).
            credential=target.credential,
            host=target.ip or target.server_id,
            port=target.port,
            service_name=target.service_name or target.instance_name,
            now=time.time(),
            # A metric's declared cap has to hold on every transport. The direct path enforces
            # `max_rows` inside execute_cursor_batches, but the bridge branch used to send no
            # limit at all, which the legacy tool reads as "no cap" — so the one kind of target
            # that cannot be connected to directly was also the one that would fetch an entire
            # result set into memory and ship it over HTTP. 0 stays 0: the tool's own default
            # applies when the metric names no cap.
            limit=int(max_rows or 0),
            timeout_seconds=int(target.sql_access.get("timeout_seconds") or 30),
        )
    if target.credential is None:
        # Connect-phase: there is no way to open a session at all, which is the same outcome for
        # this metric as a refused connect and must be graded the same way.
        raise MetricConnectionError(f"Credential not found for target {target.target_id}.")
    try:
        password = resolve_password(target.credential, secrets)
    except Exception as exc:  # noqa: BLE001 - an unresolvable password is a connect failure.
        raise MetricConnectionError(f"Credential could not be resolved: {exc}") from exc
    if per_database and sql_access.normalize_db_type(target.db_type) in POSTGRES_DB_TYPES:
        return _execute_per_database(
            target=target,
            sql_text=sql_text,
            password=password,
            sql_timeout_seconds=sql_timeout_seconds,
            max_rows=max_rows,
        )
    return _execute(
        target=target,
        sql_text=sql_text,
        password=password,
        sql_timeout_seconds=sql_timeout_seconds,
        max_rows=max_rows,
    )


def _metric_database(target: MetricTarget) -> str:
    """Which database metric collection connects to — **never** the target's ``db_name``.

    On SQL Server ``db_name`` is the *service* label from the inventory (``APPDB-DEV``,
    ``SALESDB-PROD``), not a database that exists. Passing it produced
    ``Cannot open database "APPDB-DEV" requested by the login`` on every SQL Server target at
    once: collection connects to the **instance**, and metric SQL does its own ``USE``. Empty
    lets :mod:`db_ops.common.db_connect` supply ``master``.

    The other engines have no instance-level catalog to sit in, so they do take an explicit
    database when the inventory names one — and fall back to that engine's neutral default
    (``postgres`` / ``information_schema``). Oracle ignores this entirely and connects by
    service.
    """
    if sql_access.normalize_db_type(target.db_type) == "sqlserver":
        return ""
    return str(target.connection_info.get("database") or target.db_name or "")


def _execute(
    *,
    target: MetricTarget,
    sql_text: str,
    password: str,
    sql_timeout_seconds: int,
    max_rows: int = 0,
) -> list[dict[str, Any]]:
    """Connect, run the metric SQL, return the first result set as dict rows.

    One path for every engine. The connection — driver choice, default port/database, and the
    in-server statement timeout each engine spells differently — belongs to
    :mod:`db_ops.common.db_connect`, so the metrics app no longer carries its own copy of it.
    Before that split this file had four near-identical connect functions and
    ``common.sql_run`` had a fifth that only knew SQL Server, which is why /spbot_sql_to_xlsx
    could not query a PostgreSQL target.
    """
    connection = _connect(target=target, password=password, sql_timeout_seconds=sql_timeout_seconds)
    try:
        result = execute_cursor_batches(
            connection, connection.cursor(), split_sql_batches(sql_text), commit=False,
            # 0 = "not set by this metric", so the shared default still applies.
            **({"max_rows": int(max_rows)} if max_rows else {}),
        )
    except Exception as exc:  # noqa: BLE001 - report post-connect SQL failures accurately.
        raise MetricExecutionError(f"SQL execution failed: {exc}") from exc
    finally:
        connection.close()
    if result.get("truncated"):
        # Not raised: a truncated metric still carries real findings and dropping them would be
        # worse. But it must not be silent — "100 rows" and "the first 100 of an unknown number"
        # are different answers, and the metric needs a `max_rows` big enough for its own output.
        # stdout is captured into the metrics runtime log by patch_stdout.
        print(
            f"WARNING metric result truncated target={target.target_id} "
            f"cap={int(max_rows) or 'default(100)'} — raise max_rows for this metric in "
            f"data/metric_definitions.json; the stored rows are the first page only.",
            flush=True,
        )
    return _first_result_set_rows(result)


def _connect(*, target: MetricTarget, password: str, sql_timeout_seconds: int) -> Any:
    """Open the metric connection, reporting any failure as a connect-phase failure.

    The split matters to the caller and to nobody else here: everything raised from this function
    means the target never answered, so the metric is graded by its ``connection_error_severity``
    while anything raised afterwards is graded by ``execution_error_severity``.
    """
    try:
        return db_connect.connect_engine(
            db_type=target.db_type,
            host=str(target.ip),
            port=target.port,
            database=_metric_database(target),
            service_name=str(target.connection_info.get("service_name") or target.db_name or ""),
            username=str(target.credential["username"]),
            password=password,
            sqlserver_driver=str(target.connection_info.get("sqlserver_driver", "") or "").strip(),
            connect_timeout_seconds=min(DEFAULT_CONNECT_TIMEOUT_SECONDS, sql_timeout_seconds),
            statement_timeout_seconds=sql_timeout_seconds,
            # Metric SQL is read-only, and it catches per-database errors inside a cursor. Inside a
            # transaction one such error dooms the whole transaction (error 3930) and every later
            # statement fails; autocommit keeps each statement independent. sql_tasks keeps its
            # transactional connect - only metrics flips this.
            autocommit=True,
        )
    except Exception as exc:  # noqa: BLE001 - every connect failure is graded as one.
        raise MetricConnectionError(f"Connection failed: {exc}") from exc


def _list_databases(*, target: MetricTarget, password: str, sql_timeout_seconds: int) -> list[str]:
    """Every database on this PostgreSQL target that can be connected to and is worth collecting.

    Ordered so the connected/default database comes first: if the cap below truncates the list,
    what survives is the database the target was already pointed at, which keeps the truncated
    run a superset of the old behaviour rather than a different arbitrary slice.
    """
    connection = _connect(target=target, password=password, sql_timeout_seconds=sql_timeout_seconds)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT datname FROM pg_database "
            "WHERE datallowconn AND NOT datistemplate ORDER BY datname")
        names = [str(row[0]) for row in cursor.fetchall()]
        cursor.close()
    except Exception as exc:  # noqa: BLE001 - listing failed: nothing can be collected per database.
        raise MetricExecutionError(f"Could not list databases: {exc}") from exc
    finally:
        connection.close()
    current = _metric_database(target)
    names = [name for name in names if name not in SKIP_DATABASES]
    names.sort(key=lambda name: (name != current, name.lower()))
    return names


def _with_database(target: MetricTarget, database: str) -> MetricTarget:
    """The same target, pointed at one named database.

    A copy rather than a mutation: the caller's target is shared with the rest of the run, and a
    metric that left it pointing at the last database it visited would silently redirect every
    metric collected after it.
    """
    return dataclasses.replace(
        target, connection_info={**target.connection_info, "database": database})


def _execute_per_database(
    *,
    target: MetricTarget,
    sql_text: str,
    password: str,
    sql_timeout_seconds: int,
    max_rows: int = 0,
) -> list[dict[str, Any]]:
    """Run one metric's SQL against every database on a PostgreSQL target and concatenate the rows.

    PostgreSQL cannot read another database's catalog from one connection, so a per-database
    metric run once per *target* describes exactly one database — whichever `_metric_database`
    resolved, which is `postgres` for every target in this estate and holds no user tables at all.
    Four metrics reported OK from there while the real data sat in a database nothing looked at.

    **One database failing must not cost the others.** Same rule as `load_metric_targets` applies
    to a broken `cmd_access`: a database that has been dropped mid-run, or that this login cannot
    enter, costs its own rows and nothing else. The failure is returned as a row so it reaches the
    store, because a per-database metric that quietly skips half a cluster is the failure being
    fixed here, not an acceptable outcome.
    """
    databases = _list_databases(
        target=target, password=password, sql_timeout_seconds=sql_timeout_seconds)
    rows: list[dict[str, Any]] = []
    visited = databases[:MAX_DATABASES_PER_METRIC]
    for name in visited:
        scoped = _with_database(target, name)
        try:
            rows.extend(_execute(
                target=scoped,
                sql_text=sql_text,
                password=password,
                sql_timeout_seconds=sql_timeout_seconds,
                max_rows=max_rows,
            ))
        except (MetricConnectionError, MetricExecutionError) as exc:
            rows.append({
                "metric_item": f"{name} :: collection failed",
                "metric_value": "0",
                "metric_unit": "summary",
                "status": "WARNING",
                "message": f"db={name}, collection failed: {exc}",
            })
    if len(databases) > len(visited):
        rows.append({
            "metric_item": "databases :: truncated",
            "metric_value": str(len(databases)),
            "metric_unit": "summary",
            "status": "WARNING",
            "message": (f"target has {len(databases)} connectable databases and this metric "
                        f"visited {len(visited)} (MAX_DATABASES_PER_METRIC); "
                        f"skipped={', '.join(databases[MAX_DATABASES_PER_METRIC:])}"),
        })
    return rows

def _first_result_set_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    sets = result.get("result_sets") or []
    if not sets:
        return []
    first = sets[0]
    columns = [str(col).lower() for col in first.get("columns", [])]
    return [dict(zip(columns, row)) for row in first.get("rows", [])]
