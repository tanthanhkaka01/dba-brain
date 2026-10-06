"""The catalogue names record fields - so a field's rename has to reach the catalogue too.

`config_catalog.json` says how each data file's records are keyed and labelled in the runtime store:
`label_field`, `key_fields`. 0.22.0 renamed `sql_name` to `display_name` in the records and left a
node's catalogue saying `sql_name`. On the worker every SQL task's label then read empty, while the
master's catalogue gave the display name - so a deploy's sync from the master and an upgrade's sync
on the worker each rewrote all 30 records the other had written, content unchanged, and every upgrade
reported "30 updated" for a drift that was never there (0.27.0 item 1.95). Reading it found two more
in the shipped catalogue itself: SLA policies labelled by `name`, Docker connections by `engine`.

`upgrade-config`'s field-names step now follows its own renames into the catalogue, and a shipped
catalogue may not name a field by a spelling that step would move.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.common import config_upgrade, field_migration
from db_ops.lib import field_names

ROOT = Path(__file__).resolve().parent.parent

STALE = """{
    "schema_version": 1,
    "config_sources": [
        {
            "file": "sql_commands.json",
            "app_code": "sql_tasks",
            "collections": [
                {"collection": "sql_commands", "key_fields": ["sql_id"], "label_field": "sql_name"}
            ]
        }
    ]
}
"""


@pytest.mark.parametrize("catalogue", ["db_ops/db/catalogue/config_catalog.json",
                                       "data/config_catalog.example.json"])
def test_a_shipped_catalogue_names_no_field_by_a_renamed_spelling(catalogue):
    sites = field_migration._sites()
    document = json.loads((ROOT / catalogue).read_text(encoding="utf-8"))
    stale = []
    for source in document["config_sources"]:
        for collection in source.get("collections") or []:
            kinds = field_migration._objects_of(sites, source["file"], collection["collection"])
            for name in [collection.get("label_field") or "", *(collection.get("key_fields") or [])]:
                for kind in kinds:
                    if name and field_names.standard_of(kind, name) != name:
                        stale.append((source["file"], name, field_names.standard_of(kind, name)))
    assert not stale, f"named by a spelling field-names moves (file, old, new): {stale}"


def test_the_label_follows_the_records_rename_and_the_layout_stays(tmp_path):
    path = tmp_path / "config_catalog.json"
    path.write_text(STALE, encoding="utf-8")

    message, plan = field_migration.standardize({"data_dir": str(tmp_path)})
    assert path.read_text(encoding="utf-8") == STALE, "a plan writes nothing"
    assert plan["files"][0]["changes"][0]["to"] == "display_name"

    field_migration.standardize({"data_dir": str(tmp_path), "dry_run": False})

    after = path.read_text(encoding="utf-8")
    assert after == STALE.replace('"label_field": "sql_name"', '"label_field": "display_name"'), (
        "the hand-formatted one-line collection keeps its layout; only the name changes")
    _, again = field_migration.standardize({"data_dir": str(tmp_path)})
    assert again["records_changed"] == 0, "idempotent"


def test_upgrade_config_carries_the_catalogue_forward_and_keeps_the_old_one(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config_catalog.json").write_text(STALE, encoding="utf-8")

    _, outcome = config_upgrade.upgrade({"data_dir": str(data), "dry_run": False,
                                         "steps": ["field-names"]})

    step = next(item for item in outcome["steps"] if item["step"] == "field-names")
    assert [item["file"] for item in step["files"]] == ["config_catalog.json"]
    assert json.loads((data / "config_catalog.json").read_text(encoding="utf-8"))[
        "config_sources"][0]["collections"][0]["label_field"] == "display_name"
    backups = list((tmp_path / "runtime" / "config_upgrade").glob("*/config_catalog.json"))
    assert backups and "sql_name" in backups[0].read_text(encoding="utf-8")
