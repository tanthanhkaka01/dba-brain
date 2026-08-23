"""Shared SSH transport + credential resolution (db_ops.common).

Every place that reaches an Ubuntu host over SSH opens its paramiko connection through this one
module, so the connect rules — which auth paths are allowed, and how a failure is classified —
are defined once. It connects to no app and imports no app.

**What is left here is the transport.** The credential *resolution* this module used to own is
``db_ops.common.data_sources.ssh_auth`` (finding the key under ``data/ssh_keys/``, resolving a
password from a value, an env var or the encrypted store) and the exception vocabulary is
``db_ops.lib.ssh_errors``; both are re-exported below, so a caller that opens a client still finds
everything under this name. The split happened because four app-side modules imported this one for
nothing but a key path or an error class — see ``docs/13_common.md``.

Failures are classified where the paramiko exception type is still in hand: ``SshAuthError`` (the
host answered and said no) and ``SshConnectError`` / ``SshTimeoutError`` (nothing answered) are
three different next actions, and once a failure is a string nobody can tell them apart without
pattern-matching the message.
"""

from __future__ import annotations

import socket
from pathlib import Path

# The two halves that are not transport, re-exported so this module's published surface is
# unchanged for callers that legitimately open a client. Finding the key file and resolving the
# password are reads of `data/`, which has one reader; the four exception names are vocabulary an
# app may need without importing paramiko at all. Both moved on 2026-08-15.
from db_ops.lib.packaging import install_hint
from db_ops.common.data_sources import (  # noqa: F401 - one definition, see that module
    SSH_KEYS_DIRNAME,
    resolve_ssh_key,
    resolve_ssh_password,
    ssh_keys_dir,
)
from db_ops.lib.paths import DEFAULT_DATA_DIR, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.ssh_errors import (  # noqa: F401 - one definition, see that module
    SshAuthError,
    SshConnectError,
    SshError,
    SshTimeoutError,
)


def open_ssh_client(
    host: str,
    user: str,
    *,
    port: int = 22,
    password: str | None = None,
    key_filename: str | None = None,
    timeout: int = 30,
    allow_agent_fallback: bool = False,
    announce: bool = True,
):
    """Open a paramiko SSH client to ``user@host`` using a key file or a password.

    An auth path is required unless ``allow_agent_fallback`` is set, which lets paramiko
    fall back to the SSH agent and the user's default keys — for hosts configured with
    key auth outside db_ops. For explicit key auth, agent/interactive fallbacks stay
    disabled so a wrong key fails loudly instead of silently trying something else.

    Connect failures are raised as the specific subclass (:class:`SshAuthError`,
    :class:`SshTimeoutError`, :class:`SshConnectError`) so callers can tell "wrong
    password" from "host unreachable" without pattern-matching the message."""
    if not password and not key_filename and not allow_agent_fallback:
        raise SshError("open_ssh_client needs either a password or a key_filename.")
    try:
        import paramiko  # type: ignore[import]
    except ImportError as exc:
        raise SshError(
            "paramiko is required for SSH. Install it with: " + install_hint("ssh")
        ) from exc
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if announce:
        print(f"Connecting to {user}@{host}:{port} ...", flush=True)
    connect_kwargs: dict = dict(hostname=host, port=int(port), username=user,
                                timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
    if key_filename:
        connect_kwargs["key_filename"] = key_filename
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"] = False
        if password:  # a passphrase-protected key may still want the passphrase
            connect_kwargs["password"] = password
    elif password:
        connect_kwargs["password"] = password
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"] = False
    try:
        client.connect(**connect_kwargs)
    except Exception as exc:  # noqa: BLE001 - auth/network; classify, then surface cleanly.
        raise _connect_error(
            paramiko, exc, user=user, host=host, port=port, timeout=timeout,
            offered_password=bool(password), offered_key=bool(key_filename),
        ) from exc
    return client


def _connect_error(
    paramiko,
    exc: Exception,
    *,
    user: str,
    host: str,
    port: int,
    timeout: int,
    offered_password: bool = True,
    offered_key: bool = True,
) -> SshError:
    """Map a paramiko connect exception to the SshError subclass that says what to fix.

    Auth vs unreachable is the distinction that decides an operator's next step (fix the
    credential, or open the port), so it is made here where the exception type is still
    available rather than by string-matching downstream.

    `offered_password` / `offered_key` are what we actually sent, and they separate *the
    credential is wrong* from **we had no credential to send**. The second case reaches paramiko
    as `No authentication methods available`, which reads like a server-side refusal and is
    really a configuration gap: `auth_type` defaults to `key` for SSH, so a `cmd_access` block
    written with a password and no `auth_type` sends nothing at all. That cost an afternoon here
    on 2026-08-23 against a container whose password was correct the whole time.
    """
    where = f"{user}@{host}:{port}"
    auth_exception = getattr(paramiko, "AuthenticationException", None)
    if auth_exception is not None and isinstance(exc, auth_exception):
        if not offered_password and not offered_key:
            return SshAuthError(
                f"SSH authentication failed for {where}: no credential was sent. "
                f"SSH defaults to auth_type 'key', so a cmd_access block with a password but no "
                f"auth_type offers nothing. Set \"auth_type\": \"password\" on the cmd_access "
                f"block (with credential_name naming the stored password), or set \"key_file\" "
                f"for key auth. Detail: {exc}"
            )
        return SshAuthError(f"SSH authentication failed for {where}: {exc}")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return SshTimeoutError(f"SSH connect to {where} timed out after {timeout} seconds.")
    unreachable = (
        getattr(getattr(paramiko, "ssh_exception", None), "NoValidConnectionsError", None),
        ConnectionRefusedError,
        OSError,
    )
    if isinstance(exc, tuple(item for item in unreachable if item is not None)):
        return SshConnectError(
            f"SSH connection to {host}:{port} failed. Check that port {port} is open and the "
            f"SSH server is running. Detail: {exc}"
        )
    return SshError(f"SSH connect to {where} failed: {exc}")
