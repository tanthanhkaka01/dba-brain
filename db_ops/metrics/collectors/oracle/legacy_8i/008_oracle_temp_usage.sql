-- Oracle 8i legacy variant - STORAGE_TEMP_SPACE. Sort segment usage per temporary tablespace.
--
-- Reads v$sort_segment, not v$sort_usage. v$sort_usage lists the sorts running *at this instant*,
-- so on any instance that is not sorting right now it returns nothing at all - and the metric
-- reported NO_DATA every 30 minutes, which is indistinguishable from a broken collector. Worse,
-- it meant the metric was blind exactly when it was cheap to read and only spoke up during the
-- sorts it was meant to warn about in advance.
--
-- v$sort_segment is the sort segment itself: it persists, so there is always a row per temporary
-- tablespace, and it carries the high-water mark. The high-water mark is the number that answers
-- the real question - ORA-01652 comes from the largest sort the instance has ever had to do, not
-- from the one running now.
--
-- metric_value is currently-used MB; max_used and the tablespace size are in the message so a
-- reader can see how close the worst case came to the ceiling.
-- The block size comes from v$parameter through a join rather than a scalar subquery in the
-- SELECT list, which 8i does not support (that arrived in 9i), and it is read rather than
-- assumed to be 8k because a wrong constant here would silently report every size wrong.
SELECT
    s.tablespace_name AS metric_item,
    TO_CHAR(ROUND(s.used_blocks * p.block_size / 1048576, 2)) AS metric_value,
    'MB' AS metric_unit,
    -- Always OK, and not for lack of trying: the obvious threshold, max_used against
    -- total_blocks, is meaningless here. The sort segment grows to its own high-water mark and
    -- stays there, so max_used equals total on every healthy instance and the check reported a
    -- permanent WARNING (measured on 1.236, run 28781). The real question - can the temporary
    -- tablespace still grow - is about the tablespace, not the segment inside it, and
    -- TABLESPACE_FREE_SPACE answers it with the autoextend headroom included. This metric is
    -- the usage reading that metric cannot give: what sorts actually consume, and the worst
    -- they have ever consumed.
    'OK' AS status,
    'temp tablespace=' || s.tablespace_name ||
        ', used_mb=' || ROUND(s.used_blocks * p.block_size / 1048576, 2) ||
        ', max_used_mb=' || ROUND(s.max_used_blocks * p.block_size / 1048576, 2) ||
        ', total_mb=' || ROUND(s.total_blocks * p.block_size / 1048576, 2) ||
        ', current_sorts=' || s.current_users ||
        ', extent_hits=' || s.extent_hits ||
        '; max_used is the high-water mark, which is what ORA-01652 is measured against.' AS message
FROM v$sort_segment s,
     (SELECT TO_NUMBER(value) AS block_size FROM v$parameter WHERE name = 'db_block_size') p;
