SELECT
    'instance' AS metric_item,
    'ONLINE' AS metric_value,
    NULL::text AS metric_unit,
    'OK' AS status,
    'PostgreSQL connection available. version=' || current_setting('server_version')
        || ', role=' || CASE WHEN pg_is_in_recovery() THEN 'standby' ELSE 'primary' END
        || ', uptime_seconds=' || EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))::bigint AS message;
