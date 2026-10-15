-- SQL_AGENT_JOB_RUNTIME:
-- Running jobs + next scheduled run.
-- Always returns at least one row.
-- Logging only, status always OK.

;WITH metric_data AS
(
  SELECT
    j.name COLLATE DATABASE_DEFAULT AS metric_item,
    CAST(
      'RUNNING '
      + CAST(DATEDIFF(SECOND, ja.start_execution_date, GETDATE()) AS varchar(16))
      + 's'
      AS varchar(64)
    ) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    CAST(
      'Currently executing; elapsed seconds since start'
      AS varchar(128)
    ) AS message
  FROM msdb.dbo.sysjobactivity AS ja
  INNER JOIN msdb.dbo.sysjobs AS j
    ON j.job_id = ja.job_id
  WHERE ja.start_execution_date IS NOT NULL
    AND ja.stop_execution_date IS NULL
    AND ja.session_id =
    (
      SELECT MAX(sa.session_id)
      FROM msdb.dbo.syssessions AS sa
    )

  UNION ALL

  SELECT
    j.name COLLATE DATABASE_DEFAULT AS metric_item,
    CAST(
      'NEXT '
      + CONVERT(
          varchar(19),
          DATEADD(
            SECOND,
            (sjs.next_run_time / 10000) * 3600
              + ((sjs.next_run_time / 100) % 100) * 60
              + (sjs.next_run_time % 100),
            CONVERT(
              datetime,
              CAST(sjs.next_run_date AS varchar(8)),
              112
            )
          ),
          120
        )
      AS varchar(64)
    ) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    CAST(
      'Next scheduled run for an enabled job'
      AS varchar(128)
    ) AS message
  FROM msdb.dbo.sysjobs AS j
  INNER JOIN msdb.dbo.sysjobschedules AS sjs
    ON sjs.job_id = j.job_id
  WHERE j.enabled = 1
    AND sjs.next_run_date > 0
)
SELECT
  metric_item,
  metric_value,
  metric_unit,
  status,
  message
FROM metric_data

UNION ALL

SELECT
  CAST('SQL Agent Jobs' AS nvarchar(128)) COLLATE DATABASE_DEFAULT AS metric_item,
  CAST('NO RUNNING OR SCHEDULED JOBS' AS varchar(64)) AS metric_value,
  CAST(NULL AS varchar(32)) AS metric_unit,
  CAST('OK' AS varchar(16)) AS status,
  CAST(
    'No currently running jobs and no enabled jobs with a next scheduled run'
    AS varchar(128)
  ) AS message
WHERE NOT EXISTS
(
  SELECT 1
  FROM metric_data
);