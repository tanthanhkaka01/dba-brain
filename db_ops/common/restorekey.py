"""Importing the certificate an encrypted backup set can only be read with.

A backup written ``WITH ENCRYPTION (... SERVER CERTIFICATE = ...)`` is readable **only** by an
instance holding that certificate. The backup job exports the pair beside the backups
(``<backup_dir>/_cert/<name>.cer`` + ``.pvk``) for exactly this reason: an encrypted backup that
can be restored only on the instance that wrote it is not a backup.

Without this step ``restore-full`` fails with SQL Server's own message about a missing certificate
thumbprint - true, but it names a hex string rather than the file sitting next to the backup. This
command is what turns that into an action.

**The private key is decrypted with the same passphrase the backup was encrypted with**, so the
caller passes it in like every other credential here; nothing is looked up.
"""

from __future__ import annotations

from typing import Any

DEFAULT_CERT_NAME = "db_ops_backup_cert"


class RestoreKeyError(ValueError):
    """The certificate cannot be imported."""


def _q(value: str) -> str:
    """A T-SQL string literal body: double every quote."""
    return str(value).replace("'", "''")


def _name(value: str) -> str:
    return "[" + str(value).replace("]", "]]") + "]"


def build_statements(request: dict[str, Any]) -> list[str]:
    """The statements that make the certificate available. Pure - nothing is executed."""
    name = str(request.get("certificate_name") or DEFAULT_CERT_NAME).strip()
    cer = str(request.get("cer_path") or "").strip()
    pvk = str(request.get("pvk_path") or "").strip()
    password = str(request.get("password") or "")

    if not cer or not pvk:
        raise RestoreKeyError(
            "cer_path and pvk_path are both required - the certificate is useless for a restore "
            "without its private key, and SQL Server will not import one alone."
        )
    if not password:
        raise RestoreKeyError(
            "password is required: it decrypts the private key, and is the same passphrase the "
            "backup was encrypted with."
        )

    return [
        # A master key must exist before a certificate with a private key can be created, and a
        # freshly built instance has none. Guarded, because creating a second one fails.
        "IF NOT EXISTS (SELECT 1 FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##')\n"
        f"    CREATE MASTER KEY ENCRYPTION BY PASSWORD = '{_q(password)}';",
        # Dropped and recreated rather than left alone: a certificate with the right *name* but the
        # wrong thumbprint reads as present and fails the restore, which is a worse place to find
        # out than here.
        f"IF EXISTS (SELECT 1 FROM sys.certificates WHERE name = '{_q(name)}')\n"
        f"    DROP CERTIFICATE {_name(name)};",
        f"CREATE CERTIFICATE {_name(name)}\n"
        f"    FROM FILE = '{_q(cer)}'\n"
        f"    WITH PRIVATE KEY (\n"
        f"        FILE = '{_q(pvk)}',\n"
        f"        DECRYPTION BY PASSWORD = '{_q(password)}'\n"
        f"    );",
    ]


def import_key(request: dict[str, Any]) -> dict[str, Any]:
    """Import the backup certificate onto the target instance."""
    name = str(request.get("certificate_name") or DEFAULT_CERT_NAME).strip()
    statements = build_statements(request)
    if request.get("dry_run"):
        return {"certificate_name": name, "statements": statements, "dry_run": True, "ok": True}

    target = request.get("target") or {}
    if not str(target.get("host") or "").strip():
        raise RestoreKeyError("target.host is required.")

    from db_ops.common.db_connect import connect_engine

    connection = connect_engine(
        db_type="sqlserver", host=str(target["host"]), port=int(target.get("port") or 1433),
        database="master", username=str(target.get("username") or ""),
        password=str(target.get("password") or ""), autocommit=True,
        statement_timeout_seconds=0,
    )
    try:
        cursor = connection.cursor()
        for statement in statements:
            cursor.execute(statement)
            while cursor.nextset():
                pass
        # A literal, not a `?` placeholder: SQL Server is reached through pyodbc when the
        # local ODBC stack can negotiate TLS with it and through pymssql when it cannot, and
        # the pymssql adapter takes the statement alone - `execute() takes 2 positional
        # arguments but 3 were given` on the first target that fell back. The value is ours,
        # and it is escaped.
        cursor.execute("SELECT thumbprint FROM sys.certificates WHERE name = "
                       "N'" + _q(name) + "'")
        row = cursor.fetchone()
        thumbprint = row[0].hex() if row and row[0] is not None else ""
    finally:
        connection.close()

    return {"certificate_name": name, "thumbprint": thumbprint, "imported": True, "ok": True}
