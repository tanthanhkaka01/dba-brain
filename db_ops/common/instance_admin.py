r"""Registering one database to monitor, as a single operation.

Until 2026-09-10 this took **four hand-edits and a plaintext password on disk**, and it was the
first real thing a new user had to do:

1. add an object to ``data/db_instances.json`` — eight fields, three of which fail in ways that do
   not name themselves;
2. add a matching group to ``data/users.json``, keyed on the same ``server_id``;
3. write the password into ``secrets/secret_text.json`` **in the clear**;
4. run ``encrypt-secret`` to turn that into ``data/encrypted_secret_text.json``, and remember to
   delete the plaintext.

Four files for one idea, with the identity (``server_id``) repeated in two of them and the
credential name repeated in three. Nothing checked that they agreed, and the failure mode of
getting it wrong is a target that resolves to no credential — reported, eventually, by
``check-credentials``, which is a different command run at a different time.

**The password never touches the disk unencrypted here.** Step 3 above is the one that cannot be
undone by deleting a file afterwards: a plaintext secret that existed for ten seconds is a
plaintext secret that is in the editor's undo history, the shell's history, and whatever backed the
directory up in between. This reads the encrypted store, adds the entry, and writes the store back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db_ops.common import data_sources
from db_ops.lib import secret_text as _secret_text
from db_ops.lib.json_io import atomic_write_text

#: Fields every target needs, whatever the engine. Anything else is optional and passed through, so
#: a field this module has never heard of still reaches `db_instances.json` rather than being
#: silently dropped — the inventory grows faster than any allow-list of keys would.
REQUIRED_FIELDS: tuple[str, ...] = ("server_id", "db_type", "ip")

#: What each engine listens on when the caller does not say. Stated rather than defaulted to 1433
#: for everything, because a wrong port fails as a timeout, which reads as "the host is down".
DEFAULT_PORTS: dict[str, int] = {
    "sqlserver": 1433, "oracle": 1521, "postgresql": 5432, "postgres": 5432, "mysql": 3306,
}


#: The command's help, beside the behaviour it describes.
USAGE = """usage: python -m db_ops.common.cli instance-add <json>|@<file>|- [--key-base64 ...]

Registers ONE database to monitor: the inventory record, its credential, and the secret behind it.
Replaces four hand-edits (db_instances.json, users.json, secrets/secret_text.json, encrypt-secret)
with one call, and the password never reaches the disk in the clear.

  server_id      required. Its name everywhere - reports, alerts, commands
  db_type        required. sqlserver | oracle | postgresql | mysql
  ip             required
  port           optional. Defaults per engine (1433 / 1521 / 5432 / 3306)
  username       the login. With it, give exactly one of:
  password         stored straight into the ENCRYPTED store; never written in clear
  password_ref     the name of a secret that already exists
  credential_name  optional; derived from server_id + db_type when absent
  replace        overwrite an existing server_id instead of refusing

Anything else in the object is passed through to the inventory record, so major_version,
service_name, env, platform, cmd_access and note all reach it unchanged.

