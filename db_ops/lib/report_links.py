"""Turning an absolute report URL into one that survives the node moving.

Pure. A report's text carries **absolute** URLs because half its audience reads it in Telegram,
where a relative link means nothing. The other half reads the same text rendered as a page served
from the very host those URLs name - and there a relative link is not merely shorter, it is the
only one that stays right.

Measured on 2026-09-10: after the estate moved to a new node, every published index-usage page
still linked "Server dashboard" and "Fleet inventory" at the retired worker, because the base URL
they were built from is one configured string and nothing re-renders an already-published page.
Relative hrefs cannot rot that way: they resolve against whatever host served the page.
"""

from __future__ import annotations


def page_relative(url: str, base_url: str) -> str | None:
    """The part of ``url`` below ``base_url``, or ``None`` when it points somewhere else.

    ``None`` rather than a guess: a link to a different host is a real link to a different host,
    and rewriting it relative would silently retarget it at this one.
    """
    url_text = str(url or "").strip()
    base_text = str(base_url or "").strip()
    if not url_text or not base_text:
        return None
    base_text = base_text.rstrip("/") + "/"
    if not url_text.startswith(base_text):
        return None
    remainder = url_text[len(base_text):]
    # A link to the base itself has no page to name; "./" is the honest relative form of it.
    return remainder or "./"


def href_for_page(url: str, base_url: str) -> str:
    """What to put in an ``href``: relative when it is one of our own pages, else unchanged."""
    relative = page_relative(url, base_url)
    return relative if relative is not None else str(url or "")


#: A published page named on its own, with no scheme: `database-inventory.html`,
#: `server-metrics.html?server=x`, `index-usage_<slug>.html`. Anchored so it cannot bite into the
#: middle of a word, and it deliberately does not match a path with a slash in it - these pages are
#: siblings in one directory, and anything deeper is not one of ours to linkify.
BARE_PAGE_PATTERN = r"(?<![\w./:-])([A-Za-z0-9_.-]+\.html(?:\?[^\s<\"']*)?)"


def linkify_bare_pages(text: str, make_anchor) -> str:
    """Turn a bare page name into a link, given an anchor builder.

    Needed because a base URL is no longer required. With `report_base_url` unset the report text
    says `Fleet inventory: database-inventory.html` - already relative, already correct - and the
    HTML renderer only ever linkified `http(s)://`, so those lines came out as **plain text**.
    Measured on a node built from `init` on 2026-09-10: the published index-usage page had *no
    anchors at all*, while `docs/06_reports_app.md` claimed the pages "fall back to relative
    hrefs". They fell back to relative prose.

    ``make_anchor`` takes the matched page name and returns the markup, so the escaping rules stay
    with the renderer that owns them rather than being guessed at here.
    """
    import re

    return re.sub(BARE_PAGE_PATTERN, lambda match: make_anchor(match.group(1)), str(text or ""))
