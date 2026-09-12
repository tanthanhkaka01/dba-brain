# Talking to the bot — the commands worth knowing, with real answers

Every block below is an answer the bot actually gave, with addresses, ids, database and task names
replaced by placeholders (RFC 5737 addresses, `ACME-…` / `GLOBEX-…` server ids). Set the bot up
first: [`standing-up-a-node.md`](./standing-up-a-node.md) §6.

## How a command behaves

- **Send it to the bot privately, or in a group the bot is in.** Each command declares where it may
  run; some are private-only, because their answer names hosts.
- **Two replies, always.** `▶️` when the work starts, then `✅` with the answer or `❌` with the
  failure. Both quote your message, so a busy group stays readable.
- **Clearance.** A command has a `command_type`; it runs for a user whose level is at least that, and
  in a group the group's `allow_command` must reach it too. `/spbot_list_*` is level 1, running a SQL
  task or killing a session is 50, restarting a server is 100. A new user is level 0 until
  `db-ops telegram user-level` clears them — level 0 runs only the two public commands.
- **Long answers are split, never cut**, at 4096 characters.
- **`(N inactive entries hidden)`** appears wherever a list has switched-off rows: the bot shows what
  you can act on, and says how much it left out.

`/spbot_list_all_command` lists everything this installation answers, with the level each needs —
the one command to remember.

---

## `/spbot_self_status` — what this installation is

Level 1. The first command to run after standing a node up: it answers with the build, the store, the
host it is on, and **the URLs it publishes**, which is otherwise guesswork.

```
✅ DBA Brain / db_ops - current state
product   : DBA Brain (published)  [pip: dbabrain 0.15.0]
version   : 0.15.0  (public 0.15.0)
running   : on the OS directly, on Windows 11 (10.0.26200)
python    : 3.14.4
host      : DBNODE01
ip        : 192.0.2.93
tool root : C:\dbabrain-node
node_role : worker
store     : sqlite C:\dbabrain-node\runtime\dbabrain.sqlite
cpu       : 20 core(s)
memory    : 21.7 GiB used of 31.7 GiB (68%), 10.0 GiB free
            source: GlobalMemoryStatusEx
disk      : 541.6 GiB free of 600.0 GiB (10% used)
uptime    : 38.20 h  (host up since 2026-09-10 10:31:59 +00)
db_ops up : 12.23 h  (since 2026-09-11 12:30:22 +00)

web UI    : http://192.0.2.93:8080/db_ops/
reports   : http://192.0.2.93:8080/report_dba/
  inventory  : http://192.0.2.93:8080/report_dba/database-inventory.html
  server metr: http://192.0.2.93:8080/report_dba/server-metrics.html
  index usage: http://192.0.2.93:8080/report_dba/index-usage_{server_id}.html
  sla        : http://192.0.2.93:8080/report_dba/sla.html
            (server metrics takes ?server=<server_id>; index usage is one page per server)
```

`node_role` is the line to read when a daemon seems idle: in the default `master` role it schedules
nothing. Inside a container the URLs come from `report_base_url` instead of the node's own address,
because a container cannot see the port it is published on.

## `/spbot_list_server_id` — what this node watches

Level 1. The answer is also the vocabulary for every other command: where one asks for a target, this
is what you may type.

```
✅ Server targets - server_id | db_type ip:port | instance
ACME-192-0-2-108 | sqlserver 192.0.2.108:1433 | MSSQL01
ACME-192-0-2-111 | sqlserver 192.0.2.111:1433 | MSSQLSERVER
ACME-192-0-2-250 | sqlserver 192.0.2.250:1433 | APPSQL
ACME-198-51-100-248 | sqlserver 198.51.100.248:1433 | SQLEXPRESS
ACME-198-51-100-235 | oracle 198.51.100.235:1521 | ORCL
ACME-203-0-113-115 | sqlserver 203.0.113.115:1433 | MSSQLSERVER
GLOBEX-203-0-113-86 | sqlserver 203.0.113.86:1433 | SQLEXPRESS

Use the server_id, or type: <db_type> <ip> [port]  (e.g. mssql 192.0.2.248 1433).
(17 inactive targets hidden; set active:true to use them.)
```

## `/spbot_list_sql_tasks` — the scheduled SQL, and how each runs

Level 1. Every task with its target, its window, its interval and whether it asks for parameters.
`manual` means it only runs when you ask.

