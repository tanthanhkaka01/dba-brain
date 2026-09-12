-- PERFORMANCE_LOG_BY_DATABASE: which database is producing the transaction log, and how long its
-- log writes take.
--
-- Asked on 2026-09-11 from the Workload section of one instance: WRITELOG was 36.3% of all waits
-- over 15 minutes and 25.5% over the day, and the page could say only that the instance as a whole
-- flushed log. It could not say which database. That is the first question anyone asks about
-- WRITELOG, and the instance-wide counters in PERFORMANCE_WORKLOAD_COUNTERS cannot answer it,
-- because they are read at _Total.
--
-- The same contract as that metric: raw cumulative totals, one row per database, no grading and no
-- division. The report subtracts two collections per database (db_ops.lib.interval_rates).
--
-- **Two sources, because neither has the whole answer.**
--
--   * sys.dm_os_performance_counters, object Databases, one instance per database: how much log
--     was flushed, in how many flushes, how many commits had to wait for one, and how many
--     transactions wrote. Only cntr_type = 272696576 is read -- the cumulative family.
--     "Log Flush Wait Time" and "Log Flush Write Time (ms)" sit in the same object as instantaneous
--     gauges (the value for the last second), so they are NOT here: a differenced gauge is a number
--     that looks like a rate and is not one.
--   * sys.dm_io_virtual_file_stats joined to the LOG files only: writes, bytes and io_stall_write_ms.
--     That stall is the cumulative time this database's log writes took, which is the figure the
--     wait-time gauge above cannot give -- divided by the write count it is the log write latency
--     the committing sessions were waiting on.
--
-- **What this does not answer, and nothing in the engine does.** No DMV attributes log bytes to a
-- procedure or a statement. The nearest honest proxy is logical writes per query, which
-- PERFORMANCE_TOP_QUERIES records; the report says which of the two a figure came from.
--
-- A database that restarts on its own -- restored, taken offline, AUTO_CLOSE -- resets its own
-- counters without the engine restarting. The report differences each database separately and
-- refuses a pair that went backwards, so that costs the one database its interval and nothing else.

SET NOCOUNT ON;

-- The engine restart marker, as in every cumulative collector: a delta is never taken across it.
DECLARE @counters_since varchar(19) =
    CONVERT(varchar(19), (SELECT sqlserver_start_time FROM sys.dm_os_sys_info), 120);

DECLARE @db TABLE (
    database_name      nvarchar(128) NOT NULL PRIMARY KEY,
    log_bytes_flushed  bigint NULL,
    log_flushes        bigint NULL,
    log_flush_waits    bigint NULL,
    transactions       bigint NULL,
    write_transactions bigint NULL,
    log_files          int    NULL,
    log_writes         bigint NULL,
    log_bytes_written  bigint NULL,
    log_write_stall_ms bigint NULL
);

-- One read of the counters, pivoted per database. object_name carries the instance prefix
-- ("SQLServer:" or "MSSQL$NAME:") and both char columns are blank-padded, hence LIKE and RTRIM.
INSERT INTO @db (database_name, log_bytes_flushed, log_flushes, log_flush_waits,
                 transactions, write_transactions)
SELECT RTRIM(p.instance_name),
       MAX(CASE WHEN RTRIM(p.counter_name) = 'Log Bytes Flushed/sec'  THEN p.cntr_value END),
       MAX(CASE WHEN RTRIM(p.counter_name) = 'Log Flushes/sec'        THEN p.cntr_value END),
       MAX(CASE WHEN RTRIM(p.counter_name) = 'Log Flush Waits/sec'    THEN p.cntr_value END),
       MAX(CASE WHEN RTRIM(p.counter_name) = 'Transactions/sec'       THEN p.cntr_value END),
       MAX(CASE WHEN RTRIM(p.counter_name) = 'Write Transactions/sec' THEN p.cntr_value END)
FROM sys.dm_os_performance_counters AS p
WHERE RTRIM(p.object_name) LIKE '%:Databases'
  AND p.cntr_type = 272696576
  AND RTRIM(p.counter_name) IN ('Log Bytes Flushed/sec', 'Log Flushes/sec', 'Log Flush Waits/sec',
                                'Transactions/sec', 'Write Transactions/sec')
  -- _Total is PERFORMANCE_WORKLOAD_COUNTERS' row, and the resource database writes no user log.
  AND RTRIM(p.instance_name) NOT IN ('_Total', 'mssqlsystemresource')
GROUP BY RTRIM(p.instance_name);

-- The log files' own I/O, summed per database (almost always one log file; a second one is summed
-- rather than listed, because the log is written sequentially to one file at a time).
UPDATE d
SET log_files          = f.log_files,
    log_writes         = f.log_writes,
    log_bytes_written  = f.log_bytes_written,
    log_write_stall_ms = f.log_write_stall_ms
FROM @db AS d
INNER JOIN (
    SELECT DB_NAME(vfs.database_id)       AS database_name,
           COUNT(*)                       AS log_files,
           SUM(vfs.num_of_writes)         AS log_writes,
           SUM(vfs.num_of_bytes_written)  AS log_bytes_written,
           SUM(vfs.io_stall_write_ms)     AS log_write_stall_ms
    FROM sys.dm_io_virtual_file_stats(NULL, NULL) AS vfs
    INNER JOIN sys.master_files AS mf
            ON mf.database_id = vfs.database_id
           AND mf.file_id = vfs.file_id
    WHERE mf.type = 1                     -- LOG
    GROUP BY vfs.database_id
) AS f
    ON f.database_name = d.database_name;

SELECT
    CAST(d.database_name AS nvarchar(256)) AS metric_item,
    CAST(ISNULL(d.log_bytes_flushed, 0) AS varchar(32)) AS metric_value,
    CAST('bytes' AS varchar(32)) AS metric_unit,
    -- Always OK: a total is not a condition. The report is what turns two of them into a reading.
    CAST('OK' AS varchar(16)) AS status,
    -- A field that is NULL is left out rather than written as 0: a counter missing on this build
    -- is "not collected", and a zero would be differenced into "did no work".
    CAST(
        'value=' + CAST(ISNULL(d.log_bytes_flushed, 0) AS varchar(32))
        + ', unit=bytes'
        + ', counters_since=' + @counters_since
        + ISNULL(', log_bytes_flushed='  + CAST(d.log_bytes_flushed  AS varchar(32)), '')
        + ISNULL(', log_flushes='        + CAST(d.log_flushes        AS varchar(32)), '')
        + ISNULL(', log_flush_waits='    + CAST(d.log_flush_waits    AS varchar(32)), '')
        + ISNULL(', transactions='       + CAST(d.transactions       AS varchar(32)), '')
        + ISNULL(', write_transactions=' + CAST(d.write_transactions AS varchar(32)), '')
        + ISNULL(', log_files='          + CAST(d.log_files          AS varchar(12)), '')
        + ISNULL(', log_writes='         + CAST(d.log_writes         AS varchar(32)), '')
        + ISNULL(', log_bytes_written='  + CAST(d.log_bytes_written  AS varchar(32)), '')
        + ISNULL(', log_write_stall_ms=' + CAST(d.log_write_stall_ms AS varchar(32)), '')
        + ', source=dm_os_performance_counters+dm_io_virtual_file_stats'
        + ', note=cumulative_since_start_subtract_two_collections_per_database'
        AS varchar(1000)) AS message
FROM @db AS d
ORDER BY d.log_bytes_flushed DESC;
