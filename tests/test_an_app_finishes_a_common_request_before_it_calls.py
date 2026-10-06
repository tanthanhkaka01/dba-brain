"""An app finishes a ``common.cli`` request from its own ``data/`` before it calls (rules R09).

``common.cli`` reads no configuration: a command works from its request alone, so it runs by hand on
an empty node. Until 0.24.0, 22 commands took a bare ``server_id`` and looked the login, the policy
and the confirmation price up themselves. Now the app that calls does that, through
``db_ops.lib.data_sources.request_fill`` - and what it fills is exactly what the command would have
looked up, or the operation runs as someone else, on a different schedule, at a different price.

These hold the filler to that: the login the inventory names (or the one the caller chose), the
password out of the secret store, the host's own ``cmd_access``, this server's policy over the
defaults, this node's ladder before the shipped one - and never a value the caller already stated.
"""

from __future__ import annotations

import json

import pytest

from db_ops.lib.data_sources import request_fill
from db_ops.lib.data_sources.request_fill import RequestFillError, fill_request

SECRETS = {"DBA_REF": "dba-secret", "MON_REF": "monitor-secret", "OS_REF": "os-secret"}


@pytest.fixture()
def data_dir(tmp_path):
    """One SQL Server reachable over SSH, with two database logins; a host with no way in."""
    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": "LAB-10-0-0-5", "ip": "10.0.0.5", "port": 1433, "db_type": "sqlserver",
         "platform": "linux", "instance_name": "MSSQLSERVER", "database": "a-service-label",
         "default_credential_name": "lab_dba",
         "cmd_access": {"method": "ssh", "auth_type": "password", "credential_name": "lab_os"}},
        {"server_id": "LAB-NO-ACCESS", "ip": "10.0.0.9", "db_type": "postgresql", "platform": "linux",
         "port": 5432, "database_name": "postgres"},
    ]}), encoding="utf-8")
    (tmp_path / "users.json").write_text(json.dumps({
        "database_credentials": [{"server_id": "LAB-10-0-0-5", "db_type": "sqlserver", "credentials": [
            {"credential_name": "lab_dba", "username": "dba", "password_ref": "DBA_REF", "role": "DBA"},
            {"credential_name": "lab_monitor", "username": "monitor", "password_ref": "MON_REF",
             "role": "MONITOR"},
        ]}],
        "remote_credentials": [{"server_id": "LAB-10-0-0-5", "host": "10.0.0.5", "credentials": [
            {"credential_name": "lab_os", "username": "osadmin", "password_ref": "OS_REF"}]}],
    }), encoding="utf-8")
    (tmp_path / "maintenance_policy.json").write_text(json.dumps({"maintenance_policy": {
        "defaults": {"connect_timeout_seconds": 30, "restart_wait_seconds": 600},
        "servers": {"LAB-10-0-0-5": {"restart_wait_seconds": 120}},
    }}), encoding="utf-8")
    return tmp_path


def test_a_sql_command_gets_the_inventorys_own_login_with_its_password(data_dir):
    filled = fill_request("list-databases", {"target": "LAB-10-0-0-5"}, data_dir=data_dir, secrets=SECRETS)

    connection = filled["connection"]
    assert (connection["host"], connection["port"], connection["username"]) == ("10.0.0.5", 1433, "dba")
    assert connection["password"] == "dba-secret"
    assert connection["credential_name"] == "lab_dba"


def test_the_login_the_caller_chose_wins_over_the_default(data_dir):
    filled = fill_request("list-databases", {"target": "LAB-10-0-0-5", "credential_name": "lab_monitor"},
                          data_dir=data_dir, secrets=SECRETS)

    assert (filled["connection"]["username"], filled["connection"]["password"]) == ("monitor", "monitor-secret")


def test_a_sql_server_login_names_no_database_so_it_lands_in_master(data_dir):
    """The inventory's `database` on SQL Server is a service label (rules R24), never a database."""
    filled = fill_request("db-status", {"target": "LAB-10-0-0-5"}, data_dir=data_dir, secrets=SECRETS)

    assert "database" not in filled["connection"]


