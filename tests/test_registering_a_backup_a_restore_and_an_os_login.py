"""The three registrations that were still hand-edits, and the traps each one used to set.

`instance-add` exists because registering a database was four hand-edits and a plaintext password
on disk. Three things were left in that state, and each is a file an operator had to open in an
editor to make the tool work at all:

* a **backup** entry and a **restore** entry in `data/restore_config.json` - asked for directly on
  2026-09-13, *"phải có cli thêm backup restore giống cli thêm instance nhé"*;
* an **OS login** in `users.json` `remote_credentials`, which 18 of this estate's entries were
  copied between nodes by hand to get.

What is asserted here is mostly *refusals*, and deliberately so. A command that writes the config
is easy; the value is in the three or four ways a hand-edit goes quietly wrong, each of which cost
a real diagnosis before it was written down:

* a `cmd_access.method: "local"` pointed at a remote host reports the **container's own** CPU under
  that host's name - a wrong answer, not a failure;
* `auth_type` defaults to `key`, so an SSH block with a password and no `auth_type` resolves to no
  credential at all and the password is never read;
* `cleanup_retention` absent reads exactly like `cleanup_retention` considered - six of fourteen
  restore entries silently carried none;
* and a secret must never be stored for a config that is then refused, or it sits in the encrypted
  store under a ref nothing mentions.
"""

from __future__ import annotations

import json

import pytest

from db_ops.backup_restore import registration
from db_ops.backup_restore.backup import load_backup_jobs
from db_ops.common import instance_admin, remote_credential_admin
from db_ops.lib import cmd_access, json_io, secret_text

KEY = "a-test-passphrase"


WINDOW = {"from_hour": 1, "to_hour": 4, "repeat_interval": 72000,
          "retry_interval": 1800, "timeout": 7200}

NOTIFY = {"logging_on_run": {"enabled": True, "telegram_chat": "backup"},
          "alert_on_error": {"enabled": True, "telegram_chat": "backup"}}


def _backup(**over):
    request = {
        "backup_id": "ACME_APPDB_FULL",
        "server_id": "ACME-192-0-2-10",
        "db_type": "sqlserver",
        "backup_dir": "/backup/appdb",
        "jobs": [{"job": "database", "script": "assets/backup/x.sh",
                  "cleanup_retention": 691200, "time_window": dict(WINDOW),
                  "notify": dict(NOTIFY)}],
    }
    request.update(over)
    return request


def _restore(**over):
    request = {
        "restore_id": "ACME_APPDB_TO_DRILL",
        "cleanup_retention": 86400,
        "time_window": {"from_hour": 2, "to_hour": 5, "repeat_interval": 72000,
                        "retry_interval": 600, "timeout": 7200},
        "notify": {"logging_on_run": {"enabled": True, "telegram_chat": "restore"},
                   "alert_on_error": {"enabled": True, "telegram_chat": "restore"}},
        "source": {"id": "ACME-192-0-2-10", "backup_share": "//192.0.2.10/SQLBK",
                   "credential_target": "192.0.2.10", "username": "svc_backup"},
        "target": {"id": "ACME-192-0-2-40-HOST", "vm_platform": "linux",
                   "credential_target": "192.0.2.40", "username": "dba_user",
                   "sql_instance": "localhost,1433", "sql_username": "sa",
                   "restore_data_dir": "/var/opt/mssql/data",
                   "vm_import_linux_path": "/opt/restore/import",
                   "vm_import_linux_log_path": "/opt/restore/import"},
        "databases": [{"source_database": "APPDB", "target_database": "APPDB_DRILL"}],
    }
    request.update(over)
    return request


def _stored(tmp_path) -> dict[str, str]:
    path = tmp_path / "encrypted_secret_text.json"
    return secret_text.load_secret_text_file(path, key=KEY) if path.exists() else {}


# --------------------------------------------------------------------------- host-only instances

def test_instance_add_registers_a_machine_with_no_database_on_it():
    """`db_type: "host"` has been a documented value since long before `instance-add`, and the
    command always accepted it. Its help listed the four engines, so every host-only record in
    this estate was hand-edited instead - which is the gap, and it was a documentation gap."""
    assert "host" in instance_admin.USAGE
    assert instance_admin.HOST_ONLY_DB_TYPE == "host"


