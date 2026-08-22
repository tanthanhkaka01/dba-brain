"""The switch that lets a SQL Server backup/restore also carry its instance metadata.

The design constraint is what these tests are mostly about: the scheduled MSSQL backup and
restore flows have been running for months, and adding this capability next to them must not
change them at all. An entry with no ``server_metadata`` block, or one that says
``enabled: false``, has to behave exactly as it did before the block existed.

The rest is strictness about what the block accepts, because the failure mode of a typo is
silence — a misspelled artifact name would simply not be exported, and nobody finds out until
the restore that needed it.
"""

from __future__ import annotations

import json

import pytest

from conftest import write_sqlserver_instance_policy

from db_ops.backup_restore import server_metadata as sm

@pytest.fixture(autouse=True)
def _instance_policy(estate):
    """Every test here reads the instance-portability policy; give it one of its own."""
    write_sqlserver_instance_policy(estate.data_dir)



# --------------------------------------------------------------------------- #
# The old flow must not move
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw", [None, {}, False, {"enabled": False}])
def test_an_entry_without_the_block_does_nothing_at_all(raw):
    plan = sm.parse_server_metadata(raw, label="CLOUD_MSSQL_FULL")

    assert plan.enabled is False
    assert sm.export_for_backup(plan, server_id="X", bundle_dir="/tmp/x") is None
    assert sm.replay_for_restore(plan, phase=sm.instance_bundle.PRE_DATABASE,
                                 bundle_dir="/tmp/x", target="T") is None


def test_a_disabled_block_is_the_same_as_no_block():
    assert sm.parse_server_metadata({"enabled": False, "artifacts": ["logins"]},
                                    label="X") == sm.parse_server_metadata(None, label="X")


# --------------------------------------------------------------------------- #
# The array
# --------------------------------------------------------------------------- #

def test_artifacts_selects_a_subset():
    """The point of the array: turn on logins and Agent jobs without also taking sp_configure,
    which is the setting most likely to be unwanted on a different machine."""
    plan = sm.parse_server_metadata(
        {"enabled": True, "artifacts": ["logins", "agent_jobs"]}, label="X"
    )

    assert plan.artifacts == ("logins", "agent_jobs")
    assert plan.request(target="T")["include"] == ["logins", "agent_jobs"]


def test_no_artifacts_means_everything_the_policy_declares():
    plan = sm.parse_server_metadata({"enabled": True}, label="X")

    assert plan.artifacts == ()
    assert "include" not in plan.request(target="T")


def test_a_misspelled_artifact_is_refused_rather_than_ignored():
    """Silently dropping it would produce a bundle that is short exactly one thing, discovered
    at the restore that needed it."""
    with pytest.raises(sm.ServerMetadataConfigError, match="unknown server_metadata artifact"):
        sm.parse_server_metadata({"enabled": True, "artifacts": ["loggins"]}, label="X")


def test_a_string_where_an_array_belongs_is_refused():
    """`"artifacts": "logins"` would otherwise iterate into single characters."""
    with pytest.raises(sm.ServerMetadataConfigError, match="must be an array"):
        sm.parse_server_metadata({"enabled": True, "artifacts": "logins"}, label="X")


def test_the_block_is_refused_on_a_non_sqlserver_entry():
    """Oracle and PostgreSQL carry this state inside their physical backups, so the block on one
    of them means the operator believes something untrue about their backup."""
    with pytest.raises(sm.ServerMetadataConfigError, match="SQL Server only"):
        sm.parse_server_metadata({"enabled": True}, label="CLOUD_PG_DB", db_type="postgresql")


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #

def test_both_phases_run_when_none_are_named():
    plan = sm.parse_server_metadata({"enabled": True, "target": "T"}, label="X", for_restore=True)

    assert plan.phases == sm.DEFAULT_PHASES


def test_naming_one_phase_runs_only_that_one():
    """Restoring the logins now and the jobs later is a legitimate split, not a mistake."""
    plan = sm.parse_server_metadata(
        {"enabled": True, "target": "T", "phases": ["pre-database"]}, label="X", for_restore=True
    )

    assert plan.phases == ("pre-database",)
    assert sm.replay_for_restore(plan, phase="post-database", bundle_dir="/tmp/x",
                                 target="T") is None


def test_an_invented_phase_is_refused():
    with pytest.raises(sm.ServerMetadataConfigError, match="phases may only contain"):
        sm.parse_server_metadata(
            {"enabled": True, "target": "T", "phases": ["during"]}, label="X", for_restore=True
        )


# --------------------------------------------------------------------------- #
# The restore needs an instance, not a host
# --------------------------------------------------------------------------- #

def test_a_restore_does_not_have_to_restate_where_it_restores_to():
    """The entry already says: target_server_id is the host, target_container is the container
    on it, and the inventory says which instance that container is. A third field repeating the
    answer is a second place for the same fact to live."""
    plan = sm.parse_server_metadata({"enabled": True}, label="CLOUD_MSSQL_TO_CLOUD2",
                                    for_restore=True)

    assert plan.enabled and plan.target == ""


