-- PAGE_LIFE_EXPECTANCY: buffer manager page life expectancy (seconds) for the
-- inventory performance_health block. Logging only (status always OK, no alert).
SELECT
    CAST('page_life_expectancy' AS varchar(64)) AS metric_item,
    CAST(p.cntr_value AS varchar(32)) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    CAST('Buffer Manager Page Life Expectancy' AS varchar(128)) AS message
FROM sys.dm_os_performance_counters AS p
WHERE p.counter_name = 'Page life expectancy'
  AND p.object_name LIKE '%Buffer Manager%';
