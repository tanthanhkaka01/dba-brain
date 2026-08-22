-- Oracle 8i incident metric - TOP_DISK_READ_SQL. Top SQL by disk_reads (v$sqlarea).
-- The 2026-05-04 tuning target was high disk-read SQL (PI_WIP_DETAIL update, then report
-- SELECTs); this surfaces the current worst offenders.
SELECT * FROM (
    SELECT
        TO_CHAR(hash_value) AS metric_item,
        TO_CHAR(disk_reads) AS metric_value,
        'disk_reads' AS metric_unit,
        'OK' AS status,
        'disk_reads=' || disk_reads || ', executions=' || executions ||
            ', reads_per_exec=' || ROUND(disk_reads / DECODE(executions, 0, 1, executions), 2) ||
            ', sql=' || SUBSTR(sql_text, 1, 100) AS message
    FROM v$sqlarea
    ORDER BY disk_reads DESC
)
WHERE ROWNUM <= 10;
