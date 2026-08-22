SELECT
    pid::text AS metric_item,
    EXTRACT(EPOCH FROM (now() - query_start))::bigint::text AS metric_value,
    'seconds' AS metric_unit,
    CASE WHEN EXTRACT(EPOCH FROM (now() - query_start)) >= 1800 THEN 'CRITICAL' ELSE 'WARNING' END AS status,
    'pid=' || pid
        || ', user=' || coalesce(usename, '')
        || ', db=' || coalesce(datname, '')
        || ', seconds=' || EXTRACT(EPOCH FROM (now() - query_start))::bigint
        || ', state=' || coalesce(state, '')
        || ', wait_event=' || coalesce(wait_event_type, '') AS message
FROM pg_stat_activity
WHERE state = 'active'
  AND query_start IS NOT NULL
  AND now() - query_start >= interval '300 seconds'
  AND pid <> pg_backend_pid()
  AND backend_type = 'client backend'
ORDER BY query_start;
