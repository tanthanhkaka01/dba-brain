-- Oracle 8i legacy variant - PERFORMANCE_IO_LATENCY. Per-datafile read latency from
-- v$filestat + v$datafile (8i). centiseconds per physical read.
SELECT * FROM (
    SELECT
        d.name AS metric_item,
        TO_CHAR(CASE WHEN f.phyrds > 0 THEN ROUND(f.readtim / f.phyrds, 2) ELSE 0 END) AS metric_value,
        'cs_per_read' AS metric_unit,
        'OK' AS status,
        'file=' || d.name || ', phyrds=' || f.phyrds || ', phywrts=' || f.phywrts ||
            ', readtim_cs=' || f.readtim AS message
    FROM v$filestat f, v$datafile d
    WHERE f.file# = d.file#
    ORDER BY f.readtim DESC
)
WHERE ROWNUM <= 15;
