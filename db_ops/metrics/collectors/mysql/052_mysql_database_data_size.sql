-- DATABASE_DATA_SIZE (MySQL): size of each schema's tables, in GB.
--
-- Same contract as the other engines so the inventory report reads one shape everywhere:
-- metric_item is the schema (MySQL's "database") name, metric_value is a plain number of GB.
--
-- data_length + index_length is what InnoDB has **allocated** to the tables, which matches the
-- other variants (allocated, not used). data_free is reported alongside rather than subtracted:
-- it is space already taken from the filesystem that only this schema can reuse, so a large
-- value beside a modest size is the MySQL shape of "the files grew and never came back".
--
-- The information_schema system schemas are excluded — they are engine scaffolding, not data
-- anyone manages. Sizes there are estimates for InnoDB (they come from the statistics), which
-- is stated in the message rather than presented as exact.
-- ROUND, not FORMAT: FORMAT inserts thousands separators ("1,234.56") and the report parses
-- metric_value with float(), so a big schema would come back as None and read as blank.
SELECT t.table_schema                                                        AS metric_item,
       ROUND(SUM(t.data_length + t.index_length) / 1073741824, 2)            AS metric_value,
       'GB'                                                                  AS metric_unit,
       'OK'                                                                  AS status,
       CONCAT('tables=', COUNT(*),
              ', data_mb=', ROUND(SUM(t.data_length) / 1048576, 2),
              ', index_mb=', ROUND(SUM(t.index_length) / 1048576, 2),
              ', free_mb=', ROUND(SUM(t.data_free) / 1048576, 2),
              ' (InnoDB sizes are statistics estimates)')                     AS message
FROM information_schema.tables AS t
WHERE t.table_schema NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
  AND t.table_type = 'BASE TABLE'
GROUP BY t.table_schema
ORDER BY t.table_schema;
