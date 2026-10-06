"""An inactive restore entry that cannot be read does not stop the restores that can.

Since 0.26.0 a script-driven restore entry states everything about its target, or it is refused
(owner decision G2: nothing about the target is derived from the source). The refusal was for the
whole file. Found on this estate's own configuration, 2026-10-02: two retired drills - inactive for
months - had no ``target_server_id``, two more staged into a two-level folder, and with any one of
them in the file ``list-restores`` printed an error, the scheduled restore pass failed on every
cycle, and no new entry could be registered through the bot. It is the shape the same review removed
from backups, SQL tasks, metrics and reports: one item stops the pass.

The rule is kept where it protects something:

* an **active** entry that cannot be read is refused, and the file with it - it is about to run;
* an **inactive** one runs nothing, so it is kept out of the list with its reason
  (``ScriptRestores.unusable``), ``list-restores`` shows it, and asking for it by id answers with
  that reason, not "no such entry";
* the entry being **registered** is held to the rule whether it is active or not.

The SMB entries (the SQL Server engine path, ``config.load_restore_configs``) kept refusing their
whole file for an inactive entry until 0.27.0 - no entry on this estate tripped it, so it was read
and left on 2026-10-02 (review 0.25.0, U5). The same rule holds there now (``RestoreConfigs``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.backup_restore import cli, registration, restore_by_id, restore_script, workflow
from db_ops.backup_restore import config as config_module


def _entry(restore_id: str, **fields) -> dict:
    entry = {
        "restore_id": restore_id, "db_type": "postgresql", "server_id": "SRC",
        "target_server_id": "DST", "backup_dir": "/backup", "script": "restore.sh",
        "target_backup_dir": f"/opt/db_ops/restore_staging/{restore_id}",
        "source_backup_host_dir": "/opt/backup/pg", "cleanup_retention": 691200,
    }
    entry.update(fields)
    return {key: value for key, value in entry.items() if value is not None}


def _file(tmp_path: Path, *entries: dict) -> str:
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"restores": list(entries)}}), encoding="utf-8")
    return str(path)


RETIRED = _entry("RETIRED_DRILL", active=False, target_server_id=None)


def test_a_retired_entry_with_no_target_is_kept_out_and_the_live_one_loads(tmp_path):
    jobs = restore_script.load_script_restores(_file(tmp_path, RETIRED, _entry("LIVE_DRILL")))

    assert [job.restore_id for job in jobs] == ["LIVE_DRILL"]
    assert "RETIRED_DRILL requires target_server_id" in jobs.unusable["RETIRED_DRILL"]


def test_the_same_entry_switched_on_is_refused_with_the_whole_file(tmp_path):
    """The owner's rule, untouched: an entry about to run states its target or nothing loads."""
    live_and_incomplete = {**RETIRED, "active": True}

    with pytest.raises(ValueError, match="RETIRED_DRILL requires target_server_id"):
        restore_script.load_script_restores(_file(tmp_path, live_and_incomplete, _entry("LIVE_DRILL")))


def test_an_entry_that_says_nothing_about_active_is_active(tmp_path):
    silent = {key: value for key, value in RETIRED.items() if key != "active"}

    with pytest.raises(ValueError, match="requires target_server_id"):
        restore_script.load_script_restores(_file(tmp_path, silent))


def test_a_retired_entry_with_a_shallow_staging_folder_is_kept_out(tmp_path):
    shallow = _entry("OLD_PG_TO_CLOUD", active=False, target_backup_dir="/opt/pg_restore_from_a1a")

    jobs = restore_script.load_script_restores(_file(tmp_path, shallow, _entry("LIVE_DRILL")))

    assert [job.restore_id for job in jobs] == ["LIVE_DRILL"]
    assert "too shallow" in jobs.unusable["OLD_PG_TO_CLOUD"]


def test_a_live_entry_with_a_shallow_staging_folder_is_still_refused(tmp_path):
    with pytest.raises(ValueError, match="too shallow"):
        restore_script.load_script_restores(
            _file(tmp_path, _entry("LIVE_DRILL", target_backup_dir="/opt/stage")))


