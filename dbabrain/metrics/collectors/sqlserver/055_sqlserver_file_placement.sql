-- STORAGE_FILE_PLACEMENT: data/log/tempdb file drive locations (for data-vs-log
-- colocation and tempdb placement) plus recent backup destination drives (backup_path)
-- for the inventory disk_health block. Those rows are logging only (status always OK).
--
-- One graded row per database with a backup in the last two days (0.35.0, 1.239 - the plan of
-- 2026-10-30, ransomware and backups kept beside the data; R-1 answered yes):
--   CRITICAL  a backup on the same drive as one of that database's data or log files - the drive
--             failing, or being encrypted, takes the data and its backup together;
--   WARNING   every backup on the database host - a different drive survives a failed disk, not
--             ransomware, which encrypts every drive the host has;
--   OK        a backup off the host (a UNC share or a URL).
-- A Linux path cannot be compared by drive letter here: on the host only, it is WARNING and says
-- that the mount points were not compared. SQL Server 2008 R2 has no STRING_AGG: one example path.
SELECT
    DB_NAME(mf.database_id) COLLATE DATABASE_DEFAULT + ' | ' + mf.type_desc COLLATE DATABASE_DEFAULT AS metric_item,
    UPPER(LEFT(mf.physical_name, 3)) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    CAST(LEFT(mf.physical_name, 256) AS varchar(256)) AS message
FROM sys.master_files AS mf
UNION ALL
SELECT DISTINCT
    'BACKUP | ' + bs.database_name COLLATE DATABASE_DEFAULT AS metric_item,
    UPPER(LEFT(bmf.physical_device_name, 3)) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    CAST(LEFT(bmf.physical_device_name, 256) AS varchar(256)) AS message
FROM msdb.dbo.backupmediafamily AS bmf
JOIN msdb.dbo.backupset AS bs ON bs.media_set_id = bmf.media_set_id
WHERE bs.backup_finish_date >= DATEADD(DAY, -2, GETDATE())
  AND bmf.physical_device_name IS NOT NULL
UNION ALL
SELECT
    'PLACEMENT | ' + p.db AS metric_item,
    CASE WHEN p.same_drive = 1 THEN 'same drive'
         WHEN p.off_host = 0 THEN 'same host'
         ELSE 'off host' END AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CAST(CASE WHEN p.same_drive = 1 THEN 'CRITICAL'
              WHEN p.off_host = 0 THEN 'WARNING'
              ELSE 'OK' END AS varchar(16)) AS status,
    CAST(LEFT(
        'same_drive_as_data=' + CAST(p.same_drive AS varchar(1))
        + ', off_host=' + CAST(p.off_host AS varchar(1))
        + CASE WHEN p.posix = 1 AND p.off_host = 0 THEN ', mount_points_compared=0' ELSE '' END
        + ', example=' + p.example, 256) AS varchar(256)) AS message
FROM (
    SELECT
        b.db,
        MAX(b.same_drive) AS same_drive,
        MAX(CASE WHEN b.dev LIKE '\\%' OR b.dev LIKE 'http%' THEN 1 ELSE 0 END) AS off_host,
        MAX(CASE WHEN b.dev LIKE '/%' THEN 1 ELSE 0 END) AS posix,
        MAX(b.dev) AS example
    FROM (
        -- Compared per path here: an aggregate cannot hold a subquery (Msg 130).
        SELECT d.db, d.dev,
               CASE WHEN d.dev LIKE '_:\%' AND EXISTS (
                        SELECT 1 FROM sys.master_files AS f
                        WHERE DB_NAME(f.database_id) COLLATE DATABASE_DEFAULT = d.db
                          AND UPPER(LEFT(f.physical_name, 3)) COLLATE DATABASE_DEFAULT = UPPER(LEFT(d.dev, 3)))
                    THEN 1 ELSE 0 END AS same_drive
        FROM (
            SELECT DISTINCT
                bs.database_name COLLATE DATABASE_DEFAULT AS db,
                CAST(bmf.physical_device_name AS nvarchar(260)) COLLATE DATABASE_DEFAULT AS dev
            FROM msdb.dbo.backupmediafamily AS bmf
            JOIN msdb.dbo.backupset AS bs ON bs.media_set_id = bmf.media_set_id
            WHERE bs.backup_finish_date >= DATEADD(DAY, -2, GETDATE())
              AND bmf.physical_device_name IS NOT NULL
              AND bmf.device_type IN (2, 9)      -- disk and URL; a tape or a virtual device says nothing here
        ) AS d
    ) AS b
    GROUP BY b.db
) AS p;