def test_a_host_record_takes_no_port_and_no_database_credential(tmp_path):
    outcome = instance_admin.add_instance(
        {"server_id": "ACME-HOST-1", "ip": "192.0.2.60", "db_type": "host",
         "platform": "windows"}, data_dir=tmp_path)

    assert outcome["port"] is None
    assert outcome["credential_name"] == ""
    record = json.loads((tmp_path / "db_instances.json").read_text(encoding="utf-8"))
    assert record["db_instances"][0]["db_type"] == "host"
    # No users.json: an OS-only host has no database login, so none is invented for it.
    assert not (tmp_path / "users.json").exists()


def test_a_database_login_on_a_host_record_is_refused_and_names_the_right_command(tmp_path):
    """It would be stored where nothing reads it, and it would look configured."""
    with pytest.raises(instance_admin.InstanceAdminError, match="remote-credential-add"):
        instance_admin.add_instance(
            {"server_id": "ACME-HOST-2", "ip": "192.0.2.61", "db_type": "host",
             "username": "sa", "password": "x"}, data_dir=tmp_path, key=KEY)


# ------------------------------------------------------------------------------------ OS logins

def test_an_os_login_is_written_where_resolve_cmd_credential_looks_for_it(tmp_path):
    """The proof that matters: what the command wrote is what the collector reads back."""
    instance_admin.add_instance(
        {"server_id": "ACME-HOST-3", "ip": "192.0.2.62", "db_type": "host",
         "platform": "linux"}, data_dir=tmp_path)

    outcome = remote_credential_admin.add_remote_credential(
        {"server_id": "ACME-HOST-3", "username": "deploy", "password": "s3cr3t",
         "method": "ssh", "auth_type": "password"}, data_dir=tmp_path, key=KEY)

    assert outcome["cmd_access_written"] is True
    assert outcome["host"] == "192.0.2.62", "the host came from the inventory record, not retyped"

    record = json.loads((tmp_path / "db_instances.json").read_text(encoding="utf-8"))
    instance = record["db_instances"][0]
    groups = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))["remote_credentials"]
    block = cmd_access.resolve_cmd_access(instance, platform="linux", host=instance["ip"])
    resolved = cmd_access.resolve_cmd_credential(block, groups)

    assert resolved is not None, "the credential the block names must be findable"
    assert resolved["username"] == "deploy"
    assert _stored(tmp_path)[resolved["password_ref"]] == "s3cr3t"


def test_the_platform_goes_on_the_record_and_never_inside_the_block(tmp_path):
    """`resolve_cmd_access` writes platform into the resolved block from the record. A copy in the
    raw block is either redundant or a second, disagreeing answer."""
    instance_admin.add_instance({"server_id": "ACME-HOST-4", "ip": "192.0.2.63",
                                 "db_type": "host"}, data_dir=tmp_path)
    remote_credential_admin.add_remote_credential(
        {"server_id": "ACME-HOST-4", "username": "admin", "password": "p",
         "method": "winrm", "platform": "windows"}, data_dir=tmp_path, key=KEY)

    instance = json.loads(
        (tmp_path / "db_instances.json").read_text(encoding="utf-8"))["db_instances"][0]

    assert instance["platform"] == "windows"
    assert "platform" not in instance["cmd_access"]
    assert instance["cmd_access"]["shell"] == "powershell"
    assert instance["cmd_access"]["port"] == 5985


def test_method_local_on_a_remote_host_is_refused(tmp_path):
    """It runs inside the db_ops container and reports the container's CPU under the host's name.
    A wrong answer is worse than a failure, because nothing about it looks wrong."""
    instance_admin.add_instance({"server_id": "ACME-HOST-5", "ip": "192.0.2.64",
                                 "db_type": "host"}, data_dir=tmp_path)

    with pytest.raises(remote_credential_admin.InstanceAdminError, match="container"):
        remote_credential_admin.add_remote_credential(
            {"server_id": "ACME-HOST-5", "username": "u", "password": "p", "method": "local"},
            data_dir=tmp_path, key=KEY)


