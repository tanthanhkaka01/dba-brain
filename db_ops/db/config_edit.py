"""Changing one config record — the single write path behind the web console and the CLI.

The console needed "add / edit / delete a record", and there are two ways that could have been
built. The wrong one is for the HTTP handler to validate a form, write a row, and rewrite a file;
the next caller then writes its own version of those three steps and the two disagree about what a
valid record is. This module is the other way: **one function per operation**, taking the same
arguments whether the caller is a browser, a shell, or a test.

Every write does the same two things, in this order:

1. **the store row** — upserted (or retired) through :mod:`db_ops.db.config_store`, so the change
   gets a revision, an author and a timestamp;
2. **the file** — ``data/<source_file>.json`` rebuilt from the store by
   :func:`db_ops.db.config_sync.export_file`.

The second step is not bookkeeping. The apps still read ``data/*.json``, so a change that stopped
at the store would be one the operator watched succeed and that nothing acted on — the worst kind
of write. Rebuilding the whole file from the store (rather than patching the one record in place)
is what keeps the two identical by construction.

**What is refused, and why each one:**

* *An unknown file, or an unknown collection.* Without a catalog entry there are no declared key
  fields, so there is no way to say which record an edit is addressing.
* *A payload whose key fields do not match the record being edited.* Changing a
  ``metric_code`` in place would silently retire nothing and create nothing — the row would keep
  its old key and the file would gain a second record under it. Renaming is delete-then-add, and
  saying so is better than doing half of it.
* *A key that is already live.* The partial unique index would refuse it anyway; catching it here
  turns a database error into a sentence.
* *A literal secret.* Same rule as the sync: these rows are queried, rendered and backed up.
* *Deleting the document row.* It holds every non-record key in the file — the schedules block, the
  notes, the policy objects. "Delete" on it means "empty the file", which is never what was meant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db_ops.db.config_store import (
    DOCUMENT_COLLECTION,
    DOCUMENT_KEY,
    ConfigItem,
    ConfigStore,
)
from db_ops.db.config_sync import (
    ConfigSyncError,
    assert_no_plaintext_secret,
    collection_spec,
    export_file,
    spec_for,
)


class ConfigEditError(ValueError):
    """The requested change cannot be made as asked."""


def save_record(
    store: ConfigStore,
    *,
    source_file: str,
    collection: str,
    payload: Any,
    item_key: str | None = None,
    actor: str = "",
    note: str = "",
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Create or update one record, then rebuild its file.

    ``item_key`` is the record being edited; omit it to add a new one. For a keyed collection the
    key is always *derived from the payload* — passing one only says which record you believe you
    are editing, and a mismatch is refused rather than reconciled.
    """
    spec = _spec(source_file, data_dir)
    column = _collection(spec, collection)
    if not isinstance(payload, dict):
        raise ConfigEditError(
            f"A config record must be a JSON object; got {type(payload).__name__}.")

    if column is None:
        target_key = DOCUMENT_KEY
        _refuse_records_in_document(spec, payload)
    else:
        try:
            target_key = column.key_for(payload)
        except ConfigSyncError as exc:
            raise ConfigEditError(str(exc)) from exc
        if item_key is not None and str(item_key) != target_key:
            raise ConfigEditError(
                f"This record is keyed '{item_key}', but the payload says '{target_key}' "
                f"({', '.join(column.key_fields)}). Renaming a record is a delete and an add: "
                "the old key keeps its history and the new one starts its own.")
        existing = store.get_item(source_file=spec.file, collection=collection,
                                  item_key=target_key)
        if item_key is None and existing is not None:
            raise ConfigEditError(
                f"'{target_key}' already exists in {spec.file}:{collection}. Edit that record, "
                "or retire it first if this is meant to replace it.")

    try:
        assert_no_plaintext_secret(payload, where=f"{spec.file}:{collection}[{target_key}]")
    except ConfigSyncError as exc:
        raise ConfigEditError(str(exc)) from exc

    item = ConfigItem(
        source_file=spec.file,
        collection=collection,
        item_key=target_key,
        payload=payload,
        item_ord=_position_for(store, spec.file, collection, target_key),
        label=_label_for(column, payload, spec),
        note=note,
    )
    config_item_id, action = store.upsert_item(item, actor=actor, note=note)
    written = export_file(store, spec, data_dir=data_dir)
    return {
        "source_file": spec.file, "collection": collection, "item_key": target_key,
        "config_item_id": config_item_id, "action": action, "file": written,
    }


