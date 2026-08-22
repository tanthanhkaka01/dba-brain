"""Backend adapter that lets the db_ops store classes run on SQLite or PostgreSQL.

The four store classes (``DbOpsStore``, ``MetricStore``, ``SlaStore``, ``BackupRestoreHistory``)
were written directly against ``sqlite3``: 3300-odd lines, 73 ``with self.connect()`` blocks and
335 ``row["column"]`` accesses. Rewriting them per backend would mean branching every one of those.

Instead this module presents *one* API - the subset of the ``sqlite3`` connection/cursor/row
interface the store code actually uses - with two implementations behind it:

* **SQLite** returns a real :class:`sqlite3.Connection`, unchanged. The default path is therefore
  byte-for-byte what it has always been: no wrapper, no translation, no new failure mode.
* **PostgreSQL** returns :class:`PostgresConnection`, which mimics that same surface on top of
  pg8000 and translates the handful of statements where the two dialects genuinely differ.

Three findings kept the translation layer small:

1. pg8000 accepts ``qmark`` paramstyle, so all ~128 ``?`` placeholders work untouched - and a
   literal ``%`` in SQL (``LIKE '%x%'``) needs no escaping, which the default ``format`` paramstyle
   would have required everywhere.
2. Both engines implement ``INSERT ... ON CONFLICT ... DO UPDATE``, so the store's upserts port
   as-is.
3. Almost every ``strftime`` in the codebase is Python's ``datetime.strftime``. In SQL it appears
   only in column DEFAULTs plus one UPDATE, so the timestamp rewrite has a small, known surface.

What is left to translate is listed in :func:`translate_statement`.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterator, Sequence

from db_ops.config import POSTGRESQL_BACKEND, SQLITE_BACKEND, StoreConfig

# SQLite's UTC-now expression and the PostgreSQL expression that renders the identical string.
# The store writes timestamps as ISO-8601 UTC text (``utc_now_text()``), so both engines must
# produce exactly that format or rows written by a DEFAULT stop matching rows written by Python.
SQLITE_UTC_NOW_PATTERN = re.compile(
    r"strftime\(\s*'%Y-%m-%dT%H:%M:%SZ'\s*,\s*'now'\s*\)", re.IGNORECASE
)
POSTGRES_UTC_NOW = """to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')"""


def quote_identifier(name: str) -> str:
    """Double-quote an identifier read from the catalog."""
    return '"' + str(name).replace('"', '""') + '"'


class StoreBackendError(RuntimeError):
    """The store backend could not be opened or a statement could not be translated."""


