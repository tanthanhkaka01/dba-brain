"""Open a connection to one database, on any engine db_ops supports.

**Single source of truth for reaching a database**, the way ``remote_exec`` is for reaching a
VM. Before this module the engine-specific connect code lived inside the metrics app
(``metrics/executor.py``), so it could not be reused: ``common.sql_run`` — the engine behind
``/spbot_sql_to_xlsx`` and ``python -m db_ops.common.cli run-sql`` — knew only SQL Server, and
answering "run this SELECT on that PostgreSQL box" meant writing the connect again. Two
implementations of the same thing, disagreeing on which engines exist.

What lives here is the **connection** only. Running the SQL is already shared:
``sql_execution.execute_cursor_batches`` handles batches and result capture for every engine.

Each engine gets its timeout enforced *inside the server* as well as on the socket, because a
socket that stays healthy while a relation is locked is exactly the case a connect timeout does
not cover:

* SQL Server — ``connect_sqlserver_with_fallback`` (driver choice, ODBC→pymssql fallback);
* PostgreSQL — ``statement_timeout``;
* Oracle — ``call_timeout``;
* MySQL — ``read_timeout``.

Import the driver lazily, per engine: a node that only monitors SQL Server must not need
``oracledb`` installed to start.
"""

from __future__ import annotations
from db_ops.lib.sql_text import DEFAULT_CONNECT_TIMEOUT_SECONDS  # noqa: F401 - one definition

from typing import Any

from db_ops.common import sql_execution
from db_ops.lib.sql_access import normalize_db_type  # re-exported: rule in lib, connect here
from db_ops.lib.target_profile import (
    TargetProfile, ToolChoice, ToolSelectionError,
    select_oracle_client_mode, select_sqlserver_driver,
)

DEFAULT_STATEMENT_TIMEOUT_SECONDS = 300

# Engine -> (default port, default database when the caller names none).
_ENGINE_DEFAULTS = {
    "sqlserver": (1433, "master"),
    # Metric and ad-hoc SQL mostly reads cluster-wide catalogs, so any database will do.
    "postgresql": (5432, "postgres"),
    "mysql": (3306, "information_schema"),
    "oracle": (1521, None),
}

SUPPORTED_DB_TYPES = tuple(_ENGINE_DEFAULTS)


def default_database(db_type: str) -> str:
    """The database this engine connects to when the caller names none.

    Exposed so a caller can *report* what it connected to. `sql_run` echoes the database back in
    its result, and returning "" there told an operator nothing about where the SQL actually ran.
    """
    engine = normalize_db_type(db_type)
    if engine not in _ENGINE_DEFAULTS:
        return ""
    return _ENGINE_DEFAULTS[engine][1] or ""


class DbConnectError(RuntimeError):
    """A connect failed, or the engine/driver is not available — an operator message."""




def tool_for(
    profile: TargetProfile,
    *,
    requested_driver: str = "",
    sqlserver_driver: str = "",
    oracle_client_mode: str = "",
) -> ToolChoice:
    """Which driver this profile calls for, and who decided — without connecting to anything.

    Split out of :func:`connect_engine` so a caller can *report* the choice (``run-sql`` echoes it
    back as ``tool``) or validate a request before doing any work, and so the same answer cannot be
    derived twice with two opinions. Raises :class:`DbConnectError` when the facts rule every tool
    out — an 8i target with no bridge configured is the case this exists for.

    ``requested_driver`` and ``sqlserver_driver`` are kept apart rather than merged by the caller:
    they are the same string from two places, and collapsing them is what makes ``chosen_by`` say
    ``config`` for a driver the operator typed into the request.
    """
    engine = normalize_db_type(profile.db_type)
    try:
        if engine == "oracle":
            return select_oracle_client_mode(profile, oracle_client_mode)
        if engine == "sqlserver":
            return select_sqlserver_driver(
                profile, requested=requested_driver, configured=sqlserver_driver
            )
    except ToolSelectionError as exc:
        # A tool-selection refusal already carries the fix; wrapping it in the connect error type
        # keeps every caller's `except DbConnectError` working while the message survives intact.
        raise DbConnectError(str(exc)) from exc
    return ToolChoice(engine or "unknown", "default", f"the only driver for {engine or 'this engine'}")


