IF OBJECT_ID('tempdb..#fixeddrives') IS NOT NULL
    DROP TABLE #fixeddrives;

CREATE TABLE #fixeddrives
(
    drive varchar(16),
    free_mb int
);

INSERT INTO #fixeddrives
EXEC master.dbo.xp_fixeddrives;

SELECT
    CAST(drive + ':' AS varchar(256)) AS metric_item,
    CAST(CAST(free_mb / 1024.0 AS decimal(19,2)) AS varchar(32)) AS metric_value,
    CAST('GB' AS varchar(32)) AS metric_unit,
    CASE
        WHEN free_mb / 1024.0 < 5 THEN 'CRITICAL'
        WHEN free_mb / 1024.0 < 10 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'drive=' + drive + ':, free_gb=' + CAST(CAST(free_mb / 1024.0 AS decimal(19,2)) AS varchar(32))
        + ', source=xp_fixeddrives, total_gb=unknown' AS message
FROM #fixeddrives;
