"""The last step of a restore is opening the database, not the command that claimed to restore it.

`common.cli verify-restore` has said this in its own usage text since it was written: *"Each engine
has its own way of looking finished and being unusable: SQL Server left RESTORING, Oracle MOUNTED
but never opened, PostgreSQL still in recovery. All three report success at the command that put
them there."* `restore_by_id` plans it as its last step for all three engines and raises when it
fails.

The scheduled `restore-workflow` — the one that runs nightly — never called it. So the check was
written, tested, documented and skipped by the path that needed it, and on 2026-09-15 a drill
announced `status=done` over a database `Msg 5149` had left mid-restore.

These tests cover the request built for it, which is where the two quiet mistakes live: asking the
target about the *source's* database name, and treating an entry with no credentials as a failure.
"""

from __future__ import annotations

from types import SimpleNamespace

from db_ops.backup_restore.cli import (
    _restored_databases,
    _verify_plan,
    _verify_request,
    _verify_targets,
)
from db_ops.backup_restore.config import DatabaseRestoreMapping


def _mapping(source: str, restored: str = "") -> DatabaseRestoreMapping:
    """The REAL mapping class, not a stand-in. A SimpleNamespace here carried the attribute names
    the code asked for rather than the ones the parser produces, so the suite passed while every
    engine restore's verification was skipped for want of a single name."""
    return DatabaseRestoreMapping(source_database=source, target_database=restored or source)


def _config(**kwargs) -> SimpleNamespace:
    base = {
        "vm_credential_target": "192.0.2.10",
        "restore_sql_username": "sa",
        "restore_sql_password_env": "TARGET_SA",
        "databases": [_mapping("SALES", "SALES_STG")],
        "restore_id": "DRILL",
        "target_id": "TGT",
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_the_target_is_asked_about_the_name_it_was_told_to_create() -> None:
    """A drill commonly restores `SALES` as `SALES_STG`. Asking the target about `SALES` asks it
    about a database it was never asked to create, and reports a working drill as broken."""
    assert _verify_targets(_config()) == ["SALES_STG"]


def test_an_entry_that_does_not_rename_keeps_the_source_name() -> None:
    assert _verify_targets(_config(databases=[_mapping("SALES")])) == ["SALES"]


def test_a_database_named_twice_is_asked_about_once() -> None:
    config = _config(databases=[_mapping("SALES", "STG"), _mapping("ORDERS", "STG")])

    assert _verify_targets(config) == ["STG"]


def test_the_request_carries_the_target_login_and_the_restored_names() -> None:
    request = _verify_request(_config(), {"TARGET_SA": "s3cret"})

    assert request == {
        "db_type": "sqlserver",
        "database_names": ["SALES_STG"],
        "target": {"host": "192.0.2.10", "port": 1433, "username": "sa", "password": "s3cret"},
    }


def test_an_entry_with_no_login_is_skipped_rather_than_failed() -> None:
    """"Not configured" is a state, not a failure — the rule this whole app follows. An entry this
    node was never given credentials for is not a broken restore, and counting it as one would fail
    every run on an estate that verifies some targets and not others."""
    assert _verify_request(_config(restore_sql_username=""), {"TARGET_SA": "x"}) is None
    assert _verify_request(_config(vm_credential_target=""), {"TARGET_SA": "x"}) is None
    assert _verify_request(_config(restore_sql_password_env=""), {}) is None


def test_a_named_secret_that_the_store_does_not_hold_is_skipped_not_guessed() -> None:
    """Sending an empty password produces a login failure that reads as "the restore broke the
    server's security", which is a worse answer than "I could not check"."""
    assert _verify_request(_config(), {}) is None
    assert _verify_request(_config(), {"TARGET_SA": ""}) is None


def test_an_entry_restoring_nothing_is_skipped() -> None:
    """Verifying zero databases would answer `ok: false` — `_verdict` requires at least one row —
    and fail a run that correctly had no work to do."""
    assert _verify_request(_config(databases=[]), {"TARGET_SA": "x"}) is None


# --------------------------------------------------------------------------- #
# An entry that states no database (0.27.0 1.101)
# --------------------------------------------------------------------------- #
def test_each_skip_says_its_own_reason() -> None:
    """Every skip said "no target login configured for this entry" - also over an entry whose login
    was there and whose only missing piece was the list of databases: the worker's nightly restore,
    13 databases restored every night and none ever opened by the check."""
    secrets = {"TARGET_SA": "x"}

    assert "states no database_mappings" in _verify_plan(_config(databases=[]), secrets)[1]
    assert "TARGET_SA is not in this node's secret store" in _verify_plan(_config(), {})[1]
    assert "no target login" in _verify_plan(_config(restore_sql_username=""), secrets)[1]
    assert "no target host" in _verify_plan(_config(vm_credential_target=""), secrets)[1]
    named = _config(restore_sql_instance_on_vm=r"localhost\DRILL")
    assert "names no port" in _verify_plan(named, secrets)[1]
    assert _verify_plan(_config(), secrets)[1] == ""


def _nightly(tmp_path, monkeypatch, *, mappings, restore_all=False):
    """The workflow as the worker runs it every night: a restore that reports two databases
    restored. Returns the verify requests sent and the run's summary."""
    import dataclasses

    from db_ops.backup_restore import cli
    from db_ops.backup_restore.copy_backup import CopyBackupResult
    from db_ops.backup_restore.delete_backup import DeleteBackupResult
    from db_ops.lib.config import DbOpsConfig
    from tests.test_backup_restore import make_config

    config = dataclasses.replace(
        make_config(tmp_path), restore_id="NIGHTLY", databases=tuple(mappings),
        restore_all_databases=restore_all, restore_database_name="",
        vm_credential_target="192.0.2.10", restore_sql_username="sa",
        restore_sql_password_env="TARGET_SA", restore_sql_instance_on_vm="localhost,1433")
    asked: list[dict] = []
    monkeypatch.setattr(cli, "run_target_preflight", lambda config, logger=None, **_: None)
    monkeypatch.setattr(cli, "run_copy_backup", lambda step_config, logger=None, force=False: CopyBackupResult(
        returncode=0, source_backup_dir=tmp_path, local_import_dir=tmp_path, files_considered=2,
        copied=2, skipped=0, file_results=()))
    monkeypatch.setattr(cli, "run_restore_all_latest", lambda **_: {
        "status": "SUCCESS", "per_database_restore_status": {"SALES": "SUCCESS", "ORDERS": "SUCCESS"}})
    monkeypatch.setattr(cli, "run_delete_backup", lambda step_config, logger=None, dry_run=False: DeleteBackupResult(
        returncode=0, target_backup_dir=tmp_path, cleanup_retention=0, files_considered=0, deleted=0,
        file_results=()))
    monkeypatch.setattr(cli, "_load_secrets", lambda **_: {"TARGET_SA": "x"})
    monkeypatch.setattr(cli.common_cli, "run", lambda op, request: (
        asked.append({"op": op, **request}),
        {"ok": True, "checked": len(request["database_names"]), "failed": 0, "databases": []})[1])
    summary = cli.run_restore_workflow(
        restore_configs=[config],
        app_config=DbOpsConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
                               sqlite_path=tmp_path / "runtime" / "db_ops.sqlite"))
    return asked, summary


