"""``standardize-field-names`` — stage C of one name per concept: move the files themselves.

Stages A and B (0.22.0 section 1.4) declared the standard names and taught every reader both
spellings, and left the files speaking the old ones. This rewrites them. The rename table is
:data:`db_ops.lib.field_names.RENAMES` plus ``MOVED_ONLY``, and *where* each object lives is the
reference's own ``used_in`` list — the same paths ``check-objects`` walks — so a record this reaches
is exactly a record ``check-objects`` reports as ``deprecated``, and nothing else.

A plan first, always. ``dry_run`` defaults to true, because the one outcome this must never have is
a rewrite nobody looked at: the old spelling of ``active`` is ``enabled``, and a file moved by
mistake is an instance switched on.

**A record with two spellings that disagree is never guessed at.** ``{"enabled": false,
"active": true}`` is reported as a conflict and its whole FILE is left untouched, so a run is either
the file moved or the file as it was — never half of it.

The ``*.example.json`` beside each file is moved with it: those are what a new install is given,
and an example still teaching ``app_ord`` would put the old name back on every node that starts
from it.

**The layout of a file is kept.** A key is renamed in the file's own TEXT, so a hand-formatted
example keeps its one-line arrays and its blank lines and the diff is the renames and nothing else;
the result is parsed back and compared with the migrated document before anything is written.
Only a record that loses a key (two spellings with one value) is re-serialised whole.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from db_ops.lib import field_names, shared_objects
from db_ops.lib.json_io import atomic_write_text, indent_of
from db_ops.lib.paths import PACKAGED_CATALOGUE

USAGE = """\
Usage: python -m db_ops.common.cli standardize-field-names '<json>'|@file|-