```
✅ SQL tasks: 15 command(s), 15 target(s)
#8 SQLSERVER-008
  -> ACME-192-0-2-250 db=- hour 20..23 every 72000s timeout 1800s output=plain
#17 SQLSERVER-017-WAREHOUSE_CURRENT_STOCK
  -> ACME-198-51-100-248 db=- manual (run with /spbot_run_sql_task) timeout 1800s output=xlsx
#18 SQLSERVER-018-TRACE_OPEN_TRANSACTIONS
  params: session_id, min_tran_seconds   (* = required)
  -> ACME-203-0-113-115 db=ERPDB manual (run with /spbot_run_sql_task) timeout 300s output=plain
#19 ORACLE-019-GET_JOB_DETAILS
  params: job_no*   (* = required)
  -> ACME-198-51-100-235 db=ORAAPP manual (run with /spbot_run_sql_task) timeout 1800s output=xlsx via=api
#24 SQL024-ACME-192-0-2-250-PAYROLL-ENGINE-MANUAL
  params: fromdate, todate, companyid, employeeid, fromstepno   (* = required)
  -> ACME-192-0-2-250 db=- manual (run with /spbot_run_sql_task) timeout 14400s output=plain
#28 SQL028-ACME-192-0-2-250-PAYROLL-ENGINE-EVERY-5-MIN
  -> ACME-192-0-2-250 db=- every 100s timeout 300s output=plain

(6 inactive entries hidden; set active:true to use them.)
```

Three things this answer is for: `output=xlsx` means the result arrives as a file rather than a
message; `via=api` means the target is reached through a gateway (an engine the modern driver cannot
speak to); and `db=-` means the task states its own database, so the target's `service_name` is not
one.

## `/spbot_run_sql_task <id> [params…]` — run one now

Level 50, because it executes SQL on a real instance. A task with required parameters asks for them
one at a time, and you can answer `back`, `skip` or `cancel` at any prompt. A task marked for
confirmation asks for `yes` before it runs.

## `/spbot_list_sql_runs` — what the scheduled SQL did

Level 1. Newest first, with duration and row count, so a task that quietly got slower is visible.

```
✅ Last 10 SQL task run(s), newest first:
#3772 [DONE] sql_id=28 SQL028-ACME-192-0-2-250-PAYROLL-ENGINE-EVERY-5-MIN
    2026-09-12 07:44:09 +07 took 52s rows=179 on ACME-192-0-2-250
#3771 [DONE] sql_id=28 SQL028-ACME-192-0-2-250-PAYROLL-ENGINE-EVERY-5-MIN
    2026-09-12 07:41:17 +07 took 52s rows=179 on ACME-192-0-2-250
```

## `/spbot_list_backup_id` and `/spbot_backup <id> <full|diff|log|->`

Level 1 to list, 10 to run. The listing is per backup job, with its level and its window; `-` lets
the schedule's own rule pick.

```
✅ Backup IDs (4):
- ACME_MSSQL_192_0_2_250_APPDB_DIFF  [encrypted]
    sqlserver ACME-192-0-2-250
    level: diff | schedule: 03-06h
- ACME_MSSQL_192_0_2_250_APPDB_FULL  [encrypted]
    sqlserver ACME-192-0-2-250
    level: full | schedule: 01-05h
- ACME_MSSQL_192_0_2_250_APPDB_LOG  [encrypted]
    sqlserver ACME-192-0-2-250
    level: log (no full/diff level) | schedule: every 15m
- ACME_MSSQL_198_51_100_248_ALL_FULL
    sqlserver ACME-198-51-100-248
    level: full | schedule: 01-05h

Run one:  /spbot_backup <backup_id> <full|diff|log|->
Use - to let the schedule's own rule pick the level.
(11 inactive backups hidden; set active:true to use them.)
```

`[encrypted]` is the backup's own encryption, and `level: log (no full/diff level)` is a warning worth
reading: a log chain with no full backup beside it restores nothing.

## `/spbot_list_restore_id` and `/spbot_restore <id>`

Level 1 to list, 10 to run — a restore drill, source and target named so you can see what it
overwrites before you start it.

```
✅ Restore IDs (1):
- ACME_MSSQL_198_51_100_248_TO_LAB_203_0_113_115
    smb restore
    source: 198.51.100.248
    target: 203.0.113.115

(13 inactive entries hidden; set active:true to use them.)
```

## `/spbot_list_my_commands` — what you have run

Level 1, private only. Repeats are folded (`x19`), failures are marked, and the whole line is
repeatable — which is the point: copy it, change one argument, send it again.

