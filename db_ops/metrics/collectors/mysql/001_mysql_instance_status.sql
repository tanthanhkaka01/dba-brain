SELECT
    CAST(@@hostname AS CHAR(256)) AS metric_item,
    CAST('ONLINE' AS CHAR(32)) AS metric_value,
    CAST(NULL AS CHAR(32)) AS metric_unit,
    'OK' AS status,
    CONCAT('MySQL connection is available. host=', @@hostname, ', version=', @@version) AS message;
