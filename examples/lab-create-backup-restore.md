# A lab database, backed up and restored — every engine

A walkthrough, not a tool root. It builds a throwaway database in Docker with `dbabrain sre`, backs
it up, restores it onto a second machine, then restores it again to a moment in the past. It does
this for SQL Server, PostgreSQL and Oracle, and says what the other two engines can and cannot do.
Every command ran in this order on two test VMs during the 0.23.0 release test (2026-09-25). They
ran through the same command lines the Telegram bot builds, and each step states **what it
proves**.

Placeholders: the source machine is `192.0.2.49` and the target is `192.0.2.50` (RFC 5737). The SSH
user is `labuser`. The lab names end in `_A` on the source and `_B` on the target. Substitute your
own.

---

## What each engine can do

| Engine | `--engine` | Build (`create-db-docker`) | Back up | Restore | Point in time |
| --- | --- | --- | --- | --- | --- |
| SQL Server | `mssql` | single, `ha-lab` (Always On AG) | full / diff / log | yes | yes, from log backups |
| PostgreSQL | `postgres` | single, `ha-lab` (streaming replicas) | full / incremental + WAL | yes | yes, from WAL |
| Oracle Database Free | `oracle` | single, `ha-lab` (Data Guard 1 + 1) | level 0 / 1 + archived logs | yes (RMAN `DUPLICATE`) | yes, from archived logs |
| Oracle XE | `oracle-xe` | single only | level 0 + archived logs, the Oracle Free jobs with the XE SID | yes (RMAN `DUPLICATE`, drilled .251 -> .252 on 2026-09-25) | not drilled |
| MySQL | `mysql` | single, `ha-lab` (async replica) | **not shipped**: there is no MySQL backup script, and the restore refuses `mysql` | - | - |

For a MySQL lab, sections 1 and 10 apply. Oracle XE follows the Oracle rows below, with SID and
service `XE` and `"env": {"ORACLE_SID": "XE"}` on the restore. Register **both** of its jobs:
the level 0 and the archived logs. A duplicate from a level 0 alone stops with RMAN-05541 (*no
archived logs found*). The rest of this page is SQL Server, PostgreSQL and Oracle Free.

---

## 0. Before the first command

- A tool root with `dbabrain` installed ([`standing-up-a-node.md`](./standing-up-a-node.md) §1-2).
  Run every command below from that root, with the secret key in the environment:

  ```bash
  export DB_OPS_SECRET_KEY='<your passphrase>'
  ```

- Two Ubuntu machines reachable over SSH. The SSH user needs sudo for `--install-docker`, which
  installs Docker, adds the user to the `docker` group, and creates `/opt/db_ops/containers` and
  `/opt/db_ops/backup`. Without sudo, those must already be in place and owned by the user.
- **Memory.** An Oracle lab wants about 2 GB and takes several minutes to start the first time. A
  4 GB VM holds one Oracle lab next to a small SQL Server or PostgreSQL one, not two Oracle labs.
- **The chats the entries report to.** The entries below send their messages to the `backup` and
  `restore` notify levels. A level exists only when a Telegram group carries it
  ([`standing-up-a-node.md`](./standing-up-a-node.md) §6). Until then `backup-add` and
  `restore-add` refuse the entry and name the missing level. Write `logging` and `error` instead.
- Store each machine's SSH password once. The request goes on stdin, never into argv:

  ```bash
  echo '{"ref": "REMOTE_192_0_2_49_LABUSER", "value": "<ssh password>"}' | dbabrain common secret-set -
  echo '{"ref": "REMOTE_192_0_2_50_LABUSER", "value": "<ssh password>"}' | dbabrain common secret-set -
  ```

  The commands on this page are bash. In PowerShell, drop the `echo`: a quoted string piped
  into a command is its stdin.

---

## 1. Build the labs — `dbabrain sre create-db-docker`

One command per lab, run on the node. Docker runs on the remote machine over SSH, with no hop in
between. The database password is read from an environment variable, which is how the bot passes
it, so it never appears in argv. It is stored encrypted under `<NAME>_PASSWORD`, **after** the
build succeeds.

