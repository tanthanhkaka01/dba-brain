"""Why index health gets a report of its own, per server.

A per-index listing fits nowhere else. It cannot go in the hourly alert report — one server can
carry tens of thousands of indexes and they would bury every real alert. It cannot go in the chart
series — there is no time series in "this index has never been read". The inventory page carries the
counts, which say *how much* dead weight exists but not *which* indexes.

The rules these tests pin are the ones that make the listing safe to act on: never recommend
dropping something that enforces a rule, and treat a disabled clustered index as an incident rather
than as maintenance.
"""

import pytest

from db_ops.reports import index_report as ir
from conftest import shipped_config


def _row(**over):
    base = {"server_id": "S1", "ip": "10.0.0.1", "collected_at": "2026-08-01T00:00:00Z",
            "metric_code": ir.INDEX_METRIC_CODE, "metric_item": "db.dbo.T.IX_1",
            "metric_unit": "user_updates", "metric_value": "0", "status": "OK", "message": ""}
    base.update(over)
    return base


def _detail(item, *, msg_kind="UNUSED", **fields):
    parts = {"type_desc": "NONCLUSTERED", "is_unique": "0", "is_primary_key": "0",
             "is_unique_constraint": "0", "is_disabled": "0", "user_seeks": "0",
             "user_scans": "0", "user_lookups": "0", "user_updates": "0", "last_read": "never"}
    parts.update({k: str(v) for k, v in fields.items()})
    message = msg_kind + ": " + ", ".join(f"{k}={v}" for k, v in parts.items())
    return _row(metric_item=item, message=message)


def _collect(rows, monkeypatch):
    class _Store:
        def __init__(self, *_a, **_k):
            pass

        def fetch_health_metrics(self, **_k):
            return rows

    monkeypatch.setattr(ir, "MetricStore", _Store)
    return ir.collect_index_rows("ignored")


# ---------------------------------------------------------------------------
# What counts as a drop candidate
# ---------------------------------------------------------------------------
def test_a_primary_key_is_never_offered_as_a_drop_candidate(monkeypatch):
    """A primary key can show zero seeks for years and still enforce uniqueness and hold the
    clustered structure. Suggesting a DROP for one is a wrong instruction sitting in a report."""
    rows = [_detail("db.dbo.T.PK_T", is_primary_key=1)]

    entry = _collect(rows, monkeypatch)["S1"]

    assert entry["droppable"] == []


def test_a_unique_constraint_and_a_unique_index_are_never_drop_candidates(monkeypatch):
    rows = [_detail("db.dbo.T.UQ_T", is_unique_constraint=1),
            _detail("db.dbo.T.IX_uniq", is_unique=1)]

    entry = _collect(rows, monkeypatch)["S1"]

    assert entry["droppable"] == []


def test_a_clustered_index_is_never_a_drop_candidate(monkeypatch):
    """The clustered index IS the table storage."""
    rows = [_detail("db.dbo.T.CX_T", type_desc="CLUSTERED")]

    entry = _collect(rows, monkeypatch)["S1"]

    assert entry["droppable"] == []


def test_a_plain_nonclustered_index_nobody_reads_is_a_drop_candidate(monkeypatch):
    rows = [_detail("db.dbo.T.IX_dead", user_updates=5000)]

    entry = _collect(rows, monkeypatch)["S1"]

    assert [d["item"] for d in entry["droppable"]] == ["db.dbo.T.IX_dead"]
    assert entry["droppable"][0]["writes"] == 5000


def test_an_index_that_is_read_is_not_a_drop_candidate(monkeypatch):
    rows = [_detail("db.dbo.T.IX_used", user_seeks=1)]

    entry = _collect(rows, monkeypatch)["S1"]

    assert entry["droppable"] == []


# ---------------------------------------------------------------------------
# Disabled indexes, and the one that is an incident
# ---------------------------------------------------------------------------
def test_a_disabled_clustered_index_makes_the_whole_report_critical(monkeypatch):
    """The table is unreadable until it is rebuilt. That is not maintenance."""
    rows = [_detail("db.dbo.T.CX_T", type_desc="CLUSTERED", is_disabled=1)]
    entry = _collect(rows, monkeypatch)["S1"]

    assert any(d["clustered"] for d in entry["disabled"])
    text = ir.format_index_report(entry)
    assert "CRITICAL" in text
    assert "REBUILD now" in text


