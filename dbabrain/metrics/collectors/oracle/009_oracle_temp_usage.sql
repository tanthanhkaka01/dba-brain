-- STORAGE_TEMP_SPACE (Oracle variant) - active temporary tablespace usage.
--
-- Measures temporary space in use NOW by active queries from v$tempseg_usage (blocks * block size)
-- per temporary tablespace, evaluated against total effective capacity from dba_temp_files
-- (counting autoextension headroom up to maxbytes).
--
-- Why not v$temp_space_header:
-- v$temp_space_header reports OS-level allocated extents that locally managed temporary
-- tablespaces retain after a sort completes. A single large sort causes v$temp_space_header
-- used_pct to stay permanently near 100%, producing endless false-alarm CRITICAL alerts.
-- v$tempseg_usage measures active sort, hash, and temporary segments currently held by live sessions.
--
-- An outer join from dba_temp_files ensures idle temporary tablespaces (zero active sorts)
-- still report a row with 0% utilization and status OK, avoiding empty result false positives.
--
-- Approved thresholds: >= 95% CRITICAL, >= 85% WARNING against potential capacity.
-- All thresholds are declared in the top-level params CTE so every number appears once.

WITH params AS (
    SELECT
        95 AS critical_pct,
        85 AS warning_pct
    FROM dual
),
temp_files_summary AS (
    SELECT
        tf.tablespace_name,
        SUM(tf.bytes) AS alloc_bytes,
        SUM(CASE
            WHEN tf.autoextensible = 'YES' AND tf.maxbytes > tf.bytes THEN tf.maxbytes
            ELSE tf.bytes
        END) AS capacity_bytes,
        COUNT(*) AS file_count,
        SUM(CASE WHEN tf.autoextensible = 'YES' THEN 1 ELSE 0 END) AS autoext_files
    FROM dba_temp_files tf
    GROUP BY tf.tablespace_name
),
active_sort_usage AS (
    SELECT
        u.tablespace AS tablespace_name,
        SUM(u.blocks) AS used_blocks
    FROM v$tempseg_usage u
    GROUP BY u.tablespace
),
tablespace_blocks AS (
    SELECT
        t.tablespace_name,
        t.block_size
    FROM dba_tablespaces t
    WHERE t.contents = 'TEMPORARY'
),
calculated AS (
    SELECT
        f.tablespace_name,
        f.alloc_bytes,
        f.capacity_bytes,
        f.file_count,
        f.autoext_files,
        NVL(u.used_blocks, 0) * NVL(tb.block_size, (SELECT TO_NUMBER(value) FROM v$parameter WHERE name = 'db_block_size')) AS used_bytes
    FROM temp_files_summary f
    LEFT JOIN active_sort_usage u
        ON u.tablespace_name = f.tablespace_name
    LEFT JOIN tablespace_blocks tb
        ON tb.tablespace_name = f.tablespace_name
)
SELECT
    CAST(c.tablespace_name AS varchar2(256)) AS metric_item,
    CAST(TO_CHAR(ROUND(c.used_bytes * 100 / NULLIF(c.capacity_bytes, 0), 2)) AS varchar2(32)) AS metric_value,
    CAST('pct' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN (c.used_bytes * 100 / NULLIF(c.capacity_bytes, 0)) >= p.critical_pct THEN 'CRITICAL'
        WHEN (c.used_bytes * 100 / NULLIF(c.capacity_bytes, 0)) >= p.warning_pct THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'temp_tablespace=' || c.tablespace_name ||
        ', used_pct=' || TO_CHAR(ROUND(c.used_bytes * 100 / NULLIF(c.capacity_bytes, 0), 2)) ||
        ', used_mb=' || TO_CHAR(ROUND(c.used_bytes / 1048576, 2)) ||
        ', max_mb=' || TO_CHAR(ROUND(c.capacity_bytes / 1048576, 2)) ||
        ', allocated_mb=' || TO_CHAR(ROUND(c.alloc_bytes / 1048576, 2)) ||
        ', used_pct_of_current_size=' || TO_CHAR(ROUND(c.used_bytes * 100 / NULLIF(c.alloc_bytes, 0), 2)) ||
        ', tempfiles=' || TO_CHAR(c.file_count) ||
        ', autoextend_files=' || TO_CHAR(c.autoext_files) AS message
FROM calculated c
CROSS JOIN params p
ORDER BY ROUND(c.used_bytes * 100 / NULLIF(c.capacity_bytes, 0), 2) DESC, c.tablespace_name;
