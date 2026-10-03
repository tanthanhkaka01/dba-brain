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

**Nothing is dropped.** Until 0.24.1 this dropped any certificate of the requested name and created
its own, and ``db_ops_backup_cert`` is the default name everywhere: on a target that dbabrain also
backs up, that was the target's own backup certificate. The batch is
:func:`db_ops.lib.sqlserver_certificate.import_batch`, the same one the SMB restore sends - it
finds the certificate by thumbprint and takes another name when the requested one is taken.

It runs one of two ways, as ``restore-full`` does: over a driver to ``target`` (host and port as
this machine reaches them), or through ``sqlcmd`` where the SQL Server is (``sqlcmd``, the
``run-sqlcmd`` request without its ``sql``) - for an instance this machine cannot reach directly.
"""

from __future__ import annotations

from db_ops.lib import errors
from typing import Any

from db_ops.lib import sqlserver_certificate

DEFAULT_CERT_NAME = "db_ops_backup_cert"


class RestoreKeyError(errors.RequestError):
    """The certificate cannot be imported."""


def build_statements(request: dict[str, Any]) -> list[str]:
    """The batch that makes the certificate available. Pure - nothing is executed."""
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
    return [sqlserver_certificate.import_batch(name=name, cer_path=cer, pvk_path=pvk, password=password)]


def import_key(request: dict[str, Any]) -> dict[str, Any]:
    """Import the backup certificate onto the target instance."""
    name = str(request.get("certificate_name") or DEFAULT_CERT_NAME).strip()
    statements = build_statements(request)
    if request.get("dry_run"):
        return {"certificate_name": name, "statements": statements, "dry_run": True, "ok": True}
    if isinstance(request.get("sqlcmd"), dict):
        return _through_sqlcmd(dict(request["sqlcmd"]), statements[0])
    target = request.get("target") or {}
    if not str(target.get("host") or "").strip():
        raise RestoreKeyError('target.host is required (or "sqlcmd": run it where the SQL Server is).')

    from db_ops.common import sql_run
    from db_ops.common.db_connect import connect_engine

    connection = connect_engine(
        db_type="sqlserver", host=str(target["host"]), port=int(target.get("port") or 1433),
        database="master", username=str(target.get("username") or ""),
        password=str(target.get("password") or ""), autocommit=True,
        statement_timeout_seconds=0,
    )
    try:
        # The batch's one result set comes after the statements that return none; `query_rows`
        # reads past those on either driver (rules R11).
        rows = sql_run.query_rows(connection.cursor(), statements[0])
    finally:
        connection.close()
    if not rows:
        raise RestoreKeyError("the import batch ran but answered nothing - the certificate's state is unknown.")
    row = rows[0]
    return {"certificate_name": str(row["certificate_name"]),
            "thumbprint": str(row["thumbprint"] or "").lower(),
            "imported": bool(row["imported"]), "ok": True}


def _through_sqlcmd(sqlcmd: dict[str, Any], batch: str) -> dict[str, Any]:
    from db_ops.common import sqlcmd_run

    answer = sqlcmd_run.run_sqlcmd({**sqlcmd, "sql": batch})
    if answer.get("timed_out") or answer.get("exit_code") != 0:
        detail = (str(answer.get("stdout") or "") + "\n" + str(answer.get("stderr") or "")).strip()
        raise RestoreKeyError(f"the import batch failed (exit {answer.get('exit_code')}): {detail[-800:]}")
    found = sqlserver_certificate.parse_marker(answer.get("stdout") or "")
    if found is None:
        raise RestoreKeyError("the import batch ran but printed no result line - the certificate's state is unknown.")
    return {**found, "ok": True}