# SQLite JSON functions the store's schema and queries depend on, reimplemented for PostgreSQL.
#
# Providing them as functions rather than rewriting the SQL is deliberate: ``json_valid`` appears in
# 12 CHECK constraints and ``json_extract`` in two queries, and a regex that rewrote those would have
# to understand JSON path syntax to stay correct. As functions, the store's SQL text is valid on both
# engines untouched, and the CHECK constraints keep their exact SQLite semantics.
#
# Both are declared IMMUTABLE because PostgreSQL will not accept a non-immutable function inside a
# CHECK constraint, and both are genuinely deterministic. They return NULL/false rather than raising,
# matching SQLite - a CHECK that raised instead of failing would turn a rejected row into an aborted
# transaction.
#
# They are created in the store's own schema (search_path is set to it), so nothing lands in public.
POSTGRES_COMPAT_DDL = (
    """
    CREATE OR REPLACE FUNCTION json_valid(candidate text) RETURNS boolean
    LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $json_valid$
    BEGIN
        -- SQLite's json_valid(NULL) is NULL, and a NULL CHECK passes. Preserve that so a nullable
        -- JSON column stays nullable.
        IF candidate IS NULL THEN
            RETURN NULL;
        END IF;
        PERFORM candidate::jsonb;
        RETURN true;
    EXCEPTION WHEN others THEN
        RETURN false;
    END
    $json_valid$
    """,
    """
    CREATE OR REPLACE FUNCTION json_extract(candidate text, path text) RETURNS text
    LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $json_extract$
    DECLARE
        keys text[];
    BEGIN
        IF candidate IS NULL OR path IS NULL OR left(path, 2) <> '$.' THEN
            RETURN NULL;
        END IF;
        keys := string_to_array(substr(path, 3), '.');
        RETURN candidate::jsonb #>> keys;
    EXCEPTION WHEN others THEN
        RETURN NULL;
    END
    $json_extract$
    """,
)


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #
class Row(Mapping):
    """Dict-and-tuple hybrid matching the parts of :class:`sqlite3.Row` the store code uses.

    ``sqlite3.Row`` supports ``row["col"]``, ``row[0]``, ``row.keys()``, ``"col" in row`` and
    ``dict(row)``. The store relies on all of those (335 key accesses alone), so pg8000's plain
    tuples are wrapped rather than the call sites changed.

    Note that ``in`` here tests **column names**, matching ``sqlite3.Row`` - several store methods
    do ``"error_text" in row`` to probe for a column. A Mapping gives that for free; a tuple would
    have silently tested values instead.
    """

    __slots__ = ("_columns", "_values", "_lookup")

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._lookup = {name: position for position, name in enumerate(self._columns)}

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        try:
            return self._values[self._lookup[key]]
        except KeyError:
            raise IndexError(f"No such column: {key}") from None

    def get(self, key: str | int, default: Any = None) -> Any:
        """Mapping.get, implemented explicitly.

        ``__getitem__`` raises ``IndexError`` for an unknown column because that is what
        ``sqlite3.Row`` does, but ``Mapping.get`` only catches ``KeyError`` - so the inherited
        version let the IndexError escape instead of returning the default. Both contracts have to
        hold at once: IndexError from subscripting, a default from ``get``.
        """
        if isinstance(key, int):
            return self._values[key] if -len(self._values) <= key < len(self._values) else default
        position = self._lookup.get(key)
        return default if position is None else self._values[position]

    def keys(self) -> list[str]:  # type: ignore[override] - sqlite3.Row returns a list
        return list(self._columns)

    def __iter__(self) -> Iterator[str]:
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)

    def __contains__(self, key: object) -> bool:
        return key in self._lookup

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._columns, self._values))!r})"


# --------------------------------------------------------------------------- #
# SQL translation (PostgreSQL only)
# --------------------------------------------------------------------------- #
_PRAGMA = re.compile(r"^\s*PRAGMA\b", re.IGNORECASE)
# PRAGMA table_info is not tuning - it is how the store's additive migrations ask "does this column
# exist yet?", at six call sites across sqlite_store/metrics/sla. Translating the PRAGMA into the
# equivalent catalog query means all six keep working untouched; skipping it like the other pragmas
# would report every column as missing and make each migration re-ALTER a column that already exists.
# Only the `name` column is projected because that is all those six sites read.
_PRAGMA_TABLE_INFO = re.compile(
    r"^\s*PRAGMA\s+table_info\s*\(\s*[\"']?(?P<table>[A-Za-z_][A-Za-z0-9_]*)[\"']?\s*\)\s*;?\s*$",
    re.IGNORECASE,
)
_TYPE_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    # An autoincrement primary key becomes an identity column. BY DEFAULT (not ALWAYS) so an
    # explicit id can still be inserted - the migration tool copies original ids in.
    (re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.IGNORECASE),
     "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"),
    (re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\b", re.IGNORECASE), "BIGINT PRIMARY KEY"),
    (re.compile(r"\bINTEGER\b", re.IGNORECASE), "BIGINT"),
    (re.compile(r"\bREAL\b", re.IGNORECASE), "DOUBLE PRECISION"),
    (re.compile(r"\bBLOB\b", re.IGNORECASE), "BYTEA"),
    # SQLite tolerates AUTOINCREMENT elsewhere; PostgreSQL has no such keyword.
    (re.compile(r"\s+AUTOINCREMENT\b", re.IGNORECASE), ""),
)
_DDL = re.compile(r"^\s*(CREATE|ALTER|DROP)\b", re.IGNORECASE)


