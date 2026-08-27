"""Read ``data/*.json`` and mirror it into the store's ``config_*`` tables.

The store is becoming the place config is *read and edited* from (a web UI cannot edit a JSON file
on a worker's disk), but the files stay the deployed artefact for now. This module is the bridge:
it takes what the catalog declares and makes the store say the same thing.

**Nothing is dropped and nothing is invented.** Every file listed in ``data/config_catalog.json``
is split into its keyed records plus one ``__document__`` remainder holding everything else — the
scalars, the ``notes`` arrays, the nested policy objects. :func:`rebuild_payload` puts them back
together, and ``tests/test_config_sync.py`` holds every catalogued file to a round trip. That is
what makes the store a mirror rather than a lossy summary: a file whose remainder was silently
discarded could never be written back.

Three refusals, each from something that would otherwise be found much later:

* **Duplicate keys abort that file.** Two records with the same ``metric_code`` would have one
  quietly overwrite the other, and the store would show 89 metrics where the file has 90.
* **A plaintext secret aborts that file.** These rows are queried, rendered in a UI and copied
  into backups. ``password_ref`` (a name) is fine and is what db_ops actually uses; a literal
  ``password`` is not, and the sync says which file and which path rather than storing it.
* **A missing file is reported, not applied.** Deactivating every record of a file that simply is
  not on this node — master-only config on a worker, say — would look exactly like an operator
  deleting them all.

Removal is soft, always: a record that has left the file is flagged ``is_active = 0`` and keeps
its row and its history. See :mod:`db_ops.db.config_store` for why that is the only delete.

**It runs both ways.** :func:`sync` reads the files into the store; :func:`export` writes the store
back out. The second direction is what makes an edit in the web console *take effect*: the apps
still read ``data/*.json``, so a change that only reached the store would be a change the operator
watched succeed and that nothing acted on. Every console write therefore does both, and the file
is rebuilt from the store rather than patched in place — the same :func:`rebuild_payload` the round
trip is tested with, so file and store cannot drift apart by construction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db_ops.lib.json_io import atomic_write_text, dump_json_text
from db_ops.lib.paths import CONFIG_CATALOG_FILENAME
from db_ops.db.config_store import (
    DOCUMENT_COLLECTION,
    DOCUMENT_KEY,
    ConfigItem,
    ConfigStore,
)

#: The catalog: which files are synced, who owns them, how a record is keyed. Lives in ``data/``
#: with the config it describes, because it *is* config — see CLAUDE.md, "Config is data".
#:
#: Re-exported rather than restated: ``db_ops.lib.config_bundle`` needs the same name to decide
#: what crosses to another machine, and a filename with two definitions is a filename that has
#: two values the week one of them changes. The alias stays because this module's readers know it
#: by this name.
CATALOG_FILENAME = CONFIG_CATALOG_FILENAME

#: Keys whose value is a secret itself rather than a reference to one. db_ops stores secrets in
#: ``data/encrypted_secret_text.json`` and names them by ref, so any of these carrying a non-empty
#: string means a file has been edited in a way that would put a password in the store.
#: ``password_ref`` / ``password_env`` are names, not secrets, and are deliberately absent.
SECRET_KEYS = frozenset({
    "password", "passwd", "pwd", "secret", "api_key", "apikey", "private_key",
    "bot_token", "token", "access_token",
})


class ConfigSyncError(ValueError):
    """A config file cannot be synced as written."""


@dataclass(frozen=True)
class CollectionSpec:
    """One keyed array inside a config file, as the catalog declares it."""

    collection: str
    key_fields: tuple[str, ...]
    label_field: str = ""

    def key_for(self, record: dict[str, Any]) -> str:
        """The record's identity, as one string.

        Joined with ``|`` because a composite key has to fit one column to be uniquely indexed,
        and ``sql_targets`` genuinely needs two fields (``sql_id`` + ``target_no``). A missing
        field is refused rather than defaulted: an empty half of a key silently collides with
        every other record missing the same field.
        """
        parts: list[str] = []
        for field_name in self.key_fields:
            if field_name not in record:
                raise ConfigSyncError(
                    f"record in '{self.collection}' has no '{field_name}' to key it by: "
                    f"{sorted(record)[:8]}")
            parts.append(str(record[field_name]))
        return "|".join(parts)


@dataclass(frozen=True)
class SourceSpec:
    """One config file, as the catalog declares it."""

    file: str
    app_code: str
    display_name: str = ""
    description: str = ""
    collections: tuple[CollectionSpec, ...] = field(default_factory=tuple)


def load_catalog(data_dir: str | Path | None = None) -> tuple[SourceSpec, ...]:
    """Parse ``data/config_catalog.json`` into specs. Raises on a catalog that cannot be honoured."""
    path = _data_dir(data_dir) / CATALOG_FILENAME
    if not path.is_file():
        raise ConfigSyncError(f"Config catalog not found: {path}")
    raw = read_json_file(path)
    entries = raw.get("config_sources")
    if not isinstance(entries, list):
        raise ConfigSyncError(f"{CATALOG_FILENAME} must hold a 'config_sources' array.")

    sources: list[SourceSpec] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigSyncError(f"{CATALOG_FILENAME}: every config_sources entry must be an object.")
        file_name = str(entry.get("file") or "").strip()
        app_code = str(entry.get("app_code") or "").strip()
        if not file_name or not app_code:
            raise ConfigSyncError(
                f"{CATALOG_FILENAME}: every entry needs 'file' and 'app_code'; got {entry!r}.")
        if file_name in seen:
            raise ConfigSyncError(f"{CATALOG_FILENAME}: '{file_name}' is listed twice.")
        seen.add(file_name)

        collections: list[CollectionSpec] = []
        for block in entry.get("collections") or []:
            if not isinstance(block, dict):
                raise ConfigSyncError(f"{CATALOG_FILENAME}: {file_name} has a non-object collection.")
            name = str(block.get("collection") or "").strip()
            key_fields = tuple(str(item) for item in (block.get("key_fields") or []))
            if not name or not key_fields:
                raise ConfigSyncError(
                    f"{CATALOG_FILENAME}: {file_name}.{name or '?'} needs 'collection' and "
                    "at least one 'key_fields' entry.")
            if name == DOCUMENT_COLLECTION:
                raise ConfigSyncError(
                    f"{CATALOG_FILENAME}: '{DOCUMENT_COLLECTION}' is reserved for the remainder "
                    f"of a file and cannot be declared ({file_name}).")
            collections.append(CollectionSpec(
                collection=name, key_fields=key_fields,
                label_field=str(block.get("label_field") or "").strip(),
            ))
        sources.append(SourceSpec(
            file=file_name, app_code=app_code,
            display_name=str(entry.get("display_name") or "").strip(),
            description=str(entry.get("description") or "").strip(),
            collections=tuple(collections),
        ))
    return tuple(sources)


def read_json_file(path: Path) -> dict[str, Any]:
    """Read one config file as an object.

    ``utf-8-sig`` because ``data/bot_telegram.json`` is written with a BOM — a plain ``utf-8``
    read fails it with "Unexpected UTF-8 BOM", and a sync that skipped a file over a byte order
    mark would be a silent gap rather than an error.
    """
    text = path.read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ConfigSyncError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigSyncError(f"{path.name} must hold a JSON object at the top level.")
    return payload


def assert_no_plaintext_secret(payload: Any, *, where: str) -> None:
    """Refuse a payload carrying a literal secret, naming the path that holds it.

    Walks the whole record, because the offending key is rarely at the top level — a credential
    block sits three levels down inside ``users.json``.
    """
    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if str(key).strip().lower() in SECRET_KEYS and isinstance(value, str) and value.strip():
                    raise ConfigSyncError(
                        f"{where}: '{child}' holds a literal secret. db_ops stores secrets in "
                        "data/encrypted_secret_text.json and names them by ref; put the value "
                        "there and reference it (e.g. 'password_ref') instead of syncing it "
                        "into the store.")
                walk(value, child)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(payload, "")


def split_payload(spec: SourceSpec, payload: dict[str, Any]) -> list[ConfigItem]:
    """Split one file into its keyed records plus the ``__document__`` remainder.

    The remainder always exists, even when it is empty, so every file has exactly one row that
    :func:`rebuild_payload` can start from and every file is represented in the store even if all
    its records are later retired.
    """
    items: list[ConfigItem] = []
    remainder = {key: value for key, value in payload.items()
                 if key not in {c.collection for c in spec.collections}}

    for collection in spec.collections:
        records = payload.get(collection.collection)
        if records is None:
            # A declared collection the file does not have. Not an error: the same catalog is
            # deployed to nodes whose files legitimately differ (a worker with no lab databases
            # registered yet). It simply contributes no records.
            continue
        if not isinstance(records, list):
            raise ConfigSyncError(
                f"{spec.file}: '{collection.collection}' must be an array, got "
                f"{type(records).__name__}.")
        seen: dict[str, int] = {}
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ConfigSyncError(
                    f"{spec.file}: {collection.collection}[{index}] must be an object.")
            try:
                item_key = collection.key_for(record)
            except ConfigSyncError as exc:
                raise ConfigSyncError(f"{spec.file}: {exc}") from exc
            if item_key in seen:
                raise ConfigSyncError(
                    f"{spec.file}: {collection.collection}[{index}] repeats the key "
                    f"'{item_key}' already used by index {seen[item_key]} "
                    f"(key fields: {', '.join(collection.key_fields)}). Two records with one key "
                    "cannot both be stored; fix the file rather than losing one of them.")
            seen[item_key] = index
            assert_no_plaintext_secret(
                record, where=f"{spec.file}:{collection.collection}[{item_key}]")
            label = str(record.get(collection.label_field, "") or "") if collection.label_field else ""
            items.append(ConfigItem(
                source_file=spec.file, collection=collection.collection, item_key=item_key,
                payload=record, item_ord=index, label=label,
            ))

    assert_no_plaintext_secret(remainder, where=f"{spec.file}:{DOCUMENT_COLLECTION}")
    items.append(ConfigItem(
        source_file=spec.file, collection=DOCUMENT_COLLECTION, item_key=DOCUMENT_KEY,
        payload=remainder, item_ord=0, label=spec.display_name or spec.file,
    ))
    return items


def rebuild_payload(spec: SourceSpec, items: list[ConfigItem]) -> dict[str, Any]:
    """The file's content, rebuilt from its rows. The inverse of :func:`split_payload`.

    Exists so the mirror can be proven lossless, and so a later "write the store back to
    ``data/``" step has one implementation rather than one per caller. Records come back in
    ``item_ord`` order because JSON arrays are ordered and several of them are read in order.
    """
    document: dict[str, Any] = {}
    per_collection: dict[str, list[ConfigItem]] = {}
    for item in items:
        if item.collection == DOCUMENT_COLLECTION:
            if isinstance(item.payload, dict):
                document.update(item.payload)
            continue
        per_collection.setdefault(item.collection, []).append(item)

    rebuilt = dict(document)
    for collection in spec.collections:
        rows = per_collection.get(collection.collection)
        if rows is None:
            continue
        rebuilt[collection.collection] = [
            row.payload for row in sorted(rows, key=lambda r: (r.item_ord, r.item_key))
        ]
    return rebuilt


def sync(
    store: ConfigStore,
    *,
    data_dir: str | Path | None = None,
    files: tuple[str, ...] | list[str] = (),
    apps: tuple[str, ...] | list[str] = (),
    actor: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Mirror the catalogued files into ``store``. Returns a per-file summary.

    ``files`` / ``apps`` narrow the run; empty means everything in the catalog. ``dry_run``
    computes the same plan and writes no config row, which is how a first run against a live
    store is checked before it touches anything.
    """
    catalog = load_catalog(data_dir)
    root = _data_dir(data_dir)
    wanted_files = {str(name) for name in files}
    wanted_apps = {str(name) for name in apps}

    results: list[dict[str, Any]] = []
    totals = {"inserted": 0, "updated": 0, "unchanged": 0, "deactivated": 0,
              "missing": 0, "failed": 0}

    for spec in catalog:
        if wanted_files and spec.file not in wanted_files:
            continue
        if wanted_apps and spec.app_code not in wanted_apps:
            continue
        outcome = _sync_one(store, spec, root=root, actor=actor, dry_run=dry_run)
        results.append(outcome)
        for key in totals:
            totals[key] += int(outcome.get(key) or 0)

    # A file dropped from the catalog stops being config, and its rows have to stop being
    # active — otherwise the console goes on offering an editor for something nothing reads.
    # Only on a full run: a filtered sync has not looked at the other files and must not conclude
    # anything about them.
    retired_sources: list[dict[str, Any]] = []
    if not wanted_files and not wanted_apps:
        retired_sources = _retire_uncatalogued(store, catalog, actor=actor, dry_run=dry_run)
        totals["deactivated"] += sum(int(item["deactivated"]) for item in retired_sources)

    unknown = sorted(wanted_files - {spec.file for spec in catalog})
    return {
        "dry_run": bool(dry_run),
        "data_dir": str(root),
        "files": results,
        "totals": totals,
        # A typo in --files would otherwise sync nothing and report success.
        "unknown_files": unknown,
        # Files the store still had rows for and the catalog no longer lists.
        "retired_sources": retired_sources,
    }


