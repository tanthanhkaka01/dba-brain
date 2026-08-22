"""How a collect pass is ordered: parallel across servers, serial inside one.

A pass used to walk every target in a single queue, so its cadence was the sum of all of them
and one unresponsive host set the pace for the estate. That is not a tidiness problem: a WinRM
call to an ERP application server was measured holding a pass for 490 of its 598 seconds, and
during that window the ERP *database* went unsampled — a lock chain grew from 21s to 570s and no
CRITICAL was ever raised, because the metric that would have raised it never got to run.

Fanning out per target would trade that for a different fault: several targets can name the same
machine (a database instance and the OS metrics reached through its ``cmd_access``), and stacking
two WinRM/SSH sessions or two metric queries on one box is exactly what a monitoring tool must
not do. So the unit of parallelism is ``server_id``, and these tests pin both halves of that.
"""

import threading
import time

import pytest

from db_ops.config import DbOpsConfig
from db_ops.metrics.collector import collect_metrics
from db_ops.metrics.definitions import load_max_parallel_servers
from db_ops.metrics.models import MetricDefinition, MetricTarget


def _target(server_id, suffix=""):
    return MetricTarget(
        target_id=f"{server_id}/sqlserver/db{suffix}",
        server_id=server_id,
        ip="127.0.0.1",
        db_type="sqlserver",
        db_name="db",
        credential_name="test",
        platform="windows",
    )


def _definition(metric_code="INSTANCE_STATUS"):
    return MetricDefinition(
        metric_code=metric_code,
        db_type="sqlserver",
        category="availability",
        default_importance=5,
        active=True,
        collector_type="sql",
        default_timeout=5,
    )


def _config(tmp_path):
    return DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime.sqlite",
    )


def _wire(monkeypatch, *, targets, definitions, collect_one, max_parallel):
    monkeypatch.setattr("db_ops.metrics.collector.load_metric_definitions", lambda *_, **__: definitions)
    monkeypatch.setattr("db_ops.metrics.collector.load_metric_importance_overrides", lambda *_, **__: [])
    monkeypatch.setattr("db_ops.metrics.collector.load_metric_targets", lambda **_: targets)
    monkeypatch.setattr("db_ops.metrics.collector.data_sources.load_secret_text", lambda *_, **__: {})
    monkeypatch.setattr("db_ops.metrics.collector.load_max_parallel_servers", lambda *_, **__: max_parallel)
    monkeypatch.setattr("db_ops.metrics.collector._collect_one_metric", collect_one)


def test_two_metrics_of_one_server_are_never_in_flight_at_the_same_time(tmp_path, monkeypatch):
    """The guarantee that makes the fan-out safe: one machine is still touched one call at a time.

    Two targets here share a server_id — the shape of a database instance plus the OS metrics
    read over the same host's cmd_access.
    """
    lock = threading.Lock()
    in_flight: dict[str, int] = {}
    overlaps: list[str] = []

    def collect_one(**kwargs):
        server_id = kwargs["target"].server_id
        with lock:
            if in_flight.get(server_id):
                overlaps.append(server_id)
            in_flight[server_id] = in_flight.get(server_id, 0) + 1
        time.sleep(0.05)
        with lock:
            in_flight[server_id] -= 1
        return []

    _wire(
        monkeypatch,
        targets=[_target("server-a", "-inst"), _target("server-a", "-os"), _target("server-b")],
        definitions=[_definition("INSTANCE_STATUS"), _definition("OS_CPU_USAGE")],
        collect_one=collect_one,
        max_parallel=16,
    )

    collect_metrics(config=_config(tmp_path), dry_run=False, force=True)

    assert overlaps == []


