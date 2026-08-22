"""Reading a report as it was on a past date, over HTTP.

The fleet inventory could always do this because it is published once per run under a stamped
name. The other two reports could not: ``server-metrics.html``, its per-server series JSON and
``index-usage_<slug>.html`` keep stable names and are overwritten every run, deliberately —
stamping them per run measured ~12 MB a build against a two-hourly workflow.

So they are archived once per calendar day instead, and the host resolves ``?date=`` for any file,
not just the one latest link. These tests pin the two halves of that: the day-stamped name is
found and compared correctly against the inventory's finer per-run stamp, and a request that has
no archive falls through to the live file rather than 404ing.

The HTTP tests run a real server on an ephemeral port, because the thing being tested is a request
handler — asserting on the helper functions alone would not catch a path that never reaches them.
"""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from db_ops.lib import report_archive
from db_ops.webhost.server import (
    make_handler,
    normalize_stamp,
    parse_date_param,
    refresh_latest,
    snapshot_at_or_before,
)

LATEST = "database-inventory.html"
LATEST_GLOB = "*_database-inventory-report.html"


# --------------------------------------------------------------------------- #
# Stamps: two widths that have to sort against each other
# --------------------------------------------------------------------------- #
def test_a_day_only_stamp_reads_as_the_end_of_its_day():
    """The daily archive holds the LAST build of its day, and `?date=2026-08-01` means the last
    snapshot of that day. Padding to 00:00:00 instead would make a day's own archive unreachable
    from its own date."""
    assert normalize_stamp("20260801") == "20260801_235959"
    assert normalize_stamp("20260801_150000") == "20260801_150000"


def test_the_two_stamp_widths_order_against_each_other():
    # A per-run inventory stamp from the same day sorts before the day archive's end-of-day.
    assert normalize_stamp("20260801_150000") < normalize_stamp("20260801")
    assert normalize_stamp("20260801") < normalize_stamp("20260802_000001")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-08-01", "20260801_235959"), ("2026-08-01T06:00", "20260801_060000"), ("nonsense", None)],
)
def test_the_date_query_is_parsed_into_a_comparable_stamp(value, expected):
    assert parse_date_param(value) == expected


def test_the_newest_daily_archive_at_or_before_the_date_wins(tmp_path):
    for day in ("20260801", "20260802", "20260803"):
        (tmp_path / f"{day}_server-metrics.html").write_text(day, encoding="utf-8")

    def pick(date):
        return snapshot_at_or_before(tmp_path, "*_server-metrics.html",
                                     parse_date_param(date), suffix="server-metrics.html")

    assert pick("2026-08-02").name == "20260802_server-metrics.html"
    # A date after every archive gets the newest one, not nothing.
    assert pick("2026-09-01").name == "20260803_server-metrics.html"
    # ...and a date before the archive begins has no answer at all.
    assert pick("2026-07-01") is None


