-- PERFORMANCE_QUERY_STATS_TOTALS: what the cached plans have cost since they were compiled.
--
-- The same contract as PERFORMANCE_WORKLOAD_COUNTERS: raw totals, no grading, no division. The
-- report subtracts two collections to get an interval. See that file for why the arithmetic lives
-- there and not here.
--
-- **This one is a sample, not a census, and the message says so.** sys.dm_exec_query_stats
-- reports only what is in the plan cache right now:
--
--   * an evicted plan takes its totals with it, so the sum can fall while the instance is busy --
--     which is why a delta computed across an eviction must be dropped rather than clamped;
--   * a batch that was never cached (ad hoc without "optimize for ad hoc workloads", anything
--     compiled with RECOMPILE) was never counted at all;
--   * DBCC FREEPROCCACHE, a failover, a memory-pressure flush and a configuration change all
--     reset it without restarting the engine, so sqlserver_start_time is a necessary marker and
--     not a sufficient one.
--
-- So this metric answers "how much of the work the instance did is attributable to plans I can
-- still see", which is the right question for finding the statements to tune, and it is the wrong
-- number for "how much CPU did this instance burn". PERFORMANCE_WORKLOAD_COUNTERS answers that
-- one from the resource pools, where nothing is evicted. Both are reported so the two can be read
-- against each other: query CPU far below pool CPU means the expensive work is not in the cache.
--
-- cached_plans and cache_baseline_minutes are carried for exactly that judgement. A cache whose
-- oldest plan is twenty minutes old is a cache that is churning, and its deltas mean very little.
--
-- **This is the expensive one, and it runs at half the cadence of the other two for that reason.**
-- The aggregate below scans the whole plan cache. On one measured instance -- 86,400 cached plans, 70,109
-- of them carrying statistics -- it exceeded the 60-second timeout on three consecutive runs on
-- 2026-09-03, and the identical query returned in 0.4 seconds once warm. Raising the timeout is
-- the wrong fix: holding a plan-cache scan open on a production instance for minutes is worse than
-- a missing reading. So the interval is 1800 seconds, which still fills the hour from two
-- collections, and an instance where this keeps timing out is telling you about its own plan cache
-- ("optimize for ad hoc workloads") rather than about this query.

SET NOCOUNT ON;

-- The engine restart marker, carried like every other cumulative metric so a delta is never taken
-- across a restart. It does not detect a cache flush -- cache_baseline_minutes below is what a
-- reader judges that by.
DECLARE @counters_since varchar(19) =
    CONVERT(varchar(19), (SELECT sqlserver_start_time FROM sys.dm_os_sys_info), 120);

-- One pass over the cache. Every figure below has to come from the same read: totals taken at
-- different moments cannot be compared with each other, and the whole point of the block is that
-- they can.
DECLARE @cpu_ms bigint, @elapsed_ms bigint, @logical_reads bigint, @logical_writes bigint,
        @physical_reads bigint, @executions bigint, @plans bigint, @oldest_minutes int;

SELECT @cpu_ms         = SUM(qs.total_worker_time) / 1000,
       @elapsed_ms     = SUM(qs.total_elapsed_time) / 1000,
       @logical_reads  = SUM(qs.total_logical_reads),
       @logical_writes = SUM(qs.total_logical_writes),
       @physical_reads = SUM(qs.total_physical_reads),
       @executions     = SUM(qs.execution_count),
       @plans          = COUNT_BIG(*),
       -- How far back the cache reaches. DATEDIFF over minutes rather than seconds because a
       -- cache older than 68 years would overflow, and one older than a few hours is all the
       -- precision this judgement needs.
       @oldest_minutes = DATEDIFF(minute, MIN(qs.creation_time), GETDATE())
FROM sys.dm_exec_query_stats AS qs;

DECLARE @rows TABLE (
    metric_item  varchar(64) NOT NULL PRIMARY KEY,
    metric_value varchar(32) NOT NULL,
    metric_unit  varchar(32) NOT NULL
);

INSERT INTO @rows (metric_item, metric_value, metric_unit)
SELECT x.metric_item, x.metric_value, x.metric_unit
FROM (
              SELECT 'query_cpu_ms' AS metric_item, CAST(@cpu_ms AS varchar(32)) AS metric_value,
                     'ms' AS metric_unit
    UNION ALL SELECT 'query_elapsed_ms', CAST(@elapsed_ms AS varchar(32)), 'ms'
    UNION ALL SELECT 'query_logical_reads', CAST(@logical_reads AS varchar(32)), 'pages'
    UNION ALL SELECT 'query_logical_writes', CAST(@logical_writes AS varchar(32)), 'pages'
    UNION ALL SELECT 'query_physical_reads', CAST(@physical_reads AS varchar(32)), 'pages'
    UNION ALL SELECT 'query_executions', CAST(@executions AS varchar(32)), 'count'
    -- The two below are gauges, not counters, and are marked so: the report must not difference
    -- them. They are here because they are what says whether the six above can be trusted.
    UNION ALL SELECT 'cached_plans', CAST(@plans AS varchar(32)), 'gauge_count'
    UNION ALL SELECT 'cache_baseline_minutes', CAST(@oldest_minutes AS varchar(32)), 'gauge_minutes'
) AS x
-- An empty plan cache -- a just-restarted instance -- makes every aggregate NULL. Reporting no
-- rows is right: there is nothing to subtract yet, and a zero would be read as "did no work".
WHERE x.metric_value IS NOT NULL;

SELECT
    CAST(r.metric_item AS varchar(256)) AS metric_item,
    CAST(r.metric_value AS varchar(32)) AS metric_value,
    CAST(r.metric_unit AS varchar(32)) AS metric_unit,
    -- Always OK, like every raw-counter collector: a total is not a condition.
    CAST('OK' AS varchar(16)) AS status,
    CAST(
        'value=' + r.metric_value
        + ', unit=' + r.metric_unit
        + ', counters_since=' + @counters_since
        + ', cached_plans=' + ISNULL(CAST(@plans AS varchar(32)), '0')
        + ', cache_baseline_minutes=' + ISNULL(CAST(@oldest_minutes AS varchar(32)), '0')
        + ', source=dm_exec_query_stats'
        + ', note=plan_cache_resident_only_delta_undercounts_and_can_fall_on_eviction'
        AS varchar(1000)) AS message
FROM @rows AS r
ORDER BY r.metric_item;
