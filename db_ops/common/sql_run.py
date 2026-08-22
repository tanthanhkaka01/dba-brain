"""Run SQL against **one** database target, from a JSON request object.

Every app that needs "connect to this one database, run this SQL, give me the rows back"
goes through here. Like :mod:`db_ops.common.remote_exec` (which answers the same question
for a VM), **the input is a JSON object** — the shape below travels from a config file, a
Telegram command, or the CLI into the API untranslated, and the result is JSON-shaped too::

    {
      "target": "ACME-192-0-2-115",   // server_id, or "<db_type> <ip> [port]"
      "sql": "SELECT TOP 10 * FROM sys.objects",   // or "sql_file": "path/to/query.sql"
      "database": "SALESDB",               // optional. SQL Server: default is always `master`
                                        // (say USE, or name it here). Other engines: default is
                                        // the instance's database.
      "credential_name": "...",         // optional; default = the instance's
                                        // default_credential_name (alias: "user_ref")
      "max_rows": 50000,
      "timeout_seconds": 30,             // the STATEMENT budget, and the connect unless the
                                        // next field overrides it
      "connect_timeout_seconds": 30,    // optional; opening the connection only (see below)
      "commit": false,                  // default false: the batch is ALWAYS rolled back
      "autocommit": false,              // true = no transaction at all (see below)
      "params": [505, "SALESDB"],          // values BOUND to the placeholders in the SQL
      "prelude": "DECLARE @spid int = ?;",   // SQL prepended to every batch (see below)
      "capture": "first",               // first (default) | all — see below
      "max_result_sets": 20,            // capture: all only; 0 = no cap
      "define": {"JOB_NO": "AA2503/00818"},  // optional SQL*Plus &substitutions (see below)
      "sql_access": {...},              // optional transport override; see below
      "data_dir": null                  // optional data/ folder override (tests)
    }

The connection runs with autocommit off and, unless ``commit`` is set, is **always rolled
back** — so temp-table report shapes (``SELECT ... INTO #tmp`` then a final ``SELECT``) work
while a write to a real table is reverted (SQL Server rolls back DDL too). **This is not a
security sandbox.** Rollback does not undo non-transactional side effects — ``xp_cmdshell``,
``sp_configure`` + ``RECONFIGURE``, ``BACKUP``/``RESTORE``, linked-server remote writes,
``sp_send_dbmail``, ``KILL`` — which execute and persist regardless. The real controls are the
caller's permission tier and the least-privilege login it connects with; keep that login
SELECT-only where possible. ``affected_rows`` is reported for transparency, not as a rejection.

``autocommit`` runs the batch with **no transaction at all**, which is the only way to reproduce
a *metric* here: metric SQL catches per-database errors inside a cursor, and inside a transaction
one caught error dooms the whole thing (error 3930, "the current transaction cannot be committed")
so every later statement fails. :mod:`db_ops.metrics.executor` connects with ``autocommit=True``
for exactly that reason. Without this flag, running a metric's ``.sql`` through ``run-sql`` reports
a 3930 the collector would never have hit — a false failure that sends the reader after the wrong
bug. It also means nothing is rolled back, so pass it only for read-only SQL.

``timeout_seconds`` is the **statement** budget and, on its own, the connect timeout too.
``connect_timeout_seconds`` separates them, for the same reason ``cmd_access`` keeps two: a
scheduled task that is allowed twenty minutes to run must not wait twenty minutes to discover the
host is down. Omitted (or ``0``), the connect inherits ``timeout_seconds`` — so nothing changes
for a caller that passes one number.

``params`` are **bound by the driver, never interpolated into the SQL text**, which is the whole
reason they exist: a value that reaches here came from a Telegram message, a config file or a
shell argument, and a bound value cannot become a statement. They are positional — the list is
handed to ``cursor.execute`` as a sequence, so the placeholders in the SQL must be the ones the
target's driver reads (``?`` for pyodbc and the Oracle prelude form below, ``%s`` for pg8000 and
pymssql). Named binding is deliberately not offered: it is spelled differently by every driver
this supports, and one shape that works everywhere beats four that each work once.

``prelude`` is SQL prepended to **every batch**, and it exists because of a T-SQL fact: a variable
does not survive a ``GO``, so a multi-batch script that uses ``@spid`` needs its ``DECLARE``
repeated in front of each batch with the same values bound again. The caller builds it —
:func:`db_ops.lib.sql_text.build_parameter_prelude` turns a task's declared parameters into
``("DECLARE @spid int = ?;", [505])``, validating every name and type before either reaches the
text. Nothing here parses it; a caller that already controls ``sql`` gains no reach it did not
have.

Neither is available through the **legacy Oracle bridge** (``sql_access.method`` = ``api`` /
``subprocess``): that tool takes one statement at a time and binds nothing, so a request carrying
either is refused rather than run with the values silently dropped.

**By default only the FIRST result set is captured** (an export has one sheet, a caller has one
table); later sets are drained without their rows ever being fetched, so their rowcounts still
land in ``affected_rows``. ``"capture": "all"`` keeps them instead, and the response carries them
as a JSON array::

    "result_sets": [{"columns": [...], "rows": [[...]], "row_count": 3, "truncated": false}, ...]

``result_sets`` is **always present**, so a caller reads one shape whichever mode it asked for —
under the default it holds the same single set as ``columns``/``rows``, and those four top-level
keys keep meaning exactly what they always did (the first set), because every caller written
before this reads them.

Two caps, and both are visible rather than silent: each set is cut at ``max_rows``
(``truncated`` per set), and at most ``max_result_sets`` sets are kept (``result_sets_truncated``
at the top). A script that loops can produce hundreds of result sets of ``max_rows`` rows each,
and a reader should learn that from a flag rather than from the worker's memory.

**How many sets there are to keep is the driver's answer, not this module's.** ``nextset`` is
probed, never assumed: pyodbc exposes it, so a T-SQL script with three ``SELECT``s comes back as
three sets; pg8000 does not, so the same request against PostgreSQL comes back as **one** — its
own reading of the statement, not a set this dropped. ``capture: "all"`` therefore means "keep
every set the driver offers", which is all anyone can honestly promise across four engines.

A target may declare that its SQL does **not** go over a database connection at all:
``db_instances.json`` ``sql_access.method = "api"`` / ``"subprocess"`` routes the run through the
legacy Oracle tool instead (:mod:`db_ops.common.oracle_bridge`), which is the only way to reach an
Oracle 8i host. The request may carry its own ``sql_access`` block to override the instance's — the
same escape hatch ``run-cmd`` gives for ``cmd_access``, and what lets one run be pointed at a bridge
on this machine without editing the deployed inventory. Everything else about the run is unchanged,
including the result shape, so an export does not care which transport answered it.

``define`` expands SQL*Plus substitution variables. A saved ``.sql`` from a DBA's SQL*Plus session
opens with ``DEFINE JOB_NO = '...'`` and refers to it as ``&JOB_NO``; both are *client* syntax that
SQL*Plus resolves before the server ever sees the statement, so passing such a file to any driver
fails on the literal ``&``. The file's own ``DEFINE`` lines supply the defaults and this field
overrides them, which is how the same archived script runs for a different job number without being
edited.

**Every engine db_ops knows** is supported — sqlserver, postgresql, mysql, oracle — chosen from
the target's ``db_type`` in ``db_instances.json``. On **SQL Server the connection always lands in
master** unless the request names a database: the inventory's ``database`` field is a service
label on most entries and pointing a login at one fails with ``Cannot open database ... (4060)``.
A script that needs another database issues ``USE <db>``. The connection itself belongs to
:mod:`db_ops.common.db_connect`; this module owns target/credential resolution, the row cap and
the rollback contract, all of which are engine-independent.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from db_ops.common import data_sources
from db_ops.common import db_connect
from db_ops.common import oracle_bridge
from db_ops.common import sql_execution
from db_ops.common import data_sources as target_resolve
from db_ops.lib.connection_spec import ConnectionSpec, ConnectionSpecError
from db_ops.lib.target_profile import SOURCE_CONFIG, SOURCE_REQUEST, TargetProfile, ToolChoice
# Re-exported: the row/timeout limits, the sqlplus DEFINE handling and SqlRunError moved to
# db_ops/lib/sql_text.py so apps can prepare and validate a request without importing `common`.
# Running the SQL stayed here.
from db_ops.lib.sql_text import (  # noqa: F401 - re-exported for compatibility
    DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT_SECONDS, SqlRunError, check_sqlplus_define_value,
    expand_sqlplus_defines)






#: What ``capture`` may say. ``first`` keeps one result set and drains the rest without fetching
#: their rows; ``all`` keeps them, as a JSON array.
CAPTURE_FIRST = "first"
CAPTURE_ALL = "all"
CAPTURE_MODES = (CAPTURE_FIRST, CAPTURE_ALL)

#: How many result sets ``capture: "all"`` keeps. A cap exists because a script that loops can
#: produce hundreds, each up to ``max_rows`` rows, and the reader would find that out as memory
#: rather than as a message; ``result_sets_truncated`` says when it bit. ``0`` means no cap.
DEFAULT_MAX_RESULT_SETS = 20


@dataclass(frozen=True)
class SqlRunRequest:
    """The parsed JSON request. Build it with :meth:`from_json`, never field by field."""

    target: str
    sql: str
    database: str = ""
    credential_name: str = ""
    max_rows: int = DEFAULT_MAX_ROWS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    connect_timeout_seconds: int = 0
    commit: bool = False
    autocommit: bool = False
    params: list[Any] = field(default_factory=list)
    prelude: str = ""
    capture: str = CAPTURE_FIRST
    max_result_sets: int = DEFAULT_MAX_RESULT_SETS
    data_dir: str = ""
    sql_access: dict[str, Any] = field(default_factory=dict)
    #: What the request *states* about the target — engine, version, platform, runtime. Merged
    #: over the inventory's own facts, never under them, so a caller holding better information
    #: than `db_instances.json` can act on it without editing deployed config first.
    profile: TargetProfile = field(default_factory=TargetProfile)
    #: Name the driver instead of letting the engine rule pick one. The transport already had this
    #: (`sql_access`); the driver did not, and there was no reason for the asymmetry.
    driver: str = ""
    oracle_client_mode: str = ""
    #: A complete connection stated in the request. When present, **no inventory file is read** —
    #: see :mod:`db_ops.lib.connection_spec`. Mutually exclusive with `target` in meaning, not in
    #: syntax: a `target` alongside it is kept only as the label in the answer.
    connection: ConnectionSpec | None = None

    @classmethod
    def from_json(cls, payload: Any) -> SqlRunRequest:
        """Parse the request object (a dict, or JSON text holding one).

        ``sql_file`` is accepted in place of ``sql`` so a caller can point at a ``.sql`` file
        instead of inlining a long statement; the file is read with ``utf-8-sig`` (SSMS writes
        a BOM). ``user_ref`` is an accepted alias for ``credential_name``. Unknown keys are
        ignored, so a caller may pass a wider config block through.
        """
        if isinstance(payload, SqlRunRequest):
            return payload
        if isinstance(payload, (str, bytes, bytearray)):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise SqlRunError(f"Request is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SqlRunError("Request must be a JSON object.")

        raw_connection = payload.get("connection")
        connection = None
        if raw_connection:
            try:
                connection = ConnectionSpec.from_json(raw_connection)
            except ConnectionSpecError as exc:
                raise SqlRunError(str(exc)) from exc

        target = str(payload.get("target") or "").strip()
        if not target and connection is None:
            raise SqlRunError(
                "target is required (a server_id, or '<db_type> <ip> [port]') — or a "
                '"connection" object stating the whole connection, which reads no inventory file.'
            )

        sql = str(payload.get("sql") or payload.get("sql_text") or "").strip()
        sql_file = str(payload.get("sql_file") or "").strip()
        if sql and sql_file:
            raise SqlRunError("Pass either sql or sql_file, not both.")
        if sql_file:
            path = Path(sql_file)
            if not path.exists():
                raise SqlRunError(f"sql_file not found: {sql_file}")
            sql = path.read_text(encoding="utf-8-sig").strip()
        if not sql:
            raise SqlRunError("sql is required.")
        sql = expand_sqlplus_defines(sql, payload.get("define") or payload.get("defines"))

        return cls(
            target=target,
            sql=sql,
            database=str(payload.get("database") or payload.get("database_name") or "").strip(),
            credential_name=str(
                payload.get("credential_name") or payload.get("user_ref") or ""
            ).strip(),
            max_rows=_positive_int(payload.get("max_rows"), DEFAULT_MAX_ROWS, "max_rows"),
            timeout_seconds=_positive_int(
                payload.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, "timeout_seconds"
            ),
            connect_timeout_seconds=_positive_int(
                payload.get("connect_timeout_seconds"), 0, "connect_timeout_seconds",
                allow_zero=True,
            ),
            commit=bool(payload.get("commit", False)),
            autocommit=bool(payload.get("autocommit", False)),
            params=_bind_params(payload.get("params")),
            prelude=str(payload.get("prelude") or ""),
            capture=_capture_mode(payload.get("capture")),
            max_result_sets=_positive_int(
                payload.get("max_result_sets"), DEFAULT_MAX_RESULT_SETS, "max_result_sets",
                allow_zero=True,
            ),
            data_dir=str(payload.get("data_dir") or "").strip(),
            sql_access=dict(payload.get("sql_access") or {}),
            # Two spellings, one meaning: a "profile" object for a caller passing a whole block,
            # and the bare keys (`major_version`, `platform`, `os`, `runtime`) for a human typing
            # one fact on the command line. The block wins, because stating both and meaning the
            # bare key would be the surprising reading.
            profile=TargetProfile.from_json(payload.get("profile") or {}).merge(
                TargetProfile.from_json(payload)
            ),
            driver=str(payload.get("driver") or "").strip(),
            oracle_client_mode=str(payload.get("oracle_client_mode") or "").strip(),
            connection=connection,
        )










def _bind_params(raw: Any) -> list[Any]:
    """The positional bind values, as a list.

    A **mapping is refused by name** rather than coerced: named binding is spelled differently by
    every driver here (``:name`` on Oracle, ``%(name)s`` on pg8000, unsupported on pyodbc), so a
    dict that quietly became a list would bind by insertion order — right until somebody reordered
    the JSON and the values silently swapped columns.
    """
    if raw in (None, ""):
        return []
    if isinstance(raw, dict):
        raise SqlRunError(
            'params must be a list of values bound positionally, not an object. Named binding is '
            'not portable across the drivers this supports; put the names in "prelude" instead '
            '(DECLARE @name ... = ?) and list the values here in that order.'
        )
    if isinstance(raw, (str, bytes)):
        raise SqlRunError('params must be a list of values; got a single string.')
    if not isinstance(raw, (list, tuple)):
        raise SqlRunError(f"params must be a list of values; got {type(raw).__name__}.")
    return list(raw)


def run_sql(request: Any) -> dict[str, Any]:
    """Run the request's SQL on its target and return the first result set.

    ``request`` is the JSON object documented at the top of this module (a dict, JSON text, or
    an already-parsed :class:`SqlRunRequest`). Returns::

        {"ok": True, "server_id", "database", "credential_name", "username", "columns",
         "rows", "row_count", "affected_rows", "truncated", "committed"}

    The login is echoed back (``credential_name`` / ``username``) because *which user ran this*
    is not visible in the request when it relies on the instance default — and it decides what
    the SQL was allowed to touch. ``rows`` holds native driver values (``datetime``, ``Decimal``, ...) so a caller can format
    them; use :func:`json_safe_result` for a JSON-serializable copy. Raises :class:`SqlRunError`
    with an operator-readable message for every known failure.
    """
    parsed = SqlRunRequest.from_json(request)
    resolved = resolve_request_target(parsed)
    if oracle_bridge.is_legacy(resolved.get("sql_access")):
        return _run_legacy_oracle(parsed, resolved)
    conn = connect_target(resolved, timeout_seconds=parsed.timeout_seconds,
                          connect_timeout_seconds=parsed.connect_timeout_seconds,
                          autocommit=parsed.autocommit)
    # Nothing to roll back and nothing to commit: the driver committed each statement as it ran.
    committed = parsed.autocommit
    try:
        cursor = conn.cursor()
        result_sets, affected_rows, sets_truncated = execute_capture(
            cursor, parsed.sql, max_rows=parsed.max_rows,
            db_type=str(resolved.get("db_type") or "sqlserver"),
            prelude=parsed.prelude, params=parsed.params,
            capture_all=parsed.capture == CAPTURE_ALL,
            max_result_sets=parsed.max_result_sets,
        )
        if parsed.commit and not parsed.autocommit:
            conn.commit()
            committed = True
        # Read before the connection closes; it is a property of the open session.
        observed_version = db_connect.server_version(conn, str(resolved.get("db_type") or ""))
    except Exception as exc:  # noqa: BLE001 - surface SQL/driver errors to the caller.
        raise SqlRunError(f"SQL failed: {exc}") from exc
    finally:
        if not committed:
            # The whole batch (incl. any real-table write / DDL) is undone.
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 - some drivers auto-rollback on close; ignore.
                pass
        conn.close()

    # The *planned* choice plus what actually answered. They can differ, and the difference is the
    # whole point: on SQL Server the plan is "auto" and the connection knows whether Driver 18
    # opened it first time or Driver 17 picked it up after a TLS refusal. Reporting only the plan
    # is what let a version-blind driver order go unexamined for as long as it did.
    tool_report = dict(resolved["tool"])
    describe = getattr(conn, "describe_tool", None)
    if callable(describe):
        tool_report["actual"] = describe()

    # What the *server* said its version is, next to what config claims. Free — every driver keeps
    # it from the handshake — and it closes the half of the version problem config cannot: until
    # now nothing ever compared the number somebody typed with the instance it describes.
    engine_report = resolved["profile"].to_dict()
    engine_report["observed_version"] = observed_version
    drift = db_connect.version_drift(resolved["profile"].major_version, observed_version)
    if drift:
        engine_report["version_drift"] = drift

    first = result_sets[0] if result_sets else {"columns": [], "rows": [], "truncated": False}
    return {
        "ok": True,
        "server_id": resolved["server_id"],
        "database": resolved["database_name"],
        "credential_name": resolved.get("credential_name", ""),
        "username": resolved.get("username", ""),
        # What this ran against and what opened it. The legacy bridge path has always returned
        # `db_version` with the comment that the version is the first thing a reader wants
        # confirmed; the direct path returned nothing equivalent, so the same question had two
        # answers and one of them was blank. `tool.chosen_by` is the half that matters when the
        # answer surprises someone: request, config, rule or default names where to go and edit.
        "engine": engine_report,
        "tool": tool_report,
        # The top four stay the FIRST result set, unchanged, because every caller written before
        # 2026-08-16 reads them and an export still has one sheet.
        "columns": first["columns"],
        "rows": first["rows"],
        "row_count": len(first["rows"]),
        "affected_rows": affected_rows,
        "truncated": first["truncated"],
        # Always present, so a caller reads one shape whichever mode it asked for: under the
        # default `capture: "first"` this holds the same single set as `columns`/`rows`.
        "result_sets": result_sets,
        "result_sets_truncated": sets_truncated,
        "committed": committed,
    }


def json_safe_result(result: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``result`` whose rows are JSON-serializable (dates/Decimals become text).

    **Every** set is converted, not just the top-level one. Missing the ones inside
    ``result_sets`` would leave a `datetime` in the object the CLI is about to `json.dumps` —
    which fails at the very end of a run that already did all its work.
    """
    safe = dict(result)
    safe["rows"] = [sql_execution.make_json_safe(list(row)) for row in result.get("rows", [])]
    safe["result_sets"] = [
        {**item, "rows": [sql_execution.make_json_safe(list(row)) for row in item.get("rows", [])]}
        for item in result.get("result_sets", [])
    ]
    return safe


