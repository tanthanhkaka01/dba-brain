WITH last_backup AS
(
    SELECT
        CAST(bs.database_name COLLATE DATABASE_DEFAULT AS varchar(256)) AS database_name,
        bs.type,
        bs.backup_start_date,
        bs.backup_finish_date,
        bs.backup_size,

        bs.compressed_backup_size,

        CAST(bmf.physical_device_name COLLATE DATABASE_DEFAULT AS varchar(1024)) AS physical_device_name,

        ROW_NUMBER() OVER
        (
            PARTITION BY bs.database_name, bs.type
            ORDER BY bs.backup_finish_date DESC
        ) AS rn
    FROM msdb.dbo.backupset bs
    LEFT JOIN msdb.dbo.backupmediafamily bmf
        ON bs.media_set_id = bmf.media_set_id
    WHERE bs.type IN ('D', 'I', 'L')
),
db_list AS
(
    SELECT
        CAST(d.name COLLATE DATABASE_DEFAULT AS varchar(256)) AS database_name,
        CAST(d.recovery_model_desc COLLATE DATABASE_DEFAULT AS varchar(64)) AS recovery_model_desc
    FROM sys.databases d
    WHERE d.database_id > 4
      AND d.state_desc = 'ONLINE'
)
SELECT
    CAST(
          d.database_name
        + ' / '
        + CASE lb.type
            WHEN 'D' THEN 'FULL'
            WHEN 'I' THEN 'DIFF'
            WHEN 'L' THEN 'LOG'
            ELSE 'UNKNOWN'
          END
        AS varchar(256)
    ) AS metric_item,

    CAST(
        ISNULL(DATEDIFF(hour, lb.backup_finish_date, GETDATE()), -1)
        AS varchar(32)
    ) AS metric_value,

    CAST('hours_since_last_backup' AS varchar(32)) AS metric_unit,

    CASE
        WHEN lb.backup_finish_date IS NULL THEN 'OK'
        ELSE 'OK'
    END AS status,

    CAST(
          'database=' + d.database_name
        + ', recovery_model=' + d.recovery_model_desc
        + ', backup_type='
            + CASE lb.type
                WHEN 'D' THEN 'FULL'
                WHEN 'I' THEN 'DIFF'
                WHEN 'L' THEN 'LOG'
                ELSE 'UNKNOWN'
              END
        + ', backup_finish_date='
            + ISNULL(CONVERT(varchar(19), lb.backup_finish_date, 120), 'NULL')
        + ', backup_size_mb='
            + ISNULL(
                CAST(CAST(lb.backup_size / 1024.0 / 1024.0 AS decimal(19,2)) AS varchar(32)),
                'NULL'
              )
        + ', compressed_backup_size_mb='
            + ISNULL(
                CAST(CAST(lb.compressed_backup_size / 1024.0 / 1024.0 AS decimal(19,2)) AS varchar(32)),
                'NULL'
              )
        + ', file=' + ISNULL(lb.physical_device_name, 'NULL')
        AS varchar(4000)
    ) AS message
FROM db_list d
LEFT JOIN last_backup lb
    ON lb.database_name = d.database_name
   AND lb.rn = 1
ORDER BY d.database_name, lb.type;
