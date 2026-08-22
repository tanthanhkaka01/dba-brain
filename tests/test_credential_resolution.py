"""Resolving *which login* a target runs as. One implementation in common, one rule for
every app: the credential must be named. No role guess, no file-order fallback."""

import json

import pytest

from db_ops.common import data_sources
from db_ops.common.data_sources import CredentialNotFound, find_database_credential


GROUPS = [
    {
        "server_id": "ACME-1",
        "db_type": "sqlserver",
        "service_name": "SALESDB-PROD",
        "instance_name": "SALESCLUSTER",
        "credentials": [
            {"credential_name": "readonly", "username": "monitor", "role": "READER"},
            {"credential_name": "dba", "username": "dba_user", "role": "DBA"},
        ],
    },
    {
        "server_id": "ACME-2",
        "db_type": "sqlserver",
        "credentials": [{"credential_name": "other", "username": "u2"}],
    },
]


def test_named_credential_is_returned():
    found = find_database_credential(GROUPS, server_id="ACME-1", credential_name="readonly")
    assert found["username"] == "monitor"


def test_credential_name_is_required():
    with pytest.raises(CredentialNotFound, match="No credential configured for ACME-1"):
        find_database_credential(GROUPS, server_id="ACME-1", credential_name="")


def test_a_dba_role_is_never_preferred_implicitly():
    """The old metrics rule: with no name, pick whichever entry has role DBA/SYSDBA. A config
    omission then connected as an admin login and nothing said so."""
    with pytest.raises(CredentialNotFound):
        find_database_credential(GROUPS, server_id="ACME-1", credential_name="", db_type="sqlserver")


def test_unknown_name_lists_what_the_server_has():
    with pytest.raises(CredentialNotFound, match="Available: readonly, dba"):
        find_database_credential(GROUPS, server_id="ACME-1", credential_name="nope")


def test_credentials_of_another_server_are_never_used():
    with pytest.raises(CredentialNotFound):
        find_database_credential(GROUPS, server_id="ACME-1", credential_name="other")


def test_optional_keys_narrow_only_when_supplied():
    # service_name/instance_name match case-insensitively; an empty side is not a constraint.
    assert find_database_credential(
        GROUPS, server_id="ACME-1", credential_name="dba",
        # Deliberately lowercase against an uppercase group: that IS the assertion.
        db_type="SQLSERVER", service_name="salesdb-prod", instance_name="salescluster",
    )["username"] == "dba_user"
    # A group with no service_name still matches a caller that has one (inventories differ).
    assert find_database_credential(
        GROUPS, server_id="ACME-2", credential_name="other", service_name="ANY", instance_name="ANY",
    )["username"] == "u2"


def test_wrong_db_type_does_not_match():
    with pytest.raises(CredentialNotFound):
        find_database_credential(GROUPS, server_id="ACME-1", credential_name="dba", db_type="oracle")


def test_metrics_target_without_a_credential_is_reported_not_guessed():
    """metrics keeps per-target degradation: the target still loads, with credential=None, so
    the collector reports that one target instead of aborting the whole run."""
    from db_ops.metrics import targets

    assert targets._find_credential(
        GROUPS, server_id="ACME-1", db_type="sqlserver", service_name="", instance_name="",
        credential_name="",
    ) is None
    assert targets._find_credential(
        GROUPS, server_id="ACME-1", db_type="sqlserver", service_name="", instance_name="",
        credential_name="dba",
    )["username"] == "dba_user"


def test_sql_tasks_target_uses_the_same_rule():
    import dataclasses

    from db_ops.lib.time_window import parse_time_window_config
    from db_ops.sql_tasks.runner import SqlTarget, find_database_credential as task_credential

    target = SqlTarget(
        sql_id=1, target_no=1, server_id="ACME-1", db_type="sqlserver", service_name="SALESDB-PROD",
        instance_name="SALESCLUSTER", credential_name="readonly",
        time_window=parse_time_window_config({}, context="test"), active=1, database_name="master",
    )
    assert task_credential(target, GROUPS)["username"] == "monitor"
    assert task_credential(dataclasses.replace(target, credential_name=""), GROUPS) is None


def _write_config(data_dir, *, default_credential_name="dba"):
    import json

    instance = {
        "server_id": "ACME-1", "db_type": "sqlserver", "ip": "10.0.0.1", "port": 1433,
        "service_name": "SALESDB-PROD", "instance_name": "SALESCLUSTER", "enabled": True,
        "metrics": {"enabled": True},
    }
    if default_credential_name:
        instance["default_credential_name"] = default_credential_name
    (data_dir / "db_instances.json").write_text(
        json.dumps({"db_instances": [instance]}), encoding="utf-8"
    )
    (data_dir / "users.json").write_text(
        json.dumps({"database_credentials": GROUPS}), encoding="utf-8"
    )


def test_check_credentials_cli_passes_on_a_complete_config(tmp_path, capsys):
    # Moved out of db_ops.common.cli on 2026-08-15: the command needs two apps' resolvers, and
    # the shared layer may import none. db_ops/cli.py is a root module, so it may.
    from db_ops import cli

    _write_config(tmp_path)
    assert cli.main(["check-credentials", str(tmp_path)]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["success"] is True and answer["data"]["problems"] == []
    assert "0 without a resolvable credential" in answer["message"]


def test_check_credentials_cli_fails_when_a_target_names_no_login(tmp_path, capsys):
    """The guard for the rule: dropping default_credential_name used to keep working (metrics
    picked the DBA entry). Now it must be caught before a deploy, not at 3 a.m."""
    from db_ops import cli

    _write_config(tmp_path, default_credential_name="")
    assert cli.main(["check-credentials", str(tmp_path)]) == 1
    # The finding is **in the answer** since 2026-08-16, not only in the exit code with the detail
    # on stderr — that split is what made this command unusable from a program. The exit code
    # still agrees with it, because a runbook and a scheduled caller both read `$?`.
    answer = json.loads(capsys.readouterr().out)
    assert answer["success"] is True          # the check ran; the estate is what has the problem
    assert answer["data"]["problems"] == [
        "metrics target ACME-1/sqlserver/SALESDB-PROD: no credential (default_credential_name=<unset>)"]
    assert "1 without a resolvable credential" in answer["message"]


def test_the_text_rendering_is_still_available_for_a_pasted_runbook_line(tmp_path, capsys):
    """`format: txt` keeps the old shape — problems on stderr, one summary line on stdout."""
    from db_ops import cli

    _write_config(tmp_path, default_credential_name="")
    assert cli.main(["check-credentials",
                     json.dumps({"data_dir": str(tmp_path), "format": "txt"})]) == 1
    captured = capsys.readouterr()
    assert "ACME-1/sqlserver/SALESDB-PROD: no credential" in captured.err
    assert "1 without a resolvable credential" in captured.out


def test_telegram_command_credential_uses_the_same_rule(monkeypatch):
    from db_ops.telegram import sql_commands

    monkeypatch.setattr(data_sources, "load_credentials", lambda db_type, data_dir=None: GROUPS)
    config = {"server_id": "ACME-1", "db_type": "sqlserver", "service_name": "SALESDB-PROD",
              "instance_name": "SALESCLUSTER", "credential_name": "dba"}
    assert sql_commands.find_credential(config)["username"] == "dba_user"
    with pytest.raises(CredentialNotFound):
        sql_commands.find_credential({**config, "credential_name": ""})