@pytest.mark.parametrize("retired_first", [True, False])
def test_where_a_live_entry_and_a_retired_one_share_a_folder_the_retired_one_yields(tmp_path, retired_first):
    live = _entry("LIVE_DRILL", target_backup_dir="/opt/db_ops/stage/pg")
    retired = _entry("OLD_DRILL", active=False, target_backup_dir="/opt/db_ops/stage/pg/old")
    entries = (retired, live) if retired_first else (live, retired)

    jobs = restore_script.load_script_restores(_file(tmp_path, *entries))

    assert [job.restore_id for job in jobs] == ["LIVE_DRILL"]
    assert "one directory holds the other" in jobs.unusable["OLD_DRILL"]


def test_two_live_entries_sharing_a_folder_are_still_refused(tmp_path):
    with pytest.raises(ValueError, match="one directory holds the other"):
        restore_script.load_script_restores(_file(
            tmp_path,
            _entry("A", target_backup_dir="/opt/db_ops/stage/pg"),
            _entry("B", target_backup_dir="/opt/db_ops/stage/pg")))


def test_a_file_that_is_wrong_as_a_whole_is_refused_whatever_is_inactive(tmp_path):
    """Two entries under one id is not one entry's problem: neither can be told from the other."""
    with pytest.raises(ValueError, match="Duplicate restore_id"):
        restore_script.load_script_restores(_file(tmp_path, RETIRED, RETIRED))


def test_a_retired_entry_that_is_complete_is_listed_as_inactive_not_as_unusable(tmp_path):
    jobs = restore_script.load_script_restores(_file(tmp_path, _entry("PAUSED", active=False)))

    assert [(job.restore_id, job.active) for job in jobs] == [("PAUSED", False)]
    assert jobs.unusable == {}


# --------------------------------------------------------------------------- #
# Asked for by name, it says what it lacks
# --------------------------------------------------------------------------- #
def test_restore_by_id_answers_with_the_reason_not_with_no_such_entry(tmp_path):
    config = _file(tmp_path, RETIRED, _entry("LIVE_DRILL"))

    with pytest.raises(restore_by_id.RestoreByIdError, match="RETIRED_DRILL requires target_server_id"):
        restore_by_id.restore_by_id({"restore_id": "RETIRED_DRILL", "config": config, "dry_run": True})


def test_the_reason_of_an_entry_that_loaded_is_empty(tmp_path):
    jobs = restore_script.load_script_restores(_file(tmp_path, RETIRED, _entry("LIVE_DRILL")))

    assert restore_script.unusable_reason(jobs, "LIVE_DRILL") == ""
    assert restore_script.unusable_reason([], "ANYTHING") == "", "any loader's answer may be asked"


def test_the_listing_names_what_is_incomplete_under_what_can_run(tmp_path):
    jobs = restore_script.load_script_restores(_file(tmp_path, RETIRED, _entry("LIVE_DRILL")))

    listing = cli._format_restore_list([], jobs)

    assert listing.splitlines()[0] == "Restore IDs (1):"
    assert "Inactive and incomplete (1) - not usable until fixed:" in listing
    assert "RETIRED_DRILL requires target_server_id" in listing


def test_a_listing_with_nothing_active_still_says_what_is_incomplete(tmp_path):
    jobs = restore_script.load_script_restores(_file(tmp_path, RETIRED))

    listing = cli._format_restore_list([], jobs)

    assert listing.startswith("No active restore entries")
    assert "RETIRED_DRILL requires target_server_id" in listing


# --------------------------------------------------------------------------- #
# Registering: the new entry is held to the rule, an old one no longer blocks it
# --------------------------------------------------------------------------- #
def test_an_old_unusable_entry_does_not_block_a_new_one(tmp_path):
    path = Path(_file(tmp_path, RETIRED, _entry("NEW_DRILL")))

    registration._load_every_restore(path, registering="NEW_DRILL")


def test_an_entry_being_registered_inactive_and_incomplete_is_refused(tmp_path):
    """Written, it could never run - and nothing would say so until somebody switched it on."""
    path = Path(_file(tmp_path, RETIRED))

    with pytest.raises(ValueError, match="RETIRED_DRILL requires target_server_id"):
        registration._load_every_restore(path, registering="RETIRED_DRILL")


