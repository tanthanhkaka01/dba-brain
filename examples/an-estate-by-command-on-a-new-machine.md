# A whole estate on a new machine, by command only

A walkthrough, not a tool root. It takes a working estate (secrets, bot, inventory, host logins,
SQL tasks, backups, restore drills, schedule) and rebuilds it on a machine that has never run
dbabrain, **one command per item**. Nothing is imported, no bundle is copied, and no config file is
opened in an editor.

It was run for real on 2026-09-29, on a brand-new Windows 11 PC with `pip install dbabrain` from
PyPI (0.24.0). The estate was a mid-sized one, and **an AI agent did the work**. It was handed a
plan written as one PowerShell block per item, ran each block as written, and logged every answer.
Every command below exists in the release this file ships with. The guide guard
(`tests/test_every_command_a_guide_shows_would_run.py`) enforces that.

Values are placeholders: addresses from RFC 5737 (`192.0.2.x`, `198.51.100.x`), a bot called
`@your_bot`, and chat ids like `-1001234567890`. Substitute your own.

| What was added | Count | Command |
| --- | ---: | --- |
| secrets | 105 | `secret-set` |
| Telegram groups | 9 | `group-add` |
| database and host instances | 64 (26 active) | `instance-add` |
| host (OS) logins | 28 | `remote-credential-add` |
| SQL tasks | 10 commands, 10 targets | `sql-command-add`, `sql-target-add` |
| backup / restore entries | 47 / 30 | `backup-add`, `restore-add` |
| app commands changed from the default | 3 | `app-command-set` |