def test_an_ssh_password_without_an_explicit_auth_type_is_refused(tmp_path):
    """auth_type defaults to `key`, and a key-auth block resolves to NO credential - so the
    password would be encrypted, stored, and never read by anything."""
    instance_admin.add_instance({"server_id": "ACME-HOST-6", "ip": "192.0.2.65",
                                 "db_type": "host"}, data_dir=tmp_path)

    with pytest.raises(remote_credential_admin.InstanceAdminError, match="auth_type"):
        remote_credential_admin.add_remote_credential(
            {"server_id": "ACME-HOST-6", "username": "u", "password": "p", "method": "ssh"},
            data_dir=tmp_path, key=KEY)


def test_a_second_login_on_one_machine_does_not_delete_the_first(tmp_path):
    """A machine legitimately carries several OS logins - an admin account and a service account.
    Replacing the group to add one would take the others with it."""
    instance_admin.add_instance({"server_id": "ACME-HOST-7", "ip": "192.0.2.66",
                                 "db_type": "host"}, data_dir=tmp_path)
    for user in ("admin", "svc_monitor"):
        remote_credential_admin.add_remote_credential(
            {"server_id": "ACME-HOST-7", "username": user, "password": "p"},
            data_dir=tmp_path, key=KEY)

    groups = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))["remote_credentials"]

    assert len(groups) == 1
    assert sorted(c["username"] for c in groups[0]["credentials"]) == ["admin", "svc_monitor"]


def test_registering_the_same_login_twice_is_refused_without_replace(tmp_path):
    instance_admin.add_instance({"server_id": "ACME-HOST-8", "ip": "192.0.2.67",
                                 "db_type": "host"}, data_dir=tmp_path)
    request = {"server_id": "ACME-HOST-8", "username": "admin", "password": "p"}
    remote_credential_admin.add_remote_credential(request, data_dir=tmp_path, key=KEY)

    with pytest.raises(remote_credential_admin.InstanceAdminError, match="replace"):
        remote_credential_admin.add_remote_credential(request, data_dir=tmp_path, key=KEY)

    outcome = remote_credential_admin.add_remote_credential(
        {**request, "password": "new", "replace": True}, data_dir=tmp_path, key=KEY)
    assert outcome["replaced"] is True
    assert _stored(tmp_path)[outcome["password_ref"]] == "new"


def test_a_credential_without_a_method_says_that_nothing_reaches_the_host_yet(tmp_path):
    """Half a configuration that reports success is how a target ends up registered and silent."""
    instance_admin.add_instance({"server_id": "ACME-HOST-9", "ip": "192.0.2.68",
                                 "db_type": "host"}, data_dir=tmp_path)

    outcome = remote_credential_admin.add_remote_credential(
        {"server_id": "ACME-HOST-9", "username": "admin", "password": "p"},
        data_dir=tmp_path, key=KEY)

    assert outcome["cmd_access_written"] is False
    assert any("nothing reaches this host yet" in line for line in outcome["next"])


def test_a_password_with_no_passphrase_writes_nothing_at_all(tmp_path):
    instance_admin.add_instance({"server_id": "ACME-HOST-10", "ip": "192.0.2.69",
                                 "db_type": "host"}, data_dir=tmp_path)

    with pytest.raises(remote_credential_admin.InstanceAdminError, match="passphrase"):
        remote_credential_admin.add_remote_credential(
            {"server_id": "ACME-HOST-10", "username": "u", "password": "p"},
            data_dir=tmp_path, key=None)

    assert not (tmp_path / "users.json").exists()


# ------------------------------------------------------------------------------- backup entries

def test_a_backup_entry_is_registered_and_loads_back_as_jobs(tmp_path):
    outcome = registration.add_backup(_backup(), data_dir=tmp_path, key=KEY)

    assert outcome["jobs_loaded"] == 1
    jobs = load_backup_jobs(tmp_path / "restore_config.json")
    assert [(job.backup_id, job.job) for job in jobs] == [("ACME_APPDB_FULL", "database")]


def test_the_secret_a_backup_script_reads_never_reaches_the_disk_in_the_clear(tmp_path):
    """`env_secrets` was the one field that could only be filled by writing a password into
    secrets/secret_text.json and running encrypt-secret. That is the step deleting the file
    afterwards does not undo."""
    outcome = registration.add_backup(
        _backup(env_secret_values={"BACKUP_ENCRYPTION_PASSWORD": "cert-pass"}),
        data_dir=tmp_path, key=KEY)

    document = json.loads((tmp_path / "restore_config.json").read_text(encoding="utf-8"))
    entry = document["backup_restore"]["backups"][0]
    ref = entry["env_secrets"]["BACKUP_ENCRYPTION_PASSWORD"]

    assert outcome["secrets_stored"] == [ref]
    assert "cert-pass" not in (tmp_path / "restore_config.json").read_text(encoding="utf-8")
    assert _stored(tmp_path)[ref] == "cert-pass"
    assert not (tmp_path / "secrets" / "secret_text.json").exists()


