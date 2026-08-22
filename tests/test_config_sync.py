"""The config mirror must be complete, lossless, idempotent, and unable to forget a record.

The store is about to become the place config is read and edited from, which means these four
properties are the difference between a mirror and a second, quietly divergent source of truth:

* **Complete** — every config file under ``data/`` is either in the catalog or deliberately
  excluded. A file nobody listed is a file the web UI cannot show and nobody notices is missing.
* **Lossless** — a file split into rows and rebuilt is the file again. A remainder silently
  dropped on the way in could never be written back out.
* **Idempotent** — syncing an unchanged file writes nothing. Otherwise every run bumps a revision
  on every record and the change history becomes unreadable within a day.
* **Never forgetting** — a record removed from a file keeps its row with ``is_active = 0``, and
  re-adding the same key inserts a *new* row beside it rather than resurrecting the old one.
  That is the requirement the whole schema is shaped around.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import shipped_data_dir
from db_ops.db import config_sync
from db_ops.db.config_store import DOCUMENT_COLLECTION, DOCUMENT_KEY, ConfigStore

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The shipped configuration set — the operator's `data/` where there is one, the examples renamed
#: to the names they are examples of otherwise. This file's subject is the *set* of config files,
#: so it needs a whole directory rather than the individual files `shipped_config` hands out.
#:
#: Read at import, because `test_a_file_split_into_rows_rebuilds_into_the_same_file` parametrizes
#: over the catalog and a parametrize argument cannot ask for a fixture. That is also how this
#: file used to take the whole suite down on a clean checkout: a missing `config_catalog.json`
#: raised during *collection*, so `pytest tests` reported two collection errors and ran nothing.
DATA_DIR = shipped_data_dir()

#: Files under ``data/`` that are deliberately not mirrored, and why. Listed here rather than
#: pattern-matched so adding one is a decision somebody wrote down.
NOT_SYNCED = {
    "encrypted_secret_text.json": "the secret store itself; it never leaves data/",
    "postgresql_sla_examples.json": "a sample, not live config",
    "sre_test_config.json": "a test fixture",
    "database-inventory.json": "generated report output, rebuilt every run",
    "config_catalog.json": "the catalog itself; it describes the sync rather than being synced",
}

#: Catalogued files no direct filename search finds a reader for, with the reason. The guard below
#: exists to catch **dead** config; a file whose path is built at runtime rather than spelled would
#: trip it too, so there is a way to say "checked, it is read". The list only shrinks.
READ_INDIRECTLY: dict[str, str] = {}


@pytest.fixture()
def data_copy(tmp_path: Path) -> Path:
    """A writable copy of the real ``data/`` folder, so a test may edit a config file."""
    target = tmp_path / "data"
    shutil.copytree(DATA_DIR, target)
    return target


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "db_ops.sqlite")


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=4, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Complete
# --------------------------------------------------------------------------- #
def test_every_config_file_is_either_catalogued_or_deliberately_excluded() -> None:
    """A new data/*.json must be a decision, not an omission.

    Both directions, like ``tests/test_docs_cover_every_component.py``: a file nobody catalogued
    is invisible to the web UI, and a catalog entry for a file that was deleted is a sync that
    reports 'missing' forever.
    """
    on_disk = {p.name for p in DATA_DIR.glob("*.json") if ".example." not in p.name}
    catalogued = {spec.file for spec in config_sync.load_catalog(DATA_DIR)}

    unlisted = sorted(on_disk - catalogued - set(NOT_SYNCED))
    assert not unlisted, (
        "data/*.json file(s) neither in data/config_catalog.json nor in this file's NOT_SYNCED: "
        f"{unlisted}")

    gone = sorted(catalogued - on_disk)
    assert not gone, f"data/config_catalog.json lists file(s) that no longer exist: {gone}"


def test_every_catalogued_app_code_is_a_real_package() -> None:
    """``app_code`` is what the web UI groups by, so a typo would create a fourteenth app."""
    packages = {p.name for p in (REPO_ROOT / "db_ops").iterdir()
                if p.is_dir() and not p.name.startswith("__")}
    unknown = sorted({spec.app_code for spec in config_sync.load_catalog(DATA_DIR)} - packages)
    assert not unknown, f"config_catalog.json names app_code(s) with no db_ops/ package: {unknown}"


def test_the_catalog_keys_every_record_uniquely() -> None:
    """A collision here means one real record would overwrite another, silently, on every sync."""
    for spec in config_sync.load_catalog(DATA_DIR):
        path = DATA_DIR / spec.file
        if not path.is_file():
            continue
        # split_payload is what raises on a duplicate key; running it over the live files is the
        # check, and it also proves every declared key field exists on every record.
        config_sync.split_payload(spec, config_sync.read_json_file(path))


# --------------------------------------------------------------------------- #
# Lossless
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec", config_sync.load_catalog(DATA_DIR), ids=lambda s: s.file)
def test_a_file_split_into_rows_rebuilds_into_the_same_file(spec) -> None:
    """Split then rebuild must be the identity, for every real config file.

    Parametrized per file rather than looped, so a failure names the file that lost something.
    """
    path = DATA_DIR / spec.file
    if not path.is_file():
        pytest.skip(f"{spec.file} is not present on this node")
    original = config_sync.read_json_file(path)
    items = config_sync.split_payload(spec, original)
    assert config_sync.rebuild_payload(spec, items) == original


def test_the_leftover_of_a_file_is_kept_as_one_document_row(store: ConfigStore, data_copy: Path) -> None:
    """``metric_definitions.json`` is a records array *plus* schema_version, notes and collection.

    Those three are not records and have nowhere else to live; if the sync kept only the array,
    the store would hold 90 metrics and none of the settings that decide how they are collected.
    """
    config_sync.sync(store, data_dir=data_copy, files=["metric_definitions.json"])
    document = store.get_item(source_file="metric_definitions.json",
                              collection=DOCUMENT_COLLECTION, item_key=DOCUMENT_KEY)
    assert document is not None
    payload = json.loads(document["item_json"])
    assert "collection" in payload and "schema_version" in payload
    assert "metrics" not in payload, "the records array must not be duplicated into the document"


# --------------------------------------------------------------------------- #
# Idempotent
# --------------------------------------------------------------------------- #
def test_syncing_an_unchanged_file_writes_nothing(store: ConfigStore, data_copy: Path) -> None:
    first = config_sync.sync(store, data_dir=data_copy, files=["telegram_groups.json"])
    second = config_sync.sync(store, data_dir=data_copy, files=["telegram_groups.json"])

    assert first["totals"]["inserted"] > 0
    assert second["totals"]["inserted"] == 0
    assert second["totals"]["updated"] == 0
    assert second["totals"]["unchanged"] == first["totals"]["inserted"]
    for row in store.list_items(source_file="telegram_groups.json"):
        assert row["revision"] == 1, "an unchanged record must not gain a revision"


def test_a_dry_run_reports_the_same_plan_it_would_apply(store: ConfigStore, data_copy: Path) -> None:
    """A dry run is only useful if the real run then does exactly what it said."""
    planned = config_sync.sync(store, data_dir=data_copy, files=["reports_config.json"],
                               dry_run=True)
    assert store.list_items(source_file="reports_config.json") == []

    applied = config_sync.sync(store, data_dir=data_copy, files=["reports_config.json"])
    assert applied["totals"] == planned["totals"]


def test_an_edited_record_is_updated_and_keeps_its_previous_text(store: ConfigStore,
                                                                 data_copy: Path) -> None:
    config_sync.sync(store, data_dir=data_copy, files=["reports_config.json"], actor="first")
    path = data_copy / "reports_config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["reports"][0]["display_name"] = "Renamed by the test"
    _write(path, payload)

    result = config_sync.sync(store, data_dir=data_copy, files=["reports_config.json"],
                              actor="second")
    assert result["totals"]["updated"] == 1

    row = store.get_item(source_file="reports_config.json", collection="reports",
                         item_key=payload["reports"][0]["report_code"])
    assert row["revision"] == 2
    assert row["updated_by"] == "second"
    trail = store.revisions(row["config_item_id"])
    assert [(item["revision"], item["change_type"]) for item in trail] == [(1, "insert"), (2, "update")]
    assert "Renamed by the test" not in trail[0]["item_json"], (
        "the first revision must still hold the text as it was before the edit")


# --------------------------------------------------------------------------- #
# Never forgetting
# --------------------------------------------------------------------------- #
def test_a_record_removed_from_its_file_is_deactivated_not_deleted(store: ConfigStore,
                                                                   data_copy: Path) -> None:
    config_sync.sync(store, data_dir=data_copy, files=["telegram_users.json"], actor="first")
    path = data_copy / "telegram_users.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    removed = payload["telegram_users"].pop(0)
    _write(path, payload)

    result = config_sync.sync(store, data_dir=data_copy, files=["telegram_users.json"],
                              actor="second")
    assert result["totals"]["deactivated"] == 1

    assert store.get_item(source_file="telegram_users.json", collection="telegram_users",
                          item_key=removed["user_id"]) is None
    retired = [row for row in store.list_items(source_file="telegram_users.json",
                                               include_inactive=True)
               if row["item_key"] == removed["user_id"]]
    assert len(retired) == 1
    assert retired[0]["is_active"] == 0
    assert retired[0]["deactivated_at"]
    assert json.loads(retired[0]["item_json"]) == removed, (
        "a retired record must keep the text it had when it was retired")


def test_a_retired_key_can_be_used_again_by_a_new_record(store: ConfigStore,
                                                         data_copy: Path) -> None:
    """The rule the partial unique index exists for.

    Re-adding a key that was switched off must insert a **new** row: the old record stays exactly
    as it was retired, and the new one starts its own history. Resurrecting the old row instead
    would rewrite history to say the record never left.
    """
    path = data_copy / "telegram_users.json"
    config_sync.sync(store, data_dir=data_copy, files=["telegram_users.json"], actor="first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    removed = payload["telegram_users"].pop(0)
    _write(path, payload)
    config_sync.sync(store, data_dir=data_copy, files=["telegram_users.json"], actor="second")

    payload["telegram_users"].append({**removed, "user_type": 50, "note": "Re-added."})
    _write(path, payload)
    result = config_sync.sync(store, data_dir=data_copy, files=["telegram_users.json"],
                              actor="third")
    assert result["totals"]["inserted"] == 1

    rows = [row for row in store.list_items(source_file="telegram_users.json",
                                            include_inactive=True)
            if row["item_key"] == removed["user_id"]]
    assert len(rows) == 2, "both the retired record and its replacement must exist"
    assert sorted(row["is_active"] for row in rows) == [0, 1]
    active = [row for row in rows if row["is_active"] == 1][0]
    assert active["revision"] == 1, "the new record starts its own history"
    assert json.loads(active["item_json"])["user_type"] == 50


def test_two_active_rows_cannot_share_a_key(store: ConfigStore, data_copy: Path) -> None:
    """The uniqueness is the database's, not the sync's — a UI writing directly must hit it too."""
    import sqlite3

    from db_ops.db.config_store import ConfigItem

    config_sync.sync(store, data_dir=data_copy, files=["telegram_users.json"])
    existing = store.list_items(source_file="telegram_users.json", collection="telegram_users")[0]
    with pytest.raises(sqlite3.IntegrityError):
        with store.connect() as conn:
            conn.execute(
                """
                INSERT INTO config_items
                    (config_collection_id, item_key, item_json, content_hash)
                VALUES (?, ?, '{}', 'x')
                """,
                (existing["config_collection_id"], existing["item_key"]),
            )
    # ...and the same key is insertable again once the first row is retired.
    store.deactivate_item(source_file="telegram_users.json", collection="telegram_users",
                          item_key=existing["item_key"], actor="test")
    item_id, action = store.upsert_item(ConfigItem(
        source_file="telegram_users.json", collection="telegram_users",
        item_key=existing["item_key"], payload={"user_id": existing["item_key"]}))
    assert action == "inserted" and item_id != existing["config_item_id"]


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
def test_a_file_with_two_records_on_one_key_is_refused(store: ConfigStore, data_copy: Path) -> None:
    path = data_copy / "telegram_groups.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["telegram_groups"].append(dict(payload["telegram_groups"][0]))
    _write(path, payload)

    result = config_sync.sync(store, data_dir=data_copy, files=["telegram_groups.json"])
    outcome = result["files"][0]
    assert outcome["status"] == "failed"
    assert "repeats the key" in outcome["error"]
    assert store.list_items(source_file="telegram_groups.json") == [], (
        "a file that cannot be keyed must write nothing at all, not a partial set")


def test_a_plaintext_secret_is_refused_and_named(store: ConfigStore, data_copy: Path) -> None:
    """These rows get queried, rendered and backed up. A ref may travel; a password may not."""
    path = data_copy / "docker_db_connections.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["docker_db_connections"][0]["password"] = "hunter2"
    _write(path, payload)

    result = config_sync.sync(store, data_dir=data_copy, files=["docker_db_connections.json"])
    outcome = result["files"][0]
    assert outcome["status"] == "failed"
    assert "password" in outcome["error"] and "encrypted_secret_text.json" in outcome["error"]
    assert "hunter2" not in outcome["error"], "the refusal must not quote the secret back"


def test_a_password_ref_is_not_mistaken_for_a_password(data_copy: Path) -> None:
    """``password_ref`` and ``password_env`` are names. Refusing them would refuse every file."""
    config_sync.assert_no_plaintext_secret(
        {"password_ref": "MSSQL_X_DBA", "password_env": "PG_PW"}, where="test")


def test_a_missing_file_is_reported_and_changes_nothing(store: ConfigStore,
                                                        data_copy: Path) -> None:
    """Master-only config absent on a worker must not read as 'the operator deleted it all'."""
    config_sync.sync(store, data_dir=data_copy, files=["sla_policies.json"])
    before = len(store.list_items(source_file="sla_policies.json"))
    (data_copy / "sla_policies.json").unlink()

    result = config_sync.sync(store, data_dir=data_copy, files=["sla_policies.json"])
    assert result["files"][0]["status"] == "missing"
    assert result["totals"]["deactivated"] == 0
    assert len(store.list_items(source_file="sla_policies.json")) == before


def test_every_catalogued_file_is_read_by_something() -> None:
    """Config the console can edit and nothing acts on is worse than config it cannot edit.

    That is not hypothetical. ``notify_levels.json`` and ``metric_groups.json`` sat in ``data/``
    looking exactly like live config — imported from the old SQL Server job tables — and no code
    had ever read either. Mirroring them gave the console an editor for settings that changed
    nothing, which is a trap: the save succeeds, the page says so, and the estate ignores it. Both
    were deleted on 2026-08-21.

    The search is by filename across ``db_ops/``. A file whose path is built at runtime would trip
    this without being dead, which is what ``READ_INDIRECTLY`` is for — but adding to it should mean
    "I checked and it is read", never "make the test stop".
    """
    unread = []
    for spec in config_sync.load_catalog(DATA_DIR):
        if spec.file in READ_INDIRECTLY:
            continue
        stem = spec.file.replace(".json", "")
        readers = [path for path in (REPO_ROOT / "db_ops").rglob("*.py")
                   if "__pycache__" not in path.parts
                   and stem in path.read_text(encoding="utf-8")]
        if not readers:
            unread.append(spec.file)
    assert not unread, (
        "catalogued but read by no code in db_ops/: " + ", ".join(unread) +
        ". Either it is dead and should go, or it is loaded by a path built at runtime and "
        "belongs in READ_INDIRECTLY with that noted.")


def test_a_misspelled_file_name_is_reported_rather_than_syncing_nothing(store: ConfigStore,
                                                                        data_copy: Path) -> None:
    result = config_sync.sync(store, data_dir=data_copy, files=["metric_definition.json"])
    assert result["unknown_files"] == ["metric_definition.json"]
    assert result["files"] == []


def test_one_unreadable_file_does_not_stop_the_others(store: ConfigStore, data_copy: Path) -> None:
    """One bad config file must cost one file, never the whole estate — as with metric targets."""
    (data_copy / "telegram_groups.json").write_text("{ not json", encoding="utf-8")

    result = config_sync.sync(store, data_dir=data_copy)
    statuses = {item["file"]: item["status"] for item in result["files"]}
    assert statuses["telegram_groups.json"] == "failed"
    assert statuses["metric_definitions.json"] == "ok"
    assert store.list_items(source_file="metric_definitions.json")
