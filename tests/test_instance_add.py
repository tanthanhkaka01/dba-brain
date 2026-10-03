r"""Registering one database to monitor, as one operation instead of four hand-edits.

Standing a node up on 2026-09-10 measured what this replaces: add an object to
`data/db_instances.json`, add a matching group to `data/users.json` keyed on the same `server_id`,
write the password into `secrets/secret_text.json` **in the clear**, then run `encrypt-secret` and
remember to delete the plaintext. Four files for one idea, the identity repeated in two of them and
the credential name in three, with nothing checking that they agree.

The failure mode of getting it wrong is quiet: a target that resolves to no credential, reported
later by `check-credentials`, which is a different command run at a different time.

**The password never reaches the disk unencrypted**, and that is the part a later `rm` cannot undo:
a plaintext secret that existed for ten seconds is in the editor's undo history, the shell's
history, and whatever backed the directory up in between.
"""

from __future__ import annotations

import json

import pytest

from db_ops.common import instance_admin
from db_ops.lib import secret_text as _secret_text

KEY = "test-passphrase"


def _root(tmp_path):
    (tmp_path / "db_instances.json").write_text(
        json.dumps({"db_instances": []}), encoding="utf-8")
    (tmp_path / "users.json").write_text(
        json.dumps({"database_credentials": []}), encoding="utf-8")
    return tmp_path


def _instances(root):
    return json.loads((root / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]


def _credentials(root):
    return json.loads((root / "users.json").read_text(encoding="utf-8"))["database_credentials"]


def _secrets(root):
    return _secret_text.load_secret_text_file(root / "encrypted_secret_text.json", key=KEY)


def test_one_call_writes_the_inventory_the_credential_and_the_secret(tmp_path):
    root = _root(tmp_path)
    answer = instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10",
         "username": "sa", "password": "s3cret"},
        data_dir=root, key=KEY)

    assert _instances(root)[0]["server_id"] == "LAB-1"
    assert _credentials(root)[0]["credentials"][0]["username"] == "sa"
    assert _secrets(root)[answer["password_ref"]] == "s3cret"


def test_the_password_is_never_written_in_the_clear(tmp_path):
    root = _root(tmp_path)
    instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10",
         "username": "sa", "password": "s3cret"},
        data_dir=root, key=KEY)

    # Not in any file this command touched, and no plaintext store was created on the way.
    for path in root.rglob("*"):
        if path.is_file():
            assert "s3cret" not in path.read_text(encoding="utf-8", errors="replace"), path.name
    assert not (root.parent / "secrets" / "secret_text.json").exists()


def test_the_credential_name_is_derived_so_the_three_files_cannot_disagree(tmp_path):
    root = _root(tmp_path)
    answer = instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10",
         "username": "sa", "password": "x"},
        data_dir=root, key=KEY)

    name = answer["credential_name"]
    assert name == "MSSQL_LAB_1_MONITOR"
    assert _instances(root)[0]["default_credential_name"] == name
    assert _credentials(root)[0]["credentials"][0]["credential_name"] == name
    assert _credentials(root)[0]["credentials"][0]["password_ref"] == name
    assert name in _secrets(root)


def test_the_port_defaults_per_engine_rather_than_to_one_number(tmp_path):
    # A wrong port fails as a timeout, which reads as "the host is down".
    for db_type, port in (("sqlserver", 1433), ("oracle", 1521),
                          ("postgresql", 5432), ("mysql", 3306)):
        (tmp_path / db_type).mkdir(exist_ok=True)
        root = _root(tmp_path / db_type)
        instance_admin.add_instance(
            # db_name only matters to postgresql/mysql, which refuse without one; the other two
            # ignore it, so passing it everywhere keeps this test about the port.
            {"server_id": f"T-{db_type}", "db_type": db_type, "ip": "192.0.2.10",
             "db_name": "postgres"},
            data_dir=root, key=KEY)
        assert _instances(root)[0]["port"] == port


def test_an_explicit_port_wins(tmp_path):
    root = _root(tmp_path)
    instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10", "port": 1453},
        data_dir=root, key=KEY)
    assert _instances(root)[0]["port"] == 1453