def delete_record(
    store: ConfigStore,
    *,
    source_file: str,
    collection: str,
    item_key: str,
    actor: str = "",
    note: str = "",
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Retire one record (``is_active = 0``) and rebuild its file without it.

    The row, its JSON and its revision trail stay. Its key becomes free again, so the same
    ``metric_code`` or ``group_id`` can be added back later as a **new** record with its own
    history — see :mod:`db_ops.db.config_store`.
    """
    spec = _spec(source_file, data_dir)
    _collection(spec, collection)
    if collection == DOCUMENT_COLLECTION:
        raise ConfigEditError(
            f"The {DOCUMENT_COLLECTION} row of {spec.file} holds every setting in the file that is "
            "not a keyed record. Retiring it would empty the file; edit it instead.")

    config_item_id = store.deactivate_item(
        source_file=spec.file, collection=collection, item_key=item_key,
        actor=actor, note=note or f"Retired from the console by {actor or 'an operator'}.")
    if config_item_id is None:
        raise ConfigEditError(
            f"No active record '{item_key}' in {spec.file}:{collection}.")
    written = export_file(store, spec, data_dir=data_dir)
    return {
        "source_file": spec.file, "collection": collection, "item_key": item_key,
        "config_item_id": config_item_id, "action": "deactivated", "file": written,
    }


def record_history(store: ConfigStore, *, source_file: str, collection: str,
                   item_key: str) -> list[dict[str, Any]]:
    """Every recorded state of one key, newest first, across **all** rows that have held it.

    Across rows, not within one: a key that was retired and later reused has two rows, and an
    operator asking "what has this metric_code been" means both. The rows are kept apart in the
    answer by ``config_item_id`` so it is clear where one record ended and the next began.
    """
    entries: list[dict[str, Any]] = []
    for row in store.list_items(source_file=source_file, collection=collection,
                                include_inactive=True):
        if str(row["item_key"]) != str(item_key):
            continue
        for revision in store.revisions(int(row["config_item_id"])):
            entries.append({
                "config_item_id": int(row["config_item_id"]),
                "revision": int(revision["revision"]),
                "change_type": str(revision["change_type"]),
                "changed_at": str(revision["changed_at"]),
                "changed_by": str(revision["changed_by"]),
                "note": str(revision["note"] or ""),
                "payload": json.loads(revision["item_json"]),
                "is_active": int(revision["is_active"]),
            })
    entries.sort(key=lambda item: (item["changed_at"], item["config_item_id"], item["revision"]),
                 reverse=True)
    return entries


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _spec(source_file: str, data_dir: str | Path | None):
    try:
        return spec_for(source_file, data_dir=data_dir)
    except ConfigSyncError as exc:
        raise ConfigEditError(str(exc)) from exc


def _collection(spec, collection: str):
    try:
        return collection_spec(spec, collection)
    except ConfigSyncError as exc:
        raise ConfigEditError(str(exc)) from exc


def _refuse_records_in_document(spec, payload: dict[str, Any]) -> None:
    """The document row must not carry a record array.

    It is the file *minus* its collections, and the export puts the collections back from their
    own rows. A ``metrics`` array smuggled in here would be written out and then immediately
    overwritten by the real one — or, worse, kept alongside it if the catalog ever changed.
    """
    clash = sorted({item.collection for item in spec.collections} & set(payload))
    if clash:
        raise ConfigEditError(
            f"{', '.join(clash)} is a record collection of {spec.file} and is stored one row per "
            "record, not inside the file's settings. Edit those records individually.")


def _position_for(store: ConfigStore, source_file: str, collection: str, item_key: str) -> int:
    """Where the record sits in its array: its current place, or the end for a new one.

    An edit must not move a record. ``app_commands`` is read in order and the reports render in
    the order they are configured, so a save that appended every edited record to the bottom
    would quietly reorder the file on each change.
    """
    highest = -1
    for row in store.list_items(source_file=source_file, collection=collection):
        if str(row["item_key"]) == str(item_key):
            return int(row["item_ord"])
        highest = max(highest, int(row["item_ord"]))
    return highest + 1


def _label_for(column, payload: dict[str, Any], spec) -> str:
    if column is None:
        return spec.display_name or spec.file
    if not column.label_field:
        return ""
    return str(payload.get(column.label_field, "") or "")
