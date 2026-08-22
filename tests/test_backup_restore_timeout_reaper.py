"""time_window.timeout closes an abandoned run and reports it.

Before this, `timeout` did only half a job: `schedule.is_due` fed it to `job_due` as the
stale grace, so after the timeout the *next* run was allowed to start — but the abandoned
row stayed RUNNING forever and nobody was told. That is exactly the case where telling
someone matters most: a run that dies without raising (daemon kills the process at its own
timeout, container restart, OOM) never reaches the code that emits its failure event, so
the operator sees the last step that succeeded and then silence.

The reaper does not kill anything. Process control stays with the daemon; stopping a
restore mid-RESTORE DATABASE would leave the database RESTORING.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from db_ops.backup_restore import schedule
from db_ops.db.job_runs import JobRun
from db_ops.db.store import DbOpsStore


class _Config:
    def __init__(self, sqlite_path):
        self.sqlite_path = str(sqlite_path)



    @property
    def store(self):
        """SQLite store declaration matching this fake's sqlite_path (see db_ops.config)."""
        from db_ops.config import SqliteStoreConfig, StoreConfig
        from pathlib import Path as _Path

        return StoreConfig(sqlite=SqliteStoreConfig(path=_Path(str(self.sqlite_path))))

@pytest.fixture()
def store(tmp_path):
    return DbOpsStore(str(tmp_path / "runtime.sqlite"))


def _running(store, job_code, *, started: datetime, metadata=None):
    return store.insert_job_run(
        JobRun(
            job_code=job_code,
            level="logging",
            status="RUNNING",
            message="started",
            started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            host_name="worker",
            metadata=metadata or {},
        )
    )


@pytest.fixture()
def read_row(store):
    def _read(log_id):
        with sqlite3.connect(str(store.sqlite_path)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM job_runs WHERE log_id = ?", (log_id,)).fetchone()

    return _read


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)


def test_a_run_abandoned_past_its_timeout_is_closed_and_named(store, read_row):
    """The reported case: ACME_ORA_TO_CLOUD sat at RUNNING for ~14h with timeout=21600."""
    log_id = _running(
        store, schedule.restore_job_code("ACME_ORA_TO_CLOUD"),
        started=NOW - timedelta(hours=14),
        metadata={"restore_id": "ACME_ORA_TO_CLOUD", "source_id": "S", "target_id": "T"},
    )

    reaped = schedule.reap_stale_runs(
        store=store,
        timeouts={schedule.restore_job_code("ACME_ORA_TO_CLOUD"): 21600},
        now=NOW,
    )

    assert [item["restore_id"] for item in reaped] == ["ACME_ORA_TO_CLOUD"]
    row = read_row(log_id)
    assert row["status"] == "TIMEOUT"
    assert row["level"] == "error"
    assert row["finished_at"]                       # no longer open
    assert "restore_id=ACME_ORA_TO_CLOUD" in row["message"]
    assert "timeout=21600s" in row["message"]
    assert json.loads(row["metadata_json"])["elapsed_seconds"] == 14 * 3600


def test_a_run_still_inside_its_timeout_is_left_alone(store, read_row):
    log_id = _running(store, schedule.restore_job_code("R1"), started=NOW - timedelta(minutes=30))

    assert schedule.reap_stale_runs(store=store, timeouts={schedule.restore_job_code("R1"): 7200}, now=NOW) == []
    assert read_row(log_id)["status"] == "RUNNING"


def test_timeout_zero_means_never_time_out(store, read_row):
    """Same convention as everywhere else in time_window: 0 disables the timeout."""
    log_id = _running(store, schedule.restore_job_code("R1"), started=NOW - timedelta(days=5))

    assert schedule.reap_stale_runs(store=store, timeouts={schedule.restore_job_code("R1"): 0}, now=NOW) == []
    assert read_row(log_id)["status"] == "RUNNING"


def test_a_job_code_with_no_config_left_is_not_touched(store, read_row):
    """Its entry was removed from restore_config.json, so no timeout applies to it."""
    log_id = _running(store, schedule.restore_job_code("DELETED_ENTRY"), started=NOW - timedelta(days=5))

    assert schedule.reap_stale_runs(store=store, timeouts={}, now=NOW) == []
    assert read_row(log_id)["status"] == "RUNNING"


