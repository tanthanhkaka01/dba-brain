"""Every runtime-store table has its keys, and a configuration row is switched off, never deleted
(rules R22).

A table without a primary key cannot be migrated row for row (`sqlite_to_postgres` copies by key),
cannot be updated one row at a time, and fills with duplicates nobody can tell apart. And a deleted
configuration row takes its history with it: `config_item_revisions` exists so a change can be
read back and undone, which a `DELETE` makes impossible. Both were review items - "remember to add
the key" - until 0.24.0.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

DB_OPS = Path(__file__).resolve().parents[1] / "db_ops"

#: Tables without a primary key. This guard's first run (2026-09-26) found the two archives rows
#: are moved into; a new store declares both with the id each row kept since 0.24.0, and an older
#: store gets them by `db.cli archive-keys` (`test_archive_tables_take_their_keys.py`). Empty, and
#: it stays empty.
KEYLESS_LEFT: frozenset[str] = frozenset()

#: The configuration the store holds (`db_ops/db/config_store.py`): switched off, never removed.
CONFIG_TABLES = ("config_items", "config_collections", "config_sources", "config_item_revisions")


def test_every_store_table_has_a_primary_key(tmp_path):
    """Built by the apps' own initializers, as a node builds it."""
    from db_ops.backup_restore.history import BackupRestoreHistory
    from db_ops.db import DbOpsStore
    from db_ops.metrics.storage import MetricStore
    from db_ops.sla.storage import SlaStore

    path = tmp_path / "db_ops.sqlite"
    DbOpsStore(path).initialize()
    MetricStore(path).initialize()
    SlaStore(path).initialize()
    BackupRestoreHistory(path).store.initialize()

    with sqlite3.connect(path) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        keyless = [table for table in tables
                   if not any(column[5] for column in conn.execute(f'PRAGMA table_info("{table}")'))]
    assert len(tables) > 10, f"the store built only {tables}"
    new = sorted(set(keyless) - KEYLESS_LEFT)
    assert not new, f"tables with no primary key: {new}"
    gone = sorted(KEYLESS_LEFT - set(keyless))
    assert not gone, f"these have a key now - delete them from KEYLESS_LEFT: {gone}"


def test_no_code_deletes_a_configuration_row():
    pattern = re.compile(r"DELETE\s+FROM\s+[\w.\"{}()]*\b(" + "|".join(CONFIG_TABLES) + r")\b", re.IGNORECASE)
    offenders = []
    for path in sorted(DB_OPS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and pattern.search(node.value):
                offenders.append(f"{path.relative_to(DB_OPS).as_posix()}:{node.lineno}")
    assert not offenders, (f"a configuration row is switched off, never deleted: {offenders}")
