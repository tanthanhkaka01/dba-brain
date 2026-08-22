;WITH cpu AS
(
  SELECT
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
  FROM (VALUES (1)) AS base(dummy)
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
    sql_memory_mb =
      pm.physical_memory_in_use_kb / 1024.0,
    total_memory_mb =
      sm.total_physical_memory_kb / 1024.0,
    available_memory_mb =
      sm.available_physical_memory_kb / 1024.0,
    sql_memory_pct =
      pm.physical_memory_in_use_kb * 100.0
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
)
SELECT
  CAST('cpu' AS varchar(256)) AS metric_item,

  CAST(
    CASE
      WHEN cpu.IsCpuDataValid = 0 THEN -1
      ELSE CAST(100 - cpu.SystemIdle AS decimal(10,2))
    END
    AS varchar(32)
  ) AS metric_value,

  CAST('pct' AS varchar(32)) AS metric_unit,

  CAST(
    CASE
      WHEN cpu.IsCpuDataValid = 0 THEN 'OK'
      WHEN 100 - cpu.SystemIdle >= 95 THEN 'CRITICAL'
      WHEN 100 - cpu.SystemIdle >= 90 THEN 'WARNING'
      ELSE 'OK'
    END
    AS varchar(32)
  ) AS status,

  CAST(
    CASE
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
    + ', total_physical_memory_mb='
    + CAST(
        CAST(mem.total_memory_mb AS decimal(19,2))
        AS varchar(32)
      )
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
    + ', system_low_memory_signal_state='
    + CAST(mem.system_low_memory_signal_state AS varchar(10))
    AS varchar(1024)
  ) AS message
FROM mem;