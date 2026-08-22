-- Oracle 8i legacy variant - STORAGE_DATA_FILE_SPACE. Datafile sizes per tablespace.
-- The 2026-05-04 incident added 9 datafiles (TEMP/RBS/INDX/TOOLS/DRSYS/USERS/LBR_IDX/
-- LBR_TMP/LBR_TS @1000M autoextend), so track datafile size growth here.
SELECT
    tablespace_name || ':' || file_name AS metric_item,
    TO_CHAR(ROUND(bytes / 1024 / 1024, 2)) AS metric_value,
    'MB' AS metric_unit,
    'OK' AS status,
    'tablespace=' || tablespace_name || ', file=' || file_name ||
        ', size_mb=' || ROUND(bytes / 1024 / 1024, 2) AS message
FROM dba_data_files
ORDER BY tablespace_name, file_name;
