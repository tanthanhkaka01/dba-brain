"""An edit made in the console must reach the file the apps actually read — and be refusable.

The store is a mirror, not the thing db_ops runs from: the apps still read ``data/*.json``. So the
property that matters most here is not that a row changed, it is that **the file changed too**. An
edit that stopped at the store would be one the operator watched succeed and that nothing acted
on, which is worse than an edit that failed.

The rest is what the write path has to refuse, and each refusal is a mistake that is cheap to make
and expensive to find later:

* renaming a record in place — the row keeps its old key and the file gains a second record;
* re-using a key that is live — two records, one identity;
* a literal secret — these rows are queried, rendered and backed up;
* retiring the ``__document__`` row — that is "empty the file", never what was meant.

Retiring still never deletes: the row, its JSON and its trail stay, and the key becomes free.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import write_catalogued_data

from db_ops.db import config_edit, config_sync
from db_ops.db.config_store import DOCUMENT_COLLECTION, DOCUMENT_KEY, ConfigStore

REPO_ROOT = Path(__file__).resolve().parents[1]

NEW_USER = {
    "user_id": "424242", "is_bot": False, "user_type": 10, "first_name": "Web",
    "last_name": "", "username": "webadded", "language_code": "en", "status": "active",
    "note": "Added from the console.",
}


@pytest.fixture(scope="module")
def _template(tmp_path_factory) -> tuple[Path, Path]:
    """A synced store and a normalised ``data/``, built once and copied per test.

    Syncing 377 records for every test was most of this file's runtime. The export in here is not
    setup noise either: it settles the hand-edited whitespace in a few files up front, so a later
    assertion about "one line changed" is about the record and not about a stray blank line.
    """
    root = tmp_path_factory.mktemp("config-edit-template")
    data = root / "data"
    write_catalogued_data(data)
    store_path = root / "template.sqlite"
    store = ConfigStore(store_path)
    config_sync.sync(store, data_dir=data, actor="setup")
    config_sync.export(store, data_dir=data)
    # Fold the write-ahead log into the file before it is copied: in WAL mode a fresh row can live
    # entirely in the -wal sidecar, and a copy of the main file alone would hand each test an
    # empty store with a full schema.
    checkpoint = sqlite3.connect(store_path)
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint.close()
    return store_path, data


@pytest.fixture()
def data_copy(tmp_path: Path, _template) -> Path:
    target = tmp_path / "data"
    shutil.copytree(_template[1], target)
    return target


@pytest.fixture()
def store(tmp_path: Path, _template) -> ConfigStore:
    store_path = tmp_path / "db_ops.sqlite"
    shutil.copy(_template[0], store_path)
    return ConfigStore(store_path)


def users_on_disk(data_dir: Path) -> list[dict]:
    return json.loads((data_dir / "telegram_users.json").read_text(encoding="utf-8"))["telegram_users"]


# --------------------------------------------------------------------------- #
# The file is what ships
# --------------------------------------------------------------------------- #
def test_a_new_record_reaches_the_file_the_apps_read(store: ConfigStore, data_copy: Path) -> None:
    result = config_edit.save_record(
        store, source_file="telegram_users.json", collection="telegram_users",
        payload=NEW_USER, actor="thanh", data_dir=data_copy)

    assert result["action"] == "inserted"
    assert result["file"]["status"] == "written"
    assert any(user["user_id"] == "424242" for user in users_on_disk(data_copy)), (
        "an edit that only reached the store is an edit nothing acts on")


def test_an_edit_changes_the_record_and_nothing_else_in_the_file(store: ConfigStore,
                                                                 data_copy: Path) -> None:
    before = (data_copy / "telegram_users.json").read_text(encoding="utf-8")
    record = dict(users_on_disk(data_copy)[0])
    record["user_type"] = 42
    config_edit.save_record(store, source_file="telegram_users.json",
                            collection="telegram_users", payload=record,
                            item_key=record["user_id"], actor="thanh", data_dir=data_copy)

    after = (data_copy / "telegram_users.json").read_text(encoding="utf-8")
    changed = [line for line in after.splitlines() if line not in before.splitlines()]
    assert changed == ['      "user_type": 42,'], (
        f"one field changed, so one line should differ; got {changed}")


def test_a_record_keeps_its_position_when_edited(store: ConfigStore, data_copy: Path) -> None:
    """``app_commands`` is read in order, and reports render in the order configured.

    A save that appended the edited record to the end would reorder the file on every change.
    """
    keys_before = [user["user_id"] for user in users_on_disk(data_copy)]
    record = dict(users_on_disk(data_copy)[0])
    record["note"] = "Edited."
    config_edit.save_record(store, source_file="telegram_users.json",
                            collection="telegram_users", payload=record,
                            item_key=record["user_id"], actor="thanh", data_dir=data_copy)
    assert [user["user_id"] for user in users_on_disk(data_copy)] == keys_before


def test_the_file_settings_row_can_be_edited(store: ConfigStore, data_copy: Path) -> None:
    """The ``__document__`` row is the file minus its records — editable, never deletable."""
    before = len(json.loads((data_copy / "reports_config.json").read_text(encoding="utf-8"))["reports"])
    document = json.loads(store.get_item(source_file="reports_config.json",
                                         collection=DOCUMENT_COLLECTION,
                                         item_key=DOCUMENT_KEY)["item_json"])
    document["report_base_url"] = "http://example.internal/report_dba/"
    config_edit.save_record(store, source_file="reports_config.json",
                            collection=DOCUMENT_COLLECTION, payload=document,
                            item_key=DOCUMENT_KEY, actor="thanh", data_dir=data_copy)

    written = json.loads((data_copy / "reports_config.json").read_text(encoding="utf-8"))
    assert written["report_base_url"] == "http://example.internal/report_dba/"
    assert len(written["reports"]) == before, "the record collection must survive a document edit"


# --------------------------------------------------------------------------- #
# Retiring
# --------------------------------------------------------------------------- #
def test_retiring_removes_it_from_the_file_and_keeps_the_row(store: ConfigStore,
                                                             data_copy: Path) -> None:
    target = users_on_disk(data_copy)[0]["user_id"]
    result = config_edit.delete_record(store, source_file="telegram_users.json",
                                       collection="telegram_users", item_key=target,
                                       actor="thanh", data_dir=data_copy)

    assert result["action"] == "deactivated"
    assert not any(user["user_id"] == target for user in users_on_disk(data_copy))
    kept = [row for row in store.list_items(source_file="telegram_users.json",
                                            include_inactive=True)
            if row["item_key"] == target]
    assert len(kept) == 1 and kept[0]["is_active"] == 0
    assert json.loads(kept[0]["item_json"])["user_id"] == target


def test_a_retired_key_can_be_used_again_from_the_console(store: ConfigStore,
                                                          data_copy: Path) -> None:
    config_edit.save_record(store, source_file="telegram_users.json",
                            collection="telegram_users", payload=NEW_USER,
                            actor="thanh", data_dir=data_copy)
    config_edit.delete_record(store, source_file="telegram_users.json",
                              collection="telegram_users", item_key="424242",
                              actor="thanh", data_dir=data_copy)
    again = config_edit.save_record(
        store, source_file="telegram_users.json", collection="telegram_users",
        payload={**NEW_USER, "first_name": "Someone else"}, actor="thanh", data_dir=data_copy)

    assert again["action"] == "inserted"
    rows = [row for row in store.list_items(source_file="telegram_users.json",
                                            include_inactive=True)
            if row["item_key"] == "424242"]
    assert sorted(row["is_active"] for row in rows) == [0, 1]


def test_the_document_row_cannot_be_retired(store: ConfigStore, data_copy: Path) -> None:
    with pytest.raises(config_edit.ConfigEditError, match="would empty the file"):
        config_edit.delete_record(store, source_file="reports_config.json",
                                  collection=DOCUMENT_COLLECTION, item_key=DOCUMENT_KEY,
                                  actor="thanh", data_dir=data_copy)


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
def test_renaming_a_record_in_place_is_refused(store: ConfigStore, data_copy: Path) -> None:
    """The row would keep its old key and the file would gain a second record under the new one."""
    with pytest.raises(config_edit.ConfigEditError, match="Renaming a record"):
        config_edit.save_record(store, source_file="telegram_users.json",
                                collection="telegram_users",
                                payload={**NEW_USER, "user_id": "999999"}, item_key="424242",
                                actor="thanh", data_dir=data_copy)


def test_adding_a_key_that_is_already_live_is_refused(store: ConfigStore, data_copy: Path) -> None:
    existing = users_on_disk(data_copy)[0]
    with pytest.raises(config_edit.ConfigEditError, match="already exists"):
        config_edit.save_record(store, source_file="telegram_users.json",
                                collection="telegram_users", payload=existing,
                                actor="thanh", data_dir=data_copy)


def test_a_payload_missing_a_key_field_is_refused(store: ConfigStore, data_copy: Path) -> None:
    with pytest.raises(config_edit.ConfigEditError, match="user_id"):
        config_edit.save_record(store, source_file="telegram_users.json",
                                collection="telegram_users", payload={"username": "no-key"},
                                actor="thanh", data_dir=data_copy)


def test_a_literal_secret_is_refused(store: ConfigStore, data_copy: Path) -> None:
    with pytest.raises(config_edit.ConfigEditError, match="encrypted_secret_text.json"):
        config_edit.save_record(store, source_file="telegram_users.json",
                                collection="telegram_users",
                                payload={**NEW_USER, "password": "hunter2"},
                                actor="thanh", data_dir=data_copy)


def test_an_unknown_file_or_collection_is_refused(store: ConfigStore, data_copy: Path) -> None:
    with pytest.raises(config_edit.ConfigEditError, match="not in config_catalog.json"):
        config_edit.save_record(store, source_file="invented.json", collection="x",
                                payload={}, actor="thanh", data_dir=data_copy)
    with pytest.raises(config_edit.ConfigEditError, match="is not a collection of"):
        config_edit.save_record(store, source_file="telegram_users.json", collection="nope",
                                payload={}, actor="thanh", data_dir=data_copy)


def test_a_record_array_cannot_be_smuggled_into_the_document_row(store: ConfigStore,
                                                                 data_copy: Path) -> None:
    """The export puts the collections back from their own rows; a copy here would be overwritten."""
    with pytest.raises(config_edit.ConfigEditError, match="one row per\\s+record"):
        config_edit.save_record(store, source_file="reports_config.json",
                                collection=DOCUMENT_COLLECTION,
                                payload={"report_base_url": "x", "reports": []},
                                item_key=DOCUMENT_KEY, actor="thanh", data_dir=data_copy)


def test_a_payload_that_is_not_an_object_is_refused(store: ConfigStore, data_copy: Path) -> None:
    with pytest.raises(config_edit.ConfigEditError, match="must be a JSON object"):
        config_edit.save_record(store, source_file="telegram_users.json",
                                collection="telegram_users", payload=[NEW_USER],
                                actor="thanh", data_dir=data_copy)


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
def test_the_history_spans_every_row_that_has_held_a_key(store: ConfigStore,
                                                         data_copy: Path) -> None:
    """A key retired and reused has two rows; "what has this been" means both of them."""
    config_edit.save_record(store, source_file="telegram_users.json",
                            collection="telegram_users", payload=NEW_USER,
                            actor="alice", data_dir=data_copy)
    config_edit.delete_record(store, source_file="telegram_users.json",
                              collection="telegram_users", item_key="424242",
                              actor="bob", data_dir=data_copy)
    config_edit.save_record(store, source_file="telegram_users.json",
                            collection="telegram_users",
                            payload={**NEW_USER, "first_name": "Reused"},
                            actor="carol", data_dir=data_copy)

    history = config_edit.record_history(store, source_file="telegram_users.json",
                                         collection="telegram_users", item_key="424242")
    assert [entry["change_type"] for entry in history] == ["insert", "deactivate", "insert"]
    assert {entry["changed_by"] for entry in history} == {"alice", "bob", "carol"}
    assert len({entry["config_item_id"] for entry in history}) == 2


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def test_export_is_a_no_op_when_nothing_changed(store: ConfigStore, data_copy: Path) -> None:
    """These files sit on a bind mount the deploy compares; an identical rewrite is still a diff."""
    result = config_sync.export(store, data_dir=data_copy)
    assert result["totals"]["written"] == 0
    assert result["totals"]["skipped"] == 0


def test_export_keeps_every_file_semantically_identical(store: ConfigStore,
                                                        data_copy: Path) -> None:
    before = {path.name: json.loads(path.read_text(encoding="utf-8-sig"))
              for path in data_copy.glob("*.json")}
    config_sync.export(store, data_dir=data_copy)
    after = {path.name: json.loads(path.read_text(encoding="utf-8-sig"))
             for path in data_copy.glob("*.json")}
    assert before == after


def test_a_sync_after_an_export_finds_nothing_to_do(store: ConfigStore, data_copy: Path) -> None:
    """The two directions have to agree, or config oscillates between the file and the store."""
    config_sync.export(store, data_dir=data_copy)
    result = config_sync.sync(store, data_dir=data_copy, actor="check")
    assert result["totals"]["inserted"] == 0
    assert result["totals"]["updated"] == 0
    assert result["totals"]["deactivated"] == 0


def test_export_refuses_to_write_a_file_the_store_knows_nothing_about(tmp_path: Path,
                                                                      data_copy: Path) -> None:
    """An empty result means "the sync has not run", never "the file should be empty"."""
    empty = ConfigStore(tmp_path / "empty.sqlite")
    result = config_sync.export(empty, data_dir=data_copy)
    assert result["totals"]["written"] == 0
    assert all(item["status"] == "skipped" for item in result["files"])
    assert users_on_disk(data_copy), "the real file must be untouched"
