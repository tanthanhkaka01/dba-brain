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

-- A third window, used by ONE finding only (QUERY_PLAN_REGRESSED_FREQUENT, below): how far back
-- the cheapest plan of a query is looked for. Six hours is not enough there - a plan flip that
-- survives the night has no good plan left inside the scan by morning, the bad plan becomes its
-- own baseline and the finding goes quiet while the query is still slow. It is read for the few
-- candidate queries only, so its depth costs almost nothing.
DECLARE @p_BaselineFromLocal datetime = DATEADD(DAY, -7, GETDATE());

-- Query Store keeps its times in UTC. The offset is taken from the server's own clock instead of a
-- named time zone: a hard-coded zone silently shifts every window on a server that lives elsewhere.
-- One reading, not the difference of two: GETUTCDATE() and GETDATE() are two calls, and when a
-- minute turns between them DATEDIFF counts it, and every window moves by a minute (0.27.0).
DECLARE @p_UtcOffsetMin    int      = DATEPART(TZOFFSET, SYSDATETIMEOFFSET());
DECLARE @p_FromUtc         datetime = DATEADD(MINUTE, -@p_UtcOffsetMin, @p_FromLocal);
DECLARE @p_ToUtc           datetime = DATEADD(MINUTE, -@p_UtcOffsetMin, @p_ToLocal);
DECLARE @p_BaselineFromUtc datetime = DATEADD(MINUTE, -@p_UtcOffsetMin, @p_BaselineFromLocal);

-- QUERY_PLAN_REGRESSED_FREQUENT: a SMALL query on a worse plan, executed often. Every other
-- finding in this file is about one execution being huge (>= 300 M reads, >= 1,800 s); none of
-- them can see a statement that went from 25 ms to 1,300 ms and ran 2,000 times in the hour,
-- although that is 43 minutes of CPU and made every caller ten times slower. It needs all three:
-- the plan now running costs several times the cheapest plan the same query has used, it has run
-- often enough for that average to mean something, and the CPU it burned recently is material.
-- The CPU leg is the QUERY's: the recent CPU of all its bad plans together (see query_bad below).
DECLARE @p_FreqMinExecutions   int   = 20;    -- per plan, recent and baseline alike
DECLARE @p_FreqWarnCpuRatio    float = 5;     -- recent avg CPU / cheapest other plan's avg CPU
DECLARE @p_FreqWarnTotalCpuSec float = 300;   -- CPU burned by the query's bad plans, recent rows
DECLARE @p_FreqCritCpuRatio    float = 10;
DECLARE @p_FreqCritTotalCpuSec float = 1200;

DECLARE @sql nvarchar(max);
DECLARE @db sysname;

IF OBJECT_ID('tempdb..#qs_db') IS NOT NULL DROP TABLE #qs_db;
IF OBJECT_ID('tempdb..#qs_raw') IS NOT NULL DROP TABLE #qs_raw;
IF OBJECT_ID('tempdb..#qs_cand') IS NOT NULL DROP TABLE #qs_cand;
IF OBJECT_ID('tempdb..#qs_base') IS NOT NULL DROP TABLE #qs_base;

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
    max_cpu_sec decimal(38, 6)
);

-- Queries worth a baseline lookup, and the per-plan baseline read for them.
CREATE TABLE #qs_cand
(
    database_name sysname,
    query_id bigint
);

CREATE TABLE #qs_base
(
    database_name sysname,
    query_id bigint,
    plan_id bigint,
    executions bigint,
    avg_cpu_sec float
);

DECLARE db_cur CURSOR LOCAL FAST_FORWARD FOR
SELECT database_name
FROM #qs_db;

OPEN db_cur;
FETCH NEXT FROM db_cur INTO @db;