```bash
export LAB_DB_PASSWORD='Lab_Pw_2026'        # no & and no " for Oracle (see docs/10_sre_app.md)

# the source machine
dbabrain sre create-db-docker --name PG_LAB_A --engine postgres --version 18 --mode single \
  --host-port 5432 --password-text-env LAB_DB_PASSWORD \
  --remote-host 192.0.2.49 --remote-user labuser --remote-password-ref REMOTE_192_0_2_49_LABUSER \
  --backup-mount /opt/db_ops/backup --install-docker

# the target machine: the same engine and version, its own name
dbabrain sre create-db-docker --name PG_LAB_B --engine postgres --version 18 --mode single \
  --host-port 5432 --password-text-env LAB_DB_PASSWORD \
  --remote-host 192.0.2.50 --remote-user labuser --remote-password-ref REMOTE_192_0_2_50_LABUSER \
  --backup-mount /opt/db_ops/backup --install-docker
```

The other engines change only these flags:

| Engine | `--engine` | `--version` | `--host-port` | Notes |
| --- | --- | --- | --- | --- |
| SQL Server | `mssql` | `2025-latest` (or `2022-latest`; there is no bare `2025` tag) | `1433` | mounts `/opt/db_ops/backup` even without the flag |
| PostgreSQL | `postgres` | `18` / `17` / `16` | `5432` | |
| Oracle Free | `oracle` | `23.26.3` (26ai) | `1521` | first start 5-15 min; service `FREEPDB1`, SID `FREE` |
| Oracle XE | `oracle-xe` | `11` | `1521` | service `XE`, no PDB; single only |
| MySQL | `mysql` | `8.4` / `8.0` | `3306` | `ha-lab` runs `bitnamilegacy/mysql` |

Add `--mode ha-lab` for a primary with standbys (`--replicas N`; Oracle is fixed at one standby).
Add `--dry-run` first to see the compose file, the `.env`, and the record it would register,
without touching anything.

**`--backup-mount` is what makes a lab backup-ready as built.** The lab's backups go under
`<mount>/<lab name>` (printed as *Backup folder*). PostgreSQL archives WAL there with WAL summaries
on, which incrementals need. Oracle, Free and XE, turns ARCHIVELOG on at its first start. The HA
primaries mount the folder too. A lab built without the flag, or before 0.23.0, needs a DBA to set
those up, or `--force` to rebuild it (which **wipes its data**).

**The same from Telegram:** `/spbot_create_db_docker` asks the same things one at a time: name,
engine, version, mode, port, password ref (`-` for `<NAME>_PASSWORD`), password, the remote host,
the SSH user, `secret_ref` and the SSH password's ref, `recreate`, `install_docker` and
`backup_ready` (`yes` passes `--backup-mount`).

**Proves:** the answer ends *healthy*, with the backup folder and the registered connection
(`data/docker_db_connections.json`). On the machine, `docker ps` shows the container as
`(healthy)`. The single lab's container is named after `--name`; an HA lab's primary is
`<name>-primary`.

---

## 2. Tell the node how to reach each lab

A backup or restore reaches a lab through its inventory record: the address, the container, and an
SSH login. There are two commands per lab, and the database password is the secret that step 1
stored.

```bash
# the database record (the source; repeat for the target with _B, .50 and REMOTE_192_0_2_50_*)
echo '{"server_id": "LAB-192-0-2-49-PG-5432", "db_type": "postgresql", "ip": "192.0.2.49", "port": 5432,
  "database_name": "postgres", "username": "postgres", "password_ref": "PG_LAB_A_PASSWORD",
  "container_name": "PG_LAB_A", "environment": "lab", "active": false,
  "metrics": {"enabled": false}}' | dbabrain common instance-add -

# the SSH login, and the cmd_access block that uses it
echo '{"server_id": "LAB-192-0-2-49-PG-5432", "username": "labuser",
  "password_ref": "REMOTE_192_0_2_49_LABUSER", "method": "ssh",
  "auth_type": "password"}' | dbabrain common remote-credential-add -
```