def test_an_entry_that_states_its_databases_has_each_one_opened(tmp_path, monkeypatch) -> None:
    asked, summary = _nightly(tmp_path, monkeypatch, mappings=[_mapping("SALES"), _mapping("ORDERS")])

    assert [(item["op"], item["database_names"]) for item in asked] == [("verify-restore", ["SALES", "ORDERS"])]
    assert summary["verify-restore"]["checked"] == 2


def test_an_entry_that_states_none_is_not_checked_on_a_guess(tmp_path, monkeypatch) -> None:
    """The restore reported two databases; the code does not take that as the list to check (the
    operator, 2026-10-06: state the configuration in full, the code must not guess). The entry is
    skipped, and the skip says what to state."""
    asked, summary = _nightly(tmp_path, monkeypatch, mappings=[])

    assert asked == []
    (skipped,) = summary["verify-restore"]["targets"]
    assert skipped["status"] == "SKIPPED"
    assert "database_mappings" in skipped["reason"]


def test_an_entry_that_states_restore_all_databases_has_each_restored_one_opened(tmp_path, monkeypatch) -> None:
    """Every database on the share, under its backup's name - stated, so the databases this run
    restored ARE the list: no guess (the operator, 2026-10-06, 0.27.0 1.101)."""
    asked, summary = _nightly(tmp_path, monkeypatch, mappings=[], restore_all=True)

    assert [(item["op"], item["database_names"]) for item in asked] == [("verify-restore", ["SALES", "ORDERS"])]
    assert summary["verify-restore"]["checked"] == 2


def test_only_a_database_the_restore_reported_restored_is_checked() -> None:
    output = {"per_database_restore_status": {
        "SALES": "SUCCESS", "OLD": "SKIPPED", "REHEARSED": "DRY_RUN", "BROKEN": "FAILED"}}

    assert _restored_databases(output) == ["SALES"]
    assert _restored_databases({"status": "SUCCESS"}) == []
    assert _restored_databases(None) == []