def _run_legacy_oracle(parsed: SqlRunRequest, resolved: dict[str, Any]) -> dict[str, Any]:
    """Run this request through the legacy Oracle tool and answer in :func:`run_sql`'s shape.

    An 8i host has no connection for db_ops to open, so the whole transaction contract above is
    moot here: the tool runs one statement read-only and never commits. ``committed`` is reported
    False and ``affected_rows`` 0 for that reason — not because they were not measured.
    """
    if parsed.params or parsed.prelude:
        # Refused rather than dropped. The tool inlines one statement with no binds, so running
        # the request without them would either fail on a stray `?` or - worse, for a prelude
        # whose DECLARE happens to parse - run the SQL with the values missing and report success.
        raise SqlRunError(
            f"{resolved['server_id']} reaches its SQL through the legacy Oracle bridge, which "
            "takes one statement at a time and binds no parameters. Inline the values into the "
            "SQL for that target, or use a target db_ops can connect to directly."
        )
    sql_access = resolved["sql_access"]
    try:
        secrets = data_sources.load_secret_text(parsed.data_dir or None)
    except (RuntimeError, OSError, ValueError) as exc:
        raise SqlRunError(str(exc)) from exc
    try:
        result = oracle_bridge.run_query(
            sql=parsed.sql,
            sql_access=sql_access,
            secrets=secrets,
            # The password is already resolved (the credential lookup above did it), so it
            # travels as a literal here rather than being read out of the store a second time.
            credential={
                "username": resolved["username"],
                "password": resolved["password"],
                "role": resolved.get("credential_role", ""),
            },
            host=resolved["ip"] or resolved["server_id"],
            port=resolved.get("port"),
            service_name=resolved.get("service_name") or resolved.get("instance_name") or "",
            # "database" means schema here. Oracle connects to a service, not a database, so the
            # request field that every other engine reads as "run it in here" has no other
            # meaning on this transport - and it is exactly what an archived application script
            # needs when a DBA login runs it (see oracle_bridge.schema_prelude).
            schema=parsed.database,
            now=time.time(),
            # One more than the cap, so "there were more rows" is a fact rather than a guess:
            # the tool reports truncation when it *reaches* the limit, which is also what it
            # would report for a result set that happens to be exactly max_rows long.
            limit=parsed.max_rows + 1,
            timeout_seconds=parsed.timeout_seconds,
        )
    except oracle_bridge.LegacyOracleError as exc:
        raise SqlRunError(str(exc)) from exc

    rows = result["rows"]
    truncated = len(rows) > parsed.max_rows
    if truncated:
        del rows[parsed.max_rows:]
    return {
        "ok": True,
        "server_id": resolved["server_id"],
        "database": resolved["database_name"],
        "credential_name": resolved.get("credential_name", ""),
        "username": resolved.get("username", ""),
        # Same two fields as the direct path, so a caller reads one shape whichever transport
        # answered. The version below is what the bridge *observed*; the one inside `engine` is
        # what config claims — and a disagreement between them is worth seeing.
        "engine": resolved["profile"].to_dict(),
        "tool": resolved["tool"],
        "columns": result["columns"],
        "rows": rows,
        "row_count": len(rows),
        "affected_rows": 0,
        "truncated": truncated,
        # One set, always: the bridge runs a single statement, so there is never a second one to
        # capture. Present anyway, because a caller must not have to know which transport answered
        # to know whether the key is there.
        "result_sets": [{"columns": result["columns"], "rows": rows,
                         "row_count": len(rows), "truncated": truncated}],
        "result_sets_truncated": False,
        "committed": False,
        # Which legacy transport answered, and which Oracle it actually was: on a target this old
        # the version is the first thing a reader wants confirmed.
        "transport": result["transport"],
        "db_version": result["db_version"],
    }


