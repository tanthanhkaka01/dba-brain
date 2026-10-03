"""What a SQL metric sends to ``common.cli metric-batch``, and what the answer means.

The connect, the driver, the per-database walk and the legacy-Oracle bridge run in ``common``
(:mod:`db_ops.common.metric_batch`, since 0.24.0 - they ran here, importing four ``common``
modules under an exemption). What stays is this app's: which database a metric connects to, the
credential it connects with, and how a failure is classed - never reached the target, or reached
it and the SQL failed - because the metric is graded differently for each.
"""

from __future__ import annotations

from db_ops.lib import errors
from typing import Any

from db_ops.lib import sql_access
from db_ops.lib.event_policy import PHASE_CONNECT, PHASE_EXECUTE
from db_ops.lib.sql_text import resolve_password
from db_ops.metrics import batch
from db_ops.metrics.models import MetricTarget


DEFAULT_SQL_TIMEOUT_SECONDS = 5

POSTGRES_DB_TYPES = frozenset({"postgresql", "postgres"})

#: How many databases one per-database metric will visit on a single target.
#:
#: Each one costs a connection, so an unbounded loop turns a cluster with 200 databases into 200
#: logins per metric per run. The cap is high enough for any cluster in this estate and low enough
#: that a surprise cannot flood a target; passing it is reported as a row rather than swallowed,
#: because a silently partial inventory is the failure this whole change exists to remove.
MAX_DATABASES_PER_METRIC = 50


class MetricConnectionError(errors.Unreachable):
    """The collector never reached the target: connect refused, auth rejected, no credential.

    Carried as a type rather than left to be guessed from the message, because the caller grades
    it against the metric's ``connection_error_severity``. Driver messages are not a reliable
    signal — pyodbc says "Login timeout expired" for an unreachable host and oracledb says
    "ORA-12541" — so the one place that knows the connect had not returned yet says so.
    """

    failure_phase = PHASE_CONNECT


class MetricExecutionError(errors.OperationFailed):
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
    """One SQL metric, run on its own - the rows, or the exception its failure is graded by."""
    prepared = prepare_sql(target=target, sql_text=sql_text, secrets=secrets,
                           sql_timeout_seconds=sql_timeout_seconds, max_rows=max_rows,
                           per_database=per_database)
    return batch.run_one(target, secrets, prepared)


def prepare_sql(
    *,
    target: MetricTarget,
    sql_text: str,
    secrets: dict[str, str],
    sql_timeout_seconds: int = DEFAULT_SQL_TIMEOUT_SECONDS,
    max_rows: int = 0,
    per_database: bool = False,
    item_id: str = "",
) -> batch.Prepared:
    """The item for one SQL metric. Raises before anything is sent when it cannot connect at all.

    Checked in the order the in-process path met them, so the same target fails with the same
    words: no credential, then a password that does not resolve, then a credential without a
    username - all connect-phase, because there is no way to open a session.
    """
    if not sql_access.is_legacy(target.sql_access):
        # The legacy bridge builds its own connect string from the credential; only a direct
        # connection needs one here.
        _direct_login(target, secrets)
    item = {
        "id": item_id, "kind": "sql", "sql": sql_text,
        "timeout_seconds": max(1, int(sql_timeout_seconds)),
        "max_rows": int(max_rows or 0),
        "per_database": bool(per_database)
        and sql_access.normalize_db_type(target.db_type) in POSTGRES_DB_TYPES,
        "max_databases": MAX_DATABASES_PER_METRIC,
    }
    return batch.Prepared(item=item, interpret=lambda answer: _rows(answer, target=target, max_rows=max_rows))


def _direct_login(target: MetricTarget, secrets: dict[str, str]) -> tuple[str, str]:
    if target.credential is None:
        # Connect-phase: there is no way to open a session at all, which is the same outcome for
        # this metric as a refused connect and must be graded the same way.
        raise MetricConnectionError(f"Credential not found for target {target.target_id}.")
    try:
        password = resolve_password(target.credential, secrets)
    except Exception as exc:  # noqa: BLE001 - an unresolvable password is a connect failure.
        raise MetricConnectionError(f"Credential could not be resolved: {exc}") from exc
    try:
        username = str(target.credential["username"])
    except Exception as exc:  # noqa: BLE001 - it failed inside the connect, and was graded so.
        raise MetricConnectionError(f"Connection failed: {exc}") from exc
    return username, password


