"""A copy-only run copies from server_id to the target, proves it, and restores nothing (0.34.0, 1.216).

The operator, 2026-10-29: *add a function in restore - do not restore, only copy the backup*; then
*from server_id to target_id, like a restore*; and *keep the restore config's current format, do not
invent a new object* - with one field added to the existing entries, ``action``.

Why it matters:
- A copy-only run is how a second copy, or a pre-staged one, is made. If it ran the RESTORE it would
  overwrite a database somebody may be using on the target.
- If it counted as a drill, the estate would read as drilled when nothing was ever opened.
- An entry that states no ``action`` is refused, never read as a restore (R50); upgrade-config writes
  ``"restore"``, what every entry did before.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from conftest import present_copies

from dbabrain.backup_restore import restore_by_id, restore_script, workflow
from dbabrain.lib import integrity as I
from tests.test_restore_reporting import harness  # noqa: F401 - the scheduler's fixture

# --------------------------------------------------------------------------- #
# The field
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("stated, read", [(None, None), ("restore", "restore"), ("COPY_ONLY", "copy_only")])
def test_an_entrys_action_is_read_as_stated(stated, read):
    entry = {} if stated is None else {"action": stated}
    assert I.parse_restore_action(entry, label="R") == read


@pytest.mark.parametrize("bad", ["copy", "", 1, True])
def test_an_action_outside_the_two_is_refused_by_name(bad):
    with pytest.raises(I.IntegrityConfigError, match="action must be one of restore, copy_only"):
        I.parse_restore_action({"action": bad}, label="R")


def test_an_entry_that_states_no_action_is_refused_not_read_as_a_restore():
    with pytest.raises(I.IntegrityConfigError, match="states no action"):
        I.require_restore_action(None, label="restore_id=R")


def test_upgrade_config_states_restore_and_keeps_a_stated_copy_only():
    document = {"backup_restore": {"restores": [
        {"restore_id": "OLD", "integrity": {"copy_max_retry": 3}},
        {"restore_id": "COPY", "action": "copy_only", "integrity": {"copy_max_retry": 3}}]}}

    changes = I.state_restore_integrity(document)

    assert [r["action"] for r in document["backup_restore"]["restores"]] == ["restore", "copy_only"]
    assert [c["field"] for c in changes] == ["action"], "only what was missing is written"


# --------------------------------------------------------------------------- #
# The share-driven (SQL Server) workflow stops after the copy
# --------------------------------------------------------------------------- #
class _Catalog:
    def __init__(self):
        self.closed: list[dict] = []

    def set_restore_run(self, run_id, **values):
        self.closed.append({"run": run_id, **values})


def _share_driven(tmp_path, monkeypatch, *, copy_only):
    from dbabrain.backup_restore import cli
    from dbabrain.backup_restore.copy_backup import CopyBackupResult
    from dbabrain.lib.config import DbaBrainConfig
    from tests.test_backup_restore import make_config

    copied_with: list = []
    catalog = _Catalog()
    monkeypatch.setattr(cli, "_open_catalog", lambda *_a, **_k: catalog)
    monkeypatch.setattr(cli, "run_target_preflight", lambda config, logger=None, **_: None)

    def copy(step_config, logger=None, force=False, catalog=None):
        copied_with.append(step_config)
        return CopyBackupResult(returncode=0, source_backup_dir=tmp_path, local_import_dir=tmp_path,
                                files_considered=2, copied=2, skipped=0, file_results=(),
                                restore_run_id=7, integrity={"proven": True})

    def must_not_run(**_kwargs):
        raise AssertionError("a copy-only run reached the restore or its cleanup")

    monkeypatch.setattr(cli, "run_copy_backup", copy)
    monkeypatch.setattr(cli, "run_restore_all_latest", must_not_run if copy_only else lambda **_: {
        "status": "SUCCESS", "per_database_restore_status": {}})
    monkeypatch.setattr(cli, "run_delete_backup", lambda *a, **k: must_not_run())
    summary = cli.run_restore_workflow(
        restore_configs=[dataclasses.replace(make_config(tmp_path), restore_id="COPY_250")],
        app_config=DbaBrainConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
                                  sqlite_path=tmp_path / "runtime" / "dbabrain.sqlite"),
        copy_only=copy_only)
    return summary, copied_with, catalog


def test_a_copy_only_workflow_copies_and_stops_before_any_restore(tmp_path, monkeypatch):
    summary, copied_with, catalog = _share_driven(tmp_path, monkeypatch, copy_only=True)

    assert summary["status"] == "SUCCESS" and summary["copy_only"] is True
    assert summary["restore-latest"] is None and summary["delete-backup"] is None
    assert [c.action for c in copied_with] == ["copy_only"], "the gate records the run as copy only"
    assert [(c["run"], c["phase"]) for c in catalog.closed if "phase" in c] == [(7, "copied")]


def test_a_copy_only_run_can_never_count_as_a_drill():
    """Drill status and the SLA read backup_restore_history, which only the RESTORE step writes."""
    import inspect

    from dbabrain.backup_restore import cli, restore_database

    assert "start_restore(" in inspect.getsource(restore_database), "the RESTORE step is the writer"
    workflow_source = inspect.getsource(cli.run_restore_workflow)
    assert workflow_source.index("if copy_only:\n") < workflow_source.index("run_restore_all_latest("), \
        "copy only returns before the RESTORE step is reached"


# --------------------------------------------------------------------------- #
# The scheduler does what each entry states
# --------------------------------------------------------------------------- #
def _scheduled(monkeypatch, *, action, copy_only=False):
    import dbabrain.backup_restore.cli as cli

    entry = SimpleNamespace(restore_id="R1", active=True, source_id="ACME-192-0-2-250", target_id="LAB-192-0-2-40",
                            notify={}, time_window=SimpleNamespace(timeout=7200), action=action,
                            restore_from="server")
    monkeypatch.setattr(workflow, "load_restore_configs", lambda _p: [entry])
    calls: list[dict] = []
    replayed: list = []
    monkeypatch.setattr(workflow, "replay_engine_phase", lambda *a, **k: replayed.append(k["phase"]) or ("", {}))
    monkeypatch.setattr(cli, "run_restore_workflow", lambda **kw: calls.append(kw) or {"status": "SUCCESS"})
    summary = workflow.run_scheduled_restores(app_config=SimpleNamespace(sqlite_path=":memory:"),
                                              config_path="x.json", force=True, copy_only=copy_only)
    return summary, calls, replayed


def test_an_entry_stating_copy_only_is_copied_and_says_nothing_was_restored(monkeypatch, harness):  # noqa: F811
    summary, calls, replayed = _scheduled(monkeypatch, action="copy_only")

    assert summary["succeeded"] == 1 and calls[0]["copy_only"] is True
    assert replayed == [], "nothing replayed on the target instance - no logins, no Agent jobs"
    assert harness.events[0]["message"] == "Copy only R1 started."
    assert "Nothing restored" in harness.events[-1]["message"]


def test_an_entry_stating_restore_restores_and_one_run_can_be_made_copy_only(monkeypatch, harness):  # noqa: F811
    _summary, calls, replayed = _scheduled(monkeypatch, action="restore")
    assert calls[0]["copy_only"] is False and replayed

    _summary, calls, replayed = _scheduled(monkeypatch, action="restore", copy_only=True)
    assert calls[0]["copy_only"] is True and replayed == [], "--copy-only wins for that run"


def test_an_entry_stating_no_action_fails_its_own_run_and_nothing_is_copied(monkeypatch, harness):  # noqa: F811
    summary, calls, replayed = _scheduled(monkeypatch, action=None)

    assert summary["failed"] == 1 and calls == [] and replayed == []
    assert "states no action" in harness.events[-1]["error_text"]


# --------------------------------------------------------------------------- #
# The script-driven path (PostgreSQL, Oracle, container SQL Server)
# --------------------------------------------------------------------------- #
def _script_entry(**overrides):
    from dbabrain.lib.time_window import TimeWindow

    return restore_script.ScriptRestore(**{
        "restore_id": "PG_COPY", "db_type": "postgresql", "server_id": "LAB-A", "backup_dir": "/backup",
        "script": "restore.sh", "time_window": TimeWindow(), "target_server_id": "LAB-B",
        "target_backup_dir": "/b/B", "source_backup_host_dir": "/b/A",
        "integrity": I.RestoreIntegrity(), "action": "copy_only", "restore_from": "server", **overrides})


def test_a_script_entry_copied_only_returns_after_the_proven_copy(monkeypatch):
    job = _script_entry()
    seen: dict = {}
    monkeypatch.setattr(restore_script, "load_script_restores", lambda _p=None: [job])
    monkeypatch.setattr(restore_by_id, "_host_block", lambda j, **_: {"host": "h"})
    monkeypatch.setattr(restore_by_id, "assert_target_is_not_source", lambda *a, **k: None)
    monkeypatch.setattr("dbabrain.backup_restore.backup.resolve_ssh_target", lambda *a, **k: SimpleNamespace(host="h"))
    monkeypatch.setitem(restore_by_id._PLANNERS, "postgresql",
                        lambda *a, **k: pytest.fail("a copy-only run planned a restore"))

    def transfer(job, **kwargs):
        seen["action"] = job.action
        return {"copied": 2, "skipped": 0, "bytes_copied": 9, "include": [], "restore_run_id": 11}

    monkeypatch.setattr(restore_script, "transfer_backup_to_target", transfer)
    catalog = _Catalog()

    out = restore_by_id.restore_by_id({"restore_id": "PG_COPY", "copy_only": True}, catalog=catalog)

    assert out["copy_only"] is True and out["steps"] == []
    assert seen["action"] == "copy_only", "the gate records the run as copy only"
    assert [(c["run"], c["phase"]) for c in catalog.closed if "phase" in c] == [(11, "copied")]


def test_copy_only_is_refused_for_an_entry_that_restores_in_place(monkeypatch):
    job = _script_entry(target_server_id="LAB-A", target_container="pg_lab")
    monkeypatch.setattr(restore_script, "load_script_restores", lambda _p=None: [job])

    with pytest.raises(restore_by_id.RestoreByIdError, match="restores in place, and nothing is copied"):
        restore_by_id.restore_by_id({"restore_id": "PG_COPY", "copy_only": True})


# --------------------------------------------------------------------------- #
# restore-workflow on the command line: one run either restores or it does not
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("actions, asked, copy_only", [
    (["restore"], False, False), (["copy_only"], False, True), (["restore"], True, True),
    (["copy_only", "copy_only"], False, True),
])
def test_the_command_line_run_copies_only_when_asked_or_when_every_entry_says_so(actions, asked, copy_only):
    from dbabrain.backup_restore.cli import _decide_copy_only

    configs = [SimpleNamespace(restore_id=f"R{i}", action=a, restore_from="server") for i, a in enumerate(actions)]
    assert _decide_copy_only(configs, asked=asked) is copy_only


def test_entries_that_disagree_or_state_nothing_are_refused_on_the_command_line():
    from dbabrain.backup_restore.cli import _decide_copy_only

    with pytest.raises(ValueError, match="disagree on action"):
        _decide_copy_only([SimpleNamespace(restore_id="A", action="restore", restore_from="server"),
                           SimpleNamespace(restore_id="B", action="copy_only", restore_from="server")], asked=False)
    with pytest.raises(I.IntegrityConfigError, match="states no action"):
        _decide_copy_only([SimpleNamespace(restore_id="A", action=None, restore_from="server")], asked=True)


# --------------------------------------------------------------------------- #
# The bot: /spbot_copy_backup is /spbot_restore with --copy-only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", present_copies("data/telegram_support_commands.json", "data/telegram_support_commands.example.json",
                                  "dbabrain/telegram/catalogue/telegram_support_commands.json"))
def test_the_bot_copies_only_with_the_restores_arguments_and_clearance(path):
    import json
    from pathlib import Path

    commands = {c["command_text"]: c for c in json.loads(Path(path).read_text(encoding="utf-8"))["telegram_support_commands"]}
    copy, restore = commands["spbot_copy_backup"], commands["spbot_restore"]

    assert copy["action_config"]["command_argv"] == restore["action_config"]["command_argv"] + ["--copy-only"]
    assert copy["action_config"]["parameters"] == restore["action_config"]["parameters"]
    assert copy["command_type"] == restore["command_type"], "it writes onto the target's disk: the same clearance"
    assert restore["sort_order"] < copy["sort_order"] < commands["spbot_create_db_docker"]["sort_order"]
