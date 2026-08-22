from __future__ import annotations

import datetime as _datetime
import json
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Re-exported: five modules already import load_json_file from here, and the function is
# not SQL-specific. It now lives in common/json_io.py as the tool's single JSON reader.
from db_ops.lib.json_io import load_json_file  # noqa: F401 - re-exported for compatibility
# Re-exported: the SQL text vocabulary — limits, the DECLARE prelude, parameter types and the
# password lookup — moved to db_ops/lib/sql_text.py so apps can use it without importing `common`.
# Executing the SQL stayed here, because that is an operation.
from db_ops.lib.sql_text import (  # noqa: F401 - re-exported for compatibility
    DEFAULT_CONNECT_TIMEOUT_SECONDS, MAX_RESULT_ROWS, SQL_PARAMETER_TYPES, SqlParameterError, build_parameter_prelude,
    resolve_password)



# SQL Server-specific ODBC type codes pyodbc cannot decode on its own. Reading a
# ``datetimeoffset`` column without a converter fails the whole query with
# "ODBC SQL type -155 is not yet supported. column-index=N type=-155" (SQLSTATE HY106),
# which is how every Query Store / sys.dm_* view with a datetimeoffset column used to blow up.
SQL_SS_TIMESTAMPOFFSET = -155


def connect_sqlserver(
    pyodbc_module: Any,
    *,
    server: str,
    database: str,
    username: str,
    password: str,
    timeout: int,
    driver: str = "",
) -> Any:
    """Open an ODBC connection, walking the candidate drivers until one answers.

    Kept returning a bare connection because that is what callers and tests have always taken
    from it; :func:`open_sqlserver_odbc` is the same walk with the *evidence* — which candidate
    won and what the losers said — for the one caller that reports it.
    """
    return open_sqlserver_odbc(
        pyodbc_module, server=server, database=database, username=username,
        password=password, timeout=timeout, driver=driver,
    ).conn


@dataclass
class OdbcAttempt:
    """One candidate that was tried, and how it went. The audit trail behind a driver choice."""

    driver: str
    encryption: str
    ok: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        item = {"driver": self.driver, "encryption": self.encryption, "ok": self.ok}
        if self.error:
            item["error"] = self.error[:300]
        return item


@dataclass
class OdbcConnection:
    """An open ODBC connection plus which candidate opened it and what came before."""

    conn: Any
    driver: str
    encryption: str
    attempts: list[OdbcAttempt]


def open_sqlserver_odbc(
    pyodbc_module: Any,
    *,
    server: str,
    database: str,
    username: str,
    password: str,
    timeout: int,
    driver: str = "",
) -> OdbcConnection:
    """The candidate walk, with the result of every attempt kept.

    **Why the attempts are kept.** Until 2026-08-19 this walk reported itself as the single word
    ``odbc``: which of five drivers answered, at which encryption setting, and whether it had to
    fall back at all were invisible from the outside. That is the information needed to decide
    whether the *order* is right — and the order was being decided by a TLS error string
    (:func:`_is_sqlserver_tls_error`) rather than by knowing the target is a 2008 R2 instance.
    Reporting first, re-ordering second, is the only way to change that under fourteen live
    instances and be able to say what changed.
    """
    attempts: list[OdbcAttempt] = []
    errors: list[str] = []
    candidates = sqlserver_driver_candidates(pyodbc_module, preferred_driver=driver)
    for candidate_driver, encryption_mode in candidates:
        conn_str = build_sqlserver_conn_str(
            driver=candidate_driver,
            encryption_mode=encryption_mode,
            server=server,
            database=database,
            username=username,
            password=password,
        )
        try:
            conn = pyodbc_module.connect(conn_str, timeout=timeout)
            register_output_converters(conn)
            attempts.append(OdbcAttempt(candidate_driver, encryption_mode, True))
            return OdbcConnection(conn=conn, driver=candidate_driver,
                                  encryption=encryption_mode, attempts=attempts)
        except Exception as exc:  # noqa: BLE001 - retry legacy driver for TLS/cert compatibility.
            error_text = str(exc)
            mode_text = f" {encryption_mode}" if encryption_mode else ""
            errors.append(f"{candidate_driver}{mode_text}: {error_text}")
            attempts.append(OdbcAttempt(candidate_driver, encryption_mode, False, error_text))
            if not _is_sqlserver_tls_error(error_text):
                raise
    raise RuntimeError("SQL Server connect failed after driver fallback:\n- " + "\n- ".join(errors))