Per engine, the record differs in:

| Engine | `db_type` | `port` | `username` | also |
| --- | --- | --- | --- | --- |
| SQL Server | `sqlserver` | `1433` | `sa` | `"instance_name": "MSSQLSERVER"` |
| PostgreSQL | `postgresql` | `5432` | `postgres` | `"database_name": "postgres"` |
| Oracle Free | `oracle` | `1521` | `system` | `"service_name": "FREEPDB1", "sid": "FREE", "instance_name": "FREE"` |
| Oracle XE | `oracle` | `1521` | `system` | `"service_name": "XE", "sid": "XE", "instance_name": "XE"` |

`"active": false` with metrics off keeps a throwaway lab out of the reports while leaving it usable
as a backup source and a restore target. **`auth_type` must be stated**: it defaults to `key`, and
a password given without it would be stored and never read.

**Proves:** `dbabrain check-credentials` resolves both logins, and
`dbabrain common check-references '{}'` finds nothing dangling.

---

## 3. Back it up — `backup-add`, then `backup`

One entry per level family. What each engine needs for a restore to the newest point is a full
backup. A restore to a moment also needs the log stream past that moment.

**PostgreSQL:** a daily full, incrementals through the day, and WAL every 15 minutes.

```bash
echo '{"backup_id": "PG_LAB_A_DB", "server_id": "LAB-192-0-2-49-PG-5432",
  "backup_dir": "/opt/db_ops/backup/PG_LAB_A",
  "jobs": [
    {"job": "database_full", "script": "assets/backup/postgresql/pg_basebackup_database.sh",
     "env": {"BACKUP_LEVEL": "full"}, "cleanup_retention": 259200,
     "time_window": {"from_hour": 1, "to_hour": 5, "repeat_interval": 72000, "retry_interval": 3600, "timeout": 3600},
     "notify": {"logging_on_run": {"enabled": true, "telegram_chat": "backup"},
                "alert_on_error": {"enabled": true, "telegram_chat": "backup"}}},
    {"job": "database", "script": "assets/backup/postgresql/pg_basebackup_database.sh",
     "env": {"BACKUP_LEVEL": "incr"}, "cleanup_retention": 259200,
     "time_window": {"from_hour": 6, "to_hour": 23, "repeat_interval": 21600, "retry_interval": 1800, "timeout": 3600},
     "notify": {"logging_on_run": {"enabled": true, "telegram_chat": "backup"},
                "alert_on_error": {"enabled": true, "telegram_chat": "backup"}}}]}' |
  dbabrain backup-restore backup-add -

echo '{"backup_id": "PG_LAB_A_WAL", "server_id": "LAB-192-0-2-49-PG-5432",
  "backup_dir": "/opt/db_ops/backup/PG_LAB_A",
  "jobs": [{"job": "wal", "script": "assets/backup/postgresql/pg_archive_wal.sh", "cleanup_retention": 259200,
            "time_window": {"repeat_interval": 900, "retry_interval": 300, "timeout": 1800},
            "notify": {"logging_on_run": {"enabled": false, "telegram_chat": "backup"},
                       "alert_on_error": {"enabled": true, "telegram_chat": "backup"}}}]}' |
  dbabrain backup-restore backup-add -
```

**Oracle:** the same shape. `database_full` runs `oracle_rman_database.sh` with `BACKUP_LEVEL` `0`,
`database` runs it with `1`, and a second entry runs `assets/backup/oracle/oracle_rman_archivelog.sh`
(`job: archivelog`) every 15 minutes. `backup_dir` is `/opt/db_ops/backup/ORA_LAB_A`.

**SQL Server:** one entry per level, all with `assets/backup/sqlserver/mssql_backup_database.sh`:
`full` (`BACKUP_LEVEL: full`, daily), optionally `diff`, and `log` (every 15 minutes). The script
logs in as `sa`, so it needs `env_secrets`. The drill encrypted its backups with a server
certificate. The certificate is exported to `<backup_dir>/_cert` and the restore imports it on the
target:

