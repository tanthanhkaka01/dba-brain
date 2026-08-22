-- TOP_BUFFER_GETS_SQL (Oracle 9i+) - top SQL by buffer_gets (logical reads), from v$sqlarea.
--
-- Same question as the 8i variant, and the same reason for existing: disk_reads finds the SQL
-- that hurts the storage, buffer_gets finds the SQL that burns the CPU, and on a well-cached
-- instance those are different statements. A nested-loop join over a cached table can do
-- millions of logical reads with no physical reads at all - invisible to TOP_DISK_READ_SQL, and
-- the usual reason an instance is pinned at 100% CPU while the disks are idle.
--
-- v$sqlarea + hash_value rather than v$sql + sql_id, for the reason given in
-- 065_oracle_top_disk_read_sql.sql: aggregated child cursors, and one identifier that exists in
-- every release the estate has.
--
-- Status is always OK - an inventory of the current worst offenders, not a threshold.
SELECT * FROM (
    SELECT
        CAST(TO_CHAR(hash_value) AS varchar2(256)) AS metric_item,
        TO_CHAR(buffer_gets) AS metric_value,
        CAST('buffer_gets' AS varchar2(32)) AS metric_unit,
        CAST('OK' AS varchar2(16)) AS status,
        CAST('buffer_gets=' || buffer_gets ||
            ', executions=' || executions ||
            ', gets_per_exec=' || ROUND(buffer_gets / DECODE(executions, 0, 1, executions), 2) ||
            ', disk_reads=' || disk_reads ||
            ', rows_processed=' || rows_processed ||
            ', sql=' || SUBSTR(sql_text, 1, 100) AS varchar2(4000)) AS message
    FROM v$sqlarea
    ORDER BY buffer_gets DESC
)
WHERE ROWNUM <= 10;
