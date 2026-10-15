-- Oracle 8i legacy variant - TOP_BUFFER_GETS_SQL. Top SQL by buffer_gets (logical reads).
--
-- The sibling of TOP_DISK_READ_SQL, and the half that was missing. disk_reads finds the SQL
-- that hurts the storage; buffer_gets finds the SQL that burns the CPU, and on a well-cached 8i
-- instance those are different statements entirely. A nested-loop join over a cached table can
-- do millions of logical reads with zero physical reads: invisible to disk_reads, and the usual
-- reason an 8.1.7 box is pinned at 100% CPU while the disks are idle.
--
-- reads_per_exec is the number to sort a tuning session by rather than the raw total: a
-- statement with 40 million buffer_gets over 2 million executions is a cheap query called too
-- often (fix the caller), while 40 million over 3 executions is one bad plan (fix the SQL).
-- Both totals are in the message so the two cases stay distinguishable.
--
-- Status is always OK: this is an inventory of the current worst offenders, not a threshold.
-- Turning it into an alert would page someone every time a report ran.
SELECT * FROM (
    SELECT
        TO_CHAR(hash_value) AS metric_item,
        TO_CHAR(buffer_gets) AS metric_value,
        'buffer_gets' AS metric_unit,
        'OK' AS status,
        'buffer_gets=' || buffer_gets ||
            ', executions=' || executions ||
            ', gets_per_exec=' || ROUND(buffer_gets / DECODE(executions, 0, 1, executions), 2) ||
            ', disk_reads=' || disk_reads ||
            ', rows_processed=' || rows_processed ||
            ', sql=' || SUBSTR(sql_text, 1, 100) AS message
    FROM v$sqlarea
    ORDER BY buffer_gets DESC
)
WHERE ROWNUM <= 10;
