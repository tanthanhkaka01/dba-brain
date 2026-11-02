-- Oracle 8i legacy variant - PERFORMANCE_IO_LATENCY. Per-datafile read latency from
-- v$filestat + v$datafile (8i). centiseconds per physical read.
-- The raw totals ride in the message in SQL Server's names and in milliseconds (reads, writes,
-- io_stall_read_ms, io_stall_write_ms, counters_since) so a report page can difference two stored
-- collections into the last hour and the last day (dbabrain.lib.report_windows). v$instance is
-- joined in the old comma form: this is the 8i dialect, through the bridge.
SELECT * FROM (
    SELECT
        d.name AS metric_item,
        TO_CHAR(CASE WHEN f.phyrds > 0 THEN ROUND(f.readtim / f.phyrds, 2) ELSE 0 END) AS metric_value,
        'cs_per_read' AS metric_unit,
        'OK' AS status,
        'file=' || d.name || ', phyrds=' || f.phyrds || ', phywrts=' || f.phywrts ||
            ', readtim_cs=' || f.readtim ||
            ', reads=' || f.phyrds || ', writes=' || f.phywrts ||
            ', io_stall_read_ms=' || (f.readtim * 10) || ', io_stall_write_ms=' || (f.writetim * 10) ||
            ', counters_since=' || TO_CHAR(i.startup_time, 'YYYY-MM-DD HH24:MI:SS') AS message
    FROM v$filestat f, v$datafile d, v$instance i
    WHERE f.file# = d.file#
    ORDER BY f.readtim DESC
)
WHERE ROWNUM <= 15;
