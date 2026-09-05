"""Carrying a stand-in node's history into the shared store, without inventing links.

Every primary key involved is an identity column, and a store that started at 1 has ids that are
already taken in a store with millions of rows. So nothing carries its key — and that turns the
*links* into the whole problem. One of them is a real foreign key and would be refused; the others
are plain integers that would be accepted and be wrong, pointing at whatever row in the
destination happens to hold that number. The second kind is worse, because nothing reports it.

These tests are two SQLite stores rather than one of each backend: the suite is offline by design,
and what is being checked is the remapping, the watermark and the ordering — none of which is
engine-specific. The statements themselves are the store's own, which both engines already run.
"""

from datetime import datetime, timedelta, timezone

import pytest

from db_ops.db import DbOpsStore, backfill
from db_ops.db.job_runs import JobRun
from db_ops.db.metric_store import MetricStore


def _stamp(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _store(tmp_path, name):
    store = DbOpsStore(tmp_path / f"{name}.sqlite")
    store.initialize()
    MetricStore(tmp_path / f"{name}.sqlite").initialize()
    return store


def _job_run(store, *, code, minutes_ago):
    log_id = store.insert_job_run(JobRun(job_code=code, level="logging", status="done",
                                         message="ran"))
    with store.connect() as conn:
        conn.execute("UPDATE job_runs SET created_at = ? WHERE log_id = ?",
                     (_stamp(minutes_ago), log_id))
    return log_id


def _metric_run(store, *, minutes_ago):
    with store.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO metric_runs (started_at, status, message) VALUES (?, 'DONE', 'x')",
            (_stamp(minutes_ago),))
        return int(cursor.lastrowid)


def _metric_result(store, *, run_id, code, minutes_ago):
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO metric_results (run_id, target_id, server_id, metric_code, status, "
            "collected_at) VALUES (?, 't', 's', ?, 'OK', ?)",
            (run_id, code, _stamp(minutes_ago)))


def _count(store, table):
    with store.connect() as conn:
        return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


# ---------------------------------------------------------------------------
# Only what is missing
# ---------------------------------------------------------------------------
def test_only_rows_newer_than_the_destination_are_carried(tmp_path):
    """The watermark is the destination's own newest row, so the window is the outage rather than
    the whole of the stand-in's history."""
    source = _store(tmp_path, "source")
    destination = _store(tmp_path, "destination")
    _job_run(destination, code="APP-OLD", minutes_ago=120)
    _job_run(source, code="APP-OLD-COPY", minutes_ago=130)      # older than the watermark
    _job_run(source, code="APP-NEW", minutes_ago=30)            # inside the gap

    outcome = backfill.apply(sqlite_path=tmp_path / "source.sqlite", store=destination)

    assert outcome["inserted"]["job_runs"] == 1
    with destination.connect() as conn:
        codes = [r["job_code"] for r in conn.execute("SELECT job_code FROM job_runs ORDER BY log_id")]
    assert codes == ["APP-OLD", "APP-NEW"]


def test_running_it_twice_carries_nothing_the_second_time(tmp_path):
    """The apply has to be safe to repeat: an interrupted run is finished by running it again."""
    source = _store(tmp_path, "source")
    destination = _store(tmp_path, "destination")
    _job_run(source, code="APP-A", minutes_ago=30)

    first = backfill.apply(sqlite_path=tmp_path / "source.sqlite", store=destination)
    second = backfill.apply(sqlite_path=tmp_path / "source.sqlite", store=destination)

    assert first["total"] == 1
    assert second["total"] == 0
    assert _count(destination, "job_runs") == 1


# ---------------------------------------------------------------------------
# The links, which is the whole reason this is not a copy
# ---------------------------------------------------------------------------
def test_a_child_points_at_the_parent_it_arrived_with_not_at_the_number_it_carried(tmp_path):
    """The defect this module exists to prevent. The destination already has a `metric_runs` row,
    so the source's run 1 must not stay run 1 - that number belongs to somebody else here."""
    source = _store(tmp_path, "source")
    destination = _store(tmp_path, "destination")
    _metric_run(destination, minutes_ago=200)                    # takes id 1 in the destination
    run = _metric_run(source, minutes_ago=30)                    # also id 1, in the source
    _metric_result(source, run_id=run, code="CPU", minutes_ago=29)

    backfill.apply(sqlite_path=tmp_path / "source.sqlite", store=destination)

    with destination.connect() as conn:
        carried = conn.execute(
            "SELECT run_id FROM metric_results WHERE metric_code = 'CPU'").fetchone()
        new_run = conn.execute(
            "SELECT max(run_id) AS hi FROM metric_runs").fetchone()
    assert carried["run_id"] == new_run["hi"], "the row follows its own run, not the id it held"
    assert carried["run_id"] != run, "and that is not the id it arrived with"