def resolve_request_target(parsed: SqlRunRequest) -> dict[str, Any]:
    """The resolved target for a request, by whichever of the two doors it came in.

    **The whole difference between the doors is what gets read.** With a ``connection`` block,
    nothing: the host, port, engine, version and login are all in the request, and the only thing
    that can still touch a file is a ``password_ref`` — resolved from the environment first, and
    from the encrypted store only if the environment does not have it. With a ``target``, the
    inventory answers, which is the right default for a runbook or a scheduled task and stays it.

    Both produce the same dict, so nothing downstream branches on which was used.
    """
    spec = parsed.connection
    if spec is None:
        return resolve_sqlserver_target(
            parsed.target,
            data_dir=parsed.data_dir or None,
            database=parsed.database,
            credential_name=parsed.credential_name,
            sql_access=parsed.sql_access,
            profile=parsed.profile,
            driver=parsed.driver,
            oracle_client_mode=parsed.oracle_client_mode,
        )

    password = spec.password
    if spec.password_ref:
        try:
            # `resolve_password` reads the environment first, so a caller that exports the ref as
            # an env var stays file-free even while naming one.
            secrets = (
                {} if os.getenv(spec.password_ref, "").strip()
                else data_sources.load_secret_text(parsed.data_dir or None)
            )
            password = sql_execution.resolve_password(spec.credential(), secrets)
        except (RuntimeError, OSError, ValueError) as exc:
            raise SqlRunError(str(exc)) from exc

    resolved = spec.to_resolved(
        password=password,
        database=parsed.database,
        default_database=db_connect.default_database(spec.db_type),
    )
    try:
        resolved["sql_access"] = oracle_bridge.normalize_sql_access(
            parsed.sql_access or spec.sql_access, label=spec.server_id,
        )
    except oracle_bridge.LegacyOracleError as exc:
        raise SqlRunError(str(exc)) from exc

    if oracle_bridge.is_legacy(resolved["sql_access"]):
        resolved["tool"] = ToolChoice(
            str(resolved["sql_access"].get("method") or "api"), SOURCE_REQUEST,
            "the request routes this connection through the legacy bridge, so no driver is opened",
        ).to_dict()
    else:
        try:
            resolved["tool"] = db_connect.tool_for(
                spec.profile.with_(db_type=spec.db_type),
                requested_driver=parsed.driver,
                sqlserver_driver=spec.sqlserver_driver,
                oracle_client_mode=parsed.oracle_client_mode or spec.oracle_client_mode,
            ).to_dict()
        except db_connect.DbConnectError as exc:
            raise SqlRunError(f"{spec.server_id}: {exc}") from exc
    if parsed.driver:
        resolved["sqlserver_driver"] = parsed.driver
    return resolved