def build_sqlserver_conn_str(
    *,
    driver: str,
    encryption_mode: str,
    server: str,
    database: str,
    username: str,
    password: str,
) -> str:
    conn_str = (
        f"DRIVER={odbc_value(driver)};"
        f"SERVER={odbc_value(server)};"
        f"DATABASE={odbc_value(database)};"
        f"UID={odbc_value(username)};"
        f"PWD={odbc_value(password)};"
    )
    if driver in {"ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"}:
        conn_str += f"Encrypt={encryption_mode};TrustServerCertificate=yes;"
    return conn_str


@dataclass
class SqlServerConnection:
    """An open SQL Server connection plus *how* it was opened.

    Quacks like a DB-API connection (``cursor``/``commit``/``rollback``/``close``) so a caller
    that does not care which driver answered writes one code path; ``conn`` is the raw driver
    connection for the cases that do, and ``odbc_error`` keeps the ODBC failure that forced a
    pymssql fallback visible for troubleshooting.
    """

    conn: Any
    driver: str
    odbc_error: Exception | None = None
    #: The ``Encrypt`` value the winning candidate used, and every candidate tried before it.
    #: Reported by ``run-sql`` since 2026-08-19: "odbc" alone could not tell a first-attempt
    #: success from a TLS fallback, which is the difference this driver order was changed on.
    encryption: str = ""
    attempts: list[OdbcAttempt] = field(default_factory=list)

    @property
    def is_pymssql(self) -> bool:
        return self.driver == "pymssql"

    def describe_tool(self) -> dict[str, Any]:
        """What actually opened this connection, as JSON — for the caller's answer."""
        return {
            "driver": self.driver,
            "encryption": self.encryption,
            "fell_back": bool(self.odbc_error),
            "attempts": [item.to_dict() for item in self.attempts],
        }

    def cursor(self) -> Any:
        """A cursor with pyodbc's multi-result-set semantics on either driver."""
        cursor = self.conn.cursor()
        return PymssqlCursorAdapter(cursor) if self.is_pymssql else cursor

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()


class PymssqlCursorAdapter:
    """Give a pymssql cursor the attributes result-set walking relies on.

    pymssql updates ``description``/``rowcount`` differently from pyodbc, so code that steps
    through result sets (``execute_cursor_batches``, ``sql_run.execute_capture_first``) needs
    them refreshed on ``execute``. Without this a pymssql fallback silently returns nothing.
    """

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.description = None
        self.rowcount = -1

    def execute(self, sql_text: str, parameters: Any = None) -> None:
        """Run one statement, with bound parameters when the caller passes them.

        ``parameters`` was missing until 2026-08-13 and the signature simply refused them —
        `execute() takes 2 positional arguments but 3 were given`, raised from inside a table
        load on the worker. The adapter promises a DB-API cursor and DB-API `execute` takes
        parameters; the same class already learned this lesson once for `fetchall`, and the
        note below says why the promise has to be kept rather than partly kept.

        pymssql interpolates client-side and its paramstyle is ``%s``, **not** ``?``. A caller
        must therefore ask which style to write — see
        :func:`db_ops.common.db_connect.parameter_style`, which is why it takes the connection.
        """
        if parameters is None:
            self._cursor.execute(sql_text)
        else:
            self._cursor.execute(sql_text, tuple(parameters))
        self.description = self._cursor.description
        self.rowcount = int(getattr(self._cursor, "rowcount", -1) or -1)

    def executemany(self, sql_text: str, seq_of_parameters: Any) -> None:
        """Run one statement once per parameter row.

        Absent entirely until 2026-08-13, so a batch insert reached the adapter and died on
        `execute` instead — the caller had no way to tell that this cursor could not do it.
        """
        rows = [tuple(item) for item in seq_of_parameters]
        if not rows:
            return
        self._cursor.executemany(sql_text, rows)
        self.description = self._cursor.description
        self.rowcount = int(getattr(self._cursor, "rowcount", -1) or -1)

    def fetchmany(self, size: int) -> list[Any]:
        return list(self._cursor.fetchmany(size))

    # fetchall/fetchone are forwarded because the adapter is what callers get, not the pymssql
    # cursor: a caller that reads rows any other way than fetchmany died with
    # "'PymssqlCursorAdapter' object has no attribute 'fetchall'" - on the one target whose ODBC
    # stack could not negotiate TLS, so the failure only ever appeared on the fallback path and
    # only for the code that happened to run there. The adapter promises a DB-API cursor; these
    # are part of that promise.
    def fetchall(self) -> list[Any]:
        return list(self._cursor.fetchall())

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def nextset(self) -> Any:
        return self._cursor.nextset()


