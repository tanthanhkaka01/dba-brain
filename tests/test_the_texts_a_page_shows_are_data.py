"""The words a report page shows are data, sayable per engine - and a node's own files get the shipped ones (0.39.0).

The operator, 2026-11-13, after a Linux host was told to read the Windows event log: *fix what is
hard-coded, the texts too ... look at the other places as well* - and, of the tables still in the
code: the metric labels, the health areas' notes (three named SQL Server for areas every engine
shares), the fleet page's card sentences, a workload label that said Windows.

So the texts left the report builders:

* a metric's ``label`` beside its *What to do* sentences, in ``metric_definitions.json``
  (``report_policy``);
* each health area's ``label``, ``threshold`` and ``note``, and each fleet card's ``title``, ``body``
  and ``action``, in ``reports_config.json`` (``page_texts``), where a text can be said differently
  for an engine or an operating system.

A SQL Server page reads every word it read before; a PostgreSQL or Oracle page no longer reads SQL
Server's. ``upgrade-config`` (``report-texts``) writes the shipped texts into a node's own files
where it states none, and leaves what it states alone.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from dbabrain.lib import report_texts
from dbabrain.reports import inventory_report, page_texts, server_health, server_series, workload

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_REPORTS = ROOT / "dbabrain/reports/catalogue/reports_config.json"
EXAMPLE_REPORTS = ROOT / "data/reports_config.example.json"
SHIPPED_METRICS = ROOT / "dbabrain/metrics/catalogue/metric_definitions.json"
EXAMPLE_METRICS = ROOT / "data/metric_definitions.example.json"


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _strings(module) -> list[str]:
    """Every string literal a module can show - not its docstrings, not its comments."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body
            and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant)}
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs]


# --------------------------------------------------------------------------- #
# One text, said per engine or operating system
# --------------------------------------------------------------------------- #
ENTRY = {"label": "Temporary space", "note": "General.",
         "by_db_type": {"sqlserver": {"label": "TempDB", "note": "SQL Server's."}, "oracle": {"note": ""}},
         "by_platform": {"linux": {"note": "On Linux."}}}


@pytest.mark.parametrize("field, db_type, platform, said", [
    ("label", "", "", "Temporary space"),
    ("label", "sqlserver", "", "TempDB"),
    ("label", "SQLSERVER", "", "TempDB"),
    ("note", "sqlserver", "linux", "SQL Server's."),       # the engine's word before the operating system's
    ("note", "postgresql", "linux", "On Linux."),
    ("note", "postgresql", "windows", "General."),
    ("note", "oracle", "", "General."),                    # an empty variant says nothing: the general one
    ("label", "oracle", "", "Temporary space"),
    ("threshold", "sqlserver", "", ""),                    # stated nowhere
])
def test_a_text_is_the_engines_then_the_operating_systems_then_the_general_one(field, db_type, platform, said):
    assert report_texts.pick(ENTRY, field, db_type=db_type, platform=platform) == said


def test_an_entry_that_is_not_one_says_nothing():
    assert report_texts.pick(None, "label") == "" and report_texts.pick("text", "label") == ""


# --------------------------------------------------------------------------- #
# The metric labels
# --------------------------------------------------------------------------- #
def test_a_metric_is_called_by_its_own_label_and_never_by_a_raw_code():
    assert server_series.metric_label("PERFORMANCE_IO_LATENCY") == "Disk latency"
    assert server_series.metric_label("BACKUP_AGE") == "Backup age"
    assert server_series.metric_label("SOME_NEW_METRIC") == "Some new metric"


def test_the_log_metric_read_on_both_operating_systems_is_named_for_neither():
    """It was *Event log errors* on the page of a host whose errors were in its journal."""
    label = server_series.metric_label("OS_EVENTLOG_CRITICAL")

    assert label == "System log errors"
    assert "Event log" not in label and "journal" not in label.lower()


def test_the_labels_are_no_longer_a_table_in_the_reports_code():
    assert not hasattr(server_series, "METRIC_LABELS")
    assert "Disk latency" not in _strings(server_series)


def test_every_copy_of_the_definitions_carries_the_same_labels():
    labels = [{m["metric_code"]: (m.get("report_policy") or {}).get("label") for m in _json(path)["metrics"]}
              for path in (SHIPPED_METRICS, EXAMPLE_METRICS)]

    assert labels[0] == labels[1]
    assert sum(1 for label in labels[0].values() if label) >= 65


def test_the_reader_takes_labels_from_the_file_it_is_given(tmp_path):
    path = tmp_path / "metric_definitions.json"
    path.write_text(json.dumps({"metrics": [{"metric_code": "A", "report_policy": {"label": " Alpha "}},
                                            {"metric_code": "B", "report_policy": {"label": ""}},
                                            {"metric_code": "C"}]}), encoding="utf-8")

    assert server_series.metric_labels(path) == {"A": "Alpha"}
    assert server_series.metric_labels(tmp_path / "missing.json") == {}


