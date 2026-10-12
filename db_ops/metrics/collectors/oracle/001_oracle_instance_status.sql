-- INSTANCE_STATUS for Oracle: one row, and in its message what the instance and its database say
-- about themselves (0.28.0. Until then the message held the status and the instance name, beside
-- nineteen facts for SQL Server).
--
-- Only what Oracle can answer, under the names Oracle uses - role, open mode, log mode, character
-- set, RAC. There is no patch level, listener address or authentication scheme to read from
-- these views, so none is written.
--
-- * Columns that exist from 10g on, and nothing newer (no EDITION, DATABASE_TYPE, VERSION_FULL or
--   CDB): one file serves every Oracle that is not 8i. The full release is in the banner.
-- * No value may hold a semicolon or a comma - the report reads the pairs up to either. A 23ai
--   banner ends in: Develop, Learn, and Run for Free - so both characters are taken out of it.
-- * The banner and the character set are joined, not selected as scalar subqueries, and each is
--   an aggregate over its view: there is always exactly one row, with or without a match.
-- * No comment here holds a semicolon or a quote, so no client between this file and the
--   server has to read one correctly to find where the statement ends.
SELECT
    CAST(i.instance_name AS varchar2(256)) AS metric_item,
    CAST(i.status AS varchar2(32)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    CASE WHEN i.status = 'OPEN' THEN 'OK' ELSE 'CRITICAL' END AS status,
    'Oracle instance status=' || i.status
        || '; instance=' || i.instance_name
        || '; machine=' || i.host_name
        || '; version=' || i.version
        || '; edition=' || CASE
               WHEN b.banner LIKE '%Enterprise%' THEN 'Enterprise'
               WHEN b.banner LIKE '%Standard%' THEN 'Standard'
               WHEN b.banner LIKE '%Express%' THEN 'Express'
               WHEN b.banner LIKE '%Free%' THEN 'Free'
               WHEN b.banner LIKE '%Personal%' THEN 'Personal'
               ELSE 'unknown' END
        || '; banner=' || NVL(REPLACE(REPLACE(b.banner, ',', ''), ';', ''), 'unknown')
        || '; database=' || d.name
        || '; unique_name=' || d.db_unique_name
        || '; role=' || d.database_role
        || '; open_mode=' || d.open_mode
        || '; log_mode=' || d.log_mode
        || '; platform=' || d.platform_name
        || '; charset=' || NVL(c.value, 'unknown')
        || '; rac=' || i.parallel
        || '; logins=' || i.logins
        || '; archiver=' || i.archiver
        || '; database_status=' || i.database_status
        || '; instance_role=' || i.instance_role
        || '; started=' || TO_CHAR(i.startup_time, 'YYYY-MM-DD HH24:MI:SS')
        AS message
FROM v$instance i
CROSS JOIN v$database d
LEFT JOIN (SELECT MIN(banner) AS banner FROM v$version WHERE banner LIKE 'Oracle%') b ON 1 = 1
LEFT JOIN (SELECT MIN(value) AS value FROM nls_database_parameters
           WHERE parameter = 'NLS_CHARACTERSET') c ON 1 = 1;
