"""The one stylesheet the published report pages share: tokens, and the parts every page is built of.

The operator asked (2026-10-02) for the report pages to follow one common, widely used model. The
model is the admin-dashboard family most internal tools are built on - Tabler beside AdminLTE and
CoreUI - taken as a *vocabulary*, not as a library: a page is one self-contained file served from a
node that may have no internet, so nothing is fetched. What is taken: a masthead with the title and
the page's verdict, a strip of stat cards, section heads, bordered card tables with status badges
and dots, a picker, a callout for the one sentence that qualifies the numbers. The palette is the
inventory page's, which already followed it, so the pages read as one product.

Pure text, like :data:`db_ops.lib.page_banner.CSS`: a page inlines it in its ``<style>``. Classes are
plain (``.masthead``, ``.kpi``, ``.badge.b-crit``) because each page is its own document.
"""

from __future__ import annotations

#: Colours, type and the status palette: ok / warn / crit / idle, the four every page speaks in.
TOKENS = """
:root{
  --bg:#f5f6f8; --surface:#ffffff; --surface-2:#fafbfc;
  --ink:#1b2430; --muted:#64748b; --faint:#94a3b8; --line:#e5e9ef;
  --brand:#0f2540; --brand-2:#1d3b5e; --link:#1d4ed8;
  --crit:#dc2626; --crit-bg:#fef2f2; --crit-line:#fecaca;
  --warn:#c2700a; --warn-bg:#fff8ed; --warn-line:#fde3bf;
  --ok:#15803d; --ok-bg:#f0fdf4; --ok-line:#bbf7d0;
  --idle-bg:#f1f5f9;
  --mono:"SF Mono",ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
"""

