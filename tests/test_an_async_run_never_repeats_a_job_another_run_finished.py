"""An overlapping async run never repeats a job another run has already done.

APP-BACKUP-RESTORE is ``async``: ``max_parallel`` 4, due every second since 0.24.0 (30 s before). A run lists the due jobs,
then works through them one by one; 30 s later the daemon starts another, which takes whatever is
due and not running - the jobs the first has not reached yet. That is the concurrency the schedule
test of 2026-09-25 measured on the labs: the second run took the Oracle restore while the first
was still restoring PostgreSQL.

The RUNNING claim stops the two from overlapping on one job. It did not stop the first run from
running a job again once the second had *finished* it: driving the real ``run_backup`` gave
``['LONG_A', 'SHORT_B (by the other run)', 'SHORT_B']``. Before claiming each job, a scheduled run
now asks whether another run has started it since the list was read (``schedule.taken_since``).
An explicit ``--force`` still runs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from db_ops.backup_restore import backup as backup_module
from db_ops.backup_restore import schedule, workflow
from db_ops.backup_restore.server_metadata import ServerMetadataPlan
from db_ops.db.store import RunAlreadyClaimed, utc_now_text
from db_ops.lib.time_window import TimeWindow

from conftest import patch_sql_runner


class _Store:
    """job_runs with the store's claim rule: one RUNNING row per claim key, rows read as mappings."""

    rows: list[dict] = []

    @classmethod
    def from_config(cls, config, **kwargs):
        return cls()

    def fetch_latest_job_runs_by_job_code(self):
        latest: dict[str, dict] = {}
        for row in _Store.rows:
            latest[row["job_code"]] = row
        return {code: dict(row) for code, row in latest.items()}

    def fetch_running_job_runs(self, _prefix=""):
        return []

    def job_run_started_since(self, job_code, since):
        return any(r["job_code"] == job_code and r["started_at"] >= since for r in _Store.rows)

    def insert_job_run(self, run):
        if any(r["claim_key"] == run.claim_key and r["status"] == "RUNNING" for r in _Store.rows):
            raise RunAlreadyClaimed(run.claim_key)
        _Store.rows.append({"job_code": run.job_code, "claim_key": run.claim_key, "status": "RUNNING",
                            "started_at": run.started_at, "log_id": len(_Store.rows) + 1})
        return len(_Store.rows)

    def update_job_run(self, *, log_id, status, **kwargs):
        _Store.rows[log_id - 1]["status"] = status


@pytest.fixture(autouse=True)
def _fresh_store():
    _Store.rows = []


def _other_run_does(job_code: str) -> None:
    """What the overlapping run does meanwhile: claim the job, run it, finish it."""
    store = _Store()
    log_id = store.insert_job_run(SimpleNamespace(job_code=job_code, claim_key=job_code,
                                                  started_at=utc_now_text()))
    store.update_job_run(log_id=log_id, status="DONE")


def _backup_job(backup_id):
    return backup_module.BackupJob(
        backup_id=backup_id, job="full", db_type="postgresql", server_id="S", script="x.sh",
        backup_dir="/b", cleanup_retention=1, time_window=TimeWindow(),
        server_metadata=ServerMetadataPlan())


def _run_backups(monkeypatch, *, force):
    long_a, short_b = _backup_job("LONG_A"), _backup_job("SHORT_B")
    executed: list[str] = []
    monkeypatch.setattr(backup_module, "DbOpsStore", _Store)
    monkeypatch.setattr(backup_module, "emit_backup_restore_event", lambda **k: None)
    monkeypatch.setattr(backup_module.schedule, "reap_stale_runs", lambda **k: [])
    monkeypatch.setattr(backup_module, "resolve_backup_target", lambda *a, **k: None)
    monkeypatch.setattr(backup_module, "load_backup_jobs", lambda _p: [long_a, short_b])
    monkeypatch.setattr(backup_module, "export_for_backup", lambda *a, **k: None)

    def execute(item, **k):
        executed.append(item.backup_id)
        if item.backup_id == "LONG_A":
            _other_run_does(short_b.job_code)
        return backup_module.BackupRunResult(job=item, status="done", exit_code=0, duration_ms=1,
                                             stdout="RESULT=ok", stderr="", error_text=None)

    monkeypatch.setattr(backup_module, "execute_backup_job", execute)
    summary = backup_module.run_backup(app_config=SimpleNamespace(sqlite_path=":memory:"),
                                       config_path="x", force=force)
    return executed, summary


def test_a_scheduled_backup_does_not_repeat_a_job_another_run_finished(monkeypatch):
    executed, summary = _run_backups(monkeypatch, force=False)

    assert executed == ["LONG_A"], "SHORT_B had been done by the overlapping run"
    assert summary["taken_by_another_run"] == [{"backup_id": "SHORT_B", "job": "full"}]


