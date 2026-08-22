"""Unified DB target resolution: accept a ``server_id`` OR a ``<db_type> <ip> [port]`` spec.

Different entry points historically asked for a target in different shapes — some took a
``server_id`` (``ACME-192-0-2-248``), others an ip + port + db_type. This module lets a single
free-text field accept either form and resolve it to one db instance from ``db_instances.json``:

- one token            -> a ``server_id`` (exact match)
- ``<db_type> <ip>``    -> the first instance of that db_type at that ip (top-1 by file order)
- ``<db_type> <ip> <port>`` -> that db_type at that ip AND that port

db_type is matched through :data:`DB_TYPE_ALIASES` (``mssql`` == ``sqlserver``, ``pg`` ==
``postgresql`` ...). Callers that only support one engine check ``db_type`` on the returned
instance themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db_ops.common import data_sources
from db_ops.lib.listing import active_only, hidden_note
from db_ops.lib.sql_access import KNOWN_DB_TYPES


class TargetResolveError(RuntimeError):
    """Raised when a target spec is malformed or matches no configured db instance."""


# Friendly db_type words -> the canonical db_type stored in db_instances.json.
DB_TYPE_ALIASES: dict[str, str] = {
    "mssql": "sqlserver",
    "sqlserver": "sqlserver",
    "sql": "sqlserver",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "pg": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "oracle": "oracle",
    "ora": "oracle",
}


def normalize_db_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    return DB_TYPE_ALIASES.get(text, text)


def parse_target_spec(spec: str) -> dict[str, Any]:
    """Parse a target spec into ``{"kind": "server_id"|"triple", ...}``.

    Accepted forms:
    - ``<server_id>``                      (one token, no ``:``)
    - ``<db_type> <ip>``                   (port omitted -> top-1 at that ip)
    - ``<db_type> <ip> <port>``            (space-separated port)
    - ``<db_type> <ip>:<port>``            (colon form, matching the /spbot_list_server_id output)

    Raises :class:`TargetResolveError` for an empty spec, a non-numeric port, or the wrong
    number of tokens.
    """
    tokens = str(spec or "").split()
    if not tokens:
        raise TargetResolveError("target is required (a server_id, or: <db_type> <ip>[:port]).")
    if len(tokens) == 1 and ":" not in tokens[0]:
        return {"kind": "server_id", "server_id": tokens[0]}
    if len(tokens) in (2, 3):
        db_type = normalize_db_type(tokens[0])
        ip = tokens[1]
        port: int | None = None
        # Colon form: the ip token carries the port ("192.0.2.248:1433").
        if ":" in ip:
            ip, port_text = ip.rsplit(":", 1)
            port = _parse_port(port_text)
        if len(tokens) == 3:
            if port is not None:
                raise TargetResolveError(f"port given twice: {tokens[1]!r} and {tokens[2]!r}.")
            port = _parse_port(tokens[2])
        return {"kind": "triple", "db_type": db_type, "ip": ip, "port": port}
    raise TargetResolveError(
        "target must be a server_id, or: <db_type> <ip>[:port] (e.g. 'mssql 192.0.2.248:1433')."
    )


def _parse_port(text: str) -> int:
    try:
        return int(text)
    except ValueError as exc:
        raise TargetResolveError(f"port must be a number, got {text!r}.") from exc


def resolve_target_instance(spec: str, *, data_dir: str | Path | None = None) -> dict[str, Any]:
    """Resolve a target spec to a single db instance dict from ``db_instances.json``.

    The db_type/ip form takes the first matching instance (top-1 by file order) when no port is
    given. Raises :class:`TargetResolveError` when nothing matches.
    """
    parsed = parse_target_spec(spec)
    instances = data_sources.load_db_instances(data_dir)

    if parsed["kind"] == "server_id":
        server_id = parsed["server_id"]
        for instance in instances:
            if str(instance.get("server_id", "")).strip() == server_id:
                return dict(instance)
        raise TargetResolveError(
            f"Unknown server_id: {server_id}. Use /spbot_list_server_id to see valid ids."
        )

    db_type = parsed["db_type"]
    ip = parsed["ip"]
    port = parsed["port"]
    for instance in instances:
        if normalize_db_type(instance.get("db_type")) != db_type:
            continue
        if str(instance.get("ip", "")).strip() != ip:
            continue
        if port is not None and _port_of(instance) != port:
            continue
        return dict(instance)

    where = f"{db_type} {ip}" + (f" {port}" if port is not None else "")
    raise TargetResolveError(
        f"No target matches: {where}. Use /spbot_list_server_id to see valid targets."
    )


def resolve_sql_target_fields(
    server_id: str, *, data_dir: str | Path | None = None
) -> dict[str, Any]:
    """Everything a ``sql_targets`` entry needs, derived from ``server_id`` alone.

    ``/spbot_add_sql`` used to ask the operator for db_type, instance and target database as
    well. All three are already recorded in ``db_instances.json`` against the server, so asking
    made the conversation four messages longer and gave the operator three more chances to enter
    something that does not resolve — which is exactly how SQLSERVER-017 ended up with a null
    instance and a task that could never find its database.

    Distinct from :func:`resolve_target_instance` on purpose: that one answers "which instance is
    this spec", accepts the ``<db_type> <ip> [port]`` form, and returns the raw record. This one
    answers "what do I write into a new task's target", takes only a ``server_id``, and returns
    the five fields that entry has — including the **refusal** when the id names more than one
    instance. Picking one there would run the operator's SQL against a database they never named.

    Lived in ``common/config_admin.py`` until 2026-08-15, where the Telegram app had to import
    ``common`` to reach it. It is a read of the data folder, and the data folder has one reader.
    """
    server_id = str(server_id or "").strip()
    if not server_id:
        raise TargetResolveError("server_id is required.")

    instances = data_sources.load_db_instances(data_dir)
    matches = [
        item for item in instances
        if str(item.get("server_id") or "").strip().lower() == server_id.lower()
    ]
    if not matches:
        known = sorted({str(item.get("server_id") or "").strip() for item in instances} - {""})
        hint = f" Known: {', '.join(known[:8])}" if known else ""
        raise TargetResolveError(
            f"Unknown server_id: {server_id}. Use /spbot_list_server_id to list targets.{hint}"
        )
    if len(matches) > 1:
        detail = ", ".join(
            f"{item.get('db_type')}/{item.get('instance_name') or '-'}" for item in matches
        )
        raise TargetResolveError(
            f"{server_id} has more than one instance ({detail}), so server_id alone does not "
            "say which one to use. Add the task with the CLI and name --instance-name."
        )

    entry = matches[0]
    db_type = str(entry.get("db_type") or "").strip().lower()
    if db_type not in KNOWN_DB_TYPES:
        raise TargetResolveError(
            f"{server_id} has db_type {entry.get('db_type')!r} in db_instances.json, "
            f"which is not one of {KNOWN_DB_TYPES}."
        )
    return {
        "db_type": db_type,
        "server_id": str(entry.get("server_id") or server_id),
        "service_name": _blank_to_none(entry.get("service_name") or entry.get("db_name")),
        "instance_name": _blank_to_none(entry.get("instance_name")),
        "credential_name": _blank_to_none(entry.get("default_credential_name")),
    }


def _blank_to_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def list_target_instances(data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """The queryable db targets (instances that have a db_type), in file order."""
    result: list[dict[str, Any]] = []
    for instance in data_sources.load_db_instances(data_dir):
        if not str(instance.get("db_type") or "").strip():
            continue  # OS-only hosts (e.g. ERP AOS) have no database to target.
        result.append(
            {
                "server_id": str(instance.get("server_id") or ""),
                "db_type": normalize_db_type(instance.get("db_type")),
                "ip": str(instance.get("ip") or ""),
                "port": _port_of(instance),
                "instance_name": str(instance.get("instance_name") or ""),
                "enabled": bool(instance.get("enabled", True)),
            }
        )
    return result


def format_target_list(data_dir: str | Path | None = None) -> str:
    """A human-readable listing of server targets for the Telegram reply."""
    # Only the enabled ones: this listing exists to be copied from into another command, and a
    # disabled target is one the resolver will refuse. list_target_instances keeps returning
    # everything - resolution has to be able to say "that id exists but is disabled".
    targets, hidden = active_only(list_target_instances(data_dir), key=lambda t: bool(t["enabled"]))
    if not targets:
        note = hidden_note(hidden, noun="target")
        return "No enabled database targets are configured." + (f"\n{note}" if note else "")
    lines = ["Server targets - server_id | db_type ip:port | instance"]
    for target in targets:
        port = target["port"] or "?"
        instance = f" | {target['instance_name']}" if target["instance_name"] else ""
        lines.append(f"{target['server_id']} | {target['db_type']} {target['ip']}:{port}{instance}")
    lines.append("")
    lines.append("Use the server_id, or type: <db_type> <ip> [port]  (e.g. mssql 192.0.2.248 1433).")
    note = hidden_note(hidden, noun="target")
    if note:
        lines.append(note)
    return "\n".join(lines)


def _port_of(instance: dict[str, Any]) -> int:
    try:
        return int(instance.get("port") or 0)
    except (TypeError, ValueError):
        return 0
