"""`report_policy.disabled_metric_codes` keeps a metric's alerts out of the reports on one server.

It is the per-server twin of `collect_only`: the rows are collected and stored, and the warning /
critical reports that go to Telegram leave them out. Until 0.31.0 the collector read the list as
well, so a code put there to quiet a noisy alert also stopped being collected. One SQL Server had
listed its nine `OS_*` codes since its `cmd_access` was `local`, kept them after SSH worked, and
its server page could name neither the host's sockets nor its RAM - `OS_CPU_USAGE` and
`OS_MEMORY_USAGE` had never run there (2026-10-23, 0.31.0 sheet 1.189).

What is pinned: the list silences the named metric on the named server and nothing else, and a
server whose configuration cannot be read is not silenced by accident.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dbabrain.reports import metrics_reports as mr


class _Row(dict):
    """Mapping-like, the shape the store's rows have."""


def _target(target_id: str, codes=None, *, reports=True):
    raw = {"server_id": target_id.split("/")[0]}
    if codes is not None:
        raw["report_policy"] = {"disabled_metric_codes": codes}
    return SimpleNamespace(target_id=target_id, reports_enabled=reports, raw_config=raw)


@pytest.fixture
def targets(monkeypatch):
    configured: list[SimpleNamespace] = []
    monkeypatch.setattr(mr, "_collect_only_metric_codes", lambda: set())
    monkeypatch.setattr(mr, "load_config_metric_targets", lambda **_: configured)
    return configured


def _kept(rows):
    return {(row["target_id"], row["metric_code"]) for row in mr._filter_rows_for_reports(rows)}


def test_a_listed_metric_is_left_out_of_that_servers_reports(targets):
    targets.append(_target("A/sqlserver/X", ["os_cpu_usage"]))
    rows = [_Row(target_id="A/sqlserver/X", metric_code="OS_CPU_USAGE"),
            _Row(target_id="A/sqlserver/X", metric_code="OS_MEMORY_USAGE")]

    assert _kept(rows) == {("A/sqlserver/X", "OS_MEMORY_USAGE")}


def test_the_same_metric_on_another_server_still_alerts(targets):
    targets.extend([_target("A/sqlserver/X", ["OS_CPU_USAGE"]), _target("B/sqlserver/Y", [])])
    rows = [_Row(target_id="A/sqlserver/X", metric_code="OS_CPU_USAGE"),
            _Row(target_id="B/sqlserver/Y", metric_code="OS_CPU_USAGE")]

    assert _kept(rows) == {("B/sqlserver/Y", "OS_CPU_USAGE")}


def test_a_list_that_is_not_a_list_silences_nothing(targets):
    # A hand-edit that wrote a string must not read as every metric whose code is a substring.
    targets.append(_target("A/sqlserver/X", "OS_CPU_USAGE"))
    rows = [_Row(target_id="A/sqlserver/X", metric_code="OS_CPU_USAGE")]

    assert _kept(rows) == {("A/sqlserver/X", "OS_CPU_USAGE")}


def test_a_server_switched_out_of_the_reports_stays_out(targets):
    targets.append(_target("A/sqlserver/X", [], reports=False))
    rows = [_Row(target_id="A/sqlserver/X", metric_code="BACKUP_AGE")]

    assert _kept(rows) == set()


def test_the_list_no_longer_switches_collection_off():
    from dbabrain.metrics.collector import _metric_enabled_for_target
    from dbabrain.metrics.models import MetricDefinition, MetricTarget

    target = MetricTarget(target_id="A/sqlserver/X", server_id="A", ip="192.0.2.10",
                          db_type="sqlserver", db_name="X", credential_name="c")
    target.report_policy["disabled_metric_codes"] = ["OS_CPU_USAGE"]
    metric = MetricDefinition(metric_code="OS_CPU_USAGE", db_type="multi", category="os",
                              default_importance=3, active=True, collector_type="cmd")

    assert _metric_enabled_for_target(metric, target)
