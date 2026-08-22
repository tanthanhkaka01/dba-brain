-- Oracle 8i incident metric - TABLESPACE_FREE_SPACE. Space a tablespace can still hand out,
-- which is free space inside its datafiles PLUS the growth its autoextending files have left.
-- Guards ORA-01652 / capacity incidents (2026-05-04 added 9 datafiles because tablespaces were
-- short on capacity).
--
-- It reports *effective* free rather than current free because current free alone was raising
-- false alarms: LBR_TS showed 89.98 MB free and went CRITICAL on 2026-08-13 while its second
-- datafile was autoextending with 31.8 GB of headroom left. A DBA looking at that tablespace
-- would not act, so neither should the monitor - an alert nobody acts on teaches everyone to
-- ignore the next one.
--
-- It also drives off dba_data_files with an outer join to dba_free_space, not off dba_free_space
-- alone. A tablespace with no free extent at all has NO row in dba_free_space, so the previous
-- query dropped it from the result entirely: the one state that most deserves CRITICAL was the
-- one state that reported nothing.
--
-- Rows are one per tablespace; metric_value is effective free MB. Both numbers stay in the
-- message, because "89 MB free but 31 GB of autoextend behind it" is the sentence an operator
-- actually needs. Locally managed temp tablespaces live in dba_temp_files and are not listed
-- here on purpose - STORAGE_TEMP_SPACE covers sort space.
SELECT
    metric_item,
    TO_CHAR(effective_free_mb) AS metric_value,
    'MB' AS metric_unit,
    CASE
        WHEN effective_free_mb < 100 OR pct_free < 2 THEN 'CRITICAL'
        WHEN effective_free_mb < 500 OR pct_free < 5 THEN 'WARNING'
        -- Room only because the files are still growing. Not a problem, but the one case where
        -- an operator wants to see the tablespace before it becomes one.
        WHEN free_now_mb < 100 THEN 'LOGGING'
        ELSE 'OK'
    END AS status,
    'tablespace=' || metric_item ||
        ', effective_free_mb=' || effective_free_mb ||
        ' (' || pct_free || '% of max)' ||
        ', free_now_mb=' || free_now_mb ||
        ', autoextend_headroom_mb=' || headroom_mb ||
        ', allocated_mb=' || alloc_mb ||
        ', max_mb=' || max_mb ||
        ', datafiles=' || file_count || ' (autoextend=' || autoext_files || ')' ||
        ', largest_free_extent_mb=' || largest_free_mb AS message
FROM (
    SELECT
        f.tablespace_name AS metric_item,
        ROUND(f.alloc_mb, 2) AS alloc_mb,
        ROUND(f.max_mb, 2) AS max_mb,
        ROUND(f.max_mb - f.alloc_mb, 2) AS headroom_mb,
        ROUND(NVL(s.free_mb, 0), 2) AS free_now_mb,
        ROUND(NVL(s.free_mb, 0) + (f.max_mb - f.alloc_mb), 2) AS effective_free_mb,
        ROUND((NVL(s.free_mb, 0) + (f.max_mb - f.alloc_mb)) * 100
              / DECODE(f.max_mb, 0, 1, f.max_mb), 1) AS pct_free,
        ROUND(NVL(s.largest_free_mb, 0), 2) AS largest_free_mb,
        f.file_count AS file_count,
        f.autoext_files AS autoext_files
    FROM (
        SELECT
            tablespace_name,
            SUM(bytes) / 1048576 AS alloc_mb,
            -- A file that is not autoextending can never exceed its current size; one that is
            -- stops at maxbytes. GREATEST guards the case where a file was resized past the
            -- MAXSIZE it was created with, which 8i allows and which would otherwise make the
            -- headroom come out negative.
            SUM(DECODE(autoextensible, 'YES', GREATEST(maxbytes, bytes), bytes)) / 1048576 AS max_mb,
            COUNT(*) AS file_count,
            SUM(DECODE(autoextensible, 'YES', 1, 0)) AS autoext_files
        FROM dba_data_files
        GROUP BY tablespace_name
    ) f,
    (
        SELECT
            tablespace_name,
            SUM(bytes) / 1048576 AS free_mb,
            MAX(bytes) / 1048576 AS largest_free_mb
        FROM dba_free_space
        GROUP BY tablespace_name
    ) s
    WHERE f.tablespace_name = s.tablespace_name(+)
)
ORDER BY effective_free_mb;
