-- Two windows, and they are not the same thing.
--
-- @p_FromLocal is how far back the SCAN reads, and it stays at 6 hours because that is what
-- @p_AlertFrom below compares against: the cheapest plan a query has used recently is the
-- baseline for "this plan regressed", and a 30-minute scan would keep re-electing the bad plan
-- as its own best (best_logical_reads = max_logical_reads, ratio 1.00) and lose the regression.
--
-- @p_AlertFromLocal is which findings are REPORTED, and it is the collection cadence, not the
-- scan depth. This metric runs every 15 minutes; with reporting tied to the 6-hour scan, one
-- query that ran once at 14:28 was re-reported every run until 20:28 - 24 identical CRITICALs
-- for a single finished statement. Nothing about the query changed between them, and the alert
-- stream carried the same two lines all afternoon.
DECLARE @p_FromLocal      datetime = DATEADD(HOUR, -6, GETDATE());
DECLARE @p_ToLocal        datetime = GETDATE();
DECLARE @p_AlertFromLocal datetime = DATEADD(MINUTE, -30, GETDATE());

DECLARE @sql nvarchar(max);
DECLARE @db sysname;

IF OBJECT_ID('tempdb..#qs_db') IS NOT NULL DROP TABLE #qs_db;
IF OBJECT_ID('tempdb..#qs_raw') IS NOT NULL DROP TABLE #qs_raw;

CREATE TABLE #qs_db
(
    database_name sysname,
    is_query_store_on bit,
    query_store_status varchar(30)
);

-- Coverage first, scan second: every user database is recorded here with the reason it can or
-- cannot be scanned, and the scan list below is simply the rows with no reason to skip.
--
-- This table is NOT reported on any more - "which databases have Query Store off" moved to its own
-- metric, QUERY_STORE_COVERAGE (070), because it is a configuration fact while this metric runs
-- every 15 minutes. Emitting it here produced 96 identical warnings a day per database, and the
-- alert stream became mostly that one line. It stays because the scan still has to know which
-- databases it may read; without it the cursor would hit an AG secondary and fail with error 976.
IF OBJECT_ID('tempdb..#qs_cov') IS NOT NULL DROP TABLE #qs_cov;
CREATE TABLE #qs_cov
(
    database_name sysname,
    db_state      varchar(30),
    desired_on    bit,
    skip_reason   varchar(40) NULL
);

INSERT INTO #qs_cov (database_name, db_state, desired_on, skip_reason)
SELECT
    d.name,
    d.state_desc,
    d.is_query_store_on,
    CASE
        WHEN d.state_desc <> 'ONLINE' THEN 'DATABASE_NOT_ONLINE'
        WHEN d.is_query_store_on = 0 THEN 'QUERY_STORE_OFF'
        -- A non-readable AG secondary rejects query_store reads (error 976); that is expected on a
        -- secondary, not a fault, so it is skipped without being reported as a problem.
        WHEN sys.fn_hadr_is_primary_replica(d.name) = 0 THEN 'AG_SECONDARY'
        ELSE NULL
    END
FROM sys.databases AS d
WHERE d.database_id > 4;

INSERT INTO #qs_db
SELECT
    database_name,
    desired_on,
    CASE desired_on
        WHEN 1 THEN 'QUERY_STORE_ON'
        ELSE 'QUERY_STORE_OFF'
    END AS query_store_status
FROM #qs_cov
-- skip_reason already encodes every exclusion (not online, Query Store off, AG secondary), so the
-- scan list is simply the rows with no reason to skip.
WHERE skip_reason IS NULL;

CREATE TABLE #qs_raw
(
    database_name sysname,
    query_id bigint,
    plan_id bigint,
    runtime_stats_id bigint,
    last_execution_time_local datetime,
    count_executions bigint,
    avg_logical_io_reads float,
    max_logical_io_reads float,
    avg_duration_sec decimal(38, 6),
    max_duration_sec decimal(38, 6),
    avg_cpu_sec decimal(38, 6),
    max_cpu_sec decimal(38, 6),
    query_sql_text nvarchar(max)
);

DECLARE db_cur CURSOR LOCAL FAST_FORWARD FOR
SELECT database_name
FROM #qs_db;

OPEN db_cur;
FETCH NEXT FROM db_cur INTO @db;

