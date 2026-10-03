"""The instance pickers show one row per engine: SQL Server, Oracle, PostgreSQL, Host only.

The operator, 2026-10-02: the header that picks an instance "should be split into sections". The
server-metrics picker was one row of 77 buttons - SQL Server instances, Oracle and PostgreSQL
databases and bare hosts in inventory order - and the index-report picker the same in name order,
so finding "the Oracle servers" meant reading every button. Both now group by the one rule in
``lib.engine_sections``, in the same order, with a count per engine.
"""

from __future__ import annotations

from db_ops.reports import index_report as ir
from db_ops.reports import server_report


def _entry(server_id, engine=""):
    entry = {"server_id": server_id, "ip": "", "collected_at": "", "totals": {"indexes_total": 1},
             "databases": [], "disabled": [], "droppable": [], "fragmented": []}
    if engine:
        entry["engine"] = engine
    return entry


def test_an_index_peer_carries_its_engine_and_one_with_none_is_sql_server():
    peers = {peer["server_id"]: peer for peer in ir.build_peer_links({
        "MS-1": _entry("MS-1"), "ORA-1": _entry("ORA-1", "oracle"), "PG-1": _entry("PG-1", "postgresql"),
    })}
    assert peers["MS-1"]["engine"] == "sqlserver"
    assert peers["ORA-1"]["engine"] == "oracle"
    assert peers["PG-1"]["engine"] == "postgresql"


def test_the_index_picker_has_a_labelled_row_per_engine_in_page_order(tmp_path):
    fleet = {"PG-1": _entry("PG-1", "postgresql"), "ORA-1": _entry("ORA-1", "oracle"),
             "MS-1": _entry("MS-1"), "MS-2": _entry("MS-2")}
    page = ir.write_index_report_html(fleet["MS-1"], ir.format_index_report(fleet["MS-1"]), tmp_path,
                                      ir.build_peer_links(fleet)).read_text(encoding="utf-8")
    picker = page[page.index('class="picker"'):]
    assert picker.index("SQL Server <b>2</b>") < picker.index("Oracle <b>1</b>")
    assert picker.index("Oracle <b>1</b>") < picker.index("PostgreSQL <b>1</b>")
    # Each server sits in its own engine's row.
    assert picker.index("SQL Server") < picker.index("MS-2") < picker.index("Oracle")
    assert picker.index("Oracle") < picker.index(ir.html_file_name("ORA-1")) < picker.index("PostgreSQL")


def test_the_index_picker_shows_no_heading_for_an_engine_it_has_nothing_under(tmp_path):
    fleet = {"MS-1": _entry("MS-1"), "MS-2": _entry("MS-2")}
    page = ir.write_index_report_html(fleet["MS-1"], ir.format_index_report(fleet["MS-1"]), tmp_path,
                                      ir.build_peer_links(fleet)).read_text(encoding="utf-8")
    assert "SQL Server <b>2</b>" in page
    assert "Oracle <b>" not in page and "PostgreSQL <b>" not in page


def test_the_server_metrics_page_carries_the_sections_and_each_server_its_engine():
    servers = [{"slug": "ms-1", "name": "MS-1", "engine": "sqlserver", "status": "OK", "file": "a.json"},
               {"slug": "h-1", "name": "H-1", "engine": "host", "status": "OK", "file": "b.json"}]
    page = server_report.render_page(servers=servers, snapshot_date="2026-10-02", stamp="20261002",
                                     days=7, inventory_href="database-inventory.html")
    assert "__ENGINE_SECTIONS__" not in page and "__SERVERS__" not in page
    assert '["sqlserver","SQL Server"]' in page and '["host","Host only"]' in page
    assert '"engine":"host"' in page
    assert "ENGINE_SECTIONS.map" in page, "the picker is drawn per section, not as one flat row"