```bash
echo '{"ref": "LAB_BACKUP_ENC", "value": "<a long random passphrase>"}' | dbabrain common secret-set -

echo '{"backup_id": "MSSQL_LAB_A_FULL", "server_id": "LAB-192-0-2-49-MSSQL-1433",
  "backup_dir": "/opt/db_ops/backup/MSSQL_LAB_A",
  "env_secrets": {"MSSQL_PASSWORD": "MSSQL_LAB_A_PASSWORD", "BACKUP_ENCRYPTION_PASSWORD": "LAB_BACKUP_ENC"},
  "jobs": [{"job": "full", "script": "assets/backup/sqlserver/mssql_backup_database.sh",
            "env": {"BACKUP_LEVEL": "full"}, "cleanup_retention": 259200,
            "time_window": {"from_hour": 1, "to_hour": 5, "repeat_interval": 72000, "retry_interval": 3600, "timeout": 3600},
            "notify": {"logging_on_run": {"enabled": true, "telegram_chat": "backup"},
                       "alert_on_error": {"enabled": true, "telegram_chat": "backup"}}}]}' |
  dbabrain backup-restore backup-add -
# MSSQL_LAB_A_LOG: the same with "job": "log", "BACKUP_LEVEL": "log" and
#   "time_window": {"repeat_interval": 900, "retry_interval": 300, "timeout": 1800}
```

Leave `BACKUP_ENCRYPTION_PASSWORD` out for unencrypted backups. SQL Server backs up **user**
databases. A fresh lab has none, so make one to protect, in FULL recovery (a log backup needs it):

```sql
CREATE DATABASE LABDB; ALTER DATABASE LABDB SET RECOVERY FULL;
```

**Run them now** rather than waiting for the window. These are the command lines `/spbot_backup
<id> <level>` builds:

```bash
dbabrain backup-restore backup --backup-id PG_LAB_A_DB --force --backup-type full
dbabrain backup-restore backup --backup-id PG_LAB_A_WAL --force
dbabrain backup-restore backup --backup-id ORA_LAB_A_DB --force --backup-type full
dbabrain backup-restore backup --backup-id MSSQL_LAB_A_FULL --force --backup-type full
```

`--backup-type` is one word for every engine (`full` / `diff` / `log`) and is translated per
engine: Oracle `0`/`1`, PostgreSQL `full`/`incr`.

**Proves:** each run ends `done` and writes a `backup_restore.backup.end` row. The files are under
`/opt/db_ops/backup/<lab name>` on the source. Measured on the drill: SQL Server full 7 s,
PostgreSQL full 11 s, Oracle level 0 36 s.

---

## 4. Restore onto the other machine — `restore-add`, then `restore-workflow`

A restore entry names the source (`server_id`), the target (`target_server_id`) and the target's
container. The workflow does three things:

- copies the backups host to host into `target_backup_dir`, streamed through the node, because the
  two machines are not assumed to reach each other;
- restores them in the target container;
- checks the result.

```bash
echo '{"restore_id": "PG_LAB_A_TO_B", "db_type": "postgresql",
  "server_id": "LAB-192-0-2-49-PG-5432", "target_server_id": "LAB-192-0-2-50-PG-5432",
  "target_container": "PG_LAB_B",
  "backup_dir": "/opt/db_ops/backup/PG_LAB_A", "source_backup_host_dir": "/opt/db_ops/backup/PG_LAB_A",
  "target_backup_dir": "/opt/db_ops/backup/pg_restore_from_a",
  "script": "assets/restore/postgresql/pg_restore_basebackup.sh",
  "cleanup_retention": 259200,
  "time_window": {"from_hour": 6, "to_hour": 9, "repeat_interval": 72000, "retry_interval": 3600, "timeout": 3600},
  "notify": {"logging_on_run": {"enabled": true, "telegram_chat": "restore"},
             "alert_on_error": {"enabled": true, "telegram_chat": "restore"}}}' |
  dbabrain backup-restore restore-add -
```