WHILE @@FETCH_STATUS = 0
BEGIN
    SET @sql = N'
    SELECT
        N''' + REPLACE(@db, '''', '''''') + N''' AS database_name,
        qsq.query_id,
        qsp.plan_id,
        rs.runtime_stats_id,
        CONVERT(datetime,
            rs.last_execution_time AT TIME ZONE ''UTC''
            AT TIME ZONE ''SE Asia Standard Time''
        ) AS last_execution_time_local,
        rs.count_executions,
        rs.avg_logical_io_reads,
        rs.max_logical_io_reads,
        rs.avg_duration / 1000000.0 AS avg_duration_sec,
        rs.max_duration / 1000000.0 AS max_duration_sec,
        rs.avg_cpu_time / 1000000.0 AS avg_cpu_sec,
        rs.max_cpu_time / 1000000.0 AS max_cpu_sec,
        qt.query_sql_text
    FROM ' + QUOTENAME(@db) + N'.sys.query_store_query_text qt
    JOIN ' + QUOTENAME(@db) + N'.sys.query_store_query qsq
        ON qt.query_text_id = qsq.query_text_id
    JOIN ' + QUOTENAME(@db) + N'.sys.query_store_plan qsp
        ON qsq.query_id = qsp.query_id
    JOIN ' + QUOTENAME(@db) + N'.sys.query_store_runtime_stats rs
        ON qsp.plan_id = rs.plan_id
    WHERE rs.last_execution_time >= CONVERT(datetime, CAST(@from_local AS datetime) AT TIME ZONE ''SE Asia Standard Time'' AT TIME ZONE ''UTC'')
      AND rs.last_execution_time <= CONVERT(datetime, CAST(@to_local AS datetime) AT TIME ZONE ''SE Asia Standard Time'' AT TIME ZONE ''UTC'');
    ';

    INSERT INTO #qs_raw
    EXEC sp_executesql
        @sql,
        N'@from_local datetime, @to_local datetime',
        @from_local = @p_FromLocal,
        @to_local = @p_ToLocal;

    FETCH NEXT FROM db_cur INTO @db;
END;

CLOSE db_cur;
DEALLOCATE db_cur;

