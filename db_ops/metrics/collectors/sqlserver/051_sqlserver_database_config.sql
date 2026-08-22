-- DATABASE_CONFIG: per-database recovery model, compatibility level, page verify,
-- read-only and state for the inventory database_health block. Logging only
-- (status always OK, no alert). DATABASE_STATUS already covers state/read_only for
-- alerting; this metric adds the configuration detail not collected elsewhere.
SELECT
    d.name COLLATE DATABASE_DEFAULT AS metric_item,
    d.recovery_model_desc COLLATE DATABASE_DEFAULT AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    (
        'compatibility_level=' + CAST(d.compatibility_level AS varchar(10)) +
        ', page_verify=' + d.page_verify_option_desc COLLATE DATABASE_DEFAULT +
        ', is_read_only=' + CAST(d.is_read_only AS varchar(1)) +
        ', state=' + d.state_desc COLLATE DATABASE_DEFAULT +
        -- The order an operator expects a database list in: master, tempdb, model, msdb, then
        -- user databases in the order they were created. Carried here rather than on
        -- DATABASE_STATUS because that metric selects `database_id > 4` and so has no id for the
        -- four system databases — which are exactly the rows whose position this decides.
        ', database_id=' + CAST(d.database_id AS varchar(10))
    ) AS message
FROM sys.databases AS d
ORDER BY d.database_id;
