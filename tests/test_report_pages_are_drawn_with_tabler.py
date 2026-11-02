"""Every published report page is drawn with Tabler, vendored and inlined, and nothing by hand.

The operator, 2026-10-30: *restyle the report pages in petweb's style, using open-source UI
libraries - do not design anything by hand*. petweb is Tabler, so the pages are too
(``lib.page_kit``). What these tests hold:

- the library ships with the package, its MIT license beside it, and every page carries its
  copyright notice - the license asks for it on every copy;
- a page never fetches anything: the worker that serves them has no internet, and a page mailed on
  or opened from disk must look the same;
- a page carries only the rules it can use, not the 635 KB sheet - a report root keeps a dated copy
  of every page;
- the pages' own names *are* Tabler classes through ``ALIASES``, so their look is the library's
  rules word for word, never a restatement of them;
- a page word Tabler uses for something else (a stat card flagged ``alert``) never picks up
  Tabler's meaning.
"""

from __future__ import annotations

import re

from dbabrain.lib import page_banner, page_kit, page_style
from dbabrain.reports import index_report as ir
from dbabrain.sla.models import SlaValidationSummary
from dbabrain.sla.publish import render_html


def _rules(css: str, selector: str) -> list[str]:
    """The declaration blocks of every rule whose selector list names ``selector``."""
    return [body for selectors, body in re.findall(r"(?:^|(?<=}))([^{}@]+)\{([^{}]*)\}", css)
            if selector in (one.strip() for one in selectors.split(","))]


def _sla_page() -> str:
    summary = SlaValidationSummary(status="PASSED", policy_count=0, result_count=0, passed_count=0,
                                   at_risk_count=0, failed_count=0, no_data_count=0,
                                   window_end="2026-10-02T00:00:00Z", results=())
    return render_html(summary, recent_runs=[])


def _index_page(tmp_path) -> str:
    entry = {"server_id": "ACME-1", "ip": "192.0.2.1", "collected_at": "", "totals": {"disabled": 2},
             "databases": [], "disabled": [], "droppable": [], "fragmented": []}
    return ir.write_index_report_html(entry, ir.format_index_report(entry), tmp_path).read_text(encoding="utf-8")


def test_the_library_ships_with_its_license():
    assert page_kit.SHEET_PATH.is_file() and page_kit.LICENSE_PATH.is_file()
    assert "MIT License" in page_kit.LICENSE_PATH.read_text(encoding="utf-8")
    assert f"Tabler v{page_kit.VERSION}" in page_kit.SHEET_PATH.read_text(encoding="utf-8")[:400]


def test_a_page_carries_the_kit_once_with_the_librarys_copyright_notice(tmp_path):
    for page in (_sla_page(), _index_page(tmp_path)):
        assert page.count(page_kit.MARKER) == 1
        assert "Copyright 2018-2026 The Tabler Authors" in page
        assert page.index(page_kit.MARKER) < page.index(page_style.TOKENS.strip()), \
            "the page's own rules come after the library's, so they apply over it"


def test_inlining_twice_does_not_add_a_second_copy(tmp_path):
    page = _index_page(tmp_path)
    assert page_kit.inline(page) == page


def test_a_page_fetches_nothing(tmp_path):
    for page in (_sla_page(), _index_page(tmp_path)):
        kit = page_kit.stylesheet(page)
        assert all(url.startswith(("data:", '"data:', "'data:")) for url in re.findall(r"url\(([^)]*)\)", kit))
        assert "@import" not in kit
        assert "<link" not in page and "<script src" not in page


def test_a_page_carries_only_the_rules_it_can_use(tmp_path):
    page = _index_page(tmp_path)
    kit = page_kit.stylesheet(page)
    assert len(kit) < len(page_kit.SHEET_PATH.read_text(encoding="utf-8")) // 4
    assert ".carousel" not in kit and ".offcanvas" not in kit, "components the page never names"
    assert ":root{" in kit, "the tokens every rule reads"


def test_an_aliased_name_carries_tablers_rule_word_for_word(tmp_path):
    kit = page_kit.stylesheet(_index_page(tmp_path) + '<span class="badge b-crit"></span>')
    tabler = page_kit.SHEET_PATH.read_text(encoding="utf-8")
    assert _rules(kit, ".b-crit"), "a crit badge is Tabler's light red badge"
    assert set(_rules(kit, ".b-crit")) <= set(_rules(tabler, ".bg-red-lt"))


def test_a_page_word_tabler_uses_otherwise_keeps_its_own_meaning(tmp_path):
    """``kpi alert`` is a red number on a stat card. Tabler's ``.alert`` is a padded, tinted box;
    the stat card must not become one."""
    page = _index_page(tmp_path)
    assert '<div class="kpi alert">' in page
    kit = page_kit.stylesheet(page)
    assert not _rules(kit, ".alert"), "Tabler's alert box reaches the page only through an alias"
    assert ".kpi.alert .num{" in kit, "the stat's red number is Tabler's text-red"


def test_the_banner_is_tablers_navbar_light_or_dark_with_the_page():
    """dbabrain.dev's head (2026-10-30): a navbar on the page's own surface, not a dark bar on a
    light page - so the kit carries Tabler's dark theme, which the banner's switch turns on."""
    page = "<style></style>" + page_banner.render(title="SLA", here="sla.html")
    kit = page_kit.stylesheet(page)
    assert ".dbo-banner{" in kit
    assert ".dbo-banner .dbo-links a.dbo-here" in kit and ".dbo-banner .dbo-theme" in kit
    assert "data-bs-theme=dark" in kit, "the dark theme's tokens travel with the switch"
    assert ".navbar-dark" not in kit


def test_the_showcase_does_not_take_the_kits_class_names_for_the_products_words(tmp_path):
    """The showcase leaves a word alone when the pages' own stylesheet selects it - that word is the
    product's. Tabler's hundreds of class names are not: with them, a customer table called
    ``Alert`` or ``Container`` would be published under its real name."""
    from dbabrain.common import showcase

    words = showcase._stylesheet_words(_index_page(tmp_path))
    assert "masthead" in words, "the page's own sheet still counts"
    assert "container" not in words and "carousel" not in words
