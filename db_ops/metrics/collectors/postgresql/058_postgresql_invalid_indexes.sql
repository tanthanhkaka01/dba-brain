SELECT schemaname || '.' || indexrelname AS metric_item, '0' AS metric_value, 'boolean' AS metric_unit,
       'CRITICAL' AS status, 'index is invalid or not ready; table=' || schemaname || '.' || relname AS message
FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid=s.indexrelid
WHERE NOT i.indisvalid OR NOT i.indisready ORDER BY schemaname,indexrelname LIMIT 50;
