"""A restore target whose environment nobody stated is refused, and the registrar no longer writes one
(0.39.0, decision D-39.6).

A restore REPLACEs what it lands on, so a target the inventory calls production needs the entry's
own ``allow_production_target: true`` (1.223). Until 0.39.0 that guard read only what was there: a
target whose record stated no ``environment`` - or that had no record at all - passed as if it were
known not to be production. And ``instance-add``, the command that writes the record, asked for a
``server_id``, a ``db_type`` and an ``ip`` and wrote a record with no ``site`` and no
``environment``, although the inventory reference marks both required. One command made the gap and
the other let a restore through it.

Unknown is not "not production". Nobody but the machine's owner knows which it is, so nothing here
guesses one and ``upgrade-config`` writes none: the restore is refused with what to do - state the
label on that record - and the registration is refused until it states both fields. The entry's
``allow_production_target: true`` lets any target through, exactly as before.
"""

from __future__ import annotations

import json

import pytest

from dbabrain.backup_restore import config as restore_config
from dbabrain.common import instance_admin
from dbabrain.lib import backup_runbook as R
from dbabrain.lib import cli_types, shared_objects
from dbabrain.lib import integrity as I

KEY = "test-passphrase"
REGISTRATION = {"server_id": "ACME-192-0-2-10", "db_type": "sqlserver", "ip": "192.0.2.10", "port": 1433,
                "site": "ACME", "environment": "lab"}


def _inventory(root, *records):
    (root / "db_instances.json").write_text(json.dumps({"db_instances": list(records)}), encoding="utf-8")
    return root


def _records(root):
    path = root / "db_instances.json"
    return json.loads(path.read_text(encoding="utf-8"))["db_instances"] if path.exists() else []


# --------------------------------------------------------------------------- #
# The restore: unknown is refused, and the refusal says what to do
# --------------------------------------------------------------------------- #
def test_a_target_whose_record_states_no_environment_is_refused_and_told_to_state_it():
    why = I.production_target_refusal("", allowed=False, restore_id="SALES_DRILL", target="ACME-LAB-9")
    assert why.startswith("SALES_DRILL: the target ACME-LAB-9 has no environment in db_instances.json")
    assert "State \"environment\" on ACME-LAB-9's record in db_instances.json" in why
    assert "allow_production_target" in why


def test_a_target_the_inventory_does_not_list_is_refused_and_told_to_register_it():
    why = I.production_target_refusal(None, allowed=False, restore_id="SALES_DRILL", target="ACME-NEW-1")
    assert "the target ACME-NEW-1 is not in db_instances.json" in why
    assert "Register ACME-NEW-1 in db_instances.json with its environment" in why
    assert "instance-add requires site and environment" in why


def test_an_entry_that_names_no_target_is_refused_rather_than_read_as_safe():
    why = I.production_target_refusal(None, allowed=False, restore_id="SALES_DRILL", target="")
    assert "names no target" in why and "target_server_id" in why


def test_a_blank_label_is_no_label():
    assert "has no environment" in I.production_target_refusal("   ", allowed=False, restore_id="R", target="T")


@pytest.mark.parametrize("environment", ["lab", "dev", "uat", "test", "sandbox"])
def test_a_stated_label_other_than_production_still_passes(environment):
    assert I.production_target_refusal(environment, allowed=False, restore_id="R", target="T") == ""


@pytest.mark.parametrize("environment", ["", None])
def test_allow_production_target_still_lets_an_unmarked_target_through(environment):
    assert I.production_target_refusal(environment, allowed=True, restore_id="R", target="T") == ""


def test_the_inventory_answers_no_record_apart_from_no_label(tmp_path):
    root = _inventory(tmp_path, {"server_id": "ACME-UNMARKED-1"}, {"server_id": "ACME-LAB-1", "environment": "lab"})
    assert restore_config.target_environment("ACME-UNMARKED-1", data_dir=root) == ""
    assert restore_config.target_environment("ACME-NOT-THERE", data_dir=root) is None
    assert restore_config.target_environment("", data_dir=root) is None
    assert restore_config.target_environment("ACME-LAB-1", data_dir=root) == "lab"


