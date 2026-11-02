"""The one stylesheet the published report pages share: tokens, and the parts every page is built of.

The operator asked (2026-10-02) for the report pages to follow one common, widely used model. The
model is the admin-dashboard family most internal tools are built on - Tabler beside AdminLTE and
CoreUI - taken as a *vocabulary*, not as a library: a page is one self-contained file served from a
node that may have no internet, so nothing is fetched. What is taken: a masthead with the title and
the page's verdict, a strip of stat cards, section heads, bordered card tables with status badges
and dots, a picker, a callout for the one sentence that qualifies the numbers. The palette is the
inventory page's, which already followed it, so the pages read as one product.

Since 2026-10-30 the library itself is on every page (``lib.page_kit``: Tabler, vendored and
inlined - the operator, *petweb's style, open-source libraries, nothing by hand*). The vocabulary
stayed; its look is now Tabler's rules, and what is left here is the layout Tabler has no class for,
with every token one of Tabler's variables.

Pure text, like :data:`dbabrain.lib.page_banner.CSS`: a page inlines it in its ``<style>``. Classes are
plain (``.masthead``, ``.kpi``, ``.badge.b-crit``) because each page is its own document.
"""

from __future__ import annotations

#: Colours, type and the status palette: ok / warn / crit / idle, the four every page speaks in.
#: Since 2026-10-30 every token is Tabler's own variable (``lib.page_kit`` puts the library on the
#: page): the pages keep their names for the four states, and Tabler says what they look like.
TOKENS = """
:root{
  --bg:var(--tblr-bg-surface-secondary); --surface:var(--tblr-bg-surface); --surface-2:var(--tblr-bg-surface-tertiary);
  --ink:var(--tblr-body-color); --muted:var(--tblr-secondary); --faint:var(--tblr-gray-400); --line:var(--tblr-border-color);
  --brand:var(--tblr-primary); --brand-2:var(--tblr-primary-darken); --link:var(--tblr-link-color);
  --crit:var(--tblr-red); --crit-bg:var(--tblr-red-lt); --crit-line:color-mix(in oklab,var(--tblr-red) 30%,transparent);
  --warn:var(--tblr-orange); --warn-bg:var(--tblr-orange-lt); --warn-line:color-mix(in oklab,var(--tblr-orange) 30%,transparent);
  --ok:var(--tblr-green); --ok-bg:var(--tblr-green-lt); --ok-line:color-mix(in oklab,var(--tblr-green) 30%,transparent);
  --idle-bg:var(--tblr-secondary-lt);
  --mono:var(--tblr-font-monospace);
  --sans:var(--tblr-font-sans-serif);
}
"""

#: The parts Tabler has no class for: the layout of a page, its head and its strips. Everything a
#: page draws inside them - cards, tables, badges, the callout, the picker's chips, the type scale -
#: is a Tabler class, carried to the page's own names by ``lib.page_kit.ALIASES``. Every rule here
#: is used by at least two pages; a page's own rules stay in the page.
#:
#: ``.wrap`` takes the whole window - the *fluid* layout of the same family (Bootstrap's and
#: Tabler's ``container-fluid``), not their fixed container. It was capped at 1180px and centred
#: until 2026-10-03, so on an ordinary desktop screen a third of the width was margin while the
#: tables inside scrolled sideways - the operator: *why not use 100% of the client?* A table still
#: scrolls inside its own box when it truly cannot fit (``.tbl-scroll``, their
#: ``table-responsive``). The two report templates that keep their own stylesheet (inventory,
#: server metrics) state the same rule.
PARTS = """
html{-webkit-print-color-adjust:exact; print-color-adjust:exact}
.wrap{max-width:none; margin:0; padding:0 24px 64px}
header.masthead{padding:20px 0 4px; margin-bottom:8px}
header.masthead .wrap{padding-bottom:0}
h1.title{margin:0 0 2px}
.subtitle{color:var(--muted); margin:0}
.verdict{display:inline-flex; gap:10px; align-items:baseline; flex-wrap:wrap; margin-top:14px; padding:8px 14px; border-radius:var(--tblr-border-radius); font-weight:600; font-size:15px}
.verdict .scope{font-weight:500; font-size:13px}
.kpi-strip{display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-top:16px}
.kpi{padding:12px 16px}
.kpi .num{margin:4px 0 0}
.notes p{margin:4px 0; color:var(--muted)}
section{margin-top:28px}
.sec-head{display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:10px}
.sec-head h2{margin:0}
.sec-head .hint{color:var(--muted)}
h3.part{margin:24px 0 10px}
.callout{margin:16px 0}
.tbl-scroll{max-width:100%; margin:8px 0 16px}
.tbl-scroll>table{margin:0}
td.srv{font-family:var(--mono)}
td.prose{white-space:normal; min-width:220px}
.num-cell{font-family:var(--mono); text-align:right}
.center{text-align:center}
.cell{display:inline-block; white-space:nowrap}
.dot{display:inline-block; width:9px; height:9px; border-radius:50%; background:var(--faint)}
.dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)} .dot.crit{background:var(--crit)}
.legend{display:flex; flex-wrap:wrap; gap:14px; margin-top:8px; color:var(--muted)}
.muted{color:var(--muted)}
.meta{display:flex; flex-wrap:wrap; gap:6px 18px; color:var(--muted); margin:4px 0 8px}
.meta b{color:var(--ink); font-weight:600}
.picker{display:grid; grid-template-columns:max-content 1fr; gap:8px 12px; align-items:start; margin:0 0 18px; padding:12px 14px}
.picker-title{grid-column:1/-1}
.picker-label{padding-top:4px; white-space:nowrap}
.picker-row{display:flex; flex-wrap:wrap; gap:6px}
.chip .dot{width:6px; height:6px; margin-right:6px; vertical-align:middle}
@media (max-width:760px){.kpi-strip{grid-template-columns:repeat(3,1fr)}}
"""

#: Both, in the order a page inlines them.
CSS = TOKENS + PARTS


def kpi_class(count: int, level: str) -> str:
    """A counter is coloured only when it is not zero: a red 0 reads as an alarm."""
    return level if count else ""
