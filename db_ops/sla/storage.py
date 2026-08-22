"""Backwards-compatible re-export: the SLA store now lives in ``db_ops.db.sla_store``.

It moved on 2026-08-11, for the reason ``metrics/storage.py`` moved before it: a store class inside
an app is shared API in a place nothing else may import from. This one was doing measurable damage
rather than just looking wrong — ``db/cli.py`` had to reach *up* into `sla` and `backup_restore` to
build the runtime store's schema, because two of the four classes that define it lived in apps.
That upward import was the standing exception in ``tests/test_import_boundaries.py``; moving the
two stores down retired it.

Nothing about the schema or the tables changed in the move — same DDL, same
``SLA_SCHEMA_VERSION``, so a deployed store needs no migration.

New code should import from ``db_ops.db.sla_store``.
"""

from __future__ import annotations

from db_ops.db.sla_store import (  # noqa: F401 - re-exported for compatibility
    SCHEMA_SQL,
    SLA_SCHEMA_VERSION,
    SlaStore,
)

__all__ = ["SCHEMA_SQL", "SLA_SCHEMA_VERSION", "SlaStore"]
