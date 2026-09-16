r"""Registering the OS login of one host, as a single operation.

The mirror of :mod:`db_ops.common.instance_admin`, for the credential that logs in to the
*machine* rather than to a database. Until this existed there was no command at all: 18 of this
estate's 22 `remote_credentials` groups were copied between nodes by hand, and a host-only record
registered with `instance-add` was a target nothing could log in to, because the second half of
its configuration had no command to write it.

Three fields fail in ways that do not name themselves, and all three are the reason this is a
command rather than a documented hand-edit:

* **`cmd_access.method: "local"` runs inside the db_ops container.** Pointed at a remote host it
  reports the *container's* CPU, disk and uptime under that host's name - not an error, a wrong
  answer. Refused here rather than at collection time.
* **`auth_type` defaults to `key` on SSH.** A block that means password authentication and does
  not say so resolves to no credential at all (`cmd_access.resolve_cmd_credential` returns None
  for key auth, deliberately, because the node's own key is the login), so the password that was
  carefully encrypted is never read. Required explicitly whenever a password is involved.
* **`platform` belongs on the instance, not inside `cmd_access`.** `resolve_cmd_access` writes it
  into the resolved block from the record, so a copy inside the raw block is either redundant or
  a second, disagreeing answer.

**The password never touches the disk unencrypted**, the same as `instance-add`: read the store,
add the entry, write the store back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db_ops.common import data_sources
from db_ops.common.instance_admin import InstanceAdminError, _read, _store_secret, _write
from db_ops.lib.cmd_access import SUPPORTED_CMD_ACCESS_METHODS, SUPPORTED_PLATFORMS

#: What each access method listens on when the caller does not say. The same numbers
#: `db_ops.lib.cmd_access.resolve_cmd_access` fills in, stated here so the file that is written
#: reads the way a hand-written one does instead of relying on a default at load time.
DEFAULT_CMD_PORTS: dict[str, int] = {"ssh": 22, "winrm": 5985}

#: The shell each platform is driven through. Wrong here is not subtle - a PowerShell collector
#: handed to `bash` fails on its first line - but it is another field nobody should have to know.
DEFAULT_SHELL: dict[str, str] = {"windows": "powershell", "linux": "bash"}

#: `role` on every existing entry in this estate. Kept as the default rather than invented per
#: call, because `secret_check` and the credential listings group on it.
DEFAULT_ROLE = "REMOTE"


USAGE = r"""usage: python -m db_ops.common.cli remote-credential-add <json>|@<file>|- [--key-base64 ...]

Registers the OS login of ONE host: the users.json remote_credentials entry, the secret behind
it, and - when you name a method - the cmd_access block on the instance that points at it.
The password never reaches the disk in the clear.

  server_id      required. The machine's id in db_instances.json; the only join key
  username       required. DOMAIN\user and .\user are both fine
  host           optional. Defaults to that server_id's ip in db_instances.json
  password         stored straight into the ENCRYPTED store; never written in clear
  password_ref     the name of a secret that already exists
  auth_type      ssh: password | key. REQUIRED with a password - it defaults to `key`,
                 and a key-auth block resolves to NO credential, so a password given
                 without this is encrypted, stored, and never read
  credential_name  optional; derived as remote_<last two octets>_<user>
  role           optional; default REMOTE
  replace        overwrite an existing credential_name instead of refusing

Give a method to wire the instance up in the same call:

  method         ssh | winrm. Without it, only the credential is written and nothing
                 points at it yet
  platform       windows | linux. Goes on the INSTANCE record, never inside cmd_access
  port           optional; 22 for ssh, 5985 for winrm
  shell          optional; powershell on windows, bash on linux
  ssl            winrm only; default false
  key_file       ssh key auth only
  enabled        optional; default true

  method "local" is refused for anything but this machine: it runs inside the db_ops
  container and would report the container's own CPU under the host's name.

