-- Oracle 8i legacy metric - ROLLBACK_SEGMENT_CONTENTION. Waits for rollback segment headers.
--
-- 8i has rollback segments, not automatic undo, and their number is fixed by the DBA rather than
-- managed by the instance. Too few for the transaction concurrency and sessions queue on the
-- segment headers: the symptom is a general slowdown with no slow SQL to point at, which is
-- precisely the kind of incident that gets blamed on "the database" for a week. Nothing in the
-- catalog covered it, and it cannot be carried over to a modern instance (undo tablespaces make
-- the question meaningless), so this variant is 8i-only by design.
--
-- The ratio waits/gets is the standard reading: sustained above 1-2% means add segments. Shrinks
-- and extends are in the message because a segment that repeatedly extends and shrinks back to
-- OPTIMAL is doing avoidable work on every large transaction, which is a different fix (raise
-- OPTIMAL) from adding segments.
--
-- One row per online segment plus a summary row, so the report can show the instance-wide ratio
-- without losing which segment is the hot one.
SELECT
    NVL(n.name, 'usn_' || TO_CHAR(s.usn)) AS metric_item,
    TO_CHAR(ROUND(s.waits * 100 / DECODE(s.gets, 0, 1, s.gets), 2)) AS metric_value,
    'percent' AS metric_unit,
    CASE
        WHEN s.waits * 100 / DECODE(s.gets, 0, 1, s.gets) >= 5 THEN 'CRITICAL'
        WHEN s.waits * 100 / DECODE(s.gets, 0, 1, s.gets) >= 1 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'rollback_segment=' || NVL(n.name, 'usn_' || TO_CHAR(s.usn)) ||
        ', waits=' || s.waits ||
        ', gets=' || s.gets ||
        ', wait_ratio_pct=' || ROUND(s.waits * 100 / DECODE(s.gets, 0, 1, s.gets), 2) ||
        ', active_transactions=' || s.xacts ||
        ', extents=' || s.extents ||
        ', shrinks=' || s.shrinks ||
        ', extends=' || s.extends ||
        ', size_mb=' || ROUND(s.rssize / 1048576, 2) AS message
FROM v$rollstat s, v$rollname n
WHERE s.usn = n.usn(+);
