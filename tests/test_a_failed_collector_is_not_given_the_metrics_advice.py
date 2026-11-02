"""A metric that could not be read is told to fix the reading, not the server.

Each metric's "What to do" answers a reading: *find the top process*, *free space or extend the
volume*. On a collector failure there is no reading, and the advice still printed. On one SQL
Server host the SSH login was refused (it takes keys only) and thirteen rows of the Problems table told
the operator to look for a runaway job, free disk space and read the Windows event log - on a
Linux host whose CPU, memory and disk nobody had measured (0.32.0 sheet 1.207).

Both pages build their list through ``health_model.group_findings``, so the rule lives there once.
A real reading beside a failure keeps its metric's advice: the reading is still a finding.
"""

from dbabrain.lib import health_model
from dbabrain.reports.server_health import build_problems, metric_action

NOW = 1_793_000_000


def _finding(code, item, *, severity="WARNING", message=""):
    return {"code": code, "label": code, "item": item, "value": "collector failed",
            "severity": severity, "message": message, "lastText": "",
            "action": metric_action(code), "collectedAt": NOW}


def test_a_metric_that_only_failed_to_collect_is_told_to_fix_the_collection():
    groups = health_model.group_findings([
        _finding("OS_CPU_USAGE", health_model.COLLECTOR_ITEM,
                 message="SSH authentication failed: allowed types: ['publickey']"),
    ], now=NOW)

    assert groups[0]["action"] == health_model.COLLECTOR_FAILURE_ACTION
    assert "top process" not in groups[0]["action"]


def test_every_failed_metric_on_the_page_gets_the_same_collection_advice():
    codes = ("OS_CPU_USAGE", "OS_DISK_USAGE", "OS_MEMORY_USAGE", "OS_EVENTLOG_CRITICAL")
    groups = health_model.group_findings(
        [_finding(code, health_model.COLLECTOR_ITEM) for code in codes], now=NOW)

    assert {group["action"] for group in groups} == {health_model.COLLECTOR_FAILURE_ACTION}


def test_a_real_reading_beside_a_failure_keeps_its_metrics_advice():
    groups = health_model.group_findings([
        _finding("OS_DISK_USAGE", health_model.COLLECTOR_ITEM),
        _finding("OS_DISK_USAGE", "D:", severity="CRITICAL"),
    ], now=NOW)

    assert groups[0]["action"] == metric_action("OS_DISK_USAGE")


def test_a_reading_that_is_a_problem_keeps_its_metrics_advice():
    groups = health_model.group_findings([_finding("OS_CPU_USAGE", "cpu_usage")], now=NOW)

    assert groups[0]["action"] == metric_action("OS_CPU_USAGE")


def test_the_server_page_problem_list_carries_the_collection_advice():
    series = [{"code": "OS_CPU_USAGE", "label": "CPU", "item": health_model.COLLECTOR_ITEM,
               "status": "ERROR", "numeric": False, "last": None, "lastText": "",
               "message": "Could not place the script on the host: SSH authentication failed",
               "lastAt": NOW, "points": [], "tier": "primary"}]

    problems = build_problems(series, now=NOW)

    assert problems[0]["action"] == health_model.COLLECTOR_FAILURE_ACTION
