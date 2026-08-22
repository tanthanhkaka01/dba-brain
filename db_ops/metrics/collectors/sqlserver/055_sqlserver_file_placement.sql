-- STORAGE_FILE_PLACEMENT: data/log/tempdb file drive locations (for data-vs-log
-- colocation and tempdb placement) plus recent backup destination drives (backup_path)
-- for the inventory disk_health block. Logging only (status always OK, no alert).
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
  AND bmf.physical_device_name IS NOT NULL;