def test_a_slow_server_no_longer_holds_the_others_behind_it(tmp_path, monkeypatch):
    """The reason for the change: the fast server must get its turn *while* the slow one hangs.

    The slow worker only returns once the fast one has been served, so a serial pass cannot
    satisfy this — it would have to finish the slow server first, and the fast server would then
    find ``slow_finished`` already set.
    """
    slow_started = threading.Event()
    slow_finished = threading.Event()
    fast_done = threading.Event()
    ran_while_slow_hung = []

    def collect_one(**kwargs):
        if kwargs["target"].server_id == "server-slow":
            slow_started.set()
            fast_done.wait(timeout=5)
            slow_finished.set()
        else:
            assert slow_started.wait(timeout=5), "the slow server should have started first"
            ran_while_slow_hung.append(not slow_finished.is_set())
            fast_done.set()
        return []

    _wire(
        monkeypatch,
        targets=[_target("server-slow"), _target("server-fast")],
        definitions=[_definition()],
        collect_one=collect_one,
        max_parallel=16,
    )

    began = time.monotonic()
    collect_metrics(config=_config(tmp_path), dry_run=False, force=True)

    assert ran_while_slow_hung == [True], "the fast server waited for the slow one to finish"
    assert time.monotonic() - began < 5, "the pass took the slow server's full stall"


def test_the_counts_a_pass_reports_do_not_depend_on_the_fan_out(tmp_path, monkeypatch):
    """Every counter is filled per worker and merged, so a wider fan-out must not lose a count."""
    targets = [_target(f"server-{index}") for index in range(6)]
    definitions = [_definition("INSTANCE_STATUS"), _definition("OS_CPU_USAGE")]

    def summary_at(max_parallel, path):
        _wire(monkeypatch, targets=targets, definitions=definitions,
              collect_one=lambda **_: [], max_parallel=max_parallel)
        return collect_metrics(config=_config(path), dry_run=False, force=True)

    serial = summary_at(1, tmp_path / "serial")
    parallel = summary_at(16, tmp_path / "parallel")

    assert (serial.metric_count, serial.executed_count) == (12, 12)
    assert (parallel.metric_count, parallel.executed_count) == (serial.metric_count, serial.executed_count)
    assert parallel.target_count == serial.target_count
    assert sorted(parallel.message.split()) == sorted(serial.message.split())


def test_the_run_message_reads_the_same_however_the_hosts_behaved(tmp_path, monkeypatch):
    """Workers are merged in submission order, not completion order.

    A pass is compared against yesterday's by eye, so the summary must not reshuffle itself
    because one host happened to answer first today.
    """
    targets = [_target("server-a"), _target("server-b"), _target("server-c")]

    def collect_one(**kwargs):
        # Reverse the natural order: the last server returns first.
        delay = {"server-a": 0.06, "server-b": 0.03, "server-c": 0.0}[kwargs["target"].server_id]
        time.sleep(delay)
        return []

    _wire(monkeypatch, targets=targets, definitions=[_definition()],
          collect_one=collect_one, max_parallel=16)
    summary = collect_metrics(config=_config(tmp_path), dry_run=False, force=True)

    positions = [summary.message.find(f"server-{letter}") for letter in "abc"]
    assert positions == sorted(positions), summary.message


@pytest.mark.parametrize(
    "payload",
    [
        {},                                                  # no collection block at all
        {"collection": {}},                                  # block present, setting absent
        {"collection": {"max_parallel_servers": "many"}},     # unparseable
        {"collection": {"max_parallel_servers": 0}},          # nonsense value
        {"collection": []},                                   # wrong type entirely
    ],
)
def test_an_unusable_collection_setting_falls_back_to_one_server_at_a_time(tmp_path, payload):
    """A malformed edit must degrade to the old serial pass, never to a wider fan-out than asked."""
    import json

    path = tmp_path / "metric_definitions.json"
    path.write_text(json.dumps({**payload, "metrics": []}), encoding="utf-8")

    assert load_max_parallel_servers(path) == 1


def test_the_configured_setting_is_what_the_catalog_says(tmp_path):
    import json

    path = tmp_path / "metric_definitions.json"
    path.write_text(json.dumps({"collection": {"max_parallel_servers": 16}, "metrics": []}), encoding="utf-8")

    assert load_max_parallel_servers(path) == 16
