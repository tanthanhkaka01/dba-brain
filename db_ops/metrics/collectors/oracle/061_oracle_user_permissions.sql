-- DATABASE_USER_PERMISSIONS (Oracle): database account (user) inventory with granted-role and
-- system-privilege counts, and a high-privilege flag. Oracle privileges are database-wide, so
-- this lists each non-Oracle-maintained account with its account status, default tablespace,
-- role count, and system-privilege count. Status WARNING when the account holds the DBA role or
-- a powerful system privilege (GRANT ANY, ALTER ANY TABLE, SELECT ANY TABLE, SYSDBA/SYSOPER, ...)
-- - the Oracle equivalent of the SQL Server high-privilege flag. Requires SELECT on the DBA_*
-- views (SELECT_CATALOG_ROLE); when the monitor user lacks it the metric returns a collection
-- warning rather than partial data.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        CAST(u.username AS varchar2(400)) AS metric_item,
        CAST(u.account_status AS varchar2(64)) AS metric_value,
        CAST(NULL AS varchar2(32)) AS metric_unit,
        CASE WHEN (SELECT COUNT(*) FROM dba_role_privs rp
                   WHERE rp.grantee = u.username AND rp.granted_role = 'DBA') > 0
               OR (SELECT COUNT(*) FROM dba_sys_privs sp
                   WHERE sp.grantee = u.username
                     AND sp.privilege IN ('GRANT ANY PRIVILEGE','GRANT ANY ROLE','ALTER ANY TABLE',
                                          'CREATE ANY PROCEDURE','SELECT ANY TABLE','SYSDBA','SYSOPER',
                                          'CREATE ANY TABLE','DROP ANY TABLE')) > 0
             THEN 'WARNING' ELSE 'OK' END AS status,
        CAST('account=' || u.account_status
             || ' default_ts=' || u.default_tablespace
             || ' roles=' || TO_CHAR((SELECT COUNT(*) FROM dba_role_privs rp WHERE rp.grantee = u.username))
             || ' sys_privs=' || TO_CHAR((SELECT COUNT(*) FROM dba_sys_privs sp WHERE sp.grantee = u.username))
             AS varchar2(1000)) AS message,
        CASE WHEN (SELECT COUNT(*) FROM dba_role_privs rp
                   WHERE rp.grantee = u.username AND rp.granted_role = 'DBA') > 0
             THEN 0 ELSE 1 END AS sort_rank
    FROM dba_users u
    WHERE u.username NOT IN (
        'SYS','SYSTEM','DBSNMP','OUTLN','XDB','CTXSYS','MDSYS','ORDSYS','ORDPLUGINS','APPQOSSYS',
        'GSMADMIN_INTERNAL','AUDSYS','WMSYS','OLAPSYS','ANONYMOUS','APEX_PUBLIC_USER','FLOWS_FILES',
        'SI_INFORMTN_SCHEMA','DIP','ORACLE_OCM','REMOTE_SCHEDULER_AGENT','LBACSYS','DVSYS','DVF')
    ORDER BY sort_rank, u.username
)
WHERE ROWNUM <= 200
