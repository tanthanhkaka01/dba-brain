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
from db_ops.lib import sql_access as _sql_access
from db_ops.lib.json_io import atomic_write_text

#: Fields every target needs, whatever the engine. Anything else is optional and passed through, so
#: a field this module has never heard of still reaches `db_instances.json` rather than being
#: silently dropped — the inventory grows faster than any allow-list of keys would.
REQUIRED_FIELDS: tuple[str, ...] = ("server_id", "db_type", "ip")

#: What each engine listens on when the caller does not say. Stated rather than defaulted to 1433
#: for everything, because a wrong port fails as a timeout, which reads as "the host is down".
#: ``host`` is absent on purpose - it has nothing to connect to, and its port stays ``None``.
DEFAULT_PORTS: dict[str, int] = {
    "sqlserver": 1433, "oracle": 1521, "postgresql": 5432, "postgres": 5432, "mysql": 3306,
}

#: A machine with no database on it, re-exported from the module that owns the config vocabulary.
#: It was defined here first and moved to `lib.sql_access` on 2026-09-14, when `check-credentials`
#: turned out to be recognising a host by a different test (`if not db_type`) and reporting four
#: correct records as broken. One name, one place, three readers.
#:
#: It was also left out of this command's help text, which listed the four engines, and that cost
#: every host-only record in this estate a hand-edit: the command could always take one, and
#: nothing said so where anybody was looking.
HOST_ONLY_DB_TYPE = _sql_access.HOST_ONLY_DB_TYPE

#: Engines whose connection takes a real database name. SQL Server is absent because collection
#: always connects to `master` and the metric SQL does its own USE; Oracle because it connects by
#: service and ignores the database entirely.
_DATABASE_IS_NAMED: frozenset[str] = frozenset({"postgresql", "mysql"})

#: What to name when the target is the instance rather than one database on it.
_NEUTRAL_DATABASE: dict[str, str] = {"postgresql": "postgres", "mysql": "information_schema"}


#: The command's help, beside the behaviour it describes.
USAGE = """usage: python -m db_ops.common.cli instance-add <json>|@<file>|- [--key-base64 ...]

Registers ONE database to monitor: the inventory record, its credential, and the secret behind it.
Replaces four hand-edits (db_instances.json, users.json, secrets/secret_text.json, encrypt-secret)
with one call, and the password never reaches the disk in the clear.

  server_id      required. Its name everywhere - reports, alerts, commands
  db_type        required. sqlserver | oracle | postgresql | mysql | host
  ip             required
  port           optional. Defaults per engine (1433 / 1521 / 5432 / 3306); none for host

db_type "host" is a machine with NO database - an application server, a hypervisor, a VM that
only needs OS metrics. It takes no port, no username and no database credential; give it a
platform (windows | linux) and a cmd_access block, and give it an OS login with

  python -m db_ops.common.cli remote-credential-add ...

which is the command that writes users.json remote_credentials. Registering a host here and
stopping is a target that collects nothing - the record exists and nothing can log in to it.
  username       the login. With it, give exactly one of:
  password         stored straight into the ENCRYPTED store; never written in clear
  password_ref     the name of a secret that already exists
  credential_name  optional; derived from server_id + db_type when absent
  replace        overwrite an existing server_id instead of refusing

Anything else in the object is passed through to the inventory record, so major_version,
service_name, env, platform, cmd_access and note all reach it unchanged.

Three fields fail in ways that do not name themselves, and the same warnings apply here as in the
  db_name        REQUIRED for postgresql and mysql - the database to connect to. Use
                 "postgres" / "information_schema" to monitor the instance itself. Absent,
                 the connection falls back to service_name, or to the server_id, and the
                 server answers `database "<that>" does not exist`. SQL Server does not
                 take one (collection connects to master); Oracle connects BY service

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
    # A database login on a machine with no database is a credential nothing will ever read, and
    # it looks configured. The OS login such a host actually needs is a different file and a
    # different command, so say which rather than writing the wrong one.
    if db_type == HOST_ONLY_DB_TYPE and username:
        raise InstanceAdminError(
            f"db_type 'host' is a machine with no database, so the database login {username!r} "
            "would be stored where nothing reads it. Register the host without a username, then "
            "give it an OS login with 'remote-credential-add', which writes users.json "
            "remote_credentials and the cmd_access block that names it.")

    # Every engine but SQL Server connects to a NAMED database, and when the record does not name
    # one the target builder falls back to `service_name or instance_name or server_name or
    # server_id` - so a label, or the record's own name, is handed to the server as a database.
    # Measured 2026-09-15: a store registered with service_name "DBOPS-STORE" and no database
    # failed every collection with `database "DBOPS-STORE" does not exist`, which names the label
    # and not the mistake. Oracle is exempt: it connects BY service and ignores the database.
    if db_type in _DATABASE_IS_NAMED and not str(
            payload.get("database") or payload.get("db_name") or "").strip():
        label = str(payload.get("service_name") or "").strip()
        raise InstanceAdminError(
            f"a {db_type} target needs the database to connect to - give db_name. Without it the "
            f"connection falls back to " + (f"service_name ({label!r}), which is a LABEL"
                                            if label else f"the server_id ({server_id!r})")
            + f", and the server answers `database \"{label or server_id}\" does not exist`. "
            f"Use db_name \"{_NEUTRAL_DATABASE[db_type]}\" to monitor the instance itself, or "
            "name the database this target is actually about.")

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
        credential = {
            "credential_name": credential_name,
            "username": username,
            "password_ref": password_ref or credential_name,
            "role": str(payload.get("role") or "monitor"),
        }
        # The credential is replaced INSIDE its group, never the group itself. A server
        # legitimately carries more than one database login - a monitor account and a DBA
        # account, or `sys` beside an application user - and four of this estate's do. Replacing
        # the group to add one silently deleted the others, which is only visible later as a
        # target that resolves to the wrong login or to none. Measured 2026-09-14, standing a
        # node up one `instance-add` at a time: 38 groups on the master came back as 34.
        group = next((item for item in users["database_credentials"]
                      if isinstance(item, dict) and str(item.get("server_id")) == server_id), None)
        if group is None:
            group = {"server_id": server_id, "db_type": db_type, "credentials": []}
            users["database_credentials"].append(group)
        else:
            group["db_type"] = db_type
        group["credentials"] = [item for item in (group.get("credentials") or [])
                                if str(item.get("credential_name") or "") != credential_name]
        group["credentials"].append(credential)
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