def test_a_job_without_cleanup_retention_is_refused_in_the_loader_s_own_words(tmp_path):
    """It was made mandatory on 2026-09-11 because six of fourteen restore entries silently
    carried none. This command will not supply it either."""
    with pytest.raises(registration.RegistrationError, match="cleanup_retention"):
        registration.add_backup(
            _backup(jobs=[{"job": "database", "script": "x.sh", "time_window": dict(WINDOW),
                           "notify": dict(NOTIFY)}]),
            data_dir=tmp_path, key=KEY)

    assert not (tmp_path / "restore_config.json").exists()


def test_a_backup_job_without_a_time_window_is_refused(tmp_path):
    """Backup jobs and restore entries share `schedule.is_due`, where a unit of work carrying no
    window gets an always-open window and DEFAULT_REPEAT_SECONDS - 300. So "no time_window" reads
    as unscheduled and means every five minutes, which for a full backup is what it was for the
    restore that ran back to back for 36 minutes at a time on 2026-09-14."""
    request = _backup()
    del request["jobs"][0]["time_window"]

    with pytest.raises(registration.RegistrationError, match="time_window"):
        registration.add_backup(request, data_dir=tmp_path, key=KEY)

    assert not (tmp_path / "restore_config.json").exists()


def test_the_refusal_names_the_job_that_lacks_one(tmp_path):
    """An entry carries several jobs — full, log, archivelog — and "a job needs a time_window"
    sends the reader through all of them."""
    request = _backup()
    request["jobs"] = [
        dict(request["jobs"][0]),
        {"job": "log", "script": "assets/backup/log.sh", "cleanup_retention": 172800,
         "notify": dict(NOTIFY)},
    ]

    with pytest.raises(registration.RegistrationError) as caught:
        registration.add_backup(request, data_dir=tmp_path, key=KEY)

    message = str(caught.value)
    assert "log" in message and "repeat_interval" in message


def test_an_entry_the_scheduled_app_would_reject_is_never_written(tmp_path):
    """The validation is `load_backup_jobs` itself, so a refusal here is the sentence the daemon
    would have failed with at 01:00 - and a second schema kept in this module cannot drift from
    the first, because there is no second schema."""
    registration.add_backup(_backup(), data_dir=tmp_path, key=KEY)
    before = (tmp_path / "restore_config.json").read_text(encoding="utf-8")

    with pytest.raises(registration.RegistrationError, match="refused by the loader"):
        registration.add_backup(
            _backup(backup_id="BROKEN",
                    jobs=[{"job": "database", "cleanup_retention": 1,
                           "time_window": dict(WINDOW), "notify": dict(NOTIFY)}]),
            data_dir=tmp_path, key=KEY)

    assert (tmp_path / "restore_config.json").read_text(encoding="utf-8") == before
    assert not (tmp_path / "restore_config.json.candidate").exists()


def test_no_secret_is_stored_for_an_entry_that_is_then_refused(tmp_path):
    """Otherwise it sits in the encrypted store under a ref no config mentions - invisible, and
    indistinguishable from a live one."""
    with pytest.raises(registration.RegistrationError):
        registration.add_backup(
            _backup(jobs=[{"job": "database", "script": "x.sh"}],
                    env_secret_values={"BACKUP_ENCRYPTION_PASSWORD": "cert-pass"}),
            data_dir=tmp_path, key=KEY)

    assert _stored(tmp_path) == {}


def test_a_backup_id_is_not_silently_overwritten(tmp_path):
    registration.add_backup(_backup(), data_dir=tmp_path, key=KEY)

    with pytest.raises(registration.RegistrationError, match="replace"):
        registration.add_backup(_backup(), data_dir=tmp_path, key=KEY)

    outcome = registration.add_backup(_backup(backup_dir="/backup/moved", replace=True),
                                      data_dir=tmp_path, key=KEY)
    assert outcome["replaced"] is True
    document = json.loads((tmp_path / "restore_config.json").read_text(encoding="utf-8"))
    assert len(document["backup_restore"]["backups"]) == 1
    assert document["backup_restore"]["backups"][0]["backup_dir"] == "/backup/moved"