def resolve_sqlserver_target(
    target: str,
    *,
    data_dir: str | Path | None = None,
    database: str = "",
    credential_name: str = "",
    sql_access: dict[str, Any] | None = None,
    profile: TargetProfile | None = None,
    driver: str = "",
    oracle_client_mode: str = "",
) -> dict[str, Any]:
    """Resolve a SQL Server connection from a unified target spec.

    ``target`` is either a ``server_id`` (e.g. ``ACME-192-0-2-248``) or a
    ``<db_type> <ip> [port]`` spec (e.g. ``mssql 192.0.2.248 1433``) — see
    :mod:`db_ops.common.data_sources`. The instance is looked up in ``db_instances.json``.
    Only ``sqlserver`` instances are supported. ``database`` overrides the instance's database
    for this run.

    **Which login runs the SQL**: ``credential_name`` when the caller names one, else the
    instance's ``default_credential_name``, resolved against that server's
    ``database_credentials`` group in ``users.json``; the password comes from the encrypted
    secret file via the credential's ``password_ref``. The instance default is often a DBA
    login, so a caller that only needs to read should name a least-privilege credential rather
    than inherit it. The resolved ``credential_name``/``username`` are returned so the caller
    can log who it connected as. Raises :class:`SqlRunError` when the spec is unknown, is not
    SQL Server, or has no usable credential.

    **What decides the tool**: ``profile`` (what the caller states) merged *over* the instance
    record's own ``db_type`` / ``major_version`` / ``platform`` / ``os``. The returned
    ``profile`` carries a ``sources`` map saying which side supplied each field, and ``tool`` says
    which driver that implies and who chose it — so an answer can be attributed without re-reading
    the inventory. Neither is a lookup this function performs twice: the instance record is
    already in hand.
    """
    try:
        instance = target_resolve.resolve_target_instance(target, data_dir=data_dir)
    except target_resolve.TargetResolveError as exc:
        raise SqlRunError(str(exc)) from exc

    server_id = str(instance.get("server_id") or "").strip()
    try:
        resolved_sql_access = oracle_bridge.normalize_sql_access(
            sql_access or instance.get("sql_access"), label=server_id or target,
        )
    except oracle_bridge.LegacyOracleError as exc:
        raise SqlRunError(str(exc)) from exc
    db_type = db_connect.normalize_db_type(instance.get("db_type"))
    if db_type not in db_connect.SUPPORTED_DB_TYPES:
        raw = str(instance.get("db_type") or "").strip() or "unknown"
        raise SqlRunError(
            f"Target {server_id or target} is db_type={raw}; supported: "
            f"{', '.join(db_connect.SUPPORTED_DB_TYPES)}."
        )

    credential = _find_sqlserver_credential(
        instance, data_dir=data_dir, credential_name=credential_name, db_type=db_type
    )
    try:
        # Missing decryption key / unreadable secret file is an operator condition, not a bug:
        # report it like every other run failure instead of a traceback.
        secrets = data_sources.load_secret_text(data_dir)
        password = sql_execution.resolve_password(credential, secrets)
    except (RuntimeError, OSError, ValueError) as exc:
        raise SqlRunError(str(exc)) from exc

    # What the caller states wins over what the inventory records — the caller is looking at the
    # server, `db_instances.json` is a file somebody edited. `db_type` is the exception and stays
    # the resolved one: it decides which credential group and which default database were already
    # picked above, so letting a request override it here would describe a different target than
    # the one that was resolved.
    resolved_profile = (profile or TargetProfile()).merge(
        TargetProfile.from_json(instance, source=SOURCE_CONFIG)
    ).with_(db_type=db_type)
    if oracle_bridge.is_legacy(resolved_sql_access):
        # A legacy target's tool is the transport itself, and the driver rule must not be asked:
        # it would refuse an 8i instance for being 8i, which is exactly the reason this target is
        # configured to avoid the driver in the first place.
        tool = ToolChoice(
            str(resolved_sql_access.get("method") or "api"), SOURCE_CONFIG,
            "sql_access routes this target through the legacy bridge, so no driver is opened",
        )
    else:
        try:
            tool = db_connect.tool_for(
                resolved_profile,
                requested_driver=str(driver or "").strip(),
                sqlserver_driver=str(instance.get("sqlserver_driver") or "").strip(),
                oracle_client_mode=oracle_client_mode,
            )
        except db_connect.DbConnectError as exc:
            raise SqlRunError(f"{server_id or target}: {exc}") from exc

    # Each engine has its own idea of the default database, and of what "database" even means
    # (Oracle connects to a service). db_connect owns those defaults; resolving one here rather
    # than passing "" keeps the result honest about *where the SQL ran* — which is the field an
    # operator reads first when a query returns something they did not expect.
    #
    # SQL Server never inherits the instance's `database`: it connects to **master** unless the
    # caller names a database in the request itself. The inventory field is unreliable for this
    # engine — on most entries it is empty and on the rest it duplicates `master`, but nothing
    # stops someone writing the service label there (`APPDB-PROD`, `SALESDB-PROD`), which is not a
    # database that exists. Metric collection was doing exactly that and every SQL Server target
    # failed at once with `Cannot open database "APPDB-PROD" ... (4060)`. Same class of bug, one
    # layer up: `/spbot_sql_to_xlsx` and `run-sql` would refuse to connect at all. master is
    # always openable, and a script that needs another database says `USE <db>` — or the caller
    # passes "database" explicitly, which still wins below.
    if db_type == "sqlserver":
        database_name = str(database or "") or db_connect.default_database(db_type)
    else:
        database_name = (
            str(database or instance.get("database") or "")
            or db_connect.default_database(db_type)
        )
    return {
        "server_id": server_id,
        "db_type": db_type,
        "ip": str(instance.get("ip") or ""),
        "port": int(instance.get("port") or 0) or None,
        "instance_name": str(instance.get("instance_name") or ""),
        "service_name": str(instance.get("service_name") or ""),
        "database_name": database_name,
        "sqlserver_driver": str(driver or instance.get("sqlserver_driver") or "").strip(),
        "oracle_client_mode": str(oracle_client_mode or "").strip(),
        "profile": resolved_profile,
        "tool": tool.to_dict(),
        "credential_name": str(credential.get("credential_name") or ""),
        "username": str(credential["username"]),
        "password": password,
        # SYSDBA is a property of the credential, not of the request: on 8i the DBA views live
        # nowhere else, and the legacy transport needs to know before it connects.
        "credential_role": str(credential.get("role") or ""),
        # How this target's SQL is reached. The request's own block wins over the instance's, so
        # one run can be pointed at a different bridge without touching the deployed inventory.
        "sql_access": resolved_sql_access,
    }


