SELECT 'archiver' AS metric_item, failed_count::text AS metric_value, 'count' AS metric_unit,
       CASE WHEN failed_count > 0 AND last_failed_time > COALESCE(last_archived_time,'epoch'::timestamptz) THEN 'CRITICAL'
            WHEN current_setting('archive_mode') <> 'off' AND last_archived_time IS NULL THEN 'WARNING' ELSE 'OK' END AS status,
       'archive_mode=' || current_setting('archive_mode') || ', archived_count=' || archived_count ||
       ', failed_count=' || failed_count || ', last_archived_time=' || COALESCE(last_archived_time::text,'NULL') ||
       ', last_failed_time=' || COALESCE(last_failed_time::text,'NULL') AS message FROM pg_stat_archiver;