def test_db_type_is_taken_from_the_inventory_rather_than_retyped(tmp_path):
    instance_admin.add_instance({"server_id": "ACME-192-0-2-10", "ip": "192.0.2.10",
                                 "db_type": "postgresql", "db_name": "postgres"},
                                data_dir=tmp_path)

    outcome = registration.add_backup({k: v for k, v in _backup().items() if k != "db_type"},
                                      data_dir=tmp_path, key=KEY)

    assert outcome["db_type"] == "postgresql"


# ------------------------------------------------------------------------------ restore entries

def test_a_restore_entry_is_registered_and_loads_back(tmp_path):
    outcome = registration.add_restore(_restore(), data_dir=tmp_path, key=KEY)

    assert outcome["loaded"] is True
    assert outcome["databases"] == 1


def test_a_restore_without_cleanup_retention_is_refused(tmp_path):
    request = _restore()
    del request["cleanup_retention"]

    with pytest.raises(registration.RegistrationError, match="cleanup_retention"):
        registration.add_restore(request, data_dir=tmp_path, key=KEY)

    assert not (tmp_path / "restore_config.json").exists()


def test_a_restore_without_a_time_window_is_refused(tmp_path):
    """Absent, it is not "unscheduled" - it is due on every pass of APP-BACKUP-RESTORE, which
    runs every 300 seconds. A restore that takes longer than that then runs back to back.

    Measured 2026-09-14 on an entry registered here without one: a 183 GB database restored for
    36 minutes, finished, and started again four seconds later, costing the target about 5 GB of
    free disk per cycle. Every run reported success, so nothing in the logs read as wrong.
    """
    request = _restore()
    del request["time_window"]

    with pytest.raises(registration.RegistrationError, match="time_window"):
        registration.add_restore(request, data_dir=tmp_path, key=KEY)

    assert not (tmp_path / "restore_config.json").exists()


def test_the_refusal_shows_a_window_that_can_be_pasted_in(tmp_path):
    """A required field whose shape is five keys is only required in name if the message does not
    say what one looks like."""
    request = _restore()
    request["time_window"] = {}

    with pytest.raises(registration.RegistrationError) as caught:
        registration.add_restore(request, data_dir=tmp_path, key=KEY)

    assert "repeat_interval" in str(caught.value) and "from_hour" in str(caught.value)


def test_the_three_inline_passwords_become_refs_and_the_values_go_to_the_store(tmp_path):
    request = _restore()
    request["source"]["password"] = "share-pass"
    request["target"]["password"] = "os-pass"
    request["target"]["sql_password"] = "sa-pass"

    outcome = registration.add_restore(request, data_dir=tmp_path, key=KEY)

    text = (tmp_path / "restore_config.json").read_text(encoding="utf-8")
    document = json.loads(text)
    entry = document["backup_restore"]["restores"][0]
    stored = _stored(tmp_path)

    assert len(outcome["secrets_stored"]) == 3
    for value in ("share-pass", "os-pass", "sa-pass"):
        assert value not in text
    assert stored[entry["source"]["password_env"]] == "share-pass"
    assert stored[entry["target"]["password_env"]] == "os-pass"
    assert stored[entry["target"]["sql_password_env"]] == "sa-pass"
    assert "password" not in entry["source"] and "sql_password" not in entry["target"]


def test_a_restore_missing_a_field_says_which_field_instead_of_quoting_a_dict_key(tmp_path):
    """The refusal comes through the parser itself, which is the point of validating with the real
    loader: `parse_restore_config` was fixed on the same day to name every missing field and the
    entry it belongs to, so this command inherited a better message without owning one."""
    request = _restore()
    del request["target"]["vm_import_linux_path"]
    del request["target"]["vm_import_linux_log_path"]

    with pytest.raises(registration.RegistrationError) as caught:
        registration.add_restore(request, data_dir=tmp_path, key=KEY)

    message = str(caught.value)
    assert "missing required field(s)" in message
    assert "ACME_APPDB_TO_DRILL" in message
    # The field an operator types, not only the key the code reads.
    assert "target.vm_import_linux_path" in message
    assert not (tmp_path / "restore_config.json").exists()