#: The parts. Every rule here is used by at least two pages; a page's own rules stay in the page.
#:
#: ``.wrap`` takes the whole window - the *fluid* layout of the same family (Bootstrap's and
#: Tabler's ``container-fluid``), not their fixed container. It was capped at 1180px and centred
#: until 2026-10-03, so on an ordinary desktop screen a third of the width was margin while the
#: tables inside scrolled sideways - the operator: *why not use 100% of the client?* A table still
#: scrolls inside its own box when it truly cannot fit (``.tbl-scroll``, their
#: ``table-responsive``). The two report templates that keep their own stylesheet (inventory,
#: server metrics) state the same rule.
PARTS = """
*{box-sizing:border-box}
html{-webkit-print-color-adjust:exact; print-color-adjust:exact}
body{margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans); font-size:14px; line-height:1.5}
a{color:var(--link)}
.wrap{max-width:none; margin:0; padding:0 22px 64px}
header.masthead{background:linear-gradient(135deg,var(--brand),var(--brand-2)); color:#fff; padding:26px 0 24px; margin-bottom:22px}
header.masthead .wrap{padding-bottom:0}
h1.title{font-size:26px; font-weight:700; margin:0 0 4px; letter-spacing:-.01em}
.subtitle{color:#c7d6e8; font-size:13px; margin:0}
.subtitle a{color:#c7d6e8}
.verdict{display:inline-flex; gap:10px; align-items:baseline; flex-wrap:wrap; margin-top:16px; padding:8px 14px; border-radius:9px; font-weight:650; font-size:15px}
.verdict .scope{font-weight:500; font-size:13px; opacity:.9}
.verdict.ok{background:#14532d; color:#bbf7d0} .verdict.warn{background:#713f12; color:#fde68a}
.verdict.crit{background:#7f1d1d; color:#fecaca} .verdict.idle{background:#334155; color:#e2e8f0}
.kpi-strip{display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; margin-top:18px}
.kpi{background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.14); border-radius:10px; padding:11px 14px}
.kpi .num{font-size:24px; font-weight:700; line-height:1}
.kpi .lbl{font-size:11px; color:#a9c0db; margin-top:5px}
.kpi.alert .num{color:#ffb4ad} .kpi.warnum .num{color:#ffd79b} .kpi.good .num{color:#9be8b5}
.notes p{margin:4px 0; font-size:12.5px; color:var(--muted)}
section{margin-top:30px}
.sec-head{display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:12px; padding-bottom:8px; border-bottom:2px solid var(--line)}
.sec-head h2{font-size:18px; font-weight:700; margin:0}
.sec-head .hint{font-size:12.5px; color:var(--muted)}
h3.part{font-size:15px; font-weight:700; margin:26px 0 10px; padding-bottom:6px; border-bottom:2px solid var(--line)}
.callout{margin:16px 0; padding:12px 16px; border-left:5px solid var(--warn); background:var(--warn-bg); border-radius:6px; color:#7c2d12; font-weight:600}
.tbl-scroll{overflow-x:auto; max-width:100%; border:1px solid var(--line); border-radius:10px; background:var(--surface); margin:8px 0 14px}
table{border-collapse:collapse; width:100%; font-size:12.5px}
thead th{background:var(--surface-2); text-align:left; font-weight:650; color:#475569; padding:8px 11px; border-bottom:1px solid var(--line); white-space:nowrap; font-size:11.5px}
tbody td{padding:8px 11px; border-bottom:1px solid var(--line); vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--surface-2)}
td.srv{font-family:var(--mono); font-size:11.5px}
td.prose{white-space:normal; min-width:220px; color:#3a4757}
.num-cell{font-family:var(--mono); text-align:right}
.center{text-align:center}
.badge,.cell{display:inline-block; font-size:11px; font-weight:600; padding:2px 8px; border-radius:6px; white-space:nowrap; border:1px solid transparent}
.b-ok{background:var(--ok-bg); color:var(--ok); border-color:var(--ok-line)}
.b-warn{background:var(--warn-bg); color:var(--warn); border-color:var(--warn-line)}
.b-crit{background:var(--crit-bg); color:var(--crit); border-color:var(--crit-line)}
.b-idle{background:var(--idle-bg); color:var(--muted); border-color:var(--line)}
.dot{display:inline-block; width:9px; height:9px; border-radius:50%; background:var(--faint)}
.dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)} .dot.crit{background:var(--crit)}
.legend{display:flex; flex-wrap:wrap; gap:14px; margin-top:8px; font-size:12px; color:var(--muted)}
.muted{color:var(--muted); font-size:12px}
.meta{display:flex; flex-wrap:wrap; gap:6px 18px; font-size:12.5px; color:var(--muted); margin:4px 0 8px}
.meta b{color:var(--ink); font-weight:600}
.picker{display:grid; grid-template-columns:max-content 1fr; gap:8px 12px; align-items:start;
  margin:0 0 18px; padding:12px 14px; background:var(--surface); border:1px solid var(--line); border-radius:10px}
.picker-title{grid-column:1/-1; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); font-weight:700}
.picker-label{font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--brand); font-weight:700; padding-top:4px; white-space:nowrap}
.picker-label b{font-weight:600; color:var(--muted); border:1px solid var(--line); border-radius:999px; padding:0 6px; letter-spacing:0}
.picker-row{display:flex; flex-wrap:wrap; gap:6px}
.chip{border:1px solid var(--line); border-radius:999px; padding:4px 12px; font-size:12px; color:var(--muted); text-decoration:none; background:var(--surface)}
a.chip:hover{border-color:var(--link); color:var(--link)}
.chip.on{background:var(--brand); border-color:var(--brand); color:#fff; font-weight:600}
.chip .dot{width:6px; height:6px; margin-right:6px; vertical-align:middle}
@media (max-width:760px){.kpi-strip{grid-template-columns:repeat(3,1fr)}}
"""

#: Both, in the order a page inlines them.
CSS = TOKENS + PARTS


def kpi_class(count: int, level: str) -> str:
    """A counter is coloured only when it is not zero: a red 0 reads as an alarm."""
    return level if count else ""
