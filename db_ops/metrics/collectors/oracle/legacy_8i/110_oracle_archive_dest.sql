-- Oracle 8i legacy variant - LOG_FILE_SPACE. Whether redo can still be archived.
--
-- The most dangerous failure this instance has, and the catalog had nothing for it on any Oracle
-- release: when the archive destination cannot be written - disk full, path gone, permission
-- lost - ARCH stops, every online redo group fills, and the database freezes with every session
-- hanging on "log file switch (archiving needed)". No query fails first, no session errors, the
-- instance simply stops accepting work. The modern variant of this metric reads the Fast
-- Recovery Area, which does not exist before 10g, which is why 8i needed its own.
--
-- Three things are reported, and each is a separate row so a report can show which one failed:
--   log_mode           the database is in ARCHIVELOG at all (NOARCHIVELOG is a valid choice, but
--                      it means no point-in-time recovery, so it is stated, not assumed)
--   archive_dest_N     each enabled destination and whether ARCH last succeeded on it
--   unarchived_logs    online redo groups already filled but not yet archived - the backlog that
--                      turns into a freeze when it reaches the number of groups
--
-- v$archive_dest.status is VALID while writing normally; anything else with a non-null ERROR is
-- ARCH failing right now. In NOARCHIVELOG mode the destinations are inactive and reporting them
-- as failures would be noise, so they are skipped by the WHERE clause.
SELECT
    'log_mode' AS metric_item,
    log_mode AS metric_value,
    'mode' AS metric_unit,
    'OK' AS status,
    'database log_mode=' || log_mode ||
        DECODE(log_mode, 'NOARCHIVELOG',
               '; no archiving, so no point-in-time recovery from this instance.',
               '; redo is being archived.') AS message
FROM v$database
UNION ALL
SELECT
    'archive_dest_' || TO_CHAR(dest_id) AS metric_item,
    status AS metric_value,
    'state' AS metric_unit,
    CASE WHEN status = 'VALID' THEN 'OK' ELSE 'CRITICAL' END AS status,
    'destination=' || SUBSTR(destination, 1, 200) ||
        ', status=' || status ||
        ', binding=' || binding ||
        ', error=' || NVL(SUBSTR(error, 1, 200), 'none') ||
        DECODE(status, 'VALID', '',
               '; ARCH cannot write here - when the online redo groups fill, the instance stops.')
        AS message
FROM v$archive_dest
WHERE destination IS NOT NULL
UNION ALL
-- The backlog is only a signal in ARCHIVELOG mode. In NOARCHIVELOG every non-current group reads
-- archived='NO' by definition - nothing is ever archived - so evaluating the count there reported
-- a permanent WARNING about a backlog that does not exist (measured on 2.236, run 28781). The
-- mode is joined in rather than assumed, and the row still reports the count so the reading is
-- visible either way.
SELECT
    'unarchived_logs' AS metric_item,
    TO_CHAR(g.pending) AS metric_value,
    'groups' AS metric_unit,
    CASE
        WHEN d.log_mode <> 'ARCHIVELOG' THEN 'OK'
        WHEN g.pending >= 3 THEN 'CRITICAL'
        WHEN g.pending >= 2 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'redo groups filled but not archived=' || g.pending ||
        DECODE(d.log_mode, 'ARCHIVELOG',
               '; a backlog that reaches the number of groups freezes the instance.',
               '; not a backlog - this database is in ' || d.log_mode ||
               ', where no group is ever archived.') AS message
FROM (SELECT COUNT(*) AS pending FROM v$log WHERE archived = 'NO' AND status <> 'CURRENT') g,
     v$database d;
