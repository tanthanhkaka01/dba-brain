"""The UI kit every published report page is drawn with: Tabler, vendored, inlined per page.

The operator, 2026-10-30: *restyle the report pages in petweb's style, using open-source UI
libraries - do not design anything by hand*. petweb is built on Tabler (Bootstrap 5 underneath), so
these pages are too: its cards, tables, badges, buttons, alerts and colour tokens, not a look drawn
here. ``lib.page_style`` once took Tabler only as a *vocabulary* (2026-10-02); now the library
itself is on the page and the pages' own stylesheets keep only what Tabler has no class for -
layout grids, the chart canvas, a column's width.

**Vendored, never fetched.** A report is served by a worker with no internet, mailed on, printed,
opened from disk: a CDN link would leave it unstyled in every one of those places. So
``vendor/tabler/tabler.min.css`` (Tabler 1.6.1, MIT, its LICENSE beside it) ships in the package
and each page carries its own copy, inline, like the rest of its stylesheet.

**Only what the page uses.** The whole sheet is 635 KB, and a report root keeps a dated copy of
every page it publishes. :func:`stylesheet` keeps a rule only when every class it selects is a word
somewhere in the page - markup or script, since most of these pages draw themselves in JavaScript
- so a page carries the kit's tokens, its base rules and the components it names, and nothing it
cannot show. It is a filter over Tabler's own rules, never an edit of one: what survives is
byte-for-byte the library's.

**The pages' own words, Tabler's rules.** The pages name things for what they are - ``b-crit``,
``tbl-scroll``, ``callout`` - and much of their markup is drawn by JavaScript that tests and readers
rely on. Rather than rewrite every one of those names, :data:`ALIASES` says which Tabler class each
one *is*, and the kit copies Tabler's rules for that class onto it - what Sass's ``@extend`` does.
``.b-crit`` gets Tabler's ``.bg-red-lt`` rules, word for word; nothing is restated by hand.

Pure apart from reading the vendored file, once, the way ``lib.config`` reads its own.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

LIBRARY = "Tabler"
VERSION = "1.6.1"
LICENSE_NAME = "MIT"

#: The vendored sheet and its license. Both ship: ``pyproject.toml`` package-data for
#: ``dbabrain.lib`` and ``export_public.TEXT_SUFFIXES`` carry ``.css`` and the LICENSE.
VENDOR_DIR = Path(__file__).resolve().parent / "vendor" / "tabler"
SHEET_PATH = VENDOR_DIR / "tabler.min.css"
LICENSE_PATH = VENDOR_DIR / "LICENSE"

#: The marker a page's kit block opens with, so a page can be checked for carrying it once.
MARKER = f"/* {LIBRARY} {VERSION} ({LICENSE_NAME}) - the rules this page uses */"

#: Tabler class -> the page selectors that are that class. A rule of Tabler's naming the class is
#: copied once per selector, with the class replaced by it, when the page uses that selector's
#: classes. Shared by every page: the report pages speak one vocabulary (``lib.page_style``).
ALIASES: dict[str, tuple[str, ...]] = {
    # A bordered box holding a table, a stat, a health area, a finding: Tabler's card.
    "card": (".tbl-scroll", ".tbl-wrap", ".kpi", ".vcard", ".area", ".picker", ".ecard", ".tcard", ".prob",
             ".hub-card", "details.inst", "details.srv-detail", "details.fold"),
    "card-link": ("a.hub-card",),
    "card-title": (".hub-title",),
    "table-responsive": (".tbl-scroll", ".tbl-wrap"),
    # Every report table is Tabler's compact card table.
    "table": (".tbl-scroll>table", "table.rpt", "table.db-tbl"),
    "table-sm": (".tbl-scroll>table", "table.rpt", "table.db-tbl"),
    "card-table": (".tbl-scroll>table", "table.rpt"),
    "table-hover": (".tbl-scroll>table",),
    # Small labels: a count, an address, a database name, where a value came from - Tabler's badge.
    "badge": (".sev", ".pill", ".dbchip", ".src-tag", ".gcount", ".picker-label b", ".picker .plabel b"),
    # The status palette is Tabler's colours: light on a badge, a verdict, a stat's number; solid
    # on a severity tag.
    "bg-red-lt": (".b-crit", ".verdict.crit", ".badge.bad"),
    "bg-orange-lt": (".b-warn", ".verdict.warn", ".badge.warn"),
    "bg-green-lt": (".b-ok", ".verdict.ok", ".badge.ok"),
    "bg-secondary-lt": (".b-idle", ".verdict.idle", ".badge.nodata", ".pill", ".dbchip", ".src-tag", ".gcount",
                        ".picker-label b", ".picker .plabel b"),
    "bg-azure-lt": (".b-info",),
    "bg-red": (".sev.crit",),
    "text-red-fg": (".sev.crit",),
    "bg-orange": (".sev.warn",),
    "text-orange-fg": (".sev.warn",),
    "bg-green": (".sev.ok",),
    "text-green-fg": (".sev.ok",),
    "bg-secondary": (".sev.unk",),
    "text-secondary-fg": (".sev.unk",),
    "bg-azure": (".sev.info",),
    "text-azure-fg": (".sev.info",),
    "text-red": (".kpi.alert .num",),
    "text-orange": (".kpi.warnum .num",),
    "text-green": (".kpi.good .num",),
    # A sentence that qualifies the numbers, a warning about one server, "all is well": Tabler's alerts.
    "alert": (".callout", ".content .banner", ".warns li", ".all-ok"),
    "alert-warning": (".callout", ".content .banner", ".warns li"),
    "alert-info": (".warns li.cfg",),
    "alert-success": (".all-ok",),
    # A stat card's label and number, a field's name, headings, a picker's labels: Tabler's type scale.
    "subheader": (".kpi .lbl", ".fact .k", ".eyebrow", ".d-grid .f .k", ".picker-title", ".picker-label", ".picker .plabel",
                  "h3.eng-head", "tr.grp th"),
    "h1": (".kpi .num",),
    "h2": (".sec-head h2",),
    "h3": ("h3.part",),
    "page-title": ("h1.title",),
    # The pickers' entries are Tabler's small pill buttons; the one on show is the primary one.
    "btn": (".picker button", ".picker-row .chip", ".dbo-banner .dbo-links a", ".dbo-banner .dbo-theme"),
    "btn-sm": (".picker button", ".picker-row .chip", ".dbo-banner .dbo-links a"),
    "btn-pill": (".picker button", ".picker-row .chip"),
    "btn-primary": (".picker button.on", ".picker-row .chip.on"),
    # A table's filters are Tabler's small selects.
    "form-select": (".ffilter select",),
    "form-select-sm": (".ffilter select",),
    # The head banner every page shares (lib.page_banner) is Tabler's navbar - light or dark with
    # the page, as dbabrain.dev's head - its links the navbar's ghost buttons, the page on show the
    # primary one, the theme switch an icon button.
    "navbar": (".dbo-banner",),
    "navbar-brand": (".dbo-banner .dbo-brand",),
    "btn-ghost-secondary": (".dbo-banner .dbo-links a", ".dbo-banner .dbo-theme"),
    "btn-ghost-primary": (".dbo-banner .dbo-links a.dbo-here",),
    "btn-icon": (".dbo-banner .dbo-theme",),
}

#: The pages' own words that Tabler happens to use for something else. A stat card flagged
#: ``alert`` is a red number, not Tabler's alert box; ``legend`` is a row of keys, not its chart
#: legend chip; ``empty`` is "nothing here", not its full-height empty state. Tabler's rules for
#: these never reach a page - except through :data:`ALIASES`, which names the page's own selector.
SHADOWED: frozenset[str] = frozenset({"alert", "d-grid", "empty", "grid", "legend", "prose", "toolbar"})

# At-rules whose body is more rules, filtered like the top level.
_GROUPING = ("@media", "@supports", "@container", "@layer")
_CLASS = re.compile(r"\.((?:\\.|[\w-])+)")
_WORD = re.compile(r"[A-Za-z_][\w-]*")


@lru_cache(maxsize=1)
def _sheet() -> str:
    return SHEET_PATH.read_text(encoding="utf-8")


def _banner() -> str:
    """The library's own copyright comment, carried by every page that carries any of its rules
    (the MIT license asks for the notice on every copy or substantial portion)."""
    sheet = _sheet()
    start = sheet.find("/*!")
    return sheet[start:sheet.index("*/", start) + 2] + "\n" if start >= 0 else ""


def _split_blocks(css: str) -> list[tuple[str, str]]:
    """Top-level ``(prelude, body)`` pairs. Comments and strings are respected; a statement
    at-rule (``@charset ...;``) comes back with an empty body."""
    blocks: list[tuple[str, str]] = []
    i, n, start = 0, len(css), 0
    while i < n:
        ch = css[i]
        if ch == "/" and css.startswith("/*", i):
            end = css.find("*/", i + 2)
            leading = not css[start:i].strip()
            i = n if end < 0 else end + 2
            if leading:  # a comment between rules (the library's banner) is not part of the next one
                start = i
            continue
        if ch in "\"'":
            end = i + 1
            while end < n and css[end] != ch:
                end += 2 if css[end] == "\\" else 1
            i = end + 1
            continue
        if ch == ";" and css[start:i].lstrip().startswith("@"):
            blocks.append((css[start:i + 1].strip(), ""))
            i += 1
            start = i
            continue
        if ch == "{":
            depth, j = 1, i + 1
            while j < n and depth:
                c = css[j]
                if c in "\"'":
                    k = j + 1
                    while k < n and css[k] != c:
                        k += 2 if css[k] == "\\" else 1
                    j = k + 1
                    continue
                if c == "/" and css.startswith("/*", j):
                    end = css.find("*/", j + 2)
                    j = n if end < 0 else end + 2
                    continue
                depth += 1 if c == "{" else -1 if c == "}" else 0
                j += 1
            blocks.append((css[start:i].strip(), css[i + 1:j - 1]))
            i = j
            start = i
            continue
        i += 1
    return blocks


def _split_selectors(prelude: str) -> list[str]:
    """``a, b:not(.c, .d)`` → ``["a", "b:not(.c, .d)"]``: commas inside brackets do not split."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(prelude):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(prelude[start:i].strip())
            start = i + 1
    parts.append(prelude[start:].strip())
    return [p for p in parts if p]


