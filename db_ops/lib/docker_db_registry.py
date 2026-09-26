"""The lab-database connection registry, and what is looked up before ``common`` builds a lab.

``common.cli create-db-docker`` / ``move-db-docker`` read no configuration (0.23.0): the password,
the SSH login and the engine arrive in the request as values, and the answer describes what was
built. Everything around that call is a node's own data - the encrypted secret store, and
``data/docker_db_connections.json``, the registry of what was built - and this module is it.

It was ``sre/docker_db/`` until 0.24.0, which made it ``sre``'s alone. ``control
worker-create-db-docker`` builds the same thing on the worker's host, and an app may import neither
another app nor ``common`` (rules R01, R03) - so the second caller either copied it or ran ``sre``'s
CLI inside the worker container, which is what it did (R42). Here, both import one copy.

The registry follows the ``data/`` convention (a top-level key wrapping an array of snake_case
objects, 2-space indent), and a write is an idempotent upsert keyed by ``id`` (``<NAME>``
upper-cased), so building the same name again refreshes its entry in place.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from db_ops.lib import field_names
from db_ops.lib.data_sources import REGISTRY_FILENAME
from db_ops.lib.docker_db_spec import ENGINE_META, DockerDbSpec

REGISTRY_ROOT_KEY = "docker_db_connections"
CREATED_BY = "db_ops.sre.create-db-docker"


class DockerDbRequestError(RuntimeError):
    """The request for ``common.cli`` could not be assembled from what the operator gave."""


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
def default_registry_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / REGISTRY_FILENAME


def connection_id(name: str) -> str:
    return name.upper()


def build_connection_entry(
    spec: DockerDbSpec,
    *,
    host: str,
    compose_path: str,
    worker_host: str = "",
    created_by: str = CREATED_BY,
) -> dict:
    meta = spec.meta
    docker: dict = {
        "instance_name": spec.name,
        "mode": spec.mode,
        "version": spec.version,
        "compose_path": compose_path,
    }
    if spec.is_ha:
        docker["replicas"] = spec.replicas
    entry: dict = {
        "id": connection_id(spec.name),
        # Standard names since 0.22.0: db_type, database_name, password_ref (a secret REF).
        "db_type": spec.engine,
        "host": host,
        "port": spec.host_port,
        "database_name": meta.database,
        "username": meta.username,
        "password_ref": spec.password_env,
        "docker": docker,
        # Which command made it: a record found later says whether sre built it from this node or
        # control built it on the worker's host.
        "created_by": created_by,
    }
    if worker_host:
        entry["worker_host"] = worker_host
    return entry


def load_registry(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {REGISTRY_ROOT_KEY: []}
    with path.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get(REGISTRY_ROOT_KEY), list):
        raise ValueError(
            f"{path} is not a valid docker-db registry (expected a '{REGISTRY_ROOT_KEY}' array)."
        )
    return data


def save_registry(path: str | Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def register_connection(registry_path: str | Path, entry: dict) -> str:
    """Upsert ``entry`` into the registry. Returns ``"added"`` or ``"updated"``."""
    data = load_registry(registry_path)
    entries = data[REGISTRY_ROOT_KEY]
    entry_id = entry["id"]
    for index, existing in enumerate(entries):
        if isinstance(existing, dict) and existing.get("id") == entry_id:
            entries[index] = entry
            save_registry(registry_path, data)
            return "updated"
    entries.append(entry)
    save_registry(registry_path, data)
    return "added"


def relocate_connection(registry_path: str | Path, entry_id: str, *, host: str,
                        worker_host: str, compose_path: str) -> str:
    """Point an existing connection entry at the machine the instance now runs on.

    A move changes exactly three facts — where the database answers, which host holds the
    containers, and where its compose file is — and leaves everything else (engine, port,
    credentials ref, version) alone. Rebuilding the entry from a spec instead would quietly
    reset fields the operator has edited since it was provisioned.

    Returns ``"updated"``, or ``"not_registered"`` when the instance predates the registry —
    which is not an error: the move succeeded, there was simply nothing here to correct.
    """
    data = load_registry(registry_path)
    for entry in data[REGISTRY_ROOT_KEY]:
        if not isinstance(entry, dict) or entry.get("id") != entry_id:
            continue
        entry["host"] = host
        entry["worker_host"] = worker_host
        docker = entry.get("docker")
        if isinstance(docker, dict):
            docker["compose_path"] = compose_path
        save_registry(registry_path, data)
        return "updated"
    return "not_registered"


def registered_engine(name: str, *, data_dir: str | Path | None) -> str:
    """The engine the connection registry records for instance ``name``, or ``""``.

    Where the fact already lives; ``--engine`` is the escape hatch for an instance provisioned
    before the registry existed, or created by hand.
    """
    if not data_dir:
        return ""
    try:
        registry = load_registry(default_registry_path(data_dir))
    except (OSError, ValueError):
        return ""
    for entry in registry.get(REGISTRY_ROOT_KEY, []):
        if isinstance(entry, dict) and entry.get("id") == connection_id(name):
            # `db_type` since 0.22.0; a record written earlier says `engine`.
            engine = str(field_names.read(entry, "docker_db_connection", "db_type", "") or "")
            if engine in ENGINE_META:
                return engine
    return ""


# --------------------------------------------------------------------------- #
# The password
# --------------------------------------------------------------------------- #
def resolve_password_value(
    password_env: str,
    *,
    key: str | None = None,
    key_base64: str | None = None,
    data_dir: str | Path | None = None,
    allow_missing: bool = False,
) -> tuple[str | None, str]:
    """Resolve the password for ``password_env``.

    Order: the live environment variable, then the encrypted secret store
    (ref == ``password_env``) if a key is available. Returns
    ``(value, source_label)``. With ``allow_missing`` a missing password yields
    ``(None, ...)`` (used by dry-run so nothing sensitive is required to preview).
    """
    env_value = os.environ.get(password_env, "").strip()
    if env_value:
        return env_value, f"env:{password_env}"

    secret_key: str | None = None
    try:
        from db_ops.lib.secret_text import resolve_cli_key
        secret_key = resolve_cli_key(key, key_base64)
    except Exception:  # noqa: BLE001 - no/!invalid key; fall through.
        secret_key = None
    if not secret_key:
        secret_key = os.environ.get("DB_OPS_SECRET_KEY") or None

    if secret_key and data_dir is not None:
        from db_ops.lib import data_sources
        try:
            secrets = data_sources.load_secret_text(data_dir, key=secret_key)
        except Exception:  # noqa: BLE001 - wrong key etc.
            secrets = {}
        value = (secrets.get(password_env) or "").strip()
        if value:
            return value, f"secret:{password_env}"

    if allow_missing:
        return None, f"env:{password_env}"
    raise DockerDbRequestError(
        f"Password not found. Set the '{password_env}' environment variable, or store it under "
        f"that name in the secret store and pass --key/--key-base64."
    )


def _store_key(key: str | None, key_base64: str | None) -> str:
    from db_ops.lib.secret_text import resolve_cli_key

    resolved = resolve_cli_key(key, key_base64) or os.environ.get("DB_OPS_SECRET_KEY")
    if not resolved:
        raise DockerDbRequestError(
            "Storing a password needs the secret-store passphrase (--key/--key-base64 or "
            "DB_OPS_SECRET_KEY): the store is encrypted at rest."
        )
    return resolved


def assert_password_can_be_stored(ref: str, value: str, *, data_dir: str | Path,
                                  key: str | None = None, key_base64: str | None = None,
                                  overwrite: bool = False) -> None:
    """Refuse up front what :func:`store_password` would refuse after the build: a different
    value already under this ref without ``overwrite``, or no passphrase to store with.

    Checked before building, stored only once the lab exists: a build that failed - a wrong image
    tag - used to leave its password in the store under a ref no lab uses (the official test,
    2026-09-24).
    """
    from db_ops.lib import data_sources

    existing = (data_sources.load_secret_text(data_dir, key=_store_key(key, key_base64)) or {}).get(ref)
    if existing is not None and existing != value and not overwrite:
        raise DockerDbRequestError(
            f"Secret ref {ref} already exists with a different value. Pass --overwrite-secret to "
            "replace it (from Telegram: answer 'yes' to recreate), or give no password to reuse "
            "the stored one."
        )


def store_password(ref: str, value: str, *, data_dir: str | Path, key: str | None = None,
                   key_base64: str | None = None, overwrite: bool = False) -> bool:
    """Persist ``value`` under ``ref`` in the encrypted store; ``False`` if it already held it.

    The value is also put in this process's environment, so :func:`resolve_password_value` finds
    it without a re-read.
    """
    from db_ops.lib.secret_text import set_secret_text

    written = set_secret_text(data_dir, ref, value, key=_store_key(key, key_base64),
                              overwrite=overwrite)
    os.environ[ref] = value
    return bool(written)
