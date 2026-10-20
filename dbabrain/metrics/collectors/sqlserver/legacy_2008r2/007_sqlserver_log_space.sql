-- Today the metric emits only LOGGING or OK for a log that can still grow: it grows onto the
-- disk, and a full disk is paged by STORAGE_DISK_FREE_SPACE rather than here (the operator,
-- 2026-10-15: "the log can still grow, which is why it was lowered to LOGGING; disk full is
-- the CRITICAL").
--
-- But a log that CANNOT grow - autogrowth disabled, or capped at max_size - fills and halts the
-- database (error 9002) while the disk still has plenty of room, so the disk space metric never
-- alerts. A capped or non-autogrowing log is the one failure mode the disk metric cannot see,
-- so this metric must page for it directly when it approaches its ceiling.
DECLARE @log_cannot_grow_critical_pct int = 95;
DECLARE @log_cannot_grow_warning_pct int = 85;

IF OBJECT_ID('tempdb..#logspace') IS NOT NULL
    DROP TABLE #logspace;

CREATE TABLE #logspace
(
    database_name sysname,
    log_size_mb decimal(19,2),
    log_used_pct decimal(10,2),
    status_code int
);

INSERT INTO #logspace
EXEC('DBCC SQLPERF(LOGSPACE)');

SELECT
    CAST(ls.database_name COLLATE DATABASE_DEFAULT AS varchar(256)) AS metric_item,
    CAST(ls.log_used_pct AS varchar(32)) AS metric_value,
    CAST('pct' AS varchar(32)) AS metric_unit,
    CASE
        WHEN cg.log_can_grow = 0 AND ls.log_used_pct >= @log_cannot_grow_critical_pct
        THEN 'CRITICAL'

        WHEN cg.log_can_grow = 0 AND ls.log_used_pct >= @log_cannot_grow_warning_pct
        THEN 'WARNING'

        WHEN ls.log_used_pct >= 95 AND ls.log_size_mb >= 1024
        THEN 'LOGGING'

        WHEN ls.log_used_pct >= 85 AND ls.log_size_mb >= 512
        THEN 'LOGGING'

        WHEN ls.log_used_pct >= 95
        THEN 'LOGGING'

        ELSE 'OK'
    END AS status,
    'database=' + ls.database_name COLLATE DATABASE_DEFAULT
        + ', log_used_pct=' + CAST(ls.log_used_pct AS varchar(32))
        + ', log_size_mb=' + CAST(ls.log_size_mb AS varchar(32))
        + ', log_can_grow=' + ISNULL(CAST(cg.log_can_grow AS varchar(1)), 'unknown')
        + ', log_max_size_mb=' + ISNULL(lf.log_max_size_mb, 'UNLIMITED')
        + ', log_growth=' + ISNULL(lf.log_growth, 'NONE')
        + ', log_reuse_wait=' + ISNULL(d.log_reuse_wait_desc, 'NONE') AS message
FROM #logspace ls
JOIN sys.databases d
    ON d.name COLLATE DATABASE_DEFAULT = ls.database_name COLLATE DATABASE_DEFAULT
-- sys.master_files shows a row only to a login holding VIEW ANY DEFINITION, CREATE DATABASE or
-- ALTER ANY DATABASE. Without them it shows no log file at all, and "no file can grow" would page
-- every log at 95 % - so a log whose files cannot be seen is unknown (NULL), and an unknown log is
-- graded as one that can grow.
CROSS APPLY (
    SELECT
        CASE
            WHEN NOT EXISTS (
                SELECT 1
                FROM sys.master_files mv
                WHERE mv.database_id = d.database_id
                  AND mv.type = 1
            ) THEN NULL
            WHEN EXISTS (
                SELECT 1
                FROM sys.master_files mf
                WHERE mf.database_id = d.database_id
                  AND mf.type = 1
                  AND mf.growth > 0
                  AND (
                      mf.max_size = -1
                      OR mf.max_size = 268435456
                      OR (CAST(mf.size AS bigint) + CASE
                              WHEN mf.is_percent_growth = 1 THEN (CAST(mf.size AS bigint) * mf.growth) / 100
                              ELSE CAST(mf.growth AS bigint)
                          END) <= mf.max_size
                  )
            ) THEN 1
            ELSE 0
        END AS log_can_grow
) cg
OUTER APPLY (
    SELECT TOP (1)
        CASE
            WHEN mf.max_size = -1 OR mf.max_size = 268435456 THEN 'UNLIMITED'
            ELSE CAST(CAST(mf.max_size / 128.0 AS decimal(19,2)) AS varchar(32))
        END AS log_max_size_mb,
        CASE
            WHEN mf.growth = 0 THEN 'NONE'
            WHEN mf.is_percent_growth = 1 THEN CAST(mf.growth AS varchar(10)) + '%'
            ELSE CAST(CAST(mf.growth AS bigint) / 128 AS varchar(20)) + 'MB'
        END AS log_growth
    FROM sys.master_files mf
    WHERE mf.database_id = d.database_id
      AND mf.type = 1
    ORDER BY
        CASE
            WHEN mf.growth > 0 AND (
                mf.max_size = -1
                OR mf.max_size = 268435456
                OR (CAST(mf.size AS bigint) + CASE
                        WHEN mf.is_percent_growth = 1 THEN (CAST(mf.size AS bigint) * mf.growth) / 100
                        ELSE CAST(mf.growth AS bigint)
                    END) <= mf.max_size
            ) THEN 1
            ELSE 0
        END DESC,
        mf.size DESC,
        mf.file_id ASC
) lf
WHERE (d.database_id > 4 OR d.database_id = 2)
  AND d.state = 0
  AND d.is_read_only = 0
ORDER BY ls.log_used_pct DESC;

DROP TABLE #logspace;
