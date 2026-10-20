-- LOG_FILE_SPACE (Oracle variant) - space pressure on redo/archive storage and destination health.
--
-- The Oracle answer to "is the log about to run out of room?" has two legs:
-- 1. Fast Recovery Area (FRA): when FRA fills with unreclaimable files, archiving stalls and the
--    database freezes. Thresholds: >= 95% CRITICAL, >= 85% WARNING on unreclaimable space.
-- 2. Archive destinations (v$archive_dest): an archive destination that is configured
--    (destination IS NOT NULL) and whose status is not VALID (e.g. ERROR, BAD PARAM) means ARCH
--    cannot write archives; when online redo groups fill, the instance halts with error.
--    INACTIVE and DEFERRED destinations are unused or administratively disabled and are not failures.
--
-- In a PDB session, v$recovery_file_dest or v$archive_dest may expose CDB data or 0 rows; an empty
-- destination result inside a PDB is expected and not a failure.
-- Always returns at least one row: an instance with no FRA configured falls back to redo logs.
--
-- All thresholds are declared in the top-level params CTE so every number appears once.

WITH params AS (
    SELECT
        95 AS fra_critical_pct,
        85 AS fra_warning_pct
    FROM dual
),
fra AS (
    SELECT
        d.name,
        d.space_limit,
        d.space_used,
        d.number_of_files,
        d.space_used / d.space_limit * 100 AS used_pct,
        d.space_reclaimable / d.space_limit * 100 AS reclaimable_pct,
        (d.space_used - d.space_reclaimable) / d.space_limit * 100 AS unreclaimable_pct
    FROM v$recovery_file_dest d
    WHERE d.space_limit > 0
)
SELECT
    CAST('fast_recovery_area' AS varchar2(256)) AS metric_item,
    TO_CHAR(ROUND(f.used_pct, 2)) AS metric_value,
    CAST('percent' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN f.unreclaimable_pct >= p.fra_critical_pct THEN 'CRITICAL'
        WHEN f.unreclaimable_pct >= p.fra_warning_pct THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'name=' || f.name
        || ', used_pct=' || TO_CHAR(ROUND(f.used_pct, 2))
        || ', reclaimable_pct=' || TO_CHAR(ROUND(f.reclaimable_pct, 2))
        || ', unreclaimable_pct=' || TO_CHAR(ROUND(f.unreclaimable_pct, 2))
        || ', space_limit_mb=' || TO_CHAR(ROUND(f.space_limit / 1048576))
        || ', space_used_mb=' || TO_CHAR(ROUND(f.space_used / 1048576))
        || ', files=' || TO_CHAR(f.number_of_files) AS message
FROM fra f
CROSS JOIN params p

UNION ALL

-- Per file type inside the FRA, so a run-away archived-log or flashback-log population is visible
-- instead of being averaged into one number.
SELECT
    CAST('fra_' || LOWER(REPLACE(u.file_type, ' ', '_')) AS varchar2(256)) AS metric_item,
    TO_CHAR(ROUND(u.percent_space_used, 2)) AS metric_value,
    CAST('percent' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN u.percent_space_used - u.percent_space_reclaimable >= p.fra_warning_pct THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'file_type=' || u.file_type
        || ', used_pct=' || TO_CHAR(ROUND(u.percent_space_used, 2))
        || ', reclaimable_pct=' || TO_CHAR(ROUND(u.percent_space_reclaimable, 2))
        || ', files=' || TO_CHAR(u.number_of_files) AS message
FROM v$recovery_area_usage u
CROSS JOIN params p
WHERE u.number_of_files > 0

UNION ALL

-- Configured archive destinations (v$archive_dest).
-- A destination configured (destination IS NOT NULL) that is not VALID (e.g. ERROR) halts archiving.
-- INACTIVE or DEFERRED destinations are unused or administratively disabled and are not failures.
-- Inside a PDB, v$archive_dest may show CDB destinations or no rows; an empty result is not a failure.
SELECT
    CAST('archive_dest_' || TO_CHAR(dest_id) AS varchar2(256)) AS metric_item,
    CAST(status AS varchar2(32)) AS metric_value,
    CAST('state' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN status = 'VALID' THEN 'OK'
        WHEN status IN ('INACTIVE', 'DEFERRED') THEN 'OK'
        ELSE 'CRITICAL'
    END AS status,
    'destination=' || SUBSTR(destination, 1, 200) ||
        ', status=' || status ||
        ', binding=' || binding ||
        ', error=' || NVL(SUBSTR(error, 1, 200), 'none') ||
        CASE
            WHEN status = 'VALID' OR status IN ('INACTIVE', 'DEFERRED') THEN ''
            ELSE '; ARCH cannot write here - when online redo groups fill, the instance stops.'
        END AS message
FROM v$archive_dest
WHERE destination IS NOT NULL

UNION ALL

-- No FRA: report the redo configuration so the metric still describes log capacity.
SELECT
    CAST('redo_logs' AS varchar2(256)) AS metric_item,
    TO_CHAR((SELECT ROUND(SUM(bytes) / 1048576) FROM v$log)) AS metric_value,
    CAST('MB' AS varchar2(32)) AS metric_unit,
    'OK' AS status,
    'No Fast Recovery Area configured (db_recovery_file_dest is unset), reporting redo capacity: '
        || 'groups=' || TO_CHAR((SELECT COUNT(*) FROM v$log))
        || ', total_mb=' || TO_CHAR((SELECT ROUND(SUM(bytes) / 1048576) FROM v$log)) AS message
FROM dual
WHERE NOT EXISTS
(
    SELECT 1
    FROM v$recovery_file_dest
    WHERE space_limit > 0
);
