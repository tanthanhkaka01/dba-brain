"""`metrics collect --test-run`: a metric tried on one named target, every row printed, nothing kept.

0.30.0 is about coverage on three engines, and the newest versions the estate does not run yet
(Oracle 23/26ai, PostgreSQL 18, SQL Server 2025) live only on a lab instance. The inventory keeps a
lab switched off so the scheduler never collects it, and a plain `collect` writes into the estate's
store, where a lab's rows would be read by every report as a server of the estate. `--dry-run`
connects to nothing, so it tests nothing.

A test run is the third thing: it reaches the one target it names, switched off or not, runs what
that engine has, prints what each metric graded, and writes nothing. It refuses a target that states
no `major_version`: an unknown version matches every variant and the last one listed answers - for
Oracle often the 8i file - and the first lab run of 0.30.0 reported on exactly that.
"""

from __future__ import annotations

import pytest

from dbabrain.db.metric_results import MetricResult
from dbabrain.lib import errors
from dbabrain.lib.config import DbaBrainConfig
from dbabrain.metrics import collector
from dbabrain.metrics.collector import collect_metrics
from dbabrain.metrics.models import MetricDefinition, MetricTarget

TARGET_ID = "LAB-192-0-2-20-ORA-1521/oracle/LABPDB"


def _target(*, version: int | None = 23) -> MetricTarget:
    return MetricTarget(
        target_id=TARGET_ID, server_id="LAB-192-0-2-20-ORA-1521", ip="192.0.2.20", db_type="oracle",
        db_name="LABPDB", credential_name="lab", platform="linux",
        connection_info={} if version is None else {"major_version": version},
    )


def _definition() -> MetricDefinition:
    return MetricDefinition(metric_code="INSTANCE_STATUS", db_type="oracle", category="availability",
                            default_importance=5, active=True, collector_type="sql", default_timeout=5)


def _row(target: MetricTarget, status: str) -> MetricResult:
    return MetricResult(target_id=target.target_id, server_id=target.server_id, ip=target.ip,
                        db_type=target.db_type, db_name=target.db_name, metric_code="INSTANCE_STATUS",
                        metric_item="instance", metric_value="OPEN", metric_unit=None, status=status,
                        importance=5, message="instance FREE is OPEN", collected_at="2026-10-15T00:00:00Z")


@pytest.fixture
def wired(monkeypatch, tmp_path):
    seen: dict = {"targets_asked": [], "batches": 0}

    def targets(**kwargs):
        seen["targets_asked"].append(kwargs)
        return [seen.get("target") or _target()]

    def batch(*, target, planned, secrets):
        seen["batches"] += 1
        return [[_row(target, "OK")] for _metric, _importance in planned]

    def no_store(*_args, **_kwargs):
        raise AssertionError("a test run opened the store")

    monkeypatch.setattr(collector, "load_metric_definitions", lambda *_, **__: [_definition()])
    monkeypatch.setattr(collector, "load_metric_importance_overrides", lambda *_, **__: [])
    monkeypatch.setattr(collector, "load_metric_targets", targets)
    monkeypatch.setattr(collector.data_sources, "load_secret_text", lambda *_, **__: {})
    monkeypatch.setattr(collector, "load_max_parallel_servers", lambda *_, **__: 1)
    monkeypatch.setattr(collector, "_collect_metric_batch", batch)
    monkeypatch.setattr(collector.MetricStore, "from_config", no_store)
    seen["config"] = DbaBrainConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
                                    sqlite_path=tmp_path / "runtime.sqlite")
    return seen


def test_a_test_run_prints_every_row_and_never_opens_the_store(wired, capsys):
    summary = collect_metrics(config=wired["config"], target_id=TARGET_ID, test_run=True)
    out = capsys.readouterr().out

    assert wired["batches"] == 1
    assert summary.run_id is None and summary.result_count == 1 and summary.ok_count == 1
    assert f"TEST target={TARGET_ID} metric=INSTANCE_STATUS status=OK rows=1" in out
    assert "OK item=instance value=OPEN | instance FREE is OPEN" in out


def test_a_test_run_reaches_the_target_it_names_even_when_it_is_switched_off(wired):
    collect_metrics(config=wired["config"], target_id=TARGET_ID, test_run=True)

    [asked] = wired["targets_asked"]
    assert asked["include_switched_off"] is True
    assert asked["target_id"] == TARGET_ID


def test_a_scheduled_pass_still_skips_a_switched_off_target():
    from dbabrain.lib.data_sources import collection_targets

    assert collection_targets.load_metric_targets.__kwdefaults__["include_switched_off"] is False


def test_a_test_run_names_its_one_target(wired):
    with pytest.raises(errors.InvalidRequest, match="names its one target"):
        collect_metrics(config=wired["config"], test_run=True)


def test_a_test_run_refuses_a_target_that_states_no_version(wired):
    wired["target"] = _target(version=None)

    with pytest.raises(errors.NotConfigured, match="major_version"):
        collect_metrics(config=wired["config"], target_id=TARGET_ID, test_run=True)
    assert wired["batches"] == 0


def test_the_cli_carries_the_flag():
    from dbabrain.metrics.cli import parse_args

    args = parse_args(["collect", "--test-run", "--target-id", TARGET_ID])

    assert args.test_run is True and args.target_id == TARGET_ID
