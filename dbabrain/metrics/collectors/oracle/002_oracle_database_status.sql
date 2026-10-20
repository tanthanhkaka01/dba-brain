-- DATABASE_STATUS (Oracle 12c+ multitenant variant) - CDB and PDB availability.
--
-- Multitenant-aware availability check for Oracle 12c through 26ai:
--   * Evaluates CDB root (or the connected container) and all visible PDBs from v$pdbs.
--   * When connected to CDB$ROOT, v$pdbs exposes all pluggable databases.
--   * When connected inside a PDB (e.g. FREEPDB1), v$pdbs exposes only that PDB; grades what is visible.
--   * On a PRIMARY database:
--       - Any PDB not open READ WRITE (except PDB$SEED) is CRITICAL.
--       - PDB$SEED is the template container and is expected to be READ ONLY; graded OK.
--   * On a Data Guard standby (v$database.database_role <> 'PRIMARY'):
--       - PDBs in MOUNTED or READ ONLY (Active Data Guard) state are normal and expected; graded OK.
--   * Every row names the session's connected container via SYS_CONTEXT('USERENV', 'CON_NAME').

SELECT
    CAST(metric_item AS VARCHAR2(256)) AS metric_item,
    CAST(metric_value AS VARCHAR2(32)) AS metric_value,
    CAST(NULL AS VARCHAR2(32)) AS metric_unit,
    status,
    message
FROM
(
    /* CDB root or current container */
    SELECT
        0 AS sort_order,
        d.name AS metric_item,
        d.open_mode AS metric_value,
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
        'container_type=' || CASE WHEN SYS_CONTEXT('USERENV', 'CON_NAME') IN ('CDB$ROOT', NULL) THEN 'CDB' ELSE 'PDB' END ||
        ', database=' || d.name ||
        ', container=' || NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), 'CDB$ROOT') ||
        ', session_container=' || NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), 'unknown') ||
        ', open_mode=' || d.open_mode ||
        ', log_mode=' || d.log_mode ||
        ', database_role=' || d.database_role ||
        ', compatible=' || NVL((SELECT MAX(value) FROM v$parameter WHERE name = 'compatible'), 'unknown') AS message
    FROM v$database d

    UNION ALL

    /* Visible PDBs (all PDBs from CDB root; connected PDB from a PDB session) */
    SELECT
        p.con_id AS sort_order,
        p.name AS metric_item,
        p.open_mode AS metric_value,
        CASE
            WHEN p.name = 'PDB$SEED'
                 AND p.open_mode LIKE 'READ ONLY%'
                THEN 'OK'
            WHEN d.database_role <> 'PRIMARY'
                 AND (p.open_mode LIKE 'READ ONLY%' OR p.open_mode = 'MOUNTED')
                THEN 'OK'
            WHEN p.open_mode = 'READ WRITE'
                THEN 'OK'
            ELSE 'CRITICAL'
        END AS status,
        'container_type=PDB' ||
        ', database=' || d.name ||
        ', container=' || p.name ||
        ', session_container=' || NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), 'unknown') ||
        ', con_id=' || p.con_id ||
        ', open_mode=' || p.open_mode ||
        ', restricted=' || p.restricted ||
        ', log_mode=' || d.log_mode ||
        ', database_role=' || d.database_role ||
        ', compatible=' || NVL((SELECT MAX(value) FROM v$parameter WHERE name = 'compatible'), 'unknown') AS message
    FROM v$pdbs p
    CROSS JOIN v$database d
)
ORDER BY sort_order;