# The resolver stopped being SQL-Server-only; the old name stays as an alias because it is
# published API (docs/13_common.md) and used by tests and the Telegram command docs.
resolve_target = resolve_sqlserver_target


def connect_target(target: dict[str, Any], *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
                   connect_timeout_seconds: int = 0, autocommit: bool = False) -> Any:
    """Open a connection to a resolved target, on whichever engine it names.

    Every engine rule — driver choice and the ODBC→pymssql fallback, default ports and
    databases, and the in-server statement timeout each engine spells differently — belongs to
    :mod:`db_ops.common.db_connect`, the one place db_ops reaches a database. This adds only the
    error type this module's callers expect.

    ``autocommit`` is what a metric's own connection uses; see the module docstring for why a
    metric cannot be reproduced faithfully without it.
    """
    try:
        return db_connect.connect_engine(
            autocommit=autocommit,
            db_type=str(target.get("db_type") or "sqlserver"),
            host=str(target.get("ip") or target.get("server_id")),
            port=target.get("port"),
            database=str(target.get("database_name") or ""),
            service_name=str(target.get("service_name") or ""),
            username=str(target["username"]),
            password=str(target["password"]),
            sqlserver_driver=str(target.get("sqlserver_driver") or "").strip(),
            # What makes the driver choice version-aware. The resolver has already merged the
            # request's stated facts over the inventory's, so by here there is one profile and
            # `db_connect` never has to ask where a field came from.
            profile=target.get("profile"),
            oracle_client_mode=str(target.get("oracle_client_mode") or "").strip(),
            # Two numbers, one default. `timeout_seconds` alone means both, which is what every
            # caller before 2026-08-16 passed and what most still want. They separate for the one
            # shape that needs it: a scheduled task allowed twenty minutes of *statement* must not
            # wait twenty minutes to find out the host is down.
            connect_timeout_seconds=connect_timeout_seconds or timeout_seconds,
            statement_timeout_seconds=timeout_seconds,
        )
    except db_connect.DbConnectError as exc:
        raise SqlRunError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a connect failure is an operator message, not a trace.
        raise SqlRunError(f"connect failed: {exc}") from exc


