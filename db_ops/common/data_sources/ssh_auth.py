"""Where an SSH key lives and where an SSH password comes from — both answers read ``data/``.

Split out of ``common/ssh.py`` on 2026-08-15, along the same line every other split in this
refactor followed: opening the connection is an operation and stayed in ``common``, while *finding
the key file* and *resolving the password* are reads of the data folder, and the data folder has
one reader. Four app-side imports of ``common.ssh`` existed only for these two functions.

Keys live in **``data/ssh_keys/``** — a bind-mounted, git-ignored folder that travels with the
worker's ``data`` mount. A caller passes a key by bare **name** (resolved inside that folder) or by
absolute path; a bare name is the normal form precisely so a config file does not have to know
where the folder is on this machine.
"""

from __future__ import annotations

import os
from pathlib import Path

from db_ops.common import data_sources
from db_ops.lib.ssh_errors import SshError

SSH_KEYS_DIRNAME = "ssh_keys"

__all__ = ["SSH_KEYS_DIRNAME", "resolve_ssh_key", "resolve_ssh_password", "ssh_keys_dir"]


def ssh_keys_dir(data_dir: str | Path | None = None) -> Path:
    """The folder holding SSH private keys: ``<data_dir>/ssh_keys``."""
    return data_sources._resolve_data_dir(data_dir) / SSH_KEYS_DIRNAME


def resolve_ssh_key(name_or_path: str, data_dir: str | Path | None = None) -> str:
    """Resolve an SSH private key given a bare filename (looked up in ``data/ssh_keys/``) or an
    absolute/relative path. Raises :class:`SshError` if the file does not exist."""
    text = str(name_or_path or "").strip()
    if not text:
        raise SshError("SSH key name/path is empty.")
    candidate = Path(text)
    # A bare name (no directory separators) is looked up in the keys folder.
    if candidate.name == text and not candidate.is_absolute():
        candidate = ssh_keys_dir(data_dir) / text
    candidate = candidate.expanduser()
    if not candidate.is_file():
        raise SshError(
            f"SSH key not found: {candidate}. Put the key in {ssh_keys_dir(data_dir)} "
            f"(pass its file name) or give an absolute path."
        )
    return str(candidate)


def resolve_ssh_password(
    *,
    password: str | None,
    password_ref: str | None = None,
    password_env: str | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    data_dir: str | Path | None = None,
) -> str:
    """Resolve an SSH password: explicit value > env var > encrypted secret store ref.

    ``password_env`` names an environment variable holding the value (nothing sensitive on
    argv); ``password_ref`` is a ref in the encrypted secret store, decrypted with
    ``key``/``key_base64`` or the ``DB_OPS_SECRET_KEY`` env. Raises :class:`SshError` when
    none yields a value."""
    if password:
        return password
    if password_env:
        env_value = os.environ.get(password_env, "").strip()
        if env_value:
            return env_value
    if not password_ref:
        raise SshError(
            "SSH needs a password (value / env var) or password_ref (a ref in the encrypted "
            "secret store), or use key-based auth instead."
        )
    from db_ops.lib.secret_text import resolve_cli_key

    secret_key: str | None = None
    try:
        secret_key = resolve_cli_key(key, key_base64)
    except Exception:  # noqa: BLE001 - invalid/missing key; try the env below.
        secret_key = None
    if not secret_key:
        secret_key = os.environ.get("DB_OPS_SECRET_KEY") or None
    if not secret_key:
        raise SshError(
            f"password_ref {password_ref} needs the secret-store passphrase "
            "(--key/--key-base64 or DB_OPS_SECRET_KEY)."
        )
    try:
        secrets = data_sources.load_secret_text(data_dir, key=secret_key)
    except Exception as exc:  # noqa: BLE001 - wrong key, corrupt store.
        raise SshError(f"Could not decrypt the secret store: {exc}") from exc
    value = (secrets.get(password_ref) or "").strip()
    if not value:
        raise SshError(f"Secret ref not found or empty in the secret store: {password_ref}")
    return value