def test_a_row_whose_required_parent_predates_the_window_is_left_behind_and_counted(tmp_path):
    """The parent is already in the destination under an id this run never saw, and
    `metric_results.run_id` will not take NULL - so the row cannot cross without being attached to
    a run, and the only run available is the wrong one. It stays behind, and the count says so.

    This is the run that was in flight when the outage began: rare, and a stated loss rather than
    a silent lie about which collection a measurement belongs to."""
    source = _store(tmp_path, "source")
    destination = _store(tmp_path, "destination")
    _metric_run(destination, minutes_ago=300)
    old_run = _metric_run(source, minutes_ago=300)               # older than the watermark
    _metric_result(source, run_id=old_run, code="MEM", minutes_ago=20)   # but the result is new

    outcome = backfill.apply(sqlite_path=tmp_path / "source.sqlite", store=destination)

    assert outcome["left_behind"].get("metric_results") == 1
    assert outcome["inserted"]["metric_results"] == 0
    assert _count(destination, "metric_results") == 0, "never attached to the wrong run"


def test_an_optional_link_that_cannot_be_remapped_only_costs_the_link(tmp_path):
    """`reports.telegram_send_message_id` is nullable, so the report crosses without its message
    rather than not at all - the report is the record, the link is a convenience."""
    source = _store(tmp_path, "source")
    destination = _store(tmp_path, "destination")
    report_id = source.insert_report(
        report_code="rp_daily", report_name="Daily", report_type="BACKUP_HEALTH",
        report_level="logging", report_text="body")
    with source.connect() as conn:
        conn.execute("UPDATE reports SET telegram_send_message_id = 9999, created_at = ? "
                     "WHERE report_id = ?", (_stamp(20), report_id))

    outcome = backfill.apply(sqlite_path=tmp_path / "source.sqlite", store=destination)

    assert outcome["inserted"]["reports"] == 1
    assert outcome["unlinked"].get("reports") == 1
    with destination.connect() as conn:
        row = conn.execute("SELECT telegram_send_message_id FROM reports").fetchone()
    assert row["telegram_send_message_id"] is None, "dropped, not guessed at"


def test_a_parent_is_carried_before_the_children_that_name_it(tmp_path):
    """Ordering is not cosmetic: `sla_results.sla_run_id` is a real foreign key, and a child
    inserted first is refused outright."""
    tables = [spec.name for spec in backfill.TABLES]

    assert tables.index("metric_runs") < tables.index("metric_results")
    assert tables.index("sla_runs") < tables.index("sla_results")
    assert tables.index("telegram_send_messages") < tables.index("reports")


def test_current_state_is_not_carried_at_all(tmp_path):
    """`target_health` is what the estate looks like now, rebuilt by the next metrics run. Carrying
    it would describe the estate as the stand-in last saw it."""
    assert "target_health" not in [spec.name for spec in backfill.TABLES]


# ---------------------------------------------------------------------------
# The plan, which is what anyone reads before running the apply
# ---------------------------------------------------------------------------
def test_the_plan_names_the_watermark_each_table_starts_from(tmp_path):
    source = _store(tmp_path, "source")
    destination = _store(tmp_path, "destination")
    _job_run(destination, code="APP-OLD", minutes_ago=120)
    _job_run(source, code="APP-NEW", minutes_ago=30)

    plans = {item.table: item for item in backfill.plan(
        sqlite_path=tmp_path / "source.sqlite", store=destination)}

    assert plans["job_runs"].carried == 1
    assert plans["job_runs"].watermark, "the destination's own newest row"
    assert plans["job_runs"].source_rows == 1


def test_the_plan_writes_nothing(tmp_path):
    source = _store(tmp_path, "source")
    destination = _store(tmp_path, "destination")
    _job_run(source, code="APP-A", minutes_ago=30)

    backfill.plan(sqlite_path=tmp_path / "source.sqlite", store=destination)

    assert _count(destination, "job_runs") == 0


def test_a_source_that_is_not_there_is_refused_by_name(tmp_path):
    destination = _store(tmp_path, "destination")

    with pytest.raises(backfill.BackfillError) as refusal:
        backfill.plan(sqlite_path=tmp_path / "no-such.sqlite", store=destination)

    assert "no-such.sqlite" in str(refusal.value)
