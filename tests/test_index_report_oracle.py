"""The index page for an engine that does not count index usage.

SQL Server hands out per-index seek/scan counters for free; Oracle records none unless every
index is individually placed into `ALTER INDEX ... MONITORING USAGE`, which is a change to the
database rather than to the monitor. The tempting shortcut — file Oracle's rows under
`MAINTENANCE_INDEX_USAGE` and leave the usage columns at zero — produces a page of several
hundred indexes that read as never used, which is exactly the reading that gets an index dropped
and a report query broken the following month.

So Oracle has its own metric and its own renderer, and what these tests pin is the refusal: no
usage columns, no drop candidates, and a heading that says the counters do not exist here. What
the page *does* carry is what Oracle states on its own — what exists, its size, whether it is
still usable, and how old its statistics are.
"""

from db_ops.reports.index_report import format_index_report


def _entry(indexes, totals=None, disabled=None):
    return {
        "server_id": "ACME-192-0-2-236", "ip": "192.0.2.236", "engine": "oracle",
        "collected_at": "2026-08-13T06:22:34Z",
        "totals": totals or {"indexes_total": len(indexes), "unusable": 0,
                             "unique_indexes": 1, "never_analyzed": 0, "stale_stats_30d": 1},
        "databases": [], "disabled": disabled or [], "droppable": [], "fragmented": [],
        "indexes": indexes,
    }


def _index(item, **kw):
    row = {"item": item, "kind": "", "type": "NORMAL", "index_id": "", "is_unique": 0,
           "is_primary": 0, "is_uq_constr": 0, "is_disabled": 0, "has_filter": 0,
           "table": "LTR.CT_CUT", "size_mb": "1.5", "extents": 2, "last_stats": "2026-01-04"}
    row.update(kw)
    return row


def test_the_page_says_up_front_that_this_instance_records_no_usage():
    text = format_index_report(_entry([_index("LTR.IX_CT_CUT_1")]))

    assert "Index Inventory Report" in text
    assert "does not record index usage" in text
    assert "MONITORING USAGE" in text


def test_no_drop_candidates_are_offered_where_nothing_measures_reads():
    """The SQL Server page's drop list is derived from seek/scan/lookup counts. An index with no
    counters is not an index with zero reads, and a page that cannot tell them apart must not
    recommend dropping anything."""
    text = format_index_report(_entry([_index("LTR.IX_UNUSED", is_unique=0)]))

    assert "Drop candidates" not in text
    assert "user_seeks" not in text and "user_updates" not in text


def test_an_unusable_index_is_reported_first_and_as_an_incident():
    text = format_index_report(_entry(
        [_index("LTR.IX_BROKEN", is_disabled=1, kind="UNUSABLE")],
        disabled=[{"item": "LTR.IX_BROKEN", "type": "NORMAL", "clustered": False}],
    ))

    assert "CRITICAL — 1 UNUSABLE index(es)" in text
    assert "ORA-01502" in text
    assert text.index("UNUSABLE index(es)") < text.index("All indexes")


def test_indexes_that_were_never_analyzed_get_their_own_list():
    """On 8i a table with no statistics is costed from defaults — the rule-based path in practice —
    so "never" is a different finding from "old", not a worse version of it."""
    text = format_index_report(_entry([
        _index("LTR.IX_NO_STATS", last_stats="never"),
        _index("LTR.IX_OLD_STATS", last_stats="2026-01-04"),
    ]))

    assert "Never analyzed (1)" in text
    assert "LTR.IX_NO_STATS" in text.split("### All indexes")[0]


def test_the_biggest_index_is_listed_first_because_size_is_the_only_cost_shown():
    text = format_index_report(_entry([
        _index("LTR.IX_SMALL", size_mb="0.5"),
        _index("LTR.IX_HUGE", size_mb="812.25"),
    ]))

    listing = text.split("### All indexes")[1]
    assert listing.index("LTR.IX_HUGE") < listing.index("LTR.IX_SMALL")


def test_a_sqlserver_entry_still_renders_the_usage_report():
    """The Oracle renderer is chosen by the entry's engine, and nothing else changes: a server
    without that marker must come out of the same function as before."""
    text = format_index_report({
        "server_id": "ACME-192-0-2-248", "ip": "192.0.2.248", "collected_at": "",
        "totals": {"indexes_total": 3, "used": 2, "unused": 1, "cold": 0, "disabled": 0,
                   "droppable": 1},
        "databases": [], "disabled": [], "droppable": [], "fragmented": [], "indexes": [],
    })

    assert text.startswith("Index Usage Report")
    assert "does not record index usage" not in text
