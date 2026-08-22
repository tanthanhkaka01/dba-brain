-- SQL_CONFIGURATION: instance-level sp_configure values for the inventory
-- config_warnings block. Logging only (status always OK, no alert) — the values
-- are collected so the inventory/report can derive warnings.
SELECT
    c.name COLLATE DATABASE_DEFAULT AS metric_item,
    CAST(c.value_in_use AS varchar(32)) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    CAST(c.description AS varchar(256)) AS message
FROM sys.configurations AS c
WHERE c.name IN (
    'max degree of parallelism',
    'cost threshold for parallelism',
    'min server memory (MB)',
    'max server memory (MB)',
    'backup compression default',
    'optimize for ad hoc workloads',
    'remote admin connections',
    'blocked process threshold (s)',
    'clr enabled',
    'xp_cmdshell',
    'max worker threads'
)
ORDER BY c.name;
