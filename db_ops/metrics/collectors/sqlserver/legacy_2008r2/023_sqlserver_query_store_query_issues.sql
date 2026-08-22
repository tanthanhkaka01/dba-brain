SELECT
    CAST('query_store_heavy_or_regressed' AS varchar(256)) AS metric_item,
    CAST('0' AS varchar(32)) AS metric_value,
    CAST('queries' AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(32)) AS status,
    CAST(
        'query_store_status=NOT_SUPPORTED_SQL_SERVER_2008_R2'
        + ', checked_window=last_6_hours'
        + ', message=Query Store is only available from SQL Server 2016+'
        AS varchar(4000)
    ) AS message;