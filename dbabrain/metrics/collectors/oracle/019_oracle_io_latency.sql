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
            ', avg_io_ms=' || TO_CHAR(ROUND((fs.readtim + fs.writetim) * 10 / NULLIF(fs.phyrds + fs.phywrts, 0), 2)) AS message
    FROM v$filestat fs
    JOIN v$datafile df
        ON df.file# = fs.file#
    WHERE fs.phyrds + fs.phywrts > 0
    ORDER BY (fs.readtim + fs.writetim) / NULLIF(fs.phyrds + fs.phywrts, 0) DESC
)
WHERE ROWNUM <= 20;
