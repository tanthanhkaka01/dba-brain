"""The `server_metadata` switch has to be *reached*, not merely parsed.

The block existed in `restore_config.json`, the parser understood it, and `server_metadata.py`
had a tested export and replay - and none of it ran. `export_for_backup` and `replay_for_restore`
had no callers at all, and the restore loader built its `ScriptRestore` without ever reading the
block, so a restore entry with `"enabled": true` was read from disk and dropped on the floor. The
worker was deployed in that state: config saying yes, runtime unable to act on it.

That failure is invisible from the outside. The restore succeeds, the databases come back, the
Telegram message is green, and nobody discovers the logins never travelled until someone tries to
log in - or worse, until the day the drill is not a drill. So these tests pin the wiring itself,
not the behaviour behind it: that the calls happen, at the right moment, in the right order, and
that the ways they can fail cost a warning rather than a restore.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_ops.backup_restore import backup as backup_module
from db_ops.backup_restore import restore_script
from db_ops.backup_restore import server_metadata as sm
from db_ops.backup_restore.server_metadata import ServerMetadataPlan, instance_bundle_dir
from db_ops.lib.time_window import TimeWindow


SOURCE = "CLOUD-203-0-113-188-MSSQL-1433"
TARGET_INSTANCE = "CLOUD2-203-0-113-121-MSSQL-1433"


def _restore(tmp_path, *, plan=None, phases=("pre-database", "post-database")):
    return restore_script.ScriptRestore(
        restore_id="CLOUD_MSSQL_TO_CLOUD2",
        db_type="sqlserver",
        server_id=SOURCE,
        backup_dir="/var/opt/mssql/backup/dbops",
        script="assets/restore/sqlserver/mssql_restore.sh",
        time_window=TimeWindow(),
        target_container="mssql_ha_cloud2-primary",
        target_server_id="CLOUD2-203-0-113-121-HOST",
        server_metadata=plan if plan is not None
        else ServerMetadataPlan(enabled=True, phases=tuple(phases)),
    )


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """A bundle exported for the source instance, where the restore will look for it."""
    root = tmp_path / "instance_bundles"
    monkeypatch.setattr("db_ops.backup_restore.server_metadata.BUNDLE_ROOT", root)
    (root / SOURCE / "server").mkdir(parents=True)
    return root / SOURCE


# --------------------------------------------------------------------------- #
# The restore entry's block has to survive being loaded.
# --------------------------------------------------------------------------- #


def test_an_entry_with_no_block_still_loads_as_inert(tmp_path):
    """The design constraint the whole feature is built under: the scheduled restores that have
    been running for months must not change because a capability was added beside them."""
    config = tmp_path / "restore_config.json"
    config.write_text(
        """
        {"backup_restore": {"restores": [{
            "restore_id": "CLOUD_PG_DRILL", "db_type": "postgresql",
            "server_id": "CLOUD-203-0-113-188-PG-5433", "backup_dir": "/b",
            "target_container": "pg_ha-standby-1",
            "script": "assets/restore/postgresql/pg_restore_database.sh"
        }]}}
        """,
        encoding="utf-8",
    )

    assert restore_script.load_script_restores(config)[0].server_metadata.enabled is False


# --------------------------------------------------------------------------- #
# Order around the restore.
# --------------------------------------------------------------------------- #

def test_pre_database_runs_before_the_restore_and_post_database_after(tmp_path, bundle, monkeypatch):
    """Not interchangeable: logins must exist before the databases arrive or their users are
    orphaned, and Agent job steps name databases that have to exist first."""
    calls: list[str] = []

    monkeypatch.setattr(sm, "resolve_replay_target", lambda *a, **k: TARGET_INSTANCE)
    monkeypatch.setattr(
        sm, "replay_for_restore",
        lambda plan, *, phase, **k: calls.append(f"replay:{phase}") or {"ok": True, "status": "OK"},
    )
    monkeypatch.setattr(restore_script, "resolve_ssh_target", _fake_ssh_target)
    monkeypatch.setattr(restore_script, "_script_path", lambda *a, **k: _FakeScript())
    monkeypatch.setattr(restore_script, "_script_env", lambda *a, **k: {})
    monkeypatch.setattr(restore_script, "_restore_secrets", lambda *a, **k: {})
    monkeypatch.setattr(restore_script, "transfer_backup_to_target",
                        lambda *a, **k: {"copied": 0, "skipped": 0, "bytes_copied": 0})

    def _execute(**kwargs):
        calls.append("restore-script")
        return "RESULT=ok\n", "", 0

    monkeypatch.setattr(restore_script, "execute_over_ssh", _execute)

    status, _exit, out, _err = restore_script.run_script_restore(_restore(tmp_path))

    assert status == "done"
    assert calls == ["replay:pre-database", "restore-script", "replay:post-database"]
    assert "PHASE=metadata-pre-database ok" in out
    assert "PHASE=metadata-post-database ok" in out


def test_post_database_is_skipped_when_the_restore_itself_failed(tmp_path, bundle, monkeypatch):
    """Agent jobs name databases. Creating them against a restore that did not happen leaves
    jobs pointing at databases that are not there, discovered later and out of context."""
    calls: list[str] = []

    monkeypatch.setattr(sm, "resolve_replay_target", lambda *a, **k: TARGET_INSTANCE)
    monkeypatch.setattr(
        sm, "replay_for_restore",
        lambda plan, *, phase, **k: calls.append(phase) or {"ok": True, "status": "OK"},
    )
    monkeypatch.setattr(restore_script, "resolve_ssh_target", _fake_ssh_target)
    monkeypatch.setattr(restore_script, "_script_path", lambda *a, **k: _FakeScript())
    monkeypatch.setattr(restore_script, "_script_env", lambda *a, **k: {})
    monkeypatch.setattr(restore_script, "_restore_secrets", lambda *a, **k: {})
    monkeypatch.setattr(restore_script, "transfer_backup_to_target",
                        lambda *a, **k: {"copied": 0, "skipped": 0, "bytes_copied": 0})
    monkeypatch.setattr(restore_script, "execute_over_ssh",
                        lambda **k: ("PHASE=restore-data failed\n", "boom", 1))

    status, _exit, _out, _err = restore_script.run_script_restore(_restore(tmp_path))

    assert status == "error"
    assert calls == ["pre-database"]


def test_naming_only_one_phase_runs_only_that_one(tmp_path, bundle, monkeypatch):
    """Restoring the logins now and the jobs later is a legitimate split."""
    calls: list[str] = []

    monkeypatch.setattr(sm, "resolve_replay_target", lambda *a, **k: TARGET_INSTANCE)
    monkeypatch.setattr(
        sm, "replay_for_restore",
        lambda plan, *, phase, **k: calls.append(phase) or {"ok": True, "status": "OK"},
    )
    monkeypatch.setattr(restore_script, "resolve_ssh_target", _fake_ssh_target)
    monkeypatch.setattr(restore_script, "_script_path", lambda *a, **k: _FakeScript())
    monkeypatch.setattr(restore_script, "_script_env", lambda *a, **k: {})
    monkeypatch.setattr(restore_script, "_restore_secrets", lambda *a, **k: {})
    monkeypatch.setattr(restore_script, "transfer_backup_to_target",
                        lambda *a, **k: {"copied": 0, "skipped": 0, "bytes_copied": 0})
    monkeypatch.setattr(restore_script, "execute_over_ssh", lambda **k: ("RESULT=ok\n", "", 0))

    restore_script.run_script_restore(_restore(tmp_path, phases=("pre-database",)))

    assert calls == ["pre-database"]


# --------------------------------------------------------------------------- #
# Failure costs a warning, never the restore.
# --------------------------------------------------------------------------- #

def test_a_replay_failure_leaves_the_restore_successful(tmp_path, bundle, monkeypatch):
    """The databases are the deliverable. Failing the run would report that nothing was restored
    when in fact everything was - the warning has to be loud, not fatal."""
    monkeypatch.setattr(sm, "resolve_replay_target", lambda *a, **k: TARGET_INSTANCE)
    monkeypatch.setattr(sm, "replay_for_restore",
                        lambda *a, **k: {"ok": False, "status": "FAIL", "error": "login denied"})
    monkeypatch.setattr(restore_script, "resolve_ssh_target", _fake_ssh_target)
    monkeypatch.setattr(restore_script, "_script_path", lambda *a, **k: _FakeScript())
    monkeypatch.setattr(restore_script, "_script_env", lambda *a, **k: {})
    monkeypatch.setattr(restore_script, "_restore_secrets", lambda *a, **k: {})
    monkeypatch.setattr(restore_script, "transfer_backup_to_target",
                        lambda *a, **k: {"copied": 0, "skipped": 0, "bytes_copied": 0})
    monkeypatch.setattr(restore_script, "execute_over_ssh", lambda **k: ("RESULT=ok\n", "", 0))

    status, _exit, out, _err = restore_script.run_script_restore(_restore(tmp_path))

    assert status == "done"
    assert "PHASE=metadata-pre-database FAILED" in out


def test_a_missing_bundle_names_the_backup_entry_that_should_have_written_it(tmp_path, monkeypatch):
    """The one mismatch this can be configured into: server_metadata on for the restore and off
    for the same instance's backup. A bare "bundle not found" would send an operator looking at
    the restore, which is the half that is correct."""
    root = tmp_path / "instance_bundles"
    monkeypatch.setattr("db_ops.backup_restore.server_metadata.BUNDLE_ROOT", root)

    line, summary = restore_script.replay_metadata_phase(
        _restore(tmp_path), phase="pre-database",
    )

    assert "SKIPPED" in line
    assert SOURCE in line and "server_metadata.enabled" in line
    assert summary["ok"] is False


def test_an_unresolvable_target_is_a_warning_not_a_crash(tmp_path, bundle, monkeypatch):
    """resolve_replay_target raises rather than guessing, which is right - but that exception
    reaching run_script_restore would fail a restore over a config problem in a side channel."""
    def _boom(*a, **k):
        raise ValueError("Container 'x' matches more than one instance")

    monkeypatch.setattr(sm, "resolve_replay_target", _boom)

    line, summary = restore_script.replay_metadata_phase(
        _restore(tmp_path), phase="pre-database",
    )

    assert "SKIPPED" in line and "matches more than one instance" in line
    assert summary["ok"] is False


def test_a_disabled_plan_runs_nothing_and_prints_nothing(tmp_path, monkeypatch):
    """An entry that never asked for this must not gain a line of output because of it."""
    called = []
    monkeypatch.setattr(sm, "replay_for_restore", lambda *a, **k: called.append(1))

    line, summary = restore_script.replay_metadata_phase(
        _restore(tmp_path, plan=ServerMetadataPlan()), phase="pre-database",
    )

    assert (line, summary, called) == ("", None, [])


# --------------------------------------------------------------------------- #
# The export half.
# --------------------------------------------------------------------------- #

def test_the_bundle_is_keyed_on_the_source_instance(tmp_path, monkeypatch):
    """This is what lets the backup and the restore meet without a third config field naming a
    path: the restore's `server_id` IS the instance the backup exported from."""
    root = tmp_path / "instance_bundles"
    monkeypatch.setattr("db_ops.backup_restore.server_metadata.BUNDLE_ROOT", root)

    assert instance_bundle_dir(SOURCE) == root / SOURCE


