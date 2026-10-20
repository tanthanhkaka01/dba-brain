-- PERFORMANCE_WORKLOAD_COUNTERS (Oracle 10g+): the CPU this instance and its host have spent,
-- reported raw.
--
-- The SQL Server variant (072) says why this metric records totals and never grades them: a
-- counter read once is an average over the whole uptime, and the report subtracts two collections
-- to get the interval (dbabrain.lib.interval_rates). This is the same contract on Oracle, written
-- for its CPU first because CPU was the reading the server page had for SQL Server and not for
-- Oracle at all.
--
--   * cpu_usage_ms - v$sys_time_model 'DB CPU' plus 'background cpu time', microseconds since the
--     instance (or, inside a PDB, the container) started. Foreground and background together are
--     what the instance burned. Oracle's own measure, independent of any host access.
--   * host_busy_ms / host_idle_ms - v$osstat BUSY_TIME and IDLE_TIME, hundredths of a second,
--     summed over every CPU of the host. The host's view from inside the engine, which SQL Server
--     cannot give: busy over busy-plus-idle is the host's CPU over the interval, every edition,
--     with no login on the machine.
--
-- All three are in-memory V$ views, not AWR: the licensing guide names V$ACTIVE_SESSION_HISTORY
-- and DBA_HIST_* as the Diagnostics Pack's, and not these.
--
-- schedulers_online is how many CPUs the instance may use, which is not always what the host
-- has. cpu_count is the instance's parameter (instance caging sets it lower). Standard Edition 2
-- is held to 16 CPU threads by the database itself, and Express and Free to 2, whatever cpu_count
-- says - a percentage of the host's CPUs on such an instance tops out well short of 100 with the
-- engine saturated, the mistake 072 corrects for SQL Server Standard.
WITH meta AS (
    SELECT TO_CHAR(i.startup_time, 'YYYY-MM-DD HH24:MI:SS') AS counters_since,
           (SELECT TO_CHAR(value) FROM v$osstat WHERE stat_name = 'NUM_CPUS') AS host_cpus,
           p.cpu_param,
           NVL(REPLACE(b.banner, ',', ' '), 'unknown') AS banner,
           CASE
               WHEN b.banner LIKE '%Standard Edition%' THEN LEAST(p.cpu_param, 16)
               WHEN b.banner LIKE '%Express Edition%' OR b.banner LIKE '%Free%'
                   THEN LEAST(p.cpu_param, 2)
               ELSE p.cpu_param
           END AS usable_cpus
    FROM v$instance i,
         (SELECT TO_NUMBER(value) AS cpu_param FROM v$parameter WHERE name = 'cpu_count') p,
         (SELECT MIN(banner) AS banner FROM v$version WHERE banner LIKE 'Oracle%') b
),
-- The throughput half (1.149's port, 2026-10-16): v$sysstat's instance totals, under the Workload
-- table's own keys where the meaning is the same - a transaction is a commit or a rollback, a
-- logical read is a block got from the cache, log generated is redo bytes - and under keys of their
-- own where SQL Server has no such row (user calls, executions). Two statistics adding up to one row
-- are summed. A name a version does not have is simply absent from the result.
sysstat AS (
    SELECT CASE s.name
               WHEN 'user commits' THEN 'transactions'
               WHEN 'user rollbacks' THEN 'transactions'
               WHEN 'user calls' THEN 'user_calls'
               WHEN 'execute count' THEN 'executions'
               WHEN 'parse count (hard)' THEN 'sql_compilations'
               WHEN 'session logical reads' THEN 'page_lookups'
               WHEN 'physical reads' THEN 'page_reads'
               WHEN 'physical writes' THEN 'page_writes'
               WHEN 'redo size' THEN 'log_bytes_flushed'
               WHEN 'redo synch writes' THEN 'log_flushes'
               WHEN 'logons cumulative' THEN 'logins'
               WHEN 'enqueue deadlocks' THEN 'deadlocks'
               WHEN 'sorts (disk)' THEN 'workfiles_created'
               WHEN 'physical read total IO requests' THEN 'io_reads'
               WHEN 'physical write total IO requests' THEN 'io_writes'
               WHEN 'physical read total bytes' THEN 'io_bytes_read'
               WHEN 'physical write total bytes' THEN 'io_bytes_written'
           END AS metric_item,
           CASE
               WHEN s.name IN ('session logical reads', 'physical reads', 'physical writes') THEN 'blocks'
               WHEN s.name IN ('redo size', 'physical read total bytes', 'physical write total bytes') THEN 'bytes'
               ELSE 'count'
           END AS metric_unit,
           s.value
    FROM v$sysstat s
    WHERE s.name IN ('user commits', 'user rollbacks', 'user calls', 'execute count',
                     'parse count (hard)', 'session logical reads', 'physical reads',
                     'physical writes', 'redo size', 'redo synch writes', 'logons cumulative',
                     'enqueue deadlocks', 'sorts (disk)', 'physical read total IO requests',
                     'physical write total IO requests', 'physical read total bytes',
                     'physical write total bytes')
),
counters AS (
    SELECT metric_item, TO_CHAR(SUM(value)) AS metric_value, MIN(metric_unit) AS metric_unit,
           'v$sysstat' AS source
    FROM sysstat
    GROUP BY metric_item
    UNION ALL
    SELECT 'cpu_usage_ms' AS metric_item,
           TO_CHAR(ROUND(SUM(value) / 1000)) AS metric_value,
           'ms' AS metric_unit,
           'v$sys_time_model' AS source
    FROM v$sys_time_model
    WHERE stat_name IN ('DB CPU', 'background cpu time')
    UNION ALL
    SELECT 'host_busy_ms', TO_CHAR(value * 10), 'ms', 'v$osstat'
    FROM v$osstat WHERE stat_name = 'BUSY_TIME'
    UNION ALL
    SELECT 'host_idle_ms', TO_CHAR(value * 10), 'ms', 'v$osstat'
    FROM v$osstat WHERE stat_name = 'IDLE_TIME'
)
SELECT
    CAST(c.metric_item AS varchar2(256)) AS metric_item,
    c.metric_value,
    CAST(c.metric_unit AS varchar2(32)) AS metric_unit,
    -- Always OK: a counter has no threshold. The rate it becomes is judged by whoever compares two.
    CAST('OK' AS varchar2(16)) AS status,
    'value=' || c.metric_value
        || ', unit=' || c.metric_unit
        || ', counters_since=' || m.counters_since
        || ', cpu_count=' || NVL(m.host_cpus, TO_CHAR(m.cpu_param))
        || ', schedulers_online=' || TO_CHAR(m.usable_cpus)
        || ', edition=' || m.banner
        || ', source=' || c.source
        || ', note=cumulative_since_start_subtract_two_collections' AS message
FROM counters c, meta m
WHERE c.metric_value IS NOT NULL
ORDER BY c.metric_item