def test_a_host_command_gets_the_hosts_cmd_access_with_its_login_resolved(data_dir):
    filled = fill_request("host-facts", {"target": "LAB-10-0-0-5"}, data_dir=data_dir, secrets=SECRETS)

    access = filled["access"]
    assert (access["method"], access["host"], access["username"]) == ("ssh", "10.0.0.5", "osadmin")
    assert access["password"] == "os-secret"
    assert "credential_name" not in access


def test_the_servers_own_policy_is_laid_over_the_defaults(data_dir):
    filled = fill_request("host-facts", {"target": "LAB-10-0-0-5"}, data_dir=data_dir, secrets=SECRETS)

    assert filled["policy"] == {"connect_timeout_seconds": 30, "restart_wait_seconds": 120}


def test_a_node_with_no_policy_file_sends_an_empty_policy(tmp_path, data_dir):
    (data_dir / "maintenance_policy.json").unlink()

    filled = fill_request("host-facts", {"target": "LAB-10-0-0-5"}, data_dir=data_dir, secrets=SECRETS)

    assert filled["policy"] == {}


def test_the_nodes_own_ladder_prices_the_operation_before_the_shipped_one(data_dir):
    (data_dir / "emergency_operations.json").write_text(json.dumps({
        "levels": {"20": {"confirmations": 1, "challenge": ""}},
        "operations": {"kill-spid": {"level": 20, "effects": ["ends one session"]}},
    }), encoding="utf-8")

    filled = fill_request("kill-spid", {"target": "LAB-10-0-0-5", "spid": 55}, data_dir=data_dir,
                          secrets=SECRETS)

    assert filled["rules"] == {"level": 20, "confirmations": 1, "challenge": "", "effects": ["ends one session"]}


def test_an_unreadable_ladder_prices_the_operation_at_the_strictest(data_dir):
    (data_dir / "emergency_operations.json").write_text("{not json", encoding="utf-8")

    filled = fill_request("kill-spid", {"target": "LAB-10-0-0-5"}, data_dir=data_dir, secrets=SECRETS)

    assert filled["rules"]["confirmations"] == 2


def test_authorize_is_priced_by_the_operation_it_names(data_dir):
    (data_dir / "emergency_operations.json").write_text(json.dumps({
        "levels": {"30": {"confirmations": 1, "challenge": ""}},
        "operations": {"run-sql-task": {"level": 30}},
    }), encoding="utf-8")

    filled = fill_request("authorize", {"operation": "run-sql-task"}, data_dir=data_dir)

    assert filled["rules"]["level"] == 30


def test_what_the_caller_already_stated_is_never_replaced(data_dir):
    stated = {"target": "LAB-10-0-0-5", "connection": {"host": "stated"}, "access": {"host": "stated"},
              "policy": {"stated": True}, "rules": {"confirmations": 0}}

    filled = fill_request("sqlserver-apply-cu", stated, data_dir=data_dir, secrets=SECRETS)

    assert {key: filled[key] for key in stated} == stated


def test_copy_schema_fills_each_side_from_its_own_target(data_dir):
    filled = fill_request("copy-schema", {
        "source": {"target": "LAB-10-0-0-5", "schema": "s"},
        "destination": {"target": "LAB-10-0-0-5", "credential_name": "lab_monitor"},
    }, data_dir=data_dir, secrets=SECRETS)

    assert filled["source"]["connection"]["username"] == "dba"
    assert filled["destination"]["connection"]["username"] == "monitor"


def test_a_command_that_needs_nothing_is_passed_through_unchanged(data_dir):
    request = {"target": "LAB-10-0-0-5", "time_window": {"repeat_interval": 60}}

    assert fill_request("due-check", request, data_dir=data_dir) == request


def test_run_sql_gets_the_same_login_as_the_operations_do(data_dir):
    filled = fill_request("run-sql", {"target": "LAB-10-0-0-5", "sql": "select 1"}, data_dir=data_dir,
                          secrets=SECRETS)

    assert (filled["connection"]["username"], filled["connection"]["password"]) == ("dba", "dba-secret")
    assert "secrets" not in filled  # a direct target names no bridge secret