WHILE @@FETCH_STATUS = 0
BEGIN
    -- The query text is deliberately not read: it used to be copied into #qs_raw once per
    -- runtime-stats row (nvarchar(max), thousands of rows) and was never part of the message.
    -- query_store_plan already carries query_id, so two catalog views are enough.
    SET @sql = N'
    SELECT
        N''' + REPLACE(@db, '''', '''''') + N''' AS database_name,
        qsp.query_id,
        qsp.plan_id,
        rs.runtime_stats_id,
        DATEADD(MINUTE, @utc_offset_min,
            CONVERT(datetime, SWITCHOFFSET(rs.last_execution_time, ''+00:00''))
        ) AS last_execution_time_local,
        rs.count_executions,
        rs.avg_logical_io_reads,
        rs.max_logical_io_reads,
        rs.avg_duration / 1000000.0 AS avg_duration_sec,
        rs.max_duration / 1000000.0 AS max_duration_sec,
        rs.avg_cpu_time / 1000000.0 AS avg_cpu_sec,
        rs.max_cpu_time / 1000000.0 AS max_cpu_sec
    FROM ' + QUOTENAME(@db) + N'.sys.query_store_plan qsp
    JOIN ' + QUOTENAME(@db) + N'.sys.query_store_runtime_stats rs
        ON qsp.plan_id = rs.plan_id
    WHERE rs.last_execution_time >= @from_utc
      AND rs.last_execution_time <= @to_utc;
    ';

    INSERT INTO #qs_raw
    EXEC sp_executesql
        @sql,
        N'@from_utc datetime, @to_utc datetime, @utc_offset_min int',
        @from_utc = @p_FromUtc,
        @to_utc = @p_ToUtc,
        @utc_offset_min = @p_UtcOffsetMin;

    FETCH NEXT FROM db_cur INTO @db;
END;

CLOSE db_cur;
DEALLOCATE db_cur;

-- Baseline for QUERY_PLAN_REGRESSED_FREQUENT. Only a query whose plans burned a material amount
-- of CPU in the rows touched inside the alert window is a candidate, so this second read is a
-- handful of query_ids per database and usually none at all. The CPU is the query's, summed over
-- its plans that ran often enough to judge - the same leg query_bad applies below, so a query whose
-- bad plans are each under the threshold still gets its baseline read.
INSERT INTO #qs_cand (database_name, query_id)
SELECT r.database_name, r.query_id
FROM
(
    SELECT database_name, query_id, plan_id,
           SUM(avg_cpu_sec * ISNULL(count_executions, 0)) AS recent_total_cpu_sec
    FROM #qs_raw
    WHERE last_execution_time_local >= @p_AlertFromLocal
    GROUP BY database_name, query_id, plan_id
    HAVING SUM(ISNULL(count_executions, 0)) >= @p_FreqMinExecutions
) AS r
GROUP BY r.database_name, r.query_id
HAVING SUM(r.recent_total_cpu_sec) >= @p_FreqWarnTotalCpuSec;

DECLARE base_cur CURSOR LOCAL FAST_FORWARD FOR
SELECT DISTINCT database_name
FROM #qs_cand;

OPEN base_cur;
FETCH NEXT FROM base_cur INTO @db;

