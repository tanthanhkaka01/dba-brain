-- DATABASE_STATUS (Oracle 9i-11g non-CDB variant) - database availability.
--
-- For Oracle versions prior to 12c multitenant architecture (9i, 10g, 11g):
--   * v$pdbs does not exist on 11g and older instances; this variant queries v$database without referencing multitenant views.
--   * Evaluates database open_mode and role:
--       - On PRIMARY: open_mode = 'READ WRITE' is OK; 'READ ONLY%' is WARNING; otherwise CRITICAL.
--       - On Data Guard standby (database_role <> 'PRIMARY'): MOUNTED or READ ONLY is normal and graded OK.
--   * Reports session container from SYS_CONTEXT('USERENV', 'CON_NAME') (NULL/defaulting to database name on 11g).

SELECT
    CAST(d.name AS VARCHAR2(256)) AS metric_item,
    CAST(d.open_mode AS VARCHAR2(32)) AS metric_value,
    CAST(NULL AS VARCHAR2(32)) AS metric_unit,
    CASE
        WHEN d.database_role <> 'PRIMARY'
             AND (d.open_mode LIKE 'READ ONLY%' OR d.open_mode = 'MOUNTED')
            THEN 'OK'
        WHEN d.open_mode = 'READ WRITE'
            THEN 'OK'
        WHEN d.open_mode LIKE 'READ ONLY%'
            THEN 'WARNING'
        ELSE 'CRITICAL'
    END AS status,
    'container_type=NON-CDB' ||
    ', database=' || d.name ||
    ', container=' || NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), d.name) ||
    ', session_container=' || NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), d.name) ||
    ', open_mode=' || d.open_mode ||
    ', log_mode=' || d.log_mode ||
    ', database_role=' || d.database_role ||
    ', compatible=' || NVL((SELECT MAX(value) FROM v$parameter WHERE name = 'compatible'), 'unknown') AS message
FROM v$database d;
