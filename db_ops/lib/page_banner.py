"""One head banner, identical on every page db_ops publishes.

Pure: the caller passes the title, the scope and the moment, and gets markup back. Reading a clock
or a config file is an operation and belongs to the caller.

Asked for on 2026-09-10. The four page families this tool publishes had grown four different heads:
`server-metrics.html` and `database-inventory.html` each carried a subtitle with a snapshot stamp,
in two different wordings; `sla.html` had a third; and `index-usage_<server>.html` had **no head at
all** — it opened straight on a server picker, so a reader could not tell what they were looking at,
which machine it was about, or how old it was. "How old is this page" is the first question anyone
asks of a generated report and the one it was cheapest to answer wrong.

Two rules the banner keeps, both learned the same week:

* **The stamp is rendered by the caller through `db_ops.lib.timezone`**, so every page shows the
  one clock db_ops shows, not the reader's browser. A report is a file that gets shared; it must
  say the same hour to everyone who opens it.
* **Every link here is relative.** These pages sit together under one mount, so a relative href
  resolves against whatever host served the page. An absolute one goes stale the moment the estate
  moves - which is exactly what `report_base_url` did for two days after the 2026-09-08 move.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: The product name every page leads with, so a page that has been mailed on, pasted into a ticket
#: or printed still says what produced it.
PRODUCT = "DBA Brain"

#: The sibling pages every page offers, as (label, relative href). Stable file names only - a
#: per-server page carries a slug, and naming one estate's server in shipped code is what
#: `check-identifiers` refuses.
SIBLING_PAGES: tuple[tuple[str, str], ...] = (
    ("Fleet inventory", "database-inventory.html"),
    ("Server metrics", "server-metrics.html"),
    ("SLA", "sla.html"),
)

CSS = """
.dbo-banner{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 14px;
  margin:0 0 18px;padding:14px 16px;border-radius:6px;
  background:#0f2540;color:#e8eef7;font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.dbo-banner .dbo-product{font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:#7fa6d9;font-weight:700}
.dbo-banner .dbo-title{font-size:17px;font-weight:700;color:#fff}
.dbo-banner .dbo-scope{font-size:13px;color:#b9cbe4;font-family:ui-monospace,SFMono-Regular,
  "Cascadia Mono",Consolas,monospace}
.dbo-banner .dbo-stamp{font-size:12px;color:#8fb0d8;margin-left:auto;white-space:nowrap}
.dbo-banner .dbo-links{flex-basis:100%;display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.dbo-banner .dbo-links a{font-size:12px;color:#cfe0f5;text-decoration:none;
  border:1px solid #2c4a70;border-radius:999px;padding:3px 11px;cursor:pointer}
.dbo-banner .dbo-links a:hover{border-color:#7fa6d9;color:#fff}
.dbo-banner .dbo-links a.dbo-here{background:#e8eef7;color:#0f2540;font-weight:600;
  border-color:#e8eef7}
@media print{.dbo-banner{background:#fff;color:#000;border:1px solid #000}
  .dbo-banner .dbo-title,.dbo-banner .dbo-product,.dbo-banner .dbo-scope,
  .dbo-banner .dbo-stamp{color:#000}.dbo-banner .dbo-links{display:none}}
"""


#: The label the per-server index report is offered under. It has no entry in
#: :data:`SIBLING_PAGES` for the reason stated there - its file name carries a `server_id`, and a
#: real one in shipped code is what `check-identifiers` refuses - so the **href is the caller's**
#: and only the word lives here.
INDEX_USAGE_LABEL = "Index usage"

#: The shape of a per-server index report's file name, as
#: :func:`db_ops.lib.webhost_endpoints.PER_SERVER_PAGES` states it, with the slug left open.
_INDEX_USAGE_PREFIX = "index-usage_"


def pick_index_usage(names) -> str:
    """Which index report to offer as the way in, given the file names a report root holds.

    There is one per server and no fleet-wide one, so a banner has to choose. The choice is the
    **first in sorted order**, and it is a real choice rather than an arbitrary one: every index
    page carries a picker over all the others, so any of them is an entry point to all of them,
    and sorting makes the same root offer the same page every run instead of shuffling the link
    under the reader.

    Empty when the root holds none - a link that 404s is worse than no link.
    """
    candidates = sorted(str(name) for name in names
                        if str(name).startswith(_INDEX_USAGE_PREFIX)
                        and str(name).endswith((".html", ".htm")))
    return candidates[0] if candidates else ""


def siblings_present(exists, *, index_usage: str = "") -> tuple[tuple[str, str], ...]:
    """The sibling pages that are actually published, given a predicate over the file name.

    A predicate rather than a directory, because touching the filesystem is an operation and this
    module is pure. The filtering is not cosmetic: a report root that has never run the SLA app has
    no ``sla.html``, and offering it anyway produces the one thing every page rule here forbids —
    a link that 404s, which is worse than no link.

    ``index_usage`` adds the per-server index report, which was missing from every page's head
    until 2026-09-12: the estate had been publishing 14 of them nightly and the only way to reach
    one was a per-row link on a fleet page that happened to list the server. A report nobody can
    navigate to has not been published, it has been written.
    """
    pages = tuple((label, href) for label, href in SIBLING_PAGES if exists(href))
    if index_usage and exists(index_usage):
        pages += ((INDEX_USAGE_LABEL, index_usage),)
    return pages


def _escape(value: object) -> str:
    return (str(value if value is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def render(*, title: str, snapshot_at: str = "", scope: str = "",
           here: str = "", links: Iterable[tuple[str, str]] | None = None) -> str:
    """The banner, as one block of markup.

    ``snapshot_at`` is already-rendered display text (``2026-09-10 10:57:00 +07``) — this module
    never reads a clock, because a pure function of its arguments is what lets the page be
    rebuilt for a past day and still say that day. Empty prints nothing rather than "unknown":
    a page with no honest stamp should not imply it has one.

    ``here`` is the file name of the page being rendered, so its own entry among the siblings is
    marked and not offered as a link to itself.
    """
    parts = [f'<span class="dbo-product">{_escape(PRODUCT)}</span>',
             f'<span class="dbo-title">{_escape(title)}</span>']
    if scope:
        parts.append(f'<span class="dbo-scope">{_escape(scope)}</span>')
    if snapshot_at:
        parts.append(f'<span class="dbo-stamp">snapshot {_escape(snapshot_at)}</span>')

    entries = SIBLING_PAGES if links is None else tuple(links)
    if entries:
        chips = []
        for label, href in entries:
            if here and href == here:
                chips.append(f'<a class="dbo-here" aria-current="page">{_escape(label)}</a>')
            else:
                chips.append(f'<a href="{_escape(href)}">{_escape(label)}</a>')
        parts.append('<span class="dbo-links">' + "".join(chips) + "</span>")
    return '<div class="dbo-banner">' + "".join(parts) + "</div>"


#: What :func:`render` writes for the moment, read back. A pattern rather than an HTML parser
#: because this module writes the markup it is matching: the span is one line, one class and no
#: attributes, and a parser dependency to read back six words we ourselves emitted would be the
#: heavier of the two lies.
_STAMP_PATTERN = re.compile(r'<span class="dbo-stamp">snapshot ([^<]+)</span>')


def snapshot_stamp(markup: str) -> str:
    """The rendered moment a published page carries, or ``""`` if it carries none.

    The inverse of :func:`render`'s ``snapshot_at``, and it lives here so the writer and the reader
    of that span are edited in one file.

    Why a *page* is asked rather than a clock: a snapshot taken from a node's archive is not from
    now — ``20260825_sla.html`` is three weeks old — and naming the file after the moment it was
    *downloaded* would put every day of a captured window under one date. The page already states
    the only honest answer, in the display zone with its offset, which
    :func:`db_ops.lib.timezone.parse_display` turns back into an instant.
    """
    match = _STAMP_PATTERN.search(str(markup or ""))
    return match.group(1).strip() if match else ""
