SELECT
    CAST(event_name AS CHAR(256)) AS metric_item,
    CAST(ROUND(sum_timer_wait / 1000000000000, 2) AS CHAR(32)) AS metric_value,
    CAST('seconds' AS CHAR(32)) AS metric_unit,
    'OK' AS status,
    CONCAT('event=', event_name, ', count=', count_star, ', wait_seconds=', ROUND(sum_timer_wait / 1000000000000, 2)) AS message
FROM performance_schema.events_waits_summary_global_by_event_name
WHERE count_star > 0
  AND event_name NOT LIKE 'idle%'
ORDER BY sum_timer_wait DESC
LIMIT 20;
