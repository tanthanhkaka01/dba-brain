"""The two ways the backup_restore CLI lied about a perfectly correct command line.

Both surfaced while diagnosing a real restore failure on 2026-08-02, and both cost more time than
the failure itself did — a wrong diagnostic sends someone looking for a problem that is not there.

* ``--config`` and ``--key-base64`` are registered on the top-level parser **and** on the shared
  ``parents=[config_parent]`` parser every subcommand inherits. argparse applies a subparser's
  defaults after the top-level parse, so passing either flag *before* the subcommand had it
  silently overwritten with ``None``: the run fell back to ``config.json`` and died with "No
  decryption key provided".
* ``restore-workflow --restore-id X`` reported "No backup_restore entry found" for an entry that
  is present in ``restore_config.json`` — it filters only the SQL Server engine list, and a
  script-driven entry is not in it. ``workflow.py`` documents that exact trap; the CLI fell into it.
"""

from __future__ import annotations

import json

import pytest

from db_ops.backup_restore.cli import main, parse_args


# --------------------------------------------------------------------------- #
# Flags given before the subcommand must survive the subparse
# --------------------------------------------------------------------------- #
def test_a_key_given_before_the_subcommand_is_not_erased_by_it():
    args = parse_args(["--key-base64", "c2VjcmV0", "workflow"])

    assert getattr(args, "key_base64", None) == "c2VjcmV0"


def test_a_config_given_before_the_subcommand_is_not_erased_by_it():
    args = parse_args(["--config", "config.custom.json", "workflow"])

    assert getattr(args, "config", None) == "config.custom.json"


def test_the_same_flags_still_work_after_the_subcommand():
    """The inherited copy is what makes the trailing form work; suppressing its *default* must
    not suppress the flag itself."""
    args = parse_args(["workflow", "--config", "after.json", "--key-base64", "c2VjcmV0"])

    assert getattr(args, "config", None) == "after.json"
    assert getattr(args, "key_base64", None) == "c2VjcmV0"


def test_an_omitted_flag_reads_as_none_rather_than_raising():
    """SUPPRESS leaves the attribute unset, so every reader has to use getattr with a default.
    This is the test that fails if someone reintroduces a bare ``args.key``."""
    args = parse_args(["workflow"])

    assert getattr(args, "key", None) is None
    assert getattr(args, "key_base64", None) is None
    assert getattr(args, "config", None) is None


def test_the_plain_key_flag_gets_the_same_treatment():
    assert getattr(parse_args(["--key", "s3cret", "workflow"]), "key", None) == "s3cret"


# --------------------------------------------------------------------------- #
# A script-driven entry exists — say so, and say what runs it
# --------------------------------------------------------------------------- #
def _config_with_script_restore(tmp_path):
    config = tmp_path / "restore_config.json"
    config.write_text(json.dumps({"backup_restore": {"restores": [{
        "cleanup_retention": 691200, "restore_id": "CLOUD_MSSQL_TO_CLOUD2",
        "active": True,
        "db_type": "sqlserver",
        "server_id": "SRC",
        "target_server_id": "DST",
        "target_container": "c",
        "backup_dir": "/var/opt/mssql/backup/dbops",
        "source_backup_host_dir": "/opt/backup/dbops",
        "target_backup_dir": "/opt/db_ops/stage",
        "script": "assets/restore/sqlserver/mssql_restore.sh",
        "env": {"MSSQL_USER": "sa"},
    }]}}), encoding="utf-8")
    return config


def test_restore_workflow_runs_a_script_driven_entry_on_demand(tmp_path, monkeypatch, capsys):
    """`/spbot_restore` runs `restore-workflow --restore-id`. It used to refuse a script-driven
    entry and name a CLI command instead, so no PostgreSQL, Oracle or container SQL Server drill
    could be started from the bot (1.35, 2026-09-24). It runs it now, by the scheduler's runner,
    forced and whether or not the entry is active, and prints the line the bot looks for."""
    import db_ops.backup_restore.workflow as workflow

    config = _config_with_script_restore(tmp_path)
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return {"ran": 1, "succeeded": 1, "failed": 0, "restores": []}

    monkeypatch.setattr(workflow, "run_scheduled_restores", fake_run)
    exit_code = main(["--config", str(config), "restore-workflow",
                      "--restore-id", "CLOUD_MSSQL_TO_CLOUD2"])

    assert exit_code == 0
    assert (seen["restore_id"], seen["force"], seen["on_demand"]) == ("CLOUD_MSSQL_TO_CLOUD2", True, True)
    assert "restore-workflow completed status=SUCCESS restore_id=CLOUD_MSSQL_TO_CLOUD2" in capsys.readouterr().out


def test_a_script_driven_entry_that_fails_on_demand_exits_non_zero(tmp_path, monkeypatch, capsys):
    import db_ops.backup_restore.workflow as workflow

    config = _config_with_script_restore(tmp_path)
    monkeypatch.setattr(workflow, "run_scheduled_restores",
                        lambda **kw: {"ran": 1, "succeeded": 0, "failed": 1, "restores": []})

    assert main(["--config", str(config), "restore-workflow", "--restore-id", "CLOUD_MSSQL_TO_CLOUD2"]) == 1
    assert "status=SUCCESS" not in capsys.readouterr().out


def test_an_id_that_really_does_not_exist_still_says_so(tmp_path, capsys):
    config = _config_with_script_restore(tmp_path)

    exit_code = main(["--config", str(config), "restore-workflow", "--restore-id", "NOPE"])

    message = capsys.readouterr().err
    assert exit_code != 0
    assert "No backup_restore entry found with restore_id=NOPE" in message
