SELECT slot_name AS metric_item,
       COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn),0)::bigint::text AS metric_value,
       'bytes' AS metric_unit,
       CASE WHEN NOT active AND COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn),0) >= 10737418240 THEN 'CRITICAL'
            WHEN NOT active OR COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn),0) >= 1073741824 THEN 'WARNING'
            ELSE 'OK' END AS status,
       'slot_type=' || slot_type || ', active=' || active || ', database=' || COALESCE(database,'') ||
       ', retained_wal_bytes=' || COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn),0)::bigint AS message
FROM pg_replication_slots WHERE NOT pg_is_in_recovery()
ORDER BY COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn),0) DESC LIMIT 20;
