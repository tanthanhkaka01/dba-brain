-- Oracle 8i (8.1.7) legacy variant - PERFORMANCE_WORKLOAD_COUNTERS.
--
-- The work the instance has done since it started, raw, for the report to subtract two
-- collections of. 8i has neither v$sys_time_model nor v$osstat, so its CPU is the one total it
-- keeps - v$sysstat CPU used by this session, in hundredths of a second, summed over every session
-- the instance has run, ended ones included - and its throughput is the rest of v$sysstat, under
-- the Workload table keys where the meaning is the same (the 10g file says which).
--
-- What it cannot say. 8i adds a call to these totals when the call ends, so a query still running
-- at a collection is counted at the collection after it finishes - an interval holding the end of
-- a long batch reads high and the ones before it low. Over the hour and the day that evens out.
-- There is no host figure at all from 8i.
--
-- Written for 8i: old-style joins, DECODE, no WITH, no column 9i added. No comment here holds a
-- semicolon or a quote, so no client between this file and the server has to read one correctly
-- to find where the statement ends.
SELECT
    c.metric_item,
    c.metric_value,
    c.metric_unit,
    'OK' AS status,
    'value=' || c.metric_value ||
        ', unit=' || c.metric_unit ||
        ', counters_since=' || TO_CHAR(i.startup_time, 'YYYY-MM-DD HH24:MI:SS') ||
        ', cpu_count=' || p.value ||
        ', schedulers_online=' || p.value ||
        ', source=v$sysstat' ||
        ', note=cumulative_since_start_subtract_two_collections' AS message
FROM (
    SELECT 'cpu_usage_ms' AS metric_item, TO_CHAR(s.value * 10) AS metric_value, 'ms' AS metric_unit
    FROM v$sysstat s
    WHERE s.name = 'CPU used by this session'
    UNION ALL
    SELECT m.metric_item, TO_CHAR(SUM(m.value)), MIN(m.metric_unit)
    FROM (
        SELECT DECODE(s.name,
                   'user commits', 'transactions',
                   'user rollbacks', 'transactions',
                   'user calls', 'user_calls',
                   'execute count', 'executions',
                   'parse count (hard)', 'sql_compilations',
                   'session logical reads', 'page_lookups',
                   'physical reads', 'page_reads',
                   'physical writes', 'page_writes',
                   'redo size', 'log_bytes_flushed',
                   'redo synch writes', 'log_flushes',
                   'logons cumulative', 'logins',
                   'enqueue deadlocks', 'deadlocks',
                   'sorts (disk)', 'workfiles_created') AS metric_item,
               DECODE(s.name,
                   'session logical reads', 'blocks',
                   'physical reads', 'blocks',
                   'physical writes', 'blocks',
                   'redo size', 'bytes',
                   'count') AS metric_unit,
               s.value
        FROM v$sysstat s
        WHERE s.name IN ('user commits', 'user rollbacks', 'user calls', 'execute count',
                         'parse count (hard)', 'session logical reads', 'physical reads',
                         'physical writes', 'redo size', 'redo synch writes', 'logons cumulative',
                         'enqueue deadlocks', 'sorts (disk)')
    ) m
    GROUP BY m.metric_item
) c,
v$instance i,
v$parameter p
WHERE p.name = 'cpu_count'
