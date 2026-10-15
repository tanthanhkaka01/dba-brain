SELECT 'role' AS metric_item,
       CASE WHEN pg_is_in_recovery() THEN 'standby' ELSE 'primary' END AS metric_value,
       NULL::text AS metric_unit, 'OK' AS status,
       'version_num=' || current_setting('server_version_num') || ', database=' || current_database() ||
       ', read_only=' || current_setting('transaction_read_only') ||
       ', uptime_seconds=' || EXTRACT(EPOCH FROM (clock_timestamp()-pg_postmaster_start_time()))::bigint AS message;