def test_a_slug_named_file_is_matched_on_its_whole_name(tmp_path):
    """`suffix` matters: two servers' series files differ only by slug, and reading the stamp as a
    fixed-width prefix would let one server's archive answer for another."""
    (tmp_path / "20260801_server-metrics_acme-1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "20260802_server-metrics_acme-2.json").write_text("{}", encoding="utf-8")

    got = snapshot_at_or_before(tmp_path, "*_server-metrics_acme-1.json",
                                parse_date_param("2026-08-03"), suffix="server-metrics_acme-1.json")
    assert got.name == "20260801_server-metrics_acme-1.json"


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #
@pytest.fixture
def serving(tmp_path):
    """A real host over a report directory; yields (fetch, root).

    The mount is a real directory rather than the symlink :func:`build_webroot` makes in
    production, so these run everywhere — the symlink is how the deployment gets the URL prefix,
    not something the request handler behaves differently under.
    """
    webroot = tmp_path / "webroot"
    root = webroot / "report_dba"
    root.mkdir(parents=True)

    handler = make_handler(directory=str(webroot), mount="report_dba", latest=LATEST,
                           latest_glob=LATEST_GLOB, root=root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def fetch(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/report_dba/{path}") as r:
            return r.read().decode("utf-8"), r.headers.get("X-Report-Snapshot")

    try:
        yield fetch, root
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_dated_request_serves_that_days_archive_of_a_stable_named_page(serving):
    fetch, root = serving
    (root / "server-metrics.html").write_text("TODAY", encoding="utf-8")
    for day, body in (("20260801", "FIRST-OF-AUGUST"), ("20260802", "SECOND-OF-AUGUST")):
        (root / f"{day}_server-metrics.html").write_text(body, encoding="utf-8")

    body, snapshot = fetch("server-metrics.html?date=2026-08-01")
    assert body == "FIRST-OF-AUGUST"
    assert snapshot == "20260801_server-metrics.html"

    # No date at all is still the live build.
    body, snapshot = fetch("server-metrics.html")
    assert body == "TODAY" and snapshot is None


def test_the_series_json_the_page_fetches_is_dated_too(serving):
    """The page is only an index; if its series came from today, a page dated last week would
    draw this morning's charts under last week's header."""
    fetch, root = serving
    (root / "server-metrics_acme-1.json").write_text(json.dumps({"day": "today"}), encoding="utf-8")
    (root / "20260801_server-metrics_acme-1.json").write_text(
        json.dumps({"day": "2026-08-01"}), encoding="utf-8")

    body, snapshot = fetch("server-metrics_acme-1.json?v=abc&date=2026-08-01")
    assert json.loads(body)["day"] == "2026-08-01"
    assert snapshot == "20260801_server-metrics_acme-1.json"


def test_an_index_usage_page_is_dated_by_the_same_rule(serving):
    fetch, root = serving
    (root / "index-usage_acme-1.html").write_text("LIVE", encoding="utf-8")
    (root / "20260801_index-usage_acme-1.html").write_text("ARCHIVED", encoding="utf-8")

    body, snapshot = fetch("index-usage_acme-1.html?date=2026-08-01")
    assert body == "ARCHIVED" and snapshot == "20260801_index-usage_acme-1.html"


def test_a_date_with_no_archive_falls_through_to_the_live_file(serving):
    """A 404 would be the strictly correct answer and the useless one: the reader asked for a day
    the archive does not reach, and the newest build still tells them something true."""
    fetch, root = serving
    (root / "server-metrics.html").write_text("LIVE", encoding="utf-8")
    (root / "20260801_server-metrics.html").write_text("ARCHIVED", encoding="utf-8")

    body, snapshot = fetch("server-metrics.html?date=2020-01-01")
    assert body == "LIVE" and snapshot is None


def test_the_inventory_latest_link_still_resolves_by_its_own_stamped_names(serving):
    """The inventory is the one report whose stamped files are named differently from the link
    that serves them, so it keeps its own glob. Generalising `?date=` must not break it."""
    fetch, root = serving
    for stamp, body in (("20260801_235152", "AUG-1"), ("20260803_075205", "AUG-3")):
        (root / f"{stamp}_database-inventory-report.html").write_text(body, encoding="utf-8")
    refresh_latest(root, LATEST, LATEST_GLOB)
    if not (root / LATEST).exists():  # pragma: no cover - Windows without symlink rights
        pytest.skip("the latest symlink needs privileges this machine does not have")

    body, snapshot = fetch(f"{LATEST}?date=2026-08-01")
    assert body == "AUG-1" and snapshot == "20260801_235152_database-inventory-report.html"


# --------------------------------------------------------------------------- #
# The archive itself
# --------------------------------------------------------------------------- #
def test_archiving_keeps_the_live_name_and_adds_one_dated_copy(tmp_path):
    """Copy, not move: the stable name is what every link and bookmark points at."""
    live = tmp_path / "server-metrics.html"
    live.write_text("BUILD", encoding="utf-8")

    written = report_archive.archive_daily([live], stamp="20260803_075205")

    assert live.read_text(encoding="utf-8") == "BUILD"
    assert [p.name for p in written] == ["20260803_server-metrics.html"]


def test_a_later_run_the_same_day_replaces_that_days_copy(tmp_path):
    live = tmp_path / "server-metrics.html"
    live.write_text("09:00 BUILD", encoding="utf-8")
    report_archive.archive_daily([live], stamp="20260803_090000")
    live.write_text("17:00 BUILD", encoding="utf-8")
    report_archive.archive_daily([live], stamp="20260803_170000")

    archived = sorted(p.name for p in tmp_path.iterdir())
    assert archived == ["20260803_server-metrics.html", "server-metrics.html"]
    # The day's copy is the last build of that day, which is what `?date=` asks for.
    assert (tmp_path / "20260803_server-metrics.html").read_text(encoding="utf-8") == "17:00 BUILD"


def test_a_file_that_cannot_be_archived_does_not_fail_the_report_run(tmp_path):
    """Losing one day of history for one server is not a reason to fail the run that produced it."""
    real = tmp_path / "server-metrics.html"
    real.write_text("BUILD", encoding="utf-8")

    written = report_archive.archive_daily([real, tmp_path / "never-written.json"],
                                           stamp="20260803_075205")

    assert [p.name for p in written] == ["20260803_server-metrics.html"]


# --------------------------------------------------------------------------- #
# Backfilling a day that was never archived
# --------------------------------------------------------------------------- #
def test_the_end_of_day_is_what_a_backfilled_date_means():
    """A date-only `?date=` already means "the last snapshot of that day" everywhere else, and
    the archive it looks for holds the last build of the day. Rebuilding from the START of the
    day would produce a page holding none of that day's collections."""
    from db_ops.reports.backfill import _end_of_day

    assert _end_of_day("2026-08-01") == "2026-08-01T23:59:59Z"


def test_the_window_closes_at_the_as_of_moment(tmp_path):
    """Without a ceiling, a report built for 1 August reads rows collected on the 3rd and claims
    to be the 1st."""
    from db_ops.db.metric_store import as_of_text, cutoff_text

    assert as_of_text("2026-08-01T23:59:59Z") == "2026-08-01T23:59:59Z"
    assert as_of_text(None) is None
    # The window's floor moves with the ceiling, so `days` still means "the N days before the
    # moment being described", not "the N days before today".
    assert cutoff_text(7, as_of="2026-08-01T23:59:59Z") == "2026-07-25T23:59:59Z"


def test_a_backfill_writes_only_the_dated_copy(tmp_path):
    """The live name is the URL everybody reads as "now". A backfill putting 1 August's data
    there would be worse than having no history at all."""
    from db_ops.reports import server_report

    out = tmp_path / "reports"
    out.mkdir()
    (out / "server-metrics.html").write_text("LIVE BUILD", encoding="utf-8")

    server_report._write(out, "server-metrics.html", "1 AUGUST REBUILD",
                         stamp="20260801_235959", archive_only=True)

    assert (out / "server-metrics.html").read_text(encoding="utf-8") == "LIVE BUILD"
    assert (out / "20260801_server-metrics.html").read_text(encoding="utf-8") == "1 AUGUST REBUILD"


def test_a_normal_run_writes_both_names(tmp_path):
    from db_ops.reports import server_report

    out = tmp_path / "reports"
    out.mkdir()

    server_report._write(out, "server-metrics.html", "TODAY", stamp="20260803_094711",
                         archive_only=False)

    assert (out / "server-metrics.html").read_text(encoding="utf-8") == "TODAY"
    assert (out / "20260803_server-metrics.html").read_text(encoding="utf-8") == "TODAY"


def test_the_fleet_linked_server_table_tags_each_row_with_its_instance(tmp_path):
    """The fleet asks a question no per-server page can: a dead target is usually referenced from
    several instances at once, and the drop list is work somebody schedules once."""
    import db_ops.reports.inventory_report as inventory_report

    rows_by_server = [
        {"server_id": "ACME-2", "metric_code": "LINKED_SERVER_STATUS", "metric_item": "DEAD",
         "metric_value": "UNREACHABLE", "collected_at": "2026-08-03T00:00:00Z",
         "message": "linked_server=DEAD, usable=no, failure=UNREACHABLE, "
                    "referenced_by_procedures=0, referenced_by_objects=0, databases_referencing=0"},
        {"server_id": "ACME-1", "metric_code": "LINKED_SERVER_STATUS", "metric_item": "BROKEN",
         "metric_value": "UNREACHABLE", "collected_at": "2026-08-03T00:00:00Z",
         "message": "linked_server=BROKEN, usable=no, failure=UNREACHABLE, "
                    "referenced_by_procedures=2, referenced_by_objects=2, databases_referencing=1"},
    ]

    class _Store:
        def __init__(self, *a, **kw): pass
        def fetch_health_metrics(self, **_kw): return rows_by_server

    original = inventory_report.__dict__.get("MetricStore")
    import db_ops.db.metric_store as ms
    saved = ms.MetricStore
    ms.MetricStore = _Store
    try:
        rows = inventory_report.build_fleet_linked_servers(tmp_path / "x.sqlite", days=7)
    finally:
        ms.MetricStore = saved
        if original is not None:
            inventory_report.MetricStore = original

    assert [r["server_id"] for r in rows] == ["ACME-1", "ACME-2"]   # FIX before DROP
    assert [r["verdict"] for r in rows] == ["FIX", "DROP"]