def test_a_disabled_nonclustered_index_is_listed_but_not_as_an_incident(monkeypatch):
    rows = [_detail("db.dbo.T.IX_off", is_disabled=1)]
    entry = _collect(rows, monkeypatch)["S1"]
    text = ir.format_index_report(entry)

    assert entry["disabled"] and not entry["disabled"][0]["clustered"]
    assert "CRITICAL" not in text
    assert "REBUILD to restore, or DROP" in text


def test_a_disabled_index_is_not_also_counted_as_droppable(monkeypatch):
    """It would otherwise appear twice with two different recommended actions."""
    rows = [_detail("db.dbo.T.IX_off", is_disabled=1)]

    entry = _collect(rows, monkeypatch)["S1"]

    assert entry["droppable"] == []


# ---------------------------------------------------------------------------
# Shape of the output
# ---------------------------------------------------------------------------
def test_every_detail_table_carries_a_recommended_action_column(monkeypatch):
    rows = [_detail("db.dbo.T.IX_dead", user_updates=10),
            _detail("db.dbo.T.IX_off", is_disabled=1)]
    entry = _collect(rows, monkeypatch)["S1"]

    text = ir.format_index_report(entry)

    for header in ("| Index | Type | Impact | Recommend action |",
                   "| Index | user_updates | last_read | State | Recommend action |"):
        assert header in text


def test_only_the_newest_collection_is_used(monkeypatch):
    """Each run replaces the whole picture. Mixing two runs would double-count every index."""
    old = _detail("db.dbo.T.IX_a", user_updates=1)
    old["collected_at"] = "2026-07-01T00:00:00Z"
    new = _detail("db.dbo.T.IX_b", user_updates=2)
    new["collected_at"] = "2026-08-01T00:00:00Z"

    entry = _collect([old, new], monkeypatch)["S1"]

    assert [d["item"] for d in entry["droppable"]] == ["db.dbo.T.IX_b"]


def test_the_counts_come_from_the_summary_row_not_from_the_listed_rows(monkeypatch):
    """The listing is capped; the counts must stay exact regardless."""
    summary = _row(metric_item="index_usage :: summary", metric_unit="summary",
                   message="indexes_total=949, used=364, unused=74, cold=511, disabled=0, droppable=67")
    entry = _collect([summary, _detail("db.dbo.T.IX_dead")], monkeypatch)["S1"]

    assert entry["totals"]["indexes_total"] == 949
    assert entry["totals"]["droppable"] == 67
    text = ir.format_index_report(entry, limit=1)
    assert "| droppable | 67 |" in text


def test_fragmentation_rows_that_are_collection_errors_are_not_counted(monkeypatch):
    """A query timeout is not a fragmentation finding, and must not inflate the count."""
    good = _row(metric_code=ir.FRAGMENTATION_METRIC_CODE, metric_item="db.dbo.T.IX_frag",
                metric_value="88.2", message="88.2%; page_count=100 | action=REBUILD")
    bad = _row(metric_code=ir.FRAGMENTATION_METRIC_CODE, metric_item="db",
               message="SQL execution failed: query timeout expired")

    entry = _collect([good, bad], monkeypatch)["S1"]

    assert len(entry["fragmented"]) == 1


