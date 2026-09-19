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

from db_ops.backup_restore.cli import _verify_request, _verify_targets


def _mapping(source: str, restored: str = "") -> SimpleNamespace:
    return SimpleNamespace(source_database_name=source, restore_database_name=restored)


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
        "databases": ["SALES_STG"],
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
