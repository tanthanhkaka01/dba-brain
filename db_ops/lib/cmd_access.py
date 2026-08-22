"""How to reach a *host*, as configuration — the ``cmd_access`` vocabulary.

The mirror of :mod:`db_ops.lib.sql_access`, and split out for the same reason on the same day.
Opening an SSH session and running a command are operations and stay in ``common.remote_exec`` /
``common.host_ops``; deciding what ``method: "winrm"`` *means*, which port it implies, and whether
a block needs a credential at all is a rule about values. Two consumers read that rule —
``metrics`` (to run a collector on the host) and ``host_ops`` (to restart it, control its services,
patch it) — and ``metrics`` reads it while loading its target list, in-process, once per target.

Nothing here connects to anything or reads a file; every function takes the block it was handed.

**A missing credential is not always an error, and the two cases that make it legitimate are the
reason this is a function rather than a lookup**: ``method: local`` has nothing to log in to, and
SSH key auth uses the node's own key. Anything else without a resolvable credential raises, because
an OS login is never guessed at.
"""

from __future__ import annotations

from typing import Any

from db_ops.lib.coerce import as_bool

PLATFORM_WINDOWS = "windows"
PLATFORM_LINUX = "linux"
SUPPORTED_PLATFORMS = {PLATFORM_WINDOWS, PLATFORM_LINUX}

#: ``local`` runs inside the db_ops container. With a *remote* host that silently reports the
#: container's own CPU and disk under that host's name, which is why
#: ``remote_exec.assert_local_host`` refuses the combination — see ``docs/04_metrics_engine.md``.
SUPPORTED_CMD_ACCESS_METHODS = {"local", "ssh", "winrm"}

__all__ = [
    "PLATFORM_LINUX",
    "PLATFORM_WINDOWS",
    "SUPPORTED_CMD_ACCESS_METHODS",
    "SUPPORTED_PLATFORMS",
    "infer_platform_from_os",
    "resolve_cmd_access",
    "resolve_cmd_credential",
    "resolve_platform",
]


def resolve_platform(item: dict[str, Any]) -> str:
    """The target's platform, from ``platform`` or inferred from the ``os`` text."""
    platform = str(item.get("platform") or "").strip().lower()
    if not platform:
        platform = infer_platform_from_os(str(item.get("os") or ""))
    if platform and platform not in SUPPORTED_PLATFORMS:
        raise RuntimeError(
            f"Unsupported platform '{platform}' for db instance {item.get('ip') or item.get('target_id')}."
        )
    return platform


def infer_platform_from_os(os_text: str) -> str:
    normalized = str(os_text or "").strip().lower()
    if normalized.startswith("windows"):
        return PLATFORM_WINDOWS
    if normalized.startswith("linux"):
        return PLATFORM_LINUX
    return ""


def resolve_cmd_access(item: dict[str, Any], *, platform: str, host: str) -> dict[str, Any]:
    """Normalize a db instance's ``cmd_access`` block (method, port, shell, platform).

    It validates only what is genuinely a config error — an unknown method — and leaves defaults
    to :meth:`remote_exec.RemoteAccess.from_json`, which is where they belong. The port and
    ``auth_type`` filled in here are the ones the *method* implies, not the session's.
    """
    raw = item.get("cmd_access")
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"cmd_access must be an object for db instance {item.get('ip') or item.get('target_id')}."
        )
    enabled = bool(raw.get("enabled", True))
    method = str(raw.get("method") or "").strip().lower()
    if enabled and method not in SUPPORTED_CMD_ACCESS_METHODS:
        raise RuntimeError(
            f"cmd_access.method must be one of {sorted(SUPPORTED_CMD_ACCESS_METHODS)}, "
            f"got '{method or '<missing>'}'."
        )
    resolved = dict(raw)
    resolved["enabled"] = enabled
    resolved["method"] = method
    resolved["host"] = str(raw.get("host") or host or "").strip()
    resolved["shell"] = str(raw.get("shell") or "").strip().lower()
    resolved["platform"] = platform
    if method == "winrm":
        resolved["port"] = int(raw.get("port") or 5985)
        resolved["ssl"] = as_bool(raw.get("ssl"), default=False)
    elif method == "ssh":
        resolved["port"] = int(raw.get("port") or 22)
        resolved["auth_type"] = str(raw.get("auth_type") or "key").strip().lower()
        key_file = str(raw.get("key_file") or "").strip()
        if key_file:
            resolved["key_file"] = key_file
    return resolved


def resolve_cmd_credential(
    cmd_access: dict[str, Any], groups: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The ``remote_credentials`` entry a ``cmd_access`` block names, or ``None``.

    ``groups`` is what ``data_sources.load_remote_credentials()`` returns — handed in rather than
    read here, because finding the file is a question about the machine and this is a question
    about the block.

    **A block that carries its own login is answered from itself**, and ``groups`` is never
    consulted. That is what makes ``run-cmd`` reachable for a host in no inventory at all: state
    ``username`` plus ``password`` (or ``password_ref``, which ``remote_exec`` resolves from the
    environment before the secret store) and the request is self-contained, the same way
    ``connection`` is for ``run-sql``. A ``credential_name`` still wins when both are present —
    naming an entry is asking for *that* entry, not for whatever else the block happens to hold.
    """
    if not cmd_access or not bool(cmd_access.get("enabled", True)):
        return None
    method = str(cmd_access.get("method") or "").lower()
    if method == "local":
        return None
    credential_name = str(cmd_access.get("credential_name") or "").strip()
    inline_user = str(cmd_access.get("username") or "").strip()
    if not credential_name and inline_user and (
        cmd_access.get("password") or cmd_access.get("password_ref")
    ):
        inline = {"credential_name": f"inline:{inline_user}", "username": inline_user}
        for key in ("password", "password_ref", "passphrase"):
            if cmd_access.get(key):
                inline[key] = cmd_access[key]
        return inline
    if not credential_name:
        if method == "ssh" and str(cmd_access.get("auth_type") or "key").strip().lower() != "password":
            return None
        raise RuntimeError(f"cmd_access.credential_name is required for method '{method}'.")
    for group in groups:
        host = str(group.get("host") or "").strip()
        if host and str(cmd_access.get("host") or "").strip() not in {"", host}:
            continue
        for credential in group.get("credentials", []) or []:
            if str(credential.get("credential_name") or "") == credential_name:
                return dict(credential)
    raise RuntimeError(f"Remote credential not found: {credential_name}")
