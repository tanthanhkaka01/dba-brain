-- SECURITY_DATA_ENCRYPTION (Oracle 12c+): are the datafiles encrypted at rest (TDE), and what
-- opens them?
--
-- Oracle is no different from the other engines by default: a datafile opens on another install.
-- With TDE a tablespace's blocks are encrypted by a key kept in the keystore (the wallet), so the
-- answer has two halves and both are read where the engine says them:
--
--   * `wallet` - one row per keystore of this container, from v$encryption_wallet: its status
--     (OPEN / CLOSED / NOT_AVAILABLE ...), its type and where it is. An AUTOLOGIN or
--     LOCAL_AUTOLOGIN wallet in a FILE location opens with no password, on the database host:
--     whoever copies the datafiles from that host can copy the wallet beside them, so the message
--     says `opens_without_password=yes` and the report names it.
--   * `tablespace:<name>` - one row per permanent tablespace, from dba_tablespaces.encrypted, with
--     its algorithm from v$encrypted_tablespaces.
--
-- STATUS IS ALWAYS OK - a reading, not a grade (see the SQL Server variant). metric_value is
-- `encrypted` / `not encrypted` for a tablespace, and the keystore's own status for the wallet.
--
-- con_id: connected to a PDB the v$ views show that container only; connected to CDB$ROOT they
-- show every container, while dba_tablespaces is the root's own - so both v$ reads are held to
-- the connection's container (0 in a non-CDB).
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT 'wallet' || CASE WHEN ROWNUM > 1 THEN ' ' || ROWNUM ELSE '' END AS metric_item,
           NVL(w.status, 'UNKNOWN') AS metric_value,
           'keystore' AS metric_unit,
           'OK' AS status,
           SUBSTR('engine=oracle'
                  || ', wallet_type=' || NVL(w.wallet_type, 'UNKNOWN')
                  || ', wrl_type=' || NVL(w.wrl_type, '-')
                  || ', location=' || NVL(REPLACE(w.wrl_parameter, ',', ';'), '-')
                  || ', opens_without_password='
                  || CASE WHEN UPPER(w.wallet_type) IN ('AUTOLOGIN', 'LOCAL_AUTOLOGIN') THEN 'yes'
                          WHEN w.wallet_type IS NULL OR UPPER(w.wallet_type) = 'UNKNOWN' THEN 'unknown'
                          ELSE 'no' END, 1, 1000) AS message,
           0 AS sort_rank
    FROM v$encryption_wallet w
    WHERE w.con_id IN (0, TO_NUMBER(SYS_CONTEXT('USERENV', 'CON_ID')))

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
        JOIN v$tablespace vt ON vt.ts# = ve.ts# AND vt.con_id = ve.con_id
        WHERE ve.con_id IN (0, TO_NUMBER(SYS_CONTEXT('USERENV', 'CON_ID')))
    ) e ON e.name = t.tablespace_name
    WHERE t.contents = 'PERMANENT'
)
ORDER BY sort_rank, metric_item