@pytest.mark.parametrize(
    "backup_status, expect_export",
    [("done", True), ("error", False)],
)
def test_metadata_is_exported_only_after_a_backup_that_worked(
    tmp_path, monkeypatch, backup_status, expect_export
):
    """A bundle is only meaningful beside the backup it was taken with. Writing a fresh one after
    a failed backup pairs tonight's logins with a backup that does not exist - and does it
    silently, since the export itself would succeed.

    Driven through `run_backup` rather than around it: the guard is one `if` inside that loop, and
    a test that re-stated the condition would pass whatever the loop actually did.
    """
    exported: list[dict] = []
    monkeypatch.setattr(
        backup_module, "export_for_backup",
        lambda plan, *, server_id, bundle_dir, **k:
            exported.append({"server_id": server_id, "bundle_dir": str(bundle_dir)})
            or {"ok": True, "status": "OK"},
    )
    monkeypatch.setattr(backup_module, "DbOpsStore", _FakeStore)
    monkeypatch.setattr(backup_module, "emit_backup_restore_event", lambda **k: None)
    monkeypatch.setattr(backup_module.schedule, "reap_stale_runs", lambda **k: [])
    monkeypatch.setattr(backup_module, "resolve_backup_target", lambda *a, **k: None)
    monkeypatch.setattr(backup_module, "load_backup_jobs", lambda _p: [_backup_job()])
    monkeypatch.setattr(
        backup_module, "execute_backup_job",
        lambda item, **k: backup_module.BackupRunResult(
            job=item, status=backup_status, exit_code=0 if backup_status == "done" else 1,
            duration_ms=1, stdout="RESULT=ok", stderr="", error_text=None,
        ),
    )

    backup_module.run_backup(app_config=_FakeConfig(), config_path="ignored", force=True)

    assert bool(exported) is expect_export
    if expect_export:
        assert exported[0]["server_id"] == SOURCE
        assert exported[0]["bundle_dir"].endswith(SOURCE)