**Result:** every step passed. Fifteen minutes after the daemon started, `ops-status` read *8 app
command(s): none failing, none overdue*. Forty-five minutes in, the lab's FULL, DIFF and LOG backups
and three cross-machine restores (SQL Server, PostgreSQL, Oracle) had all completed. The
[first 45 minutes](#what-the-first-45-minutes-looked-like) section has the measurements.

Read [`standing-up-a-node.md`](./standing-up-a-node.md) first if you have never run dbabrain. That
file explains what each command *is*. This one is about doing all of them, in order, at scale.

---

## 0. Three rules for the whole move

**One scheduler per estate.** Steps 1-13 touch nothing shared. Step 14 starts a daemon, and only
one daemon may run this estate. Two daemons on one bot token make Telegram refuse one poller, and
every backup and restore would run twice. **Stop the old node's daemon before step 14**, then check
that its port (8080) is closed.

**The order is the dependency order.** Secrets come first, because every later request names a
secret by its `ref` and never carries the value. Then the bot, then the instances, whose records
name those refs. Host logins come after instances because they attach to one. SQL tasks, backups
and restores name a `server_id`, so they come after the inventory. Checks go last, then the clock.

**On Windows PowerShell 5.1, send JSON as a here-string on stdin.** PowerShell 5.1 strips the inner
`"` when it passes an argument to a native program:

```powershell
db-ops common upgrade-config '{"dry_run": false}'    # exit 1: "request is not valid JSON"
```

That failure is silent inside a block whose last command succeeds. This one works on every shell,
and it also keeps a secret off the command line:

```powershell
@'
{"dry_run": false}
'@ | db-ops common upgrade-config -
```

`'{}'` is not affected, because it has no inner quotes. PowerShell 7 does not strip them at all.
Every multi-line request below uses the here-string form.

---

## 1. A new root, the package from PyPI

```powershell
New-Item -ItemType Directory -Force C:\dbabrain | Out-Null
Set-Location C:\dbabrain
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install "dbabrain[postgres,mssql,ssh,winrm,oracle]"
.venv\Scripts\python.exe -m db_ops.cli --version
```

Install every extra the estate needs now. Adding one later is harmless, but a missing `winrm`
extra shows up as a host that "cannot authenticate". Install **Microsoft ODBC Driver 18** for
SQL Server targets. This run's PC had only the legacy `SQL Server` driver, and pymssql carried the
connections.

**Proves:** the version is the one you meant.

## 2. The bare install runs clean first

```powershell
$env:PYTHONIOENCODING = "utf-8"
db-ops init
db-ops daemon --config config.json --once
```

**Measured:** 7 jobs `done`, and `APP-WEBHOST` `skip_service`, since `--once` never starts a service.
Then the daemon ran for two minutes, and `GET http://127.0.0.1:8080/db_ops/login` answered **200**.

Stopping that daemon with `Stop-Process -Force` leaves its runs `running`. The next start closes
them and logs one `ERROR app.daemon.startup.stale_running_recovered` per app. That is the recovery
working, not a fault, but it means "`errors.log` holds only its header" cannot hold after a forced
stop. Stop the daemon with its whole process tree (`taskkill /PID <pid> /T /F`). **Match only this
root's `db_ops` processes, never "every process whose command line names the folder".** In this
run, the broad match also killed the agent that was running the plan, because it was working in
the same folder.

## 3. Key, role, clock

```powershell
$env:DB_OPS_SECRET_KEY = "<your passphrase>"     # this shell only
$env:DB_OPS_NODE_ROLE  = "worker"
db-ops db --config config.json timezone --set Europe/Berlin
db-ops db --config config.json timezone
```

**Proves:** the node reads its schedules against the zone you meant. Log lines written before this
step are stamped in the old zone, with no offset in the line, so an early `errors.log` can read out
of order.

## 4. The store

`init` chose the node's own SQLite, which is the right store for a move. Point at a shared
PostgreSQL store only once the old node is stopped for good.

```powershell
db-ops db --config config.json init
db-ops db --config config.json check
```

**Measured:** 30 tables, `schema_version` 5.

## 5. Every secret, one call each

```powershell
@'
{"ref": "SQL01_MONITOR", "value": "<password>", "overwrite": true}
'@ | db-ops common secret-set -
```

Use one call per secret. `overwrite: true` makes the plan re-runnable. A second pass over a block
that half-ran does not stop on the ones already stored.

- Each call warns that `secrets/secret_text.json` (the template `init` writes) does not hold this
  ref, and that `encrypt-secret` would drop it. That warning is correct. **Once you use `secret-set`,
  never run `encrypt-secret`**: it replaces the store with that file.
- **`check-secret` logs in.** It does not just decrypt the store. It opens a session to every target
  it can map a secret to and reports `OK`, `AUTH_FAILED`, `UNREACHABLE`, `NOT_A_LOGIN`,
  `NO_TARGET` or `NO_MANAGEMENT_PORT` for each one. Run it **once**, after step 7, when the secrets
  have targets. In this run it was run three times, and each run put a failed login on the same
  twenty accounts (four expired SQL Server passwords, sixteen refused WinRM logins). Under an
  account-lockout policy, that is how a check locks out an account.

```powershell
db-ops common check-secret '{}'
```

**Measured:** 105 selected, all decrypt. After step 7, 27 logged in and 20 failed authentication.
The failures were real problems in the estate, which is what this check is for.

## 6. The bot, the groups, the operator

```powershell
@'
{"ref": "TELEGRAM_BOT_TOKEN", "value": "<bot token>", "overwrite": true}
'@ | db-ops common secret-set -
db-ops telegram --config config.json use-bot --ref TELEGRAM_BOT_TOKEN
db-ops telegram --config config.json group-add --group-id "-1001234567890" --level critical --title "Alerts - critical" --allow-command 10
db-ops telegram --config config.json group-add --group-id "-1001234567891" --level backup   --title "Backups"           --allow-command 10
db-ops telegram --config config.json user-level --user you --level 100 --pending
db-ops telegram --config config.json bot-info
db-ops telegram --config config.json groups
```

`group-add` registers a chat that has never posted. Groups made for alerts are silent, so the bot
would never discover them by itself. `--pending` holds your level until your first private message
reaches the bot. The daemon has to be running for that, so it happens in step 14.

**Measured:** `bot-info` named the bot and reported `privacy_mode` off. `groups` listed all nine as
`created, verified`.

## 7. The inventory, one `instance-add` per login

The **whole record** goes in the request. `instance-add` passes every field it does not use itself
into the inventory: `metrics` and its overrides, `cmd_access`, `sql_access`, `note`, `owner`,
`status_*`. The login goes in by reference, because the secret was stored in step 5:

```powershell
@'
{
  "server_id": "ACME-192-0-2-10-SQL01",
  "db_type": "sqlserver", "ip": "192.0.2.10", "port": 1433,
  "major_version": 16, "service_name": "MSSQLSERVER",
  "environment": "prod", "platform": "windows", "active": true,
  "metrics": {"enabled": true},
  "cmd_access": {"method": "winrm", "host": "192.0.2.10", "port": 5985,
                 "credential_name": "remote_192_0_2_10_admin", "auth_type": "password"},
  "username": "dbops_monitor",
  "password_ref": "SQL01_MONITOR",
  "credential_name": "SQL01_MONITOR_LOGIN",
  "role": "monitor",
  "replace": true
}
'@ | db-ops common instance-add -

db-ops common list-targets
```

- **A server with more than one database login takes one call per login, and the default login
  goes last.** Each call adds its login to the server's group and makes it the default.
- Register inactive servers too (`"active": false`). They keep their history and their place in the
  inventory, and every listing hides them.

**Measured:** 68 calls, 68 `success: true`. 64 instances (28 SQL Server, 16 Oracle, 13 PostgreSQL,
7 host). `list-targets` answered *26 target(s) you can address; 38 disabled*.

## 8. Host logins, one `remote-credential-add` each

```powershell
@'
{
  "server_id": "ACME-192-0-2-10-SQL01", "host": "192.0.2.10",
  "username": "svc_dbops", "password_ref": "REMOTE_192_0_2_10_ADMIN",
  "credential_name": "remote_192_0_2_10_admin",
  "auth_type": "password", "role": "REMOTE", "replace": true
}
'@ | db-ops common remote-credential-add -

db-ops check-credentials '{}'
```

This request has no `method`, because the instance already carries its `cmd_access` from step 7.
The answer then says *"no cmd_access block written, so nothing reaches the host yet"*. That sentence
describes **this call**, not the instance: the block from step 7 is still there and it names this
credential. `check-credentials` is the check that counts.

A host login whose server is not in the inventory has nothing to attach to, so leave it out. Six
were skipped for that reason here.

**Measured:** 28 `success: true`. `check-credentials` found 0 problems.

## 9. SQL tasks: the files, then what runs, then where

A task names a script file, so write the file first. The SQL is yours. Put it under
`assets/tasks/<engine>/`:

```powershell
New-Item -ItemType Directory -Force assets\tasks\sqlserver\nightly | Out-Null
@'
SELECT COUNT(*) AS n FROM dbo.audit_log;
'@ | Set-Content -Encoding utf8 assets\tasks\sqlserver\nightly\001_audit_rows.sql

@'
{"sql_id": 1, "sql_code": "SQLSERVER-001-AUDIT_ROWS", "display_name": "Audit rows",
 "db_type": "sqlserver", "script_type": "single",
 "script_path": "assets/tasks/sqlserver/nightly/001_audit_rows.sql",
 "active": true, "replace": true}
'@ | db-ops common sql-command-add -

@'
{"sql_id": 1, "server_id": "ACME-192-0-2-10-SQL01", "database_name": "Orders",
 "time_window": {"repeat_interval": 300, "retry_interval": 60, "timeout": 900},
 "notify": {"logging_on_run": {"enabled": false, "telegram_chat": "sql"},
            "alert_on_error": {"enabled": true,  "telegram_chat": "sql"}},
 "output": {"format": "none", "telegram_chat": "sql"},
 "active": true, "replace": true}
'@ | db-ops common sql-target-add -

db-ops sql-tasks --config config.json --dry-run
```

**The dry run checks the schedule, not the task's own inputs.** A task whose input is a program
(`input_type: "python"`) that calls an external executable was listed as *due*. It then failed on
its first real run because the executable was never copied to this machine. Copy everything a task
calls, not only its `.sql`.

**Measured:** 10 + 10 calls OK. The dry run found `Due SQL tasks: 10`.

## 10. Backups and restore drills

One `backup-add` per entry. An entry holds its jobs, and each job has its own window, retention and
level:

```powershell
@'
{
  "backup_id": "LAB_PG_SRC", "db_type": "postgresql",
  "server_id": "ACME-198-51-100-10-LAB-PG",
  "backup_dir": "/opt/db_ops/backup/LAB_PG_SRC",
  "jobs": [
    {"job": "database_full", "active": true,
     "script": "assets/backup/postgresql/pg_basebackup_database.sh",
     "cleanup_retention": 7200,
     "time_window": {"from_minute": 0, "to_minute": 9, "repeat_interval": 2400, "retry_interval": 600, "timeout": 3000},
     "notify": {"logging_on_run": {"enabled": true, "telegram_chat": "backup"},
                "alert_on_error": {"enabled": true, "telegram_chat": "backup"}},
     "env": {"BACKUP_LEVEL": "full"}},
    {"job": "database", "active": true,
     "script": "assets/backup/postgresql/pg_basebackup_database.sh",
     "cleanup_retention": 7200,
     "time_window": {"from_minute": 10, "to_minute": 19, "repeat_interval": 2400, "retry_interval": 600, "timeout": 3000},
     "notify": {"logging_on_run": {"enabled": true, "telegram_chat": "backup"},
                "alert_on_error": {"enabled": true, "telegram_chat": "backup"}},
     "env": {"BACKUP_LEVEL": "incr"}}
  ],
  "active": true, "replace": true
}
'@ | db-ops backup-restore --config config.json backup-add -
```

Then one `restore-add` per drill. This one restores the entry above onto a second lab machine:

```powershell
@'
{
  "restore_id": "LAB_PG_SRC_TO_DST", "active": true, "db_type": "postgresql",
  "server_id": "ACME-198-51-100-10-LAB-PG",
  "target_server_id": "ACME-198-51-100-11-LAB-PG",
  "target_container": "PG_LAB_DST",
  "backup_dir": "/opt/db_ops/backup/LAB_PG_SRC",
  "source_backup_host_dir": "/opt/db_ops/backup/LAB_PG_SRC",
  "target_backup_dir": "/opt/db_ops/backup/pg_restore_from_src",
  "script": "assets/restore/postgresql/pg_restore_basebackup.sh",
  "cleanup_retention": 7200,
  "time_window": {"from_minute": 50, "to_minute": 59, "repeat_interval": 2400, "retry_interval": 600, "timeout": 3000},
  "notify": {"logging_on_run": {"enabled": true, "telegram_chat": "restore"},
             "alert_on_error": {"enabled": true, "telegram_chat": "restore"}},
  "replace": true
}
'@ | db-ops backup-restore --config config.json restore-add -

db-ops backup-restore --config config.json list-backups
db-ops backup-restore --config config.json list-restores
```

- **A restore that copies from a Windows share needs an SMB session from this machine to that
  share.** The share is `\\192.0.2.30\SQLBK` in these examples. The old node had one, and a new
  machine does not. The restore's preflight refuses it, because an unmeasured restore is the one
  that fills a disk. Open the session, or leave that drill inactive until you have.
- The listing's footer counts inactive **jobs**, not entries. That is why it read "47 inactive
  backups hidden" when 40 of 47 entries were inactive.

**Measured:** 47 backups (7 active) and 30 restores (4 active), exactly the old node's counts.

## 11. The app commands the estate runs differently

Only change what differs from the shipped schedule, one call per app command:

```powershell
@'
{"app_code": "APP-SQL_TASKS", "max_parallel": 10,
 "time_window": {"repeat_interval": 1, "retry_interval": 60, "timeout": 1800}}
'@ | db-ops common app-command-set -
```

**Measured:** `self-status` then showed SQL tasks async ×10 every 1 s, reports every 2 min, and
backup/restore async ×4 every 30 s.

## 12. The console and the address the pages are published on

```powershell
db-ops reports --config config.json use-base-url --this-node
db-ops webhost --config config.json user-add --username you --level 100 --password-stdin
```

`user-add --remember` also keeps the console password in the secret store, where
`user-password-show` can read it back with the passphrase. Leave `--remember` off if the console
password should exist only as a hash.

## 13. Check everything before the clock

```powershell
db-ops common upgrade-config '{}'
@'
{"dry_run": false}
'@ | db-ops common upgrade-config -
db-ops common check-objects '{}'
db-ops db --config config.json sync-config '{}'
db-ops check-credentials '{}'
db-ops common check-references '{}'
db-ops common self-status '{}'
```

**Measured:** `upgrade-config` made 0 changes. `check-objects` checked 4,897 objects and found 0
violations. `sync-config` left 399 files unchanged, with 0 missing and 0 failed. `check-credentials`
found 0 problems. `check-references` checked 260 pointers and found 0 dangling. `self-status`
reported the version, `worker`, the zone from step 3 and the store from step 4. This is the point
where a move proves itself. A typo in any of the 300-odd requests above shows up here as a named
violation.

## 14. Start the clock, after the old node is stopped

```powershell
Test-NetConnection <old node> -Port 8080          # must be closed
$env:DB_OPS_NODE_ROLE = "worker"
$p = Start-Process -FilePath .venv\Scripts\python.exe `
       -ArgumentList '-m','db_ops.cli','daemon','--config','config.json' `
       -WorkingDirectory C:\dbabrain -PassThru -WindowStyle Hidden
$p.Id | Set-Content daemon.pid
```

Do not add `-RedirectStandardOutput` when a script whose own output is captured starts the daemon.
The daemon inherits the caller's pipe, and the caller never returns. The runner in this move waited
out its 900-second timeout that way. It does not harm the daemon.

Then send the bot any private message, which adopts your `--pending` level from step 6, and ask it
`/spbot_self_status`.

**The inventory and server-metrics pages return 404 for up to an hour.** They are built by the
hourly report, and that report's only run so far was in step 2, on an empty inventory. Build them
once by hand:

```powershell
db-ops reports --config config.json inventory-workflow --days 7 --beauty 1
```

**Measured at +15 min:** `ops-status` read *8 app command(s): none failing, none overdue*.
`/db_ops/login`, `/report_dba/` and `/report_dba/sla.html` answered 200, and the two built pages
answered 200 after the command above. `errors.log` held only the stale-run recoveries from step 2
and one SQL task missing its external program (step 9).

---

## What the first 45 minutes looked like

**Concurrency** is from `jobs.log`, matching start and done lines by pid:

| App | Mode | Most in flight | |
| --- | --- | ---: | --- |
| SQL tasks | async, `max_parallel` 10 | **10** | at the cap and never over it, in 2,129 starts; no task ever overlapped itself |
| backup / restore | async, 4 | 3 | an 11-minute Oracle restore copy ran while the hour's FULL backups ran in another process |
| metrics, reports, SLA, Telegram, control, web host | sync | 1 | |

A task that failed was retried after its `retry_interval` (30 min), not its 5-hour
`repeat_interval`.

**Backups and restores** on two lab machines:

| Job | Result |
| --- | --- |
| LOG / WAL / archivelog every 5 min | all `done (exit 0)` |
| FULL (SQL Server, PostgreSQL, Oracle level 0) | `done` in 4 s / 3 s / 33 s |
| DIFF / incremental / level 1 | `done` in 3 s / 3 s / 12 s |
| SQL Server restore | 25 pieces copied, then FULL + 4 LOG + certificate in 2 s, VERIFY 1 database, staging cleaned |
| PostgreSQL restore | 1,310 pieces / 502 MB copied, base backup + WAL replay in 11 s, VERIFY OK |
| Oracle restore | 21 pieces / 2.66 GB copied in 11 min, RMAN DUPLICATE in 63 s, VERIFY OK |

**Metrics** came from 14 SQL Server, 1 Oracle, 1 PostgreSQL and 4 host targets. The collection found
real problems on the first pass: a 26-session blocking chain, a log with 2,686 VLFs, failed agent
jobs, dead linked servers, 1.2 s IO latency, and databases with no backup.

**Telegram** sent 180 messages in 45 minutes. SQL tasks sent 82, mostly the lab drills posting
`running` and `done` on every run, then restores 35, backups 20, the operator's own commands 14,
warnings 13 and criticals 10. Set `logging_on_run.enabled: false` on a frequent task unless you want
to see every run.

## What bites people on a move

| | |
| --- | --- |
| Two daemons | on one estate: the bot's poller is refused and every job runs twice. Stop the old node first |
| `'{"key": "value"}'` on PowerShell 5.1 | the quotes are stripped and the command fails silently. Use a here-string on stdin |
| `check-secret` | logs in to every mapped target. Run it once, not in a loop |
| `encrypt-secret` after `secret-set` | replaces the store from a template that holds none of your secrets |
| A cleanup that matches the root folder | also stops whatever else is working in that folder. Match `db_ops` processes |
| `--dry-run` says a task is due | it did not check the programs the task calls |
| A restore from a Windows share | needs an SMB session from the new machine |
| Pages 404 after the move | built hourly. Run `inventory-workflow` once |
| The first hour of server pages | may read `HEALTHY 100` before the first hourly rollup exists. Judge the page after the hour |

## Handing the move to an agent

This move was done by an agent that had never seen the estate, and the plan's format is what made
that work:

- **One fenced block per item**, headed by the item's name. Each block is safe to run again
  (`"replace": true`, `"overwrite": true`), so a block that failed halfway is simply run again.
- **Each step ends with its check and its pass line.** The agent does not decide what "working"
  means.
- **Every answer is logged** (step, block, exit code, output) to a file the agent reads back. A
  block whose last command succeeds can hide one that failed before it, and the log is where that
  shows.
- **Destructive steps are named and gated.** Removing containers and starting the scheduler happened
  only on the operator's explicit word.

A plan like this carries every password in the clear. Keep it off version control and off shared
drives, and delete it when the move is done.
