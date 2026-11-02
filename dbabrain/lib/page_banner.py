"""One head banner, identical on every page dbabrain publishes.

Pure: the caller passes the title, the scope and the moment, and gets markup back. Reading a clock
or a config file is an operation and belongs to the caller.

Asked for on 2026-09-10. The four page families this tool publishes had grown four different heads:
`server-metrics.html` and `database-inventory.html` each carried a subtitle with a snapshot stamp,
in two different wordings; `sla.html` had a third; and `index-usage_<server>.html` had **no head at
all** — it opened straight on a server picker, so a reader could not tell what they were looking at,
which machine it was about, or how old it was. "How old is this page" is the first question anyone
asks of a generated report and the one it was cheapest to answer wrong.

Two rules the banner keeps, both learned the same week:

* **The stamp is rendered by the caller through `dbabrain.lib.timezone`**, so every page shows the
  one clock dbabrain shows, not the reader's browser. A report is a file that gets shared; it must
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
    # The fleet's backup files and restores (0.34.0) - offered, like every sibling, only once it is
    # published: a report root whose catalog has never been opened has no such page.
    ("Backup & restore", "backup-restore.html"),
)

#: The mark beside the product name - dbabrain.dev's own (its favicon), so a report and the
#: product's site read as one thing. Inline: a page opened from disk or mailed on still shows it.
MARK_SVG = (
    # No xmlns: an SVG inline in HTML needs none, and the banner carries no absolute URL at all.
    '<svg viewBox="0 0 64 64" aria-hidden="true"><defs>'
    '<linearGradient id="dbo-mark-g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2453d6"/>'
    '<stop offset="1" stop-color="#0d9488"/></linearGradient></defs>'
    '<rect width="64" height="64" rx="14" fill="url(#dbo-mark-g)"/>'
    '<g fill="none" stroke="#fff" stroke-width="4"><ellipse cx="32" cy="18" rx="14" ry="5"/>'
    '<path d="M18 18v28c0 2.8 6.3 5 14 5s14-2.2 14-5V18M18 32c0 2.8 6.3 5 14 5s14-2.2 14-5"/></g></svg>')

#: The light / dark switch's two faces: Tabler Icons' ``moon`` and ``sun`` (MIT), the icon set
#: Tabler's own pages use - not drawn here.
_ICON = ('<svg width="20" height="20" viewBox="0 0 24 24" fill="none" '
         'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
         'aria-hidden="true" class="dbo-icon-{name}">{paths}</svg>')
MOON_SVG = _ICON.format(name="moon", paths='<path d="M12 3c.132 0 .263 0 .393 0a7.5 7.5 0 0 0 7.92 '
                        '12.446a9 9 0 1 1 -8.313 -12.454z"/>')
SUN_SVG = _ICON.format(name="sun", paths='<path d="M12 12m-4 0a4 4 0 1 0 8 0a4 4 0 1 0 -8 0"/>'
                       '<path d="M3 12h1m8 -9v1m8 8h1m-9 8v1m-6.4 -15.4l.7 .7m12.1 -.7l-.7 .7m0 11.4l.7 .7'
                       'm-12.1 -.7l-.7 .7"/>')

#: Light or dark, as on dbabrain.dev (2026-10-30): the reader's own choice if they made one, kept in
#: this browser only, else the system's. It sets Tabler's ``data-bs-theme`` on the page, and every
#: colour on these pages is one of Tabler's variables, so the whole page follows. Run where the
#: banner is, at the top of the body - before the tables are drawn. Nothing is sent anywhere.
THEME_SCRIPT = """<script>(function(){var r=document.documentElement,k="dbabrain-theme",t=null;
try{t=localStorage.getItem(k)}catch(e){}
if(t!=="light"&&t!=="dark")t=window.matchMedia&&matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";
r.setAttribute("data-bs-theme",t);
var b=document.querySelector(".dbo-banner .dbo-theme");if(!b)return;
function show(){var d=r.getAttribute("data-bs-theme")==="dark";
b.setAttribute("aria-label",d?"Switch to light mode":"Switch to dark mode");b.title=b.getAttribute("aria-label")}
b.addEventListener("click",function(){var n=r.getAttribute("data-bs-theme")==="dark"?"light":"dark";
r.setAttribute("data-bs-theme",n);try{localStorage.setItem(k,n)}catch(e){}show()});show()})();</script>"""

#: The banner's own rules: only its layout. Its look is Tabler's navbar - light or dark with the
#: page - its links the navbar's ghost buttons, the page on show the primary one
#: (``lib.page_kit.ALIASES``) - every page that carries a banner carries the kit.
CSS = """
.dbo-banner{flex-wrap:wrap;align-items:center;justify-content:flex-start;gap:6px 14px;
  margin:0 0 18px;padding:10px 16px;border-radius:var(--tblr-border-radius-lg);
  border:var(--tblr-border-width) solid var(--tblr-border-color)}