def test_unknown_fields_are_passed_through_rather_than_dropped(tmp_path):
    # The inventory grows faster than any allow-list of keys, and a silently dropped `cmd_access`
    # is a target whose OS metrics never run.
    root = _root(tmp_path)
    instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10",
         "major_version": 16, "service_name": "MSSQL", "platform": "linux",
         "cmd_access": {"method": "ssh", "auth_type": "password"}},
        data_dir=root, key=KEY)
    record = _instances(root)[0]
    assert record["major_version"] == 16
    assert record["cmd_access"] == {"method": "ssh", "auth_type": "password"}


def test_a_duplicate_server_id_is_refused_rather_than_overwritten(tmp_path):
    root = _root(tmp_path)
    payload = {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10"}
    instance_admin.add_instance(dict(payload), data_dir=root, key=KEY)
    with pytest.raises(instance_admin.InstanceAdminError) as excinfo:
        instance_admin.add_instance(dict(payload), data_dir=root, key=KEY)
    assert "already in" in str(excinfo.value)
    assert len(_instances(root)) == 1


def test_replace_overwrites_exactly_one_record(tmp_path):
    root = _root(tmp_path)
    instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10"},
        data_dir=root, key=KEY)
    instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.99", "replace": True},
        data_dir=root, key=KEY)
    assert len(_instances(root)) == 1
    assert _instances(root)[0]["ip"] == "192.0.2.99"


def test_a_missing_required_field_names_all_of_them(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(instance_admin.InstanceAdminError) as excinfo:
        instance_admin.add_instance({"server_id": "LAB-1"}, data_dir=root, key=KEY)
    assert "db_type" in str(excinfo.value) and "ip" in str(excinfo.value)


def test_a_username_with_no_secret_at_all_is_refused(tmp_path):
    # The quiet failure this command exists to prevent: a target that resolves to no credential.
    root = _root(tmp_path)
    with pytest.raises(instance_admin.InstanceAdminError) as excinfo:
        instance_admin.add_instance(
            {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10", "username": "sa"},
            data_dir=root, key=KEY)
    assert "no credential" in str(excinfo.value)


def test_giving_both_a_password_and_a_ref_is_refused(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(instance_admin.InstanceAdminError) as excinfo:
        instance_admin.add_instance(
            {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10",
             "username": "sa", "password": "x", "password_ref": "EXISTING"},
            data_dir=root, key=KEY)
    assert "not both" in str(excinfo.value)


def test_an_existing_ref_is_pointed_at_rather_than_re_stored(tmp_path):
    root = _root(tmp_path)
    answer = instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10",
         "username": "sa", "password_ref": "SHARED_MONITOR_PW"},
        data_dir=root, key=KEY)
    assert answer["password_stored_encrypted"] is False
    assert _credentials(root)[0]["credentials"][0]["password_ref"] == "SHARED_MONITOR_PW"
    assert not (root / "encrypted_secret_text.json").exists()


def test_a_password_with_no_passphrase_writes_nothing(tmp_path):
    # Half-writing here would leave a registered target with no value behind its credential -
    # exactly the state this command exists to prevent, arrived at by the command itself.
    root = _root(tmp_path)
    with pytest.raises(instance_admin.InstanceAdminError) as excinfo:
        instance_admin.add_instance(
            {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10",
             "username": "sa", "password": "x"},
            data_dir=root, key=None)
    assert "passphrase" in str(excinfo.value)
    assert _instances(root) == []
    # The refusal says "Nothing was written"; until 0.26.0 users.json already held the login.
    assert _credentials(root) == []


def test_a_wrong_passphrase_refuses_rather_than_replacing_the_store(tmp_path):
    # Overwriting would lose every secret already in it, and the operator would not find out until
    # the next connection attempt.
    root = _root(tmp_path)
    instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10",
         "username": "sa", "password": "first"}, data_dir=root, key=KEY)
    with pytest.raises(instance_admin.InstanceAdminError) as excinfo:
        instance_admin.add_instance(
            {"server_id": "LAB-2", "db_type": "sqlserver", "ip": "192.0.2.11",
             "username": "sa", "password": "second"}, data_dir=root, key="wrong-passphrase")
    assert "could not be opened" in str(excinfo.value)
    assert _secrets(root)["MSSQL_LAB_1_MONITOR"] == "first"
    # The secret is stored before the login is filed, so the refused one left no login behind.
    assert [group["server_id"] for group in _credentials(root)] == ["LAB-1"]


