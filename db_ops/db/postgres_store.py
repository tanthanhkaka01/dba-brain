"""PostgreSQL side of the db_ops runtime store.

The store db_ops writes its own data to is declared in ``data/store_config.json``
(:class:`db_ops.config.StoreConfig`). This module is what turns that declaration into a live
PostgreSQL connection, and what provisions the database/schema the declaration points at.

Provisioning is deliberately separate from ``backend: postgresql`` being live: you create and
fill the Postgres store *while still running on SQLite*, verify it, and only then flip the
backend. So every function here reads ``config.store.postgresql`` regardless of which backend
is currently active.

The driver is **pg8000** — the same pure-Python driver ``db_ops.metrics`` already uses for
PostgreSQL targets, so no new dependency and no build/system deps in the image.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from db_ops.lib.packaging import install_hint
from db_ops.config import PostgresStoreConfig

# A maintenance database always exists and is never the one being created, so CREATE DATABASE
# is issued while connected here.
MAINTENANCE_DATABASE = "postgres"

# CREATE DATABASE / CREATE SCHEMA cannot take a bind parameter for the object name, so the name
# is validated instead of quoted-and-hoped. Deliberately stricter than PostgreSQL itself:
# lowercase, starts with a letter or underscore, and nothing that needs quoting ever again.
_SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PostgresStoreError(RuntimeError):
    """A store provisioning/connection step failed."""


def validate_identifier(name: str, *, kind: str) -> str:
    """Return ``name`` if it is a safe unquoted PostgreSQL identifier, else raise.

    Used for every name that is interpolated into DDL (database, schema, table, column,
    index). Rejecting up front is what keeps the DDL builders free of injection concerns.
    """
    value = str(name or "").strip()
    if not _SAFE_IDENTIFIER.match(value):
        raise PostgresStoreError(
            f"Unsafe {kind} name {name!r}. Use lowercase letters, digits and underscores, "
            "starting with a letter or underscore (max 63 characters)."
        )
    return value


def quote_identifier(name: str) -> str:
    """Double-quote a *validated* identifier for use in SQL."""
    return f'"{name}"'


def _dbapi():
    """Import pg8000 lazily, and pin its paramstyle.

    ``paramstyle`` is a **module-level global** in pg8000, so every consumer in the process shares
    it. ``qmark`` is set here, at the single place a connection is created, because the store classes
    carry ~128 SQLite-style ``?`` placeholders. Leaving it at the default ``format`` and translating
    them would also have meant escaping every literal ``%`` (``LIKE '%x%'``) in the codebase.

    Pinning it here rather than per-caller matters: while the migration module assumed ``format`` and
    ``db_ops.db.backend`` set ``qmark``, whichever ran last decided how the other's placeholders were
    parsed.
    """
    try:
        from pg8000 import dbapi  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - driver install is environmental.
        raise PostgresStoreError(
            "pg8000 is required for the PostgreSQL store. Install it with: "
            + install_hint("postgres")
        ) from exc
    dbapi.paramstyle = "qmark"
    return dbapi


def resolve_store_password(
    postgres: PostgresStoreConfig, *, key: str | None = None, password: str | None = None
) -> str:
    """Resolve the store password: explicit override first, then the encrypted secret store.

    ``password`` exists for provisioning runs from a machine without the passphrase to hand;
    the secret store (``password_ref`` + ``DB_OPS_SECRET_KEY``) is the normal path.
    """
    if password:
        return str(password)
    return postgres.resolved_password(key)


def connect(
    postgres: PostgresStoreConfig,
    *,
    key: str | None = None,
    password: str | None = None,
    database: str | None = None,
    autocommit: bool = False,
):
    """Open a pg8000 connection to the store.

    ``database`` overrides the configured one — that is how the maintenance database is reached
    for CREATE DATABASE, which cannot run while connected to its own target.
    """
    dbapi = _dbapi()
    target_database = database or postgres.database
    if not target_database:
        raise PostgresStoreError(
            "No database configured for the PostgreSQL store. Set 'database' in the "
            "postgresql block of data/store_config.json."
        )
    if not postgres.host:
        raise PostgresStoreError(
            "No host configured for the PostgreSQL store. Set 'host' in the postgresql block "
            "of data/store_config.json."
        )

    try:
        conn = dbapi.connect(
            host=postgres.host,
            port=int(postgres.port or 5432),
            user=postgres.username,
            password=resolve_store_password(postgres, key=key, password=password),
            database=target_database,
            timeout=int(postgres.connect_timeout_seconds or 10),
            application_name=postgres.application_name or "db_ops",
        )
    except Exception as exc:  # noqa: BLE001 - driver raises a wide range of connect errors.
        raise PostgresStoreError(
            f"Could not connect to PostgreSQL store {postgres.username}@{postgres.host}:"
            f"{postgres.port}/{target_database}: {exc}"
        ) from exc

    # pg8000's `timeout` is a *socket* timeout: it bounds the connect attempt and then every
    # subsequent operation on that socket. Left in place, `connect_timeout_seconds` silently becomes
    # a statement timeout - which killed a CREATE INDEX on a 3.4M-row table after 10 seconds with a
    # bare "timed out". A connect timeout and a statement timeout are different settings, so the
    # socket is returned to blocking mode once the connection is established. Bound long statements
    # server-side (statement_timeout) if you need to, not by timing out the client's socket.
    socket = getattr(conn, "_usock", None)
    if socket is not None:
        socket.settimeout(None)
    if autocommit:
        conn.autocommit = True
    return conn


def server_version(conn) -> str:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT version()")
        row = cursor.fetchone()
        return str(row[0]) if row else ""
    finally:
        cursor.close()


def is_in_recovery(conn) -> bool:
    """True when this node is a standby. The store writes, so it must be the primary."""
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT pg_is_in_recovery()")
        row = cursor.fetchone()
        return bool(row[0]) if row else False
    finally:
        cursor.close()


def database_exists(conn, database: str) -> bool:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = ?", (database,))
        return cursor.fetchone() is not None
    finally:
        cursor.close()


def schema_exists(conn, schema: str) -> bool:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = ?", (schema,))
        return cursor.fetchone() is not None
    finally:
        cursor.close()


@dataclass(frozen=True)
class ProvisionResult:
    """What :func:`create_store_database` actually did — reported, not guessed at."""
    host: str
    port: int
    database: str
    schema: str
    database_created: bool
    schema_created: bool
    server_version: str = ""
    dry_run: bool = False

    def summary(self) -> str:
        target = f"{self.host}:{self.port}/{self.database}"
        prefix = "[dry-run] would ensure" if self.dry_run else "ensured"
        bits = [
            f"database {'created' if self.database_created else 'already present'}",
            f"schema {self.schema} {'created' if self.schema_created else 'already present'}",
        ]
        return f"{prefix} {target}: " + ", ".join(bits)


def create_store_database(
    postgres: PostgresStoreConfig,
    *,
    key: str | None = None,
    password: str | None = None,
    dry_run: bool = False,
    encoding: str = "UTF8",
    require_primary: bool = True,
) -> ProvisionResult:
    """Create the db_ops store database and schema if they are not there yet.

    Idempotent: an existing database/schema is left exactly as it is, so this is safe to re-run
    (and safe to run before every migration). The database is created while connected to the
    maintenance database because PostgreSQL will not create a database from inside it, and with
    autocommit because CREATE DATABASE cannot run in a transaction block.

    ``require_primary`` refuses to provision against a standby. The store is written to, and a
    hot standby is read-only — catching that here gives a clear message instead of a confusing
    "cannot execute CREATE DATABASE in a read-only transaction" later.
    """
    database = validate_identifier(postgres.database, kind="database")
    schema = validate_identifier(postgres.schema or "public", kind="schema")

    admin = connect(
        postgres, key=key, password=password, database=MAINTENANCE_DATABASE, autocommit=True
    )
    try:
        version = server_version(admin)
        if require_primary and is_in_recovery(admin):
            raise PostgresStoreError(
                f"{postgres.host}:{postgres.port} is a standby (pg_is_in_recovery() = true). "
                "The db_ops store is written to, so point store_config.json at the primary."
            )
        needs_database = not database_exists(admin, database)
        if needs_database and not dry_run:
            cursor = admin.cursor()
            try:
                cursor.execute(
                    f"CREATE DATABASE {quote_identifier(database)} ENCODING {encoding!r}"
                )
            finally:
                cursor.close()
    finally:
        admin.close()

    if dry_run:
        # Without the database there is nothing to connect to, so the schema step is reported
        # as "would create" rather than probed.
        return ProvisionResult(
            host=postgres.host, port=int(postgres.port or 5432), database=database, schema=schema,
            database_created=needs_database, schema_created=needs_database or True,
            server_version=version, dry_run=True,
        )

    conn = connect(postgres, key=key, password=password, database=database, autocommit=True)
    try:
        needs_schema = schema != "public" and not schema_exists(conn, schema)
        if needs_schema:
            cursor = conn.cursor()
            try:
                cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(schema)}")
            finally:
                cursor.close()
    finally:
        conn.close()

    return ProvisionResult(
        host=postgres.host, port=int(postgres.port or 5432), database=database, schema=schema,
        database_created=needs_database, schema_created=needs_schema, server_version=version,
    )
