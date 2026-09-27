"""The one batch that makes a backup-encryption certificate available on a SQL Server.

A backup written ``WITH ENCRYPTION`` is readable only by an instance holding the certificate it was
encrypted with, and SQL Server finds that certificate by **thumbprint**, never by name. Three paths
imported one - ``common.cli restore-key``, the SMB restore, and the script restore's shell - and
all three decided by **name**:

* ``restore-key`` and the shell dropped any certificate of that name and created theirs. On a target
  that is backed up by dbabrain itself, that name is its OWN backup certificate - ``db_ops_backup_cert``
  is the default everywhere - and the drop left its own backups unreadable on it;
* the SMB restore skipped the import when the name existed, so a target with its own certificate of
  the default name never received the source's, and the restore failed on the thumbprint.

Found 2026-09-27 restoring a production server's backups into a lab VM that has its own
``db_ops_backup_cert``.

So this batch works by thumbprint, which it reads from the ``.cer`` file itself - the SHA-1 of the
file IS the thumbprint (measured on that lab VM: the same 40 hex digits both ways) - and it never drops anything:

* the thumbprint is already there, with its private key: nothing to do;
* it is there without its private key: the key is added (``ALTER CERTIFICATE``);
* it is not there: created under the requested name, or - when that name belongs to a different
  certificate - under ``<name>_<first 8 hex digits of the thumbprint>``.

The answer is one ``PRINT`` line (:data:`MARKER`) for a ``sqlcmd`` caller and one result set for a
driver caller, both naming the certificate that holds the thumbprint now. The shell restore
(``common/restore_scripts/sqlserver/mssql_restore.sh``) cannot import this; it sends the same batch,
written out, and says so.
"""

from __future__ import annotations

MARKER = "DB_OPS_CERTIFICATE"


def _q(value: str) -> str:
    """A T-SQL string literal body: double every quote."""
    return str(value).replace("'", "''")


def import_batch(*, name: str, cer_path: str, pvk_path: str, password: str) -> str:
    """The batch. Paths are the files as the INSTANCE reads them (a container path in a container).

    ``CREATE CERTIFICATE`` and ``ALTER CERTIFICATE`` take no variables, so the name decided at run
    time goes in through ``sp_executesql``: the file paths and the password sit inside that inner
    statement's literals, which sit inside this batch's, so they are escaped twice.
    """
    name = str(name).strip()
    if not name:
        raise ValueError("name is required: the certificate's name when it has to be created.")
    if not str(cer_path).strip() or not str(pvk_path).strip():
        raise ValueError("cer_path and pvk_path are both required - SQL Server imports neither alone.")
    if not password:
        raise ValueError("password is required: it decrypts the private key.")
    with_key = f" WITH PRIVATE KEY (FILE = N'{_q(pvk_path)}', DECRYPTION BY PASSWORD = N'{_q(password)}')"
    create_tail = f" FROM FILE = N'{_q(cer_path)}'" + with_key
    return f"""SET NOCOUNT ON;
IF NOT EXISTS (SELECT 1 FROM master.sys.symmetric_keys WHERE name = N'##MS_DatabaseMasterKey##')
    CREATE MASTER KEY ENCRYPTION BY PASSWORD = N'{_q(password)}';
DECLARE @thumbprint varbinary(64) =
    (SELECT HASHBYTES('SHA1', f.BulkColumn) FROM OPENROWSET(BULK N'{_q(cer_path)}', SINGLE_BLOB) AS f);
DECLARE @present sysname =
    (SELECT TOP (1) name FROM master.sys.certificates WHERE thumbprint = @thumbprint);
DECLARE @name sysname = N'{_q(name)}';
DECLARE @imported bit = 0;
DECLARE @sql nvarchar(max);
IF @present IS NULL
BEGIN
    IF EXISTS (SELECT 1 FROM master.sys.certificates WHERE name = @name)
        SET @name = @name + N'_' + LOWER(CONVERT(varchar(8), SUBSTRING(@thumbprint, 1, 4), 2));
    SET @sql = N'USE master; CREATE CERTIFICATE ' + QUOTENAME(@name) + N'{_q(create_tail)}';
    EXEC sys.sp_executesql @sql;
    SET @imported = 1;
END
ELSE
BEGIN
    SET @name = @present;
    IF EXISTS (SELECT 1 FROM master.sys.certificates WHERE name = @present AND pvt_key_encryption_type = 'NA')
    BEGIN
        SET @sql = N'USE master; ALTER CERTIFICATE ' + QUOTENAME(@present) + N'{_q(with_key)}';
        EXEC sys.sp_executesql @sql;
        SET @imported = 1;
    END
END
DECLARE @hex varchar(64) = CONVERT(varchar(64), @thumbprint, 2);
PRINT N'{MARKER}|' + @name + N'|' + @hex + N'|' + CASE WHEN @imported = 1 THEN N'1' ELSE N'0' END;
SELECT @name AS certificate_name, @hex AS thumbprint, @imported AS imported;"""


def parse_marker(text: str) -> dict[str, object] | None:
    """``{"certificate_name", "thumbprint", "imported"}`` from a ``sqlcmd`` run's output, or None."""
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if line.startswith(MARKER + "|"):
            parts = line.split("|")
            if len(parts) >= 4:
                return {"certificate_name": parts[1], "thumbprint": parts[2].lower(),
                        "imported": parts[3].strip() == "1"}
    return None