def test_only_the_newest_fragmentation_collection_is_listed(monkeypatch):
    """Fragmentation is a snapshot, not a series. The metric samples nightly, so every run in the
    report window carries a row for the same index — and without dating the rows they stack up:
    APPDB_Prod listed "Fragmented (26)" on a day it had 4, one index at 96% appearing three times
    at 96.0 / 95.9 / 95.2 because it had been fragmented for three days."""
    rows = [
        _row(metric_code=ir.FRAGMENTATION_METRIC_CODE, metric_item="db.dbo.T.IX_frag",
             collected_at="2026-08-10T18:00:00Z", metric_value="95.2",
             message="95.2%; page_count=1337 | action=REBUILD"),
        _row(metric_code=ir.FRAGMENTATION_METRIC_CODE, metric_item="db.dbo.T.IX_frag",
             collected_at="2026-08-11T18:00:00Z", metric_value="95.9",
             message="95.9%; page_count=1337 | action=REBUILD"),
        _row(metric_code=ir.FRAGMENTATION_METRIC_CODE, metric_item="db.dbo.T.IX_frag",
             collected_at="2026-08-12T18:00:00Z", metric_value="96.0",
             message="96.0%; page_count=1337 | action=REBUILD"),
    ]

    entry = _collect(rows, monkeypatch)["S1"]

    assert [row["pct"] for row in entry["fragmented"]] == ["96.0"]


def test_fragmentation_is_dated_by_its_own_run_not_by_the_index_inventorys(monkeypatch):
    """The two metrics are separate and run on separate schedules, so their newest collections
    almost never share a timestamp. Filtering fragmentation against the inventory's stamp would
    drop every fragmentation row instead of de-duplicating them — an empty section reading as
    "nothing fragmented", which is the one wrong answer this table must never give."""
    inventory = _detail("db.dbo.T.IX_1")                      # collected_at 2026-08-01
    fragmentation = _row(metric_code=ir.FRAGMENTATION_METRIC_CODE,
                         metric_item="db.dbo.T.IX_frag",
                         collected_at="2026-08-12T18:00:00Z", metric_value="96.0",
                         message="96.0%; page_count=1337 | action=REBUILD")

    entry = _collect([inventory, fragmentation], monkeypatch)["S1"]

    assert len(entry["fragmented"]) == 1


def test_two_servers_do_not_share_one_fragmentation_clock(monkeypatch):
    """Each server is dated on its own. A server collected earlier than its neighbour must keep
    its rows rather than be silently emptied by the other's newer stamp."""
    early = _row(server_id="S1", metric_code=ir.FRAGMENTATION_METRIC_CODE,
                 metric_item="db.dbo.T.IX_a", collected_at="2026-08-10T18:00:00Z",
                 metric_value="91.0", message="91.0%; page_count=2000 | action=REBUILD")
    late = _row(server_id="S2", metric_code=ir.FRAGMENTATION_METRIC_CODE,
                metric_item="db.dbo.T.IX_b", collected_at="2026-08-12T18:00:00Z",
                metric_value="92.0", message="92.0%; page_count=2000 | action=REBUILD")

    servers = _collect([early, late], monkeypatch)

    assert len(servers["S1"]["fragmented"]) == 1
    assert len(servers["S2"]["fragmented"]) == 1


def test_a_clean_server_says_so_instead_of_rendering_empty_tables(monkeypatch):
    summary = _row(metric_item="index_usage :: summary", metric_unit="summary",
                   message="indexes_total=10, used=10, unused=0, cold=0, disabled=0, droppable=0")
    entry = _collect([summary], monkeypatch)["S1"]

    text = ir.format_index_report(entry)

    assert "No disabled indexes, no drop candidates and nothing fragmented." in text


# ---------------------------------------------------------------------------
# The report has to be reachable, not only stored
# ---------------------------------------------------------------------------
def test_the_report_links_to_the_server_dashboard_with_the_same_slug_the_page_uses():
    """Built with server_report.page_href, not a hand-formatted query string: a slug that drifts
    from the published one is a 404, which is worse than no link."""
    from db_ops.reports.server_report import page_href

    url = ir.server_dashboard_url("ACME-192-0-2-248")

    assert url.endswith(page_href("ACME-192-0-2-248"))
    assert "server=acme-192-0-2-248" in url


def test_the_published_file_name_is_the_one_the_slug_rule_produces(tmp_path):
    entry = {"server_id": "ACME-1", "ip": "", "collected_at": "", "totals": {"indexes_total": 1},
             "databases": [], "disabled": [], "droppable": [], "fragmented": []}
    text = ir.format_index_report(entry)

    path = ir.write_index_report_html(entry, text, tmp_path)

    assert path.name == ir.html_file_name("ACME-1")


