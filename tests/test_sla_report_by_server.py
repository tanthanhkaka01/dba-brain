"""The SLA page answers "what is wrong with THIS server", and server_id is what identifies one.

300 policy-target results across 19 servers used to render as one flat list ordered by status.
That ordering answers "what is worst in the fleet", which is a different question from the one an
operator holds when they open the page, and it forced them to scan for a machine's rows among
sixteen other machines' rows.

The grouping key is `server_id` and only `server_id`: it is unique across every db_ops file, and
matching a machine any other way has already produced one server under two names in production.
"""

from __future__ import annotations

from db_ops.lib import inventory_render
from db_ops.sla import publish


class _Result:
    """The fields the renderer reads off an SlaPolicyResult."""

    def __init__(self, target_id, status="PASSED", policy_id="P", category="backup"):
        self.target_id = target_id
        self.status = status
        self.policy_id = policy_id
        self.category = category
        self.domain = category
        self.sli_code = policy_id
        self.actual_value = None
        self.actual_percent = 100.0
        self.objective_value = None
        self.objective_percent = 99.0
        self.unit = "%"
        self.comparison_operator = ">="
        self.error_budget_remaining = 1.0
        self.good_count = 1
        self.total_count = 1
        self.coverage_percent = 100
        self.data_quality_status = "OK"
        self.policy_model = "time_slo"
        self.current_status = "OK"
        self.affected_objects = 0


class _Summary:
    def __init__(self, results):
        self.results = results


def test_a_composite_target_is_grouped_by_its_server_not_its_service():
    """target_id is `<server_id>/<db_type>/<service>`, so one server owns several targets."""
    assert publish._server_of(_Result("GLOBEX-192-0-2-86/sqlserver/ERP Sync")) == "GLOBEX-192-0-2-86"
    assert publish._server_of(_Result("ACME-192-0-2-41/sqlserver/Scanpack")) == "ACME-192-0-2-41"


def test_a_result_with_no_target_belongs_to_no_server():
    assert publish._server_of(_Result("*")) == ""
    assert publish._server_of(_Result("")) == ""


def test_every_server_gets_its_own_section():
    summary = _Summary([
        _Result("A/sqlserver/one"), _Result("A/sqlserver/two"), _Result("B/sqlserver/one"),
    ])
    rendered = publish._server_sections(summary)
    assert rendered.count("<h3>") == 2
    assert "<h3>A</h3>" in rendered and "<h3>B</h3>" in rendered


def test_the_server_with_the_most_failures_comes_first():
    """An operator opening the page should not have to scroll to find the machine that is down."""
    summary = _Summary([
        _Result("quiet/sqlserver/x", status="PASSED"),
        _Result("loud/sqlserver/x", status="FAILED"),
        _Result("loud/sqlserver/y", status="FAILED"),
    ])
    rendered = publish._server_sections(summary)
    assert rendered.index("<h3>loud</h3>") < rendered.index("<h3>quiet</h3>")


def test_fleet_wide_rows_sort_last_because_they_name_no_machine():
    summary = _Summary([_Result("*", status="FAILED"), _Result("srv/sqlserver/x", status="PASSED")])
    rendered = publish._server_sections(summary)
    assert rendered.index("<h3>srv</h3>") < rendered.index("Fleet-wide")


def test_each_section_states_its_own_counts():
    summary = _Summary([
        _Result("srv/sqlserver/x", status="FAILED"),
        _Result("srv/sqlserver/y", status="AT_RISK"),
        _Result("srv/sqlserver/z", status="PASSED"),
    ])
    rendered = publish._server_sections(summary)
    assert "1 failed" in rendered and "1 at risk" in rendered and "1 passed" in rendered


def test_an_empty_run_says_so_rather_than_rendering_an_empty_table():
    assert "No policy results." in publish._server_sections(_Summary([]))


def test_the_overlay_merges_on_server_id_and_never_on_ip(capsys):
    """One machine under two ids used to merge anyway, by ip — so the page looked right while the
    metric store filed its rows under an id nothing else used. That must now refuse and say so."""
    overlay = {"servers": [{"server_id": "WRONG-1-2-3-4", "ip": "1.2.3.4",
                            "database_health": {"x": 1}}]}
    inventory = {"servers": [{"server_id": "RIGHT-1-2-3-4", "ip": "1.2.3.4"}]}

    merged = inventory_render._merge_overlay(overlay, inventory)

    assert merged == 0
    assert "database_health" not in inventory["servers"][0]
    # stderr since 2026-08-16: the warning is for the person watching, while stdout carries
    # a CLI's answer — `inventory-summary` printed both to the same stream and parsed as
    # neither. What the warning *says* is unchanged, and that is what these assert.
    warning = capsys.readouterr().err
    assert "RIGHT-1-2-3-4" in warning and "WRONG-1-2-3-4" in warning
    # The behaviour, not the sentence. This asserted the phrase "one server_id", which the message
    # dropped on 2026-08-11: one ip legitimately carries several ids here (eight lab instances share
    # 192.0.2.249), so the warning names the neighbour as context rather than asserting a
    # uniqueness rule the merge does not enforce. What must not change is that the neighbour is
    # named and the health blocks are refused.
    assert "does not contain" in warning