Stage C of one name per concept: rewrite data/*.json so every record uses the STANDARD field name
(enabled->active, env->environment, ord/app_ord/menu_order->sort_order, database/db_name->
database_name, sqlserver_major_version->major_version, a restore's databases->database_mappings).
Every reader already takes both spellings; this moves the files.

  {}                                  // PLAN ONLY for this node's data/ - nothing is written
  {"dry_run": false}                  // write it
  {"file": "db_instances.json"}       // one file (and its .example.json)
  {"data_dir": "D:/other/data"}       // another root

A record whose two spellings DISAGREE ({"enabled": false, "active": true}) is a conflict: its file
is not written at all, and the answer names the record. Fix it by hand, then run again.

The rename table is db_ops/lib/field_names.py; where each record lives is the reference's used_in
(data/shared_config_objects.json) - the same walk check-objects makes. After a write, check-objects
reports no `deprecated` field for these names. config_catalog.json names record fields too
(label_field, key_fields): they follow the same renames ({"file": "config_catalog.json"} alone).

data: {"dry_run", "files": [{"file", "records_changed", "changes", "conflicts", "written"}],
       "records_changed", "conflicts"}
"""


def _sites() -> dict[str, list[tuple[str, str]]]:
    """``file -> [(path, object)]`` for every object the rename tables touch.

    Read from the reference THIS version ships, never the node's copy: the node's copy is what an
    older version installed, and it can predate the objects whose fields are being moved.
    """
    wanted = set(field_names.RENAMES) | set(field_names.MOVED_ONLY)
    sites: dict[str, list[tuple[str, str]]] = {}
    for entry in shared_objects.load(PACKAGED_CATALOGUE):
        name = str(entry.get("object"))
        if name not in wanted:
            continue
        for site in entry.get("used_in") or []:
            file_name = str(site.get("file") or "")
            if file_name:
                sites.setdefault(file_name, []).append((str(site.get("path") or ""), name))
    return sites


def _path_of(label: str) -> tuple[Any, ...]:
    """``backup_restore.restores[3]`` -> ``("backup_restore", "restores", 3)``."""
    parts: list[Any] = []
    for name, index, key in re.findall(r"([^.\[\]{}]+)|\[(\d+)\]|\{([^}]*)\}", label):
        parts.append(int(index) if index else key if key or not name else name)
    return tuple(parts)


def _rename_keys_in_text(text: str, renames: dict[tuple[Any, ...], dict[str, str]]) -> str:
    """``text`` with each object key at a given path renamed, and every other byte left alone.

    A small scanner rather than a regex: the same key name (``enabled``) is renamed on an instance
    and must be left alone on its ``metrics`` block one level down, so the decision needs the path.
    """
    out: list[str] = []
    stack: list[dict[str, Any]] = []
    i, n, last = 0, len(text), 0
    while i < n:
        char = text[i]
        if char == '"':
            j = i + 1
            while text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            top = stack[-1] if stack else None
            if top is not None and top["type"] == "obj" and top["expect_key"]:
                key = json.loads(text[i:j + 1])
                path = tuple(f["key"] if f["type"] == "obj" else f["index"] for f in stack[:-1])
                new = renames.get(path, {}).get(key)
                if new:
                    out.append(text[last:i])
                    out.append(json.dumps(new, ensure_ascii=False))
                    last = j + 1
                top["key"], top["expect_key"] = key, False
            i = j + 1
            continue
        if char == "{":
            stack.append({"type": "obj", "key": None, "expect_key": True})
        elif char == "[":
            stack.append({"type": "arr", "index": 0})
        elif char in "}]":
            stack.pop()
        elif char == "," and stack:
            if stack[-1]["type"] == "obj":
                stack[-1]["expect_key"] = True
            else:
                stack[-1]["index"] += 1
        i += 1
    out.append(text[last:])
    return "".join(out)


def _migrate_file(path: Path, file_sites: list[tuple[str, str]], *,
                  dry_run: bool) -> dict[str, Any]:
    raw = path.read_bytes().decode("utf-8-sig")
    document = json.loads(raw)
    changes: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    renames: dict[tuple[Any, ...], dict[str, str]] = {}
    dropped = False
    records_changed = 0
    for site_path, object_name in file_sites:
        # Any path the reference can declare - a list, a map, one object, the document itself. The
        # record is changed IN PLACE, so it stays where it was and the document keeps its order.
        for where, record in shared_objects.walk_records(document, site_path):
            if not isinstance(record, dict):
                continue
            new, record_changes, record_conflicts = field_names.standardize(record, object_name)
            for item in record_conflicts:
                conflicts.append({"where": where, **item})
            if record_changes:
                records_changed += 1
                changes.extend({"where": where, **item} for item in record_changes)
                record.clear()
                record.update(new)
                renames[_path_of(where)] = {item["field"]: item["to"] for item in record_changes
                                            if item["action"] == "renamed"}
                dropped = dropped or any(item["action"] != "renamed" for item in record_changes)
    written = bool(changes) and not conflicts and not dry_run
    if written:
        text = None if dropped else _rename_keys_in_text(raw, renames)
        # The text rewrite is trusted only when it parses to exactly the migrated document.
        if text is None or json.loads(text) != document:
            text = json.dumps(document, ensure_ascii=False, indent=indent_of(path)) + "\n"
        atomic_write_text(path, text)
    return {"file": path.name, "records_changed": records_changed, "changes": changes,
            "conflicts": conflicts, "written": written}


#: The file that says how each data file's records are keyed and labelled in the runtime store. It
#: names record FIELDS, so a rename moves there too.
CATALOGUE_FILE = "config_catalog.json"


def _objects_of(sites: dict[str, list[tuple[str, str]]], source_file: str,
                collection: str) -> list[str]:
    """The record kinds a catalogue collection holds: the reference's paths for that file, read
    the way the catalogue names them (``sql_commands[]`` is the collection ``sql_commands``)."""
    return [object_name for site_path, object_name in sites.get(source_file, [])
            if re.sub(r"(\[\]|\{\})$", "", site_path) == collection]


def _migrate_catalogue(path: Path, sites: dict[str, list[tuple[str, str]]], *,
                       dry_run: bool) -> dict[str, Any]:
    """A catalogue collection's ``label_field`` and ``key_fields`` follow the records' renames.

    The records of ``sql_commands.json`` moved ``sql_name`` -> ``display_name`` in 0.22.0 and the
    catalogue a node was given before that still labelled them by ``sql_name``: every record's label
    read empty on that node while the master's catalogue gave the display name, so each sync from one
    side rewrote all 30 records the other had written - content unchanged - and every worker upgrade
    reported "30 updated" for a drift that was never there (0.27.0 item 1.95).
    """
    raw = path.read_bytes().decode("utf-8-sig")
    document = json.loads(raw)
    changes: list[dict[str, Any]] = []
    text = raw
    whole = False
    for source_index, source in enumerate(document.get("config_sources") or []):
        if not isinstance(source, dict):
            continue
        for collection_index, collection in enumerate(source.get("collections") or []):
            if not isinstance(collection, dict):
                continue
            objects = _objects_of(sites, str(source.get("file") or ""),
                                  str(collection.get("collection") or ""))
            where = f"config_sources[{source_index}].collections[{collection_index}]"
            label = collection.get("label_field")
            if isinstance(label, str) and label:
                standard = next((field_names.standard_of(name, label) for name in objects
                                 if field_names.standard_of(name, label) != label), label)
                if standard != label:
                    changes.append({"where": where, "field": "label_field", "from": label,
                                    "to": standard, "action": "renamed"})
                    collection["label_field"] = standard
                    text = re.sub(
                        rf'("collection"\s*:\s*"{re.escape(str(collection.get("collection")))}"[^{{}}]*?'
                        rf'"label_field"\s*:\s*)"{re.escape(label)}"',
                        lambda match, new=standard: f'{match.group(1)}"{new}"', text, count=1)
            keys = collection.get("key_fields")
            if isinstance(keys, list):
                moved = [next((field_names.standard_of(name, str(key)) for name in objects
                               if field_names.standard_of(name, str(key)) != key), key) for key in keys]
                if moved != keys:
                    changes.append({"where": where, "field": "key_fields", "from": keys,
                                    "to": moved, "action": "renamed"})
                    collection["key_fields"] = moved
                    whole = True  # a key list's layout varies too much to patch in the text
    written = bool(changes) and not dry_run
    if written:
        if whole or json.loads(text) != document:
            text = json.dumps(document, ensure_ascii=False, indent=indent_of(path)) + "\n"
        atomic_write_text(path, text)
    return {"file": path.name, "records_changed": len(changes), "changes": changes,
            "conflicts": [], "written": written}


def standardize(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    root = shared_objects.data_root(request.get("data_dir") or None)
    dry_run = request.get("dry_run", True) is not False
    only = str(request.get("file") or "").strip()
    sites = _sites()
    only_file = only.replace(".example.json", ".json")
    if only and only_file not in sites and only_file != CATALOGUE_FILE:
        raise ValueError(f"{only} holds no record with a renamed field; the files that do are: "
                         + ", ".join(sorted(sites) + [CATALOGUE_FILE]))

    files: list[dict[str, Any]] = []
    for file_name, file_sites in sorted(sites.items()):
        if only and only.replace(".example.json", ".json") != file_name:
            continue
        for candidate in (file_name, file_name.replace(".json", ".example.json")):
            path = root / candidate
            if path.is_file():
                files.append(_migrate_file(path, file_sites, dry_run=dry_run))
    if not only or only_file == CATALOGUE_FILE:
        for candidate in (CATALOGUE_FILE, CATALOGUE_FILE.replace(".json", ".example.json")):
            path = root / candidate
            if path.is_file():
                files.append(_migrate_catalogue(path, sites, dry_run=dry_run))

    records = sum(item["records_changed"] for item in files)
    conflicts = sum(len(item["conflicts"]) for item in files)
    verb = "would change" if dry_run else "changed"
    message = (f"{verb} {records} record(s) in "
               f"{sum(1 for item in files if item['records_changed'])} file(s)"
               + (f"; {conflicts} CONFLICT(S) - those files were not written" if conflicts else "")
               + ("; dry run, nothing written - pass dry_run false to write" if dry_run else ""))
    return message, {"dry_run": dry_run, "data_dir": str(root), "files": files,
                     "records_changed": records, "conflicts": conflicts}
