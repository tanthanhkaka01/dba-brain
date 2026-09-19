"""One config file points at another. This is where those pointers are checked.

``data/*.json`` is a small relational database with no foreign keys. A backup job names a
``server_id``, a SQL target names a ``credential_name``, an instance names the OS login that reaches
its host — and until 2026-09-19 nothing compared either side. The failure is always the same shape:
the config loads, the schedule runs, and the run dies at the moment it needs the thing that is not
there. Observed that morning, on a backup that had been active for a day::

    backup_restore.backup ERROR: backup_id=... /wal: server_id not found in db_instances.json:
    A1A-...-PG-5433

Nothing in the estate could have said that in advance, which is the gap this module closes. The
rules are data — ``data/config_references.json`` — so a new pointer is declared where the others are
rather than growing another bespoke validator, and :func:`check` reports every dangling one at once.

**It is offline and reads only config.** A ``password_ref`` is deliberately *not* checked here: the
secret store is one encrypted blob, so "does this ref exist" needs the passphrase and belongs to
``common.cli check-secret``, which already answers it by authenticating. A check that pretended to
verify a secret without the key would be the worst of the three options.

**Only `active` records are failed.** An inactive entry pointing at something that is gone is how an
estate retires a target, and reporting it as broken teaches the reader to ignore the report. It is
still listed, under ``inactive``, because an entry someone re-enables is the next outage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db_ops.lib.json_io import load_json_file
from db_ops.lib.paths import DEFAULT_DATA_DIR, reference_file
from db_ops.lib.shared_objects import walk_records

#: The file, in each of its three homes (package seed, this node's, the public example).
FILENAME = "config_references.json"

class ConfigReferenceError(ValueError):
    """The rule file is missing, unreadable, or does not describe a rule that can be checked."""


def rules_path(data_dir: str | Path | None = None) -> Path:
    """This node's rule file, its example, or the packaged copy — in that order.

    :func:`db_ops.lib.paths.reference_file` decides that order for both reference files; this is the
    name the rest of this module reads better with.
    """
    return reference_file(FILENAME, data_dir)


def load_rules(data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    path = rules_path(data_dir)
    try:
        payload = load_json_file(path)
    except Exception as exc:  # noqa: BLE001 - the reason matters more than the class
        raise ConfigReferenceError(f"{path}: {exc}") from exc
    rules = payload.get("config_references")
    if not isinstance(rules, list) or not rules:
        raise ConfigReferenceError(f"{path} describes no config_references.")
    return [rule for rule in rules if isinstance(rule, dict)]


def _read(data_dir: Path, file_name: str) -> dict[str, Any] | None:
    """One config file, falling back to its example.

    The fallback is what lets this run in the public tree, where ``data/`` holds only
    ``*.example.json`` — a check that silently passes there would be worse than one that is absent,
    because the export carries the tests too.
    """
    for candidate in (data_dir / file_name,
                      data_dir / file_name.replace(".json", ".example.json")):
        if candidate.is_file():
            try:
                return load_json_file(candidate)
            except Exception:  # noqa: BLE001 - reported by the caller as an unreadable file
                return None
    return None


def _dotted(record: Any, field: str) -> Any:
    """``cmd_access.credential_name`` read out of one record, or ``None``."""
    node = record
    for part in str(field).split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


#: A ``field`` of ``"[key]"`` means the KEYS of the object at that path rather than a field of a
#: record in it. ``telegram_config.json`` -> ``level_chat_map`` is a map from level to chat id, so
#: the thing a pointer has to land in is a key; without this the rule could only be written against
#: a list, and the first run of it reported a working route as dangling.
KEYS_FIELD = "[key]"


def _values(payload: dict[str, Any], path: str, field: str) -> set[str]:
    """Every value of ``field`` at ``path`` — the set a pointer has to land in."""
    found: set[str] = set()
    for _label, record in walk_records(payload, path):
        if field == KEYS_FIELD:
            if isinstance(record, dict):
                found |= {str(key) for key in record}
            continue
        value = _dotted(record, field)
        if value not in (None, ""):
            found.add(str(value))
    return found


def _targets(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``to`` side, always as a list.

    A pointer may have more than one legitimate home: a notify level is wired either in
    ``telegram_groups.json`` or in ``telegram_config.json``'s ``level_chat_map``, and landing in
    either is correct. Written as one target it reports the other as broken.
    """
    target = rule.get("to")
    if isinstance(target, list):
        return [item for item in target if isinstance(item, dict)]
    return [target] if isinstance(target, dict) else []


def _is_active(record: dict[str, Any], active_field: str) -> bool:
    if not active_field:
        return True
    value = _dotted(record, active_field)
    if value is None:
        return True  # absent means active everywhere in this tree
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "active", "on"}


def check(data_dir: str | Path | None = None) -> dict[str, Any]:
    """Every declared pointer in ``data_dir``, followed.

    Returns ``dangling`` (active records pointing at nothing — the ones that will fail at run time),
    ``inactive`` (the same, on a record nobody runs) and ``unreadable`` (a rule naming a file that is
    not there, which is a rule to fix rather than an estate to fix).
    """
    root = Path(data_dir or DEFAULT_DATA_DIR)
    dangling: list[dict[str, str]] = []
    inactive: list[dict[str, str]] = []
    unreadable: list[dict[str, str]] = []
    checked = 0

    for rule in load_rules(root):
        name = str(rule.get("name") or "")
        source = rule.get("from") or {}
        targets = _targets(rule)
        source_payload = _read(root, str(source.get("file") or ""))
        if source_payload is None:
            unreadable.append({"rule": name, "file": str(source.get("file") or ""),
                               "detail": "not found in this data directory, so the rule was skipped"})
            continue

        allowed: set[str] = set()
        target_labels: list[str] = []
        missing_target = False
        for target in targets:
            target_payload = _read(root, str(target.get("file") or ""))
            if target_payload is None:
                missing_target = True
                unreadable.append({
                    "rule": name, "file": str(target.get("file") or ""),
                    "detail": "not found in this data directory, so the rule was skipped"})
                break
            allowed |= _values(target_payload, str(target.get("path") or ""),
                               str(target.get("field") or ""))
            target_labels.append(
                f"{target.get('file')} {target.get('path')}.{target.get('field')}")
        if missing_target or not targets:
            continue

        source_field = str(source.get("field") or "")
        active_field = str(source.get("active_field") or "")

        for label, record in walk_records(source_payload, str(source.get("path") or "")):
            if not isinstance(record, dict):
                continue
            value = _dotted(record, source_field)
            if value in (None, ""):
                continue  # an absent pointer is the object's own required-field question
            checked += 1
            if str(value) in allowed:
                continue
            finding = {
                "rule": name,
                "where": f"{source.get('file')}:{label}.{source_field}",
                "value": str(value),
                "detail": f"{value!r} is in none of: {'; '.join(target_labels)}",
                "why": str(rule.get("why") or ""),
            }
            (dangling if _is_active(record, active_field) else inactive).append(finding)

    return {
        "data_dir": str(root),
        "pointers_checked": checked,
        "dangling": dangling,
        "inactive": inactive,
        "unreadable": unreadable,
        "ok": not dangling,
    }
