"""Complete a ``common.cli`` request from this node's ``data/`` - what an app does before it calls.

``common.cli`` works from its request alone and reads no configuration (rules R09, the operator
2026-09-26: *the app sends everything, so common.cli runs; common does not import lib to have lib
read the config instead*). A request naming only a ``server_id`` is therefore the app's to finish:
the login as a ``connection`` (a database) or an ``access`` block (a host), with the password in
it; the maintenance ``policy`` a host operation times itself by; the confirmation ``rules`` the
operation costs. This module reads ``db_instances.json``, ``users.json``, the secret store and the
policy files to do that - it is part of ``lib.data_sources``, the one reader of ``data/``. It is
imported by the apps, and in ``common`` only by a module whose job is the configuration itself
(R09's other kind - ``rotate-password`` and ``check-secret`` find a ref's server this way); an
operation in ``common`` never imports it, which ``tests/test_common_layers.py`` holds by name.

The requests it builds carry passwords, so they go to ``common.cli`` on stdin, which is how
``db_ops.transport.common_cli`` sends every request.
"""

from __future__ import annotations

from db_ops.lib import errors
import json
from pathlib import Path
from typing import Any, Mapping

from db_ops.lib import confirmation_ladder
from db_ops.lib import sql_access as sql_access_rules
from db_ops.lib.cmd_access import resolve_cmd_access, resolve_cmd_credential, resolve_platform
from db_ops.lib.data_sources import (
    _resolve_data_dir, find_database_credential, load_credentials, load_db_instances,
    load_remote_credentials, load_secret_text, load_sqlserver_instance_policy,
)
from db_ops.lib.data_sources.ssh_auth import resolve_ssh_key
from db_ops.lib.data_sources.target_resolve import (
    TargetResolveError, normalize_db_type, resolve_target_instance,
)
from db_ops.lib.paths import PACKAGED_CATALOGUE
from db_ops.lib.sql_text import resolve_password

__all__ = [
    "FILLS", "RequestFillError", "bridge_secrets", "bundle_secrets", "connection_from", "fill_request",
    "host_access",
    "instance_policy", "maintenance_policy", "operation_rules", "probe_address", "sql_connection",
]

MAINTENANCE_POLICY_FILE = "maintenance_policy.json"
EMERGENCY_OPERATIONS_FILE = "emergency_operations.json"

#: The facts about a machine a request may state beside its login - what decides the script
#: dialect and the tool. Copied off the inventory record, never guessed.
_PROFILE_KEYS = ("major_version", "os", "os_text", "platform", "os_major", "os_minor", "runtime")
#: Where a containerised engine runs - read by the host operations' runtime wrapper.
_RUNTIME_KEYS = ("container", "container_name", "pod", "namespace", "pod_container", "container_shell")


class RequestFillError(errors.NotConfigured):
    """This node's configuration cannot complete the request: an unknown server_id, no login."""