def connect_sqlserver_with_fallback(
    *,
    host: str,
    port: int = 1433,
    database: str = "master",
    username: str,
    password: str,
    driver: str = "",
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    command_timeout: int | None = None,
    autocommit: bool = False,
) -> SqlServerConnection:
    """Open a SQL Server connection over ODBC, falling back to pymssql.

    **The one place db_ops reaches a SQL Server instance.** The rule it encodes: use the
    target's configured ``sqlserver_driver`` when it names one (``pymssql`` goes straight to
    pymssql, and a pinned ODBC driver that fails is an error, not a reason to try something
    else); with nothing configured, try ODBC and fall back to pymssql, which reaches the older
    servers local ODBC cannot negotiate TLS with.

    ``connect_timeout`` bounds opening the session; ``command_timeout`` (default: the connect
    timeout) bounds the statements that follow — different questions, so they never share a
    number. ``autocommit=True`` suits read-only work: a caught per-database error inside a
    cursor cannot doom an ambient transaction and make every later statement fail with 3930.
    """
    command_timeout = connect_timeout if command_timeout is None else command_timeout
    driver = str(driver or "").strip()
    if driver.lower() == "pymssql":
        return _connect_pymssql(
            host=host, port=port, database=database, username=username, password=password,
            connect_timeout=connect_timeout, command_timeout=command_timeout,
            autocommit=autocommit,
            odbc_error=RuntimeError("Configured sqlserver_driver=pymssql."),
        )

    try:
        import pyodbc  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "pyodbc is required to connect to SQL Server. Install it with: "
            "pip install 'db_ops[mssql]' (and a system ODBC driver)"
        ) from exc
    try:
        opened = open_sqlserver_odbc(
            pyodbc,
            server=f"{host},{port}",
            database=database,
            username=username,
            password=password,
            timeout=connect_timeout,
            driver=driver,
        )
    except Exception as odbc_exc:  # noqa: BLE001 - pymssql reaches servers ODBC cannot TLS with.
        if driver:
            raise
        return _connect_pymssql(
            host=host, port=port, database=database, username=username, password=password,
            connect_timeout=connect_timeout, command_timeout=command_timeout,
            autocommit=autocommit, odbc_error=odbc_exc,
        )

    conn = opened.conn
    try:
        conn.timeout = command_timeout
    except Exception:  # noqa: BLE001 - driver without a settable timeout; harmless.
        pass
    if autocommit:
        try:
            conn.autocommit = True
        except Exception:  # noqa: BLE001 - driver without autocommit attr; harmless.
            pass
    # The driver *name* rather than the word "odbc": which of five candidates answered is the
    # thing a caller could not see, and the thing the version-ordering change has to be judged on.
    return SqlServerConnection(conn=conn, driver=opened.driver, encryption=opened.encryption,
                               attempts=opened.attempts)


