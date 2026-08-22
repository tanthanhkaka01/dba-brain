-- Oracle 8i legacy variant - DATABASE_STATUS. 8i v$database has no database_role column
-- (that addition is 9i+), which is why the 9i+ variant errors on 8i.
SELECT
    d.name AS metric_item,
    d.open_mode AS metric_value,
    NULL AS metric_unit,
    CASE
        WHEN d.open_mode = 'READ WRITE' THEN 'OK'
        WHEN d.open_mode LIKE 'READ ONLY%' THEN 'WARNING'
        ELSE 'WARNING'
    END AS status,
    'database=' || d.name || ', open_mode=' || d.open_mode || ', log_mode=' || d.log_mode
        || ', compatible=' || p.compatible AS message
FROM v$database d,
     -- COMPATIBLE parameter, same field the 9i+ variant reports. Joined as a single-row inline
     -- view rather than read with a scalar subquery in the SELECT list: scalar subqueries there
     -- are a 9i feature, so the 9i+ spelling would raise ORA-00936 on the very versions this
     -- file exists to serve. rownum keeps it to one row so the join cannot fan out.
     (SELECT value AS compatible
      FROM v$parameter
      WHERE name = 'compatible'
        AND rownum = 1) p;
