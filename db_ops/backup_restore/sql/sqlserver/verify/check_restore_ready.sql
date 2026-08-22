SELECT
    name,
    state_desc,
    recovery_model_desc,
    user_access_desc
FROM sys.databases
WHERE name = N'$(restore_database)';

