-- QUERY_LATENCY_PERCENTILES: Query Store execution-weighted plan latency percentiles per interval.
--
-- Why this percentile is measured as an execution-weighted plan average:
-- Query Store maintains runtime stats per plan and interval as summary statistics: an average,
-- a standard deviation, a min, and a max. It keeps no distribution of individual executions,
-- so this collector derives the execution-weighted percentile of the plans' average durations.
-- The spread inside any single plan is not visible in Query Store; max_ms represents the true
-- observed maximum across all executions in the interval. A true per-execution latency percentile
-- requires an Extended Events session on each instance, which was declined on 2026-09-11 as
-- production DDL.
--
-- Why completed intervals in a lookback window are read:
-- Windows must not leave gaps or create misleading partial readings. Only Query Store intervals
-- that have fully ended (end_time <= SYSUTCDATETIME()) and ended within @lookback_minutes are
-- processed. Each interval is keyed by '<database> @ <interval_start as yyyy-MM-ddTHH:mmZ>' so
-- downstream readers can deduplicate intervals across consecutive collections.
--
-- Contract:
-- Status is always LOGGING; a collector records and never grades latency numbers.
-- Reads are strictly bounded: intervals within the lookback window are identified first before
-- joining runtime_stats, preventing unbounded scans of large Query Store repositories.
-- Databases where Query Store is OFF or has no intervals in the window emit no rows.
-- Availability Group secondary replicas that reject reads are skipped to avoid error 976.

SET NOCOUNT ON;

DECLARE @lookback_minutes int = 65;

IF OBJECT_ID('tempdb..#metrics') IS NOT NULL
    DROP TABLE #metrics;

CREATE TABLE #metrics
(
    metric_item  varchar(256),
    metric_value varchar(32),
    metric_unit  varchar(32),
    status       varchar(16),
    message      varchar(1000)
);

-- Skip Availability Group secondaries that reject reads.
-- Explicit COLLATE prevents collation conflicts between tempdb and instance collations.
DECLARE @ag_secondary TABLE (db_name nvarchar(128) COLLATE DATABASE_DEFAULT PRIMARY KEY);
IF OBJECT_ID('sys.fn_hadr_is_primary_replica') IS NOT NULL
    INSERT INTO @ag_secondary (db_name)
    EXEC sys.sp_executesql
        N'SELECT name COLLATE DATABASE_DEFAULT FROM sys.databases
          WHERE sys.fn_hadr_is_primary_replica(name) = 0;';

DECLARE @sql nvarchar(max);
DECLARE @db sysname;

DECLARE db_cur CURSOR LOCAL FAST_FORWARD FOR
SELECT d.name
FROM sys.databases AS d
WHERE d.database_id > 4
  AND d.state = 0
  AND d.is_read_only = 0
  AND NOT EXISTS (SELECT 1 FROM @ag_secondary AS s WHERE s.db_name = d.name COLLATE DATABASE_DEFAULT);

OPEN db_cur;
FETCH NEXT FROM db_cur INTO @db;

