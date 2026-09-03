WITH io AS
(
    SELECT
        CAST(DB_NAME(vfs.database_id) COLLATE DATABASE_DEFAULT AS varchar(256)) AS database_name,
        CAST(mf.type_desc COLLATE DATABASE_DEFAULT AS varchar(64)) AS type_desc,
        CAST(mf.physical_name COLLATE DATABASE_DEFAULT AS varchar(512)) AS physical_name,

        vfs.num_of_reads,
        vfs.num_of_writes,
        -- The raw cumulative stall totals, carried through to the message untouched.
        --
        -- avg_*_latency_ms below is these divided by the counts, i.e. the average since the
        -- ENGINE STARTED. On 192.0.2.250 that start was 2025-10-27, so a 95.97 ms "current"
        -- latency was in fact nine months of history: a bad afternoon last winter keeps the tile
        -- red forever, and a real problem starting today is diluted to invisibility by all the
        -- good months behind it. An interval figure needs the counters themselves, differenced
        -- between two collections — db_ops.lib.interval_rates does that, and
        -- PERFORMANCE_WORKLOAD_COUNTERS carries the instance-wide sum of these same counters
        -- so a report can have the hour without loading a week of per-file rows.
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

    -- Graded here, like every other metric. Read what this number is before trusting it: it is the
    -- average since the ENGINE STARTED, not the latency right now. A file that was slow for one
    -- afternoon last winter still carries it months later, and a problem that started this morning
    -- is diluted by every good hour behind it. On 192.0.2.115 this average sat at 12.84 ms while
    -- individual 15-minute intervals reached 269 ms and 1736 ms during one log-full outage.
    -- So this CASE catches a file that has
    -- been slow *on average for as long as the instance has been up*, which is a real condition and
    -- the only one a single query on the target can see. It does not catch a spike.
    --
    -- Between 2026-08-10 and 2026-08-11 the collector graded this metric on the interval between
    -- two stored samples instead, which does catch spikes. That was withdrawn: it needed a policy
    -- module and a config file of its own for one metric, and per-metric machinery in the collector
    -- is the thing this codebase does not do. If interval grading comes back it comes back as a
    -- declared capability any cumulative metric can use, not as a branch on this metric's code.
    --
    -- The thresholds match data files' 200/500 ms, set on 2026-08-11 from measured alert volume.
    -- A target that needs different numbers sets warning_threshold / critical_threshold in its
    -- metric_overrides - the same per-target override every other metric uses.
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
        -- Lets a reader (and the delta calculation) tell "counters reset" from "counters fell",
        -- without having to guess from the numbers going backwards.
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