;WITH plan_agg AS
(
    SELECT
        database_name,
        query_id,
        plan_id,
        COUNT(*) AS runtime_stats_count,
        SUM(ISNULL(count_executions, 0)) AS executions,
        MAX(last_execution_time_local) AS last_execution_time_local,
        MAX(max_duration_sec) AS max_duration_sec,
        MAX(max_cpu_sec) AS max_cpu_sec,
        MAX(max_logical_io_reads) AS max_logical_io_reads,
        AVG(avg_duration_sec) AS avg_duration_sec,
        AVG(avg_cpu_sec) AS avg_cpu_sec,
        AVG(avg_logical_io_reads) AS avg_logical_io_reads
    FROM #qs_raw
    GROUP BY database_name, query_id, plan_id
),
query_best AS
(
    SELECT
        database_name,
        query_id,
        COUNT(*) AS plan_count,
        MIN(NULLIF(max_logical_io_reads, 0)) AS best_logical_reads,
        MIN(NULLIF(max_duration_sec, 0)) AS best_duration_sec,
        MIN(NULLIF(max_cpu_sec, 0)) AS best_cpu_sec
    FROM plan_agg
    GROUP BY database_name, query_id
),
detail AS
(
    SELECT
        p.database_name,
        p.query_id,
        p.plan_id,
        old_plan_id =
        (
            SELECT TOP 1 p2.plan_id
            FROM plan_agg p2
            WHERE p2.database_name = p.database_name
              AND p2.query_id = p.query_id
              AND p2.plan_id <> p.plan_id
            ORDER BY p2.max_logical_io_reads ASC, p2.max_duration_sec ASC
        ),
        p.runtime_stats_count,
        p.executions,
        p.last_execution_time_local,
        p.avg_duration_sec,
        p.max_duration_sec,
        p.avg_cpu_sec,
        p.max_cpu_sec,
        p.avg_logical_io_reads,
        p.max_logical_io_reads,
        q.plan_count,
        q.best_logical_reads,
        logical_read_ratio =
            CASE
                WHEN q.best_logical_reads IS NULL THEN NULL
                ELSE p.max_logical_io_reads / q.best_logical_reads
            END,
            
        issue_type =
            CASE
                -- wait / blocked
                WHEN p.max_duration_sec >= 1800
                     AND p.max_cpu_sec < 5
                     AND p.max_logical_io_reads < 100000
                THEN 'QUERY_WAIT_OR_BLOCKED'

                -- severe regression
                WHEN q.best_logical_reads IS NOT NULL
                     AND p.max_logical_io_reads >= 500000000
                     AND p.max_logical_io_reads / q.best_logical_reads >= 5
                THEN 'QUERY_PLAN_REGRESSED_READS'

                -- severe heavy query
                WHEN p.max_duration_sec >= 3600
                     AND p.max_cpu_sec >= 300
                     AND p.max_logical_io_reads >= 100000000
                THEN 'QUERY_HEAVY_CPU_AND_READS'

                -- warning heavy query
                WHEN p.max_duration_sec >= 1800
                     AND p.max_cpu_sec >= 300
                     AND p.max_logical_io_reads >= 300000000
                THEN 'QUERY_HEAVY_CPU_AND_READS'

                -- warning regression
                WHEN q.best_logical_reads IS NOT NULL
                     AND p.max_logical_io_reads >= 300000000
                     AND p.max_logical_io_reads / q.best_logical_reads >= 3
                THEN 'QUERY_PLAN_REGRESSED_READS'

                -- single metric critical
                WHEN p.max_cpu_sec >= 3600
                THEN 'QUERY_HEAVY_CPU'

                WHEN p.max_logical_io_reads >= 1000000000
                THEN 'QUERY_HEAVY_READS'

                WHEN p.max_duration_sec >= 3600
                THEN 'QUERY_LONG_DURATION_OTHER'

                -- single metric warning
                WHEN p.max_cpu_sec >= 1800
                THEN 'QUERY_HEAVY_CPU'

                WHEN p.max_logical_io_reads >= 500000000
                THEN 'QUERY_HEAVY_READS'

                WHEN p.max_duration_sec >= 1800
                THEN 'QUERY_LONG_DURATION_OTHER'

                ELSE 'OK'
            END,
        severity =
            CASE
                WHEN p.max_duration_sec >= 1800
                     AND p.max_cpu_sec < 5
                     AND p.max_logical_io_reads < 100000
                THEN CASE
                         WHEN p.max_duration_sec >= 3600 THEN 'CRITICAL'
                         ELSE 'WARNING'
                     END

                WHEN q.best_logical_reads IS NOT NULL
                     AND p.max_logical_io_reads >= 500000000
                     AND p.max_logical_io_reads / q.best_logical_reads >= 5
--                      AND q.best_logical_reads >= 100000
                THEN 'CRITICAL'

                WHEN p.max_duration_sec >= 3600
                     AND p.max_cpu_sec >= 300
                     AND p.max_logical_io_reads >= 100000000
                THEN 'CRITICAL'

                WHEN p.max_cpu_sec >= 3600
                THEN 'CRITICAL'

                WHEN p.max_logical_io_reads >= 1000000000
                THEN 'CRITICAL'

                WHEN p.max_duration_sec >= 3600
                THEN 'CRITICAL'

                WHEN p.max_duration_sec >= 1800
                     AND p.max_cpu_sec >= 300
                     AND p.max_logical_io_reads >= 300000000
                THEN 'WARNING'

                WHEN q.best_logical_reads IS NOT NULL
                     AND p.max_logical_io_reads >= 300000000
                     AND p.max_logical_io_reads / q.best_logical_reads >= 3
--                      AND q.best_logical_reads >= 100000
                THEN 'WARNING'

                WHEN p.max_cpu_sec >= 1800
                THEN 'WARNING'

                WHEN p.max_logical_io_reads >= 500000000
                THEN 'WARNING'

                WHEN p.max_duration_sec >= 1800
                THEN 'WARNING'

                ELSE 'OK'
            END
    
    FROM plan_agg p
    JOIN query_best q
        ON q.database_name = p.database_name
       AND q.query_id = p.query_id
),
issue_rows AS
(
    SELECT TOP 100
        d.*,
        query_sql_text =
        (
            SELECT TOP 1
                LEFT(REPLACE(REPLACE(r.query_sql_text, CHAR(13), ' '), CHAR(10), ' '), 1500)
            FROM #qs_raw r
            WHERE r.database_name = d.database_name
              AND r.query_id = d.query_id
              AND r.plan_id = d.plan_id
            ORDER BY r.max_duration_sec DESC, r.max_logical_io_reads DESC
        )
    FROM detail d
    WHERE d.severity IN ('WARNING', 'CRITICAL')
      -- The baseline behind d came from the full 6 hours; only the last execution decides whether
      -- this is news. A plan whose newest run predates the alert window has already been reported.
      AND d.last_execution_time_local >= @p_AlertFromLocal
    ORDER BY
        CASE d.severity WHEN 'CRITICAL' THEN 1 WHEN 'WARNING' THEN 2 ELSE 3 END,
        d.max_duration_sec DESC,
        d.max_logical_io_reads DESC
)
SELECT
    CAST(
        CASE issue_type
            WHEN 'QUERY_WAIT_OR_BLOCKED' THEN 'query_store_wait_or_blocked'
            WHEN 'QUERY_PLAN_REGRESSED_READS' THEN 'query_store_plan_regressed_reads'
            WHEN 'QUERY_HEAVY_CPU_AND_READS' THEN 'query_store_heavy_cpu_and_reads'
            WHEN 'QUERY_HEAVY_CPU' THEN 'query_store_heavy_cpu'
            WHEN 'QUERY_HEAVY_READS' THEN 'query_store_heavy_reads'
            WHEN 'QUERY_LONG_DURATION_OTHER' THEN 'query_store_long_duration_other'
            ELSE 'query_store_other'
        END
        AS varchar(256)
    ) AS metric_item,

    CAST(
        CASE issue_type
            WHEN 'QUERY_WAIT_OR_BLOCKED' THEN CAST(CAST(max_duration_sec AS decimal(18,2)) AS varchar(32))
            WHEN 'QUERY_PLAN_REGRESSED_READS' THEN CAST(CAST(logical_read_ratio AS decimal(18,2)) AS varchar(32))
            WHEN 'QUERY_HEAVY_CPU_AND_READS' THEN CAST(CAST(max_duration_sec AS decimal(18,2)) AS varchar(32))
            WHEN 'QUERY_HEAVY_CPU' THEN CAST(CAST(max_cpu_sec AS decimal(18,2)) AS varchar(32))
            WHEN 'QUERY_HEAVY_READS' THEN CAST(CAST(max_logical_io_reads AS decimal(38,0)) AS varchar(32))
            WHEN 'QUERY_LONG_DURATION_OTHER' THEN CAST(CAST(max_duration_sec AS decimal(18,2)) AS varchar(32))
            ELSE '0'
        END
        AS varchar(32)
    ) AS metric_value,

    CAST(
        CASE issue_type
            WHEN 'QUERY_WAIT_OR_BLOCKED' THEN 'duration_sec'
            WHEN 'QUERY_PLAN_REGRESSED_READS' THEN 'read_ratio'
            WHEN 'QUERY_HEAVY_CPU_AND_READS' THEN 'duration_sec'
            WHEN 'QUERY_HEAVY_CPU' THEN 'cpu_sec'
            WHEN 'QUERY_HEAVY_READS' THEN 'logical_reads'
            WHEN 'QUERY_LONG_DURATION_OTHER' THEN 'duration_sec'
            ELSE 'queries'
        END
        AS varchar(32)
    ) AS metric_unit,
    CAST(severity AS varchar(32)) AS status,
    CAST(
        'db_name=' + database_name
        + ', query_id=' + CAST(query_id AS varchar(30))
        + ', plan_id=' + CAST(plan_id AS varchar(30))
        + ', old_plan_id=' + ISNULL(CAST(old_plan_id AS varchar(30)), 'NULL')
        + ', runtime_stats_count=' + CAST(runtime_stats_count AS varchar(20))
        + ', executions=' + CAST(executions AS varchar(20))
        + ', plan_count=' + CAST(plan_count AS varchar(20))
        + ', last_execution_time=' + CONVERT(varchar(19), last_execution_time_local, 120)
        + ', issue_type=' + issue_type
        + ', avg_duration_sec=' + CAST(avg_duration_sec AS varchar(40))
        + ', max_duration_sec=' + CAST(max_duration_sec AS varchar(40))
        + ', avg_cpu_sec=' + CAST(avg_cpu_sec AS varchar(40))
        + ', max_cpu_sec=' + CAST(max_cpu_sec AS varchar(40))
        + ', avg_logical_reads=' + CAST(CAST(avg_logical_io_reads AS decimal(38,0)) AS varchar(40))
        + ', max_logical_reads=' + CAST(CAST(max_logical_io_reads AS decimal(38,0)) AS varchar(40))
        + ', best_logical_reads=' + ISNULL(CAST(CAST(best_logical_reads AS decimal(38,0)) AS varchar(40)), 'NULL')
        + ', logical_read_ratio=' + ISNULL(CAST(CAST(logical_read_ratio AS decimal(18,2)) AS varchar(40)), 'NULL')
        -- Both windows are printed because a reader who sees a 6-hour baseline next to a
        -- 30-minute alert window can tell "this just happened" from "this is what it is
        -- compared against" - which is the difference the repeated alerts hid.
        + ', alert_window=last_30_minutes'
        + ', alert_from=' + CONVERT(varchar(19), @p_AlertFromLocal, 120)
        + ', checked_window=last_6_hours'
        + ', checked_from=' + CONVERT(varchar(19), @p_FromLocal, 120)
        + ', checked_to=' + CONVERT(varchar(19), @p_ToLocal, 120)
        -- + ', query_text=' + ISNULL(query_sql_text, 'NULL')
        AS varchar(4000)
    ) AS message
FROM issue_rows

ORDER BY
    status ASC,
    metric_item ASC,
    message ASC;