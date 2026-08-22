SELECT
    (blocked.pid || ' blocked by ' || bl.blocking_pid)::text AS metric_item,
    EXTRACT(EPOCH FROM (now() - blocked.query_start))::bigint::text AS metric_value,
    'seconds' AS metric_unit,
    CASE
        WHEN EXTRACT(EPOCH FROM (now() - blocked.query_start)) >= 300 THEN 'CRITICAL'
        ELSE 'WARNING'
    END AS status,
    'blocked_pid=' || blocked.pid
        || ', blocking_pid=' || bl.blocking_pid
        || ', blocked_user=' || coalesce(blocked.usename, '')
        || ', wait_seconds=' || EXTRACT(EPOCH FROM (now() - blocked.query_start))::bigint
        || ', wait_event=' || coalesce(blocked.wait_event_type, '') AS message
FROM pg_stat_activity AS blocked
CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bl(blocking_pid)
ORDER BY blocked.query_start;
