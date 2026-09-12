"""A published page that leaves this estate has to say when it was built, in a clock anyone can read.

A report is a file that gets shared. Inside the estate that is already handled: the head banner
prints the display zone with its offset, so everyone opening the page sees the same hour. The
**file name** was the half nobody had to think about, because the node writes it — until pages
started being copied out of the estate for `examples/showcase/`, and three things went wrong at
once:

* the node names its daily archive `20260825_sla.html` in its own display zone, so the same page
  copied by two operators in two countries is filed under two different days and neither name says
  which clock it is on;
* naming the copy after *now* instead would put a fortnight of archived days under one date — the
  moment it was downloaded is a different claim from the moment it was built;
* renaming the files at all breaks every link the pages ship with, because they link to each other
  by bare file name.

So the page is asked, not the clock: `page_banner.snapshot_stamp` reads back what `render` wrote,
`timezone.parse_display` turns it into an instant, and the name states it as UTC. These tests hold
that chain together end to end — a reader that drifts from its writer fails on the one format
nobody tested.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db_ops.common import showcase
from db_ops.lib import page_banner
from db_ops.lib import timezone as tz


def test_a_rendered_stamp_can_be_read_back_into_the_moment_it_names():
    moment = datetime(2026, 9, 12, 1, 30, 33, tzinfo=timezone.utc)
    rendered = tz.format_display(moment, "+07:00")

    assert rendered == "2026-09-12 08:30:33 +07"
    assert tz.parse_display(rendered) == moment


def test_a_stamp_without_an_offset_is_refused_rather_than_guessed_at():
    # The whole reason format_display always prints an offset is that an unlabelled wall-clock time
    # cannot be placed on any axis. Reading one as UTC here would put that guess back, silently.
    assert tz.parse_display("2026-09-12 08:30:33") is None
    assert tz.parse_display("not a timestamp") is None
    assert tz.parse_display("") is None


def test_the_utc_file_stamp_says_which_clock_it_is_on():
    moment = datetime(2026, 9, 12, 8, 30, 33, tzinfo=timezone(timedelta(hours=7)))

    # 08:30 +07 is 01:30 UTC, and the name carries the Z that makes that readable in Berlin.
    assert tz.utc_file_stamp(moment) == "20260912T0130Z"


def test_the_banner_hands_its_moment_back_to_whoever_asks_the_page():
    markup = page_banner.render(title="SLA", snapshot_at="2026-08-25 23:32:25 +07")

    assert page_banner.snapshot_stamp(markup) == "2026-08-25 23:32:25 +07"
    assert page_banner.snapshot_stamp("<p>a page with no banner</p>") == ""


def test_a_showcase_page_is_named_after_the_day_it_covers_not_the_day_it_was_copied(tmp_path):
    # Two pages from two days, copied in one pass. If the copy time named them, both would be
    # filed under today and a window of history would read as one afternoon.
    source = tmp_path / "reports"
    source.mkdir()
    for name, stamp in (("20260825_sla.html", "2026-08-25 23:32:25 +07"),
                        ("sla.html", "2026-09-12 08:30:33 +07")):
        (source / name).write_text(
            page_banner.render(title="SLA", snapshot_at=stamp), encoding="utf-8")

    names = sorted(
        showcase._output_name(path, path.read_text(encoding="utf-8"),
                              _no_op_mapping(), stamped=True)
        for path in sorted(source.iterdir()))

    assert names == ["20260825T1632Z_sla.html", "20260912T0130Z_sla.html"]


def test_a_page_with_no_banner_keeps_the_name_its_node_gave_it(tmp_path):
    # Stamping it with the moment it was COPIED would state something the page never said - and it
    # would not even distinguish: a whole window of unbannered days would collapse onto one name
    # and overwrite itself down to a single file. `build` reports these under `kept_node_naming`.
    source = tmp_path / "reports"
    source.mkdir()
    names = []
    for name in ("20260901_page.html", "20260903_page.html"):
        page = source / name
        page.write_text("<p>a page from before the banner existed</p>", encoding="utf-8")
        names.append(showcase._output_name(page, page.read_text(encoding="utf-8"),
                                           _no_op_mapping(), stamped=True))

    assert names == ["20260901_page.html", "20260903_page.html"]


def test_renaming_the_pages_repoints_the_links_between_them():
    # A snapshot nobody can browse is a folder of orphans: the pages link to each other by bare
    # file name, and every one of those names has just changed.
    targets = {"sla.html": "20260912T0130Z_sla.html",
               "database-inventory.html": "20260912T0126Z_database-inventory.html"}
    markup = ('<a href="sla.html">SLA</a>'
              '<a href="database-inventory.html?date=2026-09-01">Fleet</a>'
              '<a href="https://example.com/sla.html">elsewhere</a>')

    rewritten = showcase._relink(markup, targets)

    assert 'href="20260912T0130Z_sla.html"' in rewritten
    # The query is dropped: `?date=` is answered by a running web host reading its archive, and a
    # folder of files has no host to ask.
    assert 'href="20260912T0126Z_database-inventory.html"' in rewritten
    assert "?date=" not in rewritten
    # An absolute link belongs to somebody else and is left exactly as it was.
    assert 'href="https://example.com/sla.html"' in rewritten


def test_a_page_links_to_the_sibling_from_its_own_day_and_never_forward():
    # Across a captured window the answer is not obvious, so the rule is stated: the same day when
    # the window holds it, otherwise the newest copy at or before this page's day. A page from the
    # 25th linking forward to the 12th would read as one estate on two dates.
    from pathlib import Path

    names = {Path("a"): "20260825T1632Z_sla.html",
             Path("b"): "20260912T0130Z_sla.html",
             Path("c"): "20260912T0126Z_database-inventory.html"}

    from_the_25th = showcase._link_targets("20260825T1632Z_index-usage_x.html", names)
    from_today = showcase._link_targets("20260912T0130Z_index-usage_x.html", names)

    assert from_the_25th["sla.html"] == "20260825T1632Z_sla.html"
    assert from_today["sla.html"] == "20260912T0130Z_sla.html"
    # Nothing older exists for the inventory, so the 25th gets the only copy there is rather than
    # a dangling link.
    assert from_the_25th["database-inventory.html"] == "20260912T0126Z_database-inventory.html"


def test_a_showcase_can_be_rebuilt_over_its_own_output_without_stacking_stamps():
    # Re-running is the supported way to refresh what ships, and every re-run would otherwise add
    # another prefix: 20260912T0130Z_20260912T0130Z_20260825_sla.html.
    assert showcase.stable_name("20260912T0130Z_sla.html") == "sla.html"
    assert showcase.stable_name("20260825_sla.html") == "sla.html"
    assert showcase.stable_name(
        "20260908_205847_database-inventory-report.html") == "database-inventory-report.html"
    assert showcase.stable_name("sla.html") == "sla.html"


def _no_op_mapping():
    """A mapping that renames nothing, so these tests read about names and not about the scrub."""
    from db_ops.lib import pseudonym

    return pseudonym.Mapping({})
