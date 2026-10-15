-- INVALID_OBJECTS (Oracle, every release) - stored code that will not compile.
--
-- An INVALID package or view does not fail until something calls it, and then it fails with
-- ORA-04068 / ORA-06550 in the middle of a business transaction rather than at the moment it
-- broke. What breaks it is ordinary work: a column dropped from a table, a grant revoked, a
-- dependency recompiled. The gap between "broke at 02:00 during a release" and "discovered at
-- 09:00 by a user" is exactly what this closes, and nothing in the catalog looked for it.
--
-- Grouped by owner and object type rather than listed one row per object: a failed release
-- invalidates a whole schema at once, and 300 rows saying the same thing is not 300 findings.
-- The count is the metric_value, and the first few names are in the message so the reader knows
-- where to start.
--
-- Empty result means everything compiles - the good answer, hence empty_result_is_ok.
--
-- Oracle-maintained schemas are excluded: invalid objects in SYS/SYSTEM after an upgrade are a
-- DBA task of a different kind (utlrp), and they would drown the application's own.
SELECT * FROM (
    SELECT
        owner || '/' || object_type AS metric_item,
        TO_CHAR(COUNT(*)) AS metric_value,
        'objects' AS metric_unit,
        'WARNING' AS status,
        'owner=' || owner ||
            ', object_type=' || object_type ||
            ', invalid=' || COUNT(*) ||
            ', examples=' || SUBSTR(MIN(object_name) || ',' || MAX(object_name), 1, 120) ||
            '; INVALID code raises ORA-04068 at call time, not at the time it broke.' AS message
    FROM dba_objects
    WHERE status = 'INVALID'
      AND owner NOT IN ('SYS', 'SYSTEM', 'OUTLN', 'DBSNMP', 'CTXSYS', 'MDSYS', 'ORDSYS', 'WMSYS',
                     'AURORA$JIS$UTILITY$', 'AURORA$ORB$UNAUTHENTICATED', 'OSE$HTTP$ADMIN',
                     'ORDPLUGINS', 'PERFSTAT', 'TRACESVR', 'REPADMIN')
    GROUP BY owner, object_type
    ORDER BY COUNT(*) DESC
)
WHERE ROWNUM <= 100;