def _connect_pymssql(
    *,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    connect_timeout: int,
    command_timeout: int,
    autocommit: bool,
    odbc_error: Exception,
) -> SqlServerConnection:
    try:
        import pymssql  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            f"SQL Server ODBC connect failed and pymssql is not installed. ODBC error: {odbc_error}"
        ) from exc
    try:
        conn = pymssql.connect(
            server=host,
            port=port,
            user=username,
            password=password,
            database=database,
            login_timeout=connect_timeout,
            timeout=command_timeout,
            autocommit=autocommit,
        )
    except Exception as exc:  # noqa: BLE001 - keep both driver errors visible for troubleshooting.
        raise RuntimeError(
            f"SQL Server pymssql connect failed: {exc}. ODBC error: {odbc_error}"
        ) from exc
    return SqlServerConnection(conn=conn, driver="pymssql", odbc_error=odbc_error)


def register_output_converters(conn: Any) -> Any:
    """Teach a pyodbc connection the SQL Server types it cannot decode by itself.

    Only ``datetimeoffset`` (``SQL_SS_TIMESTAMPOFFSET``) needs this today: pyodbc raises
    ``HY106 ODBC SQL type -155 is not yet supported`` instead of returning a value, so a single
    such column kills an otherwise valid SELECT. Registering the converter here — on the one
    connect helper every app goes through — means no caller has to know the type code.

    Safe on any connection object: a driver without ``add_output_converter`` (pymssql, a fake in
    tests) is left untouched. Returns ``conn`` so callers can chain.
    """
    add_converter = getattr(conn, "add_output_converter", None)
    if callable(add_converter):
        add_converter(SQL_SS_TIMESTAMPOFFSET, decode_timestampoffset)
    return conn


def decode_timestampoffset(raw: Any) -> Any:
    """Decode the 20-byte ``SQL_SS_TIMESTAMPOFFSET`` buffer into an aware ``datetime``.

    Layout (little-endian): year, month, day, hour, minute, second as int16, nanoseconds as
    uint32, then the offset's hour and minute as int16. Python datetimes carry microseconds, so
    the nanosecond field is truncated. A buffer that is not the expected length is returned as
    text rather than raising — a readable cell beats failing the whole export.
    """
    if raw is None:
        return None
    data = bytes(raw)
    if len(data) != 20:
        return data.decode("utf-16-le", errors="replace") if len(data) % 2 == 0 else data.hex()
    year, month, day, hour, minute, second, nanoseconds, tz_hour, tz_minute = struct.unpack(
        "<6hI2h", data
    )
    return _datetime.datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        nanoseconds // 1000,
        _datetime.timezone(_datetime.timedelta(hours=tz_hour, minutes=tz_minute)),
    )











def execute_cursor_batches(
    conn: Any, cursor: Any, batches: list[str], *, commit: bool,
    max_rows: int = MAX_RESULT_ROWS,
    prelude: str = "", params: "list[Any] | None" = None,
) -> dict[str, Any]:
    """Run ``batches`` and capture up to ``max_rows`` rows of each result set.

    ``truncated`` in the returned dict (and per result set) says whether the cap actually cut
    anything, which is the difference between "this instance has 100 mis-placed files" and "this
    instance has at least 100". It is detected by fetching one row past the cap and discarding it.

    The default is the 100-row **preview** cap: what a scheduled task stores in
    ``sql_runs.result_json`` and pastes into a Telegram message, where more would bloat the
    store and blow past the 4096-char message limit. A caller that is *exporting* the rows
    (a target with ``output.format = "xlsx"``) has to raise it, or the workbook silently
    contains the first 100 rows of a 5000-row answer and looks complete.
    """
    result_sets = []
    total_row_count = 0
    truncated = False
    for batch in batches:
        # The prelude re-declares the script's parameters in front of every batch, because a T-SQL
        # variable does not survive a GO, and the same values are bound again with it.
        if params:
            cursor.execute(prelude + batch, *params)
        else:
            cursor.execute(prelude + batch if prelude else batch)
        while True:
            columns = [col[0] for col in cursor.description] if cursor.description else []
            if columns:
                rows = cursor.fetchmany(max_rows)
                # Truncation used to be invisible: a result set cut at the cap looked exactly like
                # a complete one, so nobody could tell that STORAGE_FILE_PLACEMENT reporting
                # "100 files" meant "the first 100 of an unknown number". The only way to know is
                # to ask for one more row than we keep.
                was_truncated = False
                if len(rows) == max_rows:
                    was_truncated = bool(cursor.fetchmany(1))
                    truncated = truncated or was_truncated
                result_sets.append({
                    "columns": columns,
                    "rows": [make_json_safe(list(row)) for row in rows],
                    "truncated": was_truncated,
                })
                total_row_count += len(rows)
            elif cursor.rowcount and cursor.rowcount > 0:
                total_row_count += int(cursor.rowcount)

            nextset = getattr(cursor, "nextset", None)
            if not callable(nextset) or not nextset():
                break
    if commit:
        conn.commit()
    return {"row_count": total_row_count, "result_sets": result_sets[:5], "truncated": truncated}


