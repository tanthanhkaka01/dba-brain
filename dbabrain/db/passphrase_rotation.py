"""The runtime store's half of a passphrase change: every envelope it keeps, re-sealed in ONE transaction.

The store keeps two things only the secret store's passphrase opens - the repository's age keys and
the SQL Server backup certificates (0.42.0, 1.411). Both are re-sealed on one connection, so the store
is either wholly on the new passphrase or wholly on the old one: half of it on each would leave a daemon
with one passphrase unable to open the other half. Old rows are retired, never rewritten
(:mod:`dbabrain.db.envelope_generations`).
"""

from __future__ import annotations

import json
from typing import Any

from dbabrain.db import certificate_vault as V
from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore
from dbabrain.lib import secret_text

#: Each store, its table, the id and the envelope - what a plan counts.
TABLES = (("repository_keys", "key_id", "identity_envelope"), ("backup_certificates", "certificate_id", "envelope"))


def plan(target: Any, *, old_passphrase: str) -> dict[str, Any]:
    """What a rotation would re-seal: the active rows of each table and whether each opens with
    *old_passphrase* - read only."""
    RepositoryEncryptionStore(target).initialize()
    V.CertificateVault(target).initialize()
    answer: dict[str, Any] = {}
    with target.connect() as conn:
        for table, id_column, envelope in TABLES:
            rows = conn.execute(f"SELECT {id_column}, {envelope} FROM {table} WHERE retired_at IS NULL "
                                f"ORDER BY {id_column}").fetchall()
            retired = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE retired_at IS NOT NULL").fetchone()
            refused = []
            for row in rows:
                try:
                    secret_text.decrypt_secret_text(json.loads(row[envelope]), old_passphrase)
                except Exception:  # noqa: BLE001 - counted and named, never raised from a plan
                    refused.append(int(row[id_column]))
            answer[table] = {"active": len(rows), "retired": int(retired["n"] or 0), "not_opening": refused}
    return answer


def reseal_all(target: Any, *, old_passphrase: str, new_passphrase: str, reason: str) -> dict[str, Any]:
    """Both tables re-sealed on one connection - all of it, or (on any refusal) none of it."""
    keys = RepositoryEncryptionStore(target)
    vault = V.CertificateVault(target)
    keys.initialize()
    vault.initialize()
    with target.connect() as conn:
        done_keys = keys.reseal(conn, old_passphrase=old_passphrase, new_passphrase=new_passphrase, reason=reason)
        done_certs = V.reseal(vault, conn, old_passphrase=old_passphrase, new_passphrase=new_passphrase,
                              reason=reason)
    return {"repository_keys": done_keys, "backup_certificates": done_certs}