def translate_statement(sql: str) -> str | None:
    """Translate one SQLite statement for PostgreSQL, or return ``None`` to skip it.

    The differences that actually occur in this codebase:

    * ``PRAGMA`` - SQLite tuning (journal_mode, synchronous, foreign_keys, busy_timeout) with no
      PostgreSQL equivalent. Skipped rather than errored, because the store issues them on every
      single connection.
    * ``strftime('%Y-%m-%dT%H:%M:%SZ','now')`` - rewritten to the ``to_char(now() ...)`` form that
      emits the same string, in DEFAULT clauses and in the one UPDATE that uses it.
    * Type names, in DDL only - ``INTEGER``/``REAL``/``BLOB`` and
      ``INTEGER PRIMARY KEY AUTOINCREMENT``. Restricted to DDL so a query that merely mentions a
      word like "integer" in a string literal is left alone.
    * ``IF NOT EXISTS`` on ``CREATE INDEX`` and ``CREATE TABLE`` is already valid PostgreSQL, and
      ``ON CONFLICT ... DO UPDATE`` is supported by both, so neither needs touching.
    """
    if not sql or not sql.strip():
        return None
    table_info = _PRAGMA_TABLE_INFO.match(sql)
    if table_info:
        table = table_info.group("table").replace("'", "''")
        return (
            "SELECT column_name AS name FROM information_schema.columns "
            f"WHERE table_schema = current_schema() AND table_name = '{table}'"
        )
    if _PRAGMA.match(sql):
        return None

    translated = SQLITE_UTC_NOW_PATTERN.sub(POSTGRES_UTC_NOW, sql)
    if _DDL.match(translated):
        for pattern, replacement in _TYPE_REPLACEMENTS:
            translated = pattern.sub(replacement, translated)
    return translated


def split_statements(script: str) -> list[str]:
    """Split a multi-statement SQL script on semicolons that are not inside a string literal.

    ``sqlite3.executescript`` takes a whole schema script; PostgreSQL's protocol wants one
    statement per execute. Quote-awareness matters because the schema's DEFAULT clauses contain
    semicolon-free but quote-heavy expressions, and a naive ``script.split(";")`` would also break
    any future default containing one.
    """
    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(script):
        char = script[index]
        if quote:
            buffer.append(char)
            if char == quote:
                # '' inside a single-quoted literal is an escaped quote, not a terminator.
                if quote == "'" and index + 1 < len(script) and script[index + 1] == "'":
                    buffer.append(script[index + 1])
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"'):
            quote = char
            buffer.append(char)
        elif char == "-" and script.startswith("--", index):
            end = script.find("\n", index)
            index = len(script) if end == -1 else end
            continue
        elif char == ";":
            statements.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
        index += 1
    statements.append("".join(buffer))
    return [item.strip() for item in statements if item.strip()]