def test_an_8i_bridge_target_carries_its_signing_secret_and_nothing_else(data_dir):
    bridge = {"method": "api", "secret_ref": "BRIDGE_REF", "url": "http://bridge"}

    filled = fill_request("run-sql", {"target": "LAB-10-0-0-5", "sql": "select 1", "sql_access": bridge},
                          data_dir=data_dir, secrets={**SECRETS, "BRIDGE_REF": "sign-me"})

    assert filled["secrets"] == {"BRIDGE_REF": "sign-me"}


def test_run_cmd_and_a_file_transfer_get_the_hosts_login(data_dir):
    for command in ("run-cmd", "fetch-file", "send-file", "pack-files"):
        filled = fill_request(command, {"target": "LAB-10-0-0-5"}, data_dir=data_dir, secrets=SECRETS)
        assert filled["access"]["username"] == "osadmin", command


def test_a_relay_finishes_each_side_from_its_own_target(data_dir):
    filled = fill_request("relay-file", {"source": {"target": "LAB-10-0-0-5", "path": "/a"},
                                         "destination": {"target": "10.0.0.5", "path": "/b"}},
                          data_dir=data_dir, secrets=SECRETS)

    assert filled["source"]["access"]["host"] == filled["destination"]["access"]["host"] == "10.0.0.5"


def test_probe_host_gets_an_address_and_no_login(data_dir):
    filled = fill_request("probe-host", {"target": "LAB-10-0-0-5"}, data_dir=data_dir)

    assert (filled["host"], filled["port"], filled["server_id"]) == ("10.0.0.5", 1433, "LAB-10-0-0-5")
    assert "password" not in json.dumps(filled)


def test_a_request_with_no_target_to_fill_from_is_refused_by_name(data_dir):
    with pytest.raises(RequestFillError, match="names no target"):
        fill_request("list-databases", {}, data_dir=data_dir, secrets=SECRETS)


def test_a_host_with_no_way_in_is_refused_with_the_fix(data_dir):
    with pytest.raises(RequestFillError, match="cmd_access"):
        fill_request("host-facts", {"target": "LAB-NO-ACCESS"}, data_dir=data_dir, secrets=SECRETS)


def test_every_command_that_left_the_lookup_list_is_one_the_filler_knows():
    """Every operation that took a bare server_id until 0.24.0 - each must be finishable by an app."""
    left = {"authorize", "sqlserver-export-instance", "sqlserver-replay-instance", "host-facts",
            "host-service", "host-restart", "shrink-log", "kill-spid", "start-job", "disable-job",
            "sqlserver-precheck", "sqlserver-apply-cu", "sqlserver-verify-build", "list-databases",
            "list-schemas", "list-jobs", "db-status", "create-table-from-xlsx", "copy-schema",
            "trace-session",
            # and the four doors that still resolved a bare server_id until 0.24.0
            "run-sql", "run-cmd", "probe-host", "fetch-file", "send-file", "pack-files", "relay-file"}

    assert left <= set(request_fill.FILLS)


# --------------------------------------------------------------------------- #
# Which login a server_id gets - run-sql chose it until 0.24.0, and chose it this way
# --------------------------------------------------------------------------- #
def test_an_unknown_credential_name_lists_what_exists(data_dir):
    with pytest.raises(RequestFillError, match="lab_dba, lab_monitor"):
        request_fill.sql_connection("LAB-10-0-0-5", credential_name="nope", data_dir=data_dir,
                                    secrets=SECRETS)


def test_an_instance_that_declares_no_login_is_refused_not_guessed(data_dir):
    """No name, no run. Falling back to the first entry made file order decide which login a
    production query used; an unconfigured instance must be fixed, not guessed around."""
    with pytest.raises(RequestFillError, match="No credential configured for LAB-NO-ACCESS"):
        request_fill.sql_connection("LAB-NO-ACCESS", data_dir=data_dir, secrets=SECRETS)


def test_a_store_that_cannot_be_read_is_a_sentence_naming_the_server_not_a_traceback(
        data_dir, monkeypatch):
    def no_key(data_dir=None, **_kwargs):
        raise RuntimeError("No decryption key provided. Pass --key or set DB_OPS_SECRET_KEY.")

    monkeypatch.setattr(request_fill, "load_secret_text", no_key)

    with pytest.raises(RequestFillError, match="LAB-10-0-0-5: No decryption key provided"):
        request_fill.sql_connection("LAB-10-0-0-5", data_dir=data_dir)
