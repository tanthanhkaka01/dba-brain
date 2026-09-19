"""A pointer from one config file into another is followed, so the estate does not find out at run time.

The incident, on 2026-09-19 at 09:36 local, on the container worker::

    backup_restore.backup ERROR: backup_id=ACME_STORE_PG_115
    ACME_STORE_PG_115/wal: server_id not found in db_instances.json: ACME-192-0-2-115-PG-5433

An **active** backup job had been naming a `server_id` its node's inventory did not hold, and every
cycle failed the same way. Both files loaded correctly: `restore_config.json` is valid and
`db_instances.json` is valid, and nothing in the tree compared one against the other. The master's
inventory *did* have the instance — the worker's copy predated the rename — so this is also the
shape a deploy leaves behind, which is why the check takes a data directory rather than assuming
this one.

`data/config_references.json` declares the pointers and `db_ops.lib.config_references` follows
them. The first test below is that incident, reproduced from the two files' shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.lib import config_references

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(directory: Path, name: str, payload: dict) -> None:
    (directory / name).write_text(json.dumps(payload, indent=4), encoding="utf-8")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """A data directory holding this tree's real rules and the minimum they point at."""
    directory = tmp_path / "data"
    directory.mkdir()
    (directory / config_references.FILENAME).write_bytes(
        (REPO_ROOT / "data" / config_references.FILENAME).read_bytes())
    _write(directory, "db_instances.json", {"db_instances": [
        {"server_id": "ACME-192-0-2-115-MSSQL25-1433", "enabled": True},
    ]})
    _write(directory, "restore_config.json", {"backup_restore": {"backups": [], "restores": []}})
    _write(directory, "sql_targets.json", {"sql_targets": []})
    _write(directory, "sql_commands.json", {"sql_commands": []})
    _write(directory, "users.json", {"database_credentials": [], "remote_credentials": []})
    _write(directory, "telegram_groups.json", {"telegram_groups": [
        {"notify_level": "logging", "source_notify_level": "logging"},
    ]})
    _write(directory, "telegram_config.json", {"level_chat_map": {"private": "100200300"}})
    return directory


def _backup(directory: Path, *, server_id: str, active: bool = True) -> None:
    _write(directory, "restore_config.json", {"backup_restore": {"backups": [
        {"backup_id": "ACME_STORE_PG_115", "active": active, "server_id": server_id,
         "jobs": [{"job": "wal", "active": True, "cleanup_retention": 691200}]},
    ], "restores": []}})


# --------------------------------------------------------------------------- #
# The incident
# --------------------------------------------------------------------------- #
def test_an_active_backup_naming_an_instance_that_is_not_there_is_reported(data_dir):
    _backup(data_dir, server_id="ACME-192-0-2-115-PG-5433")

    result = config_references.check(data_dir)

    assert not result["ok"]
    assert len(result["dangling"]) == 1
    finding = result["dangling"][0]
    assert finding["value"] == "ACME-192-0-2-115-PG-5433"
    assert "restore_config.json" in finding["where"]
    assert "db_instances.json" in finding["detail"]
    # The rule carries why it exists, so the reader is not left to guess what breaks.
    assert "server_id not found" in finding["why"]


def test_the_same_backup_is_fine_once_the_instance_is_in_the_inventory(data_dir):
    """The master's own state: the instance exists there, which is why `check-references` is clean
    on the master and the worker still failed. The node that runs the job is the node to check."""
    _backup(data_dir, server_id="ACME-192-0-2-115-PG-5433")
    _write(data_dir, "db_instances.json", {"db_instances": [
        {"server_id": "ACME-192-0-2-115-PG-5433", "enabled": True},
    ]})

    assert config_references.check(data_dir)["ok"]


def test_an_inactive_backup_is_listed_but_does_not_fail_the_check(data_dir):
    """Retiring a target by turning it off is normal. Failing on it would teach the reader to stop
    reading the report — but it is still listed, because the entry somebody re-enables is the next
    outage."""
    _backup(data_dir, server_id="ACME-GONE", active=False)

    result = config_references.check(data_dir)

    assert result["ok"]
    assert [item["value"] for item in result["inactive"]] == ["ACME-GONE"]


# --------------------------------------------------------------------------- #
# The other pointers, one test each
# --------------------------------------------------------------------------- #
def test_a_sql_target_pointing_at_a_credential_that_does_not_exist_is_reported(data_dir):
    _write(data_dir, "sql_targets.json", {"sql_targets": [
        {"sql_id": 1, "target_no": 1, "active": True,
         "server_id": "ACME-192-0-2-115-MSSQL25-1433",
         "credential_name": "sqlserver_typo_dba"},
    ]})
    _write(data_dir, "sql_commands.json", {"sql_commands": [{"sql_id": 1}]})
    _write(data_dir, "users.json", {"database_credentials": [
        {"server_id": "ACME-192-0-2-115-MSSQL25-1433",
         "credentials": [{"credential_name": "sqlserver_real_dba"}]},
    ], "remote_credentials": []})

    dangling = config_references.check(data_dir)["dangling"]

    assert [item["value"] for item in dangling] == ["sqlserver_typo_dba"]


