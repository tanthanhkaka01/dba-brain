"""The console and the published reports are two mounts on one listener, with nothing joining them.

A user signed into `/db_ops/` could not reach the fleet inventory, the SLA page or a server's
metrics: they are served by the same process, one path segment away, and the console never named
them. The only way to open one was to be told the URL by somebody who already knew it.

The links are **root-relative** and the mount is taken from the server that is actually serving the
reports, never from a config key of its own. Both of those are the lesson from `report_base_url`,
which was a second copy of one deployment fact and spent two days pointing at a retired worker
after the estate moved (2026-09-10).
"""

from __future__ import annotations

from db_ops.webhost import pages
from db_ops.webhost.app import WebApp, WebSettings


def _console(**kwargs) -> WebApp:
    return WebApp(auth_store=None, config_store=None, settings=WebSettings(), **kwargs)


def test_a_console_that_has_not_been_told_where_the_reports_are_shows_no_report_links():
    # Not configured is a state, not a failure: guessing a mount would link at a path nothing
    # answers on, which is worse than leaving the group out.
    console = _console()
    assert console.reports_prefix == ""
    assert console.report_links == []
    assert "Reports" not in pages._sidebar("/db_ops", [], "", console.report_links)


def test_the_server_tells_the_console_which_mount_it_is_really_serving():
    console = _console()
    console.reports_mount = "report_dba"
    assert console.reports_prefix == "/report_dba"


def test_a_non_default_mount_is_carried_through_rather_than_assumed():
    console = _console()
    console.reports_mount = "dba_pages"
    assert "/dba_pages/database-inventory.html" in pages._sidebar(
        "/db_ops", [], "", console.report_links)


def _all_links(prefix="/report_dba"):
    return [(label, f"{prefix}/{page}") for label, page in pages.REPORT_PAGES]


def test_the_sidebar_names_every_stable_page():
    markup = pages._sidebar("/db_ops", [], "", _all_links())
    assert "Reports" in markup
    for label, page in pages.REPORT_PAGES:
        assert label in markup
        assert f'href="/report_dba/{page}"' in markup


def test_the_links_are_root_relative_so_they_follow_whatever_host_served_the_console():
    # No scheme and no host anywhere in them - that is what stops this going stale on a move.
    markup = pages._sidebar("/db_ops", [], "", _all_links())
    assert "http://" not in markup and "https://" not in markup


def test_the_per_server_pages_are_not_listed_because_a_real_server_id_must_not_ship():
    pages_named = [page for _, page in pages.REPORT_PAGES]
    assert not any("index-usage_" in page for page in pages_named)


def test_the_dashboard_carries_the_report_links_too():
    markup = pages.overview_page(
        prefix="/db_ops", report_links=_all_links(),
        session={"username": "someone", "user_level": 50, "expires_at": "2026-12-01"},
        blocks=[], can_edit=False, can_run=False, generated_at="2026-09-10 10:00 +07")
    assert 'href="/report_dba/sla.html"' in markup


def test_the_console_offers_only_the_report_pages_that_exist(tmp_path):
    """Measured on a fresh node, 2026-09-10: the sidebar linked all three stable pages and the
    node had only ever run the index report, so all three were **404s with labels on them**.

    The banner on a report page already filtered by existence; the console did not. Same rule,
    two places, and only one of them obeyed it.
    """
    console = _console()
    console.reports_mount = "report_dba"
    console.reports_root = tmp_path
    assert console.report_links == []

    (tmp_path / "sla.html").write_text("x", encoding="utf-8")
    assert console.report_links == [("SLA", "/report_dba/sla.html")]

    (tmp_path / "database-inventory.html").write_text("x", encoding="utf-8")
    hrefs = [href for _, href in console.report_links]
    assert "/report_dba/database-inventory.html" in hrefs
    assert "/report_dba/server-metrics.html" not in hrefs


def test_a_console_told_a_mount_but_no_directory_still_offers_the_pages(tmp_path):
    # Not every caller has a directory (a test builds a WebApp directly). Unknown is not the same
    # as empty: it means "cannot check", and hiding everything would be its own wrong answer.
    console = _console()
    console.reports_mount = "report_dba"
    assert len(console.report_links) == len(pages.REPORT_PAGES)


def test_a_page_published_after_the_console_started_appears(tmp_path):
    # Checked per request, not cached at startup: reports are generated on a schedule and the
    # console outlives them.
    console = _console()
    console.reports_mount = "report_dba"
    console.reports_root = tmp_path
    assert console.report_links == []
    (tmp_path / "server-metrics.html").write_text("x", encoding="utf-8")
    assert console.report_links == [("Server metrics", "/report_dba/server-metrics.html")]
