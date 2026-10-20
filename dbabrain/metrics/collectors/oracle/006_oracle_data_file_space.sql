-- STORAGE_DATA_FILE_SPACE (Oracle variant) - tablespace capacity and usage, autoextend aware.
--
-- Ported from legacy_8i/101 to evaluate multi-file tablespaces as ONE whole, rather than grading
-- file-by-file. In Oracle, extents can be allocated in any datafile of a tablespace that has free
-- room or can autoextend. Grading file-by-file emitted false alarms on full non-extensible files
-- even when a second datafile in the same tablespace had tens of gigabytes of autoextend headroom.
--
-- Evaluates total allocated bytes, maximum capacity (respecting autoextend up to maxbytes), and
-- free space across all data files in dba_data_files, joined with dba_free_space per tablespace.
-- An outer join ensures a tablespace with zero free extents is not silently dropped from the result.
--
-- Thresholds: >= 95% used CRITICAL, >= 85% used WARNING against effective tablespace capacity.
-- All thresholds are declared in the top-level params CTE so every number appears once.

WITH params AS (
    SELECT
        95 AS critical_pct,
        85 AS warning_pct
    FROM dual
),
files_summary AS (
    SELECT
        df.tablespace_name,
        SUM(df.bytes) AS alloc_bytes,
        SUM(CASE
            WHEN df.autoextensible = 'YES' AND df.maxbytes > df.bytes THEN df.maxbytes
            ELSE df.bytes
        END) AS capacity_bytes,
        COUNT(*) AS file_count,
        SUM(CASE WHEN df.autoextensible = 'YES' THEN 1 ELSE 0 END) AS autoext_files
    FROM dba_data_files df
    GROUP BY df.tablespace_name
),
free_summary AS (
    SELECT
        fs.tablespace_name,
        SUM(fs.bytes) AS free_bytes,
        MAX(fs.bytes) AS largest_free_bytes
    FROM dba_free_space fs
    GROUP BY fs.tablespace_name
),
tablespace_usage AS (
    SELECT
        f.tablespace_name,
        f.alloc_bytes,
        f.capacity_bytes,
        f.file_count,
        f.autoext_files,
        NVL(s.free_bytes, 0) AS free_bytes,
        NVL(s.largest_free_bytes, 0) AS largest_free_bytes,
        -- Total used space = current allocation minus current free space within datafiles
        f.alloc_bytes - NVL(s.free_bytes, 0) AS used_bytes,
        -- Effective free space = free space in files + autoextend growth headroom
        NVL(s.free_bytes, 0) + (f.capacity_bytes - f.alloc_bytes) AS effective_free_bytes,
        -- Used percentage of total potential capacity (including autoextension)
        ROUND((f.alloc_bytes - NVL(s.free_bytes, 0)) * 100 / NULLIF(f.capacity_bytes, 0), 2) AS used_pct,
        -- Used percentage of currently allocated space
        ROUND((f.alloc_bytes - NVL(s.free_bytes, 0)) * 100 / NULLIF(f.alloc_bytes, 0), 2) AS used_pct_of_current
    FROM files_summary f
    LEFT JOIN free_summary s
        ON s.tablespace_name = f.tablespace_name
),
file_usage AS (
    SELECT
        df.tablespace_name,
        df.file_name,
        df.bytes                            AS total_bytes,
        df.bytes - NVL(fs.free_bytes, 0)    AS used_bytes,
        df.autoextensible,
        CASE
            WHEN df.autoextensible = 'YES' AND df.maxbytes > df.bytes THEN df.maxbytes
            ELSE df.bytes
        END                                 AS capacity_bytes
    FROM dba_data_files df
    LEFT JOIN (
        SELECT file_id, SUM(bytes) AS free_bytes
        FROM dba_free_space
        GROUP BY file_id
    ) fs
        ON fs.file_id = df.file_id
)
-- Two shapes, one graded. The TABLESPACE is what fills: Oracle places an extent in any file of it
-- with room, so one full file beside two that can grow is no incident - the tablespace row is
-- graded. The per-FILE rows are kept exactly as they were, recorded and not graded: the server
-- page lists each tablespace's datafiles from them (reports/server_oracle.py reads `tablespace=`
-- and `file=`) and the fleet page sums their `current_mb` into the database's size
-- (reports/inventory_health.py). The tablespace row therefore says `scope=tablespace, name=` and
-- never `tablespace=` or `file=`, so neither reader counts it twice.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        1 AS sort_group,
        t.used_pct AS sort_pct,
        CAST('tablespace:' || t.tablespace_name AS varchar2(256)) AS metric_item,
        CAST(TO_CHAR(t.used_pct) AS varchar2(32)) AS metric_value,
        CAST('pct' AS varchar2(32)) AS metric_unit,
        CAST(CASE
            WHEN t.used_pct >= p.critical_pct THEN 'CRITICAL'
            WHEN t.used_pct >= p.warning_pct THEN 'WARNING'
            ELSE 'OK'
        END AS varchar2(16)) AS status,
        'scope=tablespace, name=' || t.tablespace_name ||
            ', used_pct=' || TO_CHAR(t.used_pct) ||
            ', effective_free_mb=' || TO_CHAR(ROUND(t.effective_free_bytes / 1048576, 2)) ||
            ', free_now_mb=' || TO_CHAR(ROUND(t.free_bytes / 1048576, 2)) ||
            ', autoextend_headroom_mb=' || TO_CHAR(ROUND((t.capacity_bytes - t.alloc_bytes) / 1048576, 2)) ||
            ', allocated_mb=' || TO_CHAR(ROUND(t.alloc_bytes / 1048576, 2)) ||
            ', max_mb=' || TO_CHAR(ROUND(t.capacity_bytes / 1048576, 2)) ||
            ', used_pct_of_current_size=' || TO_CHAR(t.used_pct_of_current) ||
            ', datafiles=' || TO_CHAR(t.file_count) ||
            ', autoextend_files=' || TO_CHAR(t.autoext_files) AS message
    FROM tablespace_usage t
    CROSS JOIN params p
    UNION ALL
    SELECT
        2 AS sort_group,
        ROUND(f.used_bytes * 100 / NULLIF(f.capacity_bytes, 0), 2) AS sort_pct,
        CAST(f.file_name AS varchar2(256)) AS metric_item,
        TO_CHAR(ROUND(f.used_bytes * 100 / NULLIF(f.capacity_bytes, 0), 2)) AS metric_value,
        CAST('pct' AS varchar2(32)) AS metric_unit,
        CAST('LOGGING' AS varchar2(16)) AS status,
        'tablespace=' || f.tablespace_name ||
            ', file=' || f.file_name ||
            ', used_pct=' || TO_CHAR(ROUND(f.used_bytes * 100 / NULLIF(f.capacity_bytes, 0), 2)) ||
            ', autoextensible=' || f.autoextensible ||
            ', used_pct_of_current_size=' || TO_CHAR(ROUND(f.used_bytes * 100 / NULLIF(f.total_bytes, 0), 2)) ||
            ', current_mb=' || TO_CHAR(ROUND(f.total_bytes / 1024 / 1024, 2)) ||
            ', max_mb=' || TO_CHAR(ROUND(f.capacity_bytes / 1024 / 1024, 2)) AS message
    FROM file_usage f
)
ORDER BY sort_group, sort_pct DESC, metric_item;