def make_json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [make_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}
    return str(value)


def choose_sqlserver_driver(pyodbc_module: Any) -> str:
    candidates = sqlserver_driver_candidates(pyodbc_module)
    if candidates:
        return candidates[0][0]
    raise RuntimeError("No ODBC driver found for SQL Server.")


def sqlserver_driver_candidates(pyodbc_module: Any, preferred_driver: str = "") -> list[tuple[str, str]]:
    """The (driver, Encrypt) pairs to try, in order.

    **A version-aware re-ordering was proposed, measured, and rejected on 2026-08-19.** The
    proposal was to put Driver 17 / ``Encrypt=no`` first for SQL Server 2008 R2 and older, on the
    premise that 10.50 offers TLS 1.0 only and Driver 18 refuses it — making the ODBC-18 attempt a
    wasted handshake on those instances. The premise is false on this estate. All four 10.50
    instances (`ACME-192-0-2-245`, `ACME-192-0-2-253`, `ACME-192-0-2-8`, `ACME-192-0-2-41`)
    complete on **Driver 18 with ``Encrypt=optional``, first attempt, no fallback** — measured
    through the ``tool.actual`` report added the same day.

    So the change would have bought nothing and cost something real: ``optional`` encrypts whenever
    the server offers a certificate, and ``Encrypt=no`` is plaintext. Re-ordering by version would
    have quietly downgraded four production instances. The order below stays as it is because the
    evidence says it is already right, not because nobody looked.
    """
    drivers = list(pyodbc_module.drivers())
    preferred_driver = preferred_driver.strip()
    if preferred_driver:
        if preferred_driver not in drivers:
            raise RuntimeError(f"Configured SQL Server ODBC driver is not installed: {preferred_driver}")
        if preferred_driver == "ODBC Driver 18 for SQL Server":
            # Prefer encrypted transport ("optional" = encrypt when the server offers
            # a cert, proceed if not) over plaintext ("no"), so credentials/data are
            # encrypted whenever the server supports it and only fall back to cleartext.
            # NOTE: "optional" is an ODBC Driver 18 value only — Driver 17 rejects it
            # ("Invalid value specified for connection string attribute 'Encrypt'").
            return [(preferred_driver, "optional"), (preferred_driver, "no")]
        if preferred_driver == "ODBC Driver 17 for SQL Server":
            # Driver 17 accepts only yes/no for Encrypt (not "optional"), and it is the
            # driver reserved for legacy (e.g. SQL 2008 R2, TLS 1.0-only) hosts that
            # cannot be forced to encrypt. Keep the known-good plaintext value.
            return [(preferred_driver, "no")]
        return [(preferred_driver, "")]

    preferred: list[tuple[str, tuple[str, ...]]] = [
        ("ODBC Driver 18 for SQL Server", ("optional", "no")),
        ("ODBC Driver 17 for SQL Server", ("no",)),
        ("SQL Server Native Client 11.0", ("",)),
        ("SQL Server Native Client 10.0", ("",)),
        ("SQL Server", ("",)),
    ]
    candidates: list[tuple[str, str]] = []
    for driver, encryption_modes in preferred:
        if driver in drivers:
            candidates.extend((driver, mode) for mode in encryption_modes)
    if not candidates and drivers:
        candidates.append((drivers[-1], ""))
    return candidates


