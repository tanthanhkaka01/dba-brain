-- STORAGE_DATA_FILE_SPACE - Oracle datafile usage, autoextend aware.
--
-- Usage is measured against the file's *effective capacity*, not its current allocation.
-- A file sitting at 98% of the space Oracle has allocated so far is not a problem while it
-- can still autoextend: Oracle grows it on demand up to MAXBYTES. Measuring against the
-- current size raised false CRITICALs (system01.dbf reported 98.78% on the Data Guard
-- primary while it still had room to grow), which is exactly the alert an operator learns
-- to ignore. Only a file that has nowhere left to grow - autoextend off, or already at
-- MAXBYTES - is worth waking someone for.
WITH file_usage AS (
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
),
file_pct AS (
    SELECT
        tablespace_name,
        file_name,
        autoextensible,
        total_bytes,
        capacity_bytes,
        ROUND(used_bytes * 100 / NULLIF(capacity_bytes, 0), 2) AS used_pct,
        ROUND(used_bytes * 100 / NULLIF(total_bytes, 0), 2)    AS used_pct_of_current
    FROM file_usage
)
SELECT
    CAST(file_name AS varchar2(256)) AS metric_item,
    TO_CHAR(used_pct) AS metric_value,
    CAST('pct' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN used_pct >= 95 THEN 'CRITICAL'
        WHEN used_pct >= 85 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'tablespace=' || tablespace_name ||
        ', file=' || file_name ||
        ', used_pct=' || TO_CHAR(used_pct) ||
        ', autoextensible=' || autoextensible ||
        ', used_pct_of_current_size=' || TO_CHAR(used_pct_of_current) ||
        ', current_mb=' || TO_CHAR(ROUND(total_bytes / 1024 / 1024, 2)) ||
        ', max_mb=' || TO_CHAR(ROUND(capacity_bytes / 1024 / 1024, 2)) AS message
FROM file_pct
ORDER BY used_pct DESC;
