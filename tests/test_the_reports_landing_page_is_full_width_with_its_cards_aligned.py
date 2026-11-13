"""The reports landing page is as wide as every other report page, its cards in one column (1.279).

Why it matters: found on 0.36.0's release day in headless Edge at 1366 and 1920 px - ``index.html``
was a centred 760 px column while every other page is fluid and full-width (the common template),
and its second card (*Database inventory report*) sat about 20 px right of the first. The cards are
Tabler card-links (``lib.page_kit`` aliases ``a.hub-card``), and Bootstrap's
``.card-link + .card-link { margin-left }`` pushed every card after the first.
"""

from __future__ import annotations

import re

from dbabrain.sla.publish import _INDEX_TEMPLATE, render_index_html


def _css() -> str:
    return re.search(r"<style>(.*?)</style>", _INDEX_TEMPLATE, re.S).group(1)


def test_the_landing_page_is_not_a_centred_column():
    wrap = re.search(r"\.wrap \{\{([^}]*)\}\}", _css()).group(1)
    assert "max-width: none" in wrap and "auto" not in wrap


def test_a_card_after_the_first_is_not_pushed_right():
    # As specific as the aliased Tabler rule "a.hub-card+a.hub-card" or more - a plain ".hub-card"
    # reset loses to it, which is what the first fix did.
    assert re.search(r"\.wrap a\.hub-card \+ a\.hub-card \{\{[^}]*margin-inline-start: 0", _css())


def test_both_cards_render_as_links_with_the_old_column_width_gone(tmp_path):
    (tmp_path / "database-inventory.html").write_text("x", encoding="utf-8")
    page = render_index_html(None, directory=tmp_path)
    assert page.count('class="hub-card"') == 2 and "max-width: 760px" not in page