def _without_negations(selector: str) -> str:
    """The selector with every ``:not(...)`` removed: a class named only inside one is a class the
    element must *not* have, so its absence from the page cannot drop the rule."""
    out, i = [], 0
    while True:
        at = selector.find(":not(", i)
        if at < 0:
            out.append(selector[i:])
            return "".join(out)
        out.append(selector[i:at])
        depth, j = 1, at + 5
        while j < len(selector) and depth:
            depth += 1 if selector[j] == "(" else -1 if selector[j] == ")" else 0
            j += 1
        i = j


def _selector_applies(selector: str, words: frozenset[str], tabler_part: str | None = None) -> bool:
    """Whether ``selector`` can match on a page whose words are ``words``. ``tabler_part`` is what
    is left of it once the page selectors an alias put in are taken out: only Tabler's own classes
    are checked against :data:`SHADOWED`, so ``.kpi.alert .num`` (the page's) keeps its word."""
    if "data-bs-theme=dark" in selector.replace('"', "") and "data-bs-theme" not in words:
        return False
    names = [name.replace("\\", "") for name in _CLASS.findall(_without_negations(selector))]
    own = _CLASS.findall(_without_negations(selector if tabler_part is None else tabler_part))
    return all(name in words for name in names) and not any(name in SHADOWED for name in own)


