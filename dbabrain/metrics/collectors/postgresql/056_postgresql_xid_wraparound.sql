SELECT datname AS metric_item, age(datfrozenxid)::text AS metric_value, 'transactions' AS metric_unit,
       CASE WHEN age(datfrozenxid) >= current_setting('autovacuum_freeze_max_age')::bigint * 0.9 THEN 'CRITICAL'
            WHEN age(datfrozenxid) >= current_setting('autovacuum_freeze_max_age')::bigint * 0.75 THEN 'WARNING' ELSE 'OK' END AS status,
       'xid_age=' || age(datfrozenxid) || ', freeze_max_age=' || current_setting('autovacuum_freeze_max_age') ||
       ', percent_consumed=' || round(age(datfrozenxid)::numeric/current_setting('autovacuum_freeze_max_age')::numeric*100,2) AS message
FROM pg_database WHERE datallowconn ORDER BY age(datfrozenxid) DESC;
