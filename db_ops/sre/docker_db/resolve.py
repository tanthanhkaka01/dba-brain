"""What ``sre`` looks up before it asks ``common.cli`` to build or move a lab instance.

``common.cli create-db-docker`` / ``move-db-docker`` read no configuration (0.23.0): the password
and the engine arrive in the request as values. Resolving them is this app's, because it is a
read of the encrypted secret store and of ``data/docker_db_connections.json`` - the data folder,
which an app reaches through ``common.data_sources`` and ``common`` itself does not reach at all.
"""

from __future__ import annotations

import os
from pathlib import Path

from db_ops.lib import field_names
from db_ops.lib.docker_db_spec import ENGINE_META
from db_ops.sre.docker_db import register_config


class DockerDbRequestError(RuntimeError):
    """The request for ``common.cli`` could not be assembled from what the operator gave."""


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
        from db_ops.common import data_sources
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


def registered_engine(name: str, *, data_dir: str | Path | None) -> str:
    """The engine the connection registry records for instance ``name``, or ``""``.

    Where the fact already lives; ``--engine`` is the escape hatch for an instance provisioned
    before the registry existed, or created by hand.
    """
    if not data_dir:
        return ""
    try:
        registry = register_config.load_registry(register_config.default_registry_path(data_dir))
    except (OSError, ValueError):
        return ""
    for entry in registry.get(register_config.REGISTRY_ROOT_KEY, []):
        if isinstance(entry, dict) and entry.get("id") == register_config.connection_id(name):
            # `db_type` since 0.22.0; a record written earlier says `engine`.
            engine = str(field_names.read(entry, "docker_db_connection", "db_type", "") or "")
            if engine in ENGINE_META:
                return engine
    return ""