Three fields fail in ways that do not name themselves, and the same warnings apply here as in the
guide: service_name is a LABEL and not a database, major_version selects the query variant, and
default_credential_name is a reference rather than a password.
"""


class InstanceAdminError(RuntimeError):
    """The target cannot be registered as asked."""


def _read(path: Path, root_key: str) -> dict[str, Any]:
    if not path.exists():
        return {root_key: []}
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except ValueError as exc:
        raise InstanceAdminError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InstanceAdminError(f"{path.name} is not a JSON object.")
    data.setdefault(root_key, [])
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _default_credential_name(server_id: str, db_type: str) -> str:
    """A credential name derived from the target, so the two cannot disagree.

    Shouty and underscored because that is how every ref in a real estate reads, and because the
    same string is used as the secret ref — a lowercase one beside nine uppercase ones is the kind
    of difference that survives review and fails at connect time.
    """
    engine = {"postgres": "PG", "postgresql": "PG", "sqlserver": "MSSQL",
              "oracle": "ORA", "mysql": "MYSQL"}.get(str(db_type).lower(), "DB")
    slug = "".join(char if char.isalnum() else "_" for char in str(server_id)).strip("_").upper()
    while "__" in slug:
        slug = slug.replace("__", "_")
    return f"{engine}_{slug}_MONITOR"


def add_instance(request: dict[str, Any] | None = None, *,
                 data_dir: str | Path | None = None,
                 key: str | None = None) -> dict[str, Any]:
    """Register one target: inventory, credential and secret, in one operation.

    Request fields: ``server_id``, ``db_type``, ``ip`` are required; ``port``, ``major_version``,
    ``service_name``, ``env``, ``enabled``, ``note`` and anything else are passed through to the
    inventory record. ``username`` plus either ``password`` or ``password_ref`` supply the login;
    ``credential_name`` is derived from the target when it is not given. ``replace`` allows
    overwriting an existing ``server_id``.
    """
    payload = dict(request or {})
    root = Path(data_dir) if data_dir else data_sources.DEFAULT_DATA_DIR

    missing = [name for name in REQUIRED_FIELDS if not str(payload.get(name) or "").strip()]
    if missing:
        raise InstanceAdminError(
            f"missing required field(s): {', '.join(missing)}. A target needs at least a "
            "server_id (its name everywhere), a db_type and an ip.")

    server_id = str(payload["server_id"]).strip()
    db_type = str(payload["db_type"]).strip().lower()
    username = str(payload.get("username") or "").strip()
    password = payload.get("password")
    password_ref = str(payload.get("password_ref") or "").strip()
    credential_name = str(payload.get("credential_name") or "").strip() or \
        _default_credential_name(server_id, db_type)

    if password is not None and password_ref:
        raise InstanceAdminError(
            "give either password (stored encrypted here) or password_ref (a secret that already "
            "exists), not both - two answers to 'which secret' is how the wrong one gets used.")
    if username and password is None and not password_ref:
        raise InstanceAdminError(
            f"username {username!r} was given with no password and no password_ref, so the target "
            "would resolve to no credential. Pass password to store one, or password_ref to point "
            "at a secret that already exists.")

    instances_path = data_sources.db_instances_path(root)
    instances = _read(instances_path, "db_instances")
    existing = [item for item in instances["db_instances"]
                if isinstance(item, dict) and str(item.get("server_id")) == server_id]
    if existing and not payload.get("replace"):
        raise InstanceAdminError(
            f"{server_id} is already in {instances_path.name}. Pass replace to overwrite it - a "
            "silent overwrite of a target somebody else added is not something to guess at.")

    record: dict[str, Any] = {
        key_name: value for key_name, value in payload.items()
        if key_name not in {"username", "password", "password_ref", "credential_name",
                            "replace", "verify", "role"}
    }
    record["server_id"] = server_id
    record["db_type"] = db_type
    record.setdefault("port", DEFAULT_PORTS.get(db_type, 0) or None)
    record.setdefault("enabled", True)
    if username:
        record["default_credential_name"] = credential_name

    instances["db_instances"] = [item for item in instances["db_instances"]
                                 if not (isinstance(item, dict)
                                         and str(item.get("server_id")) == server_id)]
    instances["db_instances"].append(record)

    written: list[str] = [instances_path.name]
    secret_written = ""

    if username:
        users_path = data_sources.users_path(root)
        users = _read(users_path, "database_credentials")
        users.setdefault("database_credentials", [])
        group = {"server_id": server_id, "db_type": db_type, "credentials": [{
            "credential_name": credential_name,
            "username": username,
            "password_ref": password_ref or credential_name,
            "role": str(payload.get("role") or "monitor"),
        }]}
        users["database_credentials"] = [item for item in users["database_credentials"]
                                         if not (isinstance(item, dict)
                                                 and str(item.get("server_id")) == server_id)]
        users["database_credentials"].append(group)
        _write(users_path, users)
        written.append(users_path.name)

        if password is not None:
            if not key:
                raise InstanceAdminError(
                    "a password was given but no passphrase is available to encrypt it. Export "
                    "DB_OPS_SECRET_KEY, or pass --key-base64. Nothing was written.")
            secret_written = _store_secret(root, credential_name, str(password), key)
            written.append(Path(secret_written).name)

    # The inventory is written last, so a failure encrypting the password does not leave a target
    # registered with a credential that has no value behind it - the exact half-configured state
    # `check-credentials` exists to find, arrived at by the command meant to prevent it.
    _write(instances_path, instances)

    return {
        "server_id": server_id,
        "db_type": db_type,
        "port": record.get("port"),
        "credential_name": credential_name if username else "",
        "password_ref": (password_ref or credential_name) if username else "",
        "password_stored_encrypted": bool(password is not None),
        "replaced": bool(existing),
        "files_written": written,
        "next": [
            "db-ops check-credentials",
            f"db-ops common.cli run-sql '{{\"target\":\"{server_id}\",\"sql\":\"SELECT 1\"}}'",
            "db-ops metrics collect --dry-run",
        ],
    }


def _store_secret(root: Path, ref: str, value: str, key: str) -> str:
    """Add one secret to the encrypted store, without the value ever reaching the disk in clear.

    Read, add, write back. `encrypt-secret` exists for the bulk case - a whole
    `secrets/secret_text.json` - and this is the single-entry one, which is what registering a
    target needs. Doing it through the bulk path would mean writing that plaintext file first.
    """
    path = data_sources.secret_text_path(root)
    existing: dict[str, str] = {}
    if path.exists():
        try:
            existing = dict(_secret_text.load_secret_text_file(path, key=key))
        except Exception as exc:  # noqa: BLE001 - a wrong passphrase must say so, not overwrite.
            raise InstanceAdminError(
                f"the existing secret store at {path.name} could not be opened with this "
                f"passphrase ({exc}). Nothing was written - overwriting it would lose every "
                "secret already in it.") from exc
    existing[ref] = value
    blob = _secret_text.encrypt_secret_text(existing, key)
    atomic_write_text(path, json.dumps(blob, ensure_ascii=False, indent=2) + "\n")
    return str(path)
