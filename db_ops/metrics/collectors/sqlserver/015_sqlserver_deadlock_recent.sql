WITH deadlocks AS
(
    SELECT
        CAST(xet.target_data AS xml) AS target_data
    FROM sys.dm_xe_session_targets AS xet
    JOIN sys.dm_xe_sessions AS xe
        ON xe.address = xet.event_session_address
    WHERE xe.name = 'system_health'
      AND xet.target_name = 'ring_buffer'
),
events AS
(
    SELECT
        DATEADD(hour, DATEDIFF(hour, GETUTCDATE(), GETDATE()),
            n.value('(event/@timestamp)[1]', 'datetime2')
        ) AS event_time
    FROM deadlocks
    CROSS APPLY target_data.nodes('//RingBufferTarget/event[@name="xml_deadlock_report"]') AS q(n)
),
recent AS
(
    SELECT COUNT(*) AS deadlock_count_24h
    FROM events
    WHERE event_time >= DATEADD(hour, -24, GETDATE())
)
SELECT
    CAST('deadlock' AS varchar(256)) AS metric_item,
    CAST(deadlock_count_24h AS varchar(32)) AS metric_value,
    CAST('deadlocks_24h' AS varchar(32)) AS metric_unit,
    CASE
        WHEN deadlock_count_24h >= 5 THEN 'CRITICAL'
        WHEN deadlock_count_24h > 0 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    CONCAT(
        'deadlocks_24h=', deadlock_count_24h,
        ', source=system_health_ring_buffer'
    ) AS message
FROM recent;