# --------------------------------------------------------------------------- #
# The health areas
# --------------------------------------------------------------------------- #
def test_every_area_of_the_page_has_its_three_texts_in_the_shipped_file():
    areas = _json(SHIPPED_REPORTS)["page_texts"]["areas"]

    assert set(areas) == {spec["key"] for spec in server_health.AREAS}
    for key, entry in areas.items():
        assert all(str(entry.get(field) or "").strip() for field in ("label", "threshold", "note")), key


def test_the_area_table_in_the_code_holds_structure_and_no_words():
    for spec in server_health.AREAS:
        assert not {"label", "threshold", "note"} & set(spec), spec["key"]
    shown = _strings(server_health)
    assert not [text for text in shown if "page life expectancy" in text.lower() or "TempDB filling" in text]


@pytest.mark.parametrize("key, field, said", [
    ("memory", "note", "Low page life expectancy means the buffer pool is churning"),
    ("memory", "threshold", "PLE < 2000 s = pressure"),
    ("tempdb", "label", "TempDB"),
    ("tempdb", "note", "version store"),
    ("ha", "note", "Availability Groups, database mirroring and log shipping"),
    ("disk_space", "threshold", "SQL Server: WARN < 10% or 10 GB free"),
    ("availability", "threshold", "every database ONLINE"),
])
def test_a_sql_server_page_reads_the_words_it_read_before(key, field, said):
    assert said in page_texts.area_text(key, field, db_type="sqlserver")
    assert said in page_texts.area_text(key, field, db_type="mssql")


@pytest.mark.parametrize("db_type", ["postgresql", "postgres", "oracle", "mysql", "host", ""])
def test_another_engine_is_not_told_sql_servers(db_type):
    said = " ".join(page_texts.area_text(key, field, db_type=db_type)
                    for key in ("memory", "tempdb", "ha", "disk_space", "availability")
                    for field in ("label", "threshold", "note"))

    for word in ("page life expectancy", "PLE", "TempDB", "version store", "Availability Groups", "SQL Server", "ONLINE"):
        assert word not in said, word
    assert page_texts.area_text("tempdb", "label", db_type=db_type) == "Temporary space"


def test_the_areas_built_for_a_server_carry_that_engines_words():
    def texts(db_type):
        return {area["key"]: area for area in server_health.build_areas([], db_type=db_type)}

    assert texts("sqlserver")["tempdb"]["label"] == "TempDB"
    assert texts("postgresql")["tempdb"]["label"] == "Temporary space"
    assert "page life expectancy" in texts("sqlserver")["memory"]["note"].lower()
    assert "page life expectancy" not in texts("oracle")["memory"]["note"].lower()
    assert texts("")["cpu"]["label"] == "CPU" and texts("")["cpu"]["threshold"]


def test_an_area_nobody_wrote_a_label_for_is_called_by_its_key_in_words(monkeypatch):
    monkeypatch.setattr(page_texts, "_PAGE_TEXTS", {"areas": {}})

    assert page_texts.area_text("storage_activity", "label") == "Storage activity"
    assert page_texts.area_text("storage_activity", "note") == ""


def test_a_metric_with_no_sentence_falls_back_to_its_areas_note_as_that_engine_reads_it(monkeypatch):
    monkeypatch.setattr(server_health, "_METRIC_ACTIONS", {})

    assert "TempDB" in server_health.metric_action("STORAGE_TEMP_SPACE", db_type="sqlserver")
    assert "TempDB" not in server_health.metric_action("STORAGE_TEMP_SPACE", db_type="oracle")


# --------------------------------------------------------------------------- #
# The fleet page's cards
# --------------------------------------------------------------------------- #
CARDS = ("metric_problem", "metrics_failing", "metrics_overdue", "metrics_no_evidence", "stale_servers",
         "services_stopped", "log_rpo_violated", "backup_not_graded", "backup_aging", "not_inventoried",
         "no_log_requirement", "failed_logins", "passwords_old", "low_ple", "disk_critical", "disk_low",
         "wrong_edition", "not_encrypted", "xp_cmdshell", "no_dr", "inventory_notes", "config_drift")


def test_every_card_of_the_fleet_page_has_its_text_in_the_shipped_file():
    cards = _json(SHIPPED_REPORTS)["page_texts"]["fleet_cards"]

    assert set(cards) == set(CARDS)
    for key, entry in cards.items():
        assert entry.get("title") and entry.get("body"), key
        assert entry.get("action") or key == "metric_problem", "the metric's own sentence is that card's action"


