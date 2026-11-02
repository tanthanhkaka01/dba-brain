-- SQL Server on Linux sees the machine through SQLPAL, and two of its readings are of SQLPAL,
-- not of the host (0.31.0 sheet 1.190, 1.191; measured on two Linux instances on 2026-10-23):
--   * the scheduler monitor's SystemIdle read 0 on 4,047 of 4,047 samples on a host /proc/stat
--     read 83 % idle, so 100 - SystemIdle printed a saturated CPU that was not there;
--   * dm_os_process_memory.physical_memory_in_use_kb read 4.1 GB on both instances while Total
--     Server Memory was 21.2 and 14.2 GB, and dm_os_sys_memory's total is the engine's own limit
--     (mssql-conf memory.memorylimitmb), not the host's RAM.
-- On Linux the CPU row grades the SQL share the ring buffer does report, the memory rows read
-- committed_kb and grade the low-memory flags only, and every row says which basis it used.
-- @@VERSION rather than dm_os_host_info: this file runs from 2012, the view exists from 2017.
;WITH platform AS
(
  SELECT is_linux = CASE WHEN @@VERSION LIKE '% on Linux%' THEN 1 ELSE 0 END
),
cpu AS
(
  SELECT
    platform.is_linux,
    -- The CPU the ENGINE sees, and the schedulers it runs on. Both were only ever in the
    -- hand-written inventory, so a box that gained cores kept reporting the old number
    -- until somebody remembered to edit a file. They come from the instance now.
    sql_visible_cpu_count = (SELECT si.cpu_count FROM sys.dm_os_sys_info si),
    scheduler_count =
      (SELECT COUNT(*) FROM sys.dm_os_schedulers
       WHERE status = 'VISIBLE ONLINE' AND is_online = 1),
    SQLProcessUtilization = rb.SQLProcessUtilization,
    SystemIdle = rb.SystemIdle,
    IsCpuDataValid =
      CASE
        WHEN rb.SystemIdle IS NULL THEN 0
        WHEN rb.SQLProcessUtilization IS NULL THEN 0
        WHEN rb.SystemIdle = 0
          AND rb.SQLProcessUtilization = 0 THEN 0
        WHEN rb.SystemIdle < 0 OR rb.SystemIdle > 100 THEN 0
        WHEN rb.SQLProcessUtilization < 0
          OR rb.SQLProcessUtilization > 100 THEN 0
        ELSE 1
      END
  FROM platform
  OUTER APPLY
  (
    SELECT TOP (1)
      SQLProcessUtilization =
        record.value(
          '(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]',
          'int'
        ),
      SystemIdle =
        record.value(
          '(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]',
          'int'
        )
    FROM
    (
      SELECT CAST(record AS xml) AS record
      FROM sys.dm_os_ring_buffers
      WHERE ring_buffer_type = 'RING_BUFFER_SCHEDULER_MONITOR'
        AND record LIKE '%<SystemHealth>%'
    ) AS src
    ORDER BY
      record.value('(./Record/@id)[1]', 'bigint') DESC
  ) AS rb
),
mem AS
(
  SELECT
    platform.is_linux,
    sql_memory_mb =
      pm.physical_memory_in_use_kb / 1024.0,
    sql_committed_mb =
      si.committed_kb / 1024.0,
    -- What the engine holds: on Windows the process figure (it matches Total Server Memory within
    -- a few percent on every instance of the estate), on Linux the memory manager's committed.
    sql_memory_held_mb =
      CASE WHEN platform.is_linux = 1 THEN si.committed_kb ELSE pm.physical_memory_in_use_kb END
      / 1024.0,
    total_memory_mb =
      sm.total_physical_memory_kb / 1024.0,
    available_memory_mb =
      sm.available_physical_memory_kb / 1024.0,
    sql_memory_pct =
      CASE WHEN platform.is_linux = 1 THEN si.committed_kb ELSE pm.physical_memory_in_use_kb END
      * 100.0
      / NULLIF(sm.total_physical_memory_kb, 0),
    system_memory_used_pct =
      (
        sm.total_physical_memory_kb
        - sm.available_physical_memory_kb
      ) * 100.0
      / NULLIF(sm.total_physical_memory_kb, 0),
    sm.system_low_memory_signal_state,
    pm.process_physical_memory_low
  FROM sys.dm_os_process_memory AS pm
  CROSS JOIN sys.dm_os_sys_memory AS sm
  CROSS JOIN sys.dm_os_sys_info AS si
  CROSS JOIN platform
)
SELECT
  CAST('cpu' AS varchar(256)) AS metric_item,

  CAST(
    CASE
      WHEN cpu.is_linux = 1
        AND cpu.SQLProcessUtilization BETWEEN 0 AND 100
        THEN CAST(cpu.SQLProcessUtilization AS decimal(10,2))
      WHEN cpu.is_linux = 1 OR cpu.IsCpuDataValid = 0 THEN -1
      ELSE CAST(100 - cpu.SystemIdle AS decimal(10,2))
    END
    AS varchar(32)
  ) AS metric_value,

  CAST('pct' AS varchar(32)) AS metric_unit,

  CAST(
    CASE
      WHEN cpu.is_linux = 1
        AND cpu.SQLProcessUtilization BETWEEN 0 AND 100 THEN
        CASE
          WHEN cpu.SQLProcessUtilization >= 95 THEN 'CRITICAL'
          WHEN cpu.SQLProcessUtilization >= 90 THEN 'WARNING'
          ELSE 'OK'
        END
      WHEN cpu.is_linux = 1 OR cpu.IsCpuDataValid = 0 THEN 'OK'
      WHEN 100 - cpu.SystemIdle >= 95 THEN 'CRITICAL'
      WHEN 100 - cpu.SystemIdle >= 90 THEN 'WARNING'
      ELSE 'OK'
    END
    AS varchar(32)
  ) AS status,

  CAST(
    CASE
      WHEN cpu.is_linux = 1 THEN
        'system_cpu_used_pct=N/A'
        + ', sql_process_cpu_pct='
        + COALESCE(
            CAST(
              CAST(cpu.SQLProcessUtilization AS decimal(10,2))
              AS varchar(32)
            ),
            'N/A'
          )
        + ', system_idle_pct=N/A'
        + ', cpu_basis=sql_process'
        + ', reason=system_idle_not_reported_on_linux'
      WHEN cpu.IsCpuDataValid = 0 THEN
        'system_cpu_used_pct=N/A'
        + ', sql_process_cpu_pct='
        + COALESCE(
            CAST(
              CAST(cpu.SQLProcessUtilization AS decimal(10,2))
              AS varchar(32)
            ),
            'N/A'
          )
        + ', system_idle_pct='
        + COALESCE(
            CAST(
              CAST(cpu.SystemIdle AS decimal(10,2))
              AS varchar(32)
            ),
            'N/A'
          )
        + ', reason=cpu_data_not_available_or_invalid_in_container'
      ELSE
        'system_cpu_used_pct='
        + CAST(
            CAST(100 - cpu.SystemIdle AS decimal(10,2))
            AS varchar(32)
          )
        + ', sql_process_cpu_pct='
        + CAST(
            CAST(cpu.SQLProcessUtilization AS decimal(10,2))
            AS varchar(32)
          )
        + ', system_idle_pct='
        + CAST(
            CAST(cpu.SystemIdle AS decimal(10,2))
            AS varchar(32)
          )
    END
    + ', sql_visible_cpu_count='
    + COALESCE(CAST(cpu.sql_visible_cpu_count AS varchar(32)), 'N/A')
    + ', scheduler_count='
    + COALESCE(CAST(cpu.scheduler_count AS varchar(32)), 'N/A')
    AS varchar(1024)
  ) AS message