def split_batches_for(sql_text: str, db_type: str = "sqlserver") -> list[str]:
    """The statements to run, in order, for this engine.

    ``GO`` is a SQL Server *client* batch separator, not SQL; the shared splitter honours it.
    Oracle is the one engine that also rejects the thing every other engine tolerates — a
    trailing semicolon (``SELECT 1;`` raises ORA-00911) — so it is stripped there. Doing that
    unconditionally would break MySQL/PostgreSQL scripts that legitimately send several
    semicolon-separated statements in one batch.
    """
    batches = sql_execution.split_sql_batches(sql_text)
    if db_connect.normalize_db_type(db_type) == "oracle":
        return [batch.rstrip().rstrip(";").rstrip() for batch in batches]
    return batches


def execute_capture(
    cursor: Any, sql_text: str, *, max_rows: int = DEFAULT_MAX_ROWS,
    db_type: str = "sqlserver", prelude: str = "", params: "Sequence[Any] | None" = None,
    capture_all: bool = False, max_result_sets: int = DEFAULT_MAX_RESULT_SETS,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Execute ``sql_text`` and capture its result sets.

    Returns ``(result_sets, affected_rows, sets_truncated)``. Each entry is
    ``{"columns", "rows", "row_count", "truncated"}``; ``affected_rows`` sums the rowcount of
    batches that produced no result set (DML/DDL), so a caller can see that the script wrote
    something; ``sets_truncated`` says the script produced more result sets than were kept.

    ``capture_all=False`` keeps the first set and **drains** the rest — their rows are never
    fetched, only their rowcounts counted. That is the default because it is what an export wants
    (a workbook has one sheet) and because fetching a result set nobody asked for is exactly the
    unbounded read ``max_rows`` exists to prevent.

    Engine-agnostic by construction: ``nextset`` is probed rather than assumed (only some drivers
    expose multiple result sets), and batch splitting is delegated to :func:`split_batches_for`.

    ``prelude`` goes in front of **every** batch and ``params`` are bound to **every** batch, both
    for the same T-SQL reason: a variable does not survive a ``GO``, so a ``DECLARE`` and its
    values have to be repeated per batch or the second batch fails on an undeclared name.

    The values are passed as a **sequence**, not star-unpacked. pyodbc accepts either, but
    pg8000, pymssql and oracledb take a sequence only — and this function runs on all four.
    """
    max_rows = max(1, int(max_rows))
    # `None` is "no cap", which is what `max_result_sets: 0` asks for. Spelled as None rather than
    # as 0 because `len(kept) < 0` is false and would have kept nothing at all — the opposite.
    keep_sets: int | None = 1
    if capture_all:
        keep_sets = int(max_result_sets) if int(max_result_sets) > 0 else None
    bound = tuple(params or ())
    result_sets: list[dict[str, Any]] = []
    affected_rows = 0
    sets_truncated = False

    def _fetch(description: Any) -> dict[str, Any]:
        columns = [str(col[0]) for col in description]
        rows: list[list[Any]] = []
        # One row past the cap, then discarded: a set cut at the cap is otherwise
        # indistinguishable from a complete one, which is the whole point of `truncated`.
        while len(rows) <= max_rows:
            chunk = cursor.fetchmany(min(1000, max_rows + 1 - len(rows)))
            if not chunk:
                break
            rows.extend(list(row) for row in chunk)
        truncated = len(rows) > max_rows
        if truncated:
            del rows[max_rows:]
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}

    for batch in split_batches_for(sql_text, db_type):
        if not batch.strip():
            continue
        statement = prelude + batch if prelude else batch
        if bound:
            cursor.execute(statement, bound)
        else:
            cursor.execute(statement)
        while True:
            description = cursor.description
            if description:
                if keep_sets is None or len(result_sets) < keep_sets:
                    result_sets.append(_fetch(description))
                else:
                    # Drained, not fetched. Its rows are never read.
                    sets_truncated = True
            else:
                rowcount = cursor.rowcount
                if rowcount and rowcount > 0:
                    affected_rows += int(rowcount)
            nextset = getattr(cursor, "nextset", None)
            if not callable(nextset) or not nextset():
                break
    # Under the default, "there was a second set" is not truncation — it is the documented
    # behaviour. Only a caller that asked for all of them can be short-changed.
    return result_sets, affected_rows, (sets_truncated and capture_all)


def execute_capture_first(
    cursor: Any, sql_text: str, *, max_rows: int = DEFAULT_MAX_ROWS,
    db_type: str = "sqlserver", prelude: str = "", params: "Sequence[Any] | None" = None,
) -> tuple[list[str], list[list[Any]], int, bool]:
    """:func:`execute_capture` for the first result set, as ``(columns, rows, affected, truncated)``.

    Kept as the name callers already use — ``sqlserver_emergency`` reads a single lookup through
    it, and its four-tuple is easier to unpack than a list of one dict.
    """
    result_sets, affected_rows, _ = execute_capture(
        cursor, sql_text, max_rows=max_rows, db_type=db_type, prelude=prelude, params=params,
    )
    first = result_sets[0] if result_sets else {"columns": [], "rows": [], "truncated": False}
    return first["columns"], first["rows"], affected_rows, first["truncated"]


def _find_sqlserver_credential(
    instance: dict[str, Any], *, data_dir: str | Path | None, credential_name: str = "",
    db_type: str = "sqlserver",
) -> dict[str, Any]:
    """The requested ``credential_name``, else the instance's declared default — never a guess.

    Selection itself belongs to :func:`db_ops.common.data_sources.find_database_credential`
    (shared with metrics, sql_tasks and the Telegram commands); this only decides *which name*
    to ask for and re-raises in this module's error type.

    ``db_type`` picks which credential group to search: credentials are grouped per engine in
    ``users.json``, so looking a PostgreSQL target up in the sqlserver group finds nothing and
    reports "no credential" for a target that has one.
    """
    try:
        return data_sources.find_database_credential(
            data_sources.load_credentials(db_type, data_dir),
            server_id=str(instance.get("server_id", "")).strip(),
            credential_name=credential_name.strip()
            or str(instance.get("default_credential_name") or "").strip(),
        )
    except data_sources.CredentialNotFound as exc:
        raise SqlRunError(str(exc)) from exc


def _positive_int(value: Any, default: int, name: str, *, allow_zero: bool = False) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SqlRunError(f"{name} must be an integer: {value!r}") from exc
    floor = 0 if allow_zero else 1
    if number < floor:
        raise SqlRunError(f"{name} must be >= {floor}: {number}")
    return number


def _capture_mode(value: Any) -> str:
    """``first`` (default) or ``all``. Anything else is refused rather than treated as the default.

    A typo would otherwise be invisible: the caller asked for every result set, got one, and the
    only symptom is a report that looks a little short.
    """
    text = str(value or CAPTURE_FIRST).strip().lower()
    if text not in CAPTURE_MODES:
        raise SqlRunError(f'capture must be one of {", ".join(CAPTURE_MODES)}; got {value!r}.')
    return text
