"""The SLA page reads at three levels: the estate, each database engine, each instance.

The operator, 2026-10-02: the page was "extremely ugly, and above all not split by instance at
all". Its per-question headings listed SLI codes and nothing else - "AVAILABILITY_SUCCESS_RATIO:
STALE" sixteen times, with no way to tell which sixteen instances - and there was no answer to "is
PostgreSQL fine while SQL Server is not". The layout now follows the SLO dashboards it was modelled
on: the overall verdict, one card per engine, a status grid of instance x SLI area, the checks that
did not pass with the instance they belong to, and each instance's detail.
"""

from __future__ import annotations

from db_ops.sla.models import SlaPolicyResult, SlaValidationSummary
from db_ops.sla.publish import render_html


def _result(target_id, status="PASSED", *, policy_id="AVAIL", domain="availability",
            current_status="OK", actual=100.0) -> SlaPolicyResult:
    return SlaPolicyResult(
        policy_id=policy_id, name=policy_id, target_id=target_id, scope="instance",
        category=domain, domain=domain, status=status, objective_percent=99.0, actual_percent=actual,
        error_budget_percent=1.0, budget_consumed_percent=0.0, budget_remaining_percent=100.0,
        total_count=10, good_count=10, bad_count=0, no_data=False,
        window_hours=168, window_start="2026-09-25T00:00:00Z", window_end="2026-10-02T00:00:00Z",
        current_status=current_status,
    )


def _summary(results) -> SlaValidationSummary:
    return SlaValidationSummary(
        status="FAILED" if any(r.status == "FAILED" for r in results) else "PASSED",
        policy_count=2, result_count=len(results),
        passed_count=sum(1 for r in results if r.status == "PASSED"),
        at_risk_count=sum(1 for r in results if r.status == "AT_RISK"),
        failed_count=sum(1 for r in results if r.status == "FAILED"),
        no_data_count=sum(1 for r in results if r.status == "NO_DATA"),
        window_end="2026-10-02T00:00:00Z", results=tuple(results),
    )


ESTATE = [
    _result("ORA-1/oracle/SALES"),
    _result("MS-1/sqlserver/ERP"),
    _result("MS-1/sqlserver/ERP", "FAILED", policy_id="RECOVERABILITY", domain="recoverability",
            current_status="BAD", actual=72.1),
    _result("MS-2/sqlserver/HR"),
    _result("PG-1/postgresql/store"),
    _result("PG-1/postgresql/store", "NO_DATA", policy_id="RECOVERABILITY", domain="recoverability",
            current_status="", actual=0.0),
]


def _page(results=ESTATE) -> str:
    return render_html(_summary(results), recent_runs=[])


def test_every_engine_gets_a_card_in_the_engine_order():
    page = _page()
    cards = page[page.index("By database engine"):page.index("Instance matrix")]
    assert cards.index("SQL Server") < cards.index("Oracle") < cards.index("PostgreSQL")


def test_an_engine_card_names_the_instances_to_look_at_and_not_the_healthy_ones():
    page = _page()
    sql_card = page[page.index('<span class="ename">SQL Server'):page.index('<span class="ename">Oracle')]
    assert "MS-1" in sql_card
    assert "MS-2" not in sql_card, "a passing instance is not something to look at"
    assert "1 bad right now" in sql_card


def test_an_engine_where_everything_passes_says_so():
    page = _page()
    oracle_card = page[page.index('<span class="ename">Oracle'):page.index('<span class="ename">PostgreSQL')]
    assert "Every instance passes every check." in oracle_card


def test_the_matrix_groups_instances_under_their_engine():
    page = _page()
    matrix = page[page.index("Instance matrix"):page.index("Needs attention")]
    assert matrix.index("SQL Server") < matrix.index("MS-1") < matrix.index("Oracle")
    assert matrix.index("Oracle") < matrix.index("ORA-1") < matrix.index("PostgreSQL")
    assert matrix.index("PostgreSQL") < matrix.index("PG-1")


def test_the_matrix_has_a_column_only_for_the_areas_that_have_checks():
    matrix = _page()
    assert "Availability" in matrix and "Backup &amp; recovery" in matrix
    assert "Capacity" not in matrix, "a column of dashes says nothing"


def test_a_matrix_cell_reads_the_measurement_not_the_float():
    page = _page()
    assert ">72.1%<" in page
    assert "72.10000" not in page


def test_needs_attention_names_the_instance_and_the_engine_of_every_check_that_did_not_pass():
    page = _page()
    attention = page[page.index("Needs attention"):page.index("Instance detail")]
    assert "MS-1" in attention and "PG-1" in attention
    assert "SQL Server" in attention and "PostgreSQL" in attention
    assert "ORA-1" not in attention and "MS-2" not in attention
    assert attention.index("MS-1") < attention.index("PG-1"), "a failure sorts above a gap in data"


def test_with_nothing_failing_needs_attention_says_so():
    page = _page([_result("MS-2/sqlserver/HR")])
    assert "Nothing - every check passed." in page


def test_instance_detail_is_grouped_by_engine_and_opens_only_what_needs_reading():
    page = _page()
    detail = page[page.index("Instance detail"):page.index("Recent runs")]
    assert detail.index("SQL Server") < detail.index('id="inst-ms-1"') < detail.index("Oracle")
    assert 'id="inst-ms-1" open' in detail
    assert 'id="inst-ms-2" open' not in detail
    assert 'id="inst-pg-1" open' in detail, "no data is not a pass"


def test_every_instance_link_points_at_its_detail_block():
    page = _page()
    assert 'href="#inst-ms-1"' in page and 'id="inst-ms-1"' in page


def test_the_headline_states_how_many_instances_on_how_many_engines():
    assert "4 instances on 3 engines" in _page()
