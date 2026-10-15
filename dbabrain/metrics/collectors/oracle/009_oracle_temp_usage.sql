SELECT
    CAST(tablespace_name AS varchar2(256)) AS metric_item,
    TO_CHAR(ROUND(SUM(bytes_used) * 100 / NULLIF(SUM(bytes_used + bytes_free), 0), 2)) AS metric_value,
    CAST('pct' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN SUM(bytes_used) * 100 / NULLIF(SUM(bytes_used + bytes_free), 0) >= 95 THEN 'CRITICAL'
        WHEN SUM(bytes_used) * 100 / NULLIF(SUM(bytes_used + bytes_free), 0) >= 85 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'temp_tablespace=' || tablespace_name ||
        ', used_pct=' || TO_CHAR(ROUND(SUM(bytes_used) * 100 / NULLIF(SUM(bytes_used + bytes_free), 0), 2)) AS message
FROM v$temp_space_header
GROUP BY tablespace_name
ORDER BY tablespace_name;