def test_an_explicit_force_still_runs_what_it_was_asked_to(monkeypatch):
    executed, _summary = _run_backups(monkeypatch, force=True)

    assert executed == ["LONG_A", "SHORT_B"]


def test_a_scheduled_restore_does_not_repeat_a_restore_another_run_finished(monkeypatch):
    jobs = [SimpleNamespace(restore_id=rid, label=rid, active=True, db_type="postgresql", server_id="S",
                            target_container="c", target_server_id="T", notify={},
                            job_code=schedule.restore_job_code(rid),
                            time_window=SimpleNamespace(timeout=7200))
            for rid in ("LONG_ORA", "SHORT_PG")]
    restored: list[str] = []
    monkeypatch.setattr(workflow, "DbOpsStore", _Store)
    monkeypatch.setattr(workflow, "emit_backup_restore_event", lambda **k: None)
    monkeypatch.setattr(workflow.schedule, "reap_stale_runs", lambda **k: [])
    monkeypatch.setattr(workflow, "load_restore_configs", lambda _p: [])
    monkeypatch.setattr(workflow, "load_script_restores", lambda _p: jobs)
    monkeypatch.setattr(workflow, "select_due_script_restores", lambda *, jobs, latest_runs: list(jobs))

    def restore(request, **kwargs):
        restored.append(request["restore_id"])
        if request["restore_id"] == "LONG_ORA":
            _other_run_does(jobs[1].job_code)
        return {"restore_id": request["restore_id"], "db_type": "postgresql", "steps": []}

    monkeypatch.setattr("db_ops.backup_restore.restore_by_id.restore_by_id", restore)

    summary = workflow.run_scheduled_restores(app_config=SimpleNamespace(sqlite_path=":memory:"),
                                              config_path="x.json")

    assert restored == ["LONG_ORA"], "SHORT_PG had been restored by the overlapping run"
    assert {"restore_id": "SHORT_PG", "db_type": "postgresql", "status": "taken-by-another-run"} in summary["restores"]


def test_a_scheduled_sql_scan_does_not_repeat_a_task_another_scan_finished(tmp_path, monkeypatch):
    """APP-SQL_TASKS is async too, and a repeated task there is the same SQL run twice against a
    production target. While this scan runs the slow task 1, the next scan takes task 2 and finishes
    it; when this scan reaches task 2, it must see that."""
    import json

    from db_ops.sql_tasks import runner

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    commands = [{"sql_id": n, "sql_code": f"SQLSERVER-00{n}", "sql_name": f"task {n}", "db_type": "sqlserver",
                 "script_type": "single", "script_path": "sql/tasks/sqlserver/test.sql", "active": True}
                for n in (1, 2)]
    targets = [{"sql_id": n, "target_no": 1, "output": {"format": "plain", "telegram_chat": "sql", "chat_id": ""},
                "notify": {"logging_on_run": {"enabled": True, "telegram_chat": "sql"},
                           "alert_on_error": {"enabled": True, "telegram_chat": "sql"}},
                "server_id": "server", "db_type": "sqlserver", "service_name": "svc", "instance_name": "inst",
                "credential_name": "cred", "time_window": {"repeat_interval": 60}, "active": True}
               for n in (1, 2)]
    (data_dir / "sql_commands.json").write_text(json.dumps({"sql_commands": commands}), encoding="utf-8")
    (data_dir / "sql_targets.json").write_text(json.dumps({"sql_targets": targets}), encoding="utf-8")
    started: dict[str, str] = {}

    class _SqlStore:
        def fetch_latest_done_or_running_sql_runs_by_run_key(self):
            return {}

        def fetch_latest_sql_runs_by_run_key(self):
            return {}

        def fetch_running_sql_runs(self):
            return []

        def sql_run_started_since(self, run_key, since):
            return started.get(run_key, "") >= since

    executed: list[int] = []

    def run_one(**kwargs):
        executed.append(kwargs["command"].sql_id)
        if kwargs["command"].sql_id == 1:
            other_key = next(t for t in runner.load_sql_targets(data_dir / "sql_targets.json")
                             if t.sql_id == 2).run_key
            started[other_key] = utc_now_text()        # the other scan ran task 2 meanwhile
        return True

    patch_sql_runner(monkeypatch, "mark_stale_running_sql_runs", lambda **kwargs: None)
    monkeypatch.setattr(runner.data_sources, "load_secret_text", lambda _dir: {})
    monkeypatch.setattr(runner.data_sources, "load_inventory", lambda _dir: [])
    monkeypatch.setattr(runner.data_sources, "load_all_credentials", lambda _dir: {})
    patch_sql_runner(monkeypatch, "run_one_sql_task", run_one)

    result = runner.run_scheduler_scan(store=_SqlStore(), data_dir=data_dir, dry_run=False,
                                       telegram_groups={}, logger=None)

    assert executed == [1], "task 2 had been run by the other scan"
    assert (result.success_count, result.skipped_count) == (1, 1)