| Engine | `script` | `target_container` | also |
| --- | --- | --- | --- |
| PostgreSQL | `assets/restore/postgresql/pg_restore_basebackup.sh` | `PG_LAB_B` | - |
| Oracle | `assets/restore/oracle/oracle_rman_restore.sh` | `ORA_LAB_B` | `"env": {"ORACLE_SID": "FREE"}` |
| SQL Server | `assets/restore/sqlserver/mssql_restore.sh` | `MSSQL_LAB_B` | `"env_secrets": {"MSSQL_PASSWORD": "MSSQL_LAB_B_PASSWORD", "BACKUP_ENCRYPTION_PASSWORD": "LAB_BACKUP_ENC"}`. `MSSQL_PASSWORD` is the **target's** `sa` |

**Give every restore its own `target_backup_dir`.** The copy mirrors the source: a staged file the
source no longer has is removed. Two entries sharing one folder would delete each other's files, so
the loader refuses that (and a directory less than three levels deep), and the copy only works in a
directory marked `.dbops-staging` - an empty one, or an earlier copy of the same source, is marked
on the first run.

**Run it** (`/spbot_restore <id> LATEST` builds this):

```bash
dbabrain backup-restore restore-workflow --restore-id PG_LAB_A_TO_B
```

What happens on the target, per engine:

- **PostgreSQL.** The target container is stopped. Its data directory is rebuilt from the full and
  the incrementals, in throwaway containers of the target's own image, with the backups mounted
  read-only. The WAL is replayed, and the server is started again whether or not a step failed.
  The target's own data is **replaced**.
- **Oracle.** RMAN `DUPLICATE` rebuilds the target instance from the backups. The target database
  is **replaced**, with the source's DBID.
- **SQL Server.** The chain (full, then diff, then logs) is restored into the target container and
  `DBCC CHECKDB` runs after it. A container that cannot make the check's snapshot (Msg 1823 / 7928)
  fails the check every time. Put `"checkdb": false` on that entry.

**Proves:** the run ends `completed status=SUCCESS` with a `restore-workflow.end` row, and the data
is there. Look on the target:

```bash
docker exec PG_LAB_B psql -U postgres -c "select count(*) from <a table you wrote on the source>"
docker exec -i ORA_LAB_B bash -lc 'sqlplus -s / as sysdba' <<< "select open_mode from v\$database;"
docker exec -it MSSQL_LAB_B /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa \
  -Q "select name, state_desc from sys.databases"        # asks for the password
```

Measured on the drill: SQL Server 11 s, PostgreSQL 33 s (row counts equal on both sides), Oracle
235 s (`READ WRITE`).

---

## 5. Restore to a moment

Write a row, note the time, write a second row, then back up the logs. Restoring to the noted
moment must bring back the first row and not the second. The drill did exactly this on each
engine.

```sql
-- on the source, in the database the backups cover
CREATE TABLE pitr_drill (phase varchar(20));           -- Oracle: in FREEPDB1, as system
INSERT INTO pitr_drill VALUES ('pitr_a');  COMMIT;      -- SQL Server: no COMMIT needed
-- note the time now, with its offset: 2026-09-25 07:33:47 +08:00
-- wait a few seconds
INSERT INTO pitr_drill VALUES ('pitr_b');  COMMIT;
```

Then take the backup that carries the moment. The full must be from **before** it:

```bash
dbabrain backup-restore backup --backup-id MSSQL_LAB_A_LOG --force        # SQL Server: a log backup
dbabrain backup-restore backup --backup-id PG_LAB_A_WAL --force           # PostgreSQL: switches and archives the WAL segment
dbabrain backup-restore backup --backup-id ORA_LAB_A_ARCHIVELOG --force   # Oracle: RMAN switches the redo log, then backs up
```

and restore to the moment (`/spbot_restore <id> "2026-09-25 07:33:47 +08:00"` builds this):

```bash
dbabrain backup-restore restore-workflow --restore-id PG_LAB_A_TO_B --point-in-time '2026-09-25 07:33:47 +08:00'
```

