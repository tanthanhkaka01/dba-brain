"""The texts a report page shows are data, and a node's own files get the shipped ones (0.39.0).

The operator, 2026-11-13, reading a Linux host that was told to read the Windows event log: *fix
what is hard-coded, the texts too ... look at the other places as well.* The pages' words were
tables in the reports' code - one sentence per metric code for every operating system and engine,
one note per health area whatever the engine. They are data now, in two files:

* ``metric_definitions.json`` - each metric's own: ``report_policy.label`` (what the page calls it),
  ``report_policy.action`` (*What to do*), and ``action_by_platform`` / ``action_by_db_type`` for
  the words that differ by operating system or engine;
* ``reports_config.json`` -> ``page_texts`` - what belongs to a page and not to one metric: each
  health area's ``label``, ``threshold`` and ``note`` (``areas``), and the fleet page's cards
  (``fleet_cards``: ``title``, ``body``, ``action``, with ``{placeholders}`` the page fills).

A text may be said differently for an engine or an operating system::

    "memory": {"label": "Memory", "note": "...",
               "by_db_type": {"sqlserver": {"note": "Low page life expectancy means ..."}}}

:func:`pick` reads one: the engine's, then the operating system's, then the general one.

As data, a node installed before 0.39.0 has files of its own that state none of this, and its pages
would show codes and blanks. ``upgrade-config`` (``report-texts``) closes that: what a node's file
does **not** state is written from the shipped catalogue; what it states is left exactly as it is.

Values and rules only; ``common.config_upgrade`` reads the files, the reports read the texts.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ACTION_KEYS", "LABEL_KEY", "PAGE_TEXTS", "pick", "state_metric_texts", "state_page_texts"]

#: The keys of a metric's ``report_policy`` that hold its *What to do* sentences - stated together:
#: a metric that states any of them is the node's own wording, and gets none of the shipped ones.
ACTION_KEYS = ("action", "action_by_platform", "action_by_db_type")
#: The key of a metric's ``report_policy`` that holds what a page calls it.
LABEL_KEY = "label"
#: The key of ``reports_config.json`` that holds the pages' own texts.
PAGE_TEXTS = "page_texts"


def pick(entry: Any, field: str, *, db_type: str = "", platform: str = "") -> str:
    """``field`` of one text entry, as it is said for that engine, else that operating system, else
    in general; ``""`` when the entry states none."""
    if not isinstance(entry, dict):
        return ""
    for variants, key in (("by_db_type", db_type), ("by_platform", platform)):
        wanted = str(key or "").strip().lower()
        said = entry.get(variants)
        if wanted and isinstance(said, dict):
            for name, values in said.items():
                if str(name).strip().lower() == wanted and isinstance(values, dict) and str(values.get(field) or "").strip():
                    return str(values[field]).strip()
    return str(entry.get(field) or "").strip()


def _metrics(document: Any) -> list[dict[str, Any]]:
    metrics = document.get("metrics") if isinstance(document, dict) else None
    return [item for item in metrics if isinstance(item, dict)] if isinstance(metrics, list) else []


def state_metric_texts(document: dict[str, Any], shipped: dict[str, Any]) -> list[dict[str, str]]:
    """Write the shipped sentences and label into every metric of ``document`` that states none.

    The sentences go together (a metric stating any of :data:`ACTION_KEYS` keeps its own and gets
    none); the label on its own. One change per metric and kind written. A metric the shipped
    catalogue does not know, or has no text for, is left.
    """
    stated = {str(item.get("metric_code") or ""): item["report_policy"]
              for item in _metrics(shipped) if isinstance(item.get("report_policy"), dict)}
    changes: list[dict[str, str]] = []
    for metric in _metrics(document):
        code = str(metric.get("metric_code") or "")
        policy = metric.get("report_policy")
        if policy is not None and not isinstance(policy, dict):
            continue                                   # not a shape this step writes into
        theirs = stated.get(code) or {}
        sentences = {key: theirs[key] for key in ACTION_KEYS if key in theirs}
        if sentences and not any(key in (policy or {}) for key in ACTION_KEYS):
            metric.setdefault("report_policy", {}).update(sentences)
            changes.append({"where": f"metrics[{code}]", "field": "report_policy.action",
                            "value": "the shipped What to do sentence(s): " + ", ".join(sentences)})
        if LABEL_KEY in theirs and LABEL_KEY not in (metric.get("report_policy") or {}):
            metric.setdefault("report_policy", {})[LABEL_KEY] = theirs[LABEL_KEY]
            changes.append({"where": f"metrics[{code}]", "field": "report_policy.label",
                            "value": f"the shipped label: {theirs[LABEL_KEY]}"})
    return changes


def state_page_texts(document: dict[str, Any], shipped: dict[str, Any]) -> list[dict[str, str]]:
    """Write into ``document``'s ``page_texts`` every entry of the shipped one it does not state -
    whole entries (one area, one card): an entry the node states, in whatever part, is the node's.
    One change per entry written."""
    theirs = shipped.get(PAGE_TEXTS) if isinstance(shipped, dict) else None
    if not isinstance(theirs, dict) or not isinstance(document, dict):
        return []
    mine = document.get(PAGE_TEXTS)
    if mine is not None and not isinstance(mine, dict):
        return []                                      # not a shape this step writes into
    changes: list[dict[str, str]] = []
    for section, entries in theirs.items():
        if not isinstance(entries, dict):
            continue
        held = (mine or {}).get(section)
        if held is not None and not isinstance(held, dict):
            continue
        for key, entry in entries.items():
            if key in (held or {}):
                continue
            document.setdefault(PAGE_TEXTS, {}).setdefault(section, {})[key] = entry
            changes.append({"where": f"{PAGE_TEXTS}.{section}[{key}]", "field": "(the entry)",
                            "value": "the shipped text of this " + ("area" if section == "areas" else "card"
                                                                    if section == "fleet_cards" else "entry")})
    return changes
