-- LOG_FILE_SPACE (Oracle variant) - archive destination health from v$archive_dest.
--
-- Guards against silent archiving failures where an archive destination is configured
-- (destination IS NOT NULL) but cannot be written to (status is not VALID).
-- When archive destinations fail, online redo log groups cannot be archived and the
-- database halts with "log file switch (archiving needed)" once all groups fill.
--
-- Status values:
--   * VALID    -> destination is enabled and active.
--   * INACTIVE -> destination is unused or unconfigured; not a failure.
--   * DEFERRED -> destination was administratively disabled; not a failure.
--   * ERROR / other non-VALID -> destination failed; graded CRITICAL with error in message.
--
-- Inside a Pluggable Database (PDB) session in Oracle 12c through 26ai, v$archive_dest
-- may expose CDB destinations or return 0 rows. An empty result in a PDB is expected
-- and not a failure.
SELECT
    CAST('archive_dest_' || TO_CHAR(dest_id) AS varchar2(256)) AS metric_item,
    CAST(status AS varchar2(32)) AS metric_value,
    CAST('state' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN status = 'VALID' THEN 'OK'
        WHEN status IN ('INACTIVE', 'DEFERRED') THEN 'OK'
        ELSE 'CRITICAL'
    END AS status,
    'destination=' || SUBSTR(destination, 1, 200) ||
        ', status=' || status ||
        ', binding=' || binding ||
        ', error=' || NVL(SUBSTR(error, 1, 200), 'none') ||
        CASE
            WHEN status = 'VALID' OR status IN ('INACTIVE', 'DEFERRED') THEN ''
            ELSE '; ARCH cannot write here - when online redo groups fill, the instance stops.'
        END AS message
FROM v$archive_dest
WHERE destination IS NOT NULL;