FROM cpu

UNION ALL

SELECT
  CAST('sql_memory' AS varchar(256)) AS metric_item,

  CAST(
    CAST(mem.sql_memory_pct AS decimal(10,2))
    AS varchar(32)
  ) AS metric_value,

  CAST('pct' AS varchar(32)) AS metric_unit,

  CAST(
    CASE
      WHEN mem.process_physical_memory_low = 1 THEN 'CRITICAL'
      -- On Linux the percentage is of the engine's own limit, which it fills by design.
      WHEN mem.is_linux = 1 THEN 'OK'
      WHEN mem.sql_memory_pct >= 75 THEN 'WARNING'
      ELSE 'OK'
    END
    AS varchar(32)
  ) AS status,

  CAST(
    'sql_physical_memory_in_use_mb='
    + CAST(
        CAST(mem.sql_memory_mb AS decimal(19,2))
        AS varchar(32)
      )
    + ', sql_committed_mb='
    + COALESCE(CAST(CAST(mem.sql_committed_mb AS decimal(19,2)) AS varchar(32)), 'N/A')
    + ', sql_memory_held_mb='
    + COALESCE(CAST(CAST(mem.sql_memory_held_mb AS decimal(19,2)) AS varchar(32)), 'N/A')
    + ', sql_memory_basis='
    + CASE WHEN mem.is_linux = 1 THEN 'committed_kb' ELSE 'physical_memory_in_use_kb' END
    + ', total_physical_memory_mb='
    + CAST(
        CAST(mem.total_memory_mb AS decimal(19,2))
        AS varchar(32)
      )
    + ', memory_scope='
    + CASE WHEN mem.is_linux = 1 THEN 'sql_server_limit' ELSE 'host' END
    + ', sql_memory_pct='
    + CAST(
        CAST(mem.sql_memory_pct AS decimal(10,2))
        AS varchar(32)
      )
    + ', process_physical_memory_low='
    + CAST(mem.process_physical_memory_low AS varchar(10))
    AS varchar(1024)
  ) AS message
FROM mem

UNION ALL

SELECT
  CAST('system_memory' AS varchar(256)) AS metric_item,

  CAST(
    CAST(mem.system_memory_used_pct AS decimal(10,2))
    AS varchar(32)
  ) AS metric_value,

  CAST('pct' AS varchar(32)) AS metric_unit,

  CAST(
    CASE
      WHEN mem.system_low_memory_signal_state = 1 THEN 'CRITICAL'
      -- On Linux "available" is what is left of the engine's limit, not of the host.
      WHEN mem.is_linux = 1 THEN 'OK'
      WHEN mem.available_memory_mb < 1024 THEN 'CRITICAL'
      WHEN mem.system_memory_used_pct >= 95 THEN 'CRITICAL'
      WHEN mem.available_memory_mb < 2048 THEN 'WARNING'
      WHEN mem.system_memory_used_pct >= 90 THEN 'WARNING'
      ELSE 'OK'
    END
    AS varchar(32)
  ) AS status,

  CAST(
    'system_memory_used_pct='
    + CAST(
        CAST(mem.system_memory_used_pct AS decimal(10,2))
        AS varchar(32)
      )
    + ', total_physical_memory_mb='
    + CAST(
        CAST(mem.total_memory_mb AS decimal(19,2))
        AS varchar(32)
      )
    + ', available_physical_memory_mb='
    + CAST(
        CAST(mem.available_memory_mb AS decimal(19,2))
        AS varchar(32)
      )
    + ', memory_scope='
    + CASE WHEN mem.is_linux = 1 THEN 'sql_server_limit' ELSE 'host' END
    + ', system_low_memory_signal_state='
    + CAST(mem.system_low_memory_signal_state AS varchar(10))
    AS varchar(1024)
  ) AS message
FROM mem;