IF EXISTS
(
    SELECT 1
    FROM msdb.dbo.suspect_pages sp
    WHERE sp.last_update_date >= DATEADD(day, -30, GETDATE())
)
BEGIN
    SELECT
        CAST(
              DB_NAME(sp.database_id) COLLATE DATABASE_DEFAULT
            + ' / file_id=' + CAST(sp.file_id AS varchar(32))
            + ' / page_id=' + CAST(sp.page_id AS varchar(32))
            AS varchar(256)
        ) AS metric_item,

        CAST(sp.event_type AS varchar(32)) AS metric_value,
        CAST('event_type' AS varchar(32)) AS metric_unit,

        CASE
            WHEN sp.event_type IN (1, 2, 3) THEN 'CRITICAL'
            WHEN sp.event_type IN (4, 5, 7) THEN 'WARNING'
            ELSE 'WARNING'
        END AS status,

        CAST(
              'database=' + DB_NAME(sp.database_id) COLLATE DATABASE_DEFAULT
            + ', file_id=' + CAST(sp.file_id AS varchar(32))
            + ', page_id=' + CAST(sp.page_id AS varchar(32))
            + ', event_type=' + CAST(sp.event_type AS varchar(32))
            + ', error_count=' + CAST(sp.error_count AS varchar(32))
            + ', last_update_date=' + CONVERT(varchar(19), sp.last_update_date, 120)
            AS varchar(4000)
        ) AS message
    FROM msdb.dbo.suspect_pages sp
    WHERE sp.last_update_date >= DATEADD(day, -30, GETDATE());
END
ELSE
BEGIN
    SELECT
        CAST('suspect_pages' AS varchar(256)) AS metric_item,
        CAST('0' AS varchar(32)) AS metric_value,
        CAST('pages' AS varchar(32)) AS metric_unit,
        CAST('OK' AS varchar(32)) AS status,
        CAST('No suspect pages found in last 30 days' AS varchar(4000)) AS message;
END;