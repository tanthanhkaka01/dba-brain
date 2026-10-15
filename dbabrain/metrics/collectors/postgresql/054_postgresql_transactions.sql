SELECT COALESCE(datname,'') || ':' || pid AS metric_item,
       EXTRACT(EPOCH FROM (clock_timestamp()-xact_start))::bigint::text AS metric_value, 'seconds' AS metric_unit,
       CASE WHEN clock_timestamp()-xact_start >= interval '30 minutes' THEN 'CRITICAL' ELSE 'WARNING' END AS status,
       'state=' || COALESCE(state,'') || ', user=' || COALESCE(usename,'') ||
       ', transaction_seconds=' || EXTRACT(EPOCH FROM (clock_timestamp()-xact_start))::bigint AS message
FROM pg_stat_activity WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid()
  AND clock_timestamp()-xact_start >= interval '5 minutes' ORDER BY xact_start LIMIT 20;