# --------------------------------------------------------------------------- #
# PostgreSQL connection/cursor adapters
# --------------------------------------------------------------------------- #
class PostgresCursor:
    """Cursor presenting the ``sqlite3.Cursor`` surface the store code uses."""

    def __init__(self, cursor: Any, connection: "PostgresConnection") -> None:
        self._cursor = cursor
        self._connection = connection
        self._inserted = False

    # -- results ---------------------------------------------------------- #
    @property
    def _columns(self) -> tuple[str, ...]:
        description = self._cursor.description
        if not description:
            return ()
        return tuple(str(column[0]) for column in description)

    def fetchone(self) -> Row | None:
        row = self._cursor.fetchone()
        return None if row is None else Row(self._columns, row)

    def fetchall(self) -> list[Row]:
        columns = self._columns
        return [Row(columns, row) for row in self._cursor.fetchall()]

    def fetchmany(self, size: int | None = None) -> list[Row]:
        columns = self._columns
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [Row(columns, row) for row in rows]

    def __iter__(self) -> Iterator[Row]:
        columns = self._columns
        for row in self._cursor:
            yield Row(columns, row)

    # -- metadata --------------------------------------------------------- #
    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    @property
    def description(self) -> Any:
        return self._cursor.description

    @property
    def lastrowid(self) -> int | None:
        """Id generated by the most recent INSERT on this connection.

        SQLite exposes this on the cursor. PostgreSQL's equivalent is ``lastval()``, the last value
        produced by a sequence *in this session* - which is exactly right here because every
        ``with store.connect()`` block owns its own connection, so no other statement can move it.
        Returns ``None`` when the insert touched no sequence, rather than raising, matching
        ``sqlite3``'s behaviour for a table without a rowid.
        """
        if not self._inserted:
            return None
        probe = self._connection.raw.cursor()
        try:
            probe.execute("SELECT lastval()")
            row = probe.fetchone()
            return None if row is None else int(row[0])
        except Exception:  # noqa: BLE001 - no sequence was advanced; that is a valid answer.
            self._connection.raw.rollback()
            return None
        finally:
            probe.close()

    def close(self) -> None:
        self._cursor.close()