def _retire_uncatalogued(store: ConfigStore, catalog: tuple[SourceSpec, ...], *,
                         actor: str, dry_run: bool) -> list[dict[str, Any]]:
    """Retire every active row belonging to a file the catalog no longer lists.

    Soft, like every other removal here: the rows keep their JSON and their history, and the keys
    become free. What changes is that the console stops listing the file and the export stops
    writing it — which is the whole point of dropping it.
    """
    catalogued = {spec.file for spec in catalog}
    retired: list[dict[str, Any]] = []
    for source in store.list_sources():
        source_file = str(source["source_file"])
        if source_file in catalogued:
            continue
        rows = store.list_items(source_file=source_file)
        if not rows:
            continue
        retired.append({"file": source_file, "deactivated": len(rows)})
        if dry_run:
            continue
        for row in rows:
            store.deactivate_item(
                source_file=source_file, collection=str(row["collection"]),
                item_key=str(row["item_key"]), actor=actor,
                note=f"{source_file} is no longer in {CATALOG_FILENAME}.")
        store.deactivate_source(source_file=source_file, actor=actor)
    return retired


def _sync_one(store: ConfigStore, spec: SourceSpec, *, root: Path, actor: str,
              dry_run: bool) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "file": spec.file, "app_code": spec.app_code, "status": "ok",
        "inserted": 0, "updated": 0, "unchanged": 0, "deactivated": 0,
        "missing": 0, "failed": 0, "error": "",
    }
    path = root / spec.file
    if not path.is_file():
        # Reported, never applied: see the module docstring.
        outcome.update(status="missing", missing=1,
                       error=f"{spec.file} is not present under {root}.")
        return outcome

    try:
        payload = read_json_file(path)
        items = split_payload(spec, payload)
    except ConfigSyncError as exc:
        outcome.update(status="failed", failed=1, error=str(exc))
        return outcome

    if dry_run:
        return _plan_only(store, spec, items, outcome)

    store.ensure_source(source_file=spec.file, app_code=spec.app_code,
                        display_name=spec.display_name, description=spec.description)
    store.ensure_collection(source_file=spec.file, collection=DOCUMENT_COLLECTION,
                            key_fields=(DOCUMENT_KEY,), label_field="",
                            collection_ord=len(spec.collections))
    for index, collection in enumerate(spec.collections):
        store.ensure_collection(source_file=spec.file, collection=collection.collection,
                                key_fields=collection.key_fields,
                                label_field=collection.label_field, collection_ord=index)

    present: set[tuple[str, str]] = set()
    for item in items:
        _, action = store.upsert_item(item, actor=actor)
        outcome[action] = int(outcome[action]) + 1
        present.add((item.collection, item.item_key))

    for row in store.list_items(source_file=spec.file):
        key = (str(row["collection"]), str(row["item_key"]))
        if key in present:
            continue
        store.deactivate_item(source_file=spec.file, collection=key[0], item_key=key[1],
                              actor=actor, note=f"No longer present in {spec.file}.")
        outcome["deactivated"] = int(outcome["deactivated"]) + 1
    return outcome