Neither half is guessed at. Registering the credential and not the block leaves a login
nothing reads; the response says which of the two happened.
"""


def _slug(text: str, *, upper: bool) -> str:
    """One naming rule for both derived names, taken from what the estate already writes.

    `remote_100.104_admin` beside `REMOTE_172_17_100_104_ADMIN` is not two conventions but one
    seen twice: the credential name keeps dots and hyphens and lowercases, the secret ref
    flattens everything to underscores and shouts. Derived rather than typed because the pair
    appears in three files and nothing checks that a hand-typed one agrees with the others.
    """
    if upper:
        out = "".join(char if char.isalnum() else "_" for char in str(text)).upper()
    else:
        out = "".join(char if (char.isalnum() or char in ".-") else "_" for char in str(text)).lower()
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def _default_credential_name(host: str, username: str) -> str:
    """``remote_100.104_admin`` — the last two octets, because that is how this estate reads."""
    parts = str(host).split(".")
    short = ".".join(parts[-2:]) if len(parts) == 4 and all(p.isdigit() for p in parts) else host
    return f"remote_{_slug(short, upper=False)}_{_slug(username, upper=False)}"


def _default_password_ref(host: str, username: str) -> str:
    """``REMOTE_172_17_100_104_ADMIN`` — the whole host, flattened."""
    return f"REMOTE_{_slug(host, upper=True)}_{_slug(username, upper=True)}"


def _instance_for(instances: dict[str, Any], server_id: str) -> dict[str, Any] | None:
    for item in instances.get("db_instances") or []:
        if isinstance(item, dict) and str(item.get("server_id")) == server_id:
            return item
    return None


def add_remote_credential(request: dict[str, Any] | None = None, *,
                          data_dir: str | Path | None = None,
                          key: str | None = None) -> dict[str, Any]:
    """Register one OS login, and optionally the ``cmd_access`` block that names it."""
    payload = dict(request or {})
    root = Path(data_dir) if data_dir else data_sources.DEFAULT_DATA_DIR

    server_id = str(payload.get("server_id") or "").strip()
    username = str(payload.get("username") or "").strip()
    missing = [name for name, value in (("server_id", server_id), ("username", username))
               if not value]
    if missing:
        raise InstanceAdminError(
            f"missing required field(s): {', '.join(missing)}. An OS login needs at least a "
            "server_id (the machine it belongs to) and a username.")

    instances_path = data_sources.db_instances_path(root)
    instances = _read(instances_path, "db_instances")
    instance = _instance_for(instances, server_id)

    host = str(payload.get("host") or "").strip()
    if not host:
        host = str((instance or {}).get("ip") or "").strip()
    if not host:
        raise InstanceAdminError(
            f"no host given and {server_id} is not in {instances_path.name}, so there is nothing "
            "to take an ip from. Pass host, or register the machine with instance-add first.")

    password = payload.get("password")
    password_ref = str(payload.get("password_ref") or "").strip()
    if password is not None and password_ref:
        raise InstanceAdminError(
            "give either password (stored encrypted here) or password_ref (a secret that already "
            "exists), not both - two answers to 'which secret' is how the wrong one gets used.")
    if password is not None and not key:
        raise InstanceAdminError(
            "a password was given but no passphrase is available to encrypt it. Export "
            "DB_OPS_SECRET_KEY, or pass --key-base64. Nothing was written.")

    method = str(payload.get("method") or "").strip().lower()
    auth_type = str(payload.get("auth_type") or "").strip().lower()
    has_secret = password is not None or bool(password_ref)

    if method:
        if method not in SUPPORTED_CMD_ACCESS_METHODS:
            raise InstanceAdminError(
                f"method must be one of {sorted(SUPPORTED_CMD_ACCESS_METHODS)}, got {method!r}.")
        if method == "local":
            raise InstanceAdminError(
                "method 'local' runs the command inside the db_ops container, so it would report "
                f"the container's own CPU, disk and uptime under {host}'s name - a wrong answer "
                "rather than a failure. Use ssh or winrm with this credential.")
        if method == "ssh" and has_secret and auth_type != "password":
            raise InstanceAdminError(
                "a password was given for an ssh host and auth_type is not 'password'. auth_type "
                "defaults to 'key', and a key-auth block resolves to NO credential at all - the "
                "password would be encrypted, stored, and never read. Pass "
                "auth_type: \"password\", or drop the password and use key auth.")
    if auth_type and auth_type not in {"password", "key"}:
        raise InstanceAdminError(f"auth_type must be 'password' or 'key', got {auth_type!r}.")

    platform = str(payload.get("platform") or (instance or {}).get("platform") or "").strip().lower()
    if platform and platform not in SUPPORTED_PLATFORMS:
        raise InstanceAdminError(
            f"platform must be one of {sorted(SUPPORTED_PLATFORMS)}, got {platform!r}.")

    credential_name = str(payload.get("credential_name") or "").strip() or \
        _default_credential_name(host, username)
    ref = password_ref or (_default_password_ref(host, username) if password is not None else "")

    users_path = data_sources.users_path(root)
    users = _read(users_path, "remote_credentials")
    users.setdefault("remote_credentials", [])
    group = next((item for item in users["remote_credentials"]
                  if isinstance(item, dict) and str(item.get("server_id")) == server_id), None)
    existing_credential = next(
        (item for item in ((group or {}).get("credentials") or [])
         if str(item.get("credential_name") or "") == credential_name), None)
    if existing_credential and not payload.get("replace"):
        raise InstanceAdminError(
            f"{credential_name} already exists under {server_id} in {users_path.name}. Pass "
            "replace to overwrite it - a silent overwrite of a login somebody else added is not "
            "something to guess at.")

    written: list[str] = []
    # The secret first, unlike `instance-add`, which writes users.json before it. A wrong
    # passphrase is the likeliest failure of this whole command, and failing it after the
    # credential entry is on disk leaves exactly the half-configured state `check-credentials`
    # exists to find - reached by the command meant to prevent it.
    if password is not None:
        written.append(Path(_store_secret(root, ref, str(password), key)).name)

    credential = {
        "credential_name": credential_name,
        "username": username,
        "password_ref": ref,
        "role": str(payload.get("role") or DEFAULT_ROLE),
        "account_owner": str(payload.get("account_owner") or ""),
        "notes": str(payload.get("notes") or ""),
    }
    if group is None:
        group = {"server_id": server_id, "host": host,
                 "server_description": str(payload.get("server_description") or ""),
                 "credentials": []}
        users["remote_credentials"].append(group)
    else:
        group["host"] = host
        if payload.get("server_description"):
            group["server_description"] = str(payload["server_description"])
    # The credential is replaced inside the group, never the group itself. A machine legitimately
    # carries several OS logins - an admin account and a service account - and replacing the group
    # would delete the others to add one.
    group["credentials"] = [item for item in (group.get("credentials") or [])
                            if str(item.get("credential_name") or "") != credential_name]
    group["credentials"].append(credential)
    _write(users_path, users)
    written.append(users_path.name)

    wired = False
    if method:
        if instance is None:
            raise InstanceAdminError(
                f"a method was given but {server_id} is not in {instances_path.name}, so there is "
                "no record to put a cmd_access block on. The credential was written; register the "
                "machine with instance-add and run this again with replace.")
        # A cmd_access block that is already there and already OFF stays off. Turning it on is a
        # decision somebody made in the other direction - on this estate two Windows hosts carry
        # `enabled: false` because their WinRM auth fails, and re-registering the credential put
        # two failing collectors back into every scan. Measured 2026-09-15, rebuilding a node one
        # command at a time: those two records were the only difference from the node it was
        # reproducing. A block being written for the FIRST time still defaults to on - you have
        # just wired it up, and a new block nobody enables reaches nothing.
        _existing = instance.get("cmd_access")
        _was_off = isinstance(_existing, dict) and _existing.get("enabled") is False
        block: dict[str, Any] = {
            "enabled": bool(payload.get("enabled", not _was_off)),
            "method": method,
            "host": host,
            "shell": str(payload.get("shell") or DEFAULT_SHELL.get(platform, "")),
            "port": int(payload.get("port") or DEFAULT_CMD_PORTS[method]),
            "credential_name": credential_name,
        }
        if method == "ssh":
            # Written even when it is the default, because the default is the one that silently
            # means "no password". A block that states it can be read; one that omits it cannot.
            block["auth_type"] = auth_type or "key"
            if payload.get("key_file"):
                block["key_file"] = str(payload["key_file"])
        else:
            block["ssl"] = bool(payload.get("ssl", False))
        instance["cmd_access"] = block
        # On the record, never inside the block - see the module docstring.
        if platform:
            instance["platform"] = platform
        _write(instances_path, instances)
        written.append(instances_path.name)
        wired = True

    return {
        "server_id": server_id,
        "host": host,
        "credential_name": credential_name,
        "username": username,
        "password_ref": ref,
        "password_stored_encrypted": bool(password is not None),
        "replaced": bool(existing_credential),
        "cmd_access_written": wired,
        "method": method,
        "files_written": written,
        "next": [
            f"db-ops common.cli check-secret '{{\"server_id\": \"{server_id}\"}}'",
            f"db-ops common.cli host-facts '{{\"server_id\": \"{server_id}\"}}'",
        ] if wired else [
            "no cmd_access block was written, so nothing reaches this host yet - "
            "run again with method (ssh | winrm) to wire it up",
        ],
    }
