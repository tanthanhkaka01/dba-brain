"""Two collectors feed the index page and they do not agree on a separator.

Reported on 2026-09-11 against `index-usage_acme-192-0-2-250.html`: the Fragmented table listed
162 partitions across 26 indexes and showed `-` in Part, Type, Pages, Size MB and Stats updated on
every one of them. Action was filled, which is what made it look like the row had been read.

`MAINTENANCE_INDEX_FRAGMENTATION` writes pipes and `MAINTENANCE_INDEX_USAGE` writes commas, and
`_kv` split on commas only — so a fragmentation message came back as a single field named `db`
holding the whole rest of the message. The columns had been added, the collector had been taught to
measure them, and the parser between the two was never told. The Action column was not the
collector's verdict either: it was `_fragmented_section` recomputing one because `action` had not
parsed, which is the exact defect that function was written to remove.

The messages below are real, copied from the store on 2026-09-11, with the instance's address and
database name replaced by the pseudonyms the export gate requires — the separators, field order and
values are untouched, and those are what this test is about.
"""

from __future__ import annotations

from db_ops.reports.index_report import _kv, collect_index_rows

FRAGMENTATION_MESSAGE = (
    "db=APPDB_Prod | schema=schedule | table=EmployeeProfileDay "
    "| index=IX_EmployeeProfileDay_TE | index_type=NONCLUSTERED INDEX | partition=9/21 "
    "| page_count=3781 | size_mb=29.5 | stats_updated=2026-09-11T00:46:42 | action=REBUILD"
)

USAGE_MESSAGE = (
    "COLD: db=APPDB_Prod, schema=emp, table=ContractTemplateParam, "
    "index_name=PK_ContractTemplateParam, index_id=1, type_desc=CLUSTERED, is_unique=1, "
    "is_disabled=0, has_filter=0, user_seeks=0, user_scans=0, user_lookups=0, user_updates=0, "
    "last_read=never, last_stats_update=never, uptime_days=2.4, is_primary_key=1, "
    "is_unique_constraint=0 | primary key: enforces uniqueness and is usually the clustered "
    "structure; action=KEEP (low reads are normal)"
)


def test_a_pipe_separated_fragmentation_row_yields_every_field():
    fields = _kv(FRAGMENTATION_MESSAGE)

    assert fields["index_type"] == "NONCLUSTERED INDEX"
    assert fields["partition"] == "9/21"
    assert fields["page_count"] == "3781"
    assert fields["size_mb"] == "29.5"
    assert fields["stats_updated"] == "2026-09-11T00:46:42"
    # The collector's own verdict. Without it the page recomputes one, which is how a row between
    # 30% and 59% used to be called REBUILD against the collector that called it REORGANIZE.
    assert fields["action"] == "REBUILD"


def test_a_comma_separated_usage_row_still_yields_every_field():
    """The regression the obvious fix would have caused. A usage message contains a pipe too —
    before its trailing advice sentence — so "has a pipe" is true of both formats, and choosing on
    it turns this eighteen-field row into two."""
    fields = _kv(USAGE_MESSAGE)

    assert fields["db"] == "APPDB_Prod"
    assert fields["type_desc"] == "CLUSTERED"
    assert fields["user_seeks"] == "0"
    assert fields["uptime_days"] == "2.4"
    assert len(fields) >= 17


def test_the_kind_prefix_is_stripped_and_a_timestamp_is_not_mistaken_for_one():
    """`COLD:` is a prefix and must go; the colons inside `00:46:42` are inside a value."""
    assert _kv("COLD: db=APP, schema=dbo")["db"] == "APP"
    assert _kv(FRAGMENTATION_MESSAGE)["db"] == "APPDB_Prod"


def test_the_fragmented_table_shows_the_measurements_rather_than_dashes(monkeypatch):
    """End to end, through the builder: a partitioned row reaches the page with its identity, its
    size and the collector's action — the five columns that were `-` on all 162 rows."""
    from db_ops.reports import index_report

    rows = [{
        "server_id": "ACME-198-51-100-9", "ip": "198.51.100.9", "db_type": "sqlserver",
        "metric_code": "MAINTENANCE_INDEX_FRAGMENTATION",
        "metric_item": "APPDB_Prod\\schedule.EmployeeProfileDay.IX_EmployeeProfileDay_TE#p9",
        "metric_value": "99.9", "metric_unit": "pct", "status": "WARNING",
        "collected_at": "2026-09-11T00:46:42Z", "message": FRAGMENTATION_MESSAGE,
    }]

    class _Store:
        def __init__(self, _source):
            pass

        def fetch_health_metrics(self, **_kwargs):
            return rows

    monkeypatch.setattr(index_report, "MetricStore", _Store)
    entry = collect_index_rows("ignored")["ACME-198-51-100-9"]
    fragmented = entry["fragmented"][0]

    assert fragmented["partition"] == "9/21"
    assert fragmented["index_type"] == "NONCLUSTERED INDEX"
    assert fragmented["page_count"] == "3781"
    assert fragmented["size_mb"] == "29.5"
    assert fragmented["stats_updated"] == "2026-09-11T00:46:42"
    assert fragmented["action"] == "REBUILD"


def test_the_rendered_table_carries_the_size_it_totals():
    """`Total N MB of index to rebuild` is summed from `size_mb`. With the parse broken it summed
    to zero and the line disappeared, so the page could not say how big the job was."""
    from db_ops.reports.index_report import _fragmented_section

    rendered = "\n".join(_fragmented_section(
        [{"item": "APPDB_Prod\\schedule.T.IX_T#p9", "pct": "99.9", "message": FRAGMENTATION_MESSAGE,
          "index_type": "NONCLUSTERED INDEX", "partition": "9/21", "page_count": "3781",
          "size_mb": "29.5", "stats_updated": "2026-09-11T00:46:42", "action": "REBUILD"}],
        None))

    assert "29.5 MB of index to rebuild" in rendered
    assert "| 9/21 " in rendered
    assert "NONCLUSTERED " in rendered and "NONCLUSTERED INDEX" not in rendered
    assert "3,781" in rendered
    assert "2026-09-11 00:46:42" in rendered


def test_every_field_the_fragmentation_collector_writes_is_one_the_page_can_read():
    """The join that was missing. The collector names its fields in SQL, the page names them in
    Python, and nothing compared the two lists until the page had been blank for a day."""
    import re
    from pathlib import Path

    sql = Path("db_ops/metrics/collectors/sqlserver/061_sqlserver_index_fragmentation.sql").read_text(
        encoding="utf-8")
    # The per-index message only. The summary row below it is a different shape with its own
    # fields (`distinct_indexes`, `total_mb`) that no per-index row carries.
    detail = sql.split("UNION ALL")[0]
    written = set(re.findall(r"N'\s*\|?\s*([a-z_]+)='", detail))
    parsed = set(_kv(FRAGMENTATION_MESSAGE))

    assert written, "the collector's message no longer looks like k=v pairs"
    assert written <= parsed, (
        f"the collector writes fields the sample message does not carry: {sorted(written - parsed)}. "
        "Update FRAGMENTATION_MESSAGE from a real row, then check the page renders the new field.")