def connect_engine(
    *,
    db_type: str,
    host: str,
    username: str,
    password: str,
    port: int | None = None,
    database: str | None = None,
    service_name: str = "",
    sqlserver_driver: str = "",
    oracle_client_mode: str = "",
    profile: TargetProfile | None = None,
    connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    statement_timeout_seconds: int | None = DEFAULT_STATEMENT_TIMEOUT_SECONDS,
    autocommit: bool = False,
) -> Any:
    """A DB-API connection to ``host``, for whichever engine ``db_type`` names.

    The caller owns the connection and must close it. Autocommit defaults to **off**, so a
    caller that does not commit changes nothing — the contract :mod:`db_ops.common.sql_run`
    depends on.

    ``autocommit=True`` is for read-only collection: metric SQL catches per-database errors
    inside a cursor (a ``USE`` into an unreachable AG secondary), and inside a transaction one
    such error dooms the ambient transaction, so every later statement fails with error 3930
    ("current transaction cannot be committed"). Autocommit keeps each statement independent.

    Raises :class:`DbConnectError` for an unknown engine, a missing driver, or a failed connect,
    with the engine named in the message: "connect failed" without it sends an operator to the
    wrong host.

    ``profile`` is what makes the choice version-aware (:mod:`db_ops.lib.target_profile`). It is
    optional and stays optional: with none, or with one that states no ``major_version``, every
    engine behaves exactly as it did before 2026-08-19. What it buys is the refusal that names its
    own fix — Oracle below 12.1 cannot be reached by python-oracledb in thin mode, and saying so
    here turns ``DPY-3010`` into "route this through the bridge or install a thick client".
    """
    engine = normalize_db_type(db_type)
    if engine not in _ENGINE_DEFAULTS:
        raise DbConnectError(
            f"Unsupported db_type {db_type!r}; expected one of {list(SUPPORTED_DB_TYPES)}."
        )
    # The engine argument is authoritative; a profile built for another target must not redirect
    # the connect. Everything else it carries (the version) is additive.
    resolved_profile = (profile or TargetProfile()).with_(db_type=engine)
    tool = tool_for(
        resolved_profile, sqlserver_driver=sqlserver_driver, oracle_client_mode=oracle_client_mode
    )
    if engine == "oracle" and tool.tool == "thick":
        _init_oracle_thick_client()
    default_port, default_database = _ENGINE_DEFAULTS[engine]
    resolved_port = int(port or default_port)
    resolved_database = str(database or default_database or "")
    # A connect timeout longer than the statement timeout is meaningless — the caller asked for
    # the whole operation to be bounded by the smaller number.
    connect_timeout = int(connect_timeout_seconds)
    if statement_timeout_seconds:
        connect_timeout = min(connect_timeout, int(statement_timeout_seconds))

    handler = {
        "sqlserver": _connect_sqlserver,
        "postgresql": _connect_postgresql,
        "mysql": _connect_mysql,
        "oracle": _connect_oracle,
    }[engine]
    try:
        return handler(
            host=host, port=resolved_port, database=resolved_database,
            username=str(username), password=str(password), service_name=service_name,
            sqlserver_driver=sqlserver_driver, connect_timeout=connect_timeout,
            statement_timeout=statement_timeout_seconds, autocommit=autocommit,
        )
    except DbConnectError:
        raise
    except Exception as exc:  # noqa: BLE001 - a connect failure is a message, not a trace.
        raise DbConnectError(f"{engine} connect to {host}:{resolved_port} failed: {exc}") from exc


def _connect_sqlserver(*, host, port, database, username, password, sqlserver_driver,
                       connect_timeout, statement_timeout, autocommit, **_ignored) -> Any:
    return sql_execution.connect_sqlserver_with_fallback(
        host=host, port=port, database=database or "master",
        username=username, password=password,
        driver=str(sqlserver_driver or "").strip(), connect_timeout=connect_timeout,
        command_timeout=statement_timeout, autocommit=autocommit,
    )


