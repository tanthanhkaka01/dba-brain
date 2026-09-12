"""A fragmented-index row has to say enough to decide what to do about it.

Measured on 2026-09-10 against a real timekeeping instance, from a page an operator was reading and
could not act on. It listed `Fragmented (227)` and, fourteen times in a row, the same index name at
99.9%, 99.8%, 99.8%... which reads as a duplicated row.

They were not duplicates. They were **partitions** of one date-partitioned index, and that is the
whole finding rather than a rendering detail: the maintenance job had touched partitions 7, 8 and 9
on every run for sixteen days and had never once touched 10 through 21, which sat at 99%. Nothing
on the page could have shown that, because the page had no partition column and the collector put
every partition under one name.

Two more defects fell out of the same reading, both silent:

* the size was never shown, though the collector had measured `page_count` from the first version —
  and 99% fragmented is a different problem on 30 MB than on the 53 GB this instance really had;
* the action was **recomputed by the page** at `>= 30% = REBUILD`, while the collector that
  measured the row calls `>= 60%` REBUILD and the rest REORGANIZE. Every row between 30 and 59 was
  rendered as the opposite of what the tool had decided.
"""

from __future__ import annotations

from db_ops.reports.index_report import _float, _fragmented_section, _short_index_type


def _row(**kwargs):
    base = {"item": "DB\\dbo.T.IX", "pct": "50.0", "index_type": "NONCLUSTERED INDEX",
            "partition": "1/1", "page_count": "2000", "size_mb": "15.6",
            "stats_updated": "2026-09-10T02:09:32", "action": "REORGANIZE"}
    base.update(kwargs)
    return base


def test_the_worst_index_is_listed_first_and_not_sorted_as_a_string():
    # '100.0' < '99.9' as text, so the single worst index on the instance sorted to the bottom and
    # was the first row a capped table dropped.
    text = "\n".join(_fragmented_section(
        [_row(item="DB\\dbo.T.IX_b", pct="99.9"), _row(item="DB\\dbo.T.IX_a", pct="100.0")], None))
    assert text.index("IX_a") < text.index("IX_b")


def test_a_float_percentage_survives_being_read_as_a_number():
    assert _float("99.9") == 99.9
    assert _float("100.0%") == 100.0
    assert _float("") == -1.0
    assert _float(None) == -1.0


def test_partitions_of_one_index_are_not_counted_as_separate_indexes():
    rows = [_row(item="DB\\dbo.T.IX#p10", partition="10/21"),
            _row(item="DB\\dbo.T.IX#p11", partition="11/21"),
            _row(item="DB\\dbo.T.IX#p12", partition="12/21")]
    heading = _fragmented_section(rows, None)[0]
    assert "Fragmented (3)" in heading
    assert "3 partitions across 1 indexes" in heading


def test_an_instance_with_no_partitioned_index_gets_no_partition_wording():
    rows = [_row(item="DB\\dbo.T.IX_a"), _row(item="DB\\dbo.T.IX_b")]
    assert "partitions across" not in _fragmented_section(rows, None)[0]


def test_the_row_carries_the_size_because_that_is_what_the_rebuild_costs():
    text = "\n".join(_fragmented_section([_row(page_count="19585", size_mb="153.0")], None))
    assert "19,585" in text
    assert "153.0" in text
    assert "Total 153.0 MB of index to rebuild" in text


def test_the_page_reports_the_action_the_collector_chose_rather_than_recomputing_it():
    # 45% is REORGANIZE by the collector's own >=60 rule. The page used to call it REBUILD.
    text = "\n".join(_fragmented_section([_row(pct="45.0", action="REORGANIZE")], None))
    assert "REORGANIZE" in text
    assert "REBUILD" not in text


def test_a_row_stored_before_the_collector_carried_an_action_falls_back_to_the_same_threshold():
    older = {"item": "DB\\dbo.T.IX", "pct": "45.0", "message": "page_count=2000 | action=REORGANIZE"}
    assert "REORGANIZE" in "\n".join(_fragmented_section([older], None))
    worse = {"item": "DB\\dbo.T.IX", "pct": "88.0", "message": "page_count=2000"}
    assert "REBUILD" in "\n".join(_fragmented_section([worse], None))


def test_rows_collected_before_the_new_fields_still_appear_rather_than_vanishing():
    # The store holds weeks of them; hiding them would look like the fragmentation had been fixed.
    older = {"item": "DB\\dbo.T.IX", "pct": "72.0"}
    text = "\n".join(_fragmented_section([older], None))
    assert "DB\\dbo.T.IX" in text
    assert "72.0%" in text
    assert "| - |" in text


def test_the_limited_scan_says_which_columns_it_cannot_answer():
    # record_count and page density need a SAMPLED scan; claiming them from a LIMITED one, or
    # leaving the absence unexplained, are both worse than saying so.
    text = "\n".join(_fragmented_section([_row()], None))
    assert "LIMITED" in text and "SAMPLED" in text


def test_the_index_type_loses_the_word_index_because_the_table_is_already_of_indexes():
    assert _short_index_type("NONCLUSTERED INDEX") == "NONCLUSTERED"
    assert _short_index_type("CLUSTERED INDEX") == "CLUSTERED"
    assert _short_index_type("HEAP") == "HEAP"
    assert _short_index_type("") == "-"


def test_the_cap_still_reports_what_it_left_out():
    rows = [_row(item=f"DB\\dbo.T.IX_{n}", pct=str(90 - n)) for n in range(10)]
    text = "\n".join(_fragmented_section(rows, 3))
    assert "and 7 more" in text