def _backup_job():
    return backup_module.BackupJob(
        backup_id="CLOUD_MSSQL_FULL", job="full", db_type="sqlserver", server_id=SOURCE,
        script="assets/backup/sqlserver/mssql_backup_database.sh",
        backup_dir="/var/opt/mssql/backup/dbops", retention_days=14,
        time_window=TimeWindow(), server_metadata=ServerMetadataPlan(enabled=True),
    )


class _FakeConfig:
    sqlite_path = ":memory:"


class _FakeStore:
    """Just the four calls run_backup makes on the store."""

    @classmethod
    def from_config(cls, config, **kwargs):
        return cls()

    def fetch_latest_job_runs_by_job_code(self):
        return {}

    def fetch_running_job_runs(self, _prefix=""):
        return []

    def insert_job_run(self, _run):
        return 1

    def update_job_run(self, **kwargs):
        self.last_update = kwargs


class _FakeScript:
    def read_text(self, encoding="utf-8"):
        return "#!/bin/sh\necho RESULT=ok\n"


class _FakeSshTarget:
    def __init__(self, host, container_name):
        self.host = host
        self.container_name = container_name


def _fake_ssh_target(server_id, **kwargs):
    """Source and target on different hosts - the remote drill, which is the real entry's shape.

    They must differ: `run_script_restore` refuses a restore whose target container is the source
    container, and that guard is the reason a shared `object()` stub is not enough here.
    """
    if server_id == SOURCE:
        return _FakeSshTarget("203.0.113.188", "mssql_ha-primary")
    return _FakeSshTarget("203.0.113.121", "mssql_ha_cloud2-primary")