def _plan_only(store: ConfigStore, spec: SourceSpec, items: list[ConfigItem],
               outcome: dict[str, Any]) -> dict[str, Any]:
    """What :func:`_sync_one` would do, computed against the store without writing.

    Deliberately reads through the same ``list_items`` the write path uses, so a dry run cannot
    disagree with the run it is predicting about which rows already exist.
    """
    existing = {(str(row["collection"]), str(row["item_key"])): row
                for row in store.list_items(source_file=spec.file)}
    present: set[tuple[str, str]] = set()
    for item in items:
        key = (item.collection, item.item_key)
        present.add(key)
        row = existing.get(key)
        if row is None:
            outcome["inserted"] = int(outcome["inserted"]) + 1
        elif (str(row["content_hash"]) == item.hash
              and int(row["item_ord"]) == int(item.item_ord)
              and str(row["label"] or "") == str(item.label or "")):
            outcome["unchanged"] = int(outcome["unchanged"]) + 1
        else:
            outcome["updated"] = int(outcome["updated"]) + 1
    outcome["deactivated"] = len(set(existing) - present)
    return outcome


def export_file(store: ConfigStore, spec: SourceSpec, *,
                data_dir: str | Path | None = None,
                dry_run: bool = False) -> dict[str, Any]:
    """Rebuild one ``data/*.json`` from the store. Returns what happened.

    Only the file's **active** rows are written, so a record retired in the console disappears
    from the file — which is what makes the soft delete a real delete as far as the apps are
    concerned, while the row and its history stay in the store.

    The file is left untouched when the rebuilt text matches what is already there. That is not an
    optimisation: these files are on a bind mount the deploy compares, and rewriting one with
    identical content still changes its mtime and makes a `git status` look like an edit nobody
    made.
    """
    root = _data_dir(data_dir)
    path = root / spec.file
    items = [
        ConfigItem(
            source_file=str(row["source_file"]),
            collection=str(row["collection"]),
            item_key=str(row["item_key"]),
            payload=json.loads(row["item_json"]),
            item_ord=int(row["item_ord"]),
            label=str(row["label"] or ""),
        )
        for row in store.list_items(source_file=spec.file)
    ]
    if not items:
        # Nothing mirrored for this file. Writing "{}" over a real config would be catastrophic
        # and is never what an empty result means — it means the sync has not run for this file.
        return {"file": spec.file, "status": "skipped", "written": False,
                "error": f"{spec.file} has no rows in the store; run sync-config first."}

    text = dump_json_text(rebuild_payload(spec, items), indent=_indent_for(path))
    existing = path.read_text(encoding="utf-8-sig") if path.is_file() else None
    if existing == text:
        return {"file": spec.file, "status": "unchanged", "written": False, "error": ""}
    if dry_run:
        return {"file": spec.file, "status": "differs", "written": False, "error": "",
                "detail": _describe_drift(spec, existing, text)}
    atomic_write_text(path, text)
    return {"file": spec.file, "status": "written", "written": True, "error": ""}