def _is_sqlserver_tls_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return any(
        pattern in lowered
        for pattern in (
            "certificate chain",
            "encryption not supported",
            "ssl provider",
            "ssl security error",
            "security package",
        )
    )


def odbc_value(value: Any) -> str:
    text = str(value).replace("}", "}}")
    return "{" + text + "}"


def split_sql_batches(sql_text: str) -> list[str]:
    batches: list[str] = []
    current: list[str] = []
    for line in sql_text.splitlines():
        if line.strip().upper() == "GO":
            batch = "\n".join(current).strip()
            if batch:
                batches.append(batch)
            current = []
            continue
        current.append(line)
    batch = "\n".join(current).strip()
    if batch:
        batches.append(batch)
    return batches


def load_secret_text(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = load_json_file(path)
    return {str(key): str(value) for key, value in data.items()}




def load_credentials_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = load_json_file(path)
    return list(data.get("database_credentials", []))


def load_remote_credentials_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = load_json_file(path)
    if isinstance(data.get("remote_credentials"), list):
        return list(data.get("remote_credentials", []))
    legacy = data.get("remote_users")
    if not isinstance(legacy, list):
        return []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in legacy:
        if not isinstance(item, dict):
            continue
        host = str(item.get("server_ip") or item.get("host") or "").strip()
        if not host:
            continue
        server_id = str(item.get("server_id") or f"{str(item.get('company_code') or 'REMOTE')}-{host.replace('.', '-')}").strip()
        key = (server_id, host)
        group = groups.setdefault(
            key,
            {
                "server_id": server_id,
                "host": host,
                "credentials": [],
            },
        )
        credential_name = str(item.get("credential_name") or item.get("name") or "").strip()
        if not credential_name:
            continue
        group["credentials"].append(
            {
                "credential_name": credential_name,
                "username": str(item.get("username") or item.get("login_name") or ""),
                "password_ref": str(item.get("password_ref") or item.get("authentication_info_ref") or ""),
                "role": str(item.get("role") or "REMOTE"),
                "notes": str(item.get("note") or ""),
            }
        )
    return list(groups.values())


def load_database_inventory(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file) or {}
        if not isinstance(data, dict):
            return []
        return list(data.get("servers", []))

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return load_database_inventory_without_yaml(path)

    with path.open("r", encoding="utf-8-sig") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        return []
    return list(data.get("servers", []))


def load_database_inventory_without_yaml(path: Path) -> list[dict[str, Any]]:
    servers: list[dict[str, Any]] = []
    current_server: dict[str, Any] | None = None
    current_database: dict[str, Any] | None = None
    current_list: list[Any] | None = None
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if indent == 2 and line.startswith("- server_id:"):
            current_server = {"server_id": parse_yaml_scalar(line.split(":", 1)[1]), "databases": []}
            current_database = None
            current_list = None
            servers.append(current_server)
            continue
        if current_server is None:
            continue
        if indent == 4 and not line.startswith("- ") and ":" in line:
            key, value = line.split(":", 1)
            current_list = None
            if value.strip():
                current_server[key.strip()] = parse_yaml_scalar(value)
            continue
        if indent == 6 and line.startswith("- db_type:"):
            current_database = {"db_type": parse_yaml_scalar(line.split(":", 1)[1])}
            current_server.setdefault("databases", []).append(current_database)
            current_list = None
            continue
        if current_database is None:
            continue
        if indent == 8 and not line.startswith("- ") and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            current_list = None
            if value.strip():
                current_database[key] = parse_yaml_scalar(value)
            else:
                current_database[key] = []
                current_list = current_database[key]
            continue
        if indent == 10 and line.startswith("- ") and current_list is not None:
            current_list.append(parse_yaml_scalar(line[2:]))
    return servers


def parse_yaml_scalar(value: str) -> str | int:
    text = value.strip().strip("'\"")
    if text == "<not-provided>":
        return ""
    try:
        return int(text)
    except ValueError:
        return text
