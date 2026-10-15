SELECT d.datname AS metric_item, pg_database_size(d.datname)::text AS metric_value, 'bytes' AS metric_unit,
       CASE WHEN NOT p.datallowconn THEN 'WARNING' ELSE 'OK' END AS status,
       'connections=' || d.numbackends || ', commits=' || d.xact_commit || ', rollbacks=' || d.xact_rollback ||
       ', deadlocks=' || d.deadlocks || ', temp_bytes=' || d.temp_bytes || ', conflicts=' || d.conflicts ||
       ', stats_reset=' || COALESCE(d.stats_reset::text,'NULL') AS message
FROM pg_stat_database d JOIN pg_database p ON p.datname=d.datname
WHERE NOT p.datistemplate ORDER BY pg_database_size(d.datname) DESC;
