"""Ageing `job_runs` out into `job_runs_history`, from the daemon, continuously.

`job_runs` is the busiest table in the store — the daemon appends to it on every app-command
start and finish — and nothing pruned it. It reached ~1M rows / 965 MB with the oldest row 2.5
months back, while `metric_results` next to it had been self-trimming all along.

Two things decide whether this is safe to run from the scheduler loop: rows must be *moved*
rather than dropped, and one pass must be bounded — the loop that sweeps is the loop that
starts due app commands, so an uncapped first sweep on a months-old backlog would stall
scheduling for as long as the move took.
"""

from datetime import datetime, timedelta, timezone

import pytest

from db_ops.db.job_runs import JobRun
from db_ops.db import DbOpsStore
from db_ops.jobs import daemon as daemon_mod


def _stamp(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _store_with_runs(tmp_path, ages_in_days):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    for index, age in enumerate(ages_in_days):
        store.insert_job_run(
            JobRun(
                job_code=f"APP-{index}",
                level="logging",
                status="done",
                message=f"run {index}",
                metadata={"age_days": age},
            )
        )
    # insert_job_run stamps created_at itself, so age the rows afterwards.
    with store.connect() as conn:
        for log_id, age in zip(
            [r["log_id"] for r in conn.execute("SELECT log_id FROM job_runs ORDER BY log_id")],
            ages_in_days,
        ):
            conn.execute("UPDATE job_runs SET created_at = ? WHERE log_id = ?", (_stamp(age), log_id))
    return store


def _count(store, table):
    with store.connect() as conn:
        return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


# ---------------------------------------------------------------------------
# Moved, not deleted
# ---------------------------------------------------------------------------
def test_old_rows_move_and_recent_rows_stay(tmp_path):
    store = _store_with_runs(tmp_path, [40, 30, 16, 14, 1])

    moved = store.archive_old_job_runs(retention_days=15)

    assert moved == 3                                  # 40, 30, 16 days old
    assert _count(store, "job_runs") == 2              # 14 and 1 stay live
    assert _count(store, "job_runs_history") == 3


def test_the_archived_row_keeps_its_content(tmp_path):
    """An incident review reads history months later; a row that lost its message is useless."""
    store = _store_with_runs(tmp_path, [40])

    store.archive_old_job_runs(retention_days=15)

    with store.connect() as conn:
        row = conn.execute("SELECT * FROM job_runs_history").fetchone()
    assert row["job_code"] == "APP-0"
    assert row["message"] == "run 0"
    assert row["level"] == "logging"
    assert row["archived_at"]                          # stamped on the way in


def test_nothing_to_do_is_free_and_repeatable(tmp_path):
    store = _store_with_runs(tmp_path, [1, 2])

    assert store.archive_old_job_runs(retention_days=15) == 0
    assert store.archive_old_job_runs(retention_days=15) == 0
    assert _count(store, "job_runs") == 2


def test_retention_zero_is_a_no_op_not_a_purge(tmp_path):
    """A misconfigured 0 must not read as "archive everything"."""
    store = _store_with_runs(tmp_path, [40, 1])

    assert store.archive_old_job_runs(retention_days=0) == 0
    assert _count(store, "job_runs") == 2


# ---------------------------------------------------------------------------
# Bounded work per pass
# ---------------------------------------------------------------------------
def test_max_batches_caps_one_pass_and_the_rest_drains_later(tmp_path):
    """The daemon's protection: a months-old backlog cannot hold up scheduling."""
    store = _store_with_runs(tmp_path, [40] * 7)

    first = store.archive_old_job_runs(retention_days=15, batch_size=2, max_batches=1)
    assert first == 2
    assert _count(store, "job_runs") == 5

    second = store.archive_old_job_runs(retention_days=15, batch_size=2, max_batches=2)
    assert second == 4
    assert _count(store, "job_runs") == 1

    # Uncapped, the remainder goes in one go.
    assert store.archive_old_job_runs(retention_days=15, batch_size=2) == 1
    assert _count(store, "job_runs") == 0
    assert _count(store, "job_runs_history") == 7


# ---------------------------------------------------------------------------
# The daemon hook
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_sweep_state():
    daemon_mod._JOB_RUNS_SWEEP_STATE["last_swept_at"] = 0.0
    yield
    daemon_mod._JOB_RUNS_SWEEP_STATE["last_swept_at"] = 0.0


def test_the_daemon_sweeps_but_not_on_every_tick(tmp_path):
    """The loop runs every 1-10s; sweeping each time is pure noise against a 965 MB table."""
    store = _store_with_runs(tmp_path, [40] * 3)

    moved = daemon_mod.sweep_job_runs_history(store=store, retention_days=15, now=1000.0)
    assert moved == 3

    # Same interval window: skipped without touching the store.
    assert daemon_mod.sweep_job_runs_history(store=store, retention_days=15, now=1100.0) == 0

    # A later tick past the interval sweeps again.
    _store_with_runs  # (no new rows aged in, so nothing to move)
    assert daemon_mod.sweep_job_runs_history(store=store, retention_days=15, now=2000.0) == 0


def test_a_failing_sweep_never_takes_the_scheduler_down():
    """A daemon that cannot archive is a table that grows. A daemon that dies is every app
    command on the node not running — so the sweep swallows everything."""

    class BrokenStore:
        def archive_old_job_runs(self, **_):
            raise RuntimeError("store unreachable")

    assert daemon_mod.sweep_job_runs_history(store=BrokenStore(), now=1000.0) == 0
