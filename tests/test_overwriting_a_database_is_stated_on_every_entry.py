"""Whether a restore may replace a database is stated on every entry, and off unless set (0.39.0, 1.362).

The operator, 2026-11-13 (translated): *overwriting a DB must be a required field in the config, set
explicitly, off by default (no overwriting).* Until then an entry that said nothing was read as false,
which is a default the reader cannot see - and PostgreSQL and Oracle entries never read the field. Now
an entry stating no `overwrite_existing` (or something that is not true or false) is refused inside
its own run, as an unstated `action` is (R50), and `upgrade-config` writes `false` into every entry
that states none - never `true`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import workflow
from dbabrain.lib import integrity as I


@pytest.mark.parametrize("value, read", [(True, True), (False, False), (None, None), ("yes", None), (1, None),
                                         ("false", None)])
def test_only_true_or_false_is_a_statement(value, read):
    assert I.stated_overwrite(value) is read


def test_an_entry_stating_nothing_is_refused_by_name():
    with pytest.raises(I.IntegrityConfigError, match="restore_id=X states no overwrite_existing"):
        I.require_overwrite_stated(None, label="restore_id=X")
    assert I.require_overwrite_stated(False) is False and I.require_overwrite_stated(True) is True


def _entry(**fields):
    return SimpleNamespace(restore_id="X", restore_from="server", action="restore", db_type="postgresql",
                           is_remote=True, **fields)


def test_the_workflow_refuses_it_inside_that_entrys_run():
    action, refused = workflow._entry_action(_entry(overwrite_existing=None), copy_only=False)
    assert action is None and "states no overwrite_existing" in str(refused)
    assert workflow._entry_action(_entry(overwrite_existing=False), copy_only=False) == ("restore", None)


def test_upgrade_config_writes_false_never_true_and_keeps_what_is_stated():
    document = {"backup_restore": {"restores": [
        {"restore_id": "SILENT", "action": "restore", "integrity": {}},
        {"restore_id": "ON", "action": "restore", "integrity": {}, "overwrite_existing": True},
        {"restore_id": "OFF", "action": "restore", "integrity": {}, "overwrite_existing": False},
        {"restore_id": "ODD", "action": "restore", "integrity": {}, "overwrite_existing": "yes"},
    ]}}
    changes = I.state_restore_integrity(document)
    entries = {e["restore_id"]: e for e in document["backup_restore"]["restores"]}
    assert entries["SILENT"]["overwrite_existing"] is False
    assert entries["ON"]["overwrite_existing"] is True and entries["OFF"]["overwrite_existing"] is False
    assert entries["ODD"]["overwrite_existing"] == "yes", "a value it cannot read is the operator's to correct"
    assert [c["where"] for c in changes if c["field"] == "overwrite_existing"] == ["backup_restore.restores[SILENT]"]


def test_the_shipped_reference_calls_it_required():
    import json
    from pathlib import Path

    reference = json.loads((Path(__file__).resolve().parents[1] / "dbabrain/common/catalogue/shared_config_objects.json")
                           .read_text(encoding="utf-8-sig"))["shared_config_objects"]
    entry = next(o for o in reference if o["object"] == "restore_entry")
    field = next(f for f in entry["fields"] if f["field"] == "overwrite_existing")
    assert field["required"] is True
