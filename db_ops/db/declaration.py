"""The store, stated as data so it can travel in a request.

``common`` is the layer that performs work and reads nothing. That holds for SQL against a target
database already — a ``run-sql`` request carries the host, the login and the password — but the
*runtime store* was the exception: a caller could only say "config.json" and let the other side go
and read it. So the one thing every app writes through was also the one thing ``common`` had to
look up.

This closes that. The store describes itself here — backend, host, database, schema, user, and the
**already-resolved** password — and the description travels in the request like any other value.
``common`` connects with what it was handed and resolves nothing.

Two consequences worth stating, because both were problems before:

* **A caller can name a store that is not the node's own.** The in-process path could hand over a
  live store object; a subprocess could not, so tests that write to a temp store had no way to
  point the CLI at it. A declaration is something a test can build.
* **The password is in the payload.** That is why every consumer takes the request on **stdin**
  and never as an argv word: argv is world-readable in the process table, stdin is not.
  :func:`describe` is the only place that resolves it, and :func:`redact` exists for logging.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db_ops.db.backend import StoreTarget


class StoreDeclarationError(ValueError):
    """The store block cannot be honoured as written."""


def describe(config: Any, *, key: str | None = None, password: str | None = None) -> dict[str, Any]:
    """Serialise a :class:`~db_ops.config.StoreConfig` (or a ``DbOpsConfig``) into a request block.

    ``password`` overrides the lookup; otherwise a PostgreSQL store resolves its own
    ``password_ref`` here, on the app side, so the other end never touches the secret store.
    """
    from db_ops.config import StoreConfig

    store = getattr(config, "store", config)
    if not isinstance(store, StoreConfig):
        raise StoreDeclarationError(
            f"describe expects a DbOpsConfig or StoreConfig, got {type(config).__name__}.")

    if store.is_sqlite:
        return {"backend": "sqlite", "sqlite": {"path": str(store.sqlite.path)}}

    pg = store.postgresql
    secret = password if password is not None else pg.resolved_password(key)
    return {
        "backend": "postgresql",
        "postgresql": {
            "host": pg.host, "port": int(pg.port), "database": pg.database,
            "schema": pg.schema, "username": pg.username, "password": secret,
            "sslmode": pg.sslmode, "connect_timeout_seconds": int(pg.connect_timeout_seconds),
            "application_name": pg.application_name,
        },
    }


def describe_store(store: Any) -> dict[str, Any]:
    """Describe a **live** store — the one a caller already holds and is already connected with.

    The point of the model is that the store states its own connection, so a caller that has a
    store should not have to go back to a config file to say what it is. The daemon is the case
    that forced this: its notify helper deliberately takes no config (holding Telegram settings of
    its own is exactly how it drifted from every other app), so a store object is all it has.

    The password comes from the target when it carries one — ``StoreTarget`` already accepts a
    resolved password — and is otherwise resolved the same way :func:`describe` does it.
    """
    target = getattr(store, "target", None)
    if target is None:
        raise StoreDeclarationError(
            f"describe_store expects a DbOpsStore, got {type(store).__name__}.")
    return describe(target.store, key=getattr(target, "key", None),
                    password=(getattr(target, "password", None) or None))


def parse(raw: Any) -> StoreTarget:
    """Build a :class:`StoreTarget` from a request block. Reads no config and no secret store."""
    if not isinstance(raw, dict):
        raise StoreDeclarationError("store must be an object.")
    backend = str(raw.get("backend") or "").strip().lower()

    if backend == "sqlite":
        path = str((raw.get("sqlite") or {}).get("path") or "").strip()
        if not path:
            raise StoreDeclarationError("store.sqlite.path is required for a sqlite store.")
        return StoreTarget.for_sqlite(path)

    if backend in {"postgresql", "postgres"}:
        from db_ops.config import PostgresStoreConfig, StoreConfig

        block = raw.get("postgresql") or {}
        for required in ("host", "database", "username"):
            if not str(block.get(required) or "").strip():
                raise StoreDeclarationError(f"store.postgresql.{required} is required.")
        pg = PostgresStoreConfig(
            host=str(block["host"]), port=int(block.get("port") or 5432),
            database=str(block["database"]), schema=str(block.get("schema") or ""),
            username=str(block["username"]), sslmode=str(block.get("sslmode") or "prefer"),
            connect_timeout_seconds=int(block.get("connect_timeout_seconds") or 10),
            application_name=str(block.get("application_name") or "db_ops"),
        )
        # The password rides beside the declaration rather than inside it: StoreTarget already
        # takes it as an override, which is what keeps `password_ref` (a lookup) out of here.
        return StoreTarget(StoreConfig(backend="postgresql", postgresql=pg),
                           password=str(block.get("password") or ""))

    raise StoreDeclarationError(
        f"store.backend must be sqlite or postgresql; got {backend or '<missing>'}.")


def redact(raw: Any) -> dict[str, Any]:
    """The same block with the password replaced, for logging and error messages."""
    if not isinstance(raw, dict):
        return {}
    safe = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
    if isinstance(safe.get("postgresql"), dict) and safe["postgresql"].get("password"):
        safe["postgresql"]["password"] = "***"
    return safe


def for_path(sqlite_path: str | Path) -> dict[str, Any]:
    """A sqlite declaration, for tests and for callers that already hold a path."""
    return {"backend": "sqlite", "sqlite": {"path": str(sqlite_path)}}
