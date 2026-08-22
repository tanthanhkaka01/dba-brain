"""What a metric definition promises to anyone who filters on engine type.

The definitions themselves are loaded and validated by the metrics app
(``db_ops/metrics/definitions.py``), but Reports also filters them — it renders one
section per engine — and Reports may not import an app. Both sides therefore had the same
predicate written out twice until the 2026-08-06 audit. The rule it encodes is not
obvious enough to retype safely:

* A definition with **variants** is per-engine, so it supports exactly the engines its
  variants name; the top-level ``db_type`` is then a label, not the answer.
* A definition **without** variants answers on its own ``db_type``, where the empty
  string, ``multi``, ``all`` and ``*`` all mean "every engine".

Getting the first rule wrong is silent: a multi-engine definition whose variants cover
only SQL Server would be offered for every Oracle target and simply produce nothing.
"""

from __future__ import annotations

from typing import Any


MULTI_DB_TYPE_MARKERS = frozenset({"multi", "all", "*"})


def definition_supports_db_type(definition: Any, db_type: str) -> bool:
    variants = getattr(definition, "variants", None) or getattr(definition, "sql_variants", []) or []
    if variants:
        return any(getattr(variant, "db_type", "") == db_type for variant in variants)
    return str(getattr(definition, "db_type", "")).lower() in {db_type, *MULTI_DB_TYPE_MARKERS}