WHILE @@FETCH_STATUS = 0
BEGIN
    -- Started from an nvarchar(max): a concatenation of N'...' literals is typed nvarchar(4000) and
    -- is cut at 4,000 characters before it is assigned - this statement is longer, and the cut
    -- ended it mid-expression ("Incorrect syntax near '+'") on the first lab run.
    SET @sql = CAST(N'' AS nvarchar(max)) + N'
    IF EXISTS (
        SELECT 1
        FROM ' + QUOTENAME(@db) + N'.sys.database_query_store_options
        WHERE actual_state_desc IN (''READ_WRITE'', ''READ_ONLY'')
    )
    BEGIN
        ;WITH target_intervals AS (
            SELECT runtime_stats_interval_id, start_time, end_time
            FROM ' + QUOTENAME(@db) + N'.sys.query_store_runtime_stats_interval
            WHERE end_time >= DATEADD(minute, -@lookback_minutes, SYSUTCDATETIME())
              AND end_time <= SYSUTCDATETIME()
        ),
        plan_interval_stats AS (
            -- Aggregate a plan''s multiple rows in one interval (e.g. regular vs aborted execution types)
            -- weighting avg_duration by count_executions.
            SELECT
                rs.runtime_stats_interval_id,
                qsp.plan_id,
                SUM(rs.count_executions) AS plan_executions,
                (SUM(rs.avg_duration * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0)) / 1000.0 AS plan_avg_duration_ms,
                MAX(rs.max_duration) / 1000.0 AS plan_max_duration_ms
            FROM target_intervals ti
            JOIN ' + QUOTENAME(@db) + N'.sys.query_store_runtime_stats rs
                ON rs.runtime_stats_interval_id = ti.runtime_stats_interval_id
            JOIN ' + QUOTENAME(@db) + N'.sys.query_store_plan qsp
                ON qsp.plan_id = rs.plan_id
            WHERE rs.count_executions > 0
            GROUP BY rs.runtime_stats_interval_id, qsp.plan_id
        ),
        interval_totals AS (
            SELECT
                runtime_stats_interval_id,
                SUM(plan_executions) AS total_executions,
                COUNT(*) AS plan_count,
                SUM(plan_avg_duration_ms * plan_executions) / NULLIF(SUM(plan_executions), 0) AS avg_ms,
                MAX(plan_max_duration_ms) AS max_ms
            FROM plan_interval_stats
            GROUP BY runtime_stats_interval_id
        ),
        weighted_cum AS (
            SELECT
                pis.runtime_stats_interval_id,
                pis.plan_id,
                pis.plan_executions,
                pis.plan_avg_duration_ms,
                it.total_executions,
                it.plan_count,
                it.avg_ms,
                it.max_ms,
                SUM(pis.plan_executions) OVER (
                    PARTITION BY pis.runtime_stats_interval_id
                    ORDER BY pis.plan_avg_duration_ms, pis.plan_id
                    ROWS UNBOUNDED PRECEDING
                ) AS cum_executions
            FROM plan_interval_stats pis
            JOIN interval_totals it
                ON it.runtime_stats_interval_id = pis.runtime_stats_interval_id
        ),
        percentiles AS (
            SELECT
                wc.runtime_stats_interval_id,
                MAX(wc.total_executions) AS total_executions,
                MAX(wc.plan_count) AS plan_count,
                MAX(wc.avg_ms) AS avg_ms,
                MAX(wc.max_ms) AS max_ms,
                MIN(CASE WHEN wc.cum_executions >= wc.total_executions * 0.50 THEN wc.plan_avg_duration_ms END) AS p50_ms,
                MIN(CASE WHEN wc.cum_executions >= wc.total_executions * 0.95 THEN wc.plan_avg_duration_ms END) AS p95_ms,
                MIN(CASE WHEN wc.cum_executions >= wc.total_executions * 0.99 THEN wc.plan_avg_duration_ms END) AS p99_ms
            FROM weighted_cum wc
            GROUP BY wc.runtime_stats_interval_id
        )
        INSERT INTO #metrics (metric_item, metric_value, metric_unit, status, message)
        SELECT
            CAST(N''' + REPLACE(@db, '''', '''''') + N''' + '' @ '' + REPLACE(CONVERT(varchar(16), SWITCHOFFSET(ti.start_time, ''+00:00''), 120), '' '', ''T'') + ''Z'' AS varchar(256)) AS metric_item,
            CAST(CAST(p.p95_ms AS decimal(18,2)) AS varchar(32)) AS metric_value,
            CAST(''ms'' AS varchar(32)) AS metric_unit,
            CAST(''LOGGING'' AS varchar(16)) AS status,
            CAST(
                ''database='' + N''' + REPLACE(@db, '''', '''''') + N'''
                + '', interval_start_utc='' + CONVERT(varchar(19), SWITCHOFFSET(ti.start_time, ''+00:00''), 120)
                + '', interval_end_utc='' + CONVERT(varchar(19), SWITCHOFFSET(ti.end_time, ''+00:00''), 120)
                + '', executions='' + CAST(p.total_executions AS varchar(32))
                + '', plans='' + CAST(p.plan_count AS varchar(32))
                + '', p50_ms='' + CAST(CAST(p.p50_ms AS decimal(18,2)) AS varchar(32))
                + '', p95_ms='' + CAST(CAST(p.p95_ms AS decimal(18,2)) AS varchar(32))
                + '', p99_ms='' + CAST(CAST(p.p99_ms AS decimal(18,2)) AS varchar(32))
                + '', avg_ms='' + CAST(CAST(p.avg_ms AS decimal(18,2)) AS varchar(32))
                + '', max_ms='' + CAST(CAST(p.max_ms AS decimal(18,2)) AS varchar(32))
                + '', measured_as=exec_weighted_plan_average''
                AS varchar(1000)
            ) AS message
        FROM percentiles p
        JOIN target_intervals ti
            ON ti.runtime_stats_interval_id = p.runtime_stats_interval_id
        WHERE p.total_executions > 0;
    END;
    ';

    EXEC sys.sp_executesql
        @sql,
        N'@lookback_minutes int',
        @lookback_minutes = @lookback_minutes;

    FETCH NEXT FROM db_cur INTO @db;
END;

CLOSE db_cur;
DEALLOCATE db_cur;

SELECT metric_item, metric_value, metric_unit, status, message
FROM #metrics
ORDER BY metric_item;

DROP TABLE #metrics;
