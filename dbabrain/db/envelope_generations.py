"""Rows whose secret is an envelope sealed with the store's passphrase - re-sealed as a NEW row (0.42.0, 1.411).

Two tables keep something only the secret store's passphrase opens: ``repository_keys`` (an age key's
private half) and ``backup_certificates`` (a SQL Server backup certificate and its private key). When
the passphrase is changed, the operator, 2026-11-21 (translated): *do not update that important data
and lose its trace - set the old one inactive, then make the new one active*. So a rotation never
rewrites an envelope: each active row is copied whole into a new row sealed with the new passphrase,
and the old row is marked ``retired_at`` / ``replaced_by`` / ``retired_reason`` and left exactly as it
was. Everything that refers to the old row by its id (an encrypted file's ``key_id``) is followed to the
active row of the same key by the store that reads it.

The table's own UNIQUE (``recipient``; ``server_id, certificate_name, thumbprint``) held one row per key;
from schema 2 it holds one ACTIVE row per key - a partial unique index - so the retired generations
can stay beside the active one.
"""

from __future__ import annotations

import json
import re
from typing import Any

from dbabrain.db import backend as backend_mod
from dbabrain.db.store import utc_now_text
from dbabrain.lib import secret_text

#: What a retired row carries; NULL on the active one.
RETIRE_COLUMNS = (("retired_at", "TEXT NULL"), ("replaced_by", "INTEGER NULL"), ("retired_reason", "TEXT NULL"))
_RETIRE_NAMES = tuple(name for name, _ in RETIRE_COLUMNS)


def migrate(conn: Any, backend: str, *, table: str, old_constraint: str, index: str,
            unique_columns: tuple[str, ...], schema_sql: str) -> None:
    """Bring a schema-1 ``table`` to schema 2: the retire columns, and UNIQUE over the active rows only.

    PostgreSQL: the columns are added and the table constraint dropped where the table stands.
    SQLite cannot drop a table constraint, so a table still declaring it is rebuilt from today's
    declaration in ``schema_sql`` and its rows copied across, ids and all. Run twice, it changes nothing.
    """
    if backend == backend_mod.POSTGRESQL_BACKEND:
        for name, sql in RETIRE_COLUMNS:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {sql}")
        conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {old_constraint}")
    else:
        declared = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                                (table,)).fetchone()
        if declared is not None and old_constraint in str(declared["sql"] or ""):
            before = [str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_before_v2")
            conn.execute(_create_statement(schema_sql, table))
            kept = ", ".join(before)
            conn.execute(f"INSERT INTO {table} ({kept}) SELECT {kept} FROM {table}_before_v2")
            conn.execute(f"DROP TABLE {table}_before_v2")
        present = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, sql in RETIRE_COLUMNS:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql}")
    conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {table} ({', '.join(unique_columns)}) "
                 "WHERE retired_at IS NULL")


def _create_statement(script: str, table: str) -> str:
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\(.*?\);", script, re.DOTALL)
    if match is None:
        raise ValueError(f"the schema declares no {table}")
    return match.group(0).replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1)


def reseal(conn: Any, *, table: str, id_column: str, envelope_column: str, unique_columns: tuple[str, ...],
           old_passphrase: str, new_passphrase: str, reason: str) -> list[dict[str, Any]]:
    """Every active row of ``table`` re-sealed as a new row, the old one retired - on *conn*, so the
    caller's transaction holds it all or none. Raises before writing anything when one active row does
    not open with *old_passphrase*. Answers ``[{old_id, new_id}]``; the envelopes are never returned.
    """
    if not old_passphrase or not new_passphrase or old_passphrase == new_passphrase:
        raise ValueError("an old and a different new passphrase are both required")
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM {table} WHERE retired_at IS NULL ORDER BY {id_column}").fetchall()]
    opened = []
    for row in rows:
        try:
            plain = secret_text.decrypt_secret_text(json.loads(row[envelope_column]), old_passphrase)
        except Exception as exc:  # noqa: BLE001 - one row that does not open stops the whole rotation
            raise ValueError(f"{table} row {row[id_column]} does not open with the old passphrase "
                             f"({type(exc).__name__}) - nothing was changed") from exc
        opened.append((row, plain))
    now = utc_now_text()
    done = []
    for row, plain in opened:
        old_id = int(row[id_column])
        # Retired first: the active row's UNIQUE would refuse its copy while it is still active.
        conn.execute(f"UPDATE {table} SET retired_at = ?, retired_reason = ? WHERE {id_column} = ? "
                     "AND retired_at IS NULL", (now, reason, old_id))
        copy = {k: v for k, v in row.items() if k != id_column and k not in _RETIRE_NAMES}
        copy[envelope_column] = json.dumps(secret_text.encrypt_secret_text(plain, new_passphrase))
        conn.execute(f"INSERT INTO {table} ({', '.join(copy)}) VALUES ({', '.join('?' for _ in copy)})",
                     tuple(copy.values()))
        where = " AND ".join(f"{c} = ?" for c in unique_columns)
        new = conn.execute(f"SELECT {id_column}, {envelope_column} FROM {table} WHERE {where} "
                           "AND retired_at IS NULL", tuple(row[c] for c in unique_columns)).fetchone()
        if new is None or secret_text.decrypt_secret_text(json.loads(new[envelope_column]), new_passphrase) != plain:
            raise ValueError(f"{table} row {old_id}: its new row does not read back sealed with the new "
                             "passphrase - nothing was changed")
        conn.execute(f"UPDATE {table} SET replaced_by = ? WHERE {id_column} = ?", (int(new[id_column]), old_id))
        done.append({"old_id": old_id, "new_id": int(new[id_column])})
    return done