WHILE @@FETCH_STATUS = 0
BEGIN
    SET @sql = N'
    SELECT
        @db_name AS database_name,
        qsp.query_id,
        qsp.plan_id,
        SUM(rs.count_executions) AS executions,
        SUM(rs.avg_cpu_time * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000000.0 AS avg_cpu_sec
    FROM ' + QUOTENAME(@db) + N'.sys.query_store_plan qsp
    JOIN ' + QUOTENAME(@db) + N'.sys.query_store_runtime_stats rs
        ON qsp.plan_id = rs.plan_id
    WHERE qsp.query_id IN (SELECT c.query_id FROM #qs_cand c WHERE c.database_name = @db_name)
      AND rs.last_execution_time >= @base_from_utc
    GROUP BY qsp.query_id, qsp.plan_id;
    ';

    INSERT INTO #qs_base
    EXEC sp_executesql
        @sql,
        N'@db_name sysname, @base_from_utc datetime',
        @db_name = @db,
        @base_from_utc = @p_BaselineFromUtc;

    FETCH NEXT FROM base_cur INTO @db;
END;

CLOSE base_cur;
DEALLOCATE base_cur;

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
        -- Weighted by executions. A plain AVG over the interval rows gives an hour with one
        -- execution the same say as an hour with ten thousand.
        SUM(avg_duration_sec * ISNULL(count_executions, 0)) / NULLIF(SUM(ISNULL(count_executions, 0)), 0) AS avg_duration_sec,
        SUM(avg_cpu_sec * ISNULL(count_executions, 0)) / NULLIF(SUM(ISNULL(count_executions, 0)), 0) AS avg_cpu_sec,
        SUM(avg_logical_io_reads * ISNULL(count_executions, 0)) / NULLIF(SUM(ISNULL(count_executions, 0)), 0) AS avg_logical_io_reads
    FROM #qs_raw
    GROUP BY database_name, query_id, plan_id
),
-- What the plan did in the rows touched INSIDE the alert window. The findings below are judged on
-- these numbers, not on the six-hour maxima: judged on the six hours, a plan that had one heavy
-- execution at 09:00 and has run normally ever since is re-reported every 15 minutes until 15:00,
-- because its newest execution keeps landing inside the alert window while its maximum never moves.
-- A runtime-stats row spans one Query Store interval, so "recent" reaches back to the start of the
-- interval the alert window falls in - at most one interval, not six hours.
recent_agg AS
(
    SELECT
        database_name,
        query_id,
        plan_id,
        SUM(ISNULL(count_executions, 0)) AS recent_executions,
        MAX(last_execution_time_local) AS last_execution_time_local,
        MAX(max_duration_sec) AS max_duration_sec,
        MAX(max_cpu_sec) AS max_cpu_sec,
        MAX(max_logical_io_reads) AS max_logical_io_reads,
        SUM(avg_cpu_sec * ISNULL(count_executions, 0)) AS recent_total_cpu_sec,
        SUM(avg_cpu_sec * ISNULL(count_executions, 0)) / NULLIF(SUM(ISNULL(count_executions, 0)), 0) AS recent_avg_cpu_sec
    FROM #qs_raw
    WHERE last_execution_time_local >= @p_AlertFromLocal
    GROUP BY database_name, query_id, plan_id
),
-- Each recent plan beside the cheapest OTHER plan of the same query over the baseline window, by
-- average CPU. Another plan, never this one: a plan compared with its own history has ratio ~1 by
-- construction.
recent_vs_best AS
(
    SELECT
        p.*,
        b.best_avg_cpu_sec,
        b.best_cpu_plan_id,
        cpu_ratio = p.recent_avg_cpu_sec / NULLIF(b.best_avg_cpu_sec, 0)
    FROM recent_agg p
    OUTER APPLY
    (
        SELECT TOP 1
            best_avg_cpu_sec = bb.avg_cpu_sec,
            best_cpu_plan_id = bb.plan_id
        FROM #qs_base bb
        WHERE bb.database_name = p.database_name
          AND bb.query_id = p.query_id
          AND bb.plan_id <> p.plan_id
          AND bb.executions >= @p_FreqMinExecutions
          AND bb.avg_cpu_sec > 0
        ORDER BY bb.avg_cpu_sec ASC
    ) AS b
),
-- QUERY_PLAN_REGRESSED_FREQUENT's CPU leg is the query's, not one plan's. On 2026-10-02 at 07:05 a
-- statement that had flipped between two bad plans burned 384 s on them (268 + 115) and another
-- 378 s (262 + 116): every plan under 300 s, so neither query was reported, though each was as
-- slow for its callers as the one that was. A bad plan here is one run often enough to judge, at
-- the warning ratio or worse; the recent CPU of a query's bad plans is summed, and the finding is
-- reported once per query - on its heaviest bad plan, with the sum and the count in the message.
query_bad AS
(
    SELECT
        database_name,
        query_id,
        plan_id,
        query_bad_plan_count = COUNT(*) OVER (PARTITION BY database_name, query_id),
        query_bad_cpu_sec = SUM(recent_total_cpu_sec) OVER (PARTITION BY database_name, query_id),
        bad_rank = ROW_NUMBER() OVER (PARTITION BY database_name, query_id
                                      ORDER BY recent_total_cpu_sec DESC, plan_id ASC)
    FROM recent_vs_best
    WHERE recent_executions >= @p_FreqMinExecutions
      AND cpu_ratio >= @p_FreqWarnCpuRatio
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
        h.runtime_stats_count,
        h.executions,
        p.last_execution_time_local,
        h.avg_duration_sec,
        p.max_duration_sec,
        h.avg_cpu_sec,
        p.max_cpu_sec,
        h.avg_logical_io_reads,
        p.max_logical_io_reads,
        p.recent_executions,
        p.recent_total_cpu_sec,
        p.recent_avg_cpu_sec,
        p.best_avg_cpu_sec,
        p.best_cpu_plan_id,
        p.cpu_ratio,
        qb.query_bad_plan_count,
        qb.query_bad_cpu_sec,
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

                -- small query, worse plan(s), executed often, at its CRITICAL legs: before the
                -- single-metric warnings, as its severity is below - after them, a plan that was
                -- both was reported as the WARNING and the CRITICAL was lost (0.27.0)
                WHEN p.recent_executions >= @p_FreqMinExecutions
                     AND p.cpu_ratio >= @p_FreqCritCpuRatio
                     AND qb.bad_rank = 1
                     AND qb.query_bad_cpu_sec >= @p_FreqCritTotalCpuSec
                THEN 'QUERY_PLAN_REGRESSED_FREQUENT'

                -- single metric warning
                WHEN p.max_cpu_sec >= 1800
                THEN 'QUERY_HEAVY_CPU'

                WHEN p.max_logical_io_reads >= 500000000
                THEN 'QUERY_HEAVY_READS'

                WHEN p.max_duration_sec >= 1800
                THEN 'QUERY_LONG_DURATION_OTHER'

                -- small query, worse plan(s), executed often (the thresholds at the top, query_bad)
                WHEN p.recent_executions >= @p_FreqMinExecutions
                     AND p.cpu_ratio >= @p_FreqWarnCpuRatio
                     AND qb.bad_rank = 1
                     AND qb.query_bad_cpu_sec >= @p_FreqWarnTotalCpuSec
                THEN 'QUERY_PLAN_REGRESSED_FREQUENT'

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

                -- every CRITICAL before the first WARNING (0.27.0)
                WHEN p.recent_executions >= @p_FreqMinExecutions
                     AND p.cpu_ratio >= @p_FreqCritCpuRatio
                     AND qb.bad_rank = 1
                     AND qb.query_bad_cpu_sec >= @p_FreqCritTotalCpuSec
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

                WHEN p.recent_executions >= @p_FreqMinExecutions
                     AND p.cpu_ratio >= @p_FreqWarnCpuRatio
                     AND qb.bad_rank = 1
                     AND qb.query_bad_cpu_sec >= @p_FreqWarnTotalCpuSec
                THEN 'WARNING'

                ELSE 'OK'
            END
    
    FROM recent_vs_best p
    JOIN plan_agg h
        ON h.database_name = p.database_name
       AND h.query_id = p.query_id
       AND h.plan_id = p.plan_id
    JOIN query_best q
        ON q.database_name = p.database_name
       AND q.query_id = p.query_id
    LEFT JOIN query_bad qb
        ON qb.database_name = p.database_name
       AND qb.query_id = p.query_id
       AND qb.plan_id = p.plan_id
),
issue_rows AS
(
    SELECT TOP 100
        d.*
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
            WHEN 'QUERY_PLAN_REGRESSED_FREQUENT' THEN 'query_store_plan_regressed_frequent'
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
            WHEN 'QUERY_PLAN_REGRESSED_FREQUENT' THEN CAST(CAST(cpu_ratio AS decimal(18,2)) AS varchar(32))
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
            WHEN 'QUERY_PLAN_REGRESSED_FREQUENT' THEN 'cpu_ratio'
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
        + ', avg_duration_sec=' + ISNULL(CAST(CAST(avg_duration_sec AS decimal(38,6)) AS varchar(40)), 'NULL')
        + ', max_duration_sec=' + CAST(max_duration_sec AS varchar(40))
        + ', avg_cpu_sec=' + ISNULL(CAST(CAST(avg_cpu_sec AS decimal(38,6)) AS varchar(40)), 'NULL')
        + ', max_cpu_sec=' + CAST(max_cpu_sec AS varchar(40))
        + ', avg_logical_reads=' + ISNULL(CAST(CAST(avg_logical_io_reads AS decimal(38,0)) AS varchar(40)), 'NULL')
        + ', max_logical_reads=' + CAST(CAST(max_logical_io_reads AS decimal(38,0)) AS varchar(40))
        + ', best_logical_reads=' + ISNULL(CAST(CAST(best_logical_reads AS decimal(38,0)) AS varchar(40)), 'NULL')
        + ', logical_read_ratio=' + ISNULL(CAST(CAST(logical_read_ratio AS decimal(18,2)) AS varchar(40)), 'NULL')
        -- The frequency finding's own evidence: what the plan did recently, and what it is
        -- measured against. Printed on every row so two findings on one query read the same way.
        + ', recent_executions=' + CAST(recent_executions AS varchar(20))
        + ', recent_total_cpu_sec=' + ISNULL(CAST(CAST(recent_total_cpu_sec AS decimal(18,2)) AS varchar(40)), 'NULL')
        + ', recent_avg_cpu_sec=' + ISNULL(CAST(CAST(recent_avg_cpu_sec AS decimal(18,6)) AS varchar(40)), 'NULL')
        + ', best_avg_cpu_sec=' + ISNULL(CAST(CAST(best_avg_cpu_sec AS decimal(18,6)) AS varchar(40)), 'NULL')
        + ', best_cpu_plan_id=' + ISNULL(CAST(best_cpu_plan_id AS varchar(30)), 'NULL')
        + ', cpu_ratio=' + ISNULL(CAST(CAST(cpu_ratio AS decimal(18,2)) AS varchar(40)), 'NULL')
        -- The query's bad plans together - what the frequency finding is judged on.
        + ', query_bad_plan_count=' + ISNULL(CAST(query_bad_plan_count AS varchar(20)), '0')
        + ', query_bad_cpu_sec=' + ISNULL(CAST(CAST(query_bad_cpu_sec AS decimal(18,2)) AS varchar(40)), 'NULL')
        + ', cpu_baseline_window=last_7_days'
        -- Both windows are printed because a reader who sees a 6-hour baseline next to a
        -- 30-minute alert window can tell "this just happened" from "this is what it is
        -- compared against" - which is the difference the repeated alerts hid.
        + ', alert_window=last_30_minutes'
        + ', alert_from=' + CONVERT(varchar(19), @p_AlertFromLocal, 120)
        + ', checked_window=last_6_hours'
        + ', checked_from=' + CONVERT(varchar(19), @p_FromLocal, 120)
        + ', checked_to=' + CONVERT(varchar(19), @p_ToLocal, 120)
        AS varchar(4000)
    ) AS message
FROM issue_rows

ORDER BY
    status ASC,
    metric_item ASC,
    message ASC;