-- Oracle 8i legacy metric - SEGMENT_EXTENT_LIMIT. Segments approaching their MAX_EXTENTS.
--
-- This is an 8i-shaped failure and deliberately has no modern variant. Dictionary-managed
-- tablespaces give every segment a hard extent ceiling, and a segment that reaches it fails with
-- ORA-01631/ORA-01632 while the tablespace still has gigabytes of free space - so
-- TABLESPACE_FREE_SPACE reports healthy right up to the moment the insert fails. Locally managed
-- tablespaces (the default from 9i on) make the ceiling effectively unlimited, which is why
-- looking for this on a modern instance would only produce noise.
--
-- MAX_EXTENTS is reported by 8i as 2147483645 when the segment was created UNLIMITED; that is
-- not a limit anyone will reach, so it is excluded rather than reported as 0% used.
--
-- Empty result means no segment is near its ceiling - the good answer, hence empty_result_is_ok.
SELECT * FROM (
    SELECT
        owner || '.' || segment_name AS metric_item,
        TO_CHAR(ROUND(extents * 100 / max_extents, 1)) AS metric_value,
        'percent' AS metric_unit,
        CASE
            WHEN extents * 100 / max_extents >= 90 THEN 'CRITICAL'
            ELSE 'WARNING'
        END AS status,
        'segment=' || owner || '.' || segment_name ||
            ', type=' || segment_type ||
            ', tablespace=' || tablespace_name ||
            ', extents=' || extents || '/' || max_extents ||
            ' (' || ROUND(extents * 100 / max_extents, 1) || '%)' ||
            '; at the ceiling the next extent fails with ORA-01631 even with free space in the tablespace.'
            AS message
    FROM dba_segments
    WHERE max_extents > 0
      AND max_extents < 2000000000
      AND extents * 100 / max_extents >= 75
      AND owner NOT IN ('SYS', 'SYSTEM', 'OUTLN', 'DBSNMP', 'CTXSYS', 'MDSYS', 'ORDSYS', 'WMSYS',
                     'AURORA$JIS$UTILITY$', 'AURORA$ORB$UNAUTHENTICATED', 'OSE$HTTP$ADMIN',
                     'ORDPLUGINS', 'PERFSTAT', 'TRACESVR', 'REPADMIN')
    ORDER BY extents / max_extents DESC
)
WHERE ROWNUM <= 50;
