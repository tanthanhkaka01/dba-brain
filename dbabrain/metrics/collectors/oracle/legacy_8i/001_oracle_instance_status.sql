-- Oracle 8i (8.1.7) legacy variant - INSTANCE_STATUS.
-- Validated views: v$instance, v$database, v$version, nls_database_parameters - run against both
-- 8.1.7.0.0 instances of the estate through the bridge on 2026-10-09 (0.28.0).
--
-- The message keeps its first four pairs as they were (instance, status, version, host) and adds
-- what 8i can answer. Written for 8i: an old-style join, DECODE for the edition, no column 9i
-- added. A Standard Edition banner names no edition at all (Oracle8i Release 8.1.7.0.0), so
-- Oracle without Enterprise or Personal is read as Standard.
-- No comment here holds a semicolon or a quote, so no client between this file and the server
-- has to read one correctly to find where the statement ends.
SELECT
    i.instance_name AS metric_item,
    i.status AS metric_value,
    NULL AS metric_unit,
    CASE WHEN i.status = 'OPEN' THEN 'OK' ELSE 'CRITICAL' END AS status,
    'instance=' || i.instance_name || ', status=' || i.status ||
        ', version=' || i.version || ', host=' || i.host_name ||
        ', machine=' || i.host_name ||
        ', edition=' || DECODE(SIGN(INSTR(b.banner, 'Enterprise')), 1, 'Enterprise',
                        DECODE(SIGN(INSTR(b.banner, 'Personal')), 1, 'Personal',
                        DECODE(SIGN(INSTR(b.banner, 'Oracle')), 1, 'Standard', 'unknown'))) ||
        ', banner=' || NVL(REPLACE(REPLACE(b.banner, ',', ''), ';', ''), 'unknown') ||
        ', database=' || d.name ||
        ', open_mode=' || d.open_mode ||
        ', log_mode=' || d.log_mode ||
        ', charset=' || NVL(c.value, 'unknown') ||
        ', rac=' || i.parallel ||
        ', logins=' || i.logins ||
        ', archiver=' || i.archiver ||
        ', started=' || TO_CHAR(i.startup_time, 'YYYY-MM-DD HH24:MI:SS') AS message
FROM v$instance i,
     v$database d,
     (SELECT MIN(banner) AS banner FROM v$version WHERE banner LIKE 'Oracle%') b,
     (SELECT MIN(value) AS value FROM nls_database_parameters
      WHERE parameter = 'NLS_CHARACTERSET') c;
