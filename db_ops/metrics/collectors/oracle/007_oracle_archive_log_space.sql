-- LOG_FILE_SPACE (Oracle variant) - space pressure on redo/archive storage.
--
-- The Oracle answer to "is the log about to run out of room?" is the Fast Recovery Area: when the
-- FRA fills, archiving stalls and the database stops. That is the same operational consequence a
-- full SQL Server transaction log has, which is why both live under this metric code.
--
-- metric_value is percent used, thresholds 85 WARNING / 95 CRITICAL.
--
-- The reclaimable percentage matters as much as the used percentage: a FRA at 95% where 90% is
-- reclaimable is healthy (Oracle will delete obsolete files on demand), whereas 95% with nothing
-- reclaimable is about to stop the instance. Both are in the message, and the status only escalates
-- on space that cannot be reclaimed.
--
-- Always returns at least one row: an instance with no FRA configured reports that fact, and falls
-- back to redo group sizing so the metric is never silently empty.

SELECT
    CAST('fast_recovery_area' AS varchar2(256)) AS metric_item,
    TO_CHAR(ROUND(f.used_pct, 2)) AS metric_value,
    CAST('percent' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN f.unreclaimable_pct >= 95 THEN 'CRITICAL'
        WHEN f.unreclaimable_pct >= 85 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'name=' || f.name
        || ', used_pct=' || TO_CHAR(ROUND(f.used_pct, 2))
        || ', reclaimable_pct=' || TO_CHAR(ROUND(f.reclaimable_pct, 2))
        || ', unreclaimable_pct=' || TO_CHAR(ROUND(f.unreclaimable_pct, 2))
        || ', space_limit_mb=' || TO_CHAR(ROUND(f.space_limit / 1048576))
        || ', space_used_mb=' || TO_CHAR(ROUND(f.space_used / 1048576))
        || ', files=' || TO_CHAR(f.number_of_files) AS message
FROM
(
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
) f

UNION ALL

-- Per file type inside the FRA, so a run-away archived-log or flashback-log population is visible
-- instead of being averaged into one number.
SELECT
    CAST('fra_' || LOWER(REPLACE(u.file_type, ' ', '_')) AS varchar2(256)) AS metric_item,
    TO_CHAR(ROUND(u.percent_space_used, 2)) AS metric_value,
    CAST('percent' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN u.percent_space_used - u.percent_space_reclaimable >= 85 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'file_type=' || u.file_type
        || ', used_pct=' || TO_CHAR(ROUND(u.percent_space_used, 2))
        || ', reclaimable_pct=' || TO_CHAR(ROUND(u.percent_space_reclaimable, 2))
        || ', files=' || TO_CHAR(u.number_of_files) AS message
FROM v$recovery_area_usage u
WHERE u.number_of_files > 0

UNION ALL

-- No FRA: report the redo configuration so the metric still describes log capacity.
--
-- Driven off dual with scalar subqueries, NOT off v$log with SUM()/COUNT(). An aggregate with no
-- GROUP BY returns exactly one row even when its WHERE clause matches nothing, so the earlier
-- "FROM v$log ... WHERE NOT EXISTS (fra)" shape emitted a bogus "no FRA configured, groups=0,
-- total_mb=" row on every instance that *does* have a FRA. Selecting from dual makes the
-- NOT EXISTS actually suppress the row.
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