- **Write the moment with its offset.** It is converted once, to UTC, which is the clock of every
  container lab. A server whose clock is not UTC (a SQL Server on a Windows host in local time)
  takes the moment **without** an offset, in its own clock.
- SQL Server picks the newest full, diff and logs before the moment, **plus the log that holds
  it**, and puts `STOPAT` on that last log. A full taken *after* the moment is passed over.
- PostgreSQL recovers to the moment and promotes. Oracle runs `SET UNTIL TIME` with the duplicate,
  in one `RUN` block.
- On SQL Server, a moment past the end of the logs is refused. It is not rounded down to a restore
  that succeeds hours short.

**Proves:** `select * from pitr_drill` on the target shows `pitr_a` only. Measured on the drill:
PostgreSQL 19 s, Oracle 79 s. Every one showed `pitr_a` only.

---

## 6. HA labs as the source

An `ha-lab` backs up from its **primary**: the record's `container_name` is `<name>-primary`, and
its backups go under `<mount>/<name>` like a single lab's. Every SQL Server replica mounts the
folder, because a backup may run on any of them. Restore it onto a **single** lab of the same
engine with an entry shaped like section 4. The drill restored all three that way:

- SQL Server: the full and four logs;
- PostgreSQL: onto a stopped target, with rows equal on both sides;
- Oracle Data Guard primary: the target came up `READ WRITE`.

---

## 7. What the bot says

Every run reports to Telegram in the chat its `notify` names. These are real messages from the lab
drill, with the identifiers swapped for this page's placeholders.

**A backup** sends two messages per job, START and END. A failure sends ERROR with the script's
own reason in place of END:

```
LOGGING|node|backup_restore.backup START: backup_id=PG_LAB_A_DB PG_LAB_A_DB/database_full started.
LOGGING|node|backup_restore.backup END: backup_id=PG_LAB_A_DB Backup PG_LAB_A_DB/database_full finished: done (exit 0)
ERROR|node|backup_restore.backup ERROR: backup_id=PG_LAB_A_WAL Backup PG_LAB_A_WAL/wal finished: error (exit 1) - RESULT=error reason=container 'PG_LAB_A' is not running - start it (docker start PG_LAB_A) and run the backup again.
```

The 15-minute log, WAL and archived-log jobs have `logging_on_run` off in the entries above, so
they are quiet when they work and alert when they fail. Their runs are still in `job_runs`.

**A restore** sends one message per step, in this order, each naming the restore and its mode:

| Message | Says |
| --- | --- |
| `Restore workflow started.` | `restore_mode=LATEST`, or `POINT_IN_TIME` with `point_in_time=` and `point_in_time_utc=` |
| `COPY_START` / `COPY_DONE` | source → target; pieces and bytes copied, already present, removed because the source no longer has them; `(the whole directory)` when nothing narrowed it |
| `METADATA_SKIP` (or `_START` / `_DONE`) | SQL Server logins and Agent jobs, or why not: *not needed* for PostgreSQL and Oracle |
| `RESTORE_START` | what goes in and to which point: `base backup 20260925T044646Z_FULL + 4 incremental(s), then WAL replay, to the newest backup` |
| `RESTORE_DONE` | the steps and how long they took |
| `VERIFY_START` / `VERIFY_DONE` | `1 database(s) checked, 0 unusable` |
| `DELETE_START` / `DELETE_DONE` | the staging folder past `cleanup_retention` |
| `Restore workflow finished. status=done` | `restored=` and `verified=` again, and every `warning=` |

For example, the first and last of a point-in-time restore:

```
LOGGING|node|Restore workflow started.
restore_id=PG_LAB_A_TO_B
restore_mode=POINT_IN_TIME
point_in_time=2026-09-25 12:47:16 +08:00
point_in_time_utc=2026-09-25T04:47:16+00:00
target=LAB-192-0-2-50-PG-5432 / PG_LAB_B

LOGGING|node|Restore workflow finished. status=done
restore_id=PG_LAB_A_TO_B
restore_mode=POINT_IN_TIME
point_in_time=2026-09-25 12:47:16 +08:00
point_in_time_utc=2026-09-25T04:47:16+00:00
target=LAB-192-0-2-50-PG-5432 / PG_LAB_B
restored=base backup 20260925T044646Z_FULL + 4 incremental(s), then WAL replay
verified=1 database(s) checked, 0 unusable
```

