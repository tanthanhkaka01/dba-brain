"""PostgreSQL side of the dbabrain runtime store.

The store dbabrain writes its own data to is declared in ``data/store_config.json``
(:class:`dbabrain.lib.config.StoreConfig`). This module is what turns that declaration into a live
PostgreSQL connection, and what provisions the database/schema the declaration points at.

Provisioning is deliberately separate from ``backend: postgresql`` being live: you create and
fill the Postgres store *while still running on SQLite*, verify it, and only then flip the
backend. So every function here reads ``config.store.postgresql`` regardless of which backend
is currently active.

The driver is **pg8000** — the same pure-Python driver ``dbabrain.metrics`` already uses for
PostgreSQL targets, so no new dependency and no build/system deps in the image.
"""

from __future__ import annotations

from dbabrain.lib import errors
import re
from dataclasses import dataclass
from typing import Any

from dbabrain.lib.packaging import install_hint
from dbabrain.lib.config import PostgresStoreConfig

# A maintenance database always exists and is never the one being created, so CREATE DATABASE
# is issued while connected here.
MAINTENANCE_DATABASE = "postgres"

# CREATE DATABASE / CREATE SCHEMA cannot take a bind parameter for the object name, so the name
# is validated instead of quoted-and-hoped. Deliberately stricter than PostgreSQL itself:
# lowercase, starts with a letter or underscore, and nothing that needs quoting ever again.
_SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PostgresStoreError(errors.OperationFailed):
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
    ``dbabrain.db.backend`` set ``qmark``, whichever ran last decided how the other's placeholders were
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
    the secret store (``password_ref`` + ``DBABRAIN_SECRET_KEY``) is the normal path.
    """
    if password:
        return str(password)
    return postgres.resolved_password(key)


#: libpq's sslmode values, and what each becomes for pg8000, which takes an ``ssl_context`` rather
#: than a mode. The setting was read and never passed on, so every store connection was plaintext
#: whatever it said (review 0.25.0, B5.5). ``disable``, ``allow`` and ``prefer`` - the default -
#: still connect without TLS: pg8000 cannot try TLS and fall back, and turning it on for every
#: existing store would refuse the ones that do not offer it.
SSLMODES_WITHOUT_TLS = ("disable", "allow", "prefer")
SSLMODES_WITH_TLS = ("require", "verify-ca", "verify-full")


def ssl_context_for(sslmode: str):
    """The ``ssl_context`` for ``sslmode``, or ``None`` for a plaintext connection.

    ``require`` encrypts and checks nothing (libpq's meaning); ``verify-ca`` checks the chain against
    the system's trust store (``SSL_CERT_FILE`` names another bundle); ``verify-full`` checks the
    chain and that the name is the host's.
    """
    mode = str(sslmode or "prefer").strip().lower()
    if mode in SSLMODES_WITHOUT_TLS:
        return None
    if mode not in SSLMODES_WITH_TLS:
        raise PostgresStoreError(
            f"sslmode {sslmode!r} in the postgresql block of data/store_config.json is not one of "
            f"{', '.join(SSLMODES_WITHOUT_TLS + SSLMODES_WITH_TLS)}.")
    import ssl

    context = ssl.create_default_context()
    if mode == "require":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif mode == "verify-ca":
        context.check_hostname = False
    return context


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

    ssl_context = ssl_context_for(postgres.sslmode)
    try:
        conn = dbapi.connect(
            host=postgres.host,
            port=int(postgres.port or 5432),
            user=postgres.username,
            password=resolve_store_password(postgres, key=key, password=password),
            database=target_database,
            timeout=int(postgres.connect_timeout_seconds or 10),
            application_name=postgres.application_name or "dbabrain",
            ssl_context=ssl_context,
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
    """Create the dbabrain store database and schema if they are not there yet.

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
                "The dbabrain store is written to, so point store_config.json at the primary."
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


# --------------------------------------------------------------------------- #
# A login and a schema of its own, for another tool sharing the store's instance
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AppLoginResult:
    """What :func:`create_app_login` did, step by step - reported, not guessed at."""
    host: str
    port: int
    database: str
    role: str
    schema: str
    role_created: bool
    password_set: bool
    schema_created: bool
    statements: tuple[str, ...]
    reaches_store_schema: bool
    dry_run: bool = False

    def summary(self) -> str:
        prefix = "[dry-run] would ensure" if self.dry_run else "ensured"
        bits = [
            f"role {self.role} {'created' if self.role_created else 'already present'}"
            + (", password set" if self.password_set else ""),
            f"schema {self.schema} {'created' if self.schema_created else 'already present'}",
            f"store schema {'REACHABLE - check its grants' if self.reaches_store_schema else 'not reachable'}",
        ]
        return f"{prefix} {self.host}:{self.port}/{self.database}: " + ", ".join(bits)


def _sql_literal(value: str) -> str:
    """A string literal for a utility statement (CREATE/ALTER ROLE take no bind parameters).

    standard_conforming_strings is on by default since PostgreSQL 9.1, so doubling the quote is the
    whole escape; a NUL cannot be sent at all, and is refused rather than silently cut.
    """
    if "\x00" in value:
        raise PostgresStoreError("A password cannot contain a NUL character.")
    return "'" + value.replace("'", "''") + "'"


def _role(conn, role: str) -> dict[str, Any] | None:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT rolsuper, rolcanlogin FROM pg_roles WHERE rolname = ?", (role,))
        row = cursor.fetchone()
        return None if row is None else {"superuser": bool(row[0]), "login": bool(row[1])}
    finally:
        cursor.close()


def create_app_login(
    postgres: PostgresStoreConfig,
    *,
    role: str,
    schema: str,
    role_password: str,
    reset_password: bool = False,
    key: str | None = None,
    password: str | None = None,
    dry_run: bool = False,
    require_primary: bool = True,
) -> AppLoginResult:
    """Give another tool its own login and its own schema in the store's database.

    The operator, 2026-11-04: *daily-assists keeps its records in the dbabrain PostgreSQL instance,
    database `db_ops`, a schema of its own - dbabrain may create a user for it, grant it the database
    and the schema.* So it is created here rather than by a hand-typed script, and the role gets
    exactly that: ``CONNECT`` on the store's database, the schema as its owner (so it can create its
    own tables), its ``search_path`` pointed at it, and nothing on dbabrain's own schema.

    Idempotent. An existing role keeps its password unless ``reset_password`` - the name could
    belong to something already running, and changing its password would lock that out. A role
    that is a superuser is refused outright: the point of the login is that it is not one.
    """
    database = validate_identifier(postgres.database, kind="database")
    role = validate_identifier(role, kind="role")
    schema = validate_identifier(schema, kind="schema")
    store_schema = validate_identifier(postgres.schema or "public", kind="schema")
    if schema in {store_schema, "public"}:
        raise PostgresStoreError(
            f"Schema {schema!r} is the store's own (or public); an app gets a schema of its own.")
    if not role_password:
        raise PostgresStoreError("The new login needs a password.")

    conn = connect(postgres, key=key, password=password, database=database, autocommit=True)
    try:
        if require_primary and is_in_recovery(conn):
            raise PostgresStoreError(
                f"{postgres.host}:{postgres.port} is a standby; create the login on the primary.")
        existing = _role(conn, role)
        if existing and existing["superuser"]:
            raise PostgresStoreError(f"Role {role!r} is a superuser; refusing to hand it to an app.")
        needs_schema = not schema_exists(conn, schema)

        q_role, q_schema, q_db = quote_identifier(role), quote_identifier(schema), quote_identifier(database)
        secret = _sql_literal(role_password)
        plan: list[tuple[str, str]] = []          # (statement to run, statement to report)
        if existing is None:
            text = f"CREATE ROLE {q_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD "
            plan.append((text + secret, text + "'***'"))
        elif reset_password or not existing["login"]:
            text = f"ALTER ROLE {q_role} LOGIN PASSWORD "
            plan.append((text + secret, text + "'***'"))
        if needs_schema:
            plan.append((f"CREATE SCHEMA {q_schema} AUTHORIZATION {q_role}",) * 2)
        else:
            plan.append((f"GRANT USAGE, CREATE ON SCHEMA {q_schema} TO {q_role}",) * 2)
        plan.append((f"GRANT CONNECT ON DATABASE {q_db} TO {q_role}",) * 2)
        plan.append((f"ALTER ROLE {q_role} IN DATABASE {q_db} SET search_path = {q_schema}",) * 2)

        if not dry_run:
            cursor = conn.cursor()
            try:
                for statement, _ in plan:
                    cursor.execute(statement)
            finally:
                cursor.close()

        reaches = False
        if existing is not None or not dry_run:
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT has_schema_privilege(?, ?, 'USAGE')", (role, store_schema))
                row = cursor.fetchone()
                reaches = bool(row[0]) if row else False
            finally:
                cursor.close()
    finally:
        conn.close()

    return AppLoginResult(
        host=postgres.host, port=int(postgres.port or 5432), database=database, role=role,
        schema=schema, role_created=existing is None,
        password_set=existing is None or reset_password or not existing["login"],
        schema_created=needs_schema, statements=tuple(shown for _, shown in plan),
        reaches_store_schema=reaches, dry_run=dry_run,
    )
