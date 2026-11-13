-- SECURITY_DATA_ENCRYPTION (PostgreSQL): is this database's data encrypted at rest?
--
-- Community PostgreSQL has no data-file encryption: a copy of the data directory opens on any
-- install of the same major version. That is stated here as what was read - "provider=none" -
-- and never assumed: the one thing that changes the answer inside the server is an extension
-- that provides it. `pg_tde` (Percona's) is the one this reads: installed in a database, its
-- tables are encrypted when they use the `tde_heap` access method, so the row counts them.
--
-- One row for the connected database (the variant is per_database: PostgreSQL cannot read another
-- database's catalog from one connection). metric_value is: encrypted (every table on tde_heap) |
-- partly encrypted | not encrypted.
--
-- STATUS IS ALWAYS OK - a reading, not a grade; see the SQL Server variant for why. Encryption
-- of the volume under the data directory (LUKS, an encrypted cloud disk) cannot be seen from SQL
-- and is not claimed either way: the message says only what the server itself can answer.
SELECT current_database()::varchar(400) AS metric_item,
       (CASE
            WHEN NOT t.installed OR t.tables_tde = 0 THEN 'not encrypted'
            WHEN t.tables_tde = t.tables_total THEN 'encrypted'
            ELSE 'partly encrypted'
        END)::varchar(64) AS metric_value,
       'tde'::varchar(32) AS metric_unit,
       'OK'::varchar(16) AS status,
       ('engine=postgresql'
        || ', provider=' || CASE WHEN t.installed THEN 'pg_tde ' || t.version ELSE 'none' END
        || ', pg_tde_available=' || CASE WHEN t.available THEN 'yes' ELSE 'no' END
        || ', tables_encrypted=' || t.tables_tde
        || ', tables_total=' || t.tables_total
        || ', data_checksums=' || current_setting('data_checksums')
        || ', server_version=' || current_setting('server_version'))::varchar(1000) AS message
FROM (
    SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_tde') AS installed,
           COALESCE((SELECT extversion FROM pg_extension WHERE extname = 'pg_tde'), '') AS version,
           EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_tde') AS available,
           -- Ordinary tables and materialized views of the user's schemas: what holds data.
           (SELECT count(*)
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind IN ('r', 'm')
               AND n.nspname NOT IN ('pg_catalog', 'information_schema')
               AND n.nspname NOT LIKE 'pg_toast%') AS tables_total,
           (SELECT count(*)
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_am a ON a.oid = c.relam
             WHERE c.relkind IN ('r', 'm')
               AND n.nspname NOT IN ('pg_catalog', 'information_schema')
               AND n.nspname NOT LIKE 'pg_toast%'
               AND a.amname LIKE 'tde_heap%') AS tables_tde
) AS t;