def test_a_stale_row_is_reaped_even_after_a_newer_run_overtook_it(store, read_row):
    """After the stale grace elapses the next run starts and inserts its own row, so the
    abandoned one is no longer the latest for its job_code — looking only at the latest row
    would leave it open forever."""
    job_code = schedule.restore_job_code("R1")
    stale_id = _running(store, job_code, started=NOW - timedelta(hours=10))
    fresh_id = _running(store, job_code, started=NOW - timedelta(minutes=5))

    reaped = schedule.reap_stale_runs(store=store, timeouts={job_code: 7200}, now=NOW)

    assert len(reaped) == 1
    assert read_row(stale_id)["status"] == "TIMEOUT"
    assert read_row(fresh_id)["status"] == "RUNNING"      # the live run is untouched


def test_a_backup_job_is_reaped_under_its_backup_id(store, read_row):
    job_code = schedule.backup_job_code("ACME_PG_LAB01_PRIMARY", "wal")
    log_id = _running(store, job_code, started=NOW - timedelta(hours=3),
                      metadata={"backup_id": "ACME_PG_LAB01_PRIMARY", "job": "wal"})

    reaped = schedule.reap_stale_runs(store=store, timeouts={job_code: 3600}, now=NOW)

    assert reaped[0]["backup_id"] == "ACME_PG_LAB01_PRIMARY"
    assert "backup_id=ACME_PG_LAB01_PRIMARY" in read_row(log_id)["message"]


def test_the_id_is_recovered_from_the_job_code_when_the_metadata_lost_it(store, read_row):
    """A row written before ids were required still has to produce an actionable alert; the
    job_code embeds the id by construction."""
    log_id = _running(store, schedule.restore_job_code("R_NO_META"), started=NOW - timedelta(hours=10))

    reaped = schedule.reap_stale_runs(store=store, timeouts={schedule.restore_job_code("R_NO_META"): 7200}, now=NOW)

    assert reaped[0]["restore_id"] == "R_NO_META"
    assert "restore_id=R_NO_META" in read_row(log_id)["message"]


def test_reaping_pushes_a_critical_alert_naming_the_run(store, tmp_path, monkeypatch):
    """The whole point: the operator hears about it. Without this the run simply vanished."""
    monkeypatch.setattr("db_ops.lib.telegram_route.telegram_route",
                        lambda level, **_: {"enabled": True, "alert": True, "chat_id": 42})
    job_code = schedule.restore_job_code("ACME_TO_MSSQL2025_DOCKER")
    _running(store, job_code, started=NOW - timedelta(hours=4),
             metadata={"restore_id": "ACME_TO_MSSQL2025_DOCKER", "target_id": "MSSQL2025-DOCKER"})

    schedule.reap_stale_runs(
        store=store, app_config=_Config(store.sqlite_path), timeouts={job_code: 7200}, now=NOW,
    )

    with sqlite3.connect(str(tmp_path / "runtime.sqlite")) as conn:
        text, source_id = conn.execute(
            "SELECT message_text, source_id FROM telegram_send_messages ORDER BY send_tlgmsg_id DESC LIMIT 1"
        ).fetchone()

    assert "CRITICAL" in text
    assert "restore_id=ACME_TO_MSSQL2025_DOCKER" in text
    assert "timeout=7200s" in text
    # The run's own command: a timeout happened *to* this run, so it belongs to the same
    # source_id an operator already follows. What distinguishes it is the phase, not a second
    # command name - steps are phases here, never command suffixes.
    assert source_id == "restore-workflow:ACME_TO_MSSQL2025_DOCKER"


def test_reaping_runs_before_the_due_check_in_the_scheduler(tmp_path, monkeypatch):
    """A stale row must be closed in the same cycle that notices it, not left for whenever
    someone next reads the table."""
    from db_ops.backup_restore import workflow as wf

    calls: list[str] = []
    monkeypatch.setattr(wf, "load_restore_configs", lambda _p: [])
    monkeypatch.setattr(wf, "load_script_restores", lambda _p: [])
    monkeypatch.setattr(schedule, "reap_stale_runs", lambda **kw: calls.append("reap") or [])
    monkeypatch.setattr(wf, "select_due_restores", lambda **kw: calls.append("due") or [])

    class _Store:

        @classmethod
        def from_config(cls, config, **kwargs):
            """Store doubles must offer the same constructor contract as the real classes."""
            return cls(getattr(config, 'sqlite_path', None))
        def __init__(self, *_a, **_k): pass
        def fetch_latest_job_runs_by_job_code(self): return {}
        def fetch_running_job_runs(self, _prefix=""): return []

    monkeypatch.setattr(wf, "DbOpsStore", _Store)

    wf.run_scheduled_restores(app_config=_Config(tmp_path / "r.sqlite"), config_path="x.json")

    assert calls == ["reap", "due"]