**A failure** stops at its step, and ERROR follows it with the reason. Nothing is verified after a
failed restore step, and nothing is deleted after a failed verify:

```
START  COPY_START  COPY_DONE  METADATA_SKIP  RESTORE_START  ERROR
ERROR ... error_text=restore-full failed: oracle duplicate failed: ... RMAN-05541: no archived logs found in target database
```

---

## 8. Oracle XE

XE 11g R2 backs up and restores like Oracle Free, with two differences: its SID and service are
`XE` (no PDB), and it needs **both** jobs. A duplicate from a level 0 alone stops with RMAN-05541
(*no archived logs found*), as the example above shows.

```bash
# the lab: `--engine oracle-xe --version 11`, backup-ready, on each machine
dbabrain sre create-db-docker --name XE_LAB_A --engine oracle-xe --version 11 --host-port 1521 \
  --password-text-env LAB_DB_PASSWORD --remote-host 192.0.2.49 --remote-user labuser \
  --remote-password-ref REMOTE_192_0_2_49_LABUSER --backup-mount /opt/db_ops/backup

# its record: the Oracle row of section 2 with XE's names
echo '{"server_id": "LAB-192-0-2-49-XE-1521", "db_type": "oracle", "ip": "192.0.2.49", "port": 1521,
  "username": "system", "password_ref": "XE_LAB_A_PASSWORD", "service_name": "XE", "sid": "XE",
  "instance_name": "XE", "database_name": "XE", "container_name": "XE_LAB_A",
  "environment": "lab", "active": false}' | dbabrain common instance-add -
```

Then register the two backup entries, `XE_LAB_A_DB` (`oracle_rman_database.sh`, `BACKUP_LEVEL 0`)
and `XE_LAB_A_ARCHIVELOG` (`oracle_rman_archivelog.sh`), shaped as Oracle's in section 3. Register
one restore entry shaped as section 4's, with `"env": {"ORACLE_SID": "XE"}`. On the drill: a level 0
in 8 s and a restore in 45 s, READ WRITE with every row.

---

## 9. Let the daemon run them

Registered and `active`, the entries run on their own. `APP-BACKUP-RESTORE` wakes every 30 s and
runs whatever is due. **A job is due** when its `time_window` is open on the node's configured clock
(`config.json` `timezone`) and its `repeat_interval` has passed since its last **start**. A manual
run through the bot counts as a start.

```json
{"from_hour": 1, "to_hour": 5, "repeat_interval": 72000, "retry_interval": 3600, "timeout": 3600}
```

That is a nightly full: once between 01:00 and 05:59, and not again for 20 h. A log job is
`{"repeat_interval": 900, ...}` with no hours, so it runs every 15 min, all day. A restore drill
is usually a morning window after the fulls (`06`-`09`). `from_minute` / `to_minute` narrow a
window inside its hours. `{"from_hour": 13, "to_hour": 13, "from_minute": 40, "to_minute": 59,
"repeat_interval": 1800}` is how the drill made three fulls due at 13:40 for a test.

**What runs at the same time.** One run works through its due list in order: backups first, then
restores. The next run, 30 s later, takes what is due and not yet started, because the app is
`async` (`max_parallel` 4). Measured with three fulls due at 13:40 and their restores at 13:43:

- The backups ran one after another inside the first run: SQL Server 3 s, PostgreSQL 2 s, Oracle
  32 s. The next run found nothing left.
- The restores overlapped. The first run restored SQL Server, then PostgreSQL. The next run took the
  Oracle restore while PostgreSQL was still restoring.

Each job is claimed while it runs, so two runs never do the same job at once. A job another run has
already finished is skipped, not repeated.