.dbo-banner .dbo-brand{display:inline-flex;align-items:center;gap:.55rem;padding:0;margin:0;font-weight:700}
.dbo-banner .dbo-mark{display:inline-block;width:2.1rem;height:2.1rem;flex:none}
.dbo-banner .dbo-mark svg{display:block;width:100%;height:100%}
.dbo-banner .dbo-links{display:flex;flex-wrap:wrap;gap:2px;margin-left:auto}
.dbo-banner .dbo-theme{flex:none}
.dbo-banner .dbo-theme svg{width:20px;height:20px}
.dbo-banner .dbo-icon-sun{display:none}
[data-bs-theme=dark] .dbo-banner .dbo-icon-sun{display:inline}
[data-bs-theme=dark] .dbo-banner .dbo-icon-moon{display:none}
.dbo-banner .dbo-meta{flex-basis:100%;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 14px;
  padding-top:6px;border-top:var(--tblr-border-width) solid var(--tblr-border-color)}
.dbo-banner .dbo-title{font-weight:600}
.dbo-banner .dbo-scope{font-family:var(--tblr-font-monospace);color:var(--tblr-secondary)}
.dbo-banner .dbo-stamp{margin-left:auto;white-space:nowrap;color:var(--tblr-secondary);font-size:12px}
@media print{.dbo-banner{background:#fff!important;color:#000;border:1px solid #000}
  .dbo-banner .dbo-title,.dbo-banner .dbo-product,.dbo-banner .dbo-scope,
  .dbo-banner .dbo-stamp{color:#000}.dbo-banner .dbo-links,.dbo-banner .dbo-theme{display:none}}
"""


#: The label the per-server index report is offered under. It has no entry in
#: :data:`SIBLING_PAGES` for the reason stated there - its file name carries a `server_id`, and a
#: real one in shipped code is what `check-identifiers` refuses - so the **href is the caller's**
#: and only the word lives here.
INDEX_USAGE_LABEL = "Index usage"

#: The shape of a per-server index report's file name, as
#: :func:`dbabrain.lib.webhost_endpoints.PER_SERVER_PAGES` states it, with the slug left open.
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
    # dbabrain.dev's head (2026-10-30): the mark and the product name, the pages on the right, the
    # light / dark switch last; under them what this page is, about what, and when.
    parts = [f'<span class="dbo-brand"><span class="dbo-mark">{MARK_SVG}</span>'
             f'<span class="dbo-product">{_escape(PRODUCT)}</span></span>']

    entries = SIBLING_PAGES if links is None else tuple(links)
    if entries:
        chips = []
        for label, href in entries:
            if here and href == here:
                chips.append(f'<a class="dbo-here" aria-current="page">{_escape(label)}</a>')
            else:
                chips.append(f'<a href="{_escape(href)}">{_escape(label)}</a>')
        parts.append('<span class="dbo-links">' + "".join(chips) + "</span>")
    parts.append(f'<button type="button" class="dbo-theme" aria-label="Switch theme">{MOON_SVG}{SUN_SVG}</button>')

    meta = [f'<span class="dbo-title">{_escape(title)}</span>']
    if scope:
        meta.append(f'<span class="dbo-scope">{_escape(scope)}</span>')
    if snapshot_at:
        meta.append(f'<span class="dbo-stamp">snapshot {_escape(snapshot_at)}</span>')
    parts.append('<span class="dbo-meta">' + "".join(meta) + "</span>")
    return '<div class="dbo-banner">' + "".join(parts) + "</div>" + THEME_SCRIPT


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
    :func:`dbabrain.lib.timezone.parse_display` turns back into an instant.
    """
    match = _STAMP_PATTERN.search(str(markup or ""))
    return match.group(1).strip() if match else ""