def test_a_cards_text_is_filled_with_what_the_page_computed():
    assert page_texts.card_text("metrics_failing", "title", count=3) == "Metrics currently failing on 3 server(s)"
    assert page_texts.card_text("stale_servers", "title", count=2) == "2 server(s) show last known values, not current ones"


def test_a_placeholder_the_page_did_not_fill_stays_as_written_and_fails_nothing(monkeypatch):
    monkeypatch.setattr(page_texts, "_PAGE_TEXTS", {"fleet_cards": {"x": {"title": "{count} of {total} {broken"}}})

    assert page_texts.card_text("x", "title", count=1) == "{count} of {total} {broken", "a text that cannot be read as a template is printed as it is"
    monkeypatch.setattr(page_texts, "_PAGE_TEXTS", {"fleet_cards": {"x": {"title": "{count} of {total}"}}})
    assert page_texts.card_text("x", "title", count=1) == "1 of {total}"
    assert page_texts.card_text("nothing", "title") == ""


def test_the_card_sentences_are_no_longer_in_the_fleet_pages_code():
    shown = " ".join(_strings(inventory_report))

    for sentence in ("Metrics currently failing on", "Transaction-log RPO violated", "Apply a documented SQL config baseline",
                     "xp_cmdshell is ON for", "Free space / extend the volume now"):
        assert sentence not in shown, sentence


def test_a_card_is_built_from_the_pages_text_and_the_servers_numbers():
    models = [{"role": "ACME-DB01", "ip": "192.0.2.10", "freshness_detail": {"failed": ["OS_CPU_USAGE"]}}]

    card = inventory_report._monitoring_gap_cards(models)[0]

    assert card["sev"] == "crit" and card["title"] == "Metrics currently failing on 1 server(s)"
    assert "ACME-DB01 (192.0.2.10) — OS_CPU_USAGE" in card["body"]
    assert card["action"].startswith("Read the collector error on the server page")


# --------------------------------------------------------------------------- #
# The workload label that named one operating system
# --------------------------------------------------------------------------- #
def test_the_low_memory_signal_is_the_operating_systems_not_windows():
    """SQL Server runs on Linux too: the signal is the host's, whatever the host is."""
    labels = [label for _group, rows in workload._MEMORY_ROWS for _key, label, _unit, _watch in rows]

    assert "The operating system asked for memory back" in labels
    assert not [label for label in labels if "Windows" in label]


# --------------------------------------------------------------------------- #
# A node installed before 0.39.0: upgrade-config brings the texts to its own files
# --------------------------------------------------------------------------- #
SHIPPED = {"metrics": [
    {"metric_code": "A", "report_policy": {"severity": {"default": "WARNING"}, "label": "Alpha", "action": "Do A.",
                                           "action_by_platform": {"linux": "On Linux."}}},
    {"metric_code": "B", "report_policy": {"severity": {}}},
]}


def test_a_metric_that_states_no_text_is_given_the_shipped_ones():
    node = {"metrics": [{"metric_code": "A", "report_policy": {"severity": {"default": "CRITICAL"}}},
                        {"metric_code": "A"}, {"metric_code": "B"}, {"metric_code": "NOT_SHIPPED"}]}

    changes = report_texts.state_metric_texts(node, SHIPPED)

    first, bare = node["metrics"][0], node["metrics"][1]
    assert first["report_policy"] == {"severity": {"default": "CRITICAL"}, "action": "Do A.",
                                      "action_by_platform": {"linux": "On Linux."}, "label": "Alpha"}
    assert bare["report_policy"] == {"action": "Do A.", "action_by_platform": {"linux": "On Linux."}, "label": "Alpha"}
    assert "report_policy" not in node["metrics"][2] and "report_policy" not in node["metrics"][3]
    assert [(c["where"], c["field"]) for c in changes] == [
        ("metrics[A]", "report_policy.action"), ("metrics[A]", "report_policy.label"),
        ("metrics[A]", "report_policy.action"), ("metrics[A]", "report_policy.label")]
    assert report_texts.state_metric_texts(node, SHIPPED) == [], "run again, it changes nothing"


@pytest.mark.parametrize("own", [{"action": "Mine."}, {"action_by_platform": {"linux": "Mine."}},
                                 {"action_by_db_type": {"oracle": "Mine."}}, {"action": ""}])
def test_a_metric_that_states_a_sentence_of_its_own_keeps_it_and_still_gets_a_label(own):
    node = {"metrics": [{"metric_code": "A", "report_policy": dict(own)}]}

    changes = report_texts.state_metric_texts(node, SHIPPED)

    assert node["metrics"][0]["report_policy"] == {**own, "label": "Alpha"}
    assert [c["field"] for c in changes] == ["report_policy.label"]


