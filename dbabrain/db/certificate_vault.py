"""The backup certificate vault - a SQL Server backup certificate kept by dbabrain, never beside its backups (0.37.0).

The operator, 2026-11-10 (translated): *certificates and keys must not be stored with the backup files
- keep them apart, encrypted, in the dbabrain that runs (the worker)*; *nor should they stay on the
server_id being backed up: take them once, copy them to dbabrain, then delete them on the server -
but keep them carefully, never lose them*; and, asked where: *the runtime store only*.

Why it matters: a SQL Server backup written ``WITH ENCRYPTION`` restores on another instance only
with its certificate (``.cer``) and private key (``.pvk``, itself encrypted by the backup password).
Beside the backups, whoever takes the backups takes what opens them; lost, every encrypted backup is
restorable only on the instance that wrote it. So:

* **One table, ``backup_certificates``, append-only.** A row is never deleted and its pair never
  rewritten - this module has no method that does either. One row per (server_id, certificate name,
  thumbprint): taking the same certificate again answers the row already there; a new thumbprint is
  a new row, and the old one stays (a backup made with it still needs it).
* **The pair is encrypted** with the secret store's passphrase (PBKDF2-SHA256 + Fernet, a salt per
  row - :mod:`dbabrain.lib.secret_text`'s envelope), and the row keeps what proves it: each file's
  sha256 and bytes, the thumbprint (the SHA-1 of the ``.cer`` - SQL Server's own), and the
  ``password_ref`` of the ``.pvk`` password. The password itself stays in the secret store.
* **Proven before anything relies on it.** :meth:`CertificateVault.prove` reads the row back on a
  NEW connection, decrypts it, and holds both files to the sha256, the bytes and the thumbprint
  stored; only then is ``verified_at`` set - and a source copy is deleted only for a verified row.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from dbabrain.db import backend as backend_mod
from dbabrain.db.backend import StoreTarget
from dbabrain.db.store import utc_now_text as utc_text
from dbabrain.lib import errors
from dbabrain.lib import secret_text
from dbabrain.lib.integrity import SHA1_HEX as _SHA1
from dbabrain.lib.integrity import SHA256_HEX as _SHA256

#: Bumped when this table changes.
CERTIFICATE_VAULT_SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS backup_certificates
(
    certificate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id TEXT NOT NULL,
    certificate_name TEXT NOT NULL,
    thumbprint TEXT NOT NULL,
    cer_file_name TEXT NOT NULL,
    cer_source_path TEXT NOT NULL,
    cer_size_bytes INTEGER NOT NULL,
    cer_sha256 TEXT NOT NULL,
    pvk_file_name TEXT NOT NULL,
    pvk_source_path TEXT NOT NULL,
    pvk_size_bytes INTEGER NOT NULL,
    pvk_sha256 TEXT NOT NULL,
    password_ref TEXT NOT NULL,
    envelope TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    taken_by TEXT NOT NULL,
    backup_id TEXT NULL,
    verified_at TEXT NULL,
    source_deleted_at TEXT NULL,
    CONSTRAINT uq_backup_certificates UNIQUE (server_id, certificate_name, thumbprint)
);

CREATE INDEX IF NOT EXISTS ix_backup_certificates_name ON backup_certificates (server_id, certificate_name);
"""


class CertificateVaultError(errors.OperationFailed):
    """The vault refused, or what it holds is not what it was given."""


def thumbprint_of(cer: bytes) -> str:
    """SQL Server's thumbprint of a certificate: the SHA-1 of its DER bytes (the ``.cer`` file)."""
    return hashlib.sha1(cer).hexdigest()  # noqa: S324 - SQL Server's thumbprint IS the SHA-1.


def facts_of(data: bytes) -> tuple[int, str]:
    return len(data), hashlib.sha256(data).hexdigest()


