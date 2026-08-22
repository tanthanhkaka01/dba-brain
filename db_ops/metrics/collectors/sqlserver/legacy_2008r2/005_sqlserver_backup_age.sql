WITH latest_backup AS
(
    SELECT
        database_name COLLATE DATABASE_DEFAULT AS database_name,
        MAX(backup_finish_date) AS last_backup_finish_date
    FROM msdb.dbo.backupset
    WHERE type IN ('D', 'I')
    GROUP BY database_name COLLATE DATABASE_DEFAULT
)
SELECT
    CAST(d.name COLLATE DATABASE_DEFAULT AS varchar(256)) AS metric_item,
    CASE
        WHEN lb.last_backup_finish_date IS NULL THEN NULL
        ELSE CAST(DATEDIFF(hour, lb.last_backup_finish_date, GETDATE()) AS varchar(32))
    END AS metric_value,
    CAST('hours' AS varchar(32)) AS metric_unit,
    CASE
        WHEN lb.last_backup_finish_date IS NULL THEN 'WARNING'
        WHEN DATEDIFF(hour, lb.last_backup_finish_date, GETDATE()) >= 48 THEN 'CRITICAL'
        WHEN DATEDIFF(hour, lb.last_backup_finish_date, GETDATE()) >= 24 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    CASE
        WHEN lb.last_backup_finish_date IS NULL THEN
            'No full or differential backup found for database ' + d.name COLLATE DATABASE_DEFAULT + '.'
        ELSE
            'Last full/differential backup '
                + CAST(DATEDIFF(hour, lb.last_backup_finish_date, GETDATE()) AS varchar(32))
                + ' hours ago for database ' + d.name COLLATE DATABASE_DEFAULT + '.'
    END AS message
FROM sys.databases AS d
LEFT JOIN latest_backup AS lb
    ON lb.database_name = d.name COLLATE DATABASE_DEFAULT
WHERE d.database_id > 4
  AND d.state = 0
  AND d.is_read_only = 0
ORDER BY d.name COLLATE DATABASE_DEFAULT;
