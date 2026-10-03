"""A secret named in a JSON object, resolved: an explicit value, or a ref among the secrets handed over.

``common.remote_exec``'s until 0.24.0; ``lib``'s since, because an app resolves the same fields -
``sre`` hands whole config sections to PowerShell and Ansible with every ``*_password_ref`` turned
into its value - and an app may import ``lib`` but not ``common`` (rules R03). ``remote_exec`` asks
this and raises its own error class with the same words.

**It opens no store** (0.24.0, rules R09). The encrypted store on disk used to be its last resort,
which made every ``common`` transport that asked it a reader of ``data/`` one import away - the
guard could only see that by naming this module. The store is read by
:func:`db_ops.lib.data_sources.resolve_secret_value`, the same rule with the node's store behind it,
for the apps; ``common`` is handed its secrets.

**No environment lookup** (owner decision G3.5, 2026-10-01). A request named an environment variable
to read its password from (``password_env``), and a ref missing from the secrets was looked up in the
environment under its own name: the node's environment answered for whatever a request asked, which
is how ``password_env: DB_OPS_SECRET_KEY`` sent the passphrase to a host the caller chose (F12.2).
``password_env`` is read as everywhere else in the tool - the old spelling of ``password_ref``, a
key in the secret store.
"""

from __future__ import annotations

from db_ops.lib import errors
from typing import Any, Mapping


class SecretValueError(errors.NotConfigured):
    """A secret the object names could not be resolved."""


#: Variables a request may never name as "the password": the node's own keys. A request
#: `{"host": "evil.example", "password_env": "DB_OPS_SECRET_KEY"}` sent the passphrase to every
#: credential to a server the caller chose, as an SSH password (review 0.25.0, F12.2).
_NODE_SECRETS = frozenset({"DB_OPS_SECRET_KEY", "DB_OPS_KEY_BASE64", "TELEGRAM_BOT_TOKEN"})


def _readable_env(name: str) -> bool:
    upper = name.upper()
    return not (upper in _NODE_SECRETS or upper.endswith("BOT_TOKEN"))


def resolve_secret_value(
    values: dict[str, Any],
    *,
    secrets: Mapping[str, str] | None = None,
    value_key: str = "password",
    env_key: str = "password_env",
    ref_key: str = "password_ref",
) -> str:
    """Resolve a secret from a JSON object: the explicit value, or its ref among ``secrets``.

    ``<env_key>`` is the old spelling of ``<ref_key>`` and is read as one; both naming different refs
    is refused, not settled by an order. Returns "" when the object names no secret at all -
    key-based SSH auth is a valid no-password case, so an empty result is not an error here. A ref
    ``secrets`` does not hold raises :class:`SecretValueError`; the environment is not asked.
    """
    explicit = str(values.get(value_key) or "")
    if explicit:
        return explicit
    ref = str(values.get(ref_key) or "").strip()
    old_spelling = str(values.get(env_key) or "").strip()
    if ref and old_spelling and ref != old_spelling:
        raise SecretValueError(
            f"{ref_key} {ref!r} and {env_key} {old_spelling!r} name two different secrets; state one.")
    ref = ref or old_spelling
    if not ref:
        return ""
    if not _readable_env(ref):
        raise SecretValueError(
            f"{ref!r} is one of this node's own keys - it is never a password.")
    if secrets and str(secrets.get(ref) or "").strip():
        return str(secrets[ref])
    raise SecretValueError(f"Password ref {ref!r} is not among the secrets stated with it.")
