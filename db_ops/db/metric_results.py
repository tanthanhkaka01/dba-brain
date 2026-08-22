"""The shape of one collected metric row — shared between the metrics app and the store.

It lives here rather than in the metrics app because the store that persists it sits in ``common``
(``common/metric_store.py``), and ``common`` must never import an app. A row shape used on both
sides of that boundary belongs below both.

It was its own package (``db_ops/contracts/``) until 2026-08-15, folded in because a package for
238 lines of dataclasses was one layer more than the tree needed. What survived the fold is the
property that mattered: **this module imports nothing but stdlib**, so ``db/store.py`` naming
``MetricResult`` still pulls no library code along with it. Keep it that way — the moment a shape
imports a helper, the store starts depending on the helper too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MetricResult:
    target_id: str
    server_id: str
    ip: str
    db_type: str
    db_name: str
    metric_code: str
    metric_item: str | None
    metric_value: str | None
    metric_unit: str | None
    status: str
    importance: int
    message: str | None
    collected_at: str
    raw_stdout: str | None = None
    raw_stderr: str | None = None
    exit_code: int | None = None
    execution_time: float | None = None
    collector_type: str | None = None
    category: str | None = None
    error_type: str | None = None
    normalized_error_signature: str | None = None


def rows_by_target(rows: list[Any]) -> dict[str, list[Any]]:
    """Group stored metric rows by ``target_id``, preserving each target's row order.

    Both the metrics CLI and the reports app render "one section per target" and each had
    an identical private copy of this until 2026-08-06. It lives with the row contract
    because the grouping key *is* part of that contract: ``target_id`` is the identity a
    metric row is filed under, and a row whose ``target_id`` is NULL must land under one
    agreed bucket (``""``) rather than under ``None`` in one app and ``"None"`` in another.
    """
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row["target_id"] or ""), []).append(row)
    return grouped
