-- Oracle 8i incident metric - SHARED_POOL_FREE. Free memory per SGA pool (v$sgastat).
-- Low shared-pool free memory precedes ORA-04031. The 8.1.7 profile tuning centred on
-- shared_pool_size, so track free memory here.
SELECT
    pool || ':' || name AS metric_item,
    TO_CHAR(ROUND(bytes / 1024 / 1024, 2)) AS metric_value,
    'MB' AS metric_unit,
    CASE
        WHEN pool = 'shared pool' AND bytes / 1024 / 1024 < 10 THEN 'CRITICAL'
        WHEN pool = 'shared pool' AND bytes / 1024 / 1024 < 25 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'pool=' || pool || ', name=' || name || ', free_mb=' || ROUND(bytes / 1024 / 1024, 2) AS message
FROM v$sgastat
WHERE name = 'free memory'
ORDER BY pool;