def _connect_postgresql(*, host, port, database, username, password, connect_timeout,
                        statement_timeout, autocommit=False, **_ignored) -> Any:
    try:
        from pg8000 import dbapi as pg8000_dbapi  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - driver install is environmental.
        raise DbConnectError(
            "pg8000 is required to reach PostgreSQL. Install it with: "
            "pip install 'db_ops[postgres]'"
        ) from exc

    conn = pg8000_dbapi.connect(
        host=host, port=port, database=database or "postgres",
        user=username, password=password, timeout=connect_timeout,
    )
    if statement_timeout:
        cursor = conn.cursor()
        try:
            # The value is inlined, not bound. pg8000's paramstyle is a **module-level global**
            # with no per-connection override, and the runtime store sets it to `qmark` for its
            # own placeholders. When both subsystems shared a process, whichever set it last
            # decided how the other's placeholders were read, and every PostgreSQL query failed
            # with `syntax error at or near "%"`. An int() we computed cannot be an injection.
            cursor.execute(
                f"SELECT set_config('statement_timeout', '{int(statement_timeout) * 1000}', false)"
            )
        finally:
            cursor.close()
    if autocommit:
        conn.autocommit = True
    return conn


def _connect_mysql(*, host, port, database, username, password, connect_timeout,
                   statement_timeout, autocommit=False, **_ignored) -> Any:
    read_timeout = int(statement_timeout or connect_timeout)
    try:
        import pymysql  # type: ignore[import-not-found]
    except ImportError:
        try:
            import mysql.connector  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DbConnectError(
                "pymysql (or mysql-connector-python) is required to reach MySQL. "
                "Install it with: pip install 'db_ops[mysql]'"
            ) from exc
        return mysql.connector.connect(
            host=host, port=port, user=username, password=password,
            database=database or "information_schema",
            connection_timeout=connect_timeout, read_timeout=read_timeout,
            autocommit=autocommit,
        )
    return pymysql.connect(
        host=host, port=port, user=username, password=password,
        database=database or "information_schema",
        connect_timeout=connect_timeout, read_timeout=read_timeout,
        cursorclass=pymysql.cursors.Cursor, autocommit=autocommit,
    )


def _init_oracle_thick_client() -> None:
    """Switch python-oracledb into thick mode, once per process.

    Thick mode needs an Oracle client library on the machine and is the only way this driver
    reaches a pre-12.1 server at all. ``init_oracle_client`` is process-global and raises when
    called twice, so a second call is swallowed — a caller asking for thick twice is asking for a
    state that already holds, not for an error.
    """
    try:
        import oracledb  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - driver install is environmental.
        raise DbConnectError(
            "oracledb is required for oracle_client_mode='thick'. Install it with: "
            "pip install 'db_ops[oracle]'"
        ) from exc
    init = getattr(oracledb, "init_oracle_client", None)
    if init is None:  # pragma: no cover - cx_Oracle is thick by construction.
        return
    try:
        init()
    except Exception as exc:  # noqa: BLE001 - already-initialized is success, anything else is not.
        if "already" not in str(exc).lower():
            raise DbConnectError(
                "oracle_client_mode='thick' needs an Oracle client library on this machine and "
                f"none could be loaded: {exc}"
            ) from exc


def _connect_oracle(*, host, port, database, username, password, service_name,
                    statement_timeout, autocommit=False, **_ignored) -> Any:
    try:
        import oracledb  # type: ignore[import-untyped]
    except ImportError:
        try:
            import cx_Oracle as oracledb  # type: ignore[import-not-found,no-redef]
        except ImportError as exc:
            raise DbConnectError(
                "oracledb (or cx_Oracle) is required to reach Oracle. Install it with: "
                "pip install 'db_ops[oracle]'"
            ) from exc

    # Oracle connects to a service, not a database name; callers that only have `database`
    # (the shape every other engine uses) get it treated as the service.
    resolved_service = str(service_name or database or "").strip()
    if not resolved_service:
        raise DbConnectError("Oracle needs a service_name (or database) to build its DSN.")
    conn = oracledb.connect(
        user=username, password=password,
        dsn=oracledb.makedsn(host, port, service_name=resolved_service),
    )
    if statement_timeout:
        milliseconds = int(statement_timeout) * 1000
        # oracledb exposes call_timeout; cx_Oracle spelled it callTimeout.
        if hasattr(conn, "call_timeout"):
            conn.call_timeout = milliseconds
        elif hasattr(conn, "callTimeout"):
            conn.callTimeout = milliseconds
    if autocommit and hasattr(conn, "autocommit"):
        conn.autocommit = True
    return conn


