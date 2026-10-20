-- DATABASE_USER_PERMISSIONS (Oracle 12c+): database account (user) inventory with granted-role and
-- system-privilege counts, and a high-privilege flag. Oracle privileges are database-wide, so
-- this lists each non-Oracle-maintained account with its account status, default tablespace,
-- role count, and system-privilege count. Status WARNING when the account holds the DBA role or
-- a powerful system privilege (GRANT ANY, ALTER ANY TABLE, SELECT ANY TABLE, SYSDBA/SYSOPER, ...)
-- - the Oracle equivalent of the SQL Server high-privilege flag. Requires SELECT on the DBA_*
-- views (SELECT_CATALOG_ROLE); when the monitor user lacks it the metric returns a collection
-- warning rather than partial data.
--
-- Oracle-maintained accounts are left out by dba_users.oracle_maintained (12c+), not by name. The
-- name list the 9i - 11g variant keeps misses every account Oracle added since: on the .251 26ai
-- lab on 2026-10-15 (1.146) GGSHAREDCAP (23ai) and SYSBACKUP (12c) read as WARNING findings -
-- accounts Oracle creates, locks and owns, which no DBA granted anything. The flag is Oracle's
-- own answer, and it moves with each release where a list does not.
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
    WHERE u.oracle_maintained = 'N'
    ORDER BY sort_rank, u.username
)
WHERE ROWNUM <= 200
