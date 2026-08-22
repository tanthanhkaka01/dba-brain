-- The login the toolkit uses, and nothing more than it needs.
--
-- Every metric in this example reads a dynamic management view or a system catalog. On SQL Server
-- that is two server-level permissions and no membership of any server role:
--
--   VIEW SERVER STATE    the sys.dm_* views - sessions, requests, waits, file space
--   VIEW ANY DEFINITION  the catalog views - database options, configuration, principals
--
-- Neither can change anything. A monitoring pass that runs unattended every five minutes should
-- not be able to modify the instance it is measuring, and `sysadmin` is the reflex to avoid: it
-- works immediately and then nobody ever narrows it.
--
-- Applied by step 2 of the README, not by the container image: SQL Server has no equivalent of
-- the postgres initdb directory.

CREATE LOGIN monitor_user
    WITH PASSWORD = 'Quickstart_not_a_real_password_1',
         CHECK_POLICY = OFF;
GO

GRANT VIEW SERVER STATE TO monitor_user;
GRANT VIEW ANY DEFINITION TO monitor_user;
GO

-- BACKUP_AGE reads msdb.dbo.backupset, so the login needs to get into msdb and read it.
-- db_datareader rather than a broader role: it is the smallest thing that answers the question.
USE msdb;
GO
CREATE USER monitor_user FOR LOGIN monitor_user;
ALTER ROLE db_datareader ADD MEMBER monitor_user;
GO

-- A user database, so DATABASE_STATUS and STORAGE_DATA_FILE_SPACE have something to report on
-- besides the system databases. CONNECT lets the per-database metrics reach it; without the
-- grant they report a connection failure, which is a confusing way to discover a missing
-- permission.
USE master;
GO
CREATE DATABASE APPDB;
GO
USE APPDB;
GO
CREATE USER monitor_user FOR LOGIN monitor_user;
GO