@pytest.fixture
def inventory(tmp_path):
    """A two-instance inventory to resolve against.

    These three tests read the *shipped* db_instances.json until 2026-08-12, when a site was
    disabled ahead of shutting its VMs down and the resolver stopped seeing its instances - so
    a test of the resolver failed for an operational change it has no opinion about. What is
    under test is host + container -> instance, which needs an inventory, not this inventory.
    """
    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": "LAB-HOST", "db_type": "host", "ip": "10.0.0.9", "enabled": True},
        {"server_id": "LAB-MSSQL-1433", "db_type": "sqlserver", "ip": "10.0.0.9", "port": 1433,
         "instance_name": "mssql_primary", "enabled": True,
         "default_credential_name": "c", "metrics": {"enabled": True}},
    ]}), encoding="utf-8")
    (tmp_path / "users.json").write_text(json.dumps({"database_credentials": [
        {"server_id": "LAB-MSSQL-1433", "db_type": "sqlserver",
         "credentials": [{"credential_name": "c", "username": "sa", "password_ref": "R"}]},
    ]}), encoding="utf-8")
    return tmp_path


def test_the_target_instance_is_derived_from_the_host_and_container(inventory):
    plan = sm.parse_server_metadata({"enabled": True}, label="X", for_restore=True)

    resolved = sm.resolve_replay_target(
        plan,
        target_server_id="LAB-HOST",
        target_container="mssql_primary",
        data_dir=inventory,
    )

    assert resolved == "LAB-MSSQL-1433"


def test_an_explicit_target_overrides_the_derivation(inventory):
    """The escape hatch for an instance that is not in db_instances.json at all."""
    plan = sm.parse_server_metadata({"enabled": True, "target": "SOMETHING-ELSE"},
                                    label="X", for_restore=True)

    assert sm.resolve_replay_target(plan, target_server_id="LAB-HOST",
                                    target_container="mssql_primary",
                                    data_dir=inventory) == "SOMETHING-ELSE"


def test_a_container_with_no_registered_instance_is_refused_not_guessed(inventory):
    """Replaying logins onto the wrong instance is not an error anyone notices quickly."""
    plan = sm.parse_server_metadata({"enabled": True}, label="X", for_restore=True)

    with pytest.raises(sm.ServerMetadataConfigError, match="matches container"):
        sm.resolve_replay_target(plan, target_server_id="LAB-HOST",
                                 target_container="no_such_container", data_dir=inventory)


def test_a_backup_needs_no_target_because_it_reads_its_own_instance():
    plan = sm.parse_server_metadata({"enabled": True}, label="CLOUD_MSSQL_FULL")

    assert plan.enabled and plan.target == ""


def test_the_replay_request_does_not_repeat_the_artifact_list(monkeypatch):
    """`artifacts` decides what the export writes. Replay applies whatever the bundle contains,
    so passing it again would be a second place for the same decision to live."""
    plan = sm.parse_server_metadata(
        {"enabled": True, "target": "T", "artifacts": ["logins"]}, label="X", for_restore=True
    )
    captured = {}

    def _fake(request):
        captured.update(request)
        return {"ok": True}

    # Stubbed at the CLI client: the app hands `common` a finished request across a process
    # boundary now, so this is where the request can be seen.
    monkeypatch.setattr("db_ops.backup_restore.instance_metadata.replay_instance", _fake)
    sm.replay_for_restore(plan, phase="pre-database", bundle_dir="/tmp/x", target="T")

    assert "include" not in captured
    assert captured["phase"] == "pre-database"
    assert captured["confirm"] is True and captured["assume_yes"] is True


# --------------------------------------------------------------------------- #
# Failure handling differs by side, on purpose
# --------------------------------------------------------------------------- #

def test_an_export_failure_is_returned_not_raised(monkeypatch):
    """The data backup is the thing that must not be lost to a metadata step that could not read
    sys.credentials.

    The client returns the failure as a result rather than raising - a subprocess that died has no
    exception to propagate - so what this pins is that the shape survives the boundary.
    """
    def _boom(_request):
        return {"ok": False, "error": "permission denied on sys.credentials",
                "operation": "sqlserver-export-instance"}

    monkeypatch.setattr("db_ops.backup_restore.instance_metadata.export_instance", _boom)
    plan = sm.parse_server_metadata({"enabled": True}, label="X")

    outcome = sm.export_for_backup(plan, server_id="SRC", bundle_dir="/tmp/x")

    assert outcome["ok"] is False
    assert "permission denied" in outcome["error"]


def test_a_replay_failure_is_returned_as_a_failed_result(monkeypatch):
    """Swallowing it would leave a restored database nobody can log into while the restore
    reports success."""
    def _boom(_request):
        return {"ok": False, "error": "target refused the connection",
                "operation": "sqlserver-replay-instance"}

    monkeypatch.setattr("db_ops.backup_restore.instance_metadata.replay_instance", _boom)
    plan = sm.parse_server_metadata({"enabled": True, "target": "T"}, label="X", for_restore=True)

    outcome = sm.replay_for_restore(plan, phase="pre-database", bundle_dir="/tmp/x",
                                    target="T")

    assert outcome["ok"] is False
    assert "refused the connection" in outcome["error"]


