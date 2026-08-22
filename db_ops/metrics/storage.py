"""Backwards-compatible re-export: the metric store now lives in ``db_ops.db.metric_store``.

It moved because three other apps read it — ``jobs.status``, ``reports.inventory_health`` and
``reports.server_report`` — which made it the codebase's only standing exception to "apps depend on
``common``, never on each other". Shared API living inside an app is what forces that exception, so
the store moved to the shared layer and the exception went away rather than being documented again.

This module stays so the metrics app's own imports, and the dozen tests that import
``db_ops.metrics.storage``, keep working unchanged. New code should import from
``db_ops.db.metric_store``.
"""

from __future__ import annotations

from db_ops.db.metric_store import (  # noqa: F401 - re-exported for compatibility
    METRIC_SCHEMA_VERSION,
    SCHEMA_SQL,
    MetricStore,
    cutoff_text,
    ensure_sqlite_column,
)

__all__ = [
    "METRIC_SCHEMA_VERSION",
    "SCHEMA_SQL",
    "MetricStore",
    "cutoff_text",
    "ensure_sqlite_column",
]