def test_a_second_target_keeps_the_first_one_s_secret(tmp_path):
    root = _root(tmp_path)
    instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10",
         "username": "sa", "password": "one"}, data_dir=root, key=KEY)
    instance_admin.add_instance(
        {"server_id": "LAB-2", "db_type": "postgresql", "ip": "192.0.2.11",
         "db_name": "postgres", "username": "postgres", "password": "two"},
        data_dir=root, key=KEY)
    secrets = _secrets(root)
    assert secrets["MSSQL_LAB_1_MONITOR"] == "one"
    assert secrets["PG_LAB_2_MONITOR"] == "two"
    assert len(_instances(root)) == 2


def test_a_target_with_no_login_is_allowed_and_writes_no_credential(tmp_path):
    # Some targets are reached with a credential configured elsewhere; registering the machine is
    # still worth doing on its own.
    root = _root(tmp_path)
    answer = instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10"},
        data_dir=root, key=KEY)
    assert answer["credential_name"] == ""
    assert _credentials(root) == []
    assert "default_credential_name" not in _instances(root)[0]


def test_the_command_is_registered_and_listed():
    from db_ops.common import cli, instance_admin

    assert hasattr(cli, "_instance_add_command")
    # Listed in the command index too: a command nobody can discover is a command nobody runs.
    assert "instance-add" in instance_admin.USAGE


# A second database login on a server, its default left alone (0.26.0 sheet §6 C8). Ten such logins
# on the estate - a monitor account beside the DBA's, an application user beside `sys` - had no
# command: registering the server again with the new login moved its default, and the only way
# back was a third call with the old one. `keep_default` files the login and touches nothing else.


def _registered(tmp_path):
    root = _root(tmp_path)
    instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10", "port": 1433,
         "username": "monitor", "password": "first"}, data_dir=root, key=KEY)
    return root


def _second_login(**extra):
    request = {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10",
               "username": "dba", "password": "second", "credential_name": "MSSQL_LAB_1_DBA",
               "role": "dba", "keep_default": True}
    request.update(extra)
    return request


def test_a_second_login_is_filed_beside_the_default_which_stays_the_default(tmp_path):
    root = _registered(tmp_path)
    inventory_before = (root / "db_instances.json").read_bytes()

    answer = instance_admin.add_instance(_second_login(), data_dir=root, key=KEY)

    assert (root / "db_instances.json").read_bytes() == inventory_before
    assert _instances(root)[0]["default_credential_name"] == "MSSQL_LAB_1_MONITOR"
    logins = {item["credential_name"]: item for item in _credentials(root)[0]["credentials"]}
    assert set(logins) == {"MSSQL_LAB_1_MONITOR", "MSSQL_LAB_1_DBA"}
    assert logins["MSSQL_LAB_1_DBA"]["username"] == "dba"
    assert logins["MSSQL_LAB_1_DBA"]["role"] == "dba"
    assert _secrets(root)["MSSQL_LAB_1_DBA"] == "second"
    assert _secrets(root)["MSSQL_LAB_1_MONITOR"] == "first"
    assert answer["credential_name"] == "MSSQL_LAB_1_DBA"
    assert answer["replaced"] is False
    assert "db_instances.json" not in answer["files_written"]


def test_a_second_login_for_a_server_not_registered_is_refused(tmp_path):
    # There is no default to keep, and a login filed under a server_id nobody registered is one
    # nothing resolves to.
    root = _root(tmp_path)
    with pytest.raises(instance_admin.InstanceAdminError, match="Register it first"):
        instance_admin.add_instance(_second_login(), data_dir=root, key=KEY)
    assert _credentials(root) == []


