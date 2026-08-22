WITH file_size AS (
    SELECT
        SUM(size) AS total_page_count
    FROM tempdb.sys.database_files
    WHERE type_desc = 'ROWS'
),
space_usage AS (
    SELECT
        SUM(unallocated_extent_page_count) AS free_page_count,
        SUM(user_object_reserved_page_count) AS user_object_page_count,
        SUM(internal_object_reserved_page_count) AS internal_object_page_count,
        SUM(version_store_reserved_page_count) AS version_store_page_count
    FROM tempdb.sys.dm_db_file_space_usage
)
SELECT
    CAST('tempdb' AS varchar(256)) AS metric_item,
    CAST(CAST(x.used_pct AS decimal(10,2)) AS varchar(32)) AS metric_value,
    CAST('pct' AS varchar(32)) AS metric_unit,
    CASE
        WHEN x.used_pct >= 95 AND x.free_mb < 1024 THEN 'CRITICAL'
        WHEN x.used_pct >= 85 AND x.free_mb < 2048 THEN 'WARNING'
        WHEN x.used_pct >= 95 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'tempdb_used_pct=' + CAST(CAST(x.used_pct AS decimal(10,2)) AS varchar(32))
        + ', total_mb=' + CAST(CAST(x.total_mb AS decimal(19,2)) AS varchar(32))
        + ', used_mb=' + CAST(CAST(x.used_mb AS decimal(19,2)) AS varchar(32))
        + ', free_mb=' + CAST(CAST(x.free_mb AS decimal(19,2)) AS varchar(32))
        + ', user_object_mb=' + CAST(CAST(x.user_object_mb AS decimal(19,2)) AS varchar(32))
        + ', internal_object_mb=' + CAST(CAST(x.internal_object_mb AS decimal(19,2)) AS varchar(32))
        + ', version_store_mb=' + CAST(CAST(x.version_store_mb AS decimal(19,2)) AS varchar(32)) AS message
FROM (
    SELECT
        fs.total_page_count * 8.0 / 1024.0 AS total_mb,
        su.free_page_count * 8.0 / 1024.0 AS free_mb,
        su.user_object_page_count * 8.0 / 1024.0 AS user_object_mb,
        su.internal_object_page_count * 8.0 / 1024.0 AS internal_object_mb,
        su.version_store_page_count * 8.0 / 1024.0 AS version_store_mb,
        (
            su.user_object_page_count
          + su.internal_object_page_count
          + su.version_store_page_count
        ) * 8.0 / 1024.0 AS used_mb,
        (
            (
                su.user_object_page_count
              + su.internal_object_page_count
              + su.version_store_page_count
            ) * 100.0
        ) / NULLIF(fs.total_page_count, 0) AS used_pct
    FROM file_size fs
    CROSS JOIN space_usage su
) AS x;