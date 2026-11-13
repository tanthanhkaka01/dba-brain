"""``restore-workflow --restore-id <script-driven entry> --dry-run`` restores nothing (0.37.0).

Why it matters: found 2026-11-10 while preparing a restore drill - the on-demand path of a
script-driven entry never passed ``--dry-run`` on, so a rehearsal ran the restore for real (that
day it stopped only because the target's cmd_access was not enabled yet). A dry run that writes is
worse than none: it is the command an operator runs precisely when they do not want anything done.
"""

from __future__ import annotations

from types import SimpleNamespace

from dbabrain.backup_restore import cli


def test_the_dry_run_reaches_the_runner_and_answers_success(monkeypatch, capsys):
    seen = {}

    def runner(**kwargs):
        seen.update(kwargs)
        return {"dry_run": kwargs.get("dry_run"), "restores": [{"restore_id": "R", "status": "dry-run"}],
                "succeeded": 0, "failed": 0}

    monkeypatch.setattr("dbabrain.backup_restore.workflow.run_scheduled_restores", runner)
    monkeypatch.setattr(cli, "setup_app_logger", lambda *a, **k: None)
    monkeypatch.setattr(cli, "log_function_call", lambda *a, **k: None)
    args = SimpleNamespace(dry_run=True, key=None, key_base64=None, point_in_time="", copy_only=False)

    code = cli._run_script_restore_on_demand(args, app_config=None, config_path="c", restore_id="R", log_scope="s")

    assert seen["dry_run"] is True and seen["restore_id"] == "R"
    assert code == 0 and '"dry-run"' in capsys.readouterr().out


def test_without_the_flag_it_runs_as_before(monkeypatch):
    seen = {}
    monkeypatch.setattr("dbabrain.backup_restore.workflow.run_scheduled_restores",
                        lambda **kw: seen.update(kw) or {"succeeded": 1, "failed": 0})
    monkeypatch.setattr(cli, "setup_app_logger", lambda *a, **k: None)
    monkeypatch.setattr(cli, "log_function_call", lambda *a, **k: None)
    args = SimpleNamespace(dry_run=False, key=None, key_base64=None, point_in_time="", copy_only=False)
    assert cli._run_script_restore_on_demand(args, app_config=None, config_path="c", restore_id="R", log_scope="s") == 0
    assert seen["dry_run"] is False