def test_a_matching_server_id_still_merges_normally():
    overlay = {"servers": [{"server_id": "SAME", "ip": "1.2.3.4", "database_health": {"x": 1}}]}
    inventory = {"servers": [{"server_id": "SAME", "ip": "9.9.9.9"}]}

    assert inventory_render._merge_overlay(overlay, inventory) == 1
    assert inventory["servers"][0]["database_health"] == {"x": 1}


def test_a_switched_off_instance_sharing_a_vm_is_not_reported_as_an_id_mismatch(capsys):
    """An instance with no metrics is not a naming disagreement, and must not read like one.

    Eight lab instances share one VM here, and five of them are deliberately disabled. Asking
    "this canonical server has no metrics — does anything share its ip?" answered yes for every
    one of them, so each run printed five 'one machine must have one server_id' warnings that
    were nothing of the sort. The check now runs from the collected side instead.
    """
    overlay = {"servers": [{"server_id": "LAB-1-2-3-4-PG-5433", "ip": "1.2.3.4",
                            "database_health": {"x": 1}}]}
    inventory = {"servers": [
        {"server_id": "LAB-1-2-3-4-PG-5433", "ip": "1.2.3.4"},
        {"server_id": "LAB-1-2-3-4-MSSQL-1434", "ip": "1.2.3.4"},   # enabled: false, no metrics
    ]}

    assert inventory_render._merge_overlay(overlay, inventory) == 1
    assert capsys.readouterr().err == ""    # nothing to warn about


def test_metrics_collected_under_an_unknown_id_are_reported_rather_than_dropped(capsys):
    """The failure that hid three host records for months.

    A host record collects the machine's CPU/RAM/disk under its own server_id. The merge is
    driven by the canonical inventory, so an id the canonical file does not contain was never
    visited — the page rendered without it and nothing said a word. The disk warning the CLOUD
    host was added for could not reach the report.
    """
    overlay = {"servers": [{"server_id": "CLOUD-1-2-3-4-HOST", "ip": "1.2.3.4",
                            "os_health": {"disks": {"/": {"free_percent": 2.0}}}}]}
    inventory = {"servers": [{"server_id": "CLOUD-1-2-3-4-ORA-1521", "ip": "1.2.3.4"}]}

    assert inventory_render._merge_overlay(overlay, inventory) == 0
    # stderr since 2026-08-16: the warning is for the person watching, while stdout carries
    # a CLI's answer — `inventory-summary` printed both to the same stream and parsed as
    # neither. What the warning *says* is unchanged, and that is what these assert.
    warning = capsys.readouterr().err
    assert "CLOUD-1-2-3-4-HOST" in warning
    assert "CLOUD-1-2-3-4-ORA-1521" in warning
    assert "does not appear in the report" in warning


def test_an_unknown_id_on_an_unknown_ip_still_says_so(capsys):
    """No ip twin is not "nothing to report" — the metrics still go nowhere."""
    overlay = {"servers": [{"server_id": "STRAY", "ip": "9.9.9.9", "os_health": {}}]}
    inventory = {"servers": [{"server_id": "KNOWN", "ip": "1.2.3.4"}]}

    assert inventory_render._merge_overlay(overlay, inventory) == 0
    # stderr since 2026-08-16: the warning is for the person watching, while stdout carries
    # a CLI's answer — `inventory-summary` printed both to the same stream and parsed as
    # neither. What the warning *says* is unchanged, and that is what these assert.
    warning = capsys.readouterr().err
    assert "STRAY" in warning and "not in the canonical inventory either" in warning


class _FullSummary(_Summary):
    """What render_html reads beyond the result list."""

    def __init__(self, results):
        super().__init__(results)
        self.status = "FAILED"
        self.window_end = "2026-08-05T05:42:53Z"
        self.passed_count = sum(1 for r in results if r.status == "PASSED")
        self.at_risk_count = sum(1 for r in results if r.status == "AT_RISK")
        self.failed_count = sum(1 for r in results if r.status == "FAILED")
        self.no_data_count = 0


def test_the_rendered_page_actually_uses_the_sections():
    """The regression this test exists for: `_server_sections` was written, unit-tested and
    passing while `render_html` still built one flat table, so the published page was unchanged.
    Testing the helper proves the helper; only rendering the page proves the page."""
    page = publish.render_html(
        _FullSummary([
            _Result("srv-a/sqlserver/x", status="FAILED"),
            _Result("srv-b/sqlserver/x", status="PASSED"),
        ]),
        recent_runs=[],
    )
    assert "<h3>srv-a</h3>" in page
    assert "<h3>srv-b</h3>" in page
    assert page.index("<h3>srv-a</h3>") < page.index("<h3>srv-b</h3>")
