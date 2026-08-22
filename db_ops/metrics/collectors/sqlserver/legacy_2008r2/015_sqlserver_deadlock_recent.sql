SELECT
    CAST('deadlock' AS varchar(256)) AS metric_item,
    CAST('0' AS varchar(32)) AS metric_value,
    CAST('deadlocks_24h' AS varchar(32)) AS metric_unit,
    'OK' AS status,
    'Deadlock metric is not collected on SQL Server 2008 R2 by this legacy script.' AS message;