def test_a_label_the_node_wrote_is_the_nodes():
    node = {"metrics": [{"metric_code": "A", "report_policy": {"label": "Mine", "action": "Mine."}}]}

    assert report_texts.state_metric_texts(node, SHIPPED) == []
    assert node["metrics"][0]["report_policy"] == {"label": "Mine", "action": "Mine."}


PAGES = {"page_texts": {"areas": {"cpu": {"label": "CPU", "note": "n"}, "memory": {"label": "Memory"}},
                        "fleet_cards": {"no_dr": {"title": "t", "body": "b", "action": "a"}}}}


def test_an_area_or_card_the_node_does_not_state_is_given_whole():
    node = {"report_base_url": "x", "page_texts": {"areas": {"cpu": {"label": "Processor"}}}}

    changes = report_texts.state_page_texts(node, PAGES)

    assert node["page_texts"]["areas"]["cpu"] == {"label": "Processor"}, "an entry the node states is the node's, whole"
    assert node["page_texts"]["areas"]["memory"] == {"label": "Memory"}
    assert node["page_texts"]["fleet_cards"]["no_dr"]["action"] == "a"
    assert [c["where"] for c in changes] == ["page_texts.areas[memory]", "page_texts.fleet_cards[no_dr]"]
    assert report_texts.state_page_texts(node, PAGES) == [] and node["report_base_url"] == "x"


def test_a_node_with_no_page_texts_at_all_gets_every_one():
    node = {"reports": []}

    assert len(report_texts.state_page_texts(node, PAGES)) == 3
    assert node["page_texts"] == PAGES["page_texts"]


def test_upgrade_config_writes_both_files_and_copies_each_aside_first(tmp_path):
    from dbabrain.common import config_upgrade

    metrics, reports = tmp_path / "metric_definitions.json", tmp_path / "reports_config.json"
    metrics.write_text(json.dumps({"metrics": [{"metric_code": "OS_EVENTLOG_CRITICAL", "report_policy": {"thresholds": {}}},
                                               {"metric_code": "MINE_ONLY"}]}, indent=2) + "\n", encoding="utf-8")
    reports.write_text(json.dumps({"report_base_url": "", "reports": []}, indent=2) + "\n", encoding="utf-8")
    copied = []

    plan = config_upgrade._report_texts(tmp_path, dry_run=True, before_write=copied.append)
    assert [(f["file"], f["written"]) for f in plan["files"]] == [("metric_definitions.json", False),
                                                                  ("reports_config.json", False)]
    assert not copied and "page_texts" not in _json(reports)

    config_upgrade._report_texts(tmp_path, dry_run=False, before_write=copied.append)
    policy = _json(metrics)["metrics"][0]["report_policy"]
    assert copied == [metrics, reports]
    assert policy["label"] == "System log errors" and "journalctl" in policy["action_by_platform"]["linux"]
    assert policy["thresholds"] == {} and "report_policy" not in _json(metrics)["metrics"][1]
    assert set(_json(reports)["page_texts"]) == {"areas", "fleet_cards"} and _json(reports)["reports"] == []
    assert "report-texts" in config_upgrade.STEPS
    assert config_upgrade._report_texts(tmp_path, dry_run=False, before_write=copied.append)["files"][0]["records_changed"] == 0


def test_the_shipped_file_and_its_example_carry_the_same_page_texts():
    assert _json(SHIPPED_REPORTS)["page_texts"] == _json(EXAMPLE_REPORTS)["page_texts"]


def test_until_it_has_run_a_page_reads_the_shipped_text_and_never_draws_a_card_with_no_words(tmp_path):
    """The code can arrive before ``upgrade-config`` has: the node's file states no ``page_texts`` yet."""
    node = tmp_path / "reports_config.json"
    node.write_text(json.dumps({"report_base_url": "", "reports": []}), encoding="utf-8")

    assert page_texts.page_texts(node) == _json(SHIPPED_REPORTS)["page_texts"]
    assert page_texts.page_texts(tmp_path / "not_there.json") == _json(SHIPPED_REPORTS)["page_texts"]


def test_an_entry_the_node_states_is_the_nodes_whole_and_the_rest_is_the_shipped(tmp_path):
    node = tmp_path / "reports_config.json"
    node.write_text(json.dumps({"page_texts": {"fleet_cards": {"no_dr": {"title": "No DR plan"}},
                                               "areas": "not an object"}}), encoding="utf-8")

    read = page_texts.page_texts(node)

    assert read["fleet_cards"]["no_dr"] == {"title": "No DR plan"}, "the same rule as upgrade-config: stated in part is stated"
    assert read["fleet_cards"]["metrics_failing"] == _json(SHIPPED_REPORTS)["page_texts"]["fleet_cards"]["metrics_failing"]
    assert read["areas"] == _json(SHIPPED_REPORTS)["page_texts"]["areas"], "a section that is not one is not read"