def _describe_drift(spec: SourceSpec, existing: str | None, rebuilt: str) -> str:
    """One line naming *what* differs between the file and the store.

    Whitespace-only differences are called out separately because they are the harmless case — a
    file that was hand-formatted and has since been normalised — and an operator deciding whether
    to keep their own edits needs to know which kind of drift they are looking at.
    """
    if existing is None:
        return "the file is missing; the store would create it"
    try:
        on_disk = json.loads(existing)
        from_store = json.loads(rebuilt)
    except ValueError:
        return "the file on disk is not readable as JSON"
    if on_disk == from_store:
        return "formatting only (same content)"

    changed = _changed_keys(spec, on_disk, from_store)
    if not changed:
        return "content differs"
    shown = ", ".join(changed[:6])
    return f"content differs: {shown}" + (f" and {len(changed) - 6} more" if len(changed) > 6 else "")


def _changed_keys(spec: SourceSpec, on_disk: Any, from_store: Any) -> list[str]:
    """Which records differ, by their catalogued key, so a report names them the way an operator does."""
    changed: list[str] = []
    for collection in spec.collections:
        disk_rows = {_safe_key(collection, item): item
                     for item in (on_disk.get(collection.collection) or [])
                     if isinstance(item, dict)}
        store_rows = {_safe_key(collection, item): item
                      for item in (from_store.get(collection.collection) or [])
                      if isinstance(item, dict)}
        for key in dict.fromkeys(list(store_rows) + list(disk_rows)):
            if disk_rows.get(key) != store_rows.get(key):
                where = "added" if key not in disk_rows else (
                    "removed" if key not in store_rows else "changed")
                changed.append(f"{collection.collection}[{key}] {where}")
    document_disk = {k: v for k, v in on_disk.items()
                     if k not in {c.collection for c in spec.collections}}
    document_store = {k: v for k, v in from_store.items()
                      if k not in {c.collection for c in spec.collections}}
    if document_disk != document_store:
        changed.append("file settings changed")
    return changed


