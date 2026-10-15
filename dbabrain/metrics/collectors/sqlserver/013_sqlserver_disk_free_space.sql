WITH disk AS (
    SELECT
        v.volume_mount_point,
        MIN(v.available_bytes) AS available_bytes,
        MIN(v.total_bytes) AS total_bytes
    FROM sys.master_files AS mf
    CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) AS v
    GROUP BY v.volume_mount_point
)
SELECT
    CAST(volume_mount_point AS varchar(256)) AS metric_item,
    CAST(CAST(available_bytes / 1024.0 / 1024.0 / 1024.0 AS decimal(19,2)) AS varchar(32)) AS metric_value,
    CAST('GB' AS varchar(32)) AS metric_unit,
    CASE
        WHEN available_bytes / 1024.0 / 1024.0 / 1024.0 < 5
          OR available_bytes * 100.0 / NULLIF(total_bytes, 0) < 5
            THEN 'CRITICAL'
        WHEN available_bytes / 1024.0 / 1024.0 / 1024.0 < 10
          OR available_bytes * 100.0 / NULLIF(total_bytes, 0) < 10
            THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    CONCAT(
        'drive=', volume_mount_point,
        ', free_gb=', CAST(CAST(available_bytes / 1024.0 / 1024.0 / 1024.0 AS decimal(19,2)) AS varchar(32)),
        ', total_gb=', CAST(CAST(total_bytes / 1024.0 / 1024.0 / 1024.0 AS decimal(19,2)) AS varchar(32)),
        ', free_pct=', CAST(CAST(available_bytes * 100.0 / NULLIF(total_bytes, 0) AS decimal(10,2)) AS varchar(32)),
        ', used_pct=', CAST(CAST((total_bytes - available_bytes) * 100.0 / NULLIF(total_bytes, 0) AS decimal(10,2)) AS varchar(32))
    ) AS message
FROM disk

UNION ALL

SELECT
    CAST('disk' AS varchar(256)),
    CAST('0' AS varchar(32)),
    CAST('GB' AS varchar(32)),
    'WARNING',
    'No disk volume information visible. Check permission for sys.dm_os_volume_stats.'
WHERE NOT EXISTS (SELECT 1 FROM disk);

-- -- next step check if critical
-- SELECT
--     v.volume_mount_point,
--     MIN(v.available_bytes) AS free_bytes,
--     CAST(MIN(v.available_bytes) / 1024.0 / 1024.0 AS decimal(19,2)) AS free_mb,
--     CAST(MIN(v.available_bytes) / 1024.0 / 1024.0 / 1024.0 AS decimal(19,4)) AS free_gb,
--     CAST(MIN(v.total_bytes) / 1024.0 / 1024.0 / 1024.0 AS decimal(19,2)) AS total_gb
-- FROM sys.master_files AS mf
-- CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) AS v
-- GROUP BY v.volume_mount_point;

-- -- get free space all disk
-- EXEC master.dbo.xp_fixeddrives;