def test_every_page_carries_a_picker_of_the_whole_fleet(tmp_path):
    """One file per server means no JavaScript can switch between them, so the set is only
    navigable if each page lists the others — the static equivalent of the metrics picker."""
    entry = {"server_id": "ACME-1", "ip": "", "collected_at": "", "totals": {"indexes_total": 1},
             "databases": [], "disabled": [], "droppable": [], "fragmented": []}
    peers = ir.build_peer_links({
        "ACME-1": entry,
        "ACME-2": dict(entry, server_id="ACME-2",
                      disabled=[{"item": "x", "type": "CLUSTERED", "clustered": True}]),
    })

    page = ir.write_index_report_html(entry, ir.format_index_report(entry), tmp_path,
                                      peers).read_text(encoding="utf-8")

    # The other server is a link; the one being read is not a link to itself.
    assert f'href="{ir.html_file_name("ACME-2")}"' in page
    assert f'href="{ir.html_file_name("ACME-1")}"' not in page
    # ...and a server with a disabled clustered index is flagged red in the picker itself.
    assert "#dc2626" in page


def test_a_lone_server_gets_no_picker(tmp_path):
    """A picker offering the page you are on is furniture, not navigation."""
    entry = {"server_id": "ACME-1", "ip": "", "collected_at": "", "totals": {"indexes_total": 1},
             "databases": [], "disabled": [], "droppable": [], "fragmented": []}

    page = ir.write_index_report_html(entry, ir.format_index_report(entry), tmp_path,
                                      ir.build_peer_links({"ACME-1": entry})).read_text(encoding="utf-8")

    assert 'class="picker"' not in page


def test_markdown_tables_become_real_html_tables(tmp_path):
    entry = {"server_id": "ACME-1", "ip": "", "collected_at": "",
             "totals": {"indexes_total": 1, "droppable": 1},
             "databases": [], "disabled": [],
             "droppable": [{"item": "db.dbo.T.IX_x", "writes": 9,
                            "last_read": "never", "kind": "UNUSED"}],
             "fragmented": []}
    text = ir.format_index_report(entry)

    page = ir.write_index_report_html(entry, text, tmp_path).read_text(encoding="utf-8")

    assert "<table>" in page and "<th>Recommend action</th>" in page
    assert "|---" not in page          # the alignment rule must not survive as text


# ---------------------------------------------------------------------------
# One publish step, not two schedules
# ---------------------------------------------------------------------------
def test_the_inventory_workflow_publishes_the_index_pages_too():
    """database-inventory.html, server-metrics.html and the index pages all land in the directory
    the webhost serves, so they are produced by the same workflow. A second scheduled command
    reading the same store on its own clock, to write into the same directory, only creates two
    schedules that can disagree about how fresh "the reports" are."""
    import inspect

    from db_ops.reports import inventory_summary

    source = inspect.getsource(inventory_summary.build_inventory_workflow)

    assert "create_index_reports" in source


def test_no_separate_scheduled_command_builds_the_index_report():
    """It was briefly its own app_command; folding it in is what removed the duplicate schedule."""
    import json
    from pathlib import Path

    commands = json.loads(shipped_config("app_commands.json").read_bytes().decode("utf-8-sig"))
    key = next(iter(commands))
    ids = {entry.get("app_command_id") for entry in commands[key]}

    assert "APP-REPORTS-INDEX-USAGE" not in ids
    # the workflow that does publish them must still be scheduled
    assert "APP-REPORTS-INVENTORY-WORKFLOW" in ids


# ---------------------------------------------------------------------------
# The published page is an inventory: it lists everything
# ---------------------------------------------------------------------------
def test_every_index_is_listed_not_only_the_actionable_ones(monkeypatch):
    """"Is THIS index used?" cannot be answered by a page that only shows the problems."""
    rows = [_detail("db.dbo.T.IX_used", user_seeks=10),
            _detail("db.dbo.T.PK_T", is_primary_key=1),
            _detail("db.dbo.T.IX_dead", user_updates=3)]

    entry = _collect(rows, monkeypatch)["S1"]

    assert {i["item"] for i in entry["indexes"]} == {
        "db.dbo.T.IX_used", "db.dbo.T.PK_T", "db.dbo.T.IX_dead"}


