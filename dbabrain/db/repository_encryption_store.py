"""The repository's age keys and every file encrypted with them (0.37.0).

Two append-only tables - nothing here deletes a row or rewrites a key:

* ``repository_keys`` - one row per key: the server_id it serves, its public half (``recipient``),
  its private half ENCRYPTED with the secret store's passphrase (PBKDF2 + Fernet, a salt per row),
  when it was made and when it stops encrypting (``encrypt_until``, fixed at creation:
  :mod:`dbabrain.lib.repository_keys`). An expired key stays for ever - every file encrypted with it
  needs it. The private half is also sealed as a file for dbabrain and the offline recovery key
  (:mod:`dbabrain.backup_restore.repository_encryption`), so a lost passphrase is not a lost key.
* ``repository_encrypted_rows`` - one row per CATALOG row whose every file is encrypted, proven and
  its plain file deleted on the repository (a base backup directory is several files);
* ``repository_encrypted_files`` - one row per file encrypted on the repository: the catalog row it
  belongs to (``file_id``), the plain file and the ``.age`` file with each one's bytes and sha256,
  the key, the age version, when it was encrypted, proven (decrypted back to the plain facts) and
  when the plain file was deleted on the repository. What the operator asked the catalog to say:
  *encrypted or not, how, with which key, where the key is*.
"""

from __future__ import annotations

import json
from typing import Any

from dbabrain.db import backend as backend_mod
from dbabrain.db.backend import StoreTarget
from dbabrain.db.store import utc_now_text
from dbabrain.lib import errors
from dbabrain.lib import repository_keys as K
from dbabrain.lib import secret_text

REPOSITORY_ENCRYPTION_SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS repository_keys
(
    key_id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    recipient TEXT NOT NULL,
    identity_envelope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    encrypt_until TEXT NOT NULL,
    lifetime_days INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    sealed_file TEXT NULL,
    proven_at TEXT NULL,
    CONSTRAINT uq_repository_keys_recipient UNIQUE (recipient)
);

CREATE INDEX IF NOT EXISTS ix_repository_keys_server ON repository_keys (server_id, encrypt_until);

CREATE TABLE IF NOT EXISTS repository_encrypted_files
(
    encrypted_file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    backup_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    repository_server_id TEXT NOT NULL,
    plain_path TEXT NOT NULL,
    plain_size_bytes INTEGER NOT NULL,
    plain_sha256 TEXT NOT NULL,
    encrypted_path TEXT NOT NULL,
    encrypted_size_bytes INTEGER NOT NULL,
    encrypted_sha256 TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    key_id INTEGER NOT NULL,
    age_version TEXT NOT NULL DEFAULT '',
    encrypted_at TEXT NOT NULL,
    proven_at TEXT NULL,
    plain_deleted_at TEXT NULL,
    CONSTRAINT uq_repository_encrypted_files_path UNIQUE (repository_server_id, encrypted_path)
);

CREATE INDEX IF NOT EXISTS ix_repository_encrypted_files_file ON repository_encrypted_files (file_id);

