"""The texts a page shows that belong to the page, read from ``reports_config.json`` (0.39.0).

A health area's label, threshold line and note, and the fleet page's cards, were literals in the
report builders - one wording whatever the engine: the Memory tile explained page life expectancy
to a PostgreSQL host, the temporary-space tile was called TempDB on Oracle, the replication note
described Availability Groups to a standby. They are ``page_texts`` in ``reports_config.json`` now
(:mod:`dbabrain.lib.report_texts`), each sayable per engine or operating system, and this module is
the one reader. A per-metric text - a label, a *What to do* sentence - is the metric's own and is
read in ``server_series`` / ``server_health``.

An entry the node's own file does not state is read from the file as it ships
(``reports/catalogue/reports_config.json``): a node whose ``upgrade-config`` has not run yet would
otherwise draw a card with no title and no body. An entry the node states - in whatever part - is
the node's, whole; ``upgrade-config`` (``report-texts``) writes the shipped ones into the node's
file by the same rule, so the file shows what the page reads.

Best-effort, like every reader of a page's configuration: with neither file a text is ``""`` (a
label falls back to its key in words), never a failed page.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dbabrain.lib import engine_sections, report_texts
from dbabrain.lib.paths import DEFAULT_DATA_DIR

#: The node's own file. The tests point it at the shipped example (conftest).
REPORTS_CONFIG = DEFAULT_DATA_DIR / "reports_config.json"

#: The same file as it ships, inside the package: what a page reads for an entry the node's own
#: file does not state.
SHIPPED_REPORTS_CONFIG = Path(__file__).resolve().parent / "catalogue" / "reports_config.json"

_PAGE_TEXTS: dict[str, Any] | None = None


def _stated(path: str | Path) -> dict[str, Any]:
    """The ``page_texts`` object one file states; ``{}`` when the file is missing or states none."""
    try:
        document = json.loads(Path(path).read_bytes().decode("utf-8-sig"))
        texts = document.get(report_texts.PAGE_TEXTS) if isinstance(document, dict) else None
    except (OSError, ValueError):
        texts = None
    return texts if isinstance(texts, dict) else {}


def page_texts(path: str | Path | None = None) -> dict[str, Any]:
    """The ``page_texts`` a page reads, cached: each entry (one area, one card) as the node's
    ``reports_config.json`` states it, and the shipped file's for an entry the node does not state."""
    global _PAGE_TEXTS
    if _PAGE_TEXTS is not None and path is None:
        return _PAGE_TEXTS
    texts: dict[str, Any] = {}
    for stated in (_stated(SHIPPED_REPORTS_CONFIG), _stated(path or REPORTS_CONFIG)):
        for section, entries in stated.items():
            if isinstance(entries, dict):
                texts[section] = {**texts.get(section, {}), **entries}
    if path is None:
        _PAGE_TEXTS = texts
    return texts


def _engine(db_type: object) -> str:
    """The engine's key as the texts spell it (``postgres`` -> ``postgresql``); ``""`` for a bare host."""
    key = engine_sections.section_of(db_type) if str(db_type or "").strip() else ""
    return key if key not in ("host", "other") else ""


def area_text(key: str, field: str, *, db_type: object = "", platform: object = "") -> str:
    """One text of a health area - ``label``, ``threshold`` or ``note`` - as it is said for that
    engine and operating system. A label nobody stated is the key in words."""
    entry = (page_texts().get("areas") or {}).get(key)
    said = report_texts.pick(entry, field, db_type=_engine(db_type), platform=str(platform or ""))
    if not said and field == "label":
        return str(key).replace("_", " ").capitalize()
    return said


def card_text(key: str, field: str, /, **values: Any) -> str:
    """One text of a fleet card - ``title``, ``body`` or ``action`` - with its ``{placeholders}``
    filled. A placeholder the text names and the page did not give is left as written, so a typo
    in the data shows on the page instead of failing it."""
    template = report_texts.pick((page_texts().get("fleet_cards") or {}).get(key), field)

    class _Kept(dict):
        def __missing__(self, name: str) -> str:
            return "{" + name + "}"

    try:
        return template.format_map(_Kept(values))
    except (ValueError, IndexError):
        return template
