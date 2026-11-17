"""Retiring a server from the configuration, one record at a time, the leaf first (0.41.0, 1.391).

The operator, 2026-11-17 (translated): *many instances in this list were for labs and will never be
used again* - then *not only off the report: off every JSON it is in too - the secrets,
db_instances.json, the users* - and *commands step by step, not one command that cascades and deletes
everything; delete from the deepest child up, otherwise the parent refuses*.

So there is no retire-everything command. Each command here removes ONE record, by its own key, from
one file of a ``data/`` folder, and **refuses while anything else in that folder still names it** -
every such place said, file and path, so the next step to run is in the refusal:

* :func:`remove_entry` - a record of a configuration list (a backup or restore entry, a SQL target,
  a docker connection, ...), found by the fields it is asked by;
* :func:`remove_credential` - a login in ``users.json`` (a database or an OS one); the server's group
  goes with its last login, it is only their container;
* :func:`remove_instance` - a server's record in ``db_instances.json``, with the blocks inside it;
* the secret is ``secret-remove``'s (it asks :func:`pointers` too); the report's row is
  :func:`remove_report_server`'s.

**What points at a record** is read, not listed by hand: every ``data/*.json`` is walked and every
value - and every key - equal to the record's name is a pointer, except inside the record itself. The
reference rules (``config_references.json``) know the relations a check has needed so far; a retired
server is named in more places than that (a group in ``users.json``, a docker connection, a lab's
notes), and a pointer the rules do not know would have been left dangling. Exact values only: a name
inside a sentence is not a pointer.

Nothing is written unless the request says ``apply: true``; without it the answer is the plan - what
would go, and what refuses it. A command works from its request alone (rules R09): the request names
the ``data_dir`` (else this node's data/ folder).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

from dbabrain.lib import errors
from dbabrain.lib.json_io import atomic_write_text, dump_json_text, indent_of

__all__ = ["RetireError", "pointers", "remove_entry", "remove_credential", "remove_instance",
           "remove_report_server", "said", "SKIPPED_FILES", "INVENTORY_FILE"]


class RetireError(errors.InvalidRequest):
    """A request that names no single record, or a record something still points at."""


#: Files that hold no configuration a record could be named by: the encrypted secret store (its keys
#: are the refs - ``secret-remove``'s), the reference rules (field names, not values), and the
#: reference objects and examples (documentation).
SKIPPED_FILES = ("encrypted_secret_text.json", "config_references.json", "database-inventory.json")

#: The report's canonical inventory, kept add-only by the report (this folder's copy is what a deploy
#: ships to ``runtime/reports``): a server is taken off it by the LAST step, report-server-remove, so it
#: never stands in the way of the steps before - it names a server's logins too (``remote_user``), and
#: held a retired server's OS login for ever (``SKIPPED_FILES``).
INVENTORY_FILE = "database-inventory.json"

#: The names a record is told by, for a pointer's label - the first one the record holds.
_LABEL_FIELDS = ("restore_id", "backup_id", "credential_name", "server_id", "id", "sql_id", "name", "code")


def _skipped(name: str) -> bool:
    return name in SKIPPED_FILES or name.startswith("shared_config_objects") or name.endswith(".example.json")


def _data_dir(request: dict[str, Any]) -> Path:
    """The request's ``data_dir``, else this node's - as every registrar of the folder resolves it."""
    from dbabrain.lib.paths import resolve_data_dir

    raw = str(request.get("data_dir") or "").strip()
    path = Path(raw) if raw else resolve_data_dir()
    if not path.is_dir():
        raise RetireError(f"data_dir {path} is not a folder.")
    return path


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _label(record: Any) -> str:
    if not isinstance(record, dict):
        return ""
    for field in _LABEL_FIELDS:
        if record.get(field) not in (None, ""):
            return f"{field}={record[field]}"
    return ""


def _walk(node: Any, path: str, value: str, skip_at: str, owner: Any) -> Iterator[tuple[str, str]]:
    """``(path, label)`` of every place ``value`` is named in ``node``: a value equal to it, or a key -
    except under ``skip_at``, the record being removed (by its path: the file is read afresh here)."""
    if skip_at and path == skip_at:
        return
    if isinstance(node, dict):
        for key, item in node.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key) == value:
                yield here + " (key)", _label(node) or _label(owner)
            yield from _walk(item, here, value, skip_at, node)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk(item, f"{path}[{index}]", value, skip_at, owner)
    elif isinstance(node, str) and node == value:
        yield path, _label(owner)


def pointers(data_dir: str | Path, value: str, *, skip_file: str = "", skip_at: str = "",
             also_skip: tuple[str, ...] = ()) -> list[dict[str, str]]:
    """Every place a ``data/*.json`` names ``value`` - ``[{"file", "at", "record"}]`` - except inside the
    record at ``skip_at`` of ``skip_file`` (the one being removed) and the files in ``also_skip``. A file that
    cannot be read is a pointer too: what it holds is unknown, so nothing is removed past it."""
    found: list[dict[str, str]] = []
    for path in sorted(Path(data_dir).glob("*.json")):
        if _skipped(path.name) or path.name in also_skip:
            continue
        try:
            document = _load(path)
        except (OSError, ValueError) as exc:
            found.append({"file": path.name, "at": "(unreadable)", "record": f"{type(exc).__name__}: {exc}"})
            continue
        for at, label in _walk(document, "", value, skip_at if path.name == skip_file else "", None):
            found.append({"file": path.name, "at": at, "record": label})
    return found


def said(found: list[dict[str, str]]) -> str:
    """The pointers, as a refusal names them: file and path, the first twelve."""
    shown = "; ".join(f"{p['file']} {p['at']}" + (f" ({p['record']})" if p["record"] else "") for p in found[:12])
    return shown + (f"; and {len(found) - 12} more" if len(found) > 12 else "")


def _write(path: Path, document: Any) -> None:
    """The file as it was written - its indent and its line endings - less the record: a CRLF file
    rewritten with LF showed every line changed in a diff (the master's inventory is CRLF)."""
    text = dump_json_text(document, indent=indent_of(path, 2))
    if b"\r\n" in path.read_bytes()[:4096]:
        text = text.replace("\n", "\r\n")
    atomic_write_text(path, text)


def _lists(node: Any, path: str = "") -> Iterator[tuple[str, list]]:
    """Every list of records in a document - top level, or one object down (``backup_restore.backups``)."""
    if isinstance(node, list):
        yield path, node
    elif isinstance(node, dict):
        for key, item in node.items():
            if isinstance(item, list) and all(isinstance(x, dict) for x in item):
                yield (f"{path}.{key}" if path else key), item
            elif isinstance(item, dict) and not path:
                yield from _lists(item, key)


def _outcome(*, command: str, file: str, record: str, found: list[dict[str, str]], apply: bool,
             removed: bool, more: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"command": command, "file": file, "record": record, "pointers": found, "applied": bool(apply),
            "removed": removed, **(more or {})}


def _refuse_or_remove(*, command: str, path: Path, document: Any, record_name: str, value: str,
                      container: list, record: dict, at: str, apply: bool, also_skip: tuple[str, ...] = (),
                      more: dict[str, Any] | None = None) -> dict[str, Any]:
    found = pointers(path.parent, value, skip_file=path.name, skip_at=at, also_skip=also_skip) if value else []
    if found:
        raise RetireError(f"{record_name} is still named in {len(found)} place(s) - remove those first, the "
                          f"deepest first: {said(found)}. Nothing was removed.")
    if apply:
        container.remove(record)
        _write(path, document)
    return _outcome(command=command, file=path.name, record=record_name, found=[], apply=apply, removed=apply,
                    more=more)


# --------------------------------------------------------------------------- #
# The commands
# --------------------------------------------------------------------------- #
#: The fields a record is named by - its own key, which other records may point at. A record told only by
#: a field that points elsewhere (a server_id, a credential_name) is named by nothing.
_OWN_KEYS = ("backup_id", "restore_id", "id", "name", "code", "app_code", "notify_level", "chat_id")

#: Files a record is removed from by a command of its own: entry-remove refuses them by name.
_OWN_COMMAND = {"users.json": "credential-remove", "db_instances.json": "instance-remove",
                "encrypted_secret_text.json": "secret-remove"}


def remove_entry(request: dict[str, Any]) -> dict[str, Any]:
    """``entry-remove``: ``{"data_dir", "file", "match": {field: value, ...}, "apply"}`` - the ONE record
    of a list in ``file`` whose fields equal every one of ``match`` (``{"restore_id": "..."}``,
    ``{"sql_id": 3, "target_no": 1}``, ``{"id": "..."}``). Refused while its name - its own key in
    ``match`` (``_OWN_KEYS``) - is named anywhere else; a record told only by a field that points elsewhere
    (a backup policy override by its ``server_id``) is named by nothing."""
    data_dir = _data_dir(request)
    file = str(request.get("file") or "").strip()
    match = request.get("match")
    if not file or "/" in file or "\\" in file or not file.endswith(".json"):
        raise RetireError("file is required: one data/*.json file name, e.g. restore_config.json.")
    if file in _OWN_COMMAND:
        raise RetireError(f"{file} has a command of its own: {_OWN_COMMAND[file]}.")
    if _skipped(file):
        raise RetireError(f"{file} holds no configuration record to remove.")
    if not isinstance(match, dict) or not match:
        raise RetireError('match is required: the fields the record is told by, e.g. {"restore_id": "..."}.')
    path = data_dir / file
    if not path.exists():
        raise RetireError(f"{path} does not exist.")
    document = _load(path)
    hits = [(where, items, item) for where, items in _lists(document) for item in items
            if all(item.get(field) == wanted for field, wanted in match.items())]
    if len(hits) != 1:
        raise RetireError(f"{len(hits)} record(s) of {file} match {json.dumps(match)} - one is removed at a time.")
    where, items, record = hits[0]
    # Its name is its own key; a field that points at another record (a server_id, a credential) is
    # not - a backup policy override told by its server_id names nothing, and holding it to the server
    # (named everywhere until its last step) refused it for ever.
    name = next((str(match[field]) for field in _OWN_KEYS if isinstance(match.get(field), str) and match[field]), "")
    return _refuse_or_remove(command="entry-remove", path=path, document=document, value=name,
                             record_name=f"{file} {where} {json.dumps(match)}", container=items, record=record,
                             at=f"{where}[{items.index(record)}]", apply=bool(request.get("apply")),
                             more={"list": where})


#: A pointer inside a server's record of db_instances.json - which server it is.
_INSTANCE_AT = re.compile(r"db_instances\[(\d+)\]")

#: Another login's own name in users.json - the same name in another server's group, not a pointer.
_ANOTHER_LOGINS_NAME = re.compile(r"(database|remote)_credentials\[\d+\]\.credentials\[\d+\]\.credential_name")


def remove_credential(request: dict[str, Any]) -> dict[str, Any]:
    """``credential-remove``: ``{"data_dir", "credential_name", "server_id", "apply"}`` - one login of
    ``users.json`` (``server_id`` names its group where several share the name),
    a database or an OS one. Refused while anything names it (a SQL target, an entry, another server's
    record) - but its own server's record, which names it as its default login or in its ``cmd_access``:
    that record is the parent, removed next. The login's ``password_ref`` stays: the secret is the next step's,
    ``secret-remove``. A server's group goes with its last login - it is only their container, and left
    empty it would name the server for ever."""
    data_dir = _data_dir(request)
    name = str(request.get("credential_name") or "").strip()
    server_id = str(request.get("server_id") or "").strip()
    if not name:
        raise RetireError("credential_name is required.")
    path = data_dir / "users.json"
    document = _load(path) if path.exists() else {}
    hits = [(section, group, login, f"{section}[{g}].credentials[{n}]")
            for section in ("database_credentials", "remote_credentials")
            for g, group in enumerate(document.get(section) or []) if isinstance(group, dict)
            and (not server_id or group.get("server_id") == server_id)
            for n, login in enumerate(group.get("credentials") or []) if isinstance(login, dict)
            and login.get("credential_name") == name]
    if len(hits) != 1:
        servers = sorted({str(group.get("server_id")) for _s, group, _l, _a in hits})
        raise RetireError(f"{len(hits)} login(s) named {name} in {path}" + (f" (servers {', '.join(servers)})" if servers else "")
                          + " - one is removed at a time" + ("; name its server_id" if len(hits) > 1 else "") + ".")
    section, group, login, at = hits[0]
    last = len(group.get("credentials") or []) == 1
    # The login's own server using it is the parent naming its child: it goes next (instance-remove),
    # and counted here the two would refuse each other for ever - the instance names its login, the
    # login's group names the instance. Any OTHER record naming the login still refuses it.
    # A name several servers' groups share (a lab's logins were copied per server) is named by each of
    # those servers - only this group's server is its parent; another's naming it still refuses it,
    # and the other groups' own copies of the name do not.
    # A server reads a login of its OWN group: a record of a server whose group holds this name names its
    # own copy, not this one - the lab's three servers each had a login of one name, each its own.
    instances = _load(data_dir / "db_instances.json").get("db_instances") or []         if (data_dir / "db_instances.json").exists() else []
    holders = {str(g.get("server_id")) for g in document.get(section) or [] if isinstance(g, dict)
               and any(isinstance(x, dict) and x.get("credential_name") == name for x in g.get("credentials") or [])}

    def server_of(where: str) -> str:
        found_at = _INSTANCE_AT.match(where)
        index = int(found_at.group(1)) if found_at else -1
        return str(instances[index].get("server_id")) if 0 <= index < len(instances) else ""

    found = [p for p in pointers(data_dir, name, skip_file="users.json", skip_at=at)
             if not (p["file"] == "db_instances.json" and server_of(p["at"]) in holders)
             and not (p["file"] == "users.json" and _ANOTHER_LOGINS_NAME.fullmatch(p["at"]))]
    if found:
        raise RetireError(f"login {name} is still named in {len(found)} place(s) - remove those first: "
                          f"{said(found)}. Nothing was removed.")
    apply = bool(request.get("apply"))
    if apply:
        group["credentials"].remove(login)
        if last:
            document[section].remove(group)
        _write(path, document)
    return _outcome(command="credential-remove", file="users.json", record=f"{section} {name}", found=[],
                    apply=apply, removed=apply,
                    more={"section": section, "server_id": group.get("server_id"), "group_removed": last and apply,
                          "group_goes": last, "password_ref": login.get("password_ref") or ""})


def remove_instance(request: dict[str, Any]) -> dict[str, Any]:
    """``instance-remove``: ``{"data_dir", "server_id", "apply"}`` - a server's record in
    ``db_instances.json``, with every block inside it (its metric switches, severity maps, metric env,
    report policy). Refused while its ``server_id`` is named anywhere else - an entry, a login group, a
    SQL target, another server's record."""
    data_dir = _data_dir(request)
    server_id = str(request.get("server_id") or "").strip()
    if not server_id:
        raise RetireError("server_id is required.")
    path = data_dir / "db_instances.json"
    document = _load(path) if path.exists() else {}
    items = document.get("db_instances") or []
    hits = [item for item in items if isinstance(item, dict) and item.get("server_id") == server_id]
    if len(hits) != 1:
        raise RetireError(f"{len(hits)} record(s) of {path} have server_id {server_id} - one is removed at a time.")
    return _refuse_or_remove(command="instance-remove", path=path, document=document, value=server_id,
                             record_name=f"db_instances.json server_id={server_id}", container=items,
                             record=hits[0], at=f"db_instances[{items.index(hits[0])}]",
                             apply=bool(request.get("apply")), also_skip=(INVENTORY_FILE,),
                             more={"next": f"report-server-remove {server_id}: its row in {INVENTORY_FILE}"})


def remove_report_server(request: dict[str, Any]) -> dict[str, Any]:
    """``report-server-remove``: ``{"data_dir", "server_id", "inventory", "apply"}`` - a server's row in
    the report's canonical inventory, which keeps every server it ever saw (add-only, on purpose):
    ``inventory`` its path - ``data_dir``'s own ``database-inventory.json`` when not given (the master's
    copy, which a deploy ships); ``runtime/reports/database-inventory.json`` on the node that builds the
    reports.
    Refused while ``db_instances.json`` still lists the server: the next report run would only put it
    back. Its pages and series files beside the report are left as they are - nothing links to them
    once the row is gone - and the store's history is kept."""
    data_dir = _data_dir(request)
    server_id = str(request.get("server_id") or "").strip()
    raw = str(request.get("inventory") or "").strip()
    inventory = Path(raw) if raw else data_dir / INVENTORY_FILE
    if not server_id:
        raise RetireError("server_id is required.")
    if not inventory.exists():
        raise RetireError(f"{inventory} does not exist.")
    register = data_dir / "db_instances.json"
    listed = [item for item in (_load(register).get("db_instances") or [] if register.exists() else [])
              if isinstance(item, dict) and item.get("server_id") == server_id]
    if listed:
        raise RetireError(f"{server_id} is still in {register} - instance-remove it first; the report would put "
                          "it back. Nothing was removed.")
    document = _load(inventory)
    hits = [(where, items, item) for where, items in _lists(document) for item in items
            if isinstance(item, dict) and item.get("server_id") == server_id]
    if len(hits) != 1:
        raise RetireError(f"{len(hits)} row(s) of {inventory} have server_id {server_id} - one is removed at a time.")
    where, items, row = hits[0]
    apply = bool(request.get("apply"))
    if apply:
        items.remove(row)
        _write(inventory, document)
    # servers_left: what the report lists after this row goes (or would) - the number a person reads
    # the retirement by (52 -> 31 on the estate it was built for).
    return _outcome(command="report-server-remove", file=str(inventory), record=f"{where} server_id={server_id}",
                    found=[], apply=apply, removed=apply,
                    more={"list": where, "servers_left": len(items) if apply else len(items) - 1})