def _safe_key(collection: CollectionSpec, record: dict[str, Any]) -> str:
    try:
        return collection.key_for(record)
    except ConfigSyncError:
        return "?"


def export(store: ConfigStore, *, data_dir: str | Path | None = None,
           files: tuple[str, ...] | list[str] = (),
           apps: tuple[str, ...] | list[str] = (),
           dry_run: bool = False) -> dict[str, Any]:
    """Write the store back to ``data/``. The inverse of :func:`sync`.

    ``dry_run`` reports which files *would* change and how, writing nothing — that is what
    :func:`drift` and the deploy gate are built on.
    """
    catalog = load_catalog(data_dir)
    wanted_files = {str(name) for name in files}
    wanted_apps = {str(name) for name in apps}
    results: list[dict[str, Any]] = []
    for spec in catalog:
        if wanted_files and spec.file not in wanted_files:
            continue
        if wanted_apps and spec.app_code not in wanted_apps:
            continue
        results.append(export_file(store, spec, data_dir=data_dir, dry_run=dry_run))
    return {
        "data_dir": str(_data_dir(data_dir)),
        "dry_run": bool(dry_run),
        "files": results,
        "totals": {
            "written": sum(1 for item in results if item["status"] == "written"),
            "differs": sum(1 for item in results if item["status"] == "differs"),
            "unchanged": sum(1 for item in results if item["status"] == "unchanged"),
            "skipped": sum(1 for item in results if item["status"] == "skipped"),
        },
        "unknown_files": sorted(wanted_files - {spec.file for spec in catalog}),
    }


