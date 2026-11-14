"""What to do about a metric is worded for the operating system and engine it is shown on (0.39.0).

The operator, 2026-11-13, reading the page of an Ubuntu VM whose journal held twenty errors: *the
page does not say which OS the host is - and this VM is Ubuntu, so why does it say to read the
Windows event log? ... fix what is hard-coded, the texts too ... the inventory report has it
hard-coded as well.*

The What to do sentences were a table in the report's code keyed by the metric code alone, so one
sentence was shown on every platform: the Linux host was told to read the Windows event log, on its
own page and - in one card with a Windows host - on the fleet page; a stopped Linux service was
told to check the event log and the service's recovery settings; a PostgreSQL host short of memory
was told to check max server memory.

Now the sentences are the metric's own, in ``metric_definitions.json`` (``report_policy.action``,
with ``action_by_platform`` and ``action_by_db_type`` where the right words differ), and both pages
read them for the server in front of them. A card that holds servers of several platforms names each
sentence by the platform it is for. Nothing here guesses: a server whose platform nobody read gets
the general sentence, which names no operating system.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbabrain.lib import health_model
from dbabrain.reports import inventory_report, server_health
from dbabrain.reports.server_health import build_problems, metric_action, metric_action_for, metric_actions

ROOT = Path(__file__).resolve().parents[1]
COPIES = (ROOT / "dbabrain/metrics/catalogue/metric_definitions.json", ROOT / "data/metric_definitions.example.json")
TEMPLATE = (ROOT / "dbabrain/reports/templates/server_report.html").read_text(encoding="utf-8")

#: Words that belong to one operating system, and so never in the other's sentence.
WINDOWS_WORDS = ("Windows", "Event Viewer", "Get-WinEvent", "event log", "recovery settings", "Task Manager")
LINUX_WORDS = ("journal", "journalctl", "systemctl", "systemd")


def _definitions(path=COPIES[0]):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))["metrics"]


# --------------------------------------------------------------------------- #
# The defect: a Linux host and the Windows event log
# --------------------------------------------------------------------------- #
def test_a_linux_host_is_told_to_read_its_journal():
    sentence, whom = metric_action_for("OS_EVENTLOG_CRITICAL", platform="Linux", db_type="host")

    assert "journalctl -p err" in sentence and whom == "Linux"
    assert not any(word in sentence for word in WINDOWS_WORDS)


def test_a_windows_host_is_told_to_read_its_event_log():
    sentence, whom = metric_action_for("OS_EVENTLOG_CRITICAL", platform="Windows", db_type="sqlserver")

    assert "Windows event log" in sentence and whom == "Windows"
    assert not any(word in sentence for word in LINUX_WORDS)


@pytest.mark.parametrize("platform", ["", None, "host", "docker", "solaris"])
def test_a_host_whose_platform_nobody_read_gets_a_sentence_that_names_no_operating_system(platform):
    sentence, whom = metric_action_for("OS_EVENTLOG_CRITICAL", platform=platform)

    assert whom == "" and sentence == "Read the errors in the system's own log - they usually name the cause."
    assert not any(word in sentence for word in WINDOWS_WORDS + LINUX_WORDS)


def test_a_stopped_linux_service_is_not_sent_to_the_event_log():
    linux = metric_action("OS_SERVICE_STATUS", platform="linux")
    windows = metric_action("OS_SERVICE_STATUS", platform="windows")

    assert "systemctl status" in linux and "event log" not in linux
    assert "event log" in windows and "systemctl" not in windows


def test_the_platform_is_read_whatever_its_case():
    assert metric_action("OS_EVENTLOG_CRITICAL", platform="LINUX") == metric_action("OS_EVENTLOG_CRITICAL", platform="linux")


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
def test_only_a_sql_server_is_told_about_max_server_memory():
    sql_server, whom = metric_action_for("OS_MEMORY_USAGE", platform="Windows", db_type="sqlserver")
    postgres, nobody = metric_action_for("OS_MEMORY_USAGE", platform="Linux", db_type="postgresql")

    assert "max server memory" in sql_server and whom == "SQL Server"
    assert "max server memory" not in postgres and nobody == ""


@pytest.mark.parametrize("db_type, said", [("sqlserver", "SQL Server error log"), ("mssql", "SQL Server error log"),
                                           ("oracle", "alert log"), ("postgresql", "engine's own log")])
def test_each_engine_is_sent_to_its_own_log(db_type, said):
    assert said in metric_action("LOG_RECENT_CRITICAL", db_type=db_type)


def test_tempdb_is_named_to_sql_server_only():
    assert "TempDB" in metric_action("STORAGE_TEMP_SPACE", db_type="sqlserver")
    assert "temporary tablespace" in metric_action("STORAGE_TEMP_SPACE", db_type="oracle")
    assert "temp files" in metric_action("STORAGE_TEMP_SPACE", db_type="postgres")
    assert "TempDB" not in metric_action("STORAGE_TEMP_SPACE")


def test_a_metric_with_no_sentence_of_its_own_still_falls_back_to_its_areas_note():
    # OS_TOP_PROCESSES is a reading that grades nothing, so it states no sentence of its own.
    area = next(spec for spec in server_health.AREAS if "OS_TOP_PROCESSES" in server_health.AREA_CODES[spec["key"]]) \
        if any("OS_TOP_PROCESSES" in codes for codes in server_health.AREA_CODES.values()) else None

    assert "OS_TOP_PROCESSES" not in metric_actions()
    assert metric_action("OS_TOP_PROCESSES") == ((area or {}).get("note") or server_health.GENERIC_ACTION)
    assert metric_action("NO_SUCH_METRIC") == server_health.GENERIC_ACTION


def test_a_metric_whose_only_sentences_are_per_platform_names_every_one_when_the_platform_is_unknown(monkeypatch):
    monkeypatch.setattr(server_health, "_METRIC_ACTIONS", {"X": {
        "action": "", "by_platform": {"windows": "Open Event Viewer.", "linux": "Run journalctl."}, "by_db_type": {}}})

    assert metric_action_for("X") == ("Linux: Run journalctl. Windows: Open Event Viewer.", "")
    assert metric_action_for("X", platform="linux") == ("Run journalctl.", "Linux")


# --------------------------------------------------------------------------- #
# The sentences are data, and the data is held to the metric
# --------------------------------------------------------------------------- #
def test_the_sentences_are_no_longer_a_table_in_the_reports_code():
    import ast

    assert not hasattr(server_health, "ACTIONS")
    # No string the module can show names one operating system's log or service manager (a comment
    # telling the story of the defect is not a string).
    tree = ast.parse((ROOT / "dbabrain/reports/server_health.py").read_text(encoding="utf-8"))
    shown = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert not [text for text in shown if "Windows event log" in text or "journalctl" in text]


def test_every_copy_of_the_definitions_carries_the_same_sentences():
    stated = [{m["metric_code"]: {k: v for k, v in (m.get("report_policy") or {}).items() if k.startswith("action")}
               for m in _definitions(path)} for path in COPIES]

    assert stated[0] == stated[1]
    assert sum(1 for entry in stated[0].values() if entry.get("action")) >= 86


def test_a_sentence_is_only_written_for_a_platform_or_engine_that_collects_the_metric():
    for metric in _definitions():
        policy = metric.get("report_policy") or {}
        variants = metric.get("variants") or []
        platforms = {str(v.get("platform") or "").lower() for v in variants} - {""}
        engines = {str(v.get("db_type") or "").lower() for v in variants if v.get("supported", True) is not False} - {""}
        for platform in policy.get("action_by_platform") or {}:
            assert platform in platforms, f"{metric['metric_code']}: a sentence for {platform}, where it is not collected"
        if metric.get("collector_type") != "cmd":   # an OS metric is read beside any engine
            for engine in policy.get("action_by_db_type") or {}:
                assert engine in engines, f"{metric['metric_code']}: a sentence for {engine}, where it is not collected"


def test_no_sentence_names_the_other_operating_systems_tools():
    for metric in _definitions():
        policy = metric.get("report_policy") or {}
        code = metric["metric_code"]
        by_platform = policy.get("action_by_platform") or {}
        assert not any(word in by_platform.get("linux", "") for word in WINDOWS_WORDS), code
        assert not any(word in by_platform.get("windows", "") for word in LINUX_WORDS), code
        platforms = {str(v.get("platform") or "").lower() for v in metric.get("variants") or []} - {""}
        if {"windows", "linux"} <= platforms:
            # Read on both: the sentence everybody gets names neither's log or service manager.
            general = str(policy.get("action") or "")
            assert not any(word in general for word in ("Windows event log", "Event Viewer", "journalctl", "systemctl")), code


def test_the_reader_drops_an_empty_sentence_and_lowercases_the_keys(tmp_path):
    path = tmp_path / "metric_definitions.json"
    path.write_text(json.dumps({"metrics": [
        {"metric_code": "A", "report_policy": {"action": "Do A.", "action_by_platform": {"Linux": "On Linux.", "windows": " "}}},
        {"metric_code": "B", "report_policy": {"severity": {}}},
        {"metric_code": "C"}]}), encoding="utf-8")

    assert metric_actions(path) == {"A": {"action": "Do A.", "by_platform": {"linux": "On Linux."}, "by_db_type": {}}}
    assert metric_actions(tmp_path / "missing.json") == {}, "no catalog: the area's note, never a failed page"


# --------------------------------------------------------------------------- #
# The server page
# --------------------------------------------------------------------------- #
def _series(code="OS_EVENTLOG_CRITICAL"):
    return [{"code": code, "label": "Event log errors", "item": "journal", "status": "WARNING", "numeric": True,
             "last": 20, "lastText": "20", "unit": "events", "message": "window_hours=24", "lastAt": 1_793_000_000}]


def test_the_server_pages_problems_are_worded_for_that_server():
    linux = build_problems(_series(), now=1_793_000_100, platform="Linux", db_type="host")
    windows = build_problems(_series(), now=1_793_000_100, platform="Windows", db_type="sqlserver")

    assert "journalctl" in linux[0]["action"] and linux[0]["actionFor"] == "Linux"
    assert "Windows event log" in windows[0]["action"] and windows[0]["actionFor"] == "Windows"


def test_a_group_keeps_whom_its_action_was_worded_for():
    groups = health_model.group_findings([{
        "code": "X", "label": "X", "item": "i", "value": "1", "severity": "WARNING", "message": "",
        "lastText": "1", "action": "Run journalctl.", "actionFor": "Linux", "collectedAt": 1_793_000_000}],
        now=1_793_000_100)

    assert groups[0]["action"] == "Run journalctl." and groups[0]["actionFor"] == "Linux"


# --------------------------------------------------------------------------- #
# The fleet page: one card, several platforms
# --------------------------------------------------------------------------- #
def _model(role, ip, action, whom):
    return {"role": role, "ip": ip, "problems": [{
        "code": "OS_EVENTLOG_CRITICAL", "label": "Event log errors", "severity": "WARNING", "count": 1,
        "headline": "20 events", "collectedAt": "2026-11-13T05:43:16Z", "action": action, "actionFor": whom}]}


def test_a_card_holding_a_windows_and_a_linux_host_words_the_action_for_each():
    windows = metric_action_for("OS_EVENTLOG_CRITICAL", platform="Windows")
    linux = metric_action_for("OS_EVENTLOG_CRITICAL", platform="Linux")

    card = inventory_report._metric_problem_cards([_model("WIN-HOST", "192.0.2.118", *windows),
                                                   _model("LINUX-HOST", "192.0.2.119", *linux)])[0]

    assert card["title"] == "Event log errors — 2 item(s) on 2 server(s)"
    assert card["action"] == f"Windows: {windows[0]} Linux: {linux[0]}"


def test_a_card_whose_servers_agree_says_the_sentence_once():
    linux = metric_action_for("OS_EVENTLOG_CRITICAL", platform="Linux")

    card = inventory_report._metric_problem_cards([_model("A", "192.0.2.1", *linux), _model("B", "192.0.2.2", *linux)])[0]

    assert card["action"] == linux[0]


def test_the_stopped_service_card_reads_the_metrics_sentence_for_each_host():
    models = [{"osFamily": "Windows", "dbType": "sqlserver"}, {"osFamily": "Linux", "dbType": "host"}]

    said = inventory_report._metric_action_across("OS_SERVICE_STATUS", models)

    assert said.startswith("Windows: ") and " Linux: " in said
    assert "event log" in said.split(" Linux: ")[0] and "systemctl" in said.split(" Linux: ")[1]
    assert inventory_report._metric_action_across("OS_SERVICE_STATUS", models[1:]).startswith("Start the service")


# --------------------------------------------------------------------------- #
# The operating system, first on the page
# --------------------------------------------------------------------------- #
def test_the_host_line_starts_with_the_operating_system():
    """Both writes of the line - when a server is picked, and again when its file has loaded."""
    lines = [chunk.split(";", 1)[0] for chunk in TEMPLATE.split('document.getElementById("hostMeta").textContent =')[1:]]

    assert len(lines) == 2
    assert all(line.lstrip().startswith("[osLine(server),") for line in lines)


def test_an_operating_system_nobody_read_is_said_not_guessed():
    function = TEMPLATE.split("function osLine(server){", 1)[1].split("\n}", 1)[0]

    assert 'if (!server.os) return "OS not read";' in function
    assert "inventory - not read from the host" in function, "the inventory's word is marked as that"


def test_the_pages_index_carries_what_the_os_collector_read():
    source = (ROOT / "dbabrain/reports/server_report.py").read_text(encoding="utf-8")

    assert '"os": "" if model.get("os") in (None, "", "\\u2014") else str(model.get("os")),' in source
    assert '"osStatic": "os" in (model.get("staticFields") or []),' in source
