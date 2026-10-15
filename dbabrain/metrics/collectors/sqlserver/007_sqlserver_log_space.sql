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
    CAST(DB_NAME() COLLATE DATABASE_DEFAULT AS varchar(256)) AS metric_item,
    CAST(CAST(l.used_log_space_in_percent AS decimal(10,2)) AS varchar(32)) AS metric_value,
    ''pct'' AS metric_unit,
    CASE
        WHEN l.used_log_space_in_percent >= 95
             AND l.total_log_size_in_bytes >= 1024.0 * 1024.0 * 1024.0
        THEN ''LOGGING''

        WHEN l.used_log_space_in_percent >= 85
             AND l.total_log_size_in_bytes >= 512.0 * 1024.0 * 1024.0
        THEN ''LOGGING''

        WHEN l.used_log_space_in_percent >= 95
        THEN ''LOGGING''

        ELSE ''OK''
    END AS status,
    CONCAT(
        ''database='', DB_NAME() COLLATE DATABASE_DEFAULT,
        '', log_used_pct='', CAST(CAST(l.used_log_space_in_percent AS decimal(10,2)) AS varchar(32)),
        '', log_size_mb='', CAST(CAST(l.total_log_size_in_bytes / 1024.0 / 1024.0 AS decimal(19,2)) AS varchar(32)),
        '', used_log_mb='', CAST(CAST(l.used_log_space_in_bytes / 1024.0 / 1024.0 AS decimal(19,2)) AS varchar(32)),
        '', free_log_mb='', CAST(CAST((l.total_log_size_in_bytes - l.used_log_space_in_bytes) / 1024.0 / 1024.0 AS decimal(19,2)) AS varchar(32))
    ) AS message
FROM sys.dm_db_log_space_usage AS l;
'
FROM sys.databases AS d
WHERE d.database_id > 4
  AND d.state = 0
  AND d.is_read_only = 0
  -- Skip Availability Group secondary-replica databases: a non-readable secondary rejects
  -- queries (error 976) and would fail the whole batch. NULL = not in an AG, 1 = local primary
  -- (the primary reports the log space); 0 = secondary -> excluded.
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