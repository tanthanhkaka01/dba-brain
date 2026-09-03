WITH io AS
(
    SELECT
        CAST(DB_NAME(vfs.database_id) COLLATE DATABASE_DEFAULT AS varchar(256)) AS database_name,
        CAST(mf.type_desc COLLATE DATABASE_DEFAULT AS varchar(64)) AS type_desc,
        CAST(mf.physical_name COLLATE DATABASE_DEFAULT AS varchar(512)) AS physical_name,

        vfs.num_of_reads,
        vfs.num_of_writes,
        -- Carried raw for the same reason as the modern variant: the averages below are since the
        -- engine started, and only the difference between two collections describes now. The
        -- report does that subtraction, in db_ops/lib/interval_rates.py.
        vfs.io_stall_read_ms,
        vfs.io_stall_write_ms,
        -- How much was moved, not just how many times. Two files with the same read count
        -- and the same latency can differ by two orders of magnitude in throughput, and
        -- "is this volume saturated" is a question about MB/s, not about IOPS.
        vfs.num_of_bytes_read,
        vfs.num_of_bytes_written,

        CONVERT(decimal(19,2),
            vfs.io_stall_read_ms * 1.0 / NULLIF(vfs.num_of_reads, 0)
        ) AS avg_read_latency_ms,

        CONVERT(decimal(19,2),
            vfs.io_stall_write_ms * 1.0 / NULLIF(vfs.num_of_writes, 0)
        ) AS avg_write_latency_ms
    FROM sys.dm_io_virtual_file_stats(NULL, NULL) vfs
    INNER JOIN sys.master_files mf
        ON vfs.database_id = mf.database_id
       AND vfs.file_id = mf.file_id
)
SELECT
    CAST(database_name + ' / ' + type_desc + ' / ' + physical_name AS varchar(512)) AS metric_item,

    CAST(
        CASE
            WHEN ISNULL(avg_read_latency_ms, 0) >= ISNULL(avg_write_latency_ms, 0)
                THEN ISNULL(avg_read_latency_ms, 0)
            ELSE ISNULL(avg_write_latency_ms, 0)
        END AS varchar(32)
    ) AS metric_value,

    CAST('ms' AS varchar(32)) AS metric_unit,

    -- Graded here, on the same thresholds and with the same caveat as the modern variant: this is
    -- the average since the engine started, so it catches a persistently slow file and not a spike.
    -- Keep the two files' CASE identical - a 2008 R2 instance grading differently from the rest of
    -- the estate is a difference nobody would look for when comparing two servers' reports.
    CAST(
        CASE
            WHEN ISNULL(avg_read_latency_ms, 0) >= 500
              OR ISNULL(avg_write_latency_ms, 0) >= 500 THEN 'CRITICAL'
            WHEN ISNULL(avg_read_latency_ms, 0) >= 200
              OR ISNULL(avg_write_latency_ms, 0) >= 200 THEN 'WARNING'
            ELSE 'OK'
        END AS varchar(16)
    ) AS status,

    CAST(
          'database=' + database_name
        + ', file_type=' + type_desc
        + ', avg_read_latency_ms=' + ISNULL(CAST(avg_read_latency_ms AS varchar(32)), '0')
        + ', avg_write_latency_ms=' + ISNULL(CAST(avg_write_latency_ms AS varchar(32)), '0')
        + ', reads=' + CAST(num_of_reads AS varchar(32))
        + ', writes=' + CAST(num_of_writes AS varchar(32))
        + ', io_stall_read_ms=' + CAST(io_stall_read_ms AS varchar(32))
        + ', io_stall_write_ms=' + CAST(io_stall_write_ms AS varchar(32))
        + ', bytes_read=' + CAST(num_of_bytes_read AS varchar(32))
        + ', bytes_written=' + CAST(num_of_bytes_written AS varchar(32))
        -- Lets a reader (and the delta calculation) tell "counters reset" from "counters fell".
        + ', counters_since=' + CONVERT(varchar(19), (SELECT sqlserver_start_time FROM sys.dm_os_sys_info), 120)
        + ', sample_enough='
            + CASE
                WHEN num_of_reads + num_of_writes >= 1000 THEN 'YES'
                ELSE 'NO'
              END
        + ', file=' + physical_name
        AS varchar(4000)
    ) AS message
FROM io
WHERE database_name IS NOT NULL
ORDER BY
    CASE
        WHEN num_of_reads + num_of_writes < 1000 THEN 3
        WHEN ISNULL(avg_read_latency_ms, 0) >= 100
          OR ISNULL(avg_write_latency_ms, 0) >= 100 THEN 1
        WHEN ISNULL(avg_read_latency_ms, 0) >= 50
          OR ISNULL(avg_write_latency_ms, 0) >= 50 THEN 2
        ELSE 3
    END,
    database_name,
    type_desc,
    physical_name;