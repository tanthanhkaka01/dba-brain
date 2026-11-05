"""A restore into a production instance is refused unless its entry allows it (0.35.0, 1.223 - A3.3).

A restore REPLACEs what it lands on. An entry mis-edited to name a production instance as its target
- one id typed for another - would overwrite it at the next scheduled drill, and the existing guard
only catches a target that is the entry's own *source*. R47's spirit: the target is named *and*
allowed. The inventory's ``environment`` says what an instance is; a target it calls ``prod`` /
``production`` needs ``allow_production_target: true`` on the entry. Both restore paths check it
before anything is copied: ``restore-by-id`` and the share-driven workflow.
"""

from __future__ import annotations

import pytest

from dbabrain.backup_restore import config as restore_config
from dbabrain.lib import errors
from dbabrain.lib import integrity as I

INVENTORY = [{"server_id": "ACME-PROD-1", "environment": "prod"},
             {"server_id": "ACME-LAB-1", "environment": "lab"},
             {"server_id": "ACME-UNMARKED-1"}]


@pytest.fixture(autouse=True)
def inventory(monkeypatch):
    from dbabrain.lib import data_sources

    monkeypatch.setattr(data_sources, "load_db_instances", lambda data_dir=None: INVENTORY)


@pytest.mark.parametrize("environment, allowed, refused", [
    ("prod", False, True), ("Production", False, True), ("prod", True, False),
    ("lab", False, False), ("dev", False, False), ("", False, False), (None, False, False),
])
def test_only_a_production_target_without_its_word_is_refused(environment, allowed, refused):
    why = I.production_target_refusal(environment, allowed=allowed, restore_id="R", target="T")
    assert bool(why) is refused


def test_the_refusal_names_the_entry_the_target_and_the_word_that_allows_it():
    with pytest.raises(restore_config.ProductionTargetRefused) as raised:
        restore_config.assert_target_is_allowed(restore_id="SALES_DRILL", target_server_id="ACME-PROD-1",
                                                allowed=False)
    text = str(raised.value)
    assert "SALES_DRILL" in text and "ACME-PROD-1" in text and "allow_production_target" in text
    # A refusal, and still a ValueError: the unsafe-config handlers catch it as before.
    assert isinstance(raised.value, errors.Refused) and isinstance(raised.value, ValueError)


@pytest.mark.parametrize("target", ["ACME-LAB-1", "ACME-UNMARKED-1", "NOT-IN-THE-INVENTORY", ""])
def test_a_target_not_marked_production_passes(target):
    restore_config.assert_target_is_allowed(restore_id="R", target_server_id=target, allowed=False)


def test_the_entry_word_allows_it():
    restore_config.assert_target_is_allowed(restore_id="R", target_server_id="ACME-PROD-1", allowed=True)


def test_both_entry_shapes_read_the_word():
    from dbabrain.backup_restore.restore_script import ScriptRestore

    assert ScriptRestore.__dataclass_fields__["allow_production_target"].default is False
    assert restore_config.BackupRestoreConfig.__dataclass_fields__["allow_production_target"].default is False
