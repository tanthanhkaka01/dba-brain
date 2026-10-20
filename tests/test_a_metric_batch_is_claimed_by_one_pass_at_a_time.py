"""A metric batch runs in one pass at a time, and a pass with nothing to run costs one query.

Until 0.30.0 a collect pass ran one server's metrics one after another and ended with its slowest
server, and the daemon started no other pass meanwhile (`APP-METRICS` was `sync`). On 2026-10-16 the
nightly index-fragmentation scan took 1009 s on the largest instance and held INSTANCE_STATUS of
every server for ~18 minutes. Now a pass runs one batch per server and category, the batches at once,
and the app command is `run_mode: async` - the daemon starts the next pass a second later while a
slow one runs.

An async app must refuse its own duplicates (`dbabrain/lib/run_mode.py`): eight duplicate production
SQL runs and a second restore 47 minutes into the first are why. So a batch is claimed with a lock
on a file of this node, a held claim makes the next pass skip that batch, and the pass that takes a
claim reads the batch's intervals again - the reading it planned from predates the claim, and the
pass that held it may have collected those metrics a moment ago.

A pass starts every second now, so one that finds nothing due writes nothing - no run row, no
archive, no health rebuild - after one query for every interval. The store has no connection pool,
and the old due check spent two connections on every (target, metric) pair: ~1,600 a pass.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from dbabrain.db.metric_results import MetricResult
from dbabrain.db.metric_store import MetricStore
from dbabrain.lib.config import DbaBrainConfig
from dbabrain.lib.file_lock import FileLock, LockHeld
from dbabrain.metrics import collector
from dbabrain.metrics.collector import collect_metrics
from dbabrain.metrics.models import MetricDefinition, MetricTarget


def _target(server_id: str = "server-a") -> MetricTarget:
    return MetricTarget(target_id=f"{server_id}/sqlserver/db", server_id=server_id, ip="127.0.0.1",
                        db_type="sqlserver", db_name="db", credential_name="test", platform="windows")


def _definition(metric_code: str = "INSTANCE_STATUS", category: str = "availability") -> MetricDefinition:
    return MetricDefinition(metric_code=metric_code, db_type="sqlserver", category=category,
                            default_importance=5, active=True, collector_type="sql",
                            default_timeout=5, interval_seconds=60)


def _config(tmp_path: Path) -> DbaBrainConfig:
    return DbaBrainConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
                          sqlite_path=tmp_path / "runtime.sqlite")


def _result(metric_code: str, status: str, collected_at: str, message: str = "") -> MetricResult:
    return MetricResult(target_id="target-1", server_id="server-1", ip="127.0.0.1", db_type="sqlserver",
                        db_name="db", metric_code=metric_code, metric_item=None, metric_value=None,
                        metric_unit=None, status=status, importance=5, message=message,
                        collected_at=collected_at)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Store:
    """Records what a pass asked of the store; every interval reads as `latest`."""

    def __init__(self, *latest: tuple[str | None, str | None]) -> None:
        self.latest = list(latest)
        self.calls: list[str] = []

    def latest_result_times(self, *, pairs):
        self.calls.append("latest_result_times")
        answer = self.latest.pop(0) if len(self.latest) > 1 else self.latest[0]
        return {pair: answer for pair in pairs}

    def archive_old_results(self, **_):
        self.calls.append("archive_old_results")
        return 0

    def start_run(self, **_):
        self.calls.append("start_run")
        return 7

    def insert_results(self, *, run_id, results):
        return len(results)

    def finish_run(self, **_):
        self.calls.append("finish_run")

    def rebuild_target_health(self, **_):
        self.calls.append("rebuild_target_health")
        return 0


@pytest.fixture()
def wired(monkeypatch):
    collected: list[str] = []

    def wire(*, store, definitions, targets=None):
        monkeypatch.setattr(collector, "load_metric_definitions", lambda *_, **__: definitions)
        monkeypatch.setattr(collector, "load_metric_importance_overrides", lambda *_, **__: [])
        monkeypatch.setattr(collector, "load_metric_targets", lambda **_: targets or [_target()])
        monkeypatch.setattr(collector.data_sources, "load_secret_text", lambda *_, **__: {})
        monkeypatch.setattr(collector, "load_max_parallel_servers", lambda *_, **__: 16)
        monkeypatch.setattr(collector.MetricStore, "from_config", lambda *_: store)
        monkeypatch.setattr(collector, "_collect_metric_batch",
                            lambda *, target, planned, secrets: [collected.append(metric.metric_code) or []
                                                                 for metric, _ in planned])
        return collected

    return wire


def test_a_batch_another_pass_holds_is_skipped_and_counted(tmp_path, wired):
    collected = wired(store=_Store((None, None)), definitions=[_definition()])
    config = _config(tmp_path)

    with FileLock(config.runtime_dir / "metric_claims" / "server-a__availability", wait=False):
        summary = collect_metrics(config=config)

    assert collected == []
    assert summary.skipped_claimed_count == 1
    assert summary.executed_count == 0
    assert "SKIP claimed server=server-a category=availability" in summary.message


def test_the_claim_is_released_when_the_batch_ends(tmp_path, wired):
    collected = wired(store=_Store((None, None)), definitions=[_definition()])
    config = _config(tmp_path)

    collect_metrics(config=config)

    assert collected == ["INSTANCE_STATUS"]
    with FileLock(config.runtime_dir / "metric_claims" / "server-a__availability", wait=False):
        pass


def test_a_metric_the_previous_holder_just_collected_is_not_run_again(tmp_path, wired):
    # Due when the pass planned; collected a moment ago by the time this pass holds the claim.
    store = _Store((None, None), (_now(), _now()))
    collected = wired(store=store, definitions=[_definition()])

    summary = collect_metrics(config=_config(tmp_path))

    assert collected == []
    assert store.calls.count("latest_result_times") == 2
    assert summary.skipped_interval_count == 1


def test_a_pass_with_nothing_due_writes_nothing_after_one_query(tmp_path, wired):
    store = _Store((_now(), _now()))
    wired(store=store, definitions=[_definition(), _definition("DATABASE_STATUS")])

    summary = collect_metrics(config=_config(tmp_path))

    assert summary.run_id is None
    assert store.calls == ["latest_result_times"]


def test_two_categories_of_one_server_are_two_claims(tmp_path, wired):
    collected = wired(store=_Store((None, None)),
                      definitions=[_definition(), _definition("MAINTENANCE_INDEX_FRAGMENTATION", "maintenance")])
    config = _config(tmp_path)

    with FileLock(config.runtime_dir / "metric_claims" / "server-a__maintenance", wait=False):
        summary = collect_metrics(config=config)

    assert collected == ["INSTANCE_STATUS"]
    assert summary.skipped_claimed_count == 1


def test_a_claim_name_keeps_only_what_a_file_name_can_hold():
    assert collector._safe_claim_name("A1A-10-0-0-1/sqlserver/TAC Time") == "A1A-10-0-0-1_sqlserver_TAC_Time"


def test_a_lock_that_does_not_wait_refuses_while_held_and_takes_it_after(tmp_path):
    path = tmp_path / "claim"
    with FileLock(path, wait=False):
        with pytest.raises(LockHeld):
            with FileLock(path, wait=False):
                pass
    with FileLock(path, wait=False):
        pass


def test_one_query_reads_what_the_two_per_pair_queries_read(tmp_path):
    store = MetricStore(tmp_path / "store.sqlite")
    store.initialize()
    run_id = store.start_run(started_at="2026-10-16T00:00:00Z")
    store.insert_results(run_id=run_id, results=[
        _result("METRIC_A", "OK", "2026-10-16T01:00:00Z"),
        _result("METRIC_A", "WARNING", "2026-10-16T02:00:00Z", "SQL execution failed: timeout"),
        _result("METRIC_A", "ERROR", "2026-10-16T03:00:00Z"),
        _result("METRIC_B", "OK", "2026-10-16T02:30:00Z"),
    ])
    pairs = [("target-1", "METRIC_A"), ("target-1", "METRIC_B"), ("target-2", "NEVER_COLLECTED")]

    batch = store.latest_result_times(pairs=pairs)

    assert batch == {pair: (store.latest_result_time(target_id=pair[0], metric_code=pair[1]),
                            store.latest_successful_result_time(target_id=pair[0], metric_code=pair[1]))
                     for pair in pairs}
    assert batch[("target-1", "METRIC_A")] == ("2026-10-16T03:00:00Z", "2026-10-16T01:00:00Z")
    assert store.latest_result_times(pairs=[]) == {}


def test_the_health_rebuild_reads_no_row_older_than_its_pass(tmp_path):
    store = MetricStore(tmp_path / "store.sqlite")
    store.initialize()
    run_id = store.start_run(started_at="2026-10-16T02:00:00Z")
    store.insert_results(run_id=run_id, results=[_result("METRIC_A", "CRITICAL", "2026-10-16T02:00:05Z")])

    assert store.rebuild_target_health(run_id=run_id, since="2026-10-16T02:00:00Z") == 1
    assert store.rebuild_target_health(run_id=run_id, since="2026-10-16T03:00:00Z") == 0
