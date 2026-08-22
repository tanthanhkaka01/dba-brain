-- INDEX_INVENTORY (Oracle, every release) - every index on a user table, with the facts Oracle
-- can state without being asked to start recording something first.
--
-- This is deliberately **not** MAINTENANCE_INDEX_USAGE. SQL Server keeps per-index seek/scan
-- counters for free in a DMV; Oracle keeps none unless each index is individually put into
-- ALTER INDEX ... MONITORING USAGE, which is a change to the database, not to the monitor. Filing
-- Oracle rows under the usage metric would produce a page full of zero-seek indexes that look
-- unused and are not - the exact reading that gets an index dropped and a report query broken.
-- So the rows say what is true here: what exists, how big it is, whether it still works, and how
-- old its statistics are.
--
-- Field names match the SQL Server variant where the meaning matches (`type_desc`, `is_unique`,
-- `is_primary_key`, `is_disabled`, `last_stats_update`) so one report renderer lays out both
-- engines; the usage columns are absent rather than filled with zeros.
--
-- `is_disabled` is UNUSABLE, Oracle's equivalent state: definition kept, structure gone, queries
-- stop using it and DML on the table raises ORA-01502. Status stays OK on every row - INDEX_UNUSABLE
-- is the metric that alerts on that, and two metrics alerting on one fact is how an estate learns
-- to mute both.
--
-- The first row is a summary (metric_unit = 'summary') so the report has its totals without
-- adding up several hundred detail rows. It is ordered first and the ROWNUM cap is applied after
-- the sort, so the cap can never be what removes it.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
        SELECT
            CAST('summary' AS varchar2(400)) AS metric_item,
            TO_CHAR(COUNT(*)) AS metric_value,
            CAST('summary' AS varchar2(32)) AS metric_unit,
            CAST('OK' AS varchar2(16)) AS status,
            CAST('indexes_total=' || TO_CHAR(COUNT(*)) ||
                ', unusable=' || TO_CHAR(SUM(DECODE(i.status, 'UNUSABLE', 1, 0))) ||
                ', unique_indexes=' || TO_CHAR(SUM(DECODE(i.uniqueness, 'UNIQUE', 1, 0))) ||
                ', never_analyzed=' || TO_CHAR(SUM(DECODE(i.last_analyzed, NULL, 1, 0))) ||
                ', stale_stats_30d=' || TO_CHAR(SUM(CASE WHEN i.last_analyzed IS NOT NULL
                                                          AND SYSDATE - i.last_analyzed > 30
                                                         THEN 1 ELSE 0 END))
                AS varchar2(4000)) AS message,
            0 AS sort_key,
            ' ' AS sort_name
        FROM dba_indexes i
        WHERE i.owner NOT IN ('SYS', 'SYSTEM', 'OUTLN', 'DBSNMP', 'CTXSYS', 'MDSYS', 'ORDSYS', 'WMSYS',
                     'AURORA$JIS$UTILITY$', 'AURORA$ORB$UNAUTHENTICATED', 'OSE$HTTP$ADMIN',
                     'ORDPLUGINS', 'PERFSTAT', 'TRACESVR', 'REPADMIN')
        UNION ALL
        SELECT
            CAST(i.owner || '.' || i.index_name AS varchar2(400)),
            CAST(DECODE(i.status, 'UNUSABLE', 'UNUSABLE', 'VALID') AS varchar2(400)),
            CAST('index' AS varchar2(32)),
            CAST('OK' AS varchar2(16)),
            CAST('type_desc=' || i.index_type ||
                ', table=' || i.table_owner || '.' || i.table_name ||
                ', is_unique=' || DECODE(i.uniqueness, 'UNIQUE', '1', '0') ||
                ', is_primary_key=' || DECODE(c.constraint_type, 'P', '1', '0') ||
                ', is_unique_constraint=' || DECODE(c.constraint_type, 'U', '1', '0') ||
                ', is_disabled=' || DECODE(i.status, 'UNUSABLE', '1', '0') ||
                ', leaf_blocks=' || TO_CHAR(NVL(i.leaf_blocks, 0)) ||
                ', size_mb=' || TO_CHAR(NVL(ROUND(s.bytes / 1048576, 2), 0)) ||
                ', extents=' || TO_CHAR(NVL(s.extents, 0)) ||
                ', last_stats_update=' || NVL(TO_CHAR(i.last_analyzed, 'YYYY-MM-DD'), 'never')
                AS varchar2(4000)),
            1,
            i.owner || '.' || i.index_name
        FROM dba_indexes i,
             dba_segments s,
             -- The constraint side is filtered *inside* an inline view rather than in the WHERE
             -- clause: `c.constraint_type(+) IN ('P','U')` is ORA-01719 on Oracle ("outer join
             -- operator not allowed in operand of OR or IN"), and writing the same test without
             -- (+) would silently turn the outer join into an inner one and drop every index that
             -- enforces nothing - which is most of them.
             (SELECT owner, constraint_name, constraint_type
                FROM dba_constraints
               WHERE constraint_type IN ('P', 'U')) c
        WHERE i.owner NOT IN ('SYS', 'SYSTEM', 'OUTLN', 'DBSNMP', 'CTXSYS', 'MDSYS', 'ORDSYS', 'WMSYS',
                     'AURORA$JIS$UTILITY$', 'AURORA$ORB$UNAUTHENTICATED', 'OSE$HTTP$ADMIN',
                     'ORDPLUGINS', 'PERFSTAT', 'TRACESVR', 'REPADMIN')
          AND i.owner = s.owner(+)
          AND i.index_name = s.segment_name(+)
          AND s.segment_type(+) = 'INDEX'
          -- Only the constraint an index *backs*, which Oracle records on the constraint.
          AND i.owner = c.owner(+)
          AND i.index_name = c.constraint_name(+)
    ORDER BY 6, 7
)
WHERE ROWNUM <= 500;