# --------------------------------------------------------------------------- #
# One decision, two restore paths.
# --------------------------------------------------------------------------- #
#
# The engine path (SMB share + sqlcmd, the 2.250 -> 2.249 production flow) and the script
# path (host-to-host transfer into a container, the CLOUD drills) share no execution machinery
# and cannot: one selects its restore chain in Python with PITR/STOPAT, the other in bash. What
# they do share is the decision *around* the restore - same bundle, same two phases, same
# ordering rule, same failure policy - and that now lives in exactly one place. A second copy
# would drift in the direction nobody notices: a phase quietly not running looks the same as a
# phase that ran and found nothing to do.


def _target(server_id, ip, instance_name="", db_type="sqlserver"):
    from types import SimpleNamespace
    return SimpleNamespace(server_id=server_id, ip=ip, instance_name=instance_name, db_type=db_type)


def _with_inventory(monkeypatch, targets):
    import db_ops.common.data_sources as mtc
    monkeypatch.setattr(mtc, "load_config_metric_targets", lambda **k: targets)


def test_an_engine_entry_resolves_its_instance_by_ip(monkeypatch):
    """The engine path restores through a share and a sqlcmd connection, so it has no container
    to name - but its `vm_credential_target` is the target host's ip, and one SQL Server there
    is not a guess."""
    _with_inventory(monkeypatch, [
        _target("ACME-192-0-2-249-MSSQLSERVER-1433", "192.0.2.249", "MSSQLSERVER"),
        _target("ACME-192-0-2-249-PGLAB-5433", "192.0.2.249", "pg", db_type="postgresql"),
    ])

    resolved = sm.resolve_replay_target(
        ServerMetadataPlan(enabled=True),
        target_server_id="MSSQL2025-DOCKER-192-0-2-249",   # not an inventory id
        target_container="",
        target_host="192.0.2.249",
    )

    assert resolved == "ACME-192-0-2-249-MSSQLSERVER-1433"


