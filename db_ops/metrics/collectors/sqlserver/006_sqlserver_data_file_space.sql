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

-- sys.fn_hadr_is_primary_replica does not exist before SQL Server 2012 SP1, and this variant is
-- selected from major_version 11 - which also admits 2012 RTM. A missing scalar function is a
-- BIND error, not a run-time one, so the whole file failed to compile and the metric returned
-- nothing at all on 192.0.2.12 (11.0.2100.60). Resolving the call through dynamic SQL means
-- it is only ever compiled on an instance that has it; everywhere else the set is simply empty,
-- which is the correct answer for an instance that cannot have an Availability Group replica.
--
-- Explicit COLLATE: the table variable takes the *database* collation while sys.databases.name
-- takes the *instance* collation, and on a server where they differ the join raises a collation
-- conflict instead of filtering.
DECLARE @ag_secondary TABLE (db_name nvarchar(128) COLLATE DATABASE_DEFAULT PRIMARY KEY);
IF OBJECT_ID('sys.fn_hadr_is_primary_replica') IS NOT NULL
    INSERT INTO @ag_secondary (db_name)
    EXEC sys.sp_executesql
        N'SELECT name COLLATE DATABASE_DEFAULT FROM sys.databases
          WHERE sys.fn_hadr_is_primary_replica(name) = 0;';

DECLARE @sql nvarchar(max) = N'';

SELECT @sql = @sql + N'
USE ' + QUOTENAME(d.name) + N';

INSERT INTO #metrics
(
    metric_item,
    metric_value,
    metric_unit,
    status,
    message
)
SELECT
    CAST(CONCAT(DB_NAME() COLLATE DATABASE_DEFAULT, '':'', mf.name COLLATE DATABASE_DEFAULT) AS varchar(256)) AS metric_item,

    CAST(CAST(x.used_pct AS decimal(10,2)) AS varchar(32)) AS metric_value,

    ''pct'' AS metric_unit,

    CASE
        WHEN mf.max_size = 0 THEN ''CRITICAL''

        WHEN mf.max_size > 0
             AND mf.size >= mf.max_size
        THEN ''CRITICAL''

        WHEN mf.max_size > 0
             AND ((mf.max_size - mf.size) / 128.0) < 1024
        THEN ''LOGGING''

        WHEN x.used_pct >= 95
             AND x.free_mb < 128
        THEN ''LOGGING''

        WHEN x.used_pct >= 90
             AND x.free_mb < 512
        THEN ''LOGGING''

        ELSE ''OK''
    END AS status,

    CONCAT(
        ''database='', DB_NAME() COLLATE DATABASE_DEFAULT,
        '', file='', mf.name COLLATE DATABASE_DEFAULT,
        '', used_pct='', CAST(CAST(x.used_pct AS decimal(10,2)) AS varchar(32)),
        '', size_mb='', CAST(CAST(x.size_mb AS decimal(19,2)) AS varchar(32)),
        '', free_mb='', CAST(CAST(x.free_mb AS decimal(19,2)) AS varchar(32)),
        '', growth_mb='',
            CASE
                WHEN mf.is_percent_growth = 1 THEN CONCAT(mf.growth, ''%'')
                ELSE CAST(CAST(mf.growth / 128.0 AS decimal(19,2)) AS varchar(32))
            END,
        '', is_percent_growth='', mf.is_percent_growth,
        '', max_size_mb='',
            CASE
                WHEN mf.max_size = -1 THEN ''UNLIMITED''
                WHEN mf.max_size = 0 THEN ''NO_GROWTH''
                ELSE CAST(CAST(mf.max_size / 128.0 AS decimal(19,2)) AS varchar(32))
            END
    ) AS message
FROM sys.database_files AS mf
CROSS APPLY
(
    SELECT
        CAST(FILEPROPERTY(mf.name, ''SpaceUsed'') AS decimal(19,2)) AS used_pages
) AS p
CROSS APPLY
(
    SELECT
        mf.size / 128.0 AS size_mb,
        (mf.size - p.used_pages) / 128.0 AS free_mb,
        (p.used_pages / NULLIF(mf.size, 0)) * 100 AS used_pct
) AS x
WHERE mf.type_desc = ''ROWS'';
'
FROM sys.databases AS d
WHERE d.database_id > 4
  AND d.state = 0
  AND d.is_read_only = 0
  -- Skip Availability Group secondary-replica databases: a non-readable secondary rejects
  -- queries (error 976) and would fail the whole batch. NULL = not in an AG, 1 = local primary
  -- (the primary reports these files); 0 = secondary -> excluded.
  AND NOT EXISTS (SELECT 1 FROM @ag_secondary AS s
                  WHERE s.db_name = d.name COLLATE DATABASE_DEFAULT);

EXEC sys.sp_executesql @sql;

IF EXISTS (SELECT 1 FROM #metrics)
BEGIN
    SELECT *
    FROM #metrics
    ORDER BY TRY_CAST(metric_value AS decimal(10,2)) DESC;
END
ELSE
BEGIN
    SELECT
        'server' AS metric_item,
        '0' AS metric_value,
        'pct' AS metric_unit,
        'OK' AS status,
        'No online writable user database found.' AS message;
END;