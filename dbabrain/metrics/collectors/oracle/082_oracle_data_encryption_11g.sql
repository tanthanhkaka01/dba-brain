-- SECURITY_DATA_ENCRYPTION (Oracle 11g): the 12c+ file without what 11g does not have -
-- v$encryption_wallet.wallet_type and con_id. Tablespace encryption exists from 11.1, so this is
-- the oldest release with anything to read; the wallet's kind (auto-login or not) cannot be read
-- from SQL here and is said so: `opens_without_password=unknown`.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT 'wallet' || CASE WHEN ROWNUM > 1 THEN ' ' || ROWNUM ELSE '' END AS metric_item,
           NVL(w.status, 'UNKNOWN') AS metric_value,
           'keystore' AS metric_unit,
           'OK' AS status,
           SUBSTR('engine=oracle'
                  || ', wallet_type=UNKNOWN'
                  || ', wrl_type=' || NVL(w.wrl_type, '-')
                  || ', location=' || NVL(REPLACE(w.wrl_parameter, ',', ';'), '-')
                  || ', opens_without_password=unknown', 1, 1000) AS message,
           0 AS sort_rank
    FROM v$encryption_wallet w

    UNION ALL

    SELECT 'tablespace:' || t.tablespace_name AS metric_item,
           CASE WHEN t.encrypted = 'YES' THEN 'encrypted' ELSE 'not encrypted' END AS metric_value,
           'tde' AS metric_unit,
           'OK' AS status,
           SUBSTR('engine=oracle'
                  || ', contents=' || t.contents
                  || ', status=' || t.status
                  || ', algorithm=' || NVL(e.encryptionalg, '-'), 1, 1000) AS message,
           1 AS sort_rank
    FROM dba_tablespaces t
    LEFT JOIN (
        SELECT vt.name, ve.encryptionalg
        FROM v$encrypted_tablespaces ve
        JOIN v$tablespace vt ON vt.ts# = ve.ts#
    ) e ON e.name = t.tablespace_name
    WHERE t.contents = 'PERMANENT'
)
ORDER BY sort_rank, metric_item