# ---------------------------------------------------------------------------
# A bundle that cannot be replayed is not an exported bundle
# ---------------------------------------------------------------------------
NL = chr(10)


def test_a_go_inside_a_job_step_does_not_split_the_statement():
    """An Agent job step's command is itself T-SQL and routinely contains its own GO.

    Splitting on every line that reads GO cut ``sp_add_jobstep`` in half, so the first batch ended
    mid-literal: "Unclosed quotation mark after the character string 'USE APPDB_Prod'". 167 of 174
    jobs failed to replay on 2026-08-08 - every job with more than one statement in a step - and
    the export had reported success, because writing a bundle and being able to run it are
    different things and only the first was ever checked.
    """
    from db_ops.common.sqlserver_instance import _split_batches

    text = NL.join([
        "EXEC msdb.dbo.sp_add_jobstep @job_name = 'J', @command = 'USE APPDB_Prod",
        "GO",
        "EXEC [emp].[usp_Thing]",
        "', @database_name = 'APPDB_Prod';",
        "GO",
        "EXEC msdb.dbo.sp_add_jobserver @job_name = 'J';",
    ])

    batches = _split_batches(text)

    assert len(batches) == 2, "the GO inside the quoted command must not separate batches"
    assert "sp_add_jobstep" in batches[0] and "usp_Thing" in batches[0]
    assert "sp_add_jobserver" in batches[1]
    assert all(b.count("'") % 2 == 0 for b in batches), "a batch must not end mid-literal"


def test_a_real_go_still_separates_batches():
    """The fix must not stop GO working where it genuinely is a separator."""
    from db_ops.common.sqlserver_instance import _split_batches

    assert _split_batches(NL.join(["SELECT 1;", "GO", "SELECT 2;"])) == ["SELECT 1;", "SELECT 2;"]


def test_an_escaped_quote_pair_leaves_the_literal_state_alone():
    """``''`` is T-SQL's escape for a quote: two characters, so parity is unchanged."""
    from db_ops.common.sqlserver_instance import _split_batches

    text = NL.join(["EXEC p @t = 'it''s fine';", "GO", "SELECT 1;"])

    assert _split_batches(text) == ["EXEC p @t = 'it''s fine';", "SELECT 1;"]


# --------------------------------------------------------------------------- #
# The envelope seam — added 2026-08-16
# --------------------------------------------------------------------------- #
#
# `sqlserver-export-instance` / `-replay-instance` / `-verify-instance` answer in the response
# envelope now, so the gate report they used to print at the top level sits one level down in
# `data`. `instance_metadata.run_metadata_command` unwraps it, in one place, because `status`,
# `blockers` and `evidence_file` are what an incident review looks for and they must not move
# house because the transport grew a wrapper. These pin that seam.

def test_the_gate_report_is_unwrapped_from_the_envelope(monkeypatch):
    from db_ops.backup_restore import instance_metadata
    from db_ops.lib import common_cli

    monkeypatch.setattr(common_cli, "run_allowing_failure", lambda command, request, **_kw: (
        True,
        {"ok": True, "status": "OK", "blockers": [], "gates": [{"name": "logins"}],
         "evidence_file": "runtime/evidence/x.json"},
        "",
    ))

    result = instance_metadata.export_instance({"target": "ACME-1"})

    assert result["ok"] is True
    assert result["status"] == "OK"
    assert result["evidence_file"] == "runtime/evidence/x.json"
    assert result["gates"] == [{"name": "logins"}]


def test_a_failed_gate_keeps_its_reason_and_its_blockers(monkeypatch):
    """`summarize()` writes `error` and `blockers` into `job_runs.metadata_json`; losing either in
    the unwrap would leave a restore reporting that metadata failed without saying what failed."""
    from db_ops.backup_restore import instance_metadata, server_metadata
    from db_ops.lib import common_cli

    monkeypatch.setattr(common_cli, "run_allowing_failure", lambda command, request, **_kw: (
        False,
        {"ok": False, "status": "FAIL", "blockers": ["edition_supported"]},
        "blocked by edition_supported",
    ))

    result = instance_metadata.replay_instance({"target": "ACME-1"})

    assert result["ok"] is False
    assert result["error"] == "blocked by edition_supported"
    assert server_metadata.summarize(result) == {
        "ok": False, "status": "FAIL", "error": "blocked by edition_supported",
        "blockers": ["edition_supported"]}


def test_a_command_that_could_not_run_at_all_is_passed_through(monkeypatch):
    """A process that never answered raises out of the reader. This module's contract is that a
    metadata step never fails the restore beside it, so the raise is caught and shaped like a
    failed report — one failure shape for the caller, not two."""
    from db_ops.backup_restore import instance_metadata
    from db_ops.lib import common_cli

    def boom(command, request, **_kw):
        raise common_cli.CommonCliError(f"{command} could not run: boom")

    monkeypatch.setattr(common_cli, "run_allowing_failure", boom)

    result = instance_metadata.export_instance({"target": "ACME-1"})

    assert result["ok"] is False and "could not run" in result["error"]
