-- SECURITY_DATA_ENCRYPTION (SQL Server): is each database's data file encrypted at rest (TDE)?
--
-- WHY THIS EXISTS. A database's own files (.mdf / .ndf / .ldf) attach on any other instance: whoever
-- gets onto the host as an administrator can copy them and open them on their own machine. The
-- backups were protected first (encrypted, kept on the repository); this says the same about the
-- data files, per database, read where the engine says it - sys.dm_database_encryption_keys.
--
-- One row per user database (database_id > 4, no snapshots). master, model and msdb cannot be
-- encrypted by TDE and tempdb follows the others, so listing them would be four rows that can
-- never change.
--
-- STATUS IS ALWAYS OK, DELIBERATELY. This is a reading, not a grade: with TDE off everywhere, a
-- WARNING here would be one per database, on every collection, for ever. The page names a
-- database without encryption as a finding, and the metric's `change_alert` raises an event when
-- a database's state changes (encryption turned off) - the one moment somebody has to look.
--
-- metric_value is one of: encrypted | not encrypted | encrypting | decrypting | key change |
-- protection change. The message carries what proves it: the DMV's state number, the algorithm,
-- the certificate or asymmetric key protecting the database key and its thumbprint (the report
-- holds that thumbprint to dbabrain's certificate vault: a TDE database restores nowhere without
-- that certificate), and when the certificate's private key was last backed up.
--
-- 2008 compatible on purpose (one variant covers every version that has the DMV): no CONCAT, no
-- IIF, and not dm_database_encryption_keys.encryptor_type, which is 2012+ - the encryptor's kind
-- is read from which catalog its thumbprint is found in.
SET NOCOUNT ON;

SELECT
    CAST(d.name COLLATE DATABASE_DEFAULT AS varchar(400))        AS metric_item,
    CAST(CASE
            WHEN k.database_id IS NULL OR k.encryption_state IN (0, 1) THEN 'not encrypted'
            WHEN k.encryption_state = 3 THEN 'encrypted'
            WHEN k.encryption_state = 2 THEN 'encrypting'
            WHEN k.encryption_state = 4 THEN 'key change'
            WHEN k.encryption_state = 5 THEN 'decrypting'
            WHEN k.encryption_state = 6 THEN 'protection change'
            ELSE 'state ' + CAST(k.encryption_state AS varchar(12))
         END AS varchar(64))                                     AS metric_value,
    CAST('tde' AS varchar(32))                                   AS metric_unit,
    CAST('OK' AS varchar(16))                                    AS status,
    CAST(LEFT(
          'engine=sqlserver'
        + ', state=' + ISNULL(CAST(k.encryption_state AS varchar(12)), 'none')
        + ', database_state=' + d.state_desc COLLATE DATABASE_DEFAULT
        + CASE
            WHEN k.database_id IS NULL THEN ''
            ELSE ', algorithm=' + ISNULL(k.key_algorithm COLLATE DATABASE_DEFAULT, '-')
               + ISNULL('_' + CAST(k.key_length AS varchar(12)), '')
               + ', percent_complete=' + CAST(CAST(k.percent_complete AS decimal(5, 1)) AS varchar(12))
               + ', encryptor_type=' + CASE WHEN c.certificate_id IS NOT NULL THEN 'certificate'
                                            WHEN a.asymmetric_key_id IS NOT NULL THEN 'asymmetric key'
                                            ELSE 'unknown' END
               + ', encryptor=' + ISNULL(c.name COLLATE DATABASE_DEFAULT,
                                         ISNULL(a.name COLLATE DATABASE_DEFAULT, '-'))
               + ', thumbprint=' + ISNULL(CONVERT(varchar(100), k.encryptor_thumbprint, 2), '-')
               + ', certificate_expires=' + ISNULL(CONVERT(varchar(10), c.expiry_date, 120), '-')
               + ', private_key_backed_up=' + ISNULL(CONVERT(varchar(10), c.pvt_key_last_backup_date, 120), 'never')
          END,
        1000) AS varchar(1000))                                  AS message
FROM sys.databases AS d
LEFT JOIN sys.dm_database_encryption_keys AS k
       ON k.database_id = d.database_id
LEFT JOIN master.sys.certificates AS c
       ON c.thumbprint = k.encryptor_thumbprint
LEFT JOIN master.sys.asymmetric_keys AS a
       ON a.thumbprint = k.encryptor_thumbprint
WHERE d.database_id > 4
  AND d.source_database_id IS NULL
ORDER BY d.name;
