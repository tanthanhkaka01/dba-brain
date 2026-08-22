IF OBJECT_ID('tempdb..#metrics') IS NOT NULL
    DROP TABLE #metrics;

CREATE TABLE #metrics
(
    metric_item varchar(256),
    metric_value varchar(32),
    metric_unit varchar(32),
    status varchar(32),
    message varchar(max)
);

DECLARE @sql nvarchar(max);
SET @sql = N'';

SELECT @sql = @sql + N'
USE ' + QUOTENAME(d.name) + N';

INSERT INTO #metrics
SELECT
    CAST(DB_NAME() COLLATE DATABASE_DEFAULT + '':'' + mf.name COLLATE DATABASE_DEFAULT AS varchar(256)) AS metric_item,
    CAST(CAST(((CAST(FILEPROPERTY(mf.name, ''SpaceUsed'') AS decimal(19,2))) / NULLIF(mf.size, 0)) * 100 AS decimal(10,2)) AS varchar(32)) AS metric_value,
    ''pct'' AS metric_unit,
    CASE
        WHEN mf.max_size = 0 THEN ''CRITICAL''
        WHEN mf.max_size > 0 AND mf.size >= mf.max_size THEN ''CRITICAL''
        WHEN mf.max_size > 0 AND ((mf.max_size - mf.size) / 128.0) < 1024 THEN ''LOGGING''
        WHEN (((CAST(FILEPROPERTY(mf.name, ''SpaceUsed'') AS decimal(19,2))) / NULLIF(mf.size, 0)) * 100) >= 95
             AND ((mf.size - CAST(FILEPROPERTY(mf.name, ''SpaceUsed'') AS decimal(19,2))) / 128.0) < 128 THEN ''LOGGING''
        ELSE ''OK''
    END AS status,
    ''database='' + DB_NAME() COLLATE DATABASE_DEFAULT
        + '', file='' + mf.name COLLATE DATABASE_DEFAULT
        + '', used_pct='' + CAST(CAST(((CAST(FILEPROPERTY(mf.name, ''SpaceUsed'') AS decimal(19,2))) / NULLIF(mf.size, 0)) * 100 AS decimal(10,2)) AS varchar(32))
        + '', size_mb='' + CAST(CAST(mf.size / 128.0 AS decimal(19,2)) AS varchar(32))
        + '', free_mb='' + CAST(CAST((mf.size - CAST(FILEPROPERTY(mf.name, ''SpaceUsed'') AS decimal(19,2))) / 128.0 AS decimal(19,2)) AS varchar(32)) AS message
FROM sys.database_files AS mf
WHERE mf.type_desc = ''ROWS'';
'
FROM sys.databases AS d
WHERE d.database_id > 4
  AND d.state = 0
  AND d.is_read_only = 0;

EXEC sys.sp_executesql @sql;

IF EXISTS (SELECT 1 FROM #metrics)
    SELECT * FROM #metrics ORDER BY CAST(metric_value AS decimal(10,2)) DESC;
ELSE
    SELECT 'server' AS metric_item, '0' AS metric_value, 'pct' AS metric_unit, 'OK' AS status, 'No online writable user database found.' AS message;