@pytest.mark.parametrize("target, says", [
    ("ACME-UNMARKED-1", "has no environment in db_instances.json"),
    ("ACME-NOT-THERE", "is not in db_instances.json"),
])
def test_both_restore_paths_refuse_an_unmarked_target_before_anything_runs(tmp_path, target, says):
    """``assert_target_is_allowed`` is what ``restore-by-id`` and the share-driven workflow call
    before the first copy."""
    root = _inventory(tmp_path, {"server_id": "ACME-UNMARKED-1"})
    with pytest.raises(restore_config.ProductionTargetRefused, match=says):
        restore_config.assert_target_is_allowed(restore_id="R", target_server_id=target, allowed=False,
                                                data_dir=root)
    restore_config.assert_target_is_allowed(restore_id="R", target_server_id=target, allowed=True,
                                            data_dir=root)


def test_a_record_labelled_under_the_old_spelling_is_a_stated_label(tmp_path):
    """``env`` is the inventory's legacy spelling of ``environment``, and still read."""
    root = _inventory(tmp_path, {"server_id": "ACME-OLD-1", "env": "lab"})
    restore_config.assert_target_is_allowed(restore_id="R", target_server_id="ACME-OLD-1", allowed=False,
                                            data_dir=root)


def test_the_runbook_says_the_run_will_be_refused_and_why():
    """The assistant's runbook says a step is refused before the line that would meet it, so it
    asks the same function the run asks."""
    node = R.node_form({"install": "pip", "shell": "bash", "runtime": "docker", "python": "/usr/local/bin/python"},
                       node_role="worker", has_worker=False)
    entry = {"restore_id": "R1", "server_id": "ACME-SRC-1", "target_server_id": "ACME-TGT-1",
             "db_type": "postgresql", "script": "restore.sh", "backup_dir": "/backup",
             "target_backup_dir": "/restore", "restore_from": "server", "action": "restore",
             "allow_production_target": False}

    def run_step(environments):
        runbook = R.restore_runbook(entry, node=node, twins={}, environments=environments)
        return next(step for step in runbook["steps"] if step["step"] == "Restore to the newest backup"), runbook

    unmarked, runbook = run_step({"ACME-TGT-1": ""})
    assert "State \"environment\" on ACME-TGT-1's record" in unmarked["will_be_refused"]
    assert "or lists with no environment" in runbook["guards"][0]
    assert "is not in db_instances.json" in run_step({})[0]["will_be_refused"]
    assert "will_be_refused" not in run_step({"ACME-TGT-1": "lab"})[0]


# --------------------------------------------------------------------------- #
# The registrar: a registration states its site and its environment
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("left_out, named", [
    (("site", "environment"), "site, environment"), (("site",), "site"), (("environment",), "environment"),
])
def test_a_registration_without_site_or_environment_is_refused_naming_each_and_writes_nothing(
        tmp_path, left_out, named):
    root = _inventory(tmp_path)
    request = {key: value for key, value in REGISTRATION.items() if key not in left_out}
    with pytest.raises(instance_admin.InstanceAdminError) as refused:
        instance_admin.add_instance(request, data_dir=root, key=KEY)
    text = str(refused.value)
    assert text.startswith(f"missing required field(s): {named}.")
    assert "only the machine's owner knows whether it is production" in text and "Nothing was written" in text
    assert _records(root) == []


def test_every_missing_field_is_named_in_one_refusal(tmp_path):
    with pytest.raises(instance_admin.InstanceAdminError, match=r"missing required field\(s\): ip, site, environment\."):
        instance_admin.add_instance({"server_id": "ACME-1", "db_type": "sqlserver"}, data_dir=tmp_path, key=KEY)


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_environment_is_no_environment(tmp_path, blank):
    with pytest.raises(instance_admin.InstanceAdminError, match="environment"):
        instance_admin.add_instance({**REGISTRATION, "environment": blank}, data_dir=tmp_path, key=KEY)