**An entry registered active with an open window runs at the next pass**, before any command is
typed. The drill's first scheduled incremental ran beside the bot's own full that way. Register an
entry inactive if it should wait, and switch it on when it should start.

**A whole cycle every hour.** For a test that runs often, give each family its own minutes and a
20-minute repeat:

| Entries | `from_minute` - `to_minute` | `repeat_interval` | Runs at |
| --- | --- | --- | --- |
| the fulls (SQL Server `full`, PostgreSQL `database_full`, Oracle level 0) | 0 - 9 | 1200 | :00 |
| the differentials (SQL Server `diff`, PostgreSQL `database`, Oracle level 1) | 10 - 19 | 1200 | :10 |
| the logs (SQL Server `log`, PostgreSQL WAL, Oracle archived logs) | 20 - 29 | 1200 | :20 |
| the restores to the other machine | 30 - 59 | 1200 | :30 and :50 |

No hours means every hour. The 20 minutes count from each start, so a 10-minute window holds one
run and the 30-minute restore window two. The drill ran it for three and a half hours, the last of
them with SQL tasks writing to the same databases ([`lab-sql-tasks.md`](./lab-sql-tasks.md)): 36
backups and 15 restores, all done,
each restore with its eleven messages in order and its `restored=` naming that hour's chain (full +
diff + 1 log; base + 1 incremental then WAL; the RMAN duplicate). On the target, the drill table
held every row the source had written up to the last log backup, and none after.

---

## 10. Stop what you are not using

A lab is a set of containers on a small VM. Stop it when the drill is done, and start it again the
same way:

```bash
docker compose -f /opt/db_ops/containers/PG_LAB_B/docker-compose.yml stop      # keeps the data
docker compose -f /opt/db_ops/containers/PG_LAB_B/docker-compose.yml start
```

**A stopped lab fails its scheduled jobs.** A backup or restore entry that is `active` on a running
daemon still comes due. To pause it, re-register it with `"active": false` and `"replace": true`.
Switch it back on before the next drill, because `--force` skips a job that is not active.
Removing a lab for good takes three steps: `docker compose ... down -v` removes the containers **and** the data volumes, then
delete `/opt/db_ops/containers/<name>`, then remove its registry and inventory records.

---

## 11. What bites people

| | |
| --- | --- |
| A lab built without `--backup-mount` | PostgreSQL does not archive WAL and Oracle is NOARCHIVELOG, so the log backups fail. The WAL job says why (*archive_mode is 'off'*). Adding the mount later changes only the compose file |
| Two restores sharing a `target_backup_dir` | The copy mirrors each source, so each run removes the other's files |
| Two Oracle labs | Every gvenzl image carries the same DBID and incarnation, so RMAN cannot tell two labs' pieces apart. Keep each restore's staging folder its own |
| `checkdb` on a SQL Server container | The snapshot the check needs can fail there (Msg 1823 / 7928). Use `"checkdb": false` on that entry |
| A moment with no offset | It is read as the server's own clock, UTC on a container |
| `--force` on `create-db-docker` | `docker compose down -v` first: the lab's data is gone |
| `auth_type` left out on an SSH password login | It defaults to `key`, and the password is never read |
| An Oracle lab on a 4 GB VM next to another | The first start runs out of memory. Build one at a time, and stop what the drill does not need |
| Oracle (or XE) with a level 0 only | The duplicate stops with RMAN-05541 (*no archived logs found*). Register the archived-log job too |
| A `repeat_interval` counted from a manual run | A job run through the bot at 12:30 with a 30-min repeat is not due until 13:00, whatever its window says |
| An entry switched on inside its window | It runs at the next 30-s pass, beside whatever you are running by hand |
| Cloned VMs | Clones of one template share a machine-id, so DHCP gives them the same address. Run `rm /etc/machine-id && systemd-machine-id-setup` on each, then reboot |

The engines' detail is in [`docs/10_sre_app.md`](../docs/10_sre_app.md) (building) and
[`docs/08_backup_restore_app.md`](../docs/08_backup_restore_app.md) (backup, restore and point in
time).
