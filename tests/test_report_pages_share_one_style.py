"""The report pages are built from one stylesheet, in the model the operator asked for.

2026-10-02: *look on the internet for a standard, common, widely used template and learn from it*.
The model is the admin-dashboard family (Tabler beside AdminLTE and CoreUI) taken as a vocabulary -
masthead, stat cards, section heads, card tables, badges, a picker - in the inventory page's
palette. The index pages were the odd one out: plain grey-bordered HTML tables with their own inline
CSS. They and the SLA page now inline ``lib.page_style.CSS``, so a token changed once changes every
page, and no page carries a private copy of the parts.
"""

from __future__ import annotations

from db_ops.lib import page_style
from db_ops.reports import index_report as ir
from db_ops.sla.models import SlaValidationSummary
from db_ops.sla.publish import render_html


def _entry(**totals):
    return {"server_id": "ACME-1", "ip": "192.0.2.1", "collected_at": "", "totals": dict(totals),
            "databases": [], "disabled": [], "droppable": [], "fragmented": []}


def test_the_stylesheet_carries_the_shared_parts():
    for part in (".masthead", ".kpi-strip", ".sec-head", ".tbl-scroll", ".b-crit", ".picker", ".callout"):
        assert part in page_style.CSS, part


def test_a_counter_is_coloured_only_when_it_is_not_zero():
    assert page_style.kpi_class(0, "alert") == ""
    assert page_style.kpi_class(3, "alert") == "alert"


def test_the_index_page_inlines_the_shared_stylesheet_and_opens_with_a_masthead(tmp_path):
    entry = _entry(indexes_total=674, used=411, disabled=2, droppable=45)
    page = ir.write_index_report_html(entry, ir.format_index_report(entry), tmp_path).read_text(encoding="utf-8")
    assert page_style.TOKENS.strip() in page
    assert '<header class="masthead">' in page and '<h1 class="title">ACME-1</h1>' in page
    assert '<div class="tbl-scroll"><table>' in page


def test_the_index_page_states_its_totals_as_stat_cards_coloured_by_what_they_mean(tmp_path):
    entry = _entry(indexes_total=674, used=411, disabled=2, droppable=0)
    page = ir.write_index_report_html(entry, ir.format_index_report(entry), tmp_path).read_text(encoding="utf-8")
    strip = page[page.index('class="kpi-strip"'):page.index("</header>")]
    assert ">674<" in strip and "indexes total" in strip
    assert '<div class="kpi alert"><div class="num">2</div>' in strip, "a disabled index is an alarm"
    assert '<div class="kpi "><div class="num">0</div><div class="lbl">droppable' in strip, "a zero is not"


def test_the_report_title_line_is_not_repeated_under_the_masthead(tmp_path):
    entry = _entry(indexes_total=1)
    text = ir.format_index_report(entry)
    assert text.splitlines()[0].startswith("Index Usage Report")
    page = ir.write_index_report_html(entry, text, tmp_path).read_text(encoding="utf-8")
    assert "Index Usage Report" not in page


def test_the_sla_page_inlines_the_same_stylesheet():
    summary = SlaValidationSummary(status="PASSED", policy_count=0, result_count=0, passed_count=0,
                                   at_risk_count=0, failed_count=0, no_data_count=0,
                                   window_end="2026-10-02T00:00:00Z", results=())
    page = render_html(summary, recent_runs=[])
    assert page_style.TOKENS.strip() in page
    assert "\x15" not in page, "a CSS escape written as a Python octal escape"


def test_every_report_page_takes_the_whole_window():
    """The operator, 2026-10-03: *is the width fixed? why not use 100% of the client?* Every page
    capped its content at 1180px and centred it, so a desktop screen showed a third of its width as
    margin while the tables inside scrolled sideways. The model's own answer is its fluid layout."""
    from pathlib import Path

    import db_ops.reports as reports

    templates = Path(reports.__file__).resolve().parent / "templates"
    sheets = {"lib.page_style": page_style.CSS,
              "inventory_report.html": (templates / "inventory_report.html").read_text(encoding="utf-8"),
              "server_report.html": (templates / "server_report.html").read_text(encoding="utf-8")}
    for name, css in sheets.items():
        assert ".wrap{max-width:none;" in css, name
        assert "1180px;" not in css.replace("1180px and", ""), f"{name} still caps a container"


def test_an_index_name_wraps_instead_of_widening_its_table(tmp_path):
    """An index is named `database.schema.table.index` - one unbroken token, 125 characters on the
    estate's largest server, beside fifteen more columns. The table needed 1898px in a 1134px box."""
    entry = _entry(indexes_total=1)
    page = ir.write_index_report_html(entry, ir.format_index_report(entry), tmp_path).read_text(encoding="utf-8")
    assert ".content tbody td:first-child{overflow-wrap:anywhere" in page


def test_the_name_column_takes_the_same_share_of_every_table_on_the_index_page(tmp_path):
    """The operator, 2026-10-03, on *Fragmented* above *All indexes*: *the two segments show
    differently, between the index name and the other columns*. Eight columns gave the names 780px
    and sixteen gave them 640px on one screen, so the columns after the name began in two places;
    and the name was set in another face and size than the cells beside it."""
    entry = _entry(indexes_total=1)
    page = ir.write_index_report_html(entry, ir.format_index_report(entry), tmp_path).read_text(encoding="utf-8")
    assert ".content thead th:first-child,.content tbody td:first-child{width:34%}" in page
    own = page[page.index(".content tbody td:first-child{overflow-wrap"):]
    assert "font-family" not in own[:own.index("}")], "the name reads in the face of the other cells"


def test_an_index_is_named_one_way_in_every_table_of_the_page():
    """The fragmentation collector writes `database\\schema.table.index`, the usage collector
    `database.schema.table.index`: one index, two spellings on one page, and a name copied from
    *Fragmented* was not found by searching *All indexes*."""
    rows = ir._fragmented_section(
        [{"item": "APPDB\\dbo.Orders.IX_Orders_Date", "pct": "99.4", "partition": "1/1",
          "index_type": "CLUSTERED INDEX", "page_count": "28067", "size_mb": "219.3",
          "stats_updated": "2026-10-02T01:03:55", "action": "REBUILD"}], None)

    assert any("| `APPDB.dbo.Orders.IX_Orders_Date` | 1/1 |" in row for row in rows)


def test_the_fleet_matrix_lets_a_long_mount_point_wrap():
    from pathlib import Path

    import db_ops.reports as reports

    css = (Path(reports.__file__).resolve().parent / "templates" / "inventory_report.html").read_text(encoding="utf-8")
    assert ".fill-cell{white-space:normal; overflow-wrap:anywhere}" in css