# --------------------------------------------------------------------------- #
# The pieces
# --------------------------------------------------------------------------- #
def sql_connection(target: str, *, credential_name: str = "", data_dir: str | Path | None = None,
                   secrets: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The ``connection`` block for a database named by ``server_id`` (or ``<db_type> <ip>``).

    The login is ``credential_name`` when the caller names one, else the instance's
    ``default_credential_name`` - the same choice ``run-sql`` made when it looked targets up
    itself. The password is resolved here, from the environment or the secret store.
    """
    try:
        instance = resolve_target_instance(str(target), data_dir=data_dir)
    except TargetResolveError as exc:
        raise RequestFillError(str(exc)) from exc
    db_type = normalize_db_type(instance.get("db_type"))
    server_id = str(instance.get("server_id") or target).strip()
    try:
        credential = find_database_credential(
            load_credentials(db_type, data_dir), server_id=server_id,
            credential_name=str(credential_name or "").strip()
            or str(instance.get("default_credential_name") or "").strip())
        password = resolve_password(credential, dict(secrets) if secrets is not None
                                    else load_secret_text(data_dir))
    except Exception as exc:  # noqa: BLE001 - CredentialNotFound, no key, unreadable store.
        raise RequestFillError(f"{server_id}: {exc}") from exc
    return connection_from(instance, credential, password, server_id=server_id)


def connection_from(instance: Mapping[str, Any], credential: Mapping[str, Any], password: str, *,
                    server_id: str = "") -> dict[str, Any]:
    """The ``connection`` block for an instance record and the login already chosen for it.

    For a caller that has both in hand - the SQL task runner resolves them itself - so the block is
    built one way whoever read the files (rules R11).
    """
    db_type = normalize_db_type(instance.get("db_type"))
    block: dict[str, Any] = {
        "db_type": db_type,
        "host": str(instance.get("ip") or ""),
        "port": int(instance.get("port") or 0) or None,
        "username": str(credential.get("username") or ""),
        "password": password,
        "server_id": str(server_id or instance.get("server_id") or ""),
        "credential_name": str(credential.get("credential_name") or ""),
        "role": str(credential.get("role") or ""),
        "instance_name": str(instance.get("instance_name") or ""),
        "service_name": str(instance.get("service_name") or ""),
        "sqlserver_driver": str(instance.get("sqlserver_driver") or ""),
        "sqlserver_tls_verify": instance.get("sqlserver_tls_verify") is True,
        "sql_access": dict(instance.get("sql_access") or {"method": "direct"}),
    }
    # SQL Server connects to master unless a request names a database: the inventory's field is
    # a service label there (rules R24). Every other engine connects to the one it records.
    if db_type != "sqlserver" and instance.get("database"):
        block["database"] = str(instance["database"])
    block.update({key: instance[key] for key in _PROFILE_KEYS if instance.get(key) not in (None, "")})
    return {key: value for key, value in block.items() if value not in (None, "")}


def bridge_secrets(sql_access_block: Mapping[str, Any] | None, secrets: Mapping[str, str]) -> dict[str, str]:
    """The secret values an 8i bridge's ``sql_access`` names (its signing secret, a connect string) -
    what ``run-sql`` needs stated, since it opens no store. Empty for a direct target."""
    refs = sorted(set(sql_access_rules.secret_refs(dict(sql_access_block or {})).values()))
    return {ref: str(secrets[ref]) for ref in refs if secrets.get(ref)}


def host_access(target: str, *, data_dir: str | Path | None = None,
                secrets: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The ``access`` block for a host named by ``server_id`` or ip: its ``cmd_access`` with the
    login resolved into it - a password, or an absolute key file - and the machine's facts."""
    instance = _host_instance(str(target), data_dir=data_dir)
    label = str(instance.get("server_id") or target)
    try:
        platform = resolve_platform(instance)
        block = resolve_cmd_access(instance, platform=platform, host=str(instance.get("ip") or ""))
        if not block or not block.get("enabled", True):
            raise RequestFillError(
                f"{label} has no usable cmd_access block in db_instances.json, so there is no way "
                "to reach the host. Add one (method ssh|winrm plus a credential_name).")
        needs_groups = bool(str(block.get("credential_name") or "").strip())
        credential = resolve_cmd_credential(block, load_remote_credentials(data_dir) if needs_groups else [])
    except RequestFillError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unknown method, a credential that is not there.
        raise RequestFillError(f"{label}: {exc}") from exc
    access = {key: value for key, value in block.items() if key != "credential_name"}
    if block.get("key_file"):
        access["key_file"] = resolve_ssh_key(str(block["key_file"]), data_dir)
    if credential:
        access["username"] = str(credential.get("username") or "")
        if credential.get("key_file"):
            access["key_file"] = resolve_ssh_key(str(credential["key_file"]), data_dir)
        if credential.get("password") or credential.get("password_ref"):
            try:
                access["password"] = resolve_password(
                    credential, dict(secrets) if secrets is not None else load_secret_text(data_dir))
            except Exception as exc:  # noqa: BLE001 - no key, unreadable store, missing ref.
                raise RequestFillError(f"{label}: {exc}") from exc
    # `auth_type` stays what the configuration says: this adds the secret it names, nothing else.
    access.update({key: instance[key] for key in (*_PROFILE_KEYS, *_RUNTIME_KEYS)
                   if instance.get(key) not in (None, "") and key not in access})
    return access


def probe_address(target: str, *, data_dir: str | Path | None = None) -> dict[str, Any]:
    """Where ``probe-host`` knocks: the host's address, its port and the facts that pick the probe.

    ``server_id`` rides along as the label of the answer; nothing here is a login.
    """
    instance = _host_instance(str(target), data_dir=data_dir)
    address: dict[str, Any] = {"host": str(instance.get("ip") or ""),
                               "server_id": str(instance.get("server_id") or target)}
    if instance.get("port"):
        address["port"] = int(instance["port"])
    if instance.get("db_type"):
        address["db_type"] = str(instance["db_type"])
    address.update({key: instance[key] for key in _PROFILE_KEYS if instance.get(key) not in (None, "")})
    return address


def maintenance_policy(server_id: str, *, data_dir: str | Path | None = None) -> dict[str, Any]:
    """This node's ``maintenance_policy.json``: its ``defaults`` with the server's own values over
    them. ``{}`` when the node has no file - the host operation's built-in defaults then apply."""
    path = _resolve_data_dir(data_dir) / MAINTENANCE_POLICY_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RequestFillError(f"{path} is not readable JSON: {exc}") from exc
    block = raw.get("maintenance_policy", raw) if isinstance(raw, dict) else {}
    policy = dict((block or {}).get("defaults") or {})
    policy.update(((block or {}).get("servers") or {}).get(str(server_id)) or {})
    return policy


def operation_rules(operation: str, *, data_dir: str | Path | None = None) -> dict[str, Any]:
    """What ``operation`` costs on this node: its own ladder, else the one the package ships.

    A ladder that is there and unreadable is **not** a reason to fall back: the operator has one,
    and pricing from another table would be pricing from one they are not looking at - so it is
    the strictest answer, as it always was.
    """
    own = _resolve_data_dir(data_dir) / EMERGENCY_OPERATIONS_FILE
    source = own if own.exists() else PACKAGED_CATALOGUE / EMERGENCY_OPERATIONS_FILE
    try:
        document = json.loads(source.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return dict(confirmation_ladder.STRICTEST)
    return confirmation_ladder.operation_rules(document, operation)


def instance_policy(*, data_dir: str | Path | None = None) -> dict[str, Any]:
    """``sqlserver_instance_policy.json`` - which server settings travel between instances."""
    try:
        return load_sqlserver_instance_policy(data_dir=data_dir)
    except (OSError, ValueError) as exc:
        raise RequestFillError(str(exc)) from exc


def bundle_secrets(bundle_dir: str | Path, *, secrets: Mapping[str, str]) -> dict[str, str]:
    """The secret values an instance-metadata bundle's placeholders need - its manifest's
    ``secret_refs``, each looked up in the store. A ref the store lacks is left out: the replay
    reports it missing and fails closed, which is its own decision to make."""
    manifest = Path(bundle_dir) / "manifest.json"
    try:
        refs = json.loads(manifest.read_bytes().decode("utf-8-sig")).get("secret_refs") or []
    except (OSError, ValueError, AttributeError):
        # No readable manifest is the replay's to report (it reads the same file first).
        return {}
    return {str(ref): str(secrets[ref]) for ref in refs if secrets.get(ref)}


# --------------------------------------------------------------------------- #
# One request
# --------------------------------------------------------------------------- #
_SQL = "connection"
_HOST = "access"
_HOST_POLICY = "policy"
_INSTANCE_POLICY = "instance_policy"
_RULES = "rules"
_BUNDLE_SECRETS = "secrets"
_BRIDGE_SECRETS = "bridge_secrets"
_ADDRESS = "address"

#: What each command needs stated, beside what its caller already put in the request. A command
#: missing here needs nothing filled.
FILLS: dict[str, tuple[str, ...]] = {
    "shrink-log": (_SQL, _RULES), "kill-spid": (_SQL, _RULES), "start-job": (_SQL, _RULES),
    "disable-job": (_SQL, _RULES),
    "list-databases": (_SQL,), "list-schemas": (_SQL,), "list-jobs": (_SQL,), "db-status": (_SQL,),
    "create-table-from-xlsx": (_SQL,), "trace-session": (_SQL,), "copy-schema": (_SQL,),
    "host-facts": (_HOST, _HOST_POLICY), "host-service": (_HOST, _HOST_POLICY, _RULES),
    "host-restart": (_HOST, _HOST_POLICY, _RULES),
    "sqlserver-precheck": (_HOST, _SQL, _HOST_POLICY),
    "sqlserver-apply-cu": (_HOST, _SQL, _HOST_POLICY, _RULES),
    "sqlserver-verify-build": (_HOST, _SQL, _HOST_POLICY),
    "sqlserver-export-instance": (_SQL, _INSTANCE_POLICY),
    "sqlserver-replay-instance": (_SQL, _INSTANCE_POLICY, _BUNDLE_SECRETS),
    "sqlserver-verify-instance": (_SQL,),
    "authorize": (_RULES,),
    # The four doors that still looked a server_id up until 0.24.0 (rules R09).
    "run-sql": (_SQL, _BRIDGE_SECRETS),
    "run-cmd": (_HOST,), "fetch-file": (_HOST,), "send-file": (_HOST,), "pack-files": (_HOST,),
    "relay-file": (_HOST,),
    "probe-host": (_ADDRESS,),
}


def fill_request(command: str, request: Mapping[str, Any], *, data_dir: str | Path | None = None,
                 secrets: Mapping[str, str] | None = None) -> dict[str, Any]:
    """``request`` with everything ``command`` needs stated, from its ``target`` / ``server_id``.

    What the request already states is kept: a caller that sent its own ``connection`` or
    ``rules`` meant those. Nothing is filled for a command :data:`FILLS` does not list.
    """
    filled = dict(request)
    needs = FILLS.get(command, ())
    if not needs:
        return filled
    if secrets is None and any(kind in needs for kind in (_SQL, _HOST, _BUNDLE_SECRETS)) and command != "relay-file":
        secrets = _secrets_once(data_dir)
    target = str(filled.get("target") or filled.get("server_id") or "").strip()
    if command == "relay-file":
        # Two hosts, each finished from its own target - the host login, as for fetch-file.
        for side in ("source", "destination"):
            endpoint = dict(filled.get(side) or {})
            if "access" not in endpoint and endpoint.get("target"):
                endpoint["access"] = host_access(str(endpoint["target"]), data_dir=data_dir, secrets=secrets)
            filled[side] = endpoint
        return filled
    if command == "copy-schema":
        for side in ("source", "destination"):
            endpoint = dict(filled.get(side) or {})
            if "connection" not in endpoint and endpoint.get("target"):
                endpoint["connection"] = sql_connection(
                    str(endpoint["target"]), credential_name=str(endpoint.get("credential_name") or ""),
                    data_dir=data_dir, secrets=secrets)
            filled[side] = endpoint
        return filled
    if _SQL in needs and "connection" not in filled:
        filled["connection"] = sql_connection(
            _need(target, command), credential_name=str(filled.get("credential_name") or filled.get("user_ref") or ""),
            data_dir=data_dir, secrets=secrets)
    if _HOST in needs and "access" not in filled:
        filled["access"] = host_access(_need(target, command), data_dir=data_dir, secrets=secrets)
    if _HOST_POLICY in needs and "policy" not in filled:
        filled["policy"] = maintenance_policy(target, data_dir=data_dir)
    if _INSTANCE_POLICY in needs and "policy" not in filled:
        filled["policy"] = instance_policy(data_dir=data_dir)
    if _RULES in needs and "rules" not in filled:
        filled["rules"] = operation_rules(_operation(command, filled), data_dir=data_dir)
    if _BRIDGE_SECRETS in needs and "secrets" not in filled:
        # An 8i target is reached through a bridge whose token is signed with a stored secret;
        # run-sql opens no store, so the refs its sql_access names travel with the request.
        block = filled.get("sql_access") or (filled.get("connection") or {}).get("sql_access") or {}
        if sql_access_rules.secret_refs(dict(block)):
            filled["secrets"] = bridge_secrets(block, secrets if secrets is not None else _secrets_once(data_dir))
    if _ADDRESS in needs and not str(filled.get("host") or "").strip():
        filled.update(probe_address(_need(target, command), data_dir=data_dir))
    if _BUNDLE_SECRETS in needs and "secrets" not in filled and filled.get("bundle_dir"):
        filled["secrets"] = bundle_secrets(str(filled["bundle_dir"]), secrets=secrets or {})
    return filled


def _operation(command: str, request: Mapping[str, Any]) -> str:
    """The ladder entry a command is priced by - its own name, except the two that say which."""
    if command == "authorize":
        return str(request.get("operation") or "")
    if command == "host-service":
        return f"host-service ({str(request.get('action') or '').strip().lower()})"
    return command


def _need(target: str, command: str) -> str:
    if not target:
        raise RequestFillError(f"{command}: the request names no target (a server_id) to fill it from.")
    return target


def _secrets_once(data_dir: str | Path | None) -> dict[str, str]:
    try:
        return load_secret_text(data_dir)
    except Exception as exc:  # noqa: BLE001 - no key, unreadable store: say which, once.
        raise RequestFillError(f"the secret store could not be read: {exc}") from exc


def _host_instance(spec: str, *, data_dir: str | Path | None) -> dict[str, Any]:
    """The instance a host spec names - server_id / triple first, then a bare ip, as the host
    operations always read it: an OS-only entry is reached by ip far more often than by id."""
    try:
        return resolve_target_instance(spec, data_dir=data_dir)
    except TargetResolveError as exc:
        matches = [item for item in load_db_instances(data_dir) if str(item.get("ip") or "").strip() == spec]
        if not matches:
            raise RequestFillError(str(exc)) from exc
        return dict(matches[0])
