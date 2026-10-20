-- LOCK_SLEEPING_OPEN_TRANSACTION (Oracle variant) - idle sessions holding an open transaction.
--
-- Counterpart of 024_sqlserver_sleeping_open_transaction.sql. An INACTIVE Oracle session that still
-- has a row in v$transaction is the same problem as a sleeping SQL Server session with
-- open_transaction_count > 0: undo is pinned, locks are held, and nothing is going to release them
-- until someone commits or the session is killed.
--
-- The message field names are chosen to match the SQL Server variant exactly - login, host, program,
-- idle_minutes - because this metric's report_policy groups on those keys and applies its severity
-- thresholds to idle_minutes. Renaming them here would silently drop Oracle rows out of the grouped
-- report. is_blocking is emitted too, which the policy escalates to CRITICAL on.
--
-- metric_value is the open transaction count (always 1 per session in Oracle) so the value has the
-- same meaning and unit as the SQL Server row.
--
-- CRITICAL strictly requires a waiter (a session blocked by it): an abandoned sleeping session
-- blocking someone else. A sleeping session holding a transaction that blocks nobody is normal
-- application behavior and never pages CRITICAL on its own.
--
-- All thresholds are declared in the top-level params CTE so every number appears once.

WITH params AS (
    SELECT
        60   AS idle_abandoned_seconds,
        1800 AS idle_critical_seconds,
        300  AS idle_warning_seconds
    FROM dual
)
SELECT
    CAST('SID=' || TO_CHAR(s.sid) || ',' || TO_CHAR(s."SERIAL#") AS varchar2(256)) AS metric_item,
    CAST('1' AS varchar2(32)) AS metric_value,
    CAST('transaction(s)' AS varchar2(32)) AS metric_unit,
    CASE
        -- CRITICAL strictly requires a waiter: an abandoned sleeping session blocking another session.
        WHEN s.last_call_et >= p.idle_abandoned_seconds AND s.blocking_others > 0 THEN 'CRITICAL'
        WHEN s.blocking_others > 0 THEN 'WARNING'
        WHEN s.last_call_et >= p.idle_critical_seconds THEN 'WARNING'
        WHEN s.last_call_et >= p.idle_warning_seconds THEN 'LOGGING'
        ELSE 'OK'
    END AS status,
    'Sleeping session with open transaction. '
        || 'login=' || NVL(s.username, '')
        || ', host=' || NVL(s.machine, '')
        || ', program=' || NVL(s.program, '')
        || ', idle_minutes=' || TO_CHAR(ROUND(s.last_call_et / 60))
        || ', is_blocking=' || CASE WHEN s.blocking_others > 0 THEN 'true' ELSE 'false' END
        || ', sid=' || TO_CHAR(s.sid)
        || ', serial=' || TO_CHAR(s."SERIAL#")
        || ', undo_blocks=' || TO_CHAR(s.used_ublk)
        || ', undo_records=' || TO_CHAR(s.used_urec)
        || ', tx_start=' || NVL(s.tx_start, '')
        || ', osuser=' || NVL(s.osuser, '') AS message
FROM
(
    SELECT
        se.sid,
        se."SERIAL#",
        se.username,
        se.machine,
        se.program,
        se.osuser,
        se.last_call_et,
        t.used_ublk,
        t.used_urec,
        t.start_time AS tx_start,
        (
            SELECT COUNT(*)
            FROM v$session blocked
            WHERE blocked.blocking_session = se.sid
        ) AS blocking_others
    FROM v$transaction t
    JOIN v$session se
        ON se.saddr = t.ses_addr
    WHERE se.status = 'INACTIVE'
      AND se.username IS NOT NULL
) s
CROSS JOIN params p
ORDER BY s.blocking_others DESC, s.last_call_et DESC;
