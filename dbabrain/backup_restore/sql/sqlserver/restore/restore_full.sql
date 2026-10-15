USE master;

RESTORE DATABASE [$(restore_database)]
FROM DISK = N'$(backup_file)'
WITH
    MOVE N'$(source_database)' TO N'$(data_file)',
    MOVE N'$(source_database)_log' TO N'$(log_file)',
    REPLACE,
    RECOVERY,
    CHECKSUM,
    STATS = 10;

