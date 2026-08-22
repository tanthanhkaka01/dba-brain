SELECT
    CAST(CONCAT(r.trx_mysql_thread_id, ' waiting for ', b.trx_mysql_thread_id) AS CHAR(256)) AS metric_item,
    CAST(TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS CHAR(32)) AS metric_value,
    CAST('seconds' AS CHAR(32)) AS metric_unit,
    CASE
        WHEN TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) >= 300 THEN 'CRITICAL'
        ELSE 'WARNING'
    END AS status,
    CONCAT(
        'waiting_thread=', r.trx_mysql_thread_id,
        ', blocking_thread=', b.trx_mysql_thread_id,
        ', wait_seconds=', TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW())
    ) AS message
FROM information_schema.innodb_lock_waits w
JOIN information_schema.innodb_trx b
    ON b.trx_id = w.blocking_trx_id
JOIN information_schema.innodb_trx r
    ON r.trx_id = w.requesting_trx_id
ORDER BY TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) DESC;
