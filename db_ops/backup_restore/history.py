"""Backwards-compatible re-export: the history store now lives in
``db_ops.db.backup_restore_history``.

It moved on 2026-08-11 alongside ``sla/storage.py``, and for the same reason: ``db/cli.py`` builds
the runtime store's schema from the four classes that define it, and two of those lived inside
apps — so the `db` layer had to import *up* into an app to do it. That was the standing exception
in ``tests/test_import_boundaries.py``; with both stores in the shared layer it is gone.

The tables, the DDL and ``HISTORY_SCHEMA_VERSION`` are unchanged, so a deployed store needs no
migration. New code should import from ``db_ops.db.backup_restore_history``.
"""

from __future__ import annotations

from db_ops.db.backup_restore_history import (  # noqa: F401 - re-exported for compatibility
    HISTORY_SCHEMA_VERSION,
    BackupRestoreHistory,
)

__all__ = ["HISTORY_SCHEMA_VERSION", "BackupRestoreHistory"]
