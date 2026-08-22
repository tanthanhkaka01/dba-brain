-- TOP_DISK_READ_SQL (Oracle 9i+) - top SQL by physical reads, from v$sqlarea.
--
-- The metric existed only as an 8i variant, which meant the estate's *modern* Oracle instances
-- had no top-SQL metric at all - the engine where the question is asked most often was the one
-- not answering it.
--
-- Deliberately v$sqlarea and hash_value rather than v$sql and sql_id: v$sqlarea aggregates the
-- child cursors, which is the granularity a tuning session starts at, and hash_value is the one
-- identifier present in every release from 8i to current. Keeping the same shape as the 8i
-- variant means a row from either reads identically in the report.
--
-- Status is always OK - an inventory of the current worst offenders, not a threshold.
SELECT * FROM (
    SELECT
        CAST(TO_CHAR(hash_value) AS varchar2(256)) AS metric_item,
        TO_CHAR(disk_reads) AS metric_value,
        CAST('disk_reads' AS varchar2(32)) AS metric_unit,
        CAST('OK' AS varchar2(16)) AS status,
        CAST('disk_reads=' || disk_reads ||
            ', executions=' || executions ||
            ', reads_per_exec=' || ROUND(disk_reads / DECODE(executions, 0, 1, executions), 2) ||
            ', buffer_gets=' || buffer_gets ||
            ', rows_processed=' || rows_processed ||
            ', sql=' || SUBSTR(sql_text, 1, 100) AS varchar2(4000)) AS message
    FROM v$sqlarea
    ORDER BY disk_reads DESC
)
WHERE ROWNUM <= 10;
