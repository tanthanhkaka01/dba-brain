-- PERFORMANCE_IO_LATENCY (Oracle 9i+): per-datafile I/O latency from v$filestat.
--
-- The value is the average since the instance started - all a single query can see. The raw
-- totals ride in the message (reads, writes, the stall in ms, counters_since) so a report page
-- can difference two stored collections into the last hour and the last day
-- (dbabrain.lib.report_windows), as it does for SQL Server's file stats. readtim and writetim are
-- centiseconds, kept only while timed_statistics is on.
SELECT *
FROM (
    SELECT
        CAST(df.name AS varchar2(256)) AS metric_item,
        TO_CHAR(ROUND((fs.readtim + fs.writetim) * 10 / NULLIF(fs.phyrds + fs.phywrts, 0), 2)) AS metric_value,
        CAST('ms' AS varchar2(32)) AS metric_unit,
        CASE
            WHEN (fs.readtim + fs.writetim) * 10 / NULLIF(fs.phyrds + fs.phywrts, 0) >= 50 THEN 'WARNING'
            ELSE 'OK'
        END AS status,
        'file=' || df.name ||
            ', avg_io_ms=' || TO_CHAR(ROUND((fs.readtim + fs.writetim) * 10 / NULLIF(fs.phyrds + fs.phywrts, 0), 2)) ||
            ', reads=' || TO_CHAR(fs.phyrds) ||
            ', writes=' || TO_CHAR(fs.phywrts) ||
            ', io_stall_read_ms=' || TO_CHAR(fs.readtim * 10) ||
            ', io_stall_write_ms=' || TO_CHAR(fs.writetim * 10) ||
            ', counters_since=' || TO_CHAR(i.startup_time, 'YYYY-MM-DD HH24:MI:SS') AS message
    FROM v$filestat fs
    JOIN v$datafile df
        ON df.file# = fs.file#
    CROSS JOIN v$instance i
    WHERE fs.phyrds + fs.phywrts > 0
    ORDER BY (fs.readtim + fs.writetim) / NULLIF(fs.phyrds + fs.phywrts, 0) DESC
)
WHERE ROWNUM <= 20;