class CertificateVault:
    def __init__(self, source: Any, *, key: str | None = None, password: str | None = None) -> None:
        self.target = StoreTarget.coerce(source, key=key, password=password)

    @classmethod
    def from_config(cls, config: Any, *, key: str | None = None, password: str | None = None) -> "CertificateVault":
        return cls(StoreTarget.from_config(config, key=key, password=password))

    def connect(self):
        return self.target.connect()

    def initialize(self) -> None:
        if backend_mod.schema_is_ready("CertificateVault", self.target):
            return
        if backend_mod.remote_schema_is_current(self.target, "CertificateVault", CERTIFICATE_VAULT_SCHEMA_VERSION):
            backend_mod.mark_schema_ready("CertificateVault", self.target)
            return
        self.target.prepare()
        with self.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(SCHEMA_SQL)
            backend_mod.record_schema_version(conn, "CertificateVault", CERTIFICATE_VAULT_SCHEMA_VERSION)
        backend_mod.mark_schema_ready("CertificateVault", self.target)

    # ------------------------------------------------------------------ #
    # Writing - once per (server_id, name, thumbprint), never again
    # ------------------------------------------------------------------ #
    def keep(self, *, server_id: str, certificate_name: str, cer: bytes, pvk: bytes, cer_source_path: str,
             pvk_source_path: str, password_ref: str, passphrase: str, taken_by: str,
             backup_id: str = "") -> dict[str, Any]:
        """Store the pair, encrypted; answer the row (``already_kept`` when this exact pair was).

        The same certificate taken again is the row already there, untouched - provided its files
        are the same bytes; a row with this thumbprint and other bytes is refused, never overwritten.
        """
        for name, value in (("server_id", server_id), ("certificate_name", certificate_name),
                            ("password_ref", password_ref), ("taken_by", taken_by)):
            if not str(value or "").strip():
                raise CertificateVaultError(f"{name} is required.")
        if not cer or not pvk:
            raise CertificateVaultError("both the .cer and the .pvk are required, and neither may be empty.")
        if not passphrase:
            raise CertificateVaultError("no passphrase: the pair is never stored unencrypted.")
        thumbprint = thumbprint_of(cer)
        cer_size, cer_sha = facts_of(cer)
        pvk_size, pvk_sha = facts_of(pvk)
        existing = self.find(server_id=server_id, certificate_name=certificate_name, thumbprint=thumbprint)
        if existing is not None:
            if (existing["cer_sha256"], existing["pvk_sha256"]) != (cer_sha, pvk_sha):
                raise CertificateVaultError(
                    f"{server_id} {certificate_name} thumbprint {thumbprint} is already kept with OTHER bytes "
                    f"(row {existing['certificate_id']}) - nothing is overwritten; look at both before going on.")
            return {**existing, "already_kept": True}
        envelope = secret_text.encrypt_secret_text(
            {"cer": base64.b64encode(cer).decode("ascii"), "pvk": base64.b64encode(pvk).decode("ascii")}, passphrase)
        row = {
            "server_id": server_id, "certificate_name": certificate_name, "thumbprint": thumbprint,
            "cer_file_name": _last_part(cer_source_path), "cer_source_path": cer_source_path,
            "cer_size_bytes": cer_size, "cer_sha256": cer_sha,
            "pvk_file_name": _last_part(pvk_source_path), "pvk_source_path": pvk_source_path,
            "pvk_size_bytes": pvk_size, "pvk_sha256": pvk_sha, "password_ref": password_ref,
            "envelope": json.dumps(envelope), "taken_at": utc_text(), "taken_by": taken_by,
            "backup_id": backup_id or None,
        }
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.connect() as conn:
            conn.execute(f"INSERT INTO backup_certificates ({columns}) VALUES ({marks})", tuple(row.values()))
        kept = self.find(server_id=server_id, certificate_name=certificate_name, thumbprint=thumbprint)
        if kept is None:
            raise CertificateVaultError("the row was written and cannot be read back.")
        return {**kept, "already_kept": False}

    def prove(self, certificate_id: int, *, passphrase: str) -> dict[str, Any]:
        """Read the row back on a new connection, decrypt it, and hold both files to every fact kept.
        Sets ``verified_at`` and answers the row; raises when anything differs."""
        cer, pvk, row = self.pair(certificate_id, passphrase=passphrase)
        problems = []
        if facts_of(cer) != (int(row["cer_size_bytes"]), row["cer_sha256"]):
            problems.append(".cer is not the bytes / sha256 kept")
        if facts_of(pvk) != (int(row["pvk_size_bytes"]), row["pvk_sha256"]):
            problems.append(".pvk is not the bytes / sha256 kept")
        if thumbprint_of(cer) != row["thumbprint"]:
            problems.append(".cer does not have the thumbprint kept")
        if problems:
            raise CertificateVaultError(f"certificate row {certificate_id} does not prove: {'; '.join(problems)}.")
        if not row.get("verified_at"):
            with self.connect() as conn:
                conn.execute("UPDATE backup_certificates SET verified_at = ? WHERE certificate_id = ? "
                             "AND verified_at IS NULL", (utc_text(), int(certificate_id)))
        return self.get(certificate_id) or row

    def mark_source_deleted(self, certificate_id: int) -> None:
        """Say the server's copy is gone - accepted only for a verified row."""
        row = self.get(certificate_id)
        if row is None or not row.get("verified_at"):
            raise CertificateVaultError(f"certificate row {certificate_id} is not verified - its source copy "
                                        "may not be deleted.")
        with self.connect() as conn:
            conn.execute("UPDATE backup_certificates SET source_deleted_at = ? WHERE certificate_id = ? "
                         "AND source_deleted_at IS NULL", (utc_text(), int(certificate_id)))

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def get(self, certificate_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM backup_certificates WHERE certificate_id = ?",
                               (int(certificate_id),)).fetchone()
        return dict(row) if row is not None else None

    def find(self, *, server_id: str, certificate_name: str, thumbprint: str = "") -> dict[str, Any] | None:
        """The row for that certificate - by thumbprint when given, else the newest verified one."""
        self.initialize()
        with self.connect() as conn:
            if thumbprint:
                row = conn.execute(
                    "SELECT * FROM backup_certificates WHERE server_id = ? AND certificate_name = ? "
                    "AND thumbprint = ?", (server_id, certificate_name, thumbprint.lower())).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM backup_certificates WHERE server_id = ? AND certificate_name = ? "
                    "AND verified_at IS NOT NULL ORDER BY certificate_id DESC LIMIT 1",
                    (server_id, certificate_name)).fetchone()
        return dict(row) if row is not None else None

    def held_thumbprints(self, *, server_id: str, certificate_name: str) -> list[str]:
        """Every verified thumbprint kept for that certificate name - what a backup need not export."""
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT thumbprint FROM backup_certificates WHERE server_id = ? AND certificate_name = ? "
                "AND verified_at IS NOT NULL ORDER BY certificate_id", (server_id, certificate_name)).fetchall()
        return [str(r["thumbprint"]) for r in rows]

    def pair(self, certificate_id: int, *, passphrase: str) -> tuple[bytes, bytes, dict[str, Any]]:
        """``(cer, pvk, row)`` decrypted, on a connection of its own."""
        row = self.get(certificate_id)
        if row is None:
            raise CertificateVaultError(f"no certificate row {certificate_id}.")
        try:
            plain = secret_text.decrypt_secret_text(json.loads(row["envelope"]), passphrase)
            return base64.b64decode(plain["cer"]), base64.b64decode(plain["pvk"]), row
        except Exception as exc:  # noqa: BLE001 - named, with the row
            raise CertificateVaultError(f"certificate row {certificate_id} cannot be decrypted: "
                                        f"{type(exc).__name__}: {exc}") from exc

    def listing(self) -> list[dict[str, Any]]:
        """Every row, without its envelope - what an operator reads."""
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM backup_certificates ORDER BY server_id, certificate_name, "
                                "certificate_id").fetchall()
        return [{k: v for k, v in dict(r).items() if k != "envelope"} for r in rows]


def _last_part(path: str) -> str:
    return re.split(r"[\\/]", str(path).rstrip("\\/"))[-1]
