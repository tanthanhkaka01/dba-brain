SELECT
    CAST(id AS CHAR(256)) AS metric_item,
    CAST(time AS CHAR(32)) AS metric_value,
    CAST('seconds' AS CHAR(32)) AS metric_unit,
    CASE WHEN time >= 1800 THEN 'CRITICAL' ELSE 'WARNING' END AS status,
    CONCAT('id=', id, ', user=', user, ', db=', COALESCE(db, ''), ', time_seconds=', time, ', state=', COALESCE(state, '')) AS message
FROM information_schema.processlist
WHERE command <> 'Sleep'
  AND time >= 300
ORDER BY time DESC;
