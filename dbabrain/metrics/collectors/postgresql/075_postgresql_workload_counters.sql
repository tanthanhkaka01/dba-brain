-- PERFORMANCE_WORKLOAD_COUNTERS (PostgreSQL 10+): how much work the cluster has done, raw (1.149's
-- port, 2026-10-16).
--
-- The SQL Server variant (072) says why this metric records totals and never grades them: a counter
-- read once is an average over the whole uptime, and the report subtracts two collections to get
-- the interval (dbabrain.lib.interval_rates). pg_stat_database holds PostgreSQL's totals per
-- database; summed, they are the cluster's, under the Workload table's own keys where the meaning
-- is the same:
--
--   transactions        xact_commit + xact_rollback
--   page_lookups        blks_hit + blks_read      every block a query asked the buffer cache for
--   page_reads          blks_read                 the ones it had to fetch - from the OS, which may
--                                                 still have had them cached
--   rows_read           tup_returned + tup_fetched
--   rows_written        tup_inserted + tup_updated + tup_deleted
--   temp_files          temp_files                a sort or hash that spilled to disk
--   temp_bytes          temp_bytes
--   deadlocks           deadlocks
--   logins              sessions (14+)            read through to_jsonb, so a version without
--                                                 the column parses and simply lacks the row
--   io_reads / io_stall_read_ms   blks_read and blk_read_time - only with track_io_timing on:
--                                 off, the time column is a flat 0 and the page would print a
--                                 0 ms read latency it never measured
--   log_bytes_flushed   WAL written since initdb (pg_current_wal_lsn), a primary's only: a
--                       standby replays WAL, it does not generate it
--
-- PostgreSQL has no CPU total of its own; its CPU comes from the host (OS_CPU_COUNTERS).
--
-- **counters_since** is the later of the postmaster's start and the newest stats_reset: either one
-- restarts these totals, and a pair of collections across it must not be subtracted.
WITH d AS (
    SELECT sum(xact_commit + xact_rollback) AS transactions,
           sum(blks_hit + blks_read) AS page_lookups,
           sum(blks_read) AS page_reads,
           sum(tup_returned + tup_fetched) AS rows_read,
           sum(tup_inserted + tup_updated + tup_deleted) AS rows_written,
           sum(temp_files) AS temp_files,
           sum(temp_bytes) AS temp_bytes,
           sum(deadlocks) AS deadlocks,
           sum((to_jsonb(s) ->> 'sessions')::bigint) AS logins,
           round(sum(blk_read_time))::bigint AS io_stall_read_ms,
           max(stats_reset) AS stats_reset
    FROM pg_stat_database AS s
),
meta AS (
    SELECT to_char(GREATEST(pg_postmaster_start_time(), d.stats_reset), 'YYYY-MM-DD HH24:MI:SS') AS counters_since,
           current_setting('track_io_timing') = 'on' AS io_timing,
           pg_is_in_recovery() AS standby
    FROM d
),
counters (metric_item, metric_value, metric_unit, source) AS (
    SELECT 'transactions', d.transactions, 'count', 'pg_stat_database' FROM d
    UNION ALL SELECT 'page_lookups', d.page_lookups, 'blocks', 'pg_stat_database' FROM d
    UNION ALL SELECT 'page_reads', d.page_reads, 'blocks', 'pg_stat_database' FROM d
    UNION ALL SELECT 'rows_read', d.rows_read, 'count', 'pg_stat_database' FROM d
    UNION ALL SELECT 'rows_written', d.rows_written, 'count', 'pg_stat_database' FROM d
    UNION ALL SELECT 'temp_files', d.temp_files, 'count', 'pg_stat_database' FROM d
    UNION ALL SELECT 'temp_bytes', d.temp_bytes, 'bytes', 'pg_stat_database' FROM d
    UNION ALL SELECT 'deadlocks', d.deadlocks, 'count', 'pg_stat_database' FROM d
    UNION ALL SELECT 'logins', d.logins, 'count', 'pg_stat_database' FROM d
    UNION ALL SELECT 'io_reads', d.page_reads, 'count', 'pg_stat_database' FROM d, meta m WHERE m.io_timing
    UNION ALL SELECT 'io_stall_read_ms', d.io_stall_read_ms, 'ms', 'pg_stat_database' FROM d, meta m WHERE m.io_timing
    UNION ALL
    SELECT 'log_bytes_flushed',
           CASE WHEN m.standby THEN NULL
                ELSE pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint END,
           'bytes', 'pg_current_wal_lsn'
    FROM meta m
)
SELECT
    CAST(c.metric_item AS varchar(256)) AS metric_item,
    CAST(c.metric_value AS varchar(32)) AS metric_value,
    CAST(c.metric_unit AS varchar(32)) AS metric_unit,
    -- Always OK: a counter has no threshold. The rate it becomes is judged by whoever compares two.
    CAST('OK' AS varchar(16)) AS status,
    CAST('value=' || c.metric_value
        || ', unit=' || c.metric_unit
        || ', counters_since=' || m.counters_since
        || ', source=' || c.source
        || ', note=cumulative_since_start_subtract_two_collections' AS varchar(1000)) AS message
FROM counters AS c, meta AS m
WHERE c.metric_value IS NOT NULL
ORDER BY c.metric_item