def test_a_sql_target_whose_command_id_does_not_exist_is_reported(data_dir):
    """It fails silently otherwise: the scan skips the pair, so the task simply never runs and
    reads as a schedule that has not come round yet."""
    _write(data_dir, "sql_targets.json", {"sql_targets": [
        {"sql_id": 99, "target_no": 1, "active": True,
         "server_id": "ACME-192-0-2-115-MSSQL25-1433"},
    ]})
    _write(data_dir, "sql_commands.json", {"sql_commands": [{"sql_id": 1}]})

    dangling = config_references.check(data_dir)["dangling"]

    assert [item["value"] for item in dangling] == ["99"]


def test_a_cmd_access_credential_is_followed_into_the_remote_logins(data_dir):
    """`cmd_access.credential_name` is read from inside a nested object, which is why the rule's
    field is dotted. Dangling, every OS metric for that host fails on every scan."""
    _write(data_dir, "db_instances.json", {"db_instances": [
        {"server_id": "ACME-1", "enabled": True,
         "cmd_access": {"enabled": True, "method": "ssh", "auth_type": "password",
                        "credential_name": "remote_gone"}},
    ]})
    _write(data_dir, "users.json", {"database_credentials": [], "remote_credentials": [
        {"server_id": "ACME-1", "credentials": [{"credential_name": "remote_acme_dev"}]},
    ]})

    dangling = config_references.check(data_dir)["dangling"]

    assert [item["value"] for item in dangling] == ["remote_gone"]


def test_a_notify_level_may_live_in_either_file_and_both_count(data_dir):
    """`private` is wired in telegram_config.json's level_chat_map and nowhere in
    telegram_groups.json. The first run of this rule reported it as dangling; a checker whose first
    finding is a false positive does not get read a second time."""
    _write(data_dir, "sql_targets.json", {"sql_targets": [
        {"sql_id": 1, "target_no": 1, "active": True, "server_id": "ACME-192-0-2-115-MSSQL25-1433",
         "notify": {"logging_on_run": {"enabled": True, "telegram_chat": "logging"},
                    "alert_on_error": {"enabled": True, "telegram_chat": "private"}}},
    ]})
    _write(data_dir, "sql_commands.json", {"sql_commands": [{"sql_id": 1}]})

    assert config_references.check(data_dir)["ok"]


def test_a_notify_level_that_is_in_neither_file_is_reported(data_dir):
    _write(data_dir, "sql_targets.json", {"sql_targets": [
        {"sql_id": 1, "target_no": 1, "active": True, "server_id": "ACME-192-0-2-115-MSSQL25-1433",
         "notify": {"alert_on_error": {"enabled": True, "telegram_chat": "renamed_level"}}},
    ]})
    _write(data_dir, "sql_commands.json", {"sql_commands": [{"sql_id": 1}]})

    dangling = config_references.check(data_dir)["dangling"]

    assert [item["value"] for item in dangling] == ["renamed_level"]


# --------------------------------------------------------------------------- #
# The rules themselves, and this estate
# --------------------------------------------------------------------------- #
def test_every_rule_names_a_file_that_exists_and_says_why_it_is_there():
    for rule in config_references.load_rules(REPO_ROOT / "data"):
        source = rule["from"]
        targets = rule["to"] if isinstance(rule["to"], list) else [rule["to"]]
        for side in [source, *targets]:
            name = str(side["file"])
            assert (REPO_ROOT / "data" / name).is_file() or (
                REPO_ROOT / "data" / name.replace(".json", ".example.json")).is_file(), name
        assert rule.get("name"), rule
        assert str(rule.get("why") or "").strip(), (
            f"{rule.get('name')}: a rule with no reason is a rule nobody can judge when it fires")


def test_every_rule_actually_matches_something_in_this_estate():
    """A rule whose path resolves to nothing passes forever. Two of the shared-object reference's
    paths did exactly that before 2026-09-19 — `backups[]` where the value lives on
    `backups[].jobs[]` — so this asserts the pointers are being followed, not merely declared."""
    result = config_references.check(REPO_ROOT / "data")
    assert result["unreadable"] == []
    assert result["pointers_checked"] > 0


def test_this_estate_has_no_dangling_pointer():
    result = config_references.check(REPO_ROOT / "data")
    assert result["dangling"] == [], "\n".join(
        f"{item['where']} -> {item['value']}: {item['detail']}" for item in result["dangling"])