```
✅ Your last 10 command(s), newest first:
1. /spbot_list_sql_runs
    2026-09-12 07:46:16 +07  x2
2. /spbot_list_backup_id
    2026-09-12 07:46:08 +07
3. /spbot_self_status
    2026-09-12 07:43:55 +07  x19
4. /spbot_run_sql_task 28 yes
    2026-09-05 12:18:53 +07  x2
5. /spbot_run_sql_task 28 aksdhf
    2026-09-04 13:16:46 +07  last one failed
(5 unanswered prompt(s) skipped: the bot asked a question and never got an answer, so there is no
whole command to repeat.)
```

This listing is **yours alone**: the command substitutes your own user id and is private-only, so
nobody else reads your history through the bot. What it does show is that *a command line is stored* —
see the warning under `/spbot_create_db_docker` below.

## `/spbot_create_db_docker` — a database container on a host, and the one place to be careful

Level 10. Fifteen answers, which is why it is normally run as a **conversation**: the bot asks one
question at a time, and `back`, `skip` and `cancel` work at every step.

```
/spbot_create_db_docker
```

Answered in order: a name, the engine (`mssql` / `postgres` / `mysql` / `oracle`), the image version,
`single` or `ha`, the host port, how the database password is supplied, then the host to deploy on,
its login, how that login authenticates, and finally whether to recreate an existing container and
whether to install Docker if it is missing.

It can also be sent as one line, which is what a repeat from `/spbot_list_my_commands` gives you:

```
/spbot_create_db_docker LAB_MSSQL_01 mssql 2025-latest single 1433 - <sa password> 192.0.2.115 dev REMOTE_192_0_2_115_DEV - - no yes
```

> **Two of those answers are secrets, and a command line is stored.** The message lives in Telegram's
> own chat history and in this node's `telegram_messages` / `telegram_command_messages` — readable by
> anyone who can read the node's store, though **not** by other Telegram users, and not through the
> console. Answered at a **prompt** instead, the same two fields are declared `secret` and are stored
> **masked**. Better still, do not type a secret at all:
>
> - `password_env` — the database password comes from an environment variable on the host;
> - `remote_password_ref` — the host login comes from a ref already in the encrypted store
>   (`db-ops common secret-set -`), or use `remote_key_name` for key authentication.
>
> If a secret has already gone through a command line, rotate it — the row and the chat message are
> both copies you cannot reliably delete everywhere.

## The rest, by what they are for

| Command | Level | What it does |
| --- | :-: | --- |
| `/spbot_status` | 0 | "Running." — the bot and the daemon are alive |
| `/spbot_list_all_command` | 0 | every command this installation answers, with its level |
| `/spbot_list_metrics` | 1 | the metric catalogue, and which are switched off here |
| `/spbot_metric_toggle` | 10 | switch one metric on or off, per target |
| `/spbot_report_inventory` | 2 | build the fleet inventory report now |
| `/spbot_report_hourly_metrics` | 10 | build the hourly metrics report now |
| `/spbot_report_metric_history` | 2 | one metric's history for one server, as a page |
| `/spbot_sql_export`, `/spbot_sql_to_xlsx` | 10 | run a query and get the result as a file |
| `/spbot_xlsx_to_table` | 10 | load a spreadsheet you send into a table |
| `/spbot_add_sql` | 10 | register a new SQL task through a conversation |
| `/spbot_create_db_docker` | 10 | stand up a database container on a host |
| `/spbot_trace_session` | 50 | what one session is doing, and what it blocks |
| `/spbot_kill_spid` | 50 | kill one session |
| `/spbot_shrink_log` | 50 | shrink one transaction log |
| `/spbot_start_job`, `/spbot_disable_job` | 50 | start or disable an Agent job |
| `/spbot_restart_server` | 100 | restart a host, and prove it came back |

Levels 50 and 100 also require **confirmation** — the bot asks before it acts, and the run is
recorded with who asked for it.

## When the bot says nothing

| | |
| --- | --- |
| No reply at all | no token stored, or the daemon is not running. `db-ops telegram --config config.json bot-info` answers the first; `ops-status` the second |
| "Permission denied … user_type=0" | you are not cleared: `db-ops telegram user-level --user @you --level 100` |
| Works privately, ignored in a group | the group's `allow_command` is 0 (`group-level --allow-command 10`), or the bot's **privacy mode** is on, so it only sees commands addressed to it |
| A flood of old answers at once | a newly tokened bot works through Telegram's 24-hour backlog, including commands sent before this node existed |
