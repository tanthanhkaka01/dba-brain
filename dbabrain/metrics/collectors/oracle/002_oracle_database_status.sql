SELECT
    CAST(metric_item AS VARCHAR2(256)) AS metric_item,
    CAST(metric_value AS VARCHAR2(32)) AS metric_value,
    CAST(NULL AS VARCHAR2(32)) AS metric_unit,
    status,
    message
FROM
(
    /* CDB */
    SELECT
        0 AS sort_order,
        d.name AS metric_item,
        d.open_mode AS metric_value,
        CASE
            WHEN d.open_mode = 'READ WRITE' THEN 'OK'
            WHEN d.open_mode LIKE 'READ ONLY%' THEN 'WARNING'
            ELSE 'CRITICAL'
        END AS status,
        'container_type=CDB' ||
        ', database=' || d.name ||
        ', container=CDB$ROOT' ||
        ', open_mode=' || d.open_mode ||
        ', log_mode=' || d.log_mode ||
        ', database_role=' || d.database_role ||
        -- COMPATIBLE init parameter, the Oracle counterpart of SQL Server's
        -- compatibility_level (see sqlserver/002_sqlserver_database_status.sql). It gates which
        -- features and file formats the database may use and is what an upgrade must raise
        -- separately from the binaries, so an instance can run 23.x software while still
        -- restricted to an older compatible level. MAX() keeps it a single-row scalar
        -- regardless of how many containers v$parameter exposes to this session.
        ', compatible=' || NVL((SELECT MAX(value) FROM v$parameter WHERE name = 'compatible'), 'unknown') AS message
    FROM v$database d

    UNION ALL

    /* All PDBs, including PDB$SEED and closed/mounted PDBs */
    SELECT
        p.con_id AS sort_order,
        p.name AS metric_item,
        p.open_mode AS metric_value,
        CASE
            WHEN p.name = 'PDB$SEED'
                 AND p.open_mode LIKE 'READ ONLY%'
                THEN 'OK'
            WHEN p.open_mode = 'READ WRITE'
                THEN 'OK'
            WHEN p.open_mode LIKE 'READ ONLY%'
                THEN 'WARNING'
            WHEN p.open_mode = 'MOUNTED'
                THEN 'WARNING'
            ELSE 'CRITICAL'
        END AS status,
        'container_type=PDB' ||
        ', database=' || d.name ||
        ', container=' || p.name ||
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