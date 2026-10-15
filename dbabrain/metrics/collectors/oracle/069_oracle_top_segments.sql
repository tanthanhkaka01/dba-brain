-- TOP_SEGMENT_SIZE (Oracle, every release) - the largest tables and indexes, by allocated size.
--
-- TABLESPACE_FREE_SPACE answers "is there room left"; this answers "what used the room". Those
-- are different questions and the catalog only had the first, for every Oracle release: when a
-- tablespace starts growing there was no metric that could say which segment was doing it, so
-- the answer had to be re-derived by hand every time.
--
-- Allocated bytes rather than row counts, because allocation is what consumes the tablespace and
-- it is true without statistics being current - on a legacy instance dba_tables.num_rows is
-- frequently years stale (MAINTENANCE_STATISTICS_AGE is the metric for that).
--
-- Status is always OK: size is not a fault. The value of the row is the trend across runs - a
-- segment that doubles in a week is visible in the stored history, and no single reading of it
-- would justify an alert.
--
-- Oracle-maintained schemas are excluded so the application's own segments are what shows.
SELECT * FROM (
    SELECT
        owner || '.' || segment_name AS metric_item,
        TO_CHAR(ROUND(SUM(bytes) / 1048576, 2)) AS metric_value,
        'MB' AS metric_unit,
        'OK' AS status,
        'segment=' || owner || '.' || segment_name ||
            ', type=' || MIN(segment_type) ||
            ', tablespace=' || MIN(tablespace_name) ||
            ', size_mb=' || ROUND(SUM(bytes) / 1048576, 2) ||
            ', extents=' || SUM(extents) AS message
    FROM dba_segments
    WHERE owner NOT IN ('SYS', 'SYSTEM', 'OUTLN', 'DBSNMP', 'CTXSYS', 'MDSYS', 'ORDSYS', 'WMSYS',
                     'AURORA$JIS$UTILITY$', 'AURORA$ORB$UNAUTHENTICATED', 'OSE$HTTP$ADMIN',
                     'ORDPLUGINS', 'PERFSTAT', 'TRACESVR', 'REPADMIN')
      AND segment_type IN ('TABLE', 'TABLE PARTITION', 'INDEX', 'INDEX PARTITION', 'LOBSEGMENT')
    GROUP BY owner, segment_name
    ORDER BY SUM(bytes) DESC
)
WHERE ROWNUM <= 25;