def connection_block(target: MetricTarget | None, secrets: dict[str, str]) -> dict[str, Any]:
    """The batch's ``target``: everything a connection to this target needs, resolved.

    A target whose login cannot be resolved still gets a block - its SQL items already failed in
    :func:`prepare_sql` and were never sent, and its script items do not need one.
    """
    if target is None:
        return {"target_id": ""}
    if sql_access.is_legacy(target.sql_access):
        return {
            "target_id": target.target_id, "db_type": target.db_type,
            "sql_access": target.sql_access, "credential": target.credential,
            "host": target.ip or target.server_id, "port": target.port,
            "service_name": target.service_name or target.instance_name,
        }
    block: dict[str, Any] = {
        "target_id": target.target_id, "db_type": target.db_type, "host": str(target.ip),
        "port": target.port, "database": _metric_database(target),
        "service_name": str(target.connection_info.get("service_name") or target.db_name or ""),
        "sqlserver_driver": str(target.connection_info.get("sqlserver_driver", "") or "").strip(),
        "sqlserver_tls_verify": target.connection_info.get("sqlserver_tls_verify") is True,
        "sql_access": target.sql_access,
    }
    try:
        block["username"], block["password"] = _direct_login(target, secrets)
    except MetricConnectionError:
        pass
    return block


def _metric_database(target: MetricTarget) -> str:
    """Which database metric collection connects to — **never** the target's ``db_name``.

    On SQL Server ``db_name`` is the *service* label from the inventory (``APPDB-DEV``,
    ``SALESDB-PROD``), not a database that exists. Passing it produced
    ``Cannot open database "APPDB-DEV" requested by the login`` on every SQL Server target at
    once: collection connects to the **instance**, and metric SQL does its own ``USE``. Empty
    lets ``common``'s connect supply ``master``.

    The other engines have no instance-level catalog to sit in, so they do take an explicit
    database when the inventory names one — and fall back to that engine's neutral default
    (``postgres`` / ``information_schema``). Oracle ignores this entirely and connects by
    service.
    """
    if sql_access.normalize_db_type(target.db_type) == "sqlserver":
        return ""
    return str(target.connection_info.get("database") or target.db_name or "")


def _rows(answer: dict[str, Any], *, target: MetricTarget, max_rows: int) -> list[dict[str, Any]]:
    error = answer.get("error")
    if error:
        _raise(error)
    if "databases" in answer:
        return _per_database_rows(answer, target=target, max_rows=max_rows)
    if answer.get("truncated"):
        _warn_truncated(target, max_rows)
    return [dict(row) for row in answer.get("rows") or []]


def _raise(error: dict[str, Any]) -> None:
    message = str(error.get("message") or "")
    if error.get("kind") == "connect":
        raise MetricConnectionError(message)
    if error.get("kind") == "execute":
        raise MetricExecutionError(message)
    batch.raise_other(error)


def _per_database_rows(answer: dict[str, Any], *, target: MetricTarget, max_rows: int) -> list[dict[str, Any]]:
    """The rows of a per-database metric: each database's own, and a row for each that failed.

    **One database failing must not cost the others.** A database dropped mid-run, or one this
    login cannot enter, costs its own rows and nothing else - and the failure is a row, so it
    reaches the store: a per-database metric that quietly skips half a cluster is the failure
    this exists to fix, not an acceptable outcome.
    """
    rows: list[dict[str, Any]] = []
    for database in answer.get("databases") or []:
        name = str(database.get("name") or "")
        if database.get("error"):
            rows.append({
                "metric_item": f"{name} :: collection failed",
                "metric_value": "0",
                "metric_unit": "summary",
                "status": "WARNING",
                "message": f"db={name}, collection failed: {database['error'].get('message')}",
            })
            continue
        if database.get("truncated"):
            _warn_truncated(target, max_rows)
        rows.extend(dict(row) for row in database.get("rows") or [])
    skipped = [str(name) for name in answer.get("skipped") or []]
    if skipped:
        count = int(answer.get("database_count") or 0)
        rows.append({
            "metric_item": "databases :: truncated",
            "metric_value": str(count),
            "metric_unit": "summary",
            "status": "WARNING",
            "message": (f"target has {count} connectable databases and this metric "
                        f"visited {count - len(skipped)} (MAX_DATABASES_PER_METRIC); "
                        f"skipped={', '.join(skipped)}"),
        })
    return rows


def _warn_truncated(target: MetricTarget, max_rows: int) -> None:
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