class PostgresConnection:
    """Connection presenting the ``sqlite3.Connection`` surface the store code uses.

    Mirrors ``sqlite3``'s context-manager contract - commit on clean exit, roll back on exception -
    and additionally **closes** the connection. The store never reuses a connection after its
    ``with`` block (there is not one ``.close()`` call in any of the four store classes; SQLite let
    garbage collection handle it), and PostgreSQL has a bounded ``max_connections``, so leaving them
    open would exhaust the server after a few hundred store calls.
    """

    def __init__(self, connection: Any, *, search_path: str = "") -> None:
        self.raw = connection
        self._closed = False
        if search_path:
            cursor = self.raw.cursor()
            try:
                # Identifier, so it cannot be bound as a parameter; the schema name is validated by
                # postgres_store.validate_identifier before it ever reaches here.
                cursor.execute(f'SET search_path TO "{search_path}"')
            finally:
                cursor.close()
            self.raw.commit()

    # -- context manager -------------------------------------------------- #
    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False

    # -- statements ------------------------------------------------------- #
    def execute(self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> PostgresCursor:
        translated = translate_statement(sql)
        cursor = PostgresCursor(self.raw.cursor(), self)
        if translated is None:
            # A skipped PRAGMA still has to hand back a cursor: the store does
            # conn.execute("PRAGMA ...") inline and occasionally reads from the result.
            cursor._cursor.execute("SELECT 1 WHERE false")
            return cursor
        # pg8000 does len() on the parameter tuple, so an empty tuple is required where sqlite3
        # would also accept None.
        cursor._cursor.execute(translated, tuple(parameters) if parameters else ())
        cursor._inserted = translated.lstrip()[:6].upper() == "INSERT"
        return cursor

    def executemany(self, sql: str, seq_of_parameters: Sequence[Sequence[Any]]) -> PostgresCursor:
        translated = translate_statement(sql)
        cursor = PostgresCursor(self.raw.cursor(), self)
        if translated is not None:
            cursor._cursor.executemany(translated, [tuple(item) for item in seq_of_parameters])
        return cursor

    def executescript(self, script: str) -> None:
        """Run a multi-statement script, the way ``sqlite3.executescript`` does.

        Statements are translated individually and PRAGMAs drop out, so the schema scripts the
        store already ships work unchanged.
        """
        cursor = self.raw.cursor()
        try:
            # The schema's CHECK constraints call json_valid(), so the compatibility functions have
            # to exist before the first CREATE TABLE. CREATE OR REPLACE makes this idempotent, and
            # executescript only runs during initialize(), so the cost is irrelevant.
            for statement in POSTGRES_COMPAT_DDL:
                cursor.execute(statement)
            for statement in split_statements(script):
                translated = translate_statement(statement)
                if translated is None:
                    continue
                cursor.execute(translated)
        finally:
            cursor.close()
        self.raw.commit()

    def cursor(self) -> PostgresCursor:
        return PostgresCursor(self.raw.cursor(), self)

    # -- transaction ------------------------------------------------------ #
    def commit(self) -> None:
        if not self._closed:
            self.raw.commit()

    def rollback(self) -> None:
        if not self._closed:
            self.raw.rollback()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.raw.close()

    # -- sqlite3 compatibility shims -------------------------------------- #
    @property
    def row_factory(self) -> Any:
        """Accepted and ignored: rows already come back as :class:`Row`.

        The store sets ``conn.row_factory = sqlite3.Row`` right after connecting in several
        places. Swallowing it keeps those lines working instead of forcing an edit at each one.
        """
        return Row

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        return None


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def open_connection(
    store: StoreConfig,
    *,
    sqlite_path: str | Path | None = None,
    key: str | None = None,
    password: str | None = None,
) -> sqlite3.Connection | PostgresConnection:
    """Open a store connection for the configured backend.

    For SQLite this returns a real :class:`sqlite3.Connection` with the store's usual pragmas -
    the default path keeps its exact previous behaviour. For PostgreSQL it returns a
    :class:`PostgresConnection` adapter.
    """
    if store.backend == SQLITE_BACKEND:
        return open_sqlite_connection(sqlite_path or store.sqlite.path)
    if store.backend == POSTGRESQL_BACKEND:
        return open_postgres_connection(store, key=key, password=password)
    raise StoreBackendError(f"Unknown store backend: {store.backend!r}")


def open_sqlite_connection(sqlite_path: str | Path) -> sqlite3.Connection:
    """The store's standard SQLite connection: WAL, NORMAL sync, foreign keys, busy timeout."""
    path = Path(sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def open_postgres_connection(
    store: StoreConfig, *, key: str | None = None, password: str | None = None
) -> PostgresConnection:
    from db_ops.db import postgres_store

    postgres = store.postgresql
    schema = postgres.schema or "public"
    # qmark keeps the store's '?' placeholders valid and leaves literal '%' alone. Set on the
    # module because pg8000 reads paramstyle globally at execute time.
    import pg8000.dbapi as pg8000_dbapi

    pg8000_dbapi.paramstyle = "qmark"
    raw = postgres_store.connect(postgres, key=key, password=password)
    return PostgresConnection(raw, search_path=schema)


# Per-process record of stores whose schema has already been brought up to date, keyed by
# (owner class, connection string).
#
# ``initialize()`` is called at the top of ~60 store methods, which on SQLite is a cheap no-op
# script against a local file. On PostgreSQL it is 55 DDL statements over the network, from eight
# concurrent app-command processes, and concurrent catalog writes fail outright with
# "tuple concurrently updated". Running it once per process per store is enough: a schema change
# arrives with a deploy, which restarts every process anyway.
_SCHEMA_READY: set[tuple[str, str]] = set()


def schema_is_ready(owner: str, target: "StoreTarget") -> bool:
    return (owner, target.describe()) in _SCHEMA_READY


def mark_schema_ready(owner: str, target: "StoreTarget") -> None:
    _SCHEMA_READY.add((owner, target.describe()))


def reset_schema_ready() -> None:
    """Forget what has been initialized. For tests that rebuild a store in place."""
    _SCHEMA_READY.clear()


# One arbitrary-but-stable key, so every db_ops process contends on the same lock.
_SCHEMA_LOCK_KEY = 0x6462_6F70  # "dbop"


def acquire_schema_lock(conn: Any) -> None:
    """Serialize schema DDL across processes.

    The daemon starts eight app commands, each a fresh process, each calling ``initialize()``. On
    SQLite the file lock makes that safe. On PostgreSQL they race on the system catalogs and all but
    one die with ``tuple concurrently updated`` - which is what put the worker's daemon into a crash
    loop the first time the store was switched over.

    The lock is **session**-scoped, not transaction-scoped. A transaction-scoped lock
    (``pg_advisory_xact_lock``) looked right and protected nothing: ``executescript`` commits after
    each script, and that commit released the lock while the rest of the initialization was still
    running. A session lock lasts until the connection closes, which is exactly the end of the
    ``with self.connect()`` block that owns the initialization - so it is released on success and on
    failure alike, with no unlock path to forget.
    """
    if isinstance(conn, PostgresConnection):
        conn.execute("SELECT pg_advisory_lock(?)", (_SCHEMA_LOCK_KEY,))


def remote_schema_is_current(target: "StoreTarget", owner: str, version: int) -> bool:
    """Has some process already brought this owner's schema to ``version``?

    ``schema_meta`` is the store's own version marker, so the steady-state cost of ``initialize()``
    becomes one indexed SELECT instead of ~55 DDL statements - which matters because the daemon's
    processes are new every time and cannot use the in-process memo.

    Any failure here (no ``schema_meta`` yet on a fresh store, no database at all) answers "not
    current", which sends the caller down the full DDL path. A private connection is used so a failed
    probe cannot leave an aborted transaction behind for the DDL to trip over.
    """
    try:
        conn = target.connect()
    except Exception:  # noqa: BLE001 - unreachable store is answered by the DDL path.
        return False
    try:
        row = conn.execute(
            "SELECT schema_version FROM schema_meta WHERE schema_name = ?", (owner,)
        ).fetchone()
        return row is not None and int(row[0]) == int(version)
    except Exception:  # noqa: BLE001 - missing table/database: not current.
        return False
    finally:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        conn.close()


# schema_meta is the shared version registry every store records into. DbOpsStore's SCHEMA_SQL
# creates it as part of its own schema, but MetricStore and SlaStore record here too and must not
# depend on DbOpsStore having initialized first - so the table is ensured on the spot. Identical
# definition to the one in sqlite_store.SCHEMA_SQL, and IF NOT EXISTS on both sides, so whichever
# store runs first creates it and the other finds it.
SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta
(
    schema_name TEXT NOT NULL PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
)
"""


def table_exists(conn: Any, table: str) -> bool:
    """Does this table exist? Portable across both backends.

    ``sqlite_master`` is SQLite's own catalog and has no PostgreSQL equivalent, so a store method
    that probed it ("does metric_results exist yet?") failed with 'relation "sqlite_master" does not
    exist' the moment the SLA app ran against PostgreSQL. Unlike ``PRAGMA table_info`` this is not
    worth translating textually - the query shapes vary - so callers ask here instead.
    """
    if isinstance(conn, PostgresConnection):
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    return row is not None


def resync_identity_sequences(conn: Any) -> None:
    """Point every identity sequence in the schema past the largest id present.

    PostgreSQL identity columns are declared ``GENERATED BY DEFAULT``, so inserting an explicit id
    does **not** advance the sequence. SQLite's AUTOINCREMENT counter does move, which is why this
    only matters on PostgreSQL - and why it is easy to miss.

    Any code path that copies rows with their ids leaves the sequence behind: the migration tool
    handles its own, but the store's table-rebuild migrations do the same thing
    (``INSERT INTO reports_new (...) SELECT ... FROM reports``) and left the fresh sequence at 1 with
    6495 rows in the table. The next report insert then died with
    'duplicate key value violates unique constraint "reports_new_pkey"'.

    Sweeping the whole schema rather than the rebuilt tables specifically keeps this correct for any
    future path that inserts explicit ids. It is idempotent and cheap, and runs only when
    ``initialize()`` actually does work.
    """
    if not isinstance(conn, PostgresConnection):
        return
    identity_columns = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND is_identity = 'YES'"
    ).fetchall()
    for row in identity_columns:
        table = str(row["table_name"])
        column = str(row["column_name"])
        conn.execute(
            "SELECT setval("
            "  pg_get_serial_sequence(?, ?),"
            f"  COALESCE((SELECT MAX({quote_identifier(column)}) FROM {quote_identifier(table)}), 1),"
            f"  (SELECT MAX({quote_identifier(column)}) IS NOT NULL FROM {quote_identifier(table)})"
            ")",
            (table, column),
        )


def record_schema_version(conn: Any, owner: str, version: int) -> None:
    """Mark this owner's schema as built, for :func:`remote_schema_is_current`.

    ``ON CONFLICT ... DO UPDATE`` is supported by both engines, so the upsert needs no translation.
    """
    # Raw SQLite SQL: PostgresConnection.execute translates it, and a real sqlite3 connection wants
    # it untouched. Translating here would have handed SQLite the PostgreSQL to_char() form.
    conn.execute(SCHEMA_META_DDL)
    conn.execute(
        "INSERT INTO schema_meta (schema_name, schema_version) VALUES (?, ?) "
        "ON CONFLICT(schema_name) DO UPDATE SET schema_version = excluded.schema_version",
        (owner, int(version)),
    )


class StoreTarget:
    """What a store class connects to: a backend declaration plus the secret to open it with.

    The store classes historically took a bare ``sqlite_path``, and 35 call sites plus the whole test
    suite still do that. So the rule here is deliberate and narrow:

    * constructed from a **path** -> SQLite, always. Existing callers and tests are unaffected, and
      no test can accidentally reach for a real PostgreSQL server.
    * constructed from a **config** (``StoreTarget.from_config``) -> whatever
      ``data/store_config.json`` declares.

    ``sqlite_path`` stays readable either way because callers and tests reference it; on a PostgreSQL
    store it is the path the config still carries, not something being written to.
    """

    __slots__ = ("store", "key", "password")

    def __init__(self, store: StoreConfig, *, key: str | None = None, password: str | None = None) -> None:
        self.store = store
        self.key = key
        self.password = password

    @classmethod
    def for_sqlite(cls, sqlite_path: str | Path) -> "StoreTarget":
        from db_ops.config import SqliteStoreConfig

        return cls(
            StoreConfig(backend=SQLITE_BACKEND, sqlite=SqliteStoreConfig(path=Path(sqlite_path)))
        )

    @classmethod
    def from_config(cls, config: Any, *, key: str | None = None, password: str | None = None) -> "StoreTarget":
        """Build from a :class:`~db_ops.config.DbOpsConfig` (or a bare :class:`StoreConfig`)."""
        store = getattr(config, "store", config)
        if not isinstance(store, StoreConfig):
            raise StoreBackendError(
                "from_config expects a DbOpsConfig or StoreConfig, got "
                f"{type(config).__name__}."
            )
        return cls(store, key=key, password=password)

    @classmethod
    def coerce(
        cls,
        source: "StoreTarget | StoreConfig | str | Path",
        *,
        key: str | None = None,
        password: str | None = None,
    ) -> "StoreTarget":
        """Accept whatever a store constructor was handed: a target, a config, or a path."""
        if isinstance(source, StoreTarget):
            return source
        if isinstance(source, StoreConfig):
            return cls(source, key=key, password=password)
        return cls.for_sqlite(source)

    @property
    def is_sqlite(self) -> bool:
        return self.store.backend == SQLITE_BACKEND

    @property
    def sqlite_path(self) -> Path:
        return Path(self.store.sqlite.path)

    def connect(self) -> sqlite3.Connection | PostgresConnection:
        return open_connection(self.store, key=self.key, password=self.password)

    def prepare(self) -> None:
        """Filesystem preparation before schema creation.

        SQLite needs its parent directory to exist; PostgreSQL needs nothing, and calling mkdir for
        it would silently create a stray ``runtime/`` folder on every node.
        """
        if self.is_sqlite:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        return self.store.connection_string


__all__ = [
    "POSTGRES_UTC_NOW",
    "StoreTarget",
    "PostgresConnection",
    "PostgresCursor",
    "Row",
    "StoreBackendError",
    "open_connection",
    "open_postgres_connection",
    "open_sqlite_connection",
    "split_statements",
    "translate_statement",
]