def drift(store: ConfigStore, *, data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Files where the store and ``data/`` disagree, with what differs. Writes nothing.

    This is the question a deploy has to ask before it ships anything. The store is shared between
    master and worker; ``data/`` is per node and is what the deploy bundles. So an edit made in
    the web console lands in the store and in the *worker's* files, and the master's copy stays
    behind — and the next deploy from that master silently ships the old values back over it.

    Records whose payload matches but whose formatting does not are reported as
    ``formatting only``: the deploy has no reason to stop for those.
    """
    return [item for item in export(store, data_dir=data_dir, dry_run=True)["files"]
            if item["status"] == "differs"]


def content_drift(store: ConfigStore, *, data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Only the drift that changes what an app would read."""
    return [item for item in drift(store, data_dir=data_dir)
            if not str(item.get("detail", "")).startswith("formatting only")]


def _indent_for(path: Path) -> int:
    """The indent the file already uses, so a rewrite is not a whole-file diff.

    ``db_instances.json`` is written with one space and everything else with four. Guessing
    would turn a one-record edit into a 3,000-line diff, which is how a review stops being a
    review.
    """
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines()[1:]:
            stripped = line.lstrip(" ")
            if stripped and stripped != line:
                return len(line) - len(stripped)
    except OSError:
        pass
    return 4


def spec_for(source_file: str, *, data_dir: str | Path | None = None) -> SourceSpec:
    """The catalog entry for one file, or a refusal naming what is catalogued.

    Used by every edit path: a file that is not in the catalog has no declared keys, so there is
    no safe way to say which record an edit is addressing.
    """
    catalog = load_catalog(data_dir)
    for spec in catalog:
        if spec.file == source_file:
            return spec
    raise ConfigSyncError(
        f"'{source_file}' is not in {CATALOG_FILENAME}, so its records have no declared key. "
        f"Catalogued files: {', '.join(sorted(spec.file for spec in catalog))}.")


def collection_spec(spec: SourceSpec, collection: str) -> CollectionSpec | None:
    """The declared collection by name, or ``None`` for the document pseudo-collection."""
    if collection == DOCUMENT_COLLECTION:
        return None
    for item in spec.collections:
        if item.collection == collection:
            return item
    raise ConfigSyncError(
        f"'{collection}' is not a collection of {spec.file}. It has: "
        + (", ".join(item.collection for item in spec.collections) or "(none)"))


def _data_dir(data_dir: str | Path | None) -> Path:
    if data_dir:
        return Path(data_dir)
    from db_ops.lib.paths import DEFAULT_DATA_DIR

    return Path(DEFAULT_DATA_DIR)
