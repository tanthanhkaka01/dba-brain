"""A secret named in a JSON object, resolved: an explicit value, an env var, or a ref it was handed.

``common.remote_exec``'s until 0.24.0; ``lib``'s since, because an app resolves the same fields -
``sre`` hands whole config sections to PowerShell and Ansible with every ``*_password_ref`` turned
into its value - and an app may import ``lib`` but not ``common`` (rules R03). ``remote_exec`` asks
this and raises its own error class with the same words.

**It opens no store** (0.24.0, rules R09). The encrypted store on disk used to be its last resort,
which made every ``common`` transport that asked it a reader of ``data/`` one import away - the
guard could only see that by naming this module. The store is read by
:func:`db_ops.lib.data_sources.resolve_secret_value`, the same rule with the node's store behind it,
for the apps; ``common`` states its secrets or finds them in the environment.
"""

from __future__ import annotations

import os
from typing import Any, Mapping


class SecretValueError(RuntimeError):
    """A secret the object names could not be resolved."""


def resolve_secret_value(
    values: dict[str, Any],
    *,
    secrets: Mapping[str, str] | None = None,
    value_key: str = "password",
    env_key: str = "password_env",
    ref_key: str = "password_ref",
) -> str:
    """Resolve a secret from a JSON object: explicit value > env var > a ref it was handed.

    ``<ref_key>`` is looked up in ``secrets`` first (values the caller already holds), then in the
    environment (refs double as env var names across db_ops). Returns "" when the object names no
    secret at all - key-based SSH auth is a valid no-password case, so an empty result is not an
    error here. A ref found in neither raises :class:`SecretValueError`.
    """
    explicit = str(values.get(value_key) or "")
    if explicit:
        return explicit
    env_name = str(values.get(env_key) or "").strip()
    if env_name:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            return env_value
    ref = str(values.get(ref_key) or "").strip()
    if not ref:
        return ""
    if secrets and str(secrets.get(ref) or "").strip():
        return str(secrets[ref])
    env_value = os.environ.get(ref, "").strip()
    if env_value:
        return env_value
    raise SecretValueError(
        f"Password ref {ref!r} is not among the secrets stated with it, nor in the environment.")
