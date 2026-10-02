"""One item that fails in an unexpected way is recorded as failed, and the pass goes on.

Review 0.25.0 found the same shape in four schedulers: the loop caught the errors its author had
seen, and anything else left the loop - every item after it in the pass was skipped, on every pass,
until somebody noticed the silence.
"""

from __future__ import annotations

from types import SimpleNamespace

from db_ops.backup_restore import backup as backup_module
from db_ops.transport.common_cli import CommonCliError
from tests.test_an_async_run_never_repeats_a_job_another_run_finished import _backup_job, _Store


# --------------------------------------------------------------------------- #
# F10.1 - backups
# --------------------------------------------------------------------------- #
def test_a_backup_whose_common_cli_child_crashed_is_an_error_and_the_next_job_runs(monkeypatch):
    first, second = _backup_job("CRASHES"), _backup_job("NEXT")
    executed: list[str] = []
    monkeypatch.setattr(backup_module, "DbOpsStore", _Store)
    monkeypatch.setattr(backup_module, "emit_backup_restore_event", lambda **k: None)
    monkeypatch.setattr(backup_module.schedule, "reap_stale_runs", lambda **k: [])
    monkeypatch.setattr(backup_module, "resolve_backup_target", lambda *a, **k: None)
    monkeypatch.setattr(backup_module, "load_backup_jobs", lambda _p: [first, second])
    monkeypatch.setattr(backup_module, "export_for_backup", lambda *a, **k: None)

    def execute(item, **_k):
        executed.append(item.backup_id)
        if item.backup_id == "CRASHES":
            raise CommonCliError("backup-database printed no JSON (exit -9)")
        return backup_module.BackupRunResult(job=item, status="done", exit_code=0, duration_ms=1,
                                             stdout="RESULT=ok", stderr="", error_text=None)

    monkeypatch.setattr(backup_module, "execute_backup_job", execute)
    summary = backup_module.run_backup(app_config=SimpleNamespace(sqlite_path=":memory:"),
                                       config_path="x", force=True)

    assert executed == ["CRASHES", "NEXT"]
    assert summary["failed"] == 1 and summary["succeeded"] == 1
    assert "RUNNING" not in [row["status"] for row in _Store.rows]


# --------------------------------------------------------------------------- #
# F4.1 - SQL tasks
# --------------------------------------------------------------------------- #
def test_a_task_whose_sql_file_is_missing_is_recorded_as_failed(tmp_path, monkeypatch):
    from test_sql_task_claims import RecordingSqlRunStore, inventory_and_credentials, sql_command, sql_target

    from db_ops.sql_tasks import runner

    monkeypatch.setattr(runner, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "enqueue_sql_task_message", lambda **kwargs: None)
    store = RecordingSqlRunStore()
    inventory, credentials = inventory_and_credentials()
    (tmp_path / "data").mkdir()

    ok = runner.run_one_sql_task(
        store=store, data_dir=tmp_path / "data", telegram_groups={},
        command=sql_command(script_files=("sql/removed.sql",)), target=sql_target(),
        inventory=inventory, credentials=credentials, secrets={}, logger=None)

    assert ok is False
    assert len(store.inserted) == 1, "the run is recorded, so its retry_interval applies"
    assert store.updated[-1]["status"] == "error"
    assert "SQL file not found: sql/removed.sql" in str(store.updated[-1])


def test_one_task_that_raises_does_not_stop_the_scan(tmp_path, monkeypatch):
    from test_sql_task_claims import sql_command, sql_target

    from db_ops.sql_tasks import runner

    first, second = (sql_command(sql_id=1, sql_code="ONE"), sql_target(sql_id=1)), \
        (sql_command(sql_id=2, sql_code="TWO"), sql_target(sql_id=2))
    for name in ("load_sql_commands", "load_sql_targets"):
        monkeypatch.setattr(runner, name, lambda *_a, **_k: {})
    monkeypatch.setattr(runner.data_sources, "load_secret_text", lambda *_a, **_k: {})
    monkeypatch.setattr(runner.data_sources, "load_inventory", lambda *_a, **_k: [])
    monkeypatch.setattr(runner.data_sources, "load_all_credentials", lambda *_a, **_k: {})
    monkeypatch.setattr(runner, "mark_stale_running_sql_runs", lambda **_k: None)
    monkeypatch.setattr(runner, "due_sql_tasks", lambda **_k: [first, second])
    monkeypatch.setattr(runner, "log_event", lambda *args, **kwargs: None)
    ran = []

    def run_one(*, command, **_k):
        ran.append(command.sql_code)
        if command.sql_code == "ONE":
            raise RuntimeError("something nobody planned for")
        return True

    monkeypatch.setattr(runner, "run_one_sql_task", run_one)
    store = SimpleNamespace(
        fetch_running_sql_runs=lambda: [], fetch_latest_done_or_running_sql_runs_by_run_key=lambda: {},
        fetch_latest_sql_runs_by_run_key=lambda: {}, sql_run_started_since=lambda *_a: False)

    result = runner.run_scheduler_scan(store=store, data_dir=tmp_path, dry_run=False,
                                       telegram_groups={}, logger=None)

    assert ran == ["ONE", "TWO"]
    assert (result.error_count, result.success_count) == (1, 1)


# --------------------------------------------------------------------------- #
# F7.1 - scheduled reports
# --------------------------------------------------------------------------- #
def test_one_report_that_raises_does_not_stop_the_reports_after_it(tmp_path, monkeypatch):
    from db_ops.reports import metrics_reports

    monkeypatch.setattr(metrics_reports, "DbOpsStore", lambda _p: object())
    monkeypatch.setattr(metrics_reports, "load_report_configs",
                        lambda *_a, **_k: [{"report_code": "BROKEN"}, {"report_code": "NEXT"}])
    evaluated = []

    def run_one(*, report_config, **_k):
        evaluated.append(report_config["report_code"])
        if report_config["report_code"] == "BROKEN":
            raise KeyError("a field the renderer expected")
        return {"report_code": "NEXT", "created": 1, "queued": 1}

    monkeypatch.setattr(metrics_reports, "_run_one_scheduled_report", run_one)

    outcome = metrics_reports.run_scheduled_reports(sqlite_path=tmp_path / "x.sqlite", telegram_groups={})

    assert evaluated == ["BROKEN", "NEXT"]
    assert outcome["failed"] == ["BROKEN"] and outcome["created"] == 1
