"""A restore entry states the fields that decide what its run does (0.39.0, 1.366).

The operator, 2026-11-13 (translated): *use true, false, and these fields must be required; in backup
restore most fields must be required, because it is very important - a mistake can ruin the whole
production.*

``action``, ``restore_from``, ``integrity``, ``overwrite_existing`` and a SQL Server entry's
databases were stated already. Four more decided a run from a default nobody could read in the
entry: whether production may be its target (``allow_production_target``), whether the consistency
check runs (``checkdb``), what its copy takes (``copy_selection``) and whether the copy must fit the
target first (``space_check``). Now an entry that leaves one out still loads - so it cannot stop the
other restores of its file - and is refused inside its own run, by name; ``upgrade-config`` writes
the value each entry had been running on, and ``restore-add`` writes them into a new entry.

These four were the first. On 2026-11-14 the operator made nearly every other field of a backup or
restore record required too - the window, the switch, the notify rules, the check's limits, the
drill's tier: ``tests/test_every_backup_and_restore_field_is_stated.py`` holds those.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import restore_by_id, workflow
from dbabrain.backup_restore.restore_script import load_script_restores
from dbabrain.lib.restore import stated as S

from conftest import stated

FOUR = ("allow_production_target", "checkdb", "copy_selection", "space_check")
STATED = {"allow_production_target": False, "checkdb": True, "copy_selection": "chain",
          "space_check": {"enabled": True, "factor": 2.0, "on_unknown": "refuse"}}
#: The four as the tables name them: space_check by the fields of its block.
NAMES = ("allow_production_target", "checkdb", "copy_selection", "space_check.enabled", "space_check.factor",
         "space_check.on_unknown")


def _four(names) -> tuple[str, ...]:
    return tuple(name for name in names if name in NAMES)


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
def test_the_four_fields_and_what_an_entry_that_said_nothing_ran_on():
    for kind in (S.SCRIPT_RESTORE, S.SHARE_RESTORE):
        assert tuple(field.name for field in S.FIELDS[kind])[:len(NAMES)] == NAMES, kind
    entry = {"script": "s"}
    S.state_record(entry)
    assert {name: entry[name] for name in FOUR} == STATED
    assert S.ran_on("copy_selection", {"script": "s"}) == "chain"


def test_an_entry_is_read_for_what_it_leaves_out():
    def four(entry):
        return _four(S.unstated({"script": "s", **entry}))

    assert four({}) == NAMES
    # Switched off, the check has no factor to state.
    assert four({"checkdb": False, "space_check": {"enabled": False}}) == ("allow_production_target", "copy_selection")
    assert four(STATED) == ()
    # Stated is stated, whatever it says: false and "off" are answers.
    assert four({"allow_production_target": False, "checkdb": False, "copy_selection": "window",
                 "space_check": {"enabled": False}}) == ()
    assert S.unstated(None) == ()


def test_the_refusal_names_each_field_and_what_it_decides():
    with pytest.raises(S.StatedError) as refused:
        S.require_stated(("allow_production_target", "checkdb"), label="restore_id=X")

    said = str(refused.value)
    assert said.startswith("restore_id=X states no allow_production_target, checkdb")
    assert "whether it may restore onto an instance the inventory calls production" in said
    assert "whether the consistency check runs" in said and "upgrade-config" in said
    S.require_stated((), label="x")
    S.require_stated(None, label="x")


# --------------------------------------------------------------------------- #
# upgrade-config: what each entry had been running on
# --------------------------------------------------------------------------- #
def test_upgrade_config_writes_what_the_entry_ran_on_and_keeps_what_it_states():
    document = {"backup_restore": {"restores": [
        {"restore_id": "SILENT", "script": "s"},
        {"restore_id": "PARTLY", "script": "s", "checkdb": False, "space_check": {"enabled": False}},
        {"restore_id": "ALL", "script": "s", **STATED, "copy_selection": "window"},
    ]}}

    changes = S.state_restore_fields(document)
    entries = {entry["restore_id"]: entry for entry in document["backup_restore"]["restores"]}

    assert {name: entries["SILENT"][name] for name in FOUR} == STATED
    assert entries["PARTLY"]["checkdb"] is False and entries["PARTLY"]["space_check"] == {"enabled": False}
    assert entries["PARTLY"]["allow_production_target"] is False and entries["PARTLY"]["copy_selection"] == "chain"
    assert entries["ALL"]["copy_selection"] == "window"
    assert [(c["where"], c["field"]) for c in changes if c["field"] in NAMES] == [
        *[("backup_restore.restores[SILENT]", name) for name in NAMES],
        ("backup_restore.restores[PARTLY]", "allow_production_target"),
        ("backup_restore.restores[PARTLY]", "copy_selection")]
    assert "false, stated - what the script-driven restore entry had been running on" in changes[0]["value"]
    assert S.state_restore_fields(document) == [], "run again, it changes nothing"


def test_a_share_driven_entry_may_leave_a_field_to_the_section_above_it():
    document = {"backup_restore": {"copy_selection": "window", "restores": [
        {"restore_id": "SHARE", "source": {}, "target": {}},
        {"restore_id": "SCRIPT", "script": "s"}]}}

    S.state_restore_fields(document)
    share, script = document["backup_restore"]["restores"]

    assert "copy_selection" not in share, "stated on the section is stated"
    assert script["copy_selection"] == "chain", "a script-driven entry reads no section: it states its own"
    assert share["checkdb"] is True and share["allow_production_target"] is False


# --------------------------------------------------------------------------- #
# The loaders and the run
# --------------------------------------------------------------------------- #
def _entry(**over) -> dict:
    entry = {"restore_id": "LAB_TO_LAB", "db_type": "postgresql", "server_id": "SRC",
             "target_server_id": "TGT", "backup_dir": "/out", "script": "postgresql",
             "target_backup_dir": "/opt/db_ops/staging/lab", "source_backup_host_dir": "/opt/out",
             "cleanup_retention": 86400, "time_window": {"from_hour": 0, "to_hour": 23},
             "overwrite_existing": False, "action": "restore", "restore_from": "server"}
    entry.update(over)
    # Every other field it must state, stated: this file is about the four.
    stated(entry)
    for name in FOUR:
        if name not in over:
            entry.pop(name)
    return entry


def _load(tmp_path, entry: dict):
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"restores": [entry]}}), encoding="utf-8")
    return load_script_restores(path)[0]


def test_an_entry_that_leaves_them_out_loads_and_says_which(tmp_path):
    assert _load(tmp_path, _entry()).unstated == NAMES
    assert _load(tmp_path, _entry(**STATED)).unstated == ()
    assert _load(tmp_path, _entry(checkdb=False)).unstated == (
        "allow_production_target", "copy_selection", "space_check.enabled", "space_check.factor", "space_check.on_unknown")


def test_the_workflow_refuses_it_inside_that_entrys_run(tmp_path):
    action, refused = workflow._entry_action(_load(tmp_path, _entry()), copy_only=False)

    assert action is None and "states no allow_production_target, checkdb, copy_selection, space_check" in str(refused)
    assert workflow._entry_action(_load(tmp_path, _entry(**STATED)), copy_only=False) == ("restore", None)


def test_restore_by_id_refuses_it_before_anything_is_copied(monkeypatch, tmp_path):
    from dbabrain.backup_restore import restore_script

    job = _load(tmp_path, _entry())
    monkeypatch.setattr(restore_script, "load_script_restores", lambda _path=None: [job])
    monkeypatch.setattr(restore_script, "transfer_backup_to_target",
                        lambda *a, **k: pytest.fail("copied for an entry that leaves its run to defaults"))

    with pytest.raises(restore_by_id.RestoreByIdError, match="states no allow_production_target"):
        restore_by_id.restore_by_id({"restore_id": "LAB_TO_LAB"})


def test_a_share_driven_entry_is_held_to_the_same_four():
    from dbabrain.backup_restore import config

    assert "copy_selection" not in config.RESTORE_PARSER_DEFAULTS, "a default in the parser's table made every entry read as stated"
    entry = SimpleNamespace(restore_id="SHARE", restore_from="server", action="restore", overwrite_existing=False,
                            unstated=("checkdb",))
    action, refused = workflow._entry_action(entry, copy_only=False)
    assert action is None and "restore_id=SHARE states no checkdb" in str(refused)


def test_what_only_tuned_a_run_is_asked_for_too_since_the_operator_s_second_word(tmp_path):
    """It stayed optional with the four; on 2026-11-14 it became required with nearly everything else."""
    entry = _entry(**STATED)
    for name in ("checkdb_timeout_seconds", "checkdb_jobs", "tier", "copy_mode"):
        entry.pop(name)
    job = _load(tmp_path, entry)

    assert job.unstated == ("checkdb_timeout_seconds", "checkdb_jobs", "tier", "copy_mode")
    assert job.tier is None and job.copy_mode == "auto", "it loads on what it had been running on"
    action, refused = workflow._entry_action(job, copy_only=False)
    assert action is None and "states no checkdb_timeout_seconds, checkdb_jobs, tier, copy_mode" in str(refused)


# --------------------------------------------------------------------------- #
# The estate's own files, and the reference
# --------------------------------------------------------------------------- #
def test_the_shipped_example_states_them_on_every_restore_entry():
    from pathlib import Path

    example = json.loads((Path(__file__).resolve().parents[1] / "data/restore_config.example.json")
                         .read_text(encoding="utf-8-sig"))
    above = S.section_above(example["backup_restore"])
    for entry in example["backup_restore"]["restores"]:
        # A share-driven entry's copy_file_patterns and copy_recent_hours are the block's.
        assert S.unstated(entry, above=above) == (), entry.get("restore_id")


def test_the_reference_calls_them_required():
    from pathlib import Path

    reference = json.loads((Path(__file__).resolve().parents[1] / "dbabrain/common/catalogue/shared_config_objects.json")
                           .read_text(encoding="utf-8-sig"))["shared_config_objects"]
    fields = {f["field"]: f for f in next(o for o in reference if o["object"] == "restore_entry")["fields"]}

    for name in FOUR:
        assert fields[name]["required"] is True, name
        assert "refused inside its own run" in fields[name]["rule"], name
    # Required since 2026-11-14 as well; copy_recent_hours only where it is read (a share-driven entry,
    # or a script-driven one that copies a window), so the reference cannot call it required everywhere.
    for tuning in ("checkdb_timeout_seconds", "checkdb_jobs", "tier"):
        assert fields[tuning]["required"] is True, tuning
    assert fields["copy_recent_hours"]["required"] is False
    assert "refused inside its own run" in fields["copy_recent_hours"]["rule"]


# --------------------------------------------------------------------------- #
# restore-add writes them into a new entry
# --------------------------------------------------------------------------- #
def _registered(tmp_path, request) -> dict:
    from dbabrain.backup_restore import registration
    from tests.test_registering_a_backup_a_restore_and_an_os_login import KEY

    registration.add_restore(request, data_dir=tmp_path, key=KEY)
    return json.loads((tmp_path / "restore_config.json").read_text(encoding="utf-8"))["backup_restore"]["restores"][0]


def test_a_newly_registered_entry_states_every_one_and_never_assumes_it_may_replace(tmp_path):
    from tests.test_registering_a_backup_a_restore_and_an_os_login import _restore

    written = _registered(tmp_path, _restore())

    assert written["overwrite_existing"] is False, "it carried no word at all, and its first run refused it"
    assert {name: written[name] for name in FOUR} == STATED
    assert S.unstated(written) == ()


def test_a_registration_that_states_another_value_keeps_it(tmp_path):
    from tests.test_registering_a_backup_a_restore_and_an_os_login import _restore

    written = _registered(tmp_path, _restore(checkdb=False, overwrite_existing=True, copy_selection="window"))

    assert written["checkdb"] is False and written["overwrite_existing"] is True
    assert written["copy_selection"] == "window" and written["allow_production_target"] is False


def test_a_script_driven_entry_is_read_back_by_its_own_loader_and_told_its_own_rehearsal(tmp_path):
    """It answered ``loaded: false`` and suggested ``restore-latest``, the one command that refuses it."""
    from dbabrain.backup_restore import registration
    from tests.test_registering_a_backup_a_restore_and_an_os_login import KEY, _restore, _script_restore

    scripted = registration.add_restore(_script_restore(), data_dir=tmp_path, key=KEY)
    assert scripted["loaded"] is True
    assert scripted["next"][-1] == ("dbabrain backup_restore restore-workflow --restore-id "
                                    "ACME_PG_RESTORE_DRILL --dry-run")

    shared = registration.add_restore(_restore(), data_dir=tmp_path, key=KEY)
    assert shared["loaded"] is True and " restore-latest --restore-id " in shared["next"][-1]


def test_a_script_driven_sql_server_registration_names_its_databases(tmp_path):
    from dbabrain.backup_restore import registration
    from tests.test_registering_a_backup_a_restore_and_an_os_login import _script_restore

    request = _script_restore(restore_id="ACME_MSSQL_TO_DRILL", db_type="sqlserver",
                              env={"MSSQL_PORT": "11433", "MSSQL_USER": "sa"})
    with pytest.raises(registration.RegistrationError, match="states no database_mappings and no restore_all_databases"):
        _registered(tmp_path, request)

    written = _registered(tmp_path, {**request, "database_mappings": [
        {"source_database": "APPDB", "target_database": "APPDB_DRILL"}]})
    assert written["database_mappings"] == [{"source_database": "APPDB", "target_database": "APPDB_DRILL"}]