def test_a_value_and_a_ref_for_the_same_secret_is_refused(tmp_path):
    request = _restore()
    request["source"]["password"] = "p"
    request["source"]["password_env"] = "SOME_EXISTING_REF"

    with pytest.raises(registration.RegistrationError, match="not both"):
        registration.add_restore(request, data_dir=tmp_path, key=KEY)


def test_registering_a_restore_leaves_an_existing_backup_entry_alone(tmp_path):
    """Both halves live in one file, and one command writing it must not drop the other's work."""
    registration.add_backup(_backup(), data_dir=tmp_path, key=KEY)
    registration.add_restore(_restore(), data_dir=tmp_path, key=KEY)

    document = json.loads((tmp_path / "restore_config.json").read_text(encoding="utf-8"))

    assert [b["backup_id"] for b in document["backup_restore"]["backups"]] == ["ACME_APPDB_FULL"]
    assert [r["restore_id"] for r in document["backup_restore"]["restores"]] \
        == ["ACME_APPDB_TO_DRILL"]


# ------------------------------------------------------------------- the shared request reader

def test_the_json_request_reader_takes_the_three_forms_one_way(tmp_path):
    """It moved to `lib.json_io` so the app CLIs could take the same contract as the `common`
    ones: an app may not import `common`, which is why `backup-add` could not reuse the reader
    that was private to `common/cli.py`."""
    path = tmp_path / "request.json"
    path.write_text('{"from": "file"}', encoding="utf-8")

    assert json_io.read_json_request('{"inline": true}') == {"inline": True}
    assert json_io.read_json_request(f"@{path}") == {"from": "file"}

    with pytest.raises(FileNotFoundError, match="Request file not found"):
        json_io.read_json_request(f"@{tmp_path / 'missing.json'}")
    with pytest.raises(ValueError, match="not valid JSON"):
        json_io.read_json_request("{oops")
    with pytest.raises(ValueError, match="must be a JSON object"):
        json_io.read_json_request("[]")


def test_check_credentials_does_not_demand_a_database_login_from_a_host(tmp_path):
    """A `db_type: "host"` record has no database, so it has no database credential, and the one
    command whose whole value is being believed must not call that a problem.

    Found 2026-09-14 by standing a 0.17.0 node up: `check-credentials` reported four problems on a
    correct inventory. The skip was written as `if not target.db_type`, which was right while a
    host carried `null` and silently wrong the day those records were normalised to `"host"` — the
    spelling this release documents and `instance-add` now writes. Two layers recognising the same
    thing by two different tests, which is why the answer moved into `lib.sql_access`.
    """
    from db_ops.lib import sql_access

    assert sql_access.is_host_only("host") is True
    assert sql_access.is_host_only(None) is True, "the older spelling still reads as a host"
    assert sql_access.is_host_only("") is True
    assert sql_access.is_host_only("  HOST  ") is True
    for engine in sql_access.KNOWN_DB_TYPES:
        assert sql_access.is_host_only(engine) is False, engine


def test_the_host_only_db_type_has_one_definition():
    """It had two, briefly, and the second was the one that decided whether a host was checked."""
    from db_ops.common import instance_admin
    from db_ops.lib import sql_access

    assert instance_admin.HOST_ONLY_DB_TYPE is sql_access.HOST_ONLY_DB_TYPE


def test_instance_add_keeps_a_server_s_other_database_logins(tmp_path):
    """A server legitimately carries more than one database login, and four of this estate's do:
    a monitor account beside a DBA account, or `sys` beside an application user.

    `instance-add` replaced the whole `database_credentials` group to add one, so the others went
    with it - visible only later as a target resolving to the wrong login or to none. Measured
    2026-09-14 by standing a node up one `instance-add` at a time: the master's 38 groups came
    back as 34, and one instance's `default_credential_name` pointed at a credential that was no
    longer in the file. `remote-credential-add` was written with this rule from the start; this is
    the same rule, in the command that came first.
    """
    for username, name in (("monitor", "sqlserver_S1_monitor"), ("dba", "sqlserver_S1_dba")):
        instance_admin.add_instance(
            {"server_id": "S1", "db_type": "sqlserver", "ip": "192.0.2.1",
             "username": username, "password_ref": f"REF_{username.upper()}",
             "credential_name": name, "replace": True}, data_dir=tmp_path)

    groups = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))
    groups = groups["database_credentials"]

    assert len(groups) == 1, "one server is one group"
    assert [c["credential_name"] for c in groups[0]["credentials"]] == [
        "sqlserver_S1_monitor", "sqlserver_S1_dba"]


