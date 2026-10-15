-- INSTANCE_STATUS for PostgreSQL: one row, and in its message what the server says about itself -
-- the facts a reader needs before judging anything else on the page (0.28.0. Until then the
-- message held the version, the role and the uptime, beside nineteen facts for SQL Server).
--
-- Only what PostgreSQL can answer for a pg_monitor login, under its own name. It has no machine
-- name to ask for, no edition and no patch level, so none is written: an absent key reads as
-- this engine has no such thing, which N/A under a SQL Server name would not.
--
-- * Every part that can be NULL is wrapped: one NULL in a || chain makes the whole message NULL.
--   inet_server_addr() is NULL on a unix socket, and pg_stat_ssl has no row before 9.5.
-- * No value may hold a semicolon or a comma - the report reads the pairs up to either. version()
--   holds commas (compiled by gcc ... 64-bit), so it is cut into build and platform.
-- * ip / port are the server end of this connection. In a container that is the container
--   address and port, not the published ones - the same as local_net_address on SQL Server.
-- * The collation is the one of the connected database (pg_database.datcollate). A cluster has
--   no single one, so it is named database_collation, beside connected_database, and cannot be
--   read as the server collation.
-- * No comment here holds a semicolon or a quote, so no client between this file and the
--   server has to read one correctly to find where the statement ends.
SELECT
    'instance' AS metric_item,
    'ONLINE' AS metric_value,
    NULL::text AS metric_unit,
    'OK' AS status,
    'PostgreSQL connection available. version=' || current_setting('server_version')
        || '; build=' || split_part(version(), ' on ', 1)
        || '; platform=' || split_part(split_part(version(), ' on ', 2), ',', 1)
        || '; role=' || CASE WHEN pg_is_in_recovery() THEN 'standby' ELSE 'primary' END
        || '; ip=' || COALESCE(host(inet_server_addr()), 'N/A')
        || '; port=' || COALESCE(inet_server_port()::text, current_setting('port'))
        || '; transport=' || CASE WHEN inet_server_addr() IS NULL THEN 'socket' ELSE 'TCP' END
        || '; ssl=' || COALESCE((SELECT CASE WHEN s.ssl THEN 'on' ELSE 'off' END
                                 FROM pg_stat_ssl s WHERE s.pid = pg_backend_pid()), 'N/A')
        || '; connected_database=' || current_database()
        || '; database_collation=' || COALESCE((SELECT d.datcollate FROM pg_database d
                                                WHERE d.datname = current_database()), 'N/A')
        || '; encoding=' || current_setting('server_encoding')
        || '; timezone=' || current_setting('TimeZone')
        || '; max_connections=' || current_setting('max_connections')
        || '; wal_level=' || current_setting('wal_level')
        || '; archive_mode=' || current_setting('archive_mode')
        || '; started=' || to_char(pg_postmaster_start_time(), 'YYYY-MM-DD HH24:MI:SS')
        || '; uptime_seconds=' || EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))::bigint
        AS message;