def test_optimizer_suggestions_are_not_counted_as_existing_indexes(monkeypatch):
    """MISSING rows are what the optimizer WISHED existed. Counting them made "All indexes" read
    1053 against a true total of 949 - exactly the 104 suggestions."""
    real = _detail("db.dbo.T.IX_real")
    missing = _row(metric_item="db.dbo.T", metric_unit="impact",
                   message="MISSING: table=dbo.T, columns=col1, estimated_impact=99")

    entry = _collect([real, missing], monkeypatch)["S1"]

    assert [i["item"] for i in entry["indexes"]] == ["db.dbo.T.IX_real"]


def test_the_published_page_is_not_truncated(monkeypatch):
    """limit=None is what the HTML uses; the stored copy keeps its cap because it is a database
    row that also feeds Telegram."""
    rows = [_detail(f"db.dbo.T.IX_{n}", user_updates=n) for n in range(40)]
    entry = _collect(rows, monkeypatch)["S1"]

    full = ir.format_index_report(entry, limit=None)
    capped = ir.format_index_report(entry, limit=5)

    assert "more_" not in full
    assert "more_" in capped
    for n in range(40):
        assert f"IX_{n}`" in full or f"IX_{n} " in full


def test_the_all_index_table_recommends_an_action_for_healthy_indexes_too(monkeypatch):
    rows = [_detail("db.dbo.T.PK_T", is_primary_key=1),
            _detail("db.dbo.T.IX_used", user_seeks=5)]
    entry = _collect(rows, monkeypatch)["S1"]

    text = ir.format_index_report(entry, limit=None)

    assert "KEEP — primary key" in text
    assert "keep — in use" in text


def test_the_count_in_the_heading_matches_the_totals_row(monkeypatch):
    """They disagreed once, and a report that contradicts itself is not trusted again."""
    summary = _row(metric_item="index_usage :: summary", metric_unit="summary",
                   message="indexes_total=2, used=1, unused=1, cold=0, disabled=0, droppable=1")
    rows = [summary, _detail("db.dbo.T.IX_a", user_seeks=1), _detail("db.dbo.T.IX_b")]
    entry = _collect(rows, monkeypatch)["S1"]

    text = ir.format_index_report(entry, limit=None)

    assert "All indexes (2)" in text
    assert "| indexes_total | 2 |" in text


# ---------------------------------------------------------------------------
# Two clocks, and neither is readable without the other
# ---------------------------------------------------------------------------
def test_usage_counters_are_reported_against_the_restart_they_started_from(monkeypatch):
    """`user_seeks = 0` is meaningless on its own: it says "not used SINCE an instant", and the
    instant is when the instance last restarted. Judging a drop candidate needs that date."""
    summary = _row(metric_item="index_usage :: summary", metric_unit="summary",
                   message="indexes_total=1, used=0, unused=1, cold=0, uptime_days=267.0, "
                           "counters_since=2025-11-07 13:37:41")
    entry = _collect([summary, _detail("db.dbo.T.IX_a")], monkeypatch)["S1"]

    text = ir.format_index_report(entry, limit=None)

    assert "2025-11-07 13:37:41" in text
    assert "not used since that instant" in text.replace('"', "")


def test_a_sample_shorter_than_a_business_week_warns_against_dropping(monkeypatch):
    """The metric reports detail from 12 hours of uptime, so this report is now readable long
    before its droppable count means anything. On the ERP FCI, which restarted 6 days earlier,
    every index that a weekly or month-end query serves still reads as cold — and the counts sit
    right next to a column headed "droppable"."""
    summary = _row(metric_item="index_usage :: summary", metric_unit="summary",
                   message="indexes_total=1, used=0, unused=1, cold=0, uptime_days=0.5, "
                           "counters_since=2026-07-26 18:33:25")
    entry = _collect([summary, _detail("db.dbo.T.IX_a")], monkeypatch)["S1"]

    text = ir.format_index_report(entry, limit=None)

    assert "Short sample" in text
    assert "do not drop anything" in text.lower()
    # The fraction survives: _int() would render half a day as "0 day(s)", which reads as no
    # sample at all rather than a short one.
    assert "0.5 day(s)" in text


