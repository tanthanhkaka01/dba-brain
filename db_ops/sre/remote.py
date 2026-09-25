"""Resolve how ``sre`` reaches an Ubuntu host - never reach it here.

``create-db-docker`` and ``move-db-docker`` run in ``common.cli`` since 0.23.0 and read nothing:
they take a host, a port, a username and a resolved password or key path. This module is the
``sre`` half that turns what the operator named - a ``--remote-password-ref``, or a target in
``db_instances.json`` - into that login. The SSH password is resolved like every other db_ops
secret: an explicit value wins, otherwise a ``password_ref`` decrypted from the encrypted secret
store with ``--key``/``--key-base64`` (or ``DB_OPS_SECRET_KEY``). The SSH connection itself is
:mod:`db_ops.common.docker_db.remote_host`.
"""

from __future__ import annotations

from pathlib import Path

from db_ops.common.data_sources import resolve_ssh_key, resolve_ssh_password
from db_ops.common.remote_exec import RemoteAccess, RemoteExecError
from db_ops.lib.ssh_errors import SshError


class RemoteHostError(RuntimeError):
    """The login for a host could not be resolved from what the operator gave."""


__all__ = ["RemoteHostError", "resolve_remote_ssh_password", "resolve_ssh_key", "resolve_ubuntu_login"]


def resolve_remote_ssh_password(
    *,
    password: str | None,
    password_ref: str | None,
    password_env: str | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    data_dir: str | Path | None = None,
) -> str:
    """Thin wrapper over :func:`db_ops.common.data_sources.resolve_ssh_password` (kept for callers that
    import it from here). Password resolution is a read of the encrypted store, so it lives with the data folder's
    one reader and every app shares it."""
    try:
        return resolve_ssh_password(
            password=password, password_ref=password_ref, password_env=password_env,
            key=key, key_base64=key_base64, data_dir=data_dir,
        )
    except SshError as exc:
        raise RemoteHostError(str(exc)) from exc


def resolve_ubuntu_login(target: str, *, data_dir=None) -> dict:
    """The ``ssh_login`` object for a machine ``db_instances.json`` already describes.

    The ``--remote-host/--remote-user/--remote-password-ref`` triple exists for a host that is
    not in the inventory yet — which is the normal case when *creating* an instance on a fresh
    VM. Operating on a host that already runs db_ops containers is the opposite case: the
    ``cmd_access`` block and its credential are there, and asking an operator to retype them is
    how two spellings of the same host end up in two runbooks.

    Resolution is the same three steps ``host_ops.resolve_host`` takes, out of the same files —
    ``data_sources`` for the record and the credentials, ``lib.cmd_access`` for what the block
    means — rather than a call into ``host_ops``, because an app does not import ``common``.
    Nothing here is a second interpretation of the block: the rules live in ``lib``, and both
    callers ask them. The password or the key path is resolved by ``RemoteAccess.from_json``, the
    same resolution a session would make, and the result goes to ``common.cli move-db-docker``
    in its request: that command reads nothing (0.23.0).
    """
    from db_ops.common.data_sources import load_remote_credentials, resolve_target_instance
    from db_ops.lib.cmd_access import resolve_cmd_access, resolve_cmd_credential, resolve_platform

    text = str(target or "").strip()
    if not text:
        raise RemoteHostError("a target is required (a server_id or an ip from db_instances.json).")
    try:
        instance = resolve_target_instance(text, data_dir=data_dir)
        platform = resolve_platform(instance)
        block = resolve_cmd_access(instance, platform=platform, host=str(instance.get("ip") or ""))
        credential = resolve_cmd_credential(block, load_remote_credentials(data_dir))
    except Exception as exc:  # noqa: BLE001 - every failure here is "cannot reach that host"
        raise RemoteHostError(f"{text}: {exc}") from exc

    if not block or not block.get("enabled", True):
        raise RemoteHostError(
            f"{text} has no usable cmd_access block in db_instances.json, so there is no way to "
            "reach the host. Add one (method ssh plus a credential_name)."
        )
    if platform == "windows" or str(block.get("method") or "") != "ssh":
        method = block.get("method") or "nothing"
        raise RemoteHostError(
            f"{text} is reached by {method} on {platform or 'an unknown platform'}; this needs an "
            "SSH-reachable Linux host, because docker and compose run there."
        )
    try:
        access = RemoteAccess.from_json(block, credential=credential, data_dir=data_dir)
    except RemoteExecError as exc:
        raise RemoteHostError(f"{text}: {exc}") from exc
    return {"host": access.host or text, "port": int(access.port or 22),
            "username": access.username, "password": access.password or "",
            "key_file": access.key_file or ""}
