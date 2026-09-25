# Scheduled SQL on the lab databases — SQL Server, PostgreSQL, Oracle

A walkthrough, not a tool root. It puts scheduled SQL tasks on the three lab databases that
[`lab-create-backup-restore.md`](./lab-create-backup-restore.md) builds: a long task, a medium one and
a short one on each engine, all due again ten seconds after they start, so they overlap. Every
command ran in this order during the 0.23.0 release test (2026-09-25), and each step states **what
it proves**.

Placeholders: the lab machine is `192.0.2.49` (RFC 5737), its records are the ones section 2 of the
backup walkthrough registers (`LAB-192-0-2-49-MSSQL-1433`, `LAB-192-0-2-49-PG-5432`,
`LAB-192-0-2-49-ORA-1521`). Substitute your own.

---

## What each engine runs

| Engine | `db_type` | A script is | Wait for N seconds | Parameters |
| --- | --- | --- | --- | --- |
| SQL Server | `sqlserver` | one batch, or several split by `GO` lines | `WAITFOR DELAY '00:01:00';` | yes (`DECLARE @name` lines) |
| PostgreSQL | `postgresql` | statements split on `;`, run one at a time (a `;` in a string, a comment or a `$$` body is not a split) | `select pg_sleep(60);` | **not yet** - refused at registration |
| Oracle | `oracle` | **one statement or one PL/SQL block per batch**, batches split by `GO` lines | `BEGIN DBMS_SESSION.SLEEP(60); END;` | only on an Oracle 8i bridge target (SQL*Plus `&` defines). **Not on a direct connection**, and not refused there yet - the run fails |

Each SELECT comes back as its own result set on every engine, and an INSERT's rows are counted in
the run's `row_count`. An Oracle block keeps the `;` after its `END`; a statement loses its trailing
`;`, because Oracle refuses one there.

An Oracle target needs the Oracle driver on the node: install with the `oracle` extra
(`pip install "dbabrain[oracle,postgres,mssql]"`). Without it the run says *oracledb (or cx_Oracle)
is required* and names the extra.

---

## 1. A table for the tasks to write to

