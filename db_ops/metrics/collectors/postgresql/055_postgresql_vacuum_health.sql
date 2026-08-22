SELECT schemaname || '.' || relname AS metric_item, n_dead_tup::text AS metric_value, 'count' AS metric_unit,
       CASE WHEN n_dead_tup >= 1000000 THEN 'CRITICAL' WHEN n_dead_tup >= 100000 THEN 'WARNING' ELSE 'OK' END AS status,
       'live_tuples=' || n_live_tup || ', dead_tuples=' || n_dead_tup ||
       ', last_autovacuum=' || COALESCE(last_autovacuum::text,'NULL') ||
       ', last_autoanalyze=' || COALESCE(last_autoanalyze::text,'NULL') AS message
FROM pg_stat_user_tables WHERE n_dead_tup >= 10000 OR last_autovacuum IS NULL
ORDER BY n_dead_tup DESC LIMIT 20;
