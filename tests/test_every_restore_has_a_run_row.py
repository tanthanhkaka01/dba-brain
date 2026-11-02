"""Every restore has a run row, on every path, moved through its phases (0.34.0, 1.214).

Why it matters: until 1.214 a ``restore_runs`` row was written only by the copy gate. A restore that
copies nothing - in place, the target reading the source's own folder - left no row at all. A row
that did exist stopped at ``verified``: it never said the restore ran, nor how many databases then
opened, nor which scheduler run it belonged to. "Every backup and restore run saved into the db"
(the operator, 2026-10-29) means every run, said to its end.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import restore_by_id, restore_script
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import integrity as I


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    phases: dict[int, list[str]] = {}
    real = store.set_restore_run

    def recording(run_id, **values):
        if "phase" in values:
            phases.setdefault(int(run_id), []).append(values["phase"])
        return real(run_id, **values)

    store.set_restore_run = recording
    store.phases = phases
    return store


def _row(catalog, run_id):
    with catalog.connect() as conn:
        return dict(conn.execute("SELECT * FROM restore_runs WHERE restore_run_id = ?", (run_id,)).fetchone())


# --------------------------------------------------------------------------- #
# The share-driven (SQL Server) workflow
# --------------------------------------------------------------------------- #
def _share_driven(tmp_path, monkeypatch, catalog, *, unusable=0):
    from dbabrain.backup_restore import cli
    from dbabrain.backup_restore.copy_backup import CopyBackupResult
    from dbabrain.backup_restore.delete_backup import DeleteBackupResult
    from dbabrain.lib.config import DbaBrainConfig
    from tests.test_backup_restore import make_config

    config = dataclasses.replace(
        make_config(tmp_path), restore_id="NIGHTLY", source_id="ACME-192-0-2-250", target_id="LAB-192-0-2-40",
        vm_credential_target="192.0.2.40", restore_sql_username="sa", restore_sql_password_env="TARGET_SA",
        restore_sql_instance_on_vm="localhost,1433")
    monkeypatch.setattr(cli, "_open_catalog", lambda *_a, **_k: catalog)
    monkeypatch.setattr(cli, "run_target_preflight", lambda config, logger=None, **_: None)

    def copy(step_config, logger=None, force=False, catalog=None):
        run_id = catalog.start_restore_run(action="restore", restore_id=step_config.restore_id,
                                           source_server_id=step_config.source_id,
                                           target_server_id=step_config.target_id, copy_max_retry=3,
                                           phase="copying")
        catalog.set_restore_run(run_id, phase="verified")
        return CopyBackupResult(returncode=0, source_backup_dir=tmp_path, local_import_dir=tmp_path,
                                files_considered=2, copied=2, skipped=0, file_results=(),
                                restore_run_id=run_id, integrity={"proven": True})

    monkeypatch.setattr(cli, "run_copy_backup", copy)
    monkeypatch.setattr(cli, "run_restore_all_latest", lambda **_: {
        "status": "SUCCESS", "per_database_restore_status": {"SALES": "SUCCESS", "ORDERS": "SUCCESS"}})
    monkeypatch.setattr(cli, "run_delete_backup", lambda *a, **k: DeleteBackupResult(
        returncode=0, target_backup_dir=tmp_path, cleanup_retention=0, files_considered=0, deleted=0,
        file_results=()))
    monkeypatch.setattr(cli, "_load_secrets", lambda **_: {"TARGET_SA": "x"})
    monkeypatch.setattr(cli, "_verify_plan", lambda *a, **k: ({"db_type": "sqlserver"}, ""))
    monkeypatch.setattr(cli.common_cli, "run", lambda op, request: {
        "ok": not unusable, "checked": 2, "failed": unusable,
        "databases": [{"database_name": "SALES", "ok": True}, {"database_name": "ORDERS", "ok": not unusable}]})
    app_config = DbaBrainConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
                                sqlite_path=tmp_path / "runtime" / "dbabrain.sqlite")
    return cli.run_restore_workflow(restore_configs=[config], app_config=app_config, job_run_id=4711)


def test_a_share_driven_restore_row_goes_to_done_with_its_databases_and_its_scheduler_run(
        tmp_path, monkeypatch, catalog):
    _share_driven(tmp_path, monkeypatch, catalog)

    row = _row(catalog, 1)
    assert catalog.phases[1] == ["verified", "restoring", "verifying_db", "done"]
    assert (row["phase"], row["databases_ok"], row["databases_failed"]) == ("done", 2, 0)
    assert (row["job_run_id"], row["point_in_time"]) == (4711, "LATEST")
    assert row["ended_at"]


def test_a_database_that_does_not_open_leaves_the_row_in_error_with_the_count(tmp_path, monkeypatch, catalog):
    with pytest.raises(Exception, match="cannot be opened"):
        _share_driven(tmp_path, monkeypatch, catalog, unusable=1)

    row = _row(catalog, 1)
    assert (row["phase"], row["databases_ok"], row["databases_failed"]) == ("error", 1, 1)
    assert "cannot be opened" in row["error_text"]


# --------------------------------------------------------------------------- #
# The script-driven path (PostgreSQL, Oracle, container SQL Server)
# --------------------------------------------------------------------------- #
PLAN = [{"op": "restore-full", "request": {"db_type": "postgresql"}},
        {"op": "verify-restore", "request": {"db_type": "postgresql"}}]


def _script(monkeypatch, catalog, *, remote, checked=2, failed=0):
    from dbabrain.lib.time_window import TimeWindow

    job = restore_script.ScriptRestore(
        restore_id="PG_DRILL", db_type="postgresql", server_id="LAB-A", backup_dir="/backup",
        script="restore.sh", time_window=TimeWindow(), target_server_id="LAB-B" if remote else "LAB-A",
        target_container="pg_lab_b", target_backup_dir="/b/B", source_backup_host_dir="/b/A",
        integrity=I.RestoreIntegrity(), action="restore")
    monkeypatch.setattr(restore_script, "load_script_restores", lambda _p=None: [job])
    monkeypatch.setattr(restore_by_id, "_host_block", lambda j, **_: {"host": "h"})
    monkeypatch.setattr(restore_by_id, "assert_target_is_not_source", lambda *a, **k: None)
    monkeypatch.setitem(restore_by_id._PLANNERS, "postgresql", lambda *a, **k: [dict(s) for s in PLAN])
    monkeypatch.setattr(restore_by_id, "_metadata_phase", lambda *a, **k: "")
    monkeypatch.setattr("dbabrain.backup_restore.backup.resolve_ssh_target", lambda *a, **k: SimpleNamespace(host="h"))
    monkeypatch.setattr(restore_by_id, "_execute", lambda op, request: (
        {"ok": not failed, "checked": checked, "failed": failed} if op == "verify-restore" else {"ok": True}))
    monkeypatch.setattr(restore_script, "prune_staged_backups", lambda *a, **k: {"pruned": 0})

    def transfer(job, **kwargs):
        run_id = catalog.start_restore_run(action="restore", restore_id=job.restore_id,
                                           source_server_id=job.server_id, target_server_id=job.target_server_id,
                                           copy_max_retry=3, phase="copying")
        catalog.set_restore_run(run_id, phase="verified")
        return {"copied": 1, "skipped": 0, "bytes_copied": 1, "include": [], "restore_run_id": run_id}

    monkeypatch.setattr(restore_script, "transfer_backup_to_target", transfer)
    return restore_by_id.restore_by_id({"restore_id": "PG_DRILL", "point_in_time": "2026-10-29 08:00:00 +07:00"},
                                       catalog=catalog, job_run_id=99)


def test_a_restore_that_copies_nothing_still_has_its_row(monkeypatch, catalog):
    """In place: nothing travels, no gate - and until 1.214, no row either."""
    _script(monkeypatch, catalog, remote=False)

    row = _row(catalog, 1)
    assert (row["action"], row["source_server_id"], row["target_server_id"]) == ("restore", "LAB-A", "LAB-A")
    assert catalog.phases[1] == ["restoring", "verifying_db", "done"]
    assert (row["databases_ok"], row["databases_failed"], row["job_run_id"]) == (2, 0, 99)
    assert row["point_in_time"] == "2026-10-29 08:00:00 +07:00"
    with catalog.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM restore_run_files").fetchone()[0] == 0, "nothing was copied"


def test_a_copied_restores_row_carries_on_from_its_copy_to_done(monkeypatch, catalog):
    _script(monkeypatch, catalog, remote=True)

    row = _row(catalog, 1)
    assert catalog.phases[1] == ["verified", "restoring", "verifying_db", "done"]
    assert (row["target_server_id"], row["job_run_id"], row["databases_ok"]) == ("LAB-B", 99, 2)


def test_a_failed_check_is_counted_on_the_row_before_it_fails_the_run(monkeypatch, catalog):
    with pytest.raises(restore_by_id.RestoreByIdError, match="verification failed"):
        _script(monkeypatch, catalog, remote=True, checked=2, failed=1)

    row = _row(catalog, 1)
    assert (row["phase"], row["databases_ok"], row["databases_failed"]) == ("error", 1, 1)


def test_a_record_that_cannot_be_written_never_fails_the_restore():
    from dbabrain.backup_restore.copy_gate import record_run

    class Broken:
        def set_restore_run(self, *_a, **_k):
            raise OSError("the store is down")

    record_run(Broken(), 7, phase="restoring")      # says nothing, raises nothing
    record_run(None, 7, phase="restoring")
    record_run(Broken(), None, phase="restoring")