def server_version(connection: Any, db_type: str) -> str:
    """The version string the *server* reported, read off an already-open connection.

    **No round trip.** Every driver here learns the server version during the handshake and keeps
    it; asking for it afterwards costs nothing, which is the only reason this is acceptable on a
    path the metrics collector runs 3,700 times a pass.

    It exists to close the half of the version problem that config cannot: ``major_version`` in
    ``db_instances.json`` is a number somebody typed, and until 2026-08-19 nothing ever compared it
    with the instance. A blank answer is normal — pymssql does not expose one — and is reported as
    unknown rather than as a mismatch, because "the driver did not say" and "the config is wrong"
    must not look the same.
    """
    engine = normalize_db_type(db_type)
    target = getattr(connection, "conn", connection)
    try:
        if engine == "sqlserver":
            if getattr(connection, "is_pymssql", False):
                return ""
            import pyodbc  # type: ignore[import-untyped]

            return str(target.getinfo(pyodbc.SQL_DBMS_VER) or "")
        if engine == "oracle":
            return str(getattr(target, "version", "") or "")
        if engine == "mysql":
            getter = getattr(target, "get_server_info", None)
            return str(getter() or "") if callable(getter) else ""
        if engine == "postgresql":
            statuses = getattr(target, "parameter_statuses", None) or {}
            value = statuses.get(b"server_version") or statuses.get("server_version") or b""
            return value.decode() if isinstance(value, bytes) else str(value)
    except Exception:  # noqa: BLE001 - an unreadable version must never fail a working query.
        return ""
    return ""


def version_drift(configured: int | None, observed: str) -> str:
    """``""`` when they agree or cannot be compared, else a sentence naming both.

    Deliberately a *message* rather than a boolean: the only useful form of this finding is the one
    that says which two numbers disagree, and a caller that only wants the boolean can test the
    string.
    """
    if configured is None or not observed:
        return ""
    leading = observed.strip().split(".")[0]
    if not leading.isdigit():
        return ""
    if int(leading) == int(configured):
        return ""
    return (
        f"config says major_version={configured} but the server reports {observed} — "
        "one of them is wrong, and every version-gated choice is made from the config value"
    )


def parameter_style(db_type: str, connection: Any = None) -> str:
    """Which DB-API ``paramstyle`` to write for this connection.

    Lives here rather than next to the caller that binds values because it is a fact about the
    *driver*, and which driver answers is this module's decision — the guard in
    ``tests/test_app_database_drivers.py`` exists to stop that knowledge being copied a third
    time.

    **Pass the connection.** The style is a property of the driver that actually answered, and
    an engine does not decide that on its own:

    * **SQL Server has two.** pyodbc binds ``?``; the pymssql fallback binds ``%s``. Which one
      opened depends on whether an ODBC stack is installed — pyodbc on the Windows master,
      pymssql in the Linux worker container. Answering from ``db_type`` alone was right in
      development and wrong in production, which is exactly where it was found: a table load
      built ``?`` placeholders and pymssql rejected them (2026-08-13).
    * **PostgreSQL changes with the process.** pg8000 reads ``paramstyle`` from a **module
      global** at execute time, and ``db/backend.py`` sets it to ``qmark`` so the runtime store's
      ``?`` placeholders work. So it is ``?`` inside the daemon and ``%s`` in a bare CLI run.

    Without a connection the answer is the default driver's, which is a guess for SQL Server —
    fine for building a statement to show someone, not for one about to be executed.
    """
    engine = normalize_db_type(db_type)
    if engine == "sqlserver":
        # `is_pymssql` is set by sql_execution.SqlServerConnection when the ODBC attempt failed.
        return "format" if getattr(connection, "is_pymssql", False) else "qmark"
    if engine == "oracle":
        return "numeric"
    if engine == "mysql":
        return "format"
    if engine == "postgresql":
        try:
            from pg8000 import dbapi as pg8000_dbapi  # type: ignore[import-not-found]
        except ImportError:
            return "format"
        return str(getattr(pg8000_dbapi, "paramstyle", "format"))
    raise DbConnectError(
        f"No parameter style known for db_type {db_type!r}; expected one of "
        f"{list(SUPPORTED_DB_TYPES)}."
    )