def test_an_inactive_registration_states_both_too(tmp_path):
    """Switched off is still a machine somebody may restore onto."""
    request = {key: value for key, value in REGISTRATION.items() if key != "environment"}
    with pytest.raises(instance_admin.InstanceAdminError, match="environment"):
        instance_admin.add_instance({**request, "active": False}, data_dir=tmp_path, key=KEY)


def test_a_registration_that_states_both_writes_them_on_the_record(tmp_path):
    root = _inventory(tmp_path)
    instance_admin.add_instance(dict(REGISTRATION), data_dir=root, key=KEY)
    record = _records(root)[0]
    assert record["site"] == "ACME" and record["environment"] == "lab"


def test_the_old_env_spelling_states_the_environment_and_is_written_under_the_standard_name(tmp_path):
    root = _inventory(tmp_path)
    request = {key: value for key, value in REGISTRATION.items() if key != "environment"}
    instance_admin.add_instance({**request, "env": "prod"}, data_dir=root, key=KEY)
    record = _records(root)[0]
    assert record["environment"] == "prod" and "env" not in record


def test_the_refusal_is_judged_from_the_request_alone_on_a_root_that_does_not_exist(tmp_path):
    """Rules R09: a common.cli command works from its request - nothing is read to decide this."""
    root = tmp_path / "nothing-here"
    with pytest.raises(instance_admin.InstanceAdminError, match="site, environment"):
        instance_admin.add_instance({"server_id": "ACME-1", "db_type": "sqlserver", "ip": "192.0.2.10"},
                                    data_dir=root, key=KEY)
    assert not root.exists()


def test_a_second_login_is_not_asked_for_either(tmp_path):
    """``keep_default`` writes no inventory field, so it carries neither - and still refuses one."""
    root = _inventory(tmp_path)
    instance_admin.add_instance({**REGISTRATION, "username": "monitor", "password": "first"}, data_dir=root, key=KEY)
    login = {"server_id": REGISTRATION["server_id"], "db_type": "sqlserver", "ip": REGISTRATION["ip"],
             "username": "dba", "password": "second", "credential_name": "MSSQL_ACME_DBA", "keep_default": True}
    assert instance_admin.add_instance(dict(login), data_dir=root, key=KEY)["credential_name"] == "MSSQL_ACME_DBA"
    with pytest.raises(instance_admin.InstanceAdminError, match="ignored: environment"):
        instance_admin.add_instance({**login, "environment": "lab", "replace": True}, data_dir=root, key=KEY)


def test_the_command_answers_the_refusal_as_a_failed_response(tmp_path, monkeypatch, capsys):
    from dbabrain.common import cli
    from dbabrain.lib import data_sources

    monkeypatch.setattr(data_sources, "DEFAULT_DATA_DIR", tmp_path)
    request = {key: value for key, value in REGISTRATION.items() if key not in ("site", "environment")}
    assert cli._instance_add_command([json.dumps(request)]) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer["success"] is False and "site, environment" in answer["error"]
    assert _records(tmp_path) == []


def test_the_help_and_the_reference_say_both_are_required():
    assert "site           required." in instance_admin.USAGE
    assert "environment    required." in instance_admin.USAGE
    entry = shared_objects.describe("input_instance_add", data_dir=None)
    fields = {item["field"]: item for item in entry["fields"]}
    assert fields["site"]["required"] is True and fields["environment"]["required"] is True
    assert entry["legacy_fields"]["env"] == "environment"
    assert {"site", "environment"} <= cli_types.InstanceAddRequest.__required_keys__


def test_upgrade_config_writes_no_environment_into_an_unmarked_record(tmp_path):
    """Only the owner knows a machine's environment; an upgrade that wrote one would be a guess."""
    from dbabrain.common import config_upgrade

    # A data/ folder of its own: the upgrade keeps its backups beside it, under runtime/.
    root = tmp_path / "data"
    root.mkdir()
    _inventory(root, {"server_id": "ACME-UNMARKED-1", "db_type": "host", "ip": "192.0.2.9", "active": False})
    config_upgrade.upgrade({"data_dir": str(root), "dry_run": False})
    assert "environment" not in _records(root)[0]