def _aliased(selector: str, aliases: dict[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    """``selector`` and every copy of it with an aliased Tabler class replaced by a page selector,
    each with its Tabler part (the copy with the page selectors left out). A page selector put in
    is never aliased again: ``.kpi.alert .num`` stays the page's, whatever ``alert`` is aliased to."""
    slots: list[str] = []
    out = [selector]
    for tabler, page_selectors in aliases.items():
        pattern = re.compile(r"\." + re.escape(tabler) + r"(?![\w-])")
        for one in list(out):
            if not pattern.search(one):
                continue
            for page_selector in page_selectors:
                slots.append(page_selector)
                out.append(pattern.sub(f"\0{len(slots) - 1}\0", one))
    expand = re.compile("\0(\\d+)\0")
    return [(expand.sub(lambda m: slots[int(m.group(1))], one), expand.sub(" ", one)) for one in out]


def _filter(css: str, words: frozenset[str], aliases: dict[str, tuple[str, ...]]) -> list[str]:
    kept: list[str] = []
    for prelude, body in _split_blocks(css):
        if prelude.startswith("@"):
            if not body and prelude.endswith(";"):
                kept.append(prelude)
            elif prelude.startswith(_GROUPING):
                inner = _filter(body, words, aliases)
                if inner:
                    kept.append(prelude + "{" + "".join(inner) + "}")
            elif prelude.startswith(("@keyframes", "@-webkit-keyframes")):
                kept.append("\0" + prelude + "{" + body + "}")
            else:
                kept.append(prelude + "{" + body + "}")
            continue
        selectors = list(dict.fromkeys(
            one for s in _split_selectors(prelude) for one, part in _aliased(s, aliases)
            if _selector_applies(one, words, part)))
        if selectors:
            kept.append(",".join(selectors) + "{" + body + "}")
    return kept


def stylesheet(page: str, aliases: dict[str, tuple[str, ...]] | None = None) -> str:
    """Tabler's rules that can apply to ``page``, in the library's own order and words.

    ``page`` is the whole rendered document. A rule is kept when every class it selects (outside a
    ``:not()``) appears as a word in it; rules selecting no class - the tokens on ``:root``, the
    element base - are kept always. Keyframes are kept when a kept rule names them. ``aliases``
    defaults to :data:`ALIASES`.
    """
    words = frozenset(_WORD.findall(page))
    kept = _filter(_sheet(), words, ALIASES if aliases is None else aliases)
    body = "".join(rule for rule in kept if not rule.startswith("\0"))
    for rule in kept:
        if rule.startswith("\0"):
            name = rule[1:rule.index("{")].split()[-1]
            if name in body:
                body += rule[1:]
    return _banner() + MARKER + body


def inline(page: str) -> str:
    """``page`` with the kit's stylesheet as its first ``<style>``, before the page's own, so the
    page's few rules of its own (layout) apply over the library's at equal weight."""
    at = page.find("<style")
    if at < 0:
        at = page.find("</head>")
    if at < 0 or MARKER in page:
        return page
    return page[:at] + "<style>" + stylesheet(page) + "</style>" + page[at:]
