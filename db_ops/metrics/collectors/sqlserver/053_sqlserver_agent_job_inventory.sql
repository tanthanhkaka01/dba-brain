-- SQL_AGENT_JOB_INVENTORY: every SQL Agent job with its enabled flag, what it runs, when it is
-- scheduled, and how it has actually been doing.
--
-- Logging only (status always OK, no alert). JOB_FAILED already alerts on failures; this is the
-- reference list behind it - the server page renders it as its own section, and the inventory
-- report reads the disabled ones out of it.
--
-- **Disabled jobs stay in the result.** The server page only shows enabled ones, but
-- `reports/inventory_health.py::build_sql_agent_job_health` derives `disabled_jobs` from exactly
-- this metric by looking for `metric_value = 'DISABLED'`. Filtering them out in the SQL would
-- empty that block without anything reporting an error.
--
-- The message keeps `enabled=` and `last_outcome=` as its first two keys, in that order, because
-- that same block parses them by name; everything after is additive and safe to extend.
--
-- Schedule decoding follows msdb's freq_type/freq_subday_type encoding. A job with several
-- schedules reports the first by start time - the report links to the job, it does not replace
-- the Agent UI. Only the first step's command is carried, truncated to 200 characters, with the
-- step count beside it: the point is to recognise the job, not to read its code here, and some
-- steps hold kilobytes of T-SQL that would bloat every row of every run.
--
-- History is the last 7 days from sysjobhistory (step_id = 0 is the job's own outcome row, not a
-- step's). Counting attempts rather than reading only the latest outcome is what makes a job that
-- fails every other run distinguishable from one that is simply broken right now.
SELECT
    j.name COLLATE DATABASE_DEFAULT AS metric_item,
    CASE WHEN j.enabled = 1 THEN 'ENABLED' ELSE 'DISABLED' END AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    (
        'enabled=' + CAST(j.enabled AS varchar(1)) +
        ', last_outcome=' + ISNULL(
            CASE js.last_run_outcome
                WHEN 0 THEN 'FAILED'
                WHEN 1 THEN 'SUCCEEDED'
                WHEN 2 THEN 'RETRY'
                WHEN 3 THEN 'CANCELED'
                ELSE 'UNKNOWN'
            END, 'none') +
        ', category=' + ISNULL(c.name COLLATE DATABASE_DEFAULT, 'none') +
        ', owner=' + ISNULL(SUSER_SNAME(j.owner_sid), 'unknown') +
        ', steps=' + CAST(ISNULL(st.step_count, 0) AS varchar(10)) +
        ', schedule=' + ISNULL(sc.schedule_text, 'unscheduled') +
        ', next_run=' + ISNULL(sc.next_run_text, 'none') +
        ', last_run=' + ISNULL(h.last_run_text, 'never') +
        ', max_duration_seconds_7d=' + ISNULL(CAST(h.max_duration_seconds AS varchar(20)), 'n/a') +
        ', runs_7d=' + CAST(ISNULL(h.run_count, 0) AS varchar(10)) +
        ', succeeded_7d=' + CAST(ISNULL(h.success_count, 0) AS varchar(10)) +
        ', failed_7d=' + CAST(ISNULL(h.failed_count, 0) AS varchar(10)) +
        -- command is last on purpose: a job step is full of commas and equals signs,
        -- and the report parses the earlier keys by name up to the next known key.
        ', command=' + ISNULL(REPLACE(REPLACE(SUBSTRING(st.command, 1, 200), CHAR(13), ' '), CHAR(10), ' '), 'none')
    ) AS message
FROM msdb.dbo.sysjobs AS j
LEFT JOIN msdb.dbo.syscategories AS c
    ON c.category_id = j.category_id
OUTER APPLY (
    SELECT TOP 1 s.last_run_outcome
    FROM msdb.dbo.sysjobservers AS s
    WHERE s.job_id = j.job_id
) AS js
OUTER APPLY (
    SELECT
        (SELECT COUNT(*) FROM msdb.dbo.sysjobsteps AS a WHERE a.job_id = j.job_id) AS step_count,
        s.command
    FROM msdb.dbo.sysjobsteps AS s
    WHERE s.job_id = j.job_id
      AND s.step_id = 1
) AS st
OUTER APPLY (
    SELECT TOP 1
        -- freq_type: 1 once, 4 daily, 8 weekly, 16 monthly, 32 monthly relative,
        -- 64 on Agent start, 128 when idle. freq_subday_type: 1 at the stated time,
        -- 2 seconds, 4 minutes, 8 hours - the "repeat every N" part of a daily schedule.
        CASE s.freq_type
            WHEN 1 THEN 'once'
            WHEN 4 THEN 'daily'
                    + CASE WHEN s.freq_interval > 1
                           THEN ' (every ' + CAST(s.freq_interval AS varchar(10)) + ' days)'
                           ELSE '' END
            WHEN 8 THEN 'weekly'
            WHEN 16 THEN 'monthly (day ' + CAST(s.freq_interval AS varchar(10)) + ')'
            WHEN 32 THEN 'monthly relative'
            WHEN 64 THEN 'on agent start'
            WHEN 128 THEN 'when idle'
            ELSE 'freq_type=' + CAST(s.freq_type AS varchar(10))
        END
        + ' at ' + STUFF(STUFF(RIGHT('000000' + CAST(s.active_start_time AS varchar(6)), 6), 5, 0, ':'), 3, 0, ':')
        + CASE s.freq_subday_type
            WHEN 2 THEN ', repeat every ' + CAST(s.freq_subday_interval AS varchar(10)) + ' second(s)'
            WHEN 4 THEN ', repeat every ' + CAST(s.freq_subday_interval AS varchar(10)) + ' minute(s)'
            WHEN 8 THEN ', repeat every ' + CAST(s.freq_subday_interval AS varchar(10)) + ' hour(s)'
            ELSE ''
          END
        + CASE WHEN s.enabled = 0 THEN ' [schedule disabled]' ELSE '' END AS schedule_text,
        CASE WHEN sj.next_run_date > 0
             THEN CAST(sj.next_run_date AS varchar(8)) + ' '
                  + STUFF(STUFF(RIGHT('000000' + CAST(sj.next_run_time AS varchar(6)), 6), 5, 0, ':'), 3, 0, ':')
             ELSE NULL
        END AS next_run_text
    FROM msdb.dbo.sysjobschedules AS sj
    JOIN msdb.dbo.sysschedules AS s
        ON s.schedule_id = sj.schedule_id
    WHERE sj.job_id = j.job_id
    ORDER BY s.active_start_time, s.schedule_id
) AS sc
OUTER APPLY (
    SELECT
        COUNT(*) AS run_count,
        SUM(CASE WHEN hi.run_status = 1 THEN 1 ELSE 0 END) AS success_count,
        SUM(CASE WHEN hi.run_status = 0 THEN 1 ELSE 0 END) AS failed_count,
        MAX(CAST(hi.run_date AS varchar(8)) + ' '
            + STUFF(STUFF(RIGHT('000000' + CAST(hi.run_time AS varchar(6)), 6), 5, 0, ':'), 3, 0, ':')) AS last_run_text,
        MAX((hi.run_duration / 10000) * 3600
            + ((hi.run_duration / 100) % 100) * 60
            + (hi.run_duration % 100)) AS max_duration_seconds
    FROM msdb.dbo.sysjobhistory AS hi
    WHERE hi.job_id = j.job_id
      AND hi.step_id = 0
      AND hi.run_date >= CAST(CONVERT(varchar(8), DATEADD(day, -7, GETDATE()), 112) AS int)
) AS h
ORDER BY j.name;
