SELECT
    CAST('availability_group' AS varchar(256)) AS metric_item,
    CAST('NOT_SUPPORTED' AS varchar(64)) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    'OK' AS status,
    'Availability Groups are not supported on SQL Server 2008 R2.' AS message;