def test_re_adding_the_same_credential_replaces_only_that_one(tmp_path):
    """The counterweight: `replace` must still mean replace, not accumulate duplicates."""
    for password_ref in ("REF_OLD", "REF_NEW"):
        instance_admin.add_instance(
            {"server_id": "S1", "db_type": "sqlserver", "ip": "192.0.2.1",
             "username": "monitor", "password_ref": password_ref,
             "credential_name": "sqlserver_S1_monitor", "replace": True}, data_dir=tmp_path)

    credentials = json.loads(
        (tmp_path / "users.json").read_text(encoding="utf-8"))["database_credentials"][0]["credentials"]

    assert len(credentials) == 1
    assert credentials[0]["password_ref"] == "REF_NEW"


# ------------------------------------------------------- the database a non-SQL-Server target uses

def test_a_postgres_target_without_a_database_is_refused(tmp_path):
    """Every engine but SQL Server connects to a NAMED database, and a record that names none
    falls back to `service_name or instance_name or server_name or server_id` — a label, or the
    record's own name, handed to the server as a database.

    Measured 2026-09-15: a store registered with `service_name: "DBOPS-STORE"` and no database
    failed every collection with `database "DBOPS-STORE" does not exist`. That message names the
    label, so it reads as a missing database rather than as a field used for the wrong thing —
    and `instance-add`'s own help says service_name is a LABEL and not a database.
    """
    request = {"server_id": "ACME-192-0-2-50-PG-5432", "db_type": "postgresql",
               "ip": "192.0.2.50", "service_name": "ACME-STORE"}

    with pytest.raises(instance_admin.InstanceAdminError) as caught:
        instance_admin.add_instance(request, data_dir=tmp_path)

    message = str(caught.value)
    assert "db_name" in message
    assert "ACME-STORE" in message, "the refusal quotes the label that would have been used"
    assert '"postgres"' in message, "and names the neutral database for monitoring the instance"


def test_the_refusal_falls_back_to_the_server_id_when_there_is_no_label(tmp_path):
    """With no service_name either, the database the server would be asked for is the server_id."""
    request = {"server_id": "ACME-192-0-2-50-PG-5432", "db_type": "postgresql", "ip": "192.0.2.50"}

    with pytest.raises(instance_admin.InstanceAdminError,
                       match="ACME-192-0-2-50-PG-5432"):
        instance_admin.add_instance(request, data_dir=tmp_path)


def test_a_named_database_is_accepted(tmp_path):
    outcome = instance_admin.add_instance(
        {"server_id": "ACME-192-0-2-50-PG-5432", "db_type": "postgresql", "ip": "192.0.2.50",
         "service_name": "ACME-STORE", "db_name": "acmedb"}, data_dir=tmp_path)

    assert outcome["server_id"] == "ACME-192-0-2-50-PG-5432"


def test_sqlserver_and_oracle_are_not_asked_for_one(tmp_path):
    """SQL Server collection always connects to `master` and the metric SQL does its own USE;
    Oracle connects BY service and ignores the database. Requiring it would be noise on both."""
    for db_type, ip in (("sqlserver", "192.0.2.51"), ("oracle", "192.0.2.52")):
        outcome = instance_admin.add_instance(
            {"server_id": f"ACME-{db_type.upper()}", "db_type": db_type, "ip": ip,
             "service_name": "A LABEL"}, data_dir=tmp_path)
        assert outcome["server_id"] == f"ACME-{db_type.upper()}"


