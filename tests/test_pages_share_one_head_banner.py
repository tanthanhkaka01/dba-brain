"""Every page db_ops publishes leads with the same head, and says when it was measured.

Asked for on 2026-09-10 by someone reading `index-usage_<server>.html`, which opened straight on a
server picker: no title, no machine name, and **no stamp**. "How old is this page" is the first
question anyone asks of a generated report, and three of the four page families answered it in three
different wordings while the fourth did not answer it at all.

Two properties are load-bearing rather than cosmetic, and both were learned the same week:

* the stamp is **passed in**, already rendered through `db_ops.lib.timezone`, so a page rebuilt for
  a past day says that day and every reader sees the same hour rather than their own browser's;
* every link is **relative** and only to a page that exists — an absolute one goes stale the moment
  the estate moves, and a link to a report that was never generated is a 404 with a label on it.
"""

from __future__ import annotations

from db_ops.lib import page_banner


def test_the_banner_names_the_product_the_page_and_the_machine():
    markup = page_banner.render(title="Index Usage", scope="ACME-192-0-2-115")
    assert "DBA Brain" in markup
    assert "Index Usage" in markup
    assert "ACME-192-0-2-115" in markup


def test_the_stamp_is_rendered_the_way_the_operator_asked_for_it():
    markup = page_banner.render(title="Index Usage", snapshot_at="2026-09-10 10:57:00 +07")
    assert "snapshot 2026-09-10 10:57:00 +07" in markup


def test_a_page_with_no_honest_stamp_prints_none_rather_than_claiming_one():
    # "unknown" or an empty "snapshot" label would both imply the page knows something it does not.
    assert "snapshot" not in page_banner.render(title="Index Usage")


def test_the_banner_never_reads_a_clock_of_its_own():
    # Two calls a day apart must produce identical markup for identical arguments: the page is
    # rebuilt for past days by the backfill, and a self-read clock would stamp it with today.
    first = page_banner.render(title="X", snapshot_at="2026-01-01 00:00:00 +07")
    second = page_banner.render(title="X", snapshot_at="2026-01-01 00:00:00 +07")
    assert first == second


def test_every_sibling_link_is_relative_so_it_follows_whatever_host_served_the_page():
    markup = page_banner.render(title="Index Usage")
    assert "http://" not in markup and "https://" not in markup


def test_the_page_does_not_offer_a_link_to_itself():
    markup = page_banner.render(title="SLA / SLO compliance", here="sla.html")
    assert 'href="sla.html"' not in markup
    assert 'aria-current="page"' in markup


def test_only_the_pages_that_exist_are_offered():
    present = page_banner.siblings_present(lambda name: name == "sla.html")
    assert present == (("SLA", "sla.html"),)
    markup = page_banner.render(title="Reports", links=present)
    assert 'href="database-inventory.html"' not in markup
    assert 'href="sla.html"' in markup


def test_a_report_root_holding_nothing_yet_gets_a_banner_with_no_links():
    markup = page_banner.render(title="Reports", links=page_banner.siblings_present(lambda _: False))
    assert "DBA Brain" in markup
    assert "<a" not in markup


def test_a_title_carrying_markup_cannot_break_out_of_the_banner():
    markup = page_banner.render(title='<script>x</script>', scope='" onmouseover="x')
    assert "<script>" not in markup
    assert 'onmouseover="x' not in markup


def test_the_index_usage_page_carries_the_banner_with_its_own_snapshot(tmp_path):
    from db_ops.reports.index_report import write_index_report_html

    entry = {"server_id": "ACME-192-0-2-115", "collected_at": "2026-09-10T03:57:00Z"}
    path = write_index_report_html(entry, "Index Inventory Report\n", tmp_path)
    page = path.read_text(encoding="utf-8")

    assert "dbo-banner" in page
    assert "DBA Brain" in page
    assert "ACME-192-0-2-115" in page
    assert "snapshot 2026-09-10" in page
    # The banner's CSS travels with it, or the page renders the markup unstyled.
    assert ".dbo-banner{" in page


def test_the_index_usage_page_offers_only_reports_that_exist_beside_it(tmp_path):
    from db_ops.reports.index_report import write_index_report_html

    entry = {"server_id": "ACME-192-0-2-115", "collected_at": "2026-09-10T03:57:00Z"}
    page = write_index_report_html(entry, "x\n", tmp_path).read_text(encoding="utf-8")
    assert 'href="sla.html"' not in page

    (tmp_path / "sla.html").write_text("x", encoding="utf-8")
    page = write_index_report_html(entry, "x\n", tmp_path).read_text(encoding="utf-8")
    assert 'href="sla.html"' in page


def test_the_sla_page_carries_the_same_banner(tmp_path):
    from tests.test_sla_publish import _result, _summary

    from db_ops.sla.publish import render_html

    summary = _summary([_result("SS_AVAIL", "PASSED", 99.9, 99.0, 0.9)],
                       status="PASSED", passed=1, at_risk=0, failed=0, no_data=0)
    page = render_html(summary, recent_runs=[], report_dir=tmp_path)
    assert "dbo-banner" in page
    assert "DBA Brain" in page
    assert ".dbo-banner{" in page


def test_no_page_leads_with_one_estate_s_company_name():
    """The inventory template carried `ORG1 / TRX` as literal markup, and the server template drew
    its eyebrow from the estate's own `company` field. Both said an operator's organisation on a
    page whose product is DBA Brain — one of them compiled into a file that ships. The banner is
    the only branding line now, and it is the product's.
    """
    from pathlib import Path

    templates = Path("db_ops/reports/templates")
    for name in ("inventory_report.html", "server_report.html"):
        markup = (templates / name).read_text(encoding="utf-8")
        assert "ORG1" not in markup, name
        assert "TRX" not in markup, name
        assert "__COMPANY__" not in markup, name
        assert "__PAGE_BANNER__" in markup, name


def test_the_head_offers_the_index_report_when_the_root_holds_one():
    """An estate published 14 index reports a night and no page linked to any of them.

    `SIBLING_PAGES` cannot carry this one: there is a page per server and its file name is a
    `server_id`, which is exactly what a shipped constant must never hold. So the href is the
    caller's and only the label lives in this module - and the choice of *which* index page is one
    rule in one place, because every index page carries a picker over all the others.
    """
    root = {"database-inventory.html", "server-metrics.html", "sla.html",
            "index-usage_acme-192-0-2-9.html", "index-usage_acme-192-0-2-1.html"}

    entry = page_banner.pick_index_usage(root)
    links = page_banner.siblings_present(lambda name: name in root, index_usage=entry)

    # Sorted, so the same root offers the same page every run instead of shuffling the link.
    assert entry == "index-usage_acme-192-0-2-1.html"
    assert links[-1] == ("Index usage", entry)
    assert [label for label, _ in links[:-1]] == ["Fleet inventory", "Server metrics", "SLA"]


def test_no_index_report_means_no_index_link_rather_than_a_404():
    root = {"database-inventory.html"}

    assert page_banner.pick_index_usage(root) == ""
    assert page_banner.siblings_present(lambda name: name in root, index_usage="") == (
        ("Fleet inventory", "database-inventory.html"),)
    # ...and a name that is not in the root is refused even when the caller offers it.
    assert page_banner.siblings_present(
        lambda name: name in root, index_usage="index-usage_gone.html") == (
        ("Fleet inventory", "database-inventory.html"),)