# --------------------------------------------------------------------------- #
# The SMB entries - the same rule (0.27.0)
# --------------------------------------------------------------------------- #
@pytest.fixture
def no_estate_defaults(tmp_path, monkeypatch):
    """The SMB loader takes every entry's defaults from the node's own restore file; without this
    it would read this repository's estate entries into the test's."""
    absent = tmp_path / "no_default_restore_config.json"
    monkeypatch.setattr(config_module, "DEFAULT_RESTORE_CONFIG_PATH", absent)
    monkeypatch.setattr(restore_script, "DEFAULT_RESTORE_CONFIG_PATH", absent, raising=False)


def _smb(restore_id: str, **fields) -> dict:
    entry = {
        "restore_id": restore_id, "server_id": "SRC", "target_server_id": "DST",
        "cleanup_retention": 86400,
        "source": {"backup_share": "//192.0.2.10/SQLBK"},
        "target": {"vm_platform": "linux", "vm_import_linux_path": f"/opt/restore/import/{restore_id}",
                   "restore_data_dir": "/var/opt/mssql/data"},
        "database_mappings": [{"source_database": "APPDB", "target_database": "APPDB"}],
    }
    entry.update(fields)
    return entry


#: Retired, and its share long gone from the file.
RETIRED_SMB = _smb("RETIRED_SMB", active=False, source={})


def test_a_retired_smb_entry_with_no_share_is_kept_out_and_the_live_one_loads(tmp_path, no_estate_defaults):
    configs = config_module.load_restore_configs(_file(tmp_path, RETIRED_SMB, _smb("LIVE_SMB")))

    assert [config.restore_id for config in configs] == ["LIVE_SMB"]
    assert "RETIRED_SMB" in configs.unusable["RETIRED_SMB"]
    assert "prod_backup_share" in configs.unusable["RETIRED_SMB"]


def test_the_same_smb_entry_switched_on_is_refused_with_the_whole_file(tmp_path, no_estate_defaults):
    with pytest.raises(ValueError, match="missing required field"):
        config_module.load_restore_configs(
            _file(tmp_path, {**RETIRED_SMB, "active": True}, _smb("LIVE_SMB")))


def test_an_smb_entry_that_says_nothing_about_active_is_active(tmp_path, no_estate_defaults):
    silent = {key: value for key, value in RETIRED_SMB.items() if key != "active"}

    with pytest.raises(ValueError, match="missing required field"):
        config_module.load_restore_configs(_file(tmp_path, silent))


def test_the_listing_names_an_incomplete_smb_entry_too(tmp_path, no_estate_defaults):
    path = _file(tmp_path, RETIRED_SMB, _smb("LIVE_SMB"))

    listing = cli._format_restore_list(config_module.load_restore_configs(path),
                                       restore_script.load_script_restores(path))

    assert listing.splitlines()[0] == "Restore IDs (1):"
    assert "Inactive and incomplete (1) - not usable until fixed:" in listing
    assert "RETIRED_SMB" in listing


def test_restore_by_id_answers_an_incomplete_smb_entry_with_its_reason(tmp_path, no_estate_defaults):
    config = _file(tmp_path, RETIRED_SMB, _smb("LIVE_SMB"))

    with pytest.raises(restore_by_id.RestoreByIdError, match="prod_backup_share"):
        restore_by_id.restore_by_id({"restore_id": "RETIRED_SMB", "config": config, "dry_run": True})


def test_the_workflow_answers_an_incomplete_smb_entry_with_its_reason(tmp_path, no_estate_defaults):
    """Before any store is opened: the reason is the whole answer."""
    config = _file(tmp_path, RETIRED_SMB, _smb("LIVE_SMB"))

    with pytest.raises(ValueError, match="prod_backup_share"):
        workflow.run_scheduled_restores(app_config=None, config_path=config, restore_id="RETIRED_SMB")


def test_an_old_unusable_smb_entry_does_not_block_a_new_one(tmp_path, no_estate_defaults):
    registration._load_every_restore(Path(_file(tmp_path, RETIRED_SMB, _smb("NEW_SMB"))),
                                     registering="NEW_SMB")


def test_an_smb_entry_being_registered_inactive_and_incomplete_is_refused(tmp_path, no_estate_defaults):
    with pytest.raises(ValueError, match="prod_backup_share"):
        registration._load_every_restore(Path(_file(tmp_path, RETIRED_SMB)), registering="RETIRED_SMB")