def test_a_cmd_access_block_that_is_deliberately_off_stays_off(tmp_path):
    """`enabled: false` is a decision, and re-registering the login is not a reversal of it.

    Two Windows hosts on this estate carry it because their WinRM auth fails. Rebuilding a node
    one command at a time on 2026-09-15 turned both back on — they were the only two records in 44
    that differed from the node being reproduced, and the effect would have been two failing
    collectors in every scan, on a node whose whole purpose was to show a clean one.
    """
    instance_admin.add_instance(
        {"server_id": "ACME-192-0-2-60", "db_type": "host", "ip": "192.0.2.60",
         "platform": "windows",
         "cmd_access": {"enabled": False, "method": "winrm", "host": "192.0.2.60",
                        "port": 5985, "credential_name": "remote_2.60_admin"}},
        data_dir=tmp_path)

    remote_credential_admin.add_remote_credential(
        {"server_id": "ACME-192-0-2-60", "username": "admin", "password": "pw",
         "credential_name": "remote_2.60_admin", "method": "winrm", "platform": "windows",
         "replace": True},
        data_dir=tmp_path, key=KEY)

    record = next(r for r in json.loads(
        (tmp_path / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]
        if r["server_id"] == "ACME-192-0-2-60")
    assert record["cmd_access"]["enabled"] is False
    assert record["cmd_access"]["credential_name"] == "remote_2.60_admin", (
        "the rest of the block is still rewritten; only the decision is kept")


def test_a_first_cmd_access_block_is_on(tmp_path):
    """Defaulting a NEW block to off would write access nothing reaches."""
    instance_admin.add_instance(
        {"server_id": "ACME-192-0-2-61", "db_type": "host", "ip": "192.0.2.61",
         "platform": "linux"}, data_dir=tmp_path)

    remote_credential_admin.add_remote_credential(
        {"server_id": "ACME-192-0-2-61", "username": "dev", "password": "pw",
         "auth_type": "password", "method": "ssh", "platform": "linux"},
        data_dir=tmp_path, key=KEY)

    record = next(r for r in json.loads(
        (tmp_path / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]
        if r["server_id"] == "ACME-192-0-2-61")
    assert record["cmd_access"]["enabled"] is True


def test_enabled_true_still_turns_one_back_on_when_asked(tmp_path):
    """Preserving the decision is not refusing to change it."""
    instance_admin.add_instance(
        {"server_id": "ACME-192-0-2-62", "db_type": "host", "ip": "192.0.2.62",
         "platform": "linux",
         "cmd_access": {"enabled": False, "method": "ssh", "host": "192.0.2.62",
                        "port": 22, "credential_name": "remote_2.62_dev"}},
        data_dir=tmp_path)

    remote_credential_admin.add_remote_credential(
        {"server_id": "ACME-192-0-2-62", "username": "dev", "password": "pw",
         "auth_type": "password", "method": "ssh", "platform": "linux",
         "credential_name": "remote_2.62_dev", "enabled": True, "replace": True},
        data_dir=tmp_path, key=KEY)

    record = next(r for r in json.loads(
        (tmp_path / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]
        if r["server_id"] == "ACME-192-0-2-62")
    assert record["cmd_access"]["enabled"] is True


def test_a_restore_without_a_notify_object_is_refused(tmp_path):
    """Absent, it still notifies — and that is the trap. The loader defaults it ON, deliberately,
    but to the NEUTRAL `logging` and `error` levels rather than the entry's own chat.

    Measured 2026-09-15: a restore registered here posted "Restore workflow started" into the Logs
    group while the operator watched the Restore group and reported that no message was sent at
    all. Both messages were queued and delivered. Every hand-written entry on this estate routes to
    its own chat; only the ones this command made did not, because the field was never offered.
    """
    request = _restore()
    del request["notify"]

    with pytest.raises(registration.RegistrationError) as caught:
        registration.add_restore(request, data_dir=tmp_path, key=KEY)

    message = str(caught.value)
    assert "notify" in message
    assert "logging" in message and "error" in message, "the refusal names where it would go"
    assert not (tmp_path / "restore_config.json").exists()


def test_a_backup_job_without_a_notify_object_is_refused(tmp_path):
    request = _backup()
    del request["jobs"][0]["notify"]

    with pytest.raises(registration.RegistrationError, match="notify"):
        registration.add_backup(request, data_dir=tmp_path, key=KEY)


def test_one_notify_on_the_entry_covers_every_job(tmp_path):
    """An entry's jobs usually go to one chat, and repeating the object per job is noise."""
    request = _backup(notify=dict(NOTIFY))
    del request["jobs"][0]["notify"]

    outcome = registration.add_backup(request, data_dir=tmp_path, key=KEY)

    assert outcome["backup_id"] == "ACME_APPDB_FULL"
