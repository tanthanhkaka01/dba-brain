"""Run SQL against **one** database target, from a JSON request object.

Every app that needs "connect to this one database, run this SQL, give me the rows back"
goes through here. Like :mod:`db_ops.common.remote_exec` (which answers the same question
for a VM), **the input is a JSON object** — the shape below travels from a config file, a
Telegram command, or the CLI into the API untranslated, and the result is JSON-shaped too::

    {
      "connection": {...},              // REQUIRED since 0.24.0: the login, complete - db_type,
                                        // host, port, username, password (lib.connection_spec)
      "target": "ACME-192-0-2-115",     // the label the answer carries; nothing is looked up
      "sql_text": "SELECT TOP 10 * FROM sys.objects",   // or "sql_file": "path/to/query.sql";
                                        // "sql" is still read, never written
      "database_name": "SALESDB",       // optional ("database" still read). SQL Server: default is always `master`
                                        // (say USE, or name it here). Other engines: default is
                                        // the connection's database.
      "max_rows": 50000,
      "timeout_seconds": 30,             // the STATEMENT budget, and the connect unless the
                                        // next field overrides it
      "connect_timeout_seconds": 30,    // optional; opening the connection only (see below)
      "commit": false,                  // default false: the batch is ALWAYS rolled back
      "autocommit": false,              // true = no transaction at all (see below)
      "params": [505, "SALESDB"],          // values BOUND to the placeholders in the SQL
      "prelude": "DECLARE @spid int = ?;",   // SQL prepended to every batch (see below)
      "named_params": {"job_no": "AA2608/01902"},  // Oracle / PostgreSQL: bound where the SQL
                                        // says :job_no (see below); not with params/prelude
      "capture": "first",               // first (default) | all — see below
      "max_result_sets": 20,            // capture: all only; 0 = no cap
      "define": {"JOB_NO": "AA2503/00818"},  // optional SQL*Plus &substitutions (see below)
      "sql_access": {...},              // optional transport override; see below
      "secrets": {"<ref>": "..."}       // optional; the refs that sql_access names (an 8i bridge)
    }

**Nothing is looked up** (rules R09, 0.24.0). Until then a bare ``target`` was resolved here against
``db_instances.json``, ``users.json`` and the secret store; now the app that holds the ``server_id``
finishes the request from its own ``data/`` (``db_ops.lib.data_sources.request_fill``) and a request
without ``connection`` is refused with :data:`NO_CONNECTION`. The lookup itself -
``resolve_sqlserver_target`` - left with it: ``rotate-password`` and ``check-secret``, whose job is
the store, fill their target the way the apps do and hand this module the stated connection.

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

``named_params`` is the same promise for the two engines whose SQL names a value ``:name`` -
Oracle (a bind variable) and PostgreSQL (a psql variable) - and which read no ``DECLARE``. Each
``:name`` outside a string or a comment is rewritten, statement by statement, into the placeholder
the driver in this process reads (:func:`db_ops.common.db_connect.parameter_style`), and the values
are bound positionally in that order. So the one shape the caller writes is portable, and the
driver-specific spelling that made named binding unportable stays here. A name no statement says
is refused before anything runs, and so is the field on another engine (added in 0.24.0 for SQL
tasks with parameters on those engines, 0.23.0 section 1.55).

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

A target may declare that its SQL does **not** go over a database connection at all: a
``sql_access.method = "api"`` / ``"subprocess"`` in the connection routes the run through the
legacy Oracle tool instead (:mod:`db_ops.common.oracle_bridge`), which is the only way to reach an
Oracle 8i host. The request may carry its own ``sql_access`` block to override the connection's — the
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
the connection's ``db_type``. On **SQL Server the connection always lands in
master** unless the request names a database: the inventory's ``database`` field is a service
label on most entries and pointing a login at one fails with ``Cannot open database ... (4060)``.
A script that needs another database issues ``USE <db>``. The connection itself belongs to
:mod:`db_ops.common.db_connect`; this module owns the stated connection's resolution, the row cap and
the rollback contract, all of which are engine-independent.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from db_ops.common import db_connect
from db_ops.common import oracle_bridge
from db_ops.common import sql_execution
from db_ops.lib.connection_spec import ConnectionSpec, ConnectionSpecError
from db_ops.lib.driver_warnings import read_next_set
from db_ops.lib.target_profile import SOURCE_REQUEST, TargetProfile, ToolChoice
# Re-exported: the row/timeout limits, the sqlplus DEFINE handling and SqlRunError moved to
# db_ops/lib/sql_text.py so apps can prepare and validate a request without importing `common`.
# Running the SQL stayed here.
from db_ops.lib.sql_text import (  # noqa: F401 - re-exported for compatibility
    DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT_SECONDS, SqlRunError, check_sqlplus_define_value,
    expand_sqlplus_defines)
from db_ops.lib.sql_text import (NAMED_BIND_DB_TYPES, SqlParameterError, bind_named_values,
                                 check_named_values, named_placeholders, oracle_statement,
                                 split_postgresql_statements)






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
    max_rows: int = DEFAULT_MAX_ROWS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    connect_timeout_seconds: int = 0
    commit: bool = False
    autocommit: bool = False
    params: list[Any] = field(default_factory=list)
    prelude: str = ""
    #: Lower-case name -> value, bound where an Oracle or PostgreSQL statement says ``:name``.
    named_params: dict[str, Any] = field(default_factory=dict)
    capture: str = CAPTURE_FIRST
    max_result_sets: int = DEFAULT_MAX_RESULT_SETS
    sql_access: dict[str, Any] = field(default_factory=dict)
    #: ``{ref: value}`` for the secret refs an 8i bridge's ``sql_access`` names - its signing secret,
    #: a whole connect string. Stated by the caller, like the password: this process opens no store.
    secrets: dict[str, str] = field(default_factory=dict)
    #: What the request *states* about the target — engine, version, platform, runtime. Merged
    #: over the inventory's own facts, never under them, so a caller holding better information
    #: than `db_instances.json` can act on it without editing deployed config first.
    profile: TargetProfile = field(default_factory=TargetProfile)
    #: Name the driver instead of letting the engine rule pick one. The transport already had this
    #: (`sql_access`); the driver did not, and there was no reason for the asymmetry.
    driver: str = ""
    oracle_client_mode: str = ""
    #: The complete connection - required since 0.24.0 (rules R09): no inventory file is read, and
    #: a `target` alongside it is only the label in the answer. See :mod:`db_ops.lib.connection_spec`.
    connection: ConnectionSpec | None = None

    @classmethod
    def from_json(cls, payload: Any) -> SqlRunRequest:
        """Parse the request object (a dict, or JSON text holding one).

        ``sql_file`` is accepted in place of ``sql`` so a caller can point at a ``.sql`` file
        instead of inlining a long statement; the file is read with ``utf-8-sig`` (SSMS writes
        a BOM). Unknown keys are ignored, so a caller may pass a wider config block through.
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
        if connection is None:
            # A server_id names a login only in some node's data/, which this process does not
            # read (rules R09) - the caller states it (db_ops.lib.data_sources.request_fill).
            raise SqlRunError(NO_CONNECTION.format(what="run-sql"))

        sql = str(payload.get("sql_text") or payload.get("sql") or "").strip()
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

        params = _bind_params(payload.get("params"))
        prelude = str(payload.get("prelude") or "")
        try:
            named_params = check_named_values(payload.get("named_params"))
        except SqlParameterError as exc:
            raise SqlRunError(str(exc)) from exc
        if named_params and (params or prelude):
            # Two numberings of one statement's placeholders cannot both be right.
            raise SqlRunError("named_params binds :name placeholders; params and prelude bind "
                              "positional ones. Pass one or the other.")

        return cls(
            target=target,
            sql=sql,
            database=str(payload.get("database_name") or payload.get("database") or "").strip(),
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
            params=params,
            prelude=prelude,
            named_params=named_params,
            capture=_capture_mode(payload.get("capture")),
            max_result_sets=_positive_int(
                payload.get("max_result_sets"), DEFAULT_MAX_RESULT_SETS, "max_result_sets",
                allow_zero=True,
            ),
            sql_access=dict(payload.get("sql_access") or {}),
            secrets={str(key): str(value) for key, value in dict(payload.get("secrets") or {}).items()},
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
            'params must be a list of values bound positionally, not an object. For values by name '
            'use "named_params" on Oracle or PostgreSQL (the SQL says :name), or "prelude" on SQL '
            'Server (DECLARE @name ... = ?) with the values listed here in that order.'
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
    resolved = resolve_connection_spec(
        parsed.connection, database=parsed.database, sql_access=parsed.sql_access,
        driver=parsed.driver, oracle_client_mode=parsed.oracle_client_mode, profile=parsed.profile)
    if oracle_bridge.is_legacy(resolved.get("sql_access")):
        return _run_legacy_oracle(parsed, resolved)
    # Before connecting: a value meant for a :name the SQL never says is a request error, and a
    # connection opened only to refuse it is a login the server's audit shows for nothing.
    check_named_params(parsed.sql, str(resolved.get("db_type") or ""), parsed.named_params)
    conn = connect_target(resolved, timeout_seconds=parsed.timeout_seconds,
                          connect_timeout_seconds=parsed.connect_timeout_seconds,
                          autocommit=parsed.autocommit)
    # Nothing to roll back and nothing to commit: the driver committed each statement as it ran.
    committed = parsed.autocommit
    warnings: list[str] = []
    try:
        cursor = conn.cursor()
        result_sets, affected_rows, sets_truncated = execute_capture(
            cursor, parsed.sql, max_rows=parsed.max_rows,
            db_type=str(resolved.get("db_type") or "sqlserver"),
            prelude=parsed.prelude, params=parsed.params, named_params=parsed.named_params,
            capture_all=parsed.capture == CAPTURE_ALL,
            max_result_sets=parsed.max_result_sets,
            warnings=warnings,
        )
        if warnings and not parsed.autocommit:
            # Under a wrapping transaction the promise is all or nothing, and past a warning the
            # driver shows nothing - not even an error (lib/driver_warnings.py). "All" cannot be
            # checked, so the finally below rolls back, and the reason is said instead of the bare
            # warning that used to be reported as the failure. With autocommit every statement has
            # already committed as it ran, and that run is not failed for a warning.
            raise SqlRunError(
                "stopped reading at a SQL Server warning, and nothing after it could be checked, so "
                f"the transaction was rolled back: {warnings[0]}. A script that manages its own "
                "transactions runs with autocommit."
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
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - see below
            # A target that dropped mid-statement has a dead socket, and pg8000 raises on closing
            # it. Unguarded, that replaced the SqlRunError above and reached the caller as a
            # traceback instead of an answer (0.24.0 §1.60, seen on the 0.23.0 soak).
            pass

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
        "database_name": resolved["database_name"],
        "credential_name": resolved.get("credential_name", ""),
        "username": resolved.get("username", ""),
        # What this ran against and what opened it. The legacy bridge path has always returned
        # `db_version` with the comment that the version is the first thing a reader wants
        # confirmed; the direct path returned nothing equivalent, so the same question had two
        # answers and one of them was blank. `tool.chosen_by` is the half that matters when the
        # answer surprises someone: request, config, rule or default names where to go and edit.
        "target_profile": engine_report,
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
        # Driver warnings that ended the reading of a batch. Empty on almost every run; when not,
        # the run succeeded but nothing after the warning was seen (lib/driver_warnings.py).
        "warnings": warnings,
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
    if parsed.params or parsed.prelude or parsed.named_params:
        # Refused rather than dropped. The tool inlines one statement with no binds, so running
        # the request without them would either fail on a stray `?` or - worse, for a prelude
        # whose DECLARE happens to parse - run the SQL with the values missing and report success.
        raise SqlRunError(
            f"{resolved['server_id']} reaches its SQL through the legacy Oracle bridge, which "
            "takes one statement at a time and binds no parameters. Inline the values into the "
            "SQL for that target, or use a target db_ops can connect to directly."
        )
    sql_access = resolved["sql_access"]
    # The bridge's own secrets are the request's (rules R09) - never the store's.
    secrets = dict(parsed.secrets)
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
        "database_name": resolved["database_name"],
        "credential_name": resolved.get("credential_name", ""),
        "username": resolved.get("username", ""),
        # Same two fields as the direct path, so a caller reads one shape whichever transport
        # answered. The version below is what the bridge *observed*; the one inside `target_profile` is
        # what config claims — and a disagreement between them is worth seeing.
        "target_profile": resolved["profile"].to_dict(),
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


#: What a command below says when its request states no database to reach. The fix is the
#: caller's, and the message says whose: common.cli reads no configuration (rules R09).
NO_CONNECTION = (
    '{what} needs a "connection" object - db_type, host, port, username and password. common.cli '
    "reads no configuration (rules R09): the app that holds a server_id states the login "
    "(db_ops.lib.data_sources.request_fill does it from this node's data/)."
)


def resolve_stated_connection(request: Any, *, database: str = "", what: str = "") -> dict[str, Any]:
    """The database a request states in its ``connection`` block - the only way the operations in
    ``common`` reach one since 0.24.0 (rules R09).

    The same resolved dict ``run-sql`` works from, so nothing downstream changes. A request naming
    only a ``server_id`` is refused with :data:`NO_CONNECTION`, and a ``password_ref`` is taken from
    the environment or not at all: opening the secret store would be the lookup this replaces.
    """
    raw = request.get("connection") if isinstance(request, dict) else None
    if not raw:
        raise SqlRunError(NO_CONNECTION.format(what=what or "this command"))
    try:
        spec = ConnectionSpec.from_json(raw)
    except ConnectionSpecError as exc:
        raise SqlRunError(str(exc)) from exc
    return resolve_connection_spec(
        spec, database=database, sql_access=dict(request.get("sql_access") or {}),
        driver=str(request.get("driver") or "").strip(),
        oracle_client_mode=str(request.get("oracle_client_mode") or "").strip())


def stated_connection(request: Any, *, what: str = "") -> dict[str, Any]:
    """The request's ``connection`` block, checked as :func:`resolve_stated_connection` checks it -
    for a parser that hands the block on to :func:`run_sql` rather than connecting itself, so a
    refusal names the missing login before anything runs."""
    resolve_stated_connection(request, what=what)
    return dict(request["connection"])


def resolve_connection_spec(
    spec: ConnectionSpec,
    *,
    database: str = "",
    sql_access: dict[str, Any] | None = None,
    driver: str = "",
    oracle_client_mode: str = "",
    profile: TargetProfile | None = None,
) -> dict[str, Any]:
    """A stated connection, as the resolved dict every caller downstream reads.

    A ``password_ref`` is taken from this process's environment or not at all: no secret store is
    opened (rules R09, 0.24.0 - until then a ref the environment lacked was read from the store).

    ``profile`` is what the request states about the target beside its connection - ``run-sql``'s
    top-level ``major_version``, ``platform``, ``os``, ``runtime`` or ``profile`` block. It is laid
    over the connection's own facts, never under them: it is the field that decides a driver, and
    when every request came through this door it was parsed and then dropped, so an 8i target
    stated as version 8 went to python-oracledb anyway.
    """
    password = spec.password
    if spec.password_ref and not password:
        # Not from the environment either (owner decision G3.5). A ref here was looked up in this
        # process's environment under the name the request gave, so a request naming
        # DB_OPS_SECRET_KEY sent the node's passphrase, as a login password, to the host it named.
        raise SqlRunError(
            f"connection.password_ref {spec.password_ref!r} is not resolved here: this command reads "
            'no secret store and no environment (rules R09, G3.5). Send "password" - the app that '
            "calls resolves the ref from its own store.")

    resolved = spec.to_resolved(
        password=password,
        database=database,
        default_database=db_connect.default_database(spec.db_type),
    )
    stated = (profile or TargetProfile()).merge(spec.profile).with_(db_type=spec.db_type)
    resolved["profile"] = stated
    try:
        resolved["sql_access"] = oracle_bridge.normalize_sql_access(
            sql_access or spec.sql_access, label=spec.server_id,
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
                stated,
                requested_driver=driver,
                sqlserver_driver=spec.sqlserver_driver,
                oracle_client_mode=oracle_client_mode or spec.oracle_client_mode,
            ).to_dict()
        except db_connect.DbConnectError as exc:
            raise SqlRunError(f"{spec.server_id}: {exc}") from exc
    if driver:
        resolved["sqlserver_driver"] = driver
    return resolved


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
            sqlserver_tls_verify=target.get("sqlserver_tls_verify") is True,
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


def split_batches_for(sql_text: str, db_type: str = "sqlserver", *,
                      statements: bool = True) -> list[str]:
    """The statements to run, in order, for this engine.

    ``GO`` is a SQL Server *client* batch separator, not SQL; the shared splitter honours it.
    Oracle is the one engine that also rejects the thing every other engine tolerates — a
    trailing semicolon (``SELECT 1;`` raises ORA-00911) — so it is stripped there, except after a
    PL/SQL block's ``END``, which requires it (``lib.sql_text.oracle_statement``). Doing that
    unconditionally would break MySQL/PostgreSQL scripts that legitimately send several
    semicolon-separated statements in one batch.
    """
    batches = sql_execution.split_sql_batches(sql_text)
    engine = db_connect.normalize_db_type(db_type)
    if engine == "oracle":
        return [oracle_statement(batch) for batch in batches]
    if engine == "postgresql" and statements:
        # One statement per execute: pg8000 returns one result set per execute, so a whole script
        # merged every SELECT's rows into one set (lib.sql_text.split_postgresql_statements). Not
        # when values are bound - they are numbered for the whole script, not for each statement.
        return [stmt for batch in batches for stmt in split_postgresql_statements(batch)]
    return batches


def check_named_params(sql_text: str, db_type: str, named_params: "dict[str, Any] | None") -> None:
    """Refuse ``named_params`` this SQL cannot take: another engine's, or a name it never says.

    Both are refusals rather than a value quietly unused: a task run with ``job_no=...`` against a
    script that says ``:jobno`` would otherwise run with nothing bound where the operator meant a
    value, and look like it worked.
    """
    if not named_params:
        return
    engine = db_connect.normalize_db_type(db_type)
    if engine not in NAMED_BIND_DB_TYPES:
        raise SqlRunError(
            f"named_params are bound where the SQL says :name, on {' and '.join(NAMED_BIND_DB_TYPES)}; "
            f"this target is {engine or 'of no known engine'}. On SQL Server the SQL says @name: pass "
            '"prelude" (lib.sql_text.build_parameter_prelude) and "params".')
    said = named_placeholders(sql_text, engine)
    unread = sorted(set(named_params) - said)
    if unread:
        raise SqlRunError(
            f"named_params {', '.join(unread)}: the SQL never says "
            f"{', '.join(':' + name for name in unread)} outside a string or a comment, so the "
            f"value would bind to nothing. It says: {', '.join(sorted(said)) or 'no :name at all'}.")


def execute_capture(
    cursor: Any, sql_text: str, *, max_rows: int = DEFAULT_MAX_ROWS,
    db_type: str = "sqlserver", prelude: str = "", params: "Sequence[Any] | None" = None,
    named_params: "dict[str, Any] | None" = None,
    capture_all: bool = False, max_result_sets: int = DEFAULT_MAX_RESULT_SETS,
    warnings: "list[str] | None" = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Execute ``sql_text`` and capture its result sets.

    ``warnings``, when given, collects the driver warnings that ended the reading of a batch (see
    :mod:`db_ops.lib.driver_warnings`): a warning is not raised, and a caller that passes no list
    gets the same behaviour with the warnings dropped.

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

    ``named_params`` (Oracle, PostgreSQL) are bound **per statement**: each statement gets the
    values of the ``:name`` placeholders it says, written in the driver's own style
    (:func:`db_ops.lib.sql_text.bind_named_values`). So a PostgreSQL script is still split into
    its statements, where positional ``params`` have to keep it whole.
    """
    check_named_params(sql_text, db_type, named_params)
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

    style = db_connect.parameter_style(db_type) if named_params else ""
    for batch in split_batches_for(sql_text, db_type, statements=not bound):
        if not batch.strip():
            continue
        statement = prelude + batch if prelude else batch
        values: "tuple[Any, ...]" = bound
        if named_params:
            try:
                statement, by_name = bind_named_values(statement, named_params, db_type=db_type,
                                                       style=style)
            except SqlParameterError as exc:
                raise SqlRunError(str(exc)) from exc
            values = tuple(by_name)
        if values:
            cursor.execute(statement, values)
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
            if not read_next_set(cursor, warnings if warnings is not None else []):
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


#: How many rows :func:`query_rows` reads before it refuses. A read that answers as dicts is a
#: catalogue read - databases, logins, jobs, the files in a folder - and one past a million rows is
#: reading the wrong thing; handing back the first million of an unknown number would hide that.
QUERY_ROWS_CEILING = 1_000_000


def query_rows(cursor: Any, sql_text: str, *, params: "Sequence[Any] | None" = None,
               db_type: str = "sqlserver") -> list[dict[str, Any]]:
    """The first result set of ``sql_text`` as dicts keyed by column name - **every** row, each
    value as the driver returned it.

    The one way a module of ``common`` that holds a connection reads rows (rules R11). Six modules
    had each written it again around their own cursor - ``execute``, ``description``,
    ``fetchall`` - and drifted: one skipped a result set with no rows in front of the answer (an
    ``EXEC`` before the ``SELECT``) and five did not; one turned every value into text. Built on
    :func:`execute_capture`, so a batch is split and bound the way ``run-sql`` does it.

    Values are not made JSON-safe: a SID or a password hash is ``bytes`` and has to stay bytes to be
    written back as a ``0x...`` literal. A caller that wants text converts what it reads.
    """
    result_sets, _affected, _ = execute_capture(
        cursor, sql_text, max_rows=QUERY_ROWS_CEILING, db_type=db_type, params=params)
    if not result_sets:
        return []
    first = result_sets[0]
    if first["truncated"]:
        raise SqlRunError(f"the query answered more than {QUERY_ROWS_CEILING} rows; this reads a "
                          "catalogue, not a table - narrow it.")
    columns = first["columns"]
    return [dict(zip(columns, row)) for row in first["rows"]]


def query_value(cursor: Any, sql_text: str, *, params: "Sequence[Any] | None" = None,
                db_type: str = "sqlserver") -> Any:
    """The first column of the first row :func:`query_rows` reads, or ``None`` when there is none -
    a ``COUNT(*)``, an existence check. By position, not by name: an unnamed column is ``""`` on
    one driver and ``COUNT(*)`` on another."""
    rows = query_rows(cursor, sql_text, params=params, db_type=db_type)
    return next(iter(rows[0].values()), None) if rows else None


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