CREATE TABLE IF NOT EXISTS repository_encrypted_rows
(
    file_id INTEGER PRIMARY KEY,
    backup_id TEXT NOT NULL,
    repository_server_id TEXT NOT NULL,
    encrypted_files INTEGER NOT NULL,
    key_id INTEGER NOT NULL,
    completed_at TEXT NOT NULL
);
"""


class RepositoryEncryptionError(errors.OperationFailed):
    """The key ring or the record refused."""


class RepositoryEncryptionStore:
    def __init__(self, source: Any, *, key: str | None = None, password: str | None = None) -> None:
        self.target = StoreTarget.coerce(source, key=key, password=password)

    @classmethod
    def from_config(cls, config: Any, **kwargs: Any) -> "RepositoryEncryptionStore":
        return cls(StoreTarget.from_config(config, **kwargs))

    def connect(self):
        return self.target.connect()

    def initialize(self) -> None:
        if backend_mod.schema_is_ready("RepositoryEncryptionStore", self.target):
            return
        if backend_mod.remote_schema_is_current(self.target, "RepositoryEncryptionStore",
                                                REPOSITORY_ENCRYPTION_SCHEMA_VERSION):
            backend_mod.mark_schema_ready("RepositoryEncryptionStore", self.target)
            return
        self.target.prepare()
        with self.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(SCHEMA_SQL)
            backend_mod.record_schema_version(conn, "RepositoryEncryptionStore", REPOSITORY_ENCRYPTION_SCHEMA_VERSION)
        backend_mod.mark_schema_ready("RepositoryEncryptionStore", self.target)

    # ------------------------------------------------------------------ #
    # Keys
    # ------------------------------------------------------------------ #
    def add_key(self, *, server_id: str, identity: str, recipient: str, created_at: str, days: int,
                passphrase: str, created_by: str) -> dict[str, Any]:
        if not passphrase:
            raise RepositoryEncryptionError("no passphrase: a key's private half is never stored in clear.")
        if not K.IDENTITY.match(identity) or not K.RECIPIENT.match(recipient):
            raise RepositoryEncryptionError("not an age key pair.")
        until = K.time_text(K.encrypt_until(K.parse_time(created_at, "created_at"), days))
        envelope = json.dumps(secret_text.encrypt_secret_text({"identity": identity}, passphrase))
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO repository_keys (server_id, algorithm, recipient, identity_envelope, created_at, "
                "encrypt_until, lifetime_days, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (server_id, K.ALGORITHM, recipient, envelope, created_at, until, int(days), created_by))
        row = self.key_by_recipient(recipient)
        if row is None:
            raise RepositoryEncryptionError("the key was written and cannot be read back.")
        return row

    def mark_key_sealed(self, key_id: int, *, sealed_file: str) -> None:
        """The key's sealed file is written and proven - only then may it encrypt."""
        with self.connect() as conn:
            conn.execute("UPDATE repository_keys SET sealed_file = ?, proven_at = ? WHERE key_id = ? "
                         "AND proven_at IS NULL", (sealed_file, utc_now_text(), int(key_id)))

    def keys(self, *, server_id: str | None = None) -> list[dict[str, Any]]:
        """Every key (no private half), oldest first."""
        self.initialize()
        with self.connect() as conn:
            if server_id is None:
                rows = conn.execute("SELECT * FROM repository_keys ORDER BY key_id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM repository_keys WHERE server_id = ? ORDER BY key_id",
                                    (server_id,)).fetchall()
        return [{k: v for k, v in dict(r).items() if k != "identity_envelope"} for r in rows]

    def key(self, key_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM repository_keys WHERE key_id = ?", (int(key_id),)).fetchone()
        return {k: v for k, v in dict(row).items() if k != "identity_envelope"} if row is not None else None

    def key_by_recipient(self, recipient: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM repository_keys WHERE recipient = ?", (recipient,)).fetchone()
        return {k: v for k, v in dict(row).items() if k != "identity_envelope"} if row is not None else None

    def identity(self, key_id: int, *, passphrase: str) -> str:
        """The key's private half - for decrypting, which an expired key still does."""
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT identity_envelope, recipient FROM repository_keys WHERE key_id = ?",
                               (int(key_id),)).fetchone()
        if row is None:
            raise RepositoryEncryptionError(f"no repository key {key_id}.")
        try:
            identity = secret_text.decrypt_secret_text(json.loads(row["identity_envelope"]), passphrase)["identity"]
        except Exception as exc:  # noqa: BLE001 - named, with the key
            raise RepositoryEncryptionError(f"repository key {key_id} cannot be decrypted: "
                                            f"{type(exc).__name__}: {exc}") from exc
        return str(identity)

    # ------------------------------------------------------------------ #
    # Encrypted files
    # ------------------------------------------------------------------ #
    def record_encrypted(self, **values: Any) -> dict[str, Any]:
        columns = ("file_id", "backup_id", "server_id", "repository_server_id", "plain_path", "plain_size_bytes",
                   "plain_sha256", "encrypted_path", "encrypted_size_bytes", "encrypted_sha256", "key_id",
                   "age_version", "encrypted_at")
        missing = [c for c in columns if values.get(c) in (None, "") and c != "age_version"]
        if missing:
            raise RepositoryEncryptionError(f"an encrypted file is recorded with every fact; missing {', '.join(missing)}.")
        row = {c: ((values.get(c) or "") if c == "age_version" else values[c]) for c in columns}
        row["algorithm"] = K.ALGORITHM
        self.initialize()
        with self.connect() as conn:
            conn.execute(f"INSERT INTO repository_encrypted_files ({', '.join(row)}) "
                         f"VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        found = self.encrypted_file(repository_server_id=values["repository_server_id"],
                                    encrypted_path=values["encrypted_path"])
        if found is None:
            raise RepositoryEncryptionError("the encrypted file was recorded and cannot be read back.")
        return found

    def mark_proven(self, encrypted_file_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE repository_encrypted_files SET proven_at = ? WHERE encrypted_file_id = ? "
                         "AND proven_at IS NULL", (utc_now_text(), int(encrypted_file_id)))

    def mark_plain_deleted(self, encrypted_file_id: int) -> None:
        row = self.encrypted_file_by_id(encrypted_file_id)
        if row is None or not row.get("proven_at"):
            raise RepositoryEncryptionError(f"encrypted file {encrypted_file_id} is not proven - its plain file "
                                            "may not be deleted.")
        with self.connect() as conn:
            conn.execute("UPDATE repository_encrypted_files SET plain_deleted_at = ? WHERE encrypted_file_id = ? "
                         "AND plain_deleted_at IS NULL", (utc_now_text(), int(encrypted_file_id)))

    def encrypted_file(self, *, repository_server_id: str, encrypted_path: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM repository_encrypted_files WHERE repository_server_id = ? AND "
                               "encrypted_path = ?", (repository_server_id, encrypted_path)).fetchone()
        return dict(row) if row is not None else None

    def encrypted_file_by_id(self, encrypted_file_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM repository_encrypted_files WHERE encrypted_file_id = ?",
                               (int(encrypted_file_id),)).fetchone()
        return dict(row) if row is not None else None

    def mark_row_done(self, *, file_id: int, backup_id: str, repository_server_id: str, encrypted_files: int,
                      key_id: int) -> None:
        if encrypted_files < 1:
            raise RepositoryEncryptionError(f"catalog row {file_id}: no file encrypted - not done.")
        self.initialize()
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM repository_encrypted_rows WHERE file_id = ?", (int(file_id),)).fetchone():
                return
            conn.execute("INSERT INTO repository_encrypted_rows (file_id, backup_id, repository_server_id, "
                         "encrypted_files, key_id, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (int(file_id), backup_id, repository_server_id, int(encrypted_files), int(key_id),
                          utc_now_text()))

    def done_rows(self, *, backup_id: str) -> dict[int, dict[str, Any]]:
        """The catalog rows of an entry whose repository copy is wholly encrypted, by file_id."""
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM repository_encrypted_rows WHERE backup_id = ?", (backup_id,)).fetchall()
        return {int(r["file_id"]): dict(r) for r in rows}

    def proven_under(self, *, repository_server_id: str, folder: str) -> dict[str, dict[str, Any]]:
        """Every PROVEN encrypted file under ``folder`` on that repository, by its ``.age`` path - what
        a restore from the repository decrypts, and nothing else."""
        self.initialize()
        prefix = folder.rstrip("/") + "/"
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM repository_encrypted_files WHERE repository_server_id = ? AND proven_at IS NOT NULL "
                "AND substr(encrypted_path, 1, ?) = ? ORDER BY encrypted_file_id",
                (repository_server_id, len(prefix), prefix)).fetchall()
        return {str(r["encrypted_path"]): dict(r) for r in rows}

    def files_for_server(self, *, server_id: str, repository_server_id: str) -> list[dict[str, Any]]:
        """Every encrypted file of one server's backups on one repository, proven or not - what the
        repository check expects there (a pass that stopped between encrypting and proving left one)."""
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM repository_encrypted_files WHERE server_id = ? AND repository_server_id = ? "
                "ORDER BY encrypted_file_id", (server_id, repository_server_id)).fetchall()
        return [dict(r) for r in rows]

    def files_of_many(self, file_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """:meth:`files_of` for many catalog rows, one read per 500."""
        self.initialize()
        found: dict[int, list[dict[str, Any]]] = {}
        ids = sorted({int(f) for f in file_ids})
        with self.connect() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ", ".join("?" for _ in chunk)
                for row in conn.execute(f"SELECT * FROM repository_encrypted_files WHERE file_id IN ({marks}) "
                                        "ORDER BY encrypted_file_id", tuple(chunk)).fetchall():
                    found.setdefault(int(row["file_id"]), []).append(dict(row))
        return found

    def files_of(self, file_id: int) -> list[dict[str, Any]]:
        """Every encrypted file of one catalog row (a base backup directory has several)."""
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM repository_encrypted_files WHERE file_id = ? "
                                "ORDER BY encrypted_file_id", (int(file_id),)).fetchall()
        return [dict(r) for r in rows]
