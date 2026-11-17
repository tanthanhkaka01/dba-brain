"""A server is retired from the configuration one record at a time, the deepest first (0.41.0, 1.391).

The operator, 2026-11-17 (translated): lab instances never used again should come off the report - *and
off every JSON they are in: the secrets, db_instances.json, the users* - by *commands step by step, not
one command that cascades and deletes everything; delete from the deepest child up, otherwise the
parent refuses*.

What would go wrong unseen: a server removed while an entry still names it (the entry then dangles, and
an active one fails every run); a login removed while an instance still uses it; a secret removed while a
login names it; a report row removed while the register lists the server (the next run puts it back);
a removal written without being asked; a file rewritten in another format, so its diff shows every line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbabrain.common import config_retire as R

SERVER = "LAB-192-0-2-10"


@pytest.fixture
def data(tmp_path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    (d / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": SERVER, "db_type": "sqlserver", "default_credential_name": "LAB_SA",
         "metrics": {"enabled": False}},
        {"server_id": "LAB-KEPT", "db_type": "sqlserver"}]}, indent=2), encoding="utf-8")
    (d / "users.json").write_text(json.dumps({"database_credentials": [
        {"server_id": SERVER, "credentials": [{"credential_name": "LAB_SA", "password_ref": "LAB_SA_PW"}]}],
        "remote_credentials": []}, indent=2), encoding="utf-8")
    (d / "restore_config.json").write_text(json.dumps({"backup_restore": {
        "backups": [{"backup_id": "LAB_FULL", "server_id": SERVER}],
        "restores": [{"restore_id": "LAB_TO_DRILL", "server_id": SERVER, "target_server_id": "LAB-KEPT"}]}},
        indent=1), encoding="utf-8")
    # The report's inventory, CRLF as the master's is.
    (d / "database-inventory.json").write_bytes(
        json.dumps({"servers": [{"server_id": SERVER}, {"server_id": "LAB-KEPT"}]}, indent=1)
        .replace("\n", "\r\n").encode())
    return d


def _plan(fn, data, **request):
    return fn({"data_dir": str(data), **request})


def test_the_server_is_refused_while_anything_names_it_and_each_place_is_said(data):
    with pytest.raises(R.RetireError) as refused:
        _plan(R.remove_instance, data, server_id=SERVER, apply=True)
    said = str(refused.value)
    for place in ("restore_config.json backup_restore.backups[0].server_id (backup_id=LAB_FULL)",
                  "backup_restore.restores[0].server_id (restore_id=LAB_TO_DRILL)",
                  "users.json database_credentials[0].server_id"):
        assert place in said
    assert "database-inventory.json" not in said, "the report's row is the last step, not a pointer"
    assert "db_instances[0]" not in said, "its own record is not a pointer to itself"
    assert SERVER in (data / "db_instances.json").read_text(encoding="utf-8"), "nothing was removed"


def test_the_leaf_first_takes_it_off_everywhere_and_nothing_is_left_naming_it(data):
    _plan(R.remove_entry, data, file="restore_config.json", match={"restore_id": "LAB_TO_DRILL"}, apply=True)
    _plan(R.remove_entry, data, file="restore_config.json", match={"backup_id": "LAB_FULL"}, apply=True)
    with pytest.raises(R.RetireError, match="users.json database_credentials"):
        _plan(R.remove_instance, data, server_id=SERVER)                         # its login group, even as a plan
    # Its own server naming the login is the parent: the login, the child, goes first all the same.
    _plan(R.remove_credential, data, credential_name="LAB_SA", apply=True)
    _plan(R.remove_instance, data, server_id=SERVER, apply=True)
    _plan(R.remove_report_server, data, server_id=SERVER, apply=True)

    assert R.pointers(data, SERVER) == [] and R.pointers(data, "LAB_SA") == []
    assert R.pointers(data, "LAB-KEPT"), "the other server is untouched"


def test_a_login_another_server_uses_is_refused(data):
    instances = json.loads((data / "db_instances.json").read_text(encoding="utf-8"))
    instances["db_instances"][1]["default_credential_name"] = "LAB_SA"
    (data / "db_instances.json").write_text(json.dumps(instances, indent=2), encoding="utf-8")

    with pytest.raises(R.RetireError, match=r"db_instances\[1\].default_credential_name \(server_id=LAB-KEPT\)"):
        _plan(R.remove_credential, data, credential_name="LAB_SA", apply=True)


def test_a_login_goes_once_only_its_own_server_uses_it_and_its_group_with_the_last(data):
    out = _plan(R.remove_credential, data, credential_name="LAB_SA", apply=True)

    assert out["removed"] and out["group_removed"] and out["password_ref"] == "LAB_SA_PW"
    assert json.loads((data / "users.json").read_text(encoding="utf-8"))["database_credentials"] == []


def test_without_apply_it_is_a_plan_and_nothing_is_written(data):
    before = (data / "restore_config.json").read_bytes()
    out = _plan(R.remove_entry, data, file="restore_config.json", match={"restore_id": "LAB_TO_DRILL"})
    assert (out["removed"], out["applied"], out["list"]) == (False, False, "backup_restore.restores")
    assert (data / "restore_config.json").read_bytes() == before


def test_a_removal_keeps_the_files_indent_and_line_endings(data):
    (data / "db_instances.json").write_text(json.dumps({"db_instances": [{"server_id": SERVER}]}), encoding="utf-8")
    raw = (data / "database-inventory.json").read_bytes()
    _plan(R.remove_entry, data, file="restore_config.json", match={"restore_id": "LAB_TO_DRILL"}, apply=True)
    assert '\n "backup_restore": {' in (data / "restore_config.json").read_text(encoding="utf-8"), "indent 1 kept"

    (data / "db_instances.json").write_text(json.dumps({"db_instances": []}), encoding="utf-8")
    _plan(R.remove_report_server, data, server_id=SERVER, apply=True)
    after = (data / "database-inventory.json").read_bytes()
    assert b"\r\n" in after and b"\n" not in after.replace(b"\r\n", b"")
    assert len(raw.splitlines()) - len(after.splitlines()) == 3, "the row's three lines, nothing else"


def test_the_reports_row_says_how_many_servers_the_report_keeps_as_a_plan_and_when_applied(data):
    """The count a person reads a retirement by (the estate's report went 52 -> 31): a plan says what
    would be left, the applied run what is - the same number."""
    (data / "db_instances.json").write_text(json.dumps({"db_instances": []}), encoding="utf-8")
    planned = _plan(R.remove_report_server, data, server_id=SERVER)
    applied = _plan(R.remove_report_server, data, server_id=SERVER, apply=True)
    assert planned["servers_left"] == applied["servers_left"] == 1
    assert not planned["removed"] and applied["removed"]


def test_the_reports_row_is_refused_while_the_register_lists_the_server(data):
    with pytest.raises(R.RetireError, match="instance-remove it first"):
        _plan(R.remove_report_server, data, server_id=SERVER, apply=True)


@pytest.mark.parametrize("request_, words", [
    ({"file": "users.json", "match": {"credential_name": "LAB_SA"}}, "credential-remove"),
    ({"file": "db_instances.json", "match": {"server_id": SERVER}}, "instance-remove"),
    ({"file": "restore_config.json", "match": {"server_id": SERVER}}, "2 record"),
    ({"file": "restore_config.json", "match": {"restore_id": "NONE"}}, "0 record"),
    ({"file": "../outside.json", "match": {"id": "x"}}, "one data/\\*.json file name"),
    ({"file": "restore_config.json"}, "match is required"),
])
def test_one_record_is_removed_at_a_time_and_by_its_own_command(data, request_, words):
    with pytest.raises(R.RetireError, match=words):
        _plan(R.remove_entry, data, **request_)


def test_a_file_that_cannot_be_read_stops_every_removal_past_it(data):
    (data / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(R.RetireError, match=r"broken.json \(unreadable\)"):
        _plan(R.remove_entry, data, file="restore_config.json", match={"restore_id": "LAB_TO_DRILL"})


def test_the_reference_and_its_examples_are_not_configuration():
    assert R._skipped("shared_config_objects.json") and R._skipped("db_instances.example.json")
    assert R._skipped("encrypted_secret_text.json") and R._skipped("config_references.json")
    assert not R._skipped("restore_config.json")


def test_a_secret_is_refused_while_a_login_names_its_ref(data, monkeypatch, capsys):
    from dbabrain.common import cli

    monkeypatch.setattr("dbabrain.lib.paths.DEFAULT_DATA_DIR", data)
    import io

    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"ref": "LAB_SA_PW", "value": "x"})
                                                                 .encode()), encoding="utf-8"))
    code = cli._secret_remove_command(["-"])
    answer = json.loads(capsys.readouterr().out)
    assert code != 0 and not answer["success"]
    assert "LAB_SA_PW is still named in 1 place(s)" in answer["error"] and "users.json" in answer["error"]


def test_a_record_told_only_by_the_server_it_points_at_goes_before_the_server(data):
    """A backup policy override is told by its server_id: it names nothing, the server names it not."""
    (data / "backup_policy.json").write_text(json.dumps({"backup_policy": {
        "overrides": [{"server_id": SERVER, "required": False}, {"server_id": "LAB-KEPT"}]}}), encoding="utf-8")

    out = _plan(R.remove_entry, data, file="backup_policy.json", match={"server_id": SERVER}, apply=True)

    assert out["removed"] and out["list"] == "backup_policy.overrides"
    assert json.loads((data / "backup_policy.json").read_text(encoding="utf-8"))["backup_policy"]["overrides"] == [
        {"server_id": "LAB-KEPT"}]


def test_a_name_several_servers_share_is_removed_from_the_group_named_and_the_report_never_holds_it(data):
    """The labs' logins were copied per server under one name; the report's inventory names logins too."""
    users = json.loads((data / "users.json").read_text(encoding="utf-8"))
    users["database_credentials"].append({"server_id": "LAB-KEPT", "credentials": [
        {"credential_name": "LAB_SA", "password_ref": "LAB_SA_PW"}]})
    (data / "users.json").write_text(json.dumps(users, indent=2), encoding="utf-8")
    inventory = json.loads((data / "database-inventory.json").read_text(encoding="utf-8"))
    inventory["servers"][0]["remote_user"] = {"credential_name": "LAB_SA"}
    (data / "database-inventory.json").write_text(json.dumps(inventory, indent=1), encoding="utf-8")

    with pytest.raises(R.RetireError, match=r"2 login\(s\) named LAB_SA .*servers LAB-192-0-2-10, LAB-KEPT.*name its server_id"):
        _plan(R.remove_credential, data, credential_name="LAB_SA")
    out = _plan(R.remove_credential, data, credential_name="LAB_SA", server_id=SERVER, apply=True)

    assert out["server_id"] == SERVER and out["group_removed"]
    left = json.loads((data / "users.json").read_text(encoding="utf-8"))["database_credentials"]
    assert [g["server_id"] for g in left] == ["LAB-KEPT"], "the other server's copy stays"


def test_a_server_naming_its_own_copy_of_a_shared_name_does_not_hold_another_servers_login(data):
    """Each lab server's group held a login of one name; each instance reads its own group's."""
    users = json.loads((data / "users.json").read_text(encoding="utf-8"))
    users["database_credentials"].append({"server_id": "LAB-KEPT", "credentials": [{"credential_name": "LAB_SA"}]})
    (data / "users.json").write_text(json.dumps(users, indent=2), encoding="utf-8")
    instances = json.loads((data / "db_instances.json").read_text(encoding="utf-8"))
    instances["db_instances"][1]["default_credential_name"] = "LAB_SA"          # LAB-KEPT's own copy
    (data / "db_instances.json").write_text(json.dumps(instances, indent=2), encoding="utf-8")

    out = _plan(R.remove_credential, data, credential_name="LAB_SA", server_id=SERVER, apply=True)

    assert out["removed"], "LAB-KEPT names its own LAB_SA, not this server's"