def test_two_instances_on_one_ip_is_refused_not_guessed(monkeypatch):
    """Same rule the container branch follows. Replaying logins onto the wrong instance of a
    pair is not an error anyone notices quickly."""
    _with_inventory(monkeypatch, [
        _target("A-1433", "10.0.0.1", "ONE"),
        _target("A-1434", "10.0.0.1", "TWO"),
    ])

    with pytest.raises(sm.ServerMetadataConfigError, match="more than one SQL Server"):
        sm.resolve_replay_target(
            ServerMetadataPlan(enabled=True),
            target_server_id="", target_container="", target_host="10.0.0.1",
        )


def test_both_paths_call_the_same_shared_function(tmp_path, bundle, monkeypatch):
    """The point of the extraction, pinned: patching the shared entry point silences BOTH paths.
    If either grew its own copy, one of these two would still fire."""
    from db_ops.backup_restore import workflow as wf

    seen: list[str] = []
    monkeypatch.setattr(sm, "replay_phase",
                        lambda plan, **k: seen.append(k["label"]) or ("", None))
    monkeypatch.setattr(restore_script, "replay_phase", sm.replay_phase)
    monkeypatch.setattr(wf, "replay_phase", sm.replay_phase)

    restore_script.replay_metadata_phase(_restore(tmp_path), phase="pre-database")

    class _EngineConfig:
        server_metadata = ServerMetadataPlan(enabled=True)
        restore_id = "ACME_TO_MSSQL2025_DOCKER"
        source_id = "ACME-192-0-2-250"
        target_id = "MSSQL2025-DOCKER-192-0-2-249"
        vm_credential_target = "192.0.2.249"

    wf.replay_engine_phase(_EngineConfig(), phase="pre-database")

    assert seen == ["CLOUD_MSSQL_TO_CLOUD2 (sqlserver)", "ACME_TO_MSSQL2025_DOCKER"]

def test_the_cli_restore_workflow_replays_metadata_too():
    """It did not, and said nothing about not doing it.

    ``replay_engine_phase`` lived only in ``run_scheduled_restores``, so the nightly run replayed
    logins, roles and Agent jobs while an operator running ``restore-workflow`` by hand got the
    databases and none of the machinery around them - silently, because a phase that never runs
    looks exactly like a phase that ran and found nothing. Same shape as ``restore-by-id``
    emitting no events: the behaviour depended on how you invoked it.
    """
    import ast
    import pathlib as _p

    from db_ops.backup_restore import workflow as wf

    source = (_p.Path(wf.__file__).parent / "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "replay_engine_phase" in called, (
        "cli.py's restore-workflow must replay instance metadata, like the scheduled path does")


def test_both_phases_run_and_the_post_phase_comes_after_the_restore():
    """Order is the rule, not a detail: 'pre-database' has to land before the databases so their
    users are not orphaned, and 'post-database' after, because Agent job steps name databases that
    must already exist."""
    import pathlib as _p

    from db_ops.backup_restore import workflow as wf

    source = (_p.Path(wf.__file__).parent / "cli.py").read_text(encoding="utf-8")
    body = source[source.index('elif args.command == "restore-workflow":'):]
    body = body[:body.index('elif args.command == "verify-restore":')]

    pre = body.index("PRE_DATABASE")
    run = body.index("output = run_restore_workflow(")
    post = body.index("POST_DATABASE")
    assert pre < run < post, "pre-database must precede the restore and post-database follow it"