Each task waits, inserts a row and counts its rows, so a restore or a look at the table later shows
exactly which runs committed. One table per lab, made by hand (these are the lab's own tables, not
the tool's):

```sql
-- SQL Server, database LABTEST
CREATE TABLE dbo.sqltask_drill (id int IDENTITY PRIMARY KEY, task varchar(40) NOT NULL,
  at datetime2 NOT NULL DEFAULT sysutcdatetime(), spid int NOT NULL DEFAULT @@SPID);

-- PostgreSQL, database labtest
create table sqltask_drill (id bigserial primary key, task text not null,
  at timestamptz not null default clock_timestamp(), pid int not null default pg_backend_pid());

-- Oracle, service FREEPDB1, as the task's login (system here)
create table sqltask_drill (id number generated always as identity primary key,
  task varchar2(40) not null, at timestamp default systimestamp not null,
  sid number default sys_context('USERENV','SID') not null);
```

---

## 2. Register what runs — `sql-command-add`

One command per script. `sql_text` carries the SQL and the command writes it to
`assets/tasks/<db_type>/NNN_<name>.sql`, so the script is a reviewable file and never a string in
configuration. The request goes in a file (or on stdin), never in argv:

```bash
cat > mssql_10m.json <<'EOF'
{"display_name": "Lab drill MSSQL 10 min", "db_type": "sqlserver",
 "sql_text": "WAITFOR DELAY '00:10:00';\nINSERT INTO dbo.sqltask_drill(task) VALUES ('MSSQL_10M');\nSELECT task, COUNT(*) AS runs, MAX(at) AS last_at FROM dbo.sqltask_drill WHERE task = 'MSSQL_10M' GROUP BY task;\n",
 "note": "Lab drill: a 10-minute task beside a 5- and a 1-minute one, to watch overlap and long runs."}
EOF
dbabrain common sql-command-add @mssql_10m.json
```

The answer names what it wrote:

```
registered sql_id 1 (SQLSERVER-001-LAB_DRILL_MSSQL_10_MIN) - wrote assets/tasks/sqlserver/001_Lab_drill_MSSQL_10_min.sql, sql_commands.json
```

The same shape for the other engines, with their own wait:

```text
# PostgreSQL - statements, run one at a time
select pg_sleep(600);
insert into sqltask_drill(task) values ('PG_10M');
select task, count(*) as runs, max(at) as last_at from sqltask_drill where task = 'PG_10M' group by task;

# Oracle - one statement or block per batch, GO between them
BEGIN DBMS_SESSION.SLEEP(600); END;
GO
INSERT INTO sqltask_drill(task) VALUES ('ORA_10M');
GO
SELECT task, COUNT(*) AS runs, MAX(at) AS last_at FROM sqltask_drill WHERE task = 'ORA_10M' GROUP BY task;
```

Nine commands in the drill: 10, 5 and 1 minute on each engine, `sql_id` 1-9.

**Proves:** `dbabrain sql-tasks list-tasks --all` lists nine commands. A PostgreSQL command with
`parameters` is refused by name (*a postgresql task takes no parameters yet*), and an engine a task
cannot run on (`mysql`) is refused at registration, not nine hours later at its first run.

---

## 3. Say where and when — `sql-target-add`

One target per command: the lab, its database, and the schedule.

```bash
cat > t1.json <<'EOF'
{"sql_id": 1, "server_id": "LAB-192-0-2-49-MSSQL-1433", "database_name": "LABTEST",
 "time_window": {"from_hour": 0, "to_hour": 23, "from_day": 1, "to_day": 31,
                 "repeat_interval": 10, "retry_interval": 10, "timeout": 900},
 "active": true, "logging_on_run": true, "alert_on_error": true,
 "note": "10-minute run, due again 10 s after it starts."}
EOF
dbabrain common sql-target-add @t1.json
```

- **`timeout` is above the run's length.** It is the statement budget on every engine
  (`statement_timeout` on PostgreSQL, `call_timeout` on Oracle, the query timeout on SQL Server).
  The drill used the length plus five minutes.
- **`repeat_interval` counts from the last start.** At 10 s every task is due again as soon as it
  ends: a task never runs twice at once, so "every 10 s" means "back to back".
- **`logging_on_run`** sends *running* and *done* for each run. The drill had it on for the 10- and
  5-minute tasks and off for the 1-minute ones, which would otherwise send two messages a minute
  each. `alert_on_error` is on for all of them.
- Registered `active` inside an open window, a target runs at the next scan, a second later.

**Proves:** within a few seconds `dbabrain db sql-run-history '{"limit": 4}'` shows them running:

```
Last 4 SQL task run(s), newest first:
#8297 [RUNNING] sql_id=9 ORACLE-009-LAB_DRILL_ORA_1_MIN
    2026-09-25 17:40:08 +08 took - on LAB-192-0-2-49-ORA-1521
#8294 [DONE] sql_id=9 ORACLE-009-LAB_DRILL_ORA_1_MIN
    2026-09-25 17:39:07 +08 took 60s rows=2 on LAB-192-0-2-49-ORA-1521
```

`rows=2` is the count's one row plus the INSERT's one. A PostgreSQL run reads `rows=3`: its
`pg_sleep` is a result set of its own.

---

## 4. How many run at once — `max_parallel`

`APP-SQL_TASKS` is an `async` app. Every second it starts a **scan**, up to `max_parallel` scans at a
time. A scan reads the due tasks and runs them **one after another**. A task another scan has
already started is skipped, never run twice.

With the shipped `max_parallel` 4 and nine long tasks:

- four tasks ran at once, and the other five waited for a scan to finish;
- a scan that finished a short task went on to the next due one, so it chained a 10- and a
  5-minute task (the longest scan took 966 s, under the app's 1800 s `timeout`);
- a 1-minute task waited up to 848 s between runs.

Raised to 10 on a running node, no restart needed:

```bash
echo '{"app_code": "APP-SQL_TASKS", "max_parallel": 10}' | dbabrain common app-command-set -
# APP-SQL_TASKS: max_parallel 4 -> 10
```

Twenty seconds later all nine ran at once, and each task started again 0-5 s after its last run
ended: 55 runs in 14 minutes, none failed, none ran twice at once.

**Size it to the tasks, not higher.** Each scan is a process holding a database session. A
production node's slow tasks should not wait behind each other, but `max_parallel` is also what
stops a burst from opening a session per task on the same server. And keep the app's `timeout`
(1800 s) above the longest chain a scan can take: when the daemon kills a scan at its timeout, the
statement on the server keeps running.

---

## 5. What the bot says

With `logging_on_run` on, each run sends two messages to the `sql` chat:

```
[SQL] SQL task done
sql_code: POSTGRESQL-005-LAB_DRILL_PG_5_MIN
display_name: Lab drill PG 5 min
server_id: LAB-192-0-2-49-PG-5432
target_no: 1
sql_run_id: 8225
time: 2026-09-25 17:07:09 +08
message: SQL task POSTGRESQL-005-LAB_DRILL_PG_5_MIN finished on LAB-192-0-2-49-PG-5432/labtest in 300762 ms.
```

A failure names what failed and how to fix it, whatever `logging_on_run` says:

```
[SQL] SQL task error
sql_code: POSTGRESQL-004-LAB_DRILL_PG_10_MIN
...
error: could not reach LAB-192-0-2-49-PG-5432/labtest - the instance is down, its address or port is wrong, or something between them blocks it (SQL failed: timed out)
```

That one is real, and it was wrong: before 0.23.0 a PostgreSQL statement could not run longer than
the connect timeout (30 s). It is the message you get now when the lab really is down.

---

## 6. When the daemon stops mid-run

A restart kills the scans that were running. Their rows stay `running` until the next scan, which
closes each as an error (*stale running: pid ... is gone*) and runs the task again. The statement
the killed scan had sent may still be running on the server; a sleep ends by itself, a real task
may need its session killed by hand.

---

## 7. Stop them

A task that repeats every 10 s is a lot of work for a lab. Switch each target off by registering it
again with `"active": false`, its `target_no` and `"replace": true`. `replace` writes the whole
record, so send the whole request of section 3, not only the switch:

```bash
# t1.json from section 3, with "target_no": 1, "active": false, "replace": true added
dbabrain common sql-target-add @t1.json
```

Then put `max_parallel` back where the node's real work needs it.

---

## What bites people

| | |
| --- | --- |
| An Oracle script with two statements and no `GO` | It is one batch, and Oracle refuses it. Put a `GO` line between them |
| `BEGIN ... END` without the final `;` | PL/SQL requires it (PLS-00103). Keep it; the tool no longer strips it |
| A PostgreSQL task with `parameters` | Refused: they are T-SQL `DECLARE` lines. Write the values into the script |
| An Oracle task with `parameters` on a direct connection | Registered, then fails at its first run: the same `DECLARE` lines. Write the values into the script |
| `"replace": true` with only the field you meant to change | The target is rewritten from that request: its window and database are gone. Send the whole record |
| `timeout` below the run's length | The server cancels the statement at `timeout` |
| Nine long tasks and `max_parallel` 4 | Five of them wait; a short task can wait many minutes for its turn |
| `logging_on_run` on a 1-minute task | Two messages a minute, per task |
| No Oracle driver on the node | *oracledb ... is required*: install the `oracle` extra |

The runner's detail is in [`docs/05_sql_task_runner.md`](../docs/05_sql_task_runner.md), and
`run-sql`, which every task runs through, in [`docs/13_common.md`](../docs/13_common.md).
