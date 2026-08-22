SELECT
    'server' AS metric_item,
    count(*)::text AS metric_value,
    'sessions' AS metric_unit,
    CASE
        WHEN count(*) >= 0.90 * current_setting('max_connections')::int THEN 'CRITICAL'
        WHEN count(*) >= 0.75 * current_setting('max_connections')::int THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'active_connections=' || count(*)
        || ', max_connections=' || current_setting('max_connections')
        || ', running_queries=' || sum(CASE WHEN state = 'active' THEN 1 ELSE 0 END)
        || ', idle_in_transaction=' || sum(CASE WHEN state = 'idle in transaction' THEN 1 ELSE 0 END) AS message
FROM pg_stat_activity
WHERE pid <> pg_backend_pid();