def test_a_full_sample_carries_no_short_sample_warning(monkeypatch):
    """A warning that fires on every server is a warning nobody reads."""
    summary = _row(metric_item="index_usage :: summary", metric_unit="summary",
                   message="indexes_total=1, uptime_days=278.4, counters_since=2025-10-27 10:52:22")
    entry = _collect([summary, _detail("db.dbo.T.IX_a")], monkeypatch)["S1"]

    assert "Short sample" not in ir.format_index_report(entry, limit=None)


def test_an_unreadable_uptime_does_not_cry_wolf():
    assert ir._is_short_sample("") is False
    assert ir._is_short_sample(None) is False
    assert ir._is_short_sample("0.5") is True
    assert ir._is_short_sample("278.4") is False


def test_the_statistics_date_is_a_different_clock_and_says_so(monkeypatch):
    """Statistics age is set by UPDATE STATISTICS, not by usage. Conflating the two is how a
    heavily used index with months-old statistics gets read as healthy."""
    rows = [_detail("db.dbo.T.IX_a", user_seeks=5, last_stats_update="2026-01-02 03:04:05")]
    summary = _row(metric_item="index_usage :: summary", metric_unit="summary",
                   message="indexes_total=1, uptime_days=10, counters_since=2026-01-01 00:00:00")
    entry = _collect([summary] + rows, monkeypatch)["S1"]

    text = ir.format_index_report(entry, limit=None)

    assert "| last_stats_update |" in text
    assert "2026-01-02 03:04:05" in text
    assert "different clock" in text


def test_by_database_comes_before_the_drop_candidates():
    """The reader picks a database first, then reads its candidates — so the map goes above the
    list rather than after it."""
    entry = {"server_id": "S1", "ip": "", "collected_at": "", "indexes": [],
             "totals": {"indexes_total": 2, "droppable": 1},
             "databases": [{"database": "DB1", "indexes_total": 2, "cold": 1,
                            "disabled": 0, "droppable": 1}],
             "disabled": [],
             "droppable": [{"item": "DB1.dbo.T.IX_a", "writes": 1,
                            "last_read": "never", "kind": "UNUSED"}],
             "fragmented": []}

    text = ir.format_index_report(entry, limit=None)

    assert text.index("### By database") < text.index("### Drop candidates")


def test_the_restart_banner_is_the_first_thing_after_the_server_identity(monkeypatch):
    """It qualifies every number on the page. Under the tables it was read past, and a reader who
    misses it takes `user_seeks = 0` for "never used" and drops an index that is merely idle."""
    summary = _row(metric_item="index_usage :: summary", metric_unit="summary",
                   message="indexes_total=1, uptime_days=267.0, counters_since=2025-11-07 13:37:41")
    entry = _collect([summary, _detail("db.dbo.T.IX_a")], monkeypatch)["S1"]

    text = ir.format_index_report(entry, limit=None)

    banner = next(i for i, line in enumerate(text.splitlines()) if line.startswith("## Usage counters"))
    first_table = next(i for i, line in enumerate(text.splitlines()) if line.startswith("| Metric"))
    assert banner < first_table


def test_the_banner_renders_as_a_banner_not_a_paragraph(tmp_path, monkeypatch):
    summary = _row(metric_item="index_usage :: summary", metric_unit="summary",
                   message="indexes_total=1, uptime_days=267.0, counters_since=2025-11-07 13:37:41")
    entry = _collect([summary, _detail("db.dbo.T.IX_a")], monkeypatch)["S1"]
    text = ir.format_index_report(entry, limit=None)

    page = ir.write_index_report_html(entry, text, tmp_path).read_text(encoding="utf-8")

    assert '<div class="banner">' in page
    assert "2025-11-07 13:37:41" in page
    assert page.index('class="banner"') < page.index("<table>")
    # `## ` must not be mistaken for the `### ` section headings
    assert "<h3>Usage counters" not in page
