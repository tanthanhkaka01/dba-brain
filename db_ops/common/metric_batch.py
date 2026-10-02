"""``metric-batch`` - run one target's metric items and say what each one returned.

``metrics`` decides *what* to run - which metrics are due, which file fits this target, the
environment a script gets, the password - and what the answer *means*: the JSON-rows contract, the
severity, the overrides, the stored row. Running it is an operation, and operations are
``common``'s (rules R03, R10): the driver connect, the per-database walk a PostgreSQL metric needs,
the legacy-Oracle bridge, a script on this machine or shipped over SSH/WinRM. That half was
``metrics/executor.py`` and the transport functions of ``metrics/collector.py`` until 0.24.0.

**One process per target, not per metric.** A pass runs ~390 executions (peak ~1,370); a process
each would spend 43-138 s starting interpreters, longer than the interval between passes. The
batch keeps a target's metrics strictly one after another, as the in-process loop did.

**A failure is data, never a stop.** Each item answers on its own - rows, or an ``error`` that
carries the message, the phase the raiser declared (``connect``/``execute``, empty when it could
not tell) and whatever output there was - because one broken metric must not cost the rest of the
target, and the caller grades connect and execute failures differently.

Reads no configuration (R09): the target, the resolved password and the secret refs a target names
arrive in the request, on stdin.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from db_ops.common import db_catalog, db_connect, oracle_bridge, remote_exec, sql_run
from db_ops.common.sql_execution import make_json_safe
from db_ops.lib import sql_access as sql_access_rules
from db_ops.lib.coerce import as_text
from db_ops.lib.event_policy import PHASE_CONNECT, PHASE_EXECUTE
from db_ops.lib.sql_text import MAX_RESULT_ROWS

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5

#: Databases every PostgreSQL cluster has and that hold nothing worth collecting. `template0` in
#: particular refuses connections outright (`datallowconn = false`), so it is excluded by the
#: query as well; naming them here keeps the intent readable next to it.
SKIP_DATABASES = frozenset({"template0", "template1"})

KINDS = ("sql", "script", "local")


class ItemFailure(RuntimeError):
    """One item failed in a way its caller must be able to tell apart - carried as data."""

    def __init__(self, message: str, *, phase: str = "", kind: str = "other", stdout: Any = "",
                 stderr: Any = "", exit_code: int | None = None,
                 duration_seconds: float | None = None) -> None:
        super().__init__(message)
        self.phase, self.kind = phase, kind
        self.stdout, self.stderr = as_text(stdout), as_text(stderr)
        self.exit_code, self.duration_seconds = exit_code, duration_seconds

    def to_dict(self) -> dict[str, Any]:
        return {"message": str(self), "failure_phase": self.phase, "kind": self.kind,
                "stdout": self.stdout, "stderr": self.stderr, "exit_code": self.exit_code,
                "duration_seconds": self.duration_seconds}


def run(request: dict[str, Any]) -> dict[str, Any]:
    target = request.get("target")
    items = request.get("items")
    if not isinstance(target, dict):
        raise ValueError('metric-batch needs "target": the connection facts of one target (an object).')
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError('metric-batch needs "items": a list of objects, one per metric to run.')
    secrets = {str(key): str(value) for key, value in (request.get("secrets") or {}).items()}
    return {"target_id": str(target.get("target_id") or ""),
            "items": [_run_item(item, target, secrets) for item in items]}


def _run_item(item: dict[str, Any], target: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    kind = str(item.get("kind") or "")
    # Wall-clock, so the caller stamps the row with when THIS metric ran - not when the batch
    # started or ended, which is what the in-process loop recorded and a report compares by.
    answer: dict[str, Any] = {"id": item.get("id"), "kind": kind, "started_at": time.time()}
    try:
        if kind == "sql":
            answer.update(_sql(item, target, secrets))
        elif kind == "script":
            answer.update(_script(item, secrets))
        elif kind == "local":
            answer.update(_local(item))
        else:
            raise ValueError(f"unknown item kind {kind!r}; expected one of {', '.join(KINDS)}.")
    except ItemFailure as failure:
        answer["error"] = failure.to_dict()
    except Exception as exc:  # noqa: BLE001 - one item's failure is its answer, not the batch's.
        answer["error"] = {"message": str(exc), "kind": "other",
                           "failure_phase": str(getattr(exc, "failure_phase", "") or "")}
    return answer


# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #
def _sql(item: dict[str, Any], target: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    sql_text = str(item.get("sql") or "")
    timeout = max(1, int(item.get("timeout_seconds") or 5))
    max_rows = int(item.get("max_rows") or 0)
    legacy = target.get("sql_access") or {}
    if sql_access_rules.is_legacy(legacy):
        # No connect/execute split to carry here: the bridge connects and runs in one call and
        # reports a single error, classified by the caller from its message.
        return {"rows": oracle_bridge.run_bridge_query(
            sql=sql_text, sql_access=legacy, secrets=secrets,
            # The connect string is built from this target's own credential.
            credential=target.get("credential"),
            host=str(target.get("host") or ""), port=target.get("port"),
            service_name=str(target.get("service_name") or ""), now=time.time(),
            # A metric's declared cap has to hold on every transport; 0 leaves the tool's own.
            limit=max_rows,
            timeout_seconds=int(legacy.get("timeout_seconds") or oracle_bridge.DEFAULT_BRIDGE_TIMEOUT_SECONDS),
        )}
    if item.get("per_database"):
        return _per_database(item, target, sql_text=sql_text, timeout=timeout, max_rows=max_rows)
    rows, truncated = _execute(target, str(target.get("database") or ""), sql_text,
                               timeout=timeout, max_rows=max_rows)
    return {"rows": rows, "truncated": truncated}


def _connect(target: dict[str, Any], database: str, *, timeout: int) -> Any:
    """Open the metric connection; any failure here means the target never answered."""
    try:
        return db_connect.connect_engine(
            db_type=str(target.get("db_type") or ""),
            host=str(target.get("host") or ""),
            port=target.get("port"),
            database=database,
            service_name=str(target.get("service_name") or ""),
            username=str(target.get("username") or ""),
            password=str(target.get("password") or ""),
            sqlserver_driver=str(target.get("sqlserver_driver") or "").strip(),
            sqlserver_tls_verify=target.get("sqlserver_tls_verify") is True,
            connect_timeout_seconds=min(DEFAULT_CONNECT_TIMEOUT_SECONDS, timeout),
            statement_timeout_seconds=timeout,
            # Metric SQL is read-only, and it catches per-database errors inside a cursor. Inside a
            # transaction one such error dooms the whole transaction (error 3930) and every later
            # statement fails; autocommit keeps each statement independent.
            autocommit=True,
        )
    except Exception as exc:  # noqa: BLE001 - every connect failure is graded as one.
        raise ItemFailure(f"Connection failed: {exc}", phase=PHASE_CONNECT, kind="connect") from exc


def _execute(target: dict[str, Any], database: str, sql_text: str, *, timeout: int,
             max_rows: int) -> tuple[list[dict[str, Any]], bool]:
    """Connect, run the SQL, return the first result set as dict rows and whether it was cut.

    Read by ``sql_run.execute_capture``, the reader ``run-sql`` runs on (rules R11) - the batches
    split and an Oracle statement's ``;`` dropped as ``run-sql`` does it. Until 0.25.0 every metric
    went through a second one, ``sql_execution.execute_cursor_batches``.
    """
    connection = _connect(target, database, timeout=timeout)
    try:
        result_sets, _affected, _ = sql_run.execute_capture(
            connection.cursor(), sql_text,
            # 0 = "not set by this metric": the preview cap a metric reads at, 100 rows.
            max_rows=int(max_rows) or MAX_RESULT_ROWS,
            db_type=str(target.get("db_type") or ""),
        )
    except Exception as exc:  # noqa: BLE001 - report post-connect SQL failures accurately.
        raise ItemFailure(f"SQL execution failed: {exc}", phase=PHASE_EXECUTE, kind="execute") from exc
    finally:
        connection.close()
    if not result_sets:
        return [], False
    first = result_sets[0]
    columns = [str(column).lower() for column in first["columns"]]
    # JSON-safe here, as it always was: the answer goes back to the metrics app on stdout.
    return ([dict(zip(columns, make_json_safe(row))) for row in first["rows"]],
            bool(first["truncated"]))


def _list_databases(target: dict[str, Any], *, timeout: int) -> list[str]:
    """Every database on this PostgreSQL target that can be connected to and is worth collecting,
    the one the target is pointed at first - so a cut list keeps what the old behaviour read."""
    current = str(target.get("database") or "")
    connection = _connect(target, current, timeout=timeout)
    try:
        names = [str(row["name"]) for row in db_catalog.databases(connection.cursor(), "postgresql")
                 if row.get("allow_connections") and not row.get("is_template")]
    except Exception as exc:  # noqa: BLE001 - listing failed: nothing can be collected per database.
        raise ItemFailure(f"Could not list databases: {exc}", phase=PHASE_EXECUTE, kind="execute") from exc
    finally:
        connection.close()
    names = [name for name in names if name not in SKIP_DATABASES]
    names.sort(key=lambda name: (name != current, name.lower()))
    return names


def _per_database(item: dict[str, Any], target: dict[str, Any], *, sql_text: str, timeout: int,
                  max_rows: int) -> dict[str, Any]:
    """Run the SQL against every database on a PostgreSQL target, each answering on its own.

    PostgreSQL cannot read another database's catalog from one connection. **One database failing
    must not cost the others**: a dropped database or one this login cannot enter answers with its
    error, and the caller turns that into a row of its own.
    """
    databases = _list_databases(target, timeout=timeout)
    cap = int(item.get("max_databases") or 0) or len(databases)
    visited = []
    for name in databases[:cap]:
        try:
            rows, truncated = _execute(target, name, sql_text, timeout=timeout, max_rows=max_rows)
            visited.append({"name": name, "rows": rows, "truncated": truncated})
        except ItemFailure as failure:
            visited.append({"name": name, "error": failure.to_dict()})
    return {"databases": visited, "database_count": len(databases), "skipped": databases[cap:]}


# --------------------------------------------------------------------------- #
# Scripts
# --------------------------------------------------------------------------- #
def _script(item: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    """A metric script shipped to the target over SSH or WinRM, with its environment prepended."""
    timeout = int(item.get("timeout_seconds") or 5)
    started = time.monotonic()
    try:
        result = remote_exec.run_script(
            dict(item.get("access") or {}), str(item.get("script") or ""),
            credential=item.get("credential") or {}, secrets=secrets,
            shell=str(item.get("shell") or "") or None, timeout_seconds=timeout,
            env=item.get("env") or {}, default_timeout_seconds=timeout,
        )
    except remote_exec.RemoteExecError as exc:
        raise ItemFailure(
            str(exc), phase=_remote_failure_phase(exc), kind="remote", stdout=exc.stdout,
            stderr=exc.stderr,
            duration_seconds=exc.duration_seconds or round(time.monotonic() - started, 3),
        ) from exc
    return {"exit_code": result.exit_code, "stdout": as_text(result.stdout),
            "stderr": as_text(result.stderr), "duration_seconds": result.duration_seconds}


def _remote_failure_phase(exc: remote_exec.RemoteExecError) -> str:
    """Whether a remote_exec failure happened opening the session or running the script.

    Auth rejected and host unreachable can only be the session. A timeout is the one ambiguous
    case - the same class covers "the connect never completed" and "the script ran too long" - and
    ``command`` separates them: it is set only once there is a command to run.
    """
    if isinstance(exc, (remote_exec.RemoteAuthError, remote_exec.RemoteConnectError)):
        return PHASE_CONNECT
    if isinstance(exc, remote_exec.RemoteTimeoutError) and not str(getattr(exc, "command", "") or ""):
        return PHASE_CONNECT
    return PHASE_EXECUTE


def _local(item: dict[str, Any]) -> dict[str, Any]:
    """A metric script run on this machine - inside the worker container, on the worker.

    ``argv`` is the caller's: which interpreter runs a ``.py``, ``.ps1`` or ``.sh`` is the metrics
    app's decision, stated in the request like every other fact (R09) - and ``common`` itself
    chooses to launch no Python (R04, ``test_import_boundaries``).
    """
    if item.get("require_local_host"):
        # `local` pointed at another machine files THIS host's numbers under that host's name.
        remote_exec.assert_local_host(str(item.get("host") or ""), method="local")
    path = Path(str(item.get("path") or ""))
    argv = item.get("argv")
    if not isinstance(argv, list) or not argv:
        raise ValueError("a local item needs \"argv\": the command line that runs its script.")
    timeout = int(item.get("timeout_seconds") or 5)
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in (item.get("env") or {}).items()})
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [str(part) for part in argv], cwd=str(path.parent), capture_output=True, text=True,
            timeout=timeout, check=False, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ItemFailure(
            f"Command timed out after {timeout} seconds.", phase=PHASE_EXECUTE, kind="timeout",
            stdout=exc.stdout or "", stderr=exc.stderr or "",
            duration_seconds=round(time.monotonic() - started, 3),
        ) from exc
    return {"exit_code": completed.returncode, "stdout": completed.stdout or "",
            "stderr": completed.stderr or "", "duration_seconds": round(time.monotonic() - started, 3)}
