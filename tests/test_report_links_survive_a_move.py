"""A published page must not keep linking at the node that rendered it.

The report text carries absolute URLs because half its audience reads it in Telegram, where a
relative link means nothing. The other half reads the same text as a page served from the very
host those URLs name - and there the absolute form is a liability: nothing re-renders a page that
is already on disk, so when the estate moved on 2026-09-08 every index-usage page went on pointing
"Server dashboard" and "Fleet inventory" at the retired worker. Two days later someone clicked one.

A relative href cannot rot that way: it resolves against whatever host served the page. So the
text keeps the absolute URL and the HTML gets the relative one, and the two are built from the
same configured base so they can never name different pages.
"""

from __future__ import annotations

from db_ops.lib.report_links import href_for_page, page_relative


def test_one_of_our_own_pages_becomes_relative():
    assert page_relative("http://192.0.2.249:8080/report_dba/database-inventory.html",
                         "http://192.0.2.249:8080/report_dba/") == "database-inventory.html"


def test_a_missing_trailing_slash_on_the_base_is_not_a_different_base():
    assert page_relative("http://192.0.2.249:8080/report_dba/sla.html",
                         "http://192.0.2.249:8080/report_dba") == "sla.html"


def test_a_link_to_another_host_is_left_alone_because_it_really_does_point_elsewhere():
    # Rewriting this one relative would silently retarget a genuine external link at our own host.
    assert page_relative("https://learn.microsoft.com/sql",
                         "http://192.0.2.249:8080/report_dba/") is None
    assert href_for_page("https://learn.microsoft.com/sql",
                         "http://192.0.2.249:8080/report_dba/") == "https://learn.microsoft.com/sql"


def test_an_unconfigured_base_leaves_every_url_absolute():
    # DEFAULT_REPORT_BASE_URL is empty on purpose; with no base there is nothing to be relative to.
    assert page_relative("http://192.0.2.249:8080/report_dba/sla.html", "") is None


def test_a_link_to_the_base_itself_becomes_the_current_directory():
    assert page_relative("http://192.0.2.249:8080/report_dba/",
                         "http://192.0.2.249:8080/report_dba/") == "./"


def test_the_rendered_page_links_relative_while_the_text_keeps_the_absolute_url(monkeypatch):
    from db_ops.reports import index_report

    monkeypatch.setattr(index_report, "report_base_url",
                        lambda: "http://192.0.2.249:8080/report_dba/")
    html = index_report._markdown_tables_to_html(
        "Index Inventory Report\n"
        "Fleet inventory: http://192.0.2.249:8080/report_dba/database-inventory.html\n")

    assert 'href="database-inventory.html"' in html
    # The reader still sees where the page lives; only the href is relative.
    assert "http://192.0.2.249:8080/report_dba/database-inventory.html" in html
    assert 'href="http://192.0.2.249:8080/report_dba/database-inventory.html"' not in html


def test_a_url_on_its_own_line_is_rewritten_the_same_way(monkeypatch):
    from db_ops.reports import index_report

    monkeypatch.setattr(index_report, "report_base_url",
                        lambda: "http://192.0.2.249:8080/report_dba/")
    html = index_report._markdown_tables_to_html("http://192.0.2.249:8080/report_dba/sla.html\n")
    assert 'href="sla.html"' in html


def test_a_bare_page_name_becomes_a_link_when_there_is_no_base_url(monkeypatch):
    """`report_base_url` is derived now, so "unset" is the ordinary case rather than a corner.

    With it unset the report text already reads `Fleet inventory: database-inventory.html` —
    relative and correct — and the renderer only linkified `http(s)://`, so the line came out as
    plain prose. Measured on a node built from `init` on 2026-09-10: the published index-usage page
    carried **no anchors at all**, while the docs claimed it "falls back to relative hrefs".
    """
    from db_ops.reports import index_report

    monkeypatch.setattr(index_report, "report_base_url", lambda: "")
    html = index_report._markdown_tables_to_html(
        "Fleet inventory: database-inventory.html\n")
    assert 'href="database-inventory.html"' in html


def test_a_page_name_carrying_a_query_string_keeps_it(monkeypatch):
    from db_ops.reports import index_report

    monkeypatch.setattr(index_report, "report_base_url", lambda: "")
    html = index_report._markdown_tables_to_html(
        "Server dashboard: server-metrics.html?server=acme-1\n")
    assert 'href="server-metrics.html?server=acme-1"' in html


def test_an_absolute_url_on_the_line_still_wins(monkeypatch):
    # Both rules must not fire on one line: the absolute form is rewritten to a relative href and
    # linkifying the *result* again would nest an anchor inside an anchor.
    from db_ops.reports import index_report

    monkeypatch.setattr(index_report, "report_base_url",
                        lambda: "http://192.0.2.249:8080/report_dba/")
    html = index_report._markdown_tables_to_html(
        "Fleet inventory: http://192.0.2.249:8080/report_dba/database-inventory.html\n")
    assert html.count("<a href=") == 1


def test_a_word_that_merely_ends_in_html_is_not_turned_into_a_link():
    from db_ops.lib.report_links import linkify_bare_pages

    # No false positives on prose: only a token that reads as a sibling page.
    assert linkify_bare_pages("see the .html format", lambda p: "LINK") == "see the .html format"
    assert linkify_bare_pages("a/deep/path.html", lambda p: "LINK") == "a/deep/path.html"
    assert linkify_bare_pages("sla.html", lambda p: "LINK") == "LINK"