@pytest.mark.parametrize("field, value", [("ip", "192.0.2.99"), ("db_type", "postgresql")])
def test_a_second_login_that_describes_another_server_is_refused(tmp_path, field, value):
    # A wrong server_id beside the right ip would file the login under the wrong machine.
    root = _registered(tmp_path)
    with pytest.raises(instance_admin.InstanceAdminError, match="does not describe LAB-1"):
        instance_admin.add_instance(_second_login(**{field: value}), data_dir=root, key=KEY)
    assert len(_credentials(root)[0]["credentials"]) == 1


def test_a_second_login_must_be_named_since_the_derived_name_is_the_default_s(tmp_path):
    root = _registered(tmp_path)
    request = _second_login()
    del request["credential_name"]
    with pytest.raises(instance_admin.InstanceAdminError, match="needs a credential_name"):
        instance_admin.add_instance(request, data_dir=root, key=KEY)
    assert _secrets(root)["MSSQL_LAB_1_MONITOR"] == "first"


def test_a_second_login_named_as_the_default_is_refused(tmp_path):
    root = _registered(tmp_path)
    with pytest.raises(instance_admin.InstanceAdminError, match="default login"):
        instance_admin.add_instance(
            _second_login(credential_name="MSSQL_LAB_1_MONITOR"), data_dir=root, key=KEY)
    assert _secrets(root)["MSSQL_LAB_1_MONITOR"] == "first"


def test_a_second_login_carrying_an_inventory_field_is_refused(tmp_path):
    # Nothing on the record changes on this path, so a port here would be silently dropped.
    root = _registered(tmp_path)
    with pytest.raises(instance_admin.InstanceAdminError, match="ignored: port"):
        instance_admin.add_instance(_second_login(port=1533), data_dir=root, key=KEY)


def test_a_second_login_with_no_username_is_refused(tmp_path):
    root = _registered(tmp_path)
    request = _second_login()
    del request["username"], request["password"]
    with pytest.raises(instance_admin.InstanceAdminError, match="names none"):
        instance_admin.add_instance(request, data_dir=root, key=KEY)


def test_an_existing_second_login_is_overwritten_only_with_replace(tmp_path):
    root = _registered(tmp_path)
    instance_admin.add_instance(_second_login(), data_dir=root, key=KEY)
    with pytest.raises(instance_admin.InstanceAdminError, match="Pass replace"):
        instance_admin.add_instance(_second_login(password="third"), data_dir=root, key=KEY)
    assert _secrets(root)["MSSQL_LAB_1_DBA"] == "second"

    answer = instance_admin.add_instance(
        _second_login(password="third", replace=True), data_dir=root, key=KEY)
    assert answer["replaced"] is True
    assert _secrets(root)["MSSQL_LAB_1_DBA"] == "third"
    assert len(_credentials(root)[0]["credentials"]) == 2


def test_keep_default_false_is_an_ordinary_registration_and_not_written_on_the_record(tmp_path):
    root = _root(tmp_path)
    instance_admin.add_instance(
        {"server_id": "LAB-1", "db_type": "sqlserver", "ip": "192.0.2.10", "keep_default": False},
        data_dir=root, key=KEY)
    assert "keep_default" not in _instances(root)[0]


def test_the_command_says_a_login_was_added_not_that_the_server_was_registered(tmp_path, monkeypatch,
                                                                              capsys):
    # "registered LAB-1" for a call that registered nothing reads as the inventory having changed;
    # and with replace, "replaced LAB-1" as the server's record having been overwritten.
    from db_ops.common import cli
    from db_ops.lib import data_sources

    root = _registered(tmp_path)
    monkeypatch.setattr(data_sources, "DEFAULT_DATA_DIR", root)
    monkeypatch.delenv("DB_OPS_SECRET_KEY", raising=False)
    cli._instance_add_command([json.dumps(_second_login()), "--key", KEY])
    message = json.loads(capsys.readouterr().out)["message"]
    assert message.startswith("added login MSSQL_LAB_1_DBA on LAB-1, beside its default")
