# SQL Task Runner

## Purpose

The SQL Task Runner runs scheduled SQL task workflows against configured targets and records success/failure in the runtime store.

## Package / Files

- `db_ops/sql_tasks/`
- `data/sql_commands.json`
- `data/sql_targets.json`
- `assets/tasks/`
- the runtime store declared in `data/store_config.json` (PostgreSQL in this tree; `runtime/db_ops.sqlite` when the backend is `sqlite`)

## Runtime Tables

- Reads `sql_runs` to decide whether a target/task is due.
- Writes `sql_runs` for each attempted task run.
- May queue notifications in `telegram_send_messages` when configured.

## Config Files

`data/sql_commands.json` defines SQL command IDs/codes and SQL script definitions using `script_type` plus `script_path` or `script_paths`. `data/sql_targets.json` maps commands to database targets, credentials, optional `database_name`, repeat intervals, time windows, timeout, and alert behavior.

**Timezone**: `time_window` hours are the **node's local time — +07 (Asia/Ho_Chi_Minh) on both master and worker** — so `from_hour: 1` runs at 01:00 +07. Store rows (`sql_runs.created_at` etc.) are written in **UTC (+00)** on either backend; add +07 when reading them manually. See "Timezone convention" in [`docs/13_common.md`](./13_common.md).

For SQL Server targets, set `database_name` in `data/sql_targets.json` when a task must execute inside a specific database, for example `"database_name": "Globex_Prod"`. `service_name` identifies the database service/instance target; it is not the execution database name. When `database_name` is omitted, SQL Server tasks connect to `master`.

**Autocommit tasks.** By default a SQL Server task runs inside one transaction and commits at the end (atomic; a mid-script failure rolls back). Set `"autocommit": true` on the `sql_commands.json` entry to run with the connection in **autocommit mode** — no wrapping transaction, each batch commits on its own. Required for procedures that refuse to run inside an open transaction, e.g. `schedule.usp_Run_V2` raises `must not be called inside an active transaction because RUNNING logs must be committed before SQL execution`. Note two things also apply to such scripts: an EXEC parameter must be a **constant or variable**, never an inline expression (`@p = CAST(DATEADD(...) AS DATE)` fails with *Incorrect syntax near 'DATEADD'* — compute it into a `DECLARE`d variable first); and `GO` batch separators are honored (each batch runs in order).

SQL files live under `assets/tasks/`. `script_type=array` runs multiple files in the configured order, `script_type=folder` runs discovered `*.sql` files sorted by filename, and execution stops at the first failed file. Folder tasks treat filenames as execution order: schema or table-structure changes that stored procedures depend on must have an earlier prefix such as `001_...`. Before running or approving a folder task, use dry-run output to inspect `file_order=[...]`, not just the file count.

## Data Flow

SQL command config + target config + credentials + SQL files -> due/time-window check -> SQL execution -> `sql_runs` row -> optional Telegram queue row for logging or error alerts.

**The SQL itself runs in `python -m db_ops.common.cli run-sql`** (2026-08-16), one request object
per task target, not on a connection this app opens. It used to open one — and therefore carried
its own opinion on driver choice, the ODBC→pymssql fallback, batch splitting and which transport
an Oracle 8i target uses. `execute_on_target` now states the run and reads the answer:

| What the task decides | Request field |
| --- | --- |
| where, and as whom | `target` (server_id), `database`, `credential_name` |
| the commit mode | `autocommit` / `commit` — an autocommit task commits per batch, which is what procs rejecting `@@TRANCOUNT > 0` need |
| **two** timeouts, kept apart | `timeout_seconds` (statements) and `connect_timeout_seconds` — a task allowed twenty minutes must not wait twenty minutes to learn the host is down |
| how many rows and sets to keep | `max_rows`, `capture: "all"`, `max_result_sets: 0` |
| what a parameter means here | `prelude` + `params` (bound) on a normal target; `define` (SQL*Plus substitution) on an 8i one, which binds nothing |
| which transport | `sql_access`, straight off the target |

Every result set is asked for and the first **five** are stored, as they always were; the rows of
the rest are still counted into the run's `row_count`, so a task never reports fewer rows than it
read. Before the switch both resolvers were run over all 12 configured task targets and agreed on
ip, port and login for every one.

## JSON Export Outputs

SQL task `14` (`SQLSERVER-014-EXPORT-TICKET-DETAIL-JSON`) is used by Telegram command `/spbot_json_exp_ticket_detail`. The SQL runner stores the task result in `sql_runs.result_json`; the Telegram command then writes the `ResultJson` column to a JSON file under:

```text
runtime/output/telegram/json_exports/json_exp_ticket_detail_YYYYMMDD_HHMMSS/
```

The timestamped folder name is deterministic and safe for Docker/Linux paths. The folder is created before writing the file; if it cannot be created, the Telegram command reports a clear folder-creation error.

Normal SQL task runtime output is written to `logs/sql_tasks_runtime.log`. Telegram failure replies should not dump normal `LOGGING` lines from that file/stdout; they should show stderr or the meaningful final error line.

## How to Run

```powershell
# `db_ops.sql_tasks.cli` is an equivalent alias (uniform <app>.cli convention)
python -m db_ops.sql_tasks.runner --config config.json
python -m db_ops.sql_tasks.runner --config config.json --dry-run
python -m db_ops.sql_tasks.runner --config config.json run-sql-id --sql-id 9 --force
python -m db_ops.sql_tasks.runner --config config.json --dry-run run-sql-id --sql-id 9 --force
```

`--dry-run` is a **top-level** flag: it goes before `run-sql-id`, not after. Placed after the
subcommand it is an argparse usage error, not a silently ignored option.

### From Telegram

`/spbot_run_sql_task <sql_id>` runs the same thing on the worker — `sql_id` is the number
`/spbot_list_sql_tasks` prints. It runs in the background (configured task timeouts reach
7200 s, too long for a foreground reply) and each target reports its own result as a separate
message, because the runner already queues those itself.

It always passes `--force`, which this CLI *requires* for a targeted run. That is the part to
be aware of: a forced run skips the time window, the repeat interval **and the active flag**, so
a task `/spbot_list_sql_tasks` hides as inactive still runs when its id is named. Deliberate —
an operator who disabled a schedule may still want one manual run — but it is why the command
sits at clearance 10 with `/spbot_restore` and `/spbot_add_sql` rather than with the listings: a
SQL task may be an UPDATE against production.

## Parameters

A task may declare parameters, and the script then reads them as ordinary T-SQL variables. Added
2026-08-12; a task without a `parameters` block behaves exactly as before.

```json
{
  "sql_id": 12, "sql_code": "SQLSERVER-012", "db_type": "sqlserver",
  "script_path": "tasks/sqlserver/kill_report.sql",
  "parameters": [
    {"name": "spid", "type": "int", "required": true},
    {"name": "db",   "type": "nvarchar(128)", "default": "SALESDB"}
  ]
}
```

```sql
-- the script just uses them; nothing declares them itself
SELECT * FROM sys.dm_exec_sessions WHERE session_id = @spid;
```

**On an Oracle 8i target the substitution is textual, not bound.** Those targets run through
the legacy tool (`sql_access.method`, see below), which takes one statement and no bind list, and
their scripts are archived SQL*Plus files that refer to `&JOB_NO` — a marker that appears where a
bind variable is not legal anyway. So a parameter value is expanded the way SQL*Plus itself would
expand it, over the script's own `DEFINE` line. Because the value is pasted into the statement, it
is validated first: a value carrying a quote, a comment marker or a `;` is refused, and a
parameter name the task does not declare is refused rather than silently matching nothing (which
would run the archived script's own job number and look like it worked).

**A value may be given without its name.** `--param session_id=1068` and a bare `1068` mean the
same thing for a task whose first parameter is `session_id`: bare values fill the declared
parameters in order, skipping any already named, and are bound once the task is loaded
(`bind_parameter_values`). This is for the person answering `/spbot_run_sql_task` on a phone —
the prompt has just told them which parameter it wants, so demanding `NAME=VALUE` back is the
bot being obtuse about something it already said. More bare values than the task declares is an
error naming what it takes, never a silent drop.

Values come in on the run:

```bash
python -m db_ops.sql_tasks.runner --config config.json run-sql-id --sql-id 12 --force     --param spid=505 --param db=SALESDB
python -m db_ops.sql_tasks.runner --config config.json run-sql-id --sql-id 12 --force     --params "spid=505 db=SALESDB"
```

`/spbot_run_sql_task 12 spid=505 db=SALESDB` from Telegram. Two spellings because a shell repeats a
flag naturally while a Telegram command renders one template per argv entry and has a single slot
to fill; `--params` is split with shell quoting rules, so `"note=needs a look"` survives.

**The value is bound, never interpolated.** `common.sql_execution.build_parameter_prelude` emits
`DECLARE @spid int = ?;` and hands the value to the driver — the caller is a chat message, and
pasting it into the script is how that becomes arbitrary SQL. Only the **name** and the **type**
reach the SQL text, because neither can be bound, so both are validated first: the name against an
identifier pattern, the type against the `SQL_PARAMETER_TYPES` allow-list (`int`, `nvarchar(128)`,
`decimal(18,2)`, `date`, … — `sysname` and anything else is refused).

The declaration is repeated in front of **every batch**: a T-SQL variable does not survive a `GO`.

| Case | Result |
| --- | --- |
| value given | bound as typed |
| no value, `default` declared | the default is bound |
| no value, `required: true` | refused **before connecting** |
| no value, no default | `NULL` is bound — so `WHERE @db IS NULL OR name = @db` means "all" |

### A SQL task and a `common` command may answer the same question

`sql_id` 18 (`trace_open_transactions`) returns much the same rows as
`python -m db_ops.common.cli trace-session`. Compared side by side on 2026-08-12 against
192.0.2.115 they agreed exactly: 33 transactions each, same SPIDs, same application users.

**Keep both.** They are not two implementations of one thing; they are the same question asked from
two layers, and the difference is *where estate-specific knowledge is allowed to live*:

| | `common.cli trace-session` | `sql_id` 18 |
| --- | --- | --- |
| Belongs to | every estate — a shared capability | **this** estate's SALESDB, named in `data/` |
| Knows about Dynamics AX | as little as possible | as much as the script wants |
| Runs where | any SQL Server; degrades if `USERINFO` is absent | SALESDB only — the `USERINFO` join will not compile elsewhere |
| Leaves behind | JSON on stdout | a row in `sql_runs`, a Telegram message, an xlsx |
| Changed by | a code change and a release | editing a `.sql` file and a JSON entry |

db_ops does not only serve the ERP. A capability that hard-codes one ERP's tables into the shared
layer is the thing to avoid; a SQL task that does so in `assets/` and `data/` is exactly where that
belongs. So the general command stays general, and anything specific to SALESDB goes in the task.

## Useful Manual Queries

```sql
SELECT sql_run_id, created_at, run_key, sql_code, target_no, server_id, database_name, status, duration_ms, row_count, error_text
FROM sql_runs
ORDER BY created_at DESC, sql_run_id DESC
LIMIT 50;

SELECT run_key, max(created_at) AS latest_run
FROM sql_runs
GROUP BY run_key
ORDER BY latest_run DESC;
```

## Common Issues

- Task is not due: check the latest `sql_runs` row, target interval, and time window.
- `run-sql-id --force` behaves differently: it bypasses active/time-window/interval checks for matching SQL ID targets.
- SQL file fails midway: multi-file tasks stop at the first failure.
- Stored procedure alters fail in a folder task: check `file_order=[...]`; a DDL/table-change script with a later filename can run after dependent procedure files and break deployment.
- Telegram alert is missing: check `logging_on_run`, `alert_on_error`, and pending rows in `telegram_send_messages`.
- A run is `error` with `metadata_json` = `{"stale_running": true}`: nothing failed in the SQL — the
  run's *process* died and the row was reaped. See *Stale running rows*.

### Reading the history back

`list-tasks` answers *what tasks exist*. The other question — *what ran, and how did it end* — is
`db-ops db sql-run-history`, a JSON-object command like every other:

```bash
python -m db_ops.db.cli sql-run-history '{"limit": 10}'
python -m db_ops.db.cli sql-run-history '{"limit": 20, "sql_id": 28}'
```

It lives in `db.cli` rather than here for the reason `restore-drill-status` does: the question is
asked **by** operators and reports, not by the app that performs the work. `sql_tasks` runs tasks
and records them in `sql_runs`; `db_ops.common.sql_run_history` reads that record, and the two never
import each other. `/spbot_list_sql_runs` is the same command from Telegram.

The output is lines rather than JSON because its first reader is a person who has just been paged.
A failed run carries the first line of its reason, so the answer does not require opening the store.

### Stale running rows

Every scan begins with `mark_stale_running_sql_runs`. A run row still `running` past its target's
`timeout` is closed as `error` with `metadata_json` = `{"stale_running": true}`, so the run_key is
free and the task can be scheduled again — `due_sql_tasks` refuses to start a target whose latest
run is `running`, so without this one dead process would stop the task for good.

Two things this **cannot** do, and both have bitten:

- **It does not stop the SQL.** The runner executes a task inline in the scan process, so when the
  daemon kills that process at the `APP-SQL_TASKS` command timeout, the session on the database
  server is left executing. Closing the row says the *run* is over, not the work. A task that
  guards itself with an application lock will then report SKIPPED every cycle until that orphaned
  session is killed by hand.
- **It cannot see how long a task really takes.** Set the target's `timeout` above the longest real
  cycle, and keep it below the `timeout` on the `APP-SQL_TASKS` entry in `data/app_commands.json`
  (1800 s) — above that the daemon kills the scan first and the reaper only records the aftermath.

It reports what it closes: a target with `alert_on_error.enabled` gets a Telegram message naming the
run and warning that the SQL may still be executing. Before 2026-09-03 it did not, and sql_id 28 sat
in `error` for half an hour with no message anywhere while its orphaned session blocked every
following cycle.

## Config Priority

The SQL task runner resolves its config file using this chain:

1. `--config <path>` CLI argument.
2. `DB_OPS_SQL_TASKS_CONFIG` environment variable.
3. `config.sql_tasks.json` next to `config.json`, or in the current working directory.
4. `config.json` shared fallback.

The selected source is printed to stderr on startup.

App-specific config file: `config.sql_tasks.json`

## Standalone Mode vs Full-Suite Mode

**Full-suite mode** (default): the runner reads `config.json`, resolves `REPO_ROOT` paths for secrets and inventory, and queues Telegram alerts into the shared runtime store.

**Standalone mode**: copy `config.sql_tasks.json`, `data/sql_commands.json`, `data/sql_targets.json`, and relevant `assets/tasks/` files next to the EXE. Point the store at a local path - a standalone EXE is the one layout where `sqlite_path` is still the natural setting, because it has no shared server. Credential and inventory files must also be co-located or the runner will attempt connections without them (gracefully returning empty results).

Required config keys: `log_dir`, plus a resolvable runtime store (`store_config_file`, an inline `store` block, or `sqlite_path`).

## Optional Integrations

**Telegram alerts**: each SQL target carries two notify switches, `logging_on_run` (fired on run start/finish) and `alert_on_error` (fired on failure). Each is an object:

```json
"logging_on_run": { "enabled": true, "telegram_chat": "logging", "chat_id": "" },
"alert_on_error": { "enabled": true, "telegram_chat": "error",   "chat_id": "" }
```

- `enabled` — whether to notify at all.
- `telegram_chat` — the notify level whose group receives the message (`logging`/`warning`/`critical`/`error`/`test`/`private`); resolved against the level map in `data/telegram_groups.json` (`level_chat_map`).
- `chat_id` — an explicit chat_id override; when non-empty it wins over `telegram_chat`, routing that target to a specific chat.

`alert_on_error` fires on **both** ways a run can fail: the exception path inside `run_sql_target`,
and `mark_stale_running_sql_runs`, which closes out a run whose process died before it could report
anything (see *Stale running rows* below). The second one used to write the error row and log it and
nothing else, so a killed run was silent in Telegram no matter what the target asked for.

The legacy boolean form (`logging_on_run: true`) is still accepted and behaves as `{enabled: true, telegram_chat: "logging"}` (and `alert_on_error: true` → `error`). When the resolved chat_id is empty (unmapped level and no override), no row is written. If the Telegram app is not running, the queued row remains pending until it is processed. The runner does not import or call the Telegram app directly.

Use `python -m db_ops.telegram.cli groups` to print the whole level -> chat_id map, or `python -m db_ops.telegram.cli route <level>` for one level's `{enabled, alert, chat_id}`. Routing is the Telegram app's, so it answers; `common` reads no Telegram settings.

## EXE Packaging Notes

- Credentials come from `data/encrypted_secret_text.json` (decrypted with the `--key_base64`/`--key` passphrase or `DB_OPS_SECRET_KEY`) and targets from `data/db_instances.json`, both resolved through `db_ops/common/data_sources/`. Provide the `data/` directory and supply the key, or tasks will run without credentials.
- SQL file paths in `sql_commands.json` are resolved relative to `TOOL_ROOT` and `REPO_ROOT`. Use absolute paths when packaging as EXE.

## Adding a SQL task at runtime (add-sql admin)

`python -m db_ops.common.cli add-sql` registers a new single-script SQL task **and enables it**
without a redeploy. The engine is `db_ops/common/config_admin.py`; this app does not import it,
it is one of the command's two callers (the other is the `/spbot_add_sql` conversation). A
`db_ops.sql_tasks.config_admin` shim used to offer a second name for the same command and was
deleted on 2026-08-15.

It performs three atomic writes, in dependency order:

1. writes the `.sql` text to `assets/tasks/<db_type>/<server_id>/<NNN>_<name>.sql`
   (folder keyed by the unique `server_id`), resolved by `runner.resolve_sql_file`;
2. appends a `sql_commands.json` entry (`script_type='single'`, `active`);
3. appends a `sql_targets.json` entry (server / instance / credential + time window, `active`,
   plus the `output` block described below).

Each write is a temp-file + `os.replace`, so a crash never leaves half-written config, and the
`.sql` file is written first so an enabled command never points at a missing script. `sql_id`
auto-increments from the current max; `sql_code` is derived as `<DBTYPE>-<NNN>-<NAME>`.

CLI — a JSON object, like every other `common` command:

```bash
python -m db_ops.common.cli add-sql '{"db_type": "sqlserver",
  "server_id": "ACME-192-0-2-250", "instance_name": "APPDB",
  "sql_name": "Nightly cleanup", "from_hour": 20, "to_hour": 23,
  "repeat_interval": 3600, "timeout": 600, "sql_file": "./cleanup.sql"}'
```

The flag form (`add-sql --db-type ... --sql-file ...`) is still accepted so pasted runbook lines
keep working; every flag is the same key without the dashes. `"inactive": true` registers without
enabling, `"manual_only": true` is a shortcut for `"repeat_interval": -1`, and
`"output": "none|plain|xlsx|csv|txt|xml"` sets the delivery format.

**Both forms answer in the standard response envelope** (`success` / `operation` / `message` /
`error` / `data` / `metrics`), and invalid input — unknown `db_type`, empty name, bad time window,
unknown output, a misspelled request key — comes back as `success: false` with the reason in
`error`, before any file is touched. Until 2026-08-15 a refusal printed `ERROR: …` on **stderr**
with exit 2 and nothing on stdout, which is why nothing could call this command programmatically.

### `list-tasks` — what tasks exist, as JSON

```bash
python -m db_ops.sql_tasks.cli list-tasks              # every runnable task
python -m db_ops.sql_tasks.cli list-tasks --sql-id 19  # one, even if inactive
python -m db_ops.sql_tasks.cli list-tasks --all        # include inactive
```

Prints one JSON object: `sql_tasks[]` with each task's `parameters` / `parameter_names` /
`required_parameter_names`, its `targets` (server, schema, output format, time window,
`sql_access_method`), plus `command_count`, `target_count` and `hidden_count`.

**This is how other apps ask.** It needs no runtime store and no secret key, so a caller can
list tasks while the database is down. The Telegram bot uses it for `/spbot_list_sql_tasks` and
to decide whether `/spbot_run_sql_task` must ask for parameters — it used to read
`sql_commands.json` itself and re-implement which tasks count as runnable, so it could answer
differently from the runner, and a task's declared parameters were not part of its picture at
all: `/spbot_run_sql_task` never asked for one, and every run of a task that required a
parameter failed telling the operator to pass a `--param` they were never asked for. The
listing is built from the same loaders the runner executes with, so it cannot drift from what
running the task would do.

### Oracle 8i targets (`sql_access`)

A target whose db_instance declares `sql_access.method` = `api`/`subprocess` does not get a
database connection: its SQL goes through the legacy Oracle tool
(see [`docs/13_common.md`](./13_common.md) → `oracle_bridge`), because no driver db_ops can
install speaks to 8.1.7. The runner reads the block from
`db_instances.json` **by server_id** — the transport belongs to the server, so one entry covers
every task on it — and a `sql_targets` entry may override it.

Two things change shape on that transport, and nothing else does:

- the target's **`database_name` means schema** (Oracle connects to a service), issued as
  `ALTER SESSION SET CURRENT_SCHEMA` — which is what lets a task's DBA login run an
  application's unqualified script instead of failing with ORA-00942;
- **parameters are substituted textually**, as described under [Parameters](#parameters).

`ORACLE-019-GET_JOB_DETAILS` is the worked example: an archived SQL*Plus script an operator
already had, one `job_no` parameter, schema `LTR`, xlsx out.

### Schedule: manual runs (`repeat_interval: -1`)

A target whose `time_window.repeat_interval` is **`-1`** is never picked up by the scheduler. It
runs only when someone forces it — `run-sql-id --force`, i.e. `/spbot_run_sql_task`.

`-1` (`MANUAL_ONLY` in `db_ops/lib/time_window.py`) is a shared scheduling value like
`0` (`RUN_ONCE`), so it lives in the target's own `time_window` rather than in a separate
`manual_only` key. One place says when a task runs, and a `manual_only: true` sitting next to a
`repeat_interval: 3600` can never disagree with itself.

Two values it is deliberately **not**:

- **`repeat_interval: 0`** is *run-once*, and run-once still runs: `job_due` returns `True` while
  the entry has never run. `0` therefore cannot express "only when a human asks".
- **`active: false`** stops the scheduler too, but `/spbot_list_sql_tasks` hides inactive entries
  as "not in use" — wrong for a task the operator runs by hand every week. A manual target stays
  `active` and is listed as `manual (run with /spbot_run_sql_task)`.

Nothing the scheduler does starts a manual target: no first run, no retry-on-failure, no
stale-running recovery. (A stale `running` row left behind by a forced run is still cleaned up —
that is `mark_stale_running_sql_runs`, which does not consult the schedule, and alerts on what it
closes — see *Stale running rows*.) The entry keeps its
day/hour bounds in the JSON and `timeout` still applies to a forced run, but the bounds are never
consulted, so the listing does not print them.

Only `-1` is accepted; any other negative `repeat_interval` is still rejected, so a typo like
`-300` fails loudly instead of silently turning a five-minute task into one that never runs.

### Result delivery: the `output` block

What a finished run does with its rows is a property of the **target**:

```json
"output": { "format": "xlsx", "telegram_chat": "sql", "chat_id": "" }
```

| `format` | What the run delivers |
| --- | --- |
| `xlsx` `csv` `txt` `xml` | The first non-empty result set is written to `runtime/output/sql_tasks/sql_<NNN>_<task>_<stamp>.<format>` and queued as **its own Telegram document** message. The inline table is suppressed — it would only repeat the attachment. |
| `plain` | The rows are rendered as a markdown table inside the run message. **Every fetched row is shown** — a body past Telegram's 4096-char limit is split across messages by `db_ops.lib.telegram_text.split_telegram_message` rather than clipped, because the rows that used to fall off the end were the ones somebody ran the task to see. Still clipped to 8 columns × 24 characters per cell, so a row stays on one phone-width line. How many rows are fetched is `output.max_rows` — see below. |

### `output.max_rows` — how much of an answer reaches the chat

Inline output used to fetch a flat 100 rows, decided in the runner. That number was invisible and
harmless only because the message showed the first 20 anyway; once every fetched row started being
rendered, it became the thing silently deciding how complete an answer was. It is now config:

```json
"output": {"format": "plain", "telegram_chat": "sql", "max_rows": 250}
```

- Absent → `DEFAULT_INLINE_MAX_ROWS` (**1000**).
- Range `1..MAX_INLINE_MAX_ROWS` (**5000**); anything else is **refused when the target loads**,
  not clamped — a 20000 quietly reduced to 5000 looks like it worked until somebody counts rows.
- File formats ignore it entirely and fetch `XLSX_MAX_ROWS` (50 000).

**The ceiling is Telegram, not memory.** Rows leave as ~3900-character messages, so roughly 30 rows
per message, and Telegram rate-limits a group to about 20 messages a minute. A genuinely uncapped
result would not simply be long — it would 429 partway through and arrive in pieces. Past a few
hundred rows, `output.format: "xlsx"` is the right answer, not a bigger `max_rows`.

`STORED_RESULT_MAX_ROWS` (100) is **separate** and unchanged: it bounds what goes into
`sql_runs.result_json`. It used to be defined as `= MAX_RESULT_ROWS`, so raising what an operator
sees in chat would have multiplied the size of every stored run row as a side effect. The store's
budget and the reader's are different questions and now have different numbers.
| `none` | The task runs and reports success/failure only. What a maintenance `UPDATE` wants. |

**`output` and `notify` are required on every target, and a missing one is refused by name.**

An absent `output` used to mean `plain`, so that targets written before the field existed kept
pasting their rows into the run message. The reasoning was sound and the conclusion was half
right: `none` must never be inferred from silence — but neither must anything else. Thirteen of
this estate's seventeen targets said nothing, which meant reading `sql_targets.json` could not
answer *"what does `/spbot_run_sql_task 22` send back?"*; the answer lived in a default inside
the loader. Those thirteen now say `plain` in the file, which is exactly what they were already
doing, so nothing changed except that it is now written down.

A block that is *present* and leaves `format` empty still means `none`. That inference survives
because the operator wrote the block: the silence inside it is theirs, not the file's.

`add-sql` has always asked for `output` and marked it required, so a task registered through the
documented path is unaffected. `tests/test_sql_target_output_is_declared.py` holds both halves —
the shipped configuration declares one everywhere, and the loader refuses an entry that does not.

**The rows go to whoever asked**, in whatever form the target produces. A file always did;
an inline table did not, so `/spbot_run_sql_task` in one chat answered "finished" there and
printed the actual rows in the task's configured group — which the person who ran it may not
even be in. Now a run with `--output-chat-id` queues its table to that chat as its own message
and the run log keeps only its status lines, exactly as an export has always behaved. A
scheduled run has no requester, so its rows stay on the log line as before.

**Where the export is delivered**, most specific first:

1. `--output-chat-id` on the run — `/spbot_run_sql_task` passes the chat that asked, so a file
   someone requested by hand comes back to them. Without this the first export went to the
   `Ops - Logging` group while the operator who ran the command saw only "finished" and no file.
2. the target's `output.chat_id`, then its `output.telegram_chat` notify level;
3. failing both, the target's `logging_on_run` chat — a target that sets a format but no chat
   still delivers rather than dropping the file silently.

The document is queued as **its own message**, not attached to the run log: the log is an audit
line that belongs in the notify chat, the file is a deliverable that belongs where it was asked
for — and a target with `logging_on_run` disabled must still receive the export it configured.

**Row cap.** A task normally captures at most `MAX_RESULT_ROWS` (100) rows per result set: that is
the preview stored in `sql_runs.result_json` and pasted into the message. A target that exports a
**file** (any of `xlsx` / `csv` / `txt` / `xml`) raises it to `XLSX_MAX_ROWS` (50 000, the same ceiling `/spbot_sql_to_xlsx` uses) — otherwise the
workbook holds the first 100 rows of a 5 000-row answer and nothing says so, because the file
looks complete. What lands in the store is trimmed back to 100 rows plus a `rows_omitted` count,
so one export does not become a multi-megabyte row in Postgres; `row_count` stays the real total.

A script that returns no result set produces no workbook (a one-empty-sheet file tells the
operator nothing); the run still reports its own success. A failure to *write* the workbook never
fails the task — the SQL already ran and committed, so the run is recorded as done with an
`output_note` in its metadata.

The workbook writer is `db_ops/lib/xlsx_export.py`, shared with `/spbot_sql_to_xlsx`, so a
task-produced file and an ad-hoc one are the same artifact. The other file formats go through
`db_ops/lib/result_format.py`, the same renderer `run-sql`'s `"format"` uses — so a scheduled
export and an ad-hoc one cannot drift into two different-looking files. In `csv` a SQL NULL is an
empty **unquoted** field and `""` is the empty string (PostgreSQL's `COPY ... WITH CSV`
convention); in `txt` and `xml` it is spelled out. Which set of formats writes a file is
`config_admin.FILE_OUTPUT_FORMATS` — three separate decisions read it (row cap, write the
document, suppress the inline table) and each was spelled `== "xlsx"` before the others existed.

### Telegram: `/spbot_add_sql` (admin, multi-step)

The same engine is exposed as the Telegram command `spbot_add_sql` (`command_type=10`, private
chat only). The bot walks the operator through five prompts — **server_id → sql_name → schedule
→ output → SQL body** — via the standard conversation-state machinery.

**`db_type`, instance and target database are not asked for.** `db_instances.json` already
records all three against the server, so `resolve_target_from_server_id` fills in `db_type`,
`service_name`, `instance_name` and `credential_name` from the `server_id` alone. Asking made the
conversation four messages longer *and* gave the operator three more chances to enter something
that does not resolve — which is exactly how `SQLSERVER-017` was created with a null instance and
a task that could never find its database. The one case `server_id` genuinely cannot answer is a
server running two instances; that is rejected with a message naming them, rather than guessed,
because picking one would run the SQL against a database nobody named.

The **schedule** step accepts `manual` (written as `repeat_interval: -1`), `default`, or
`from_hour to_hour repeat_interval timeout`. The **output** step accepts `xlsx` / `csv` / `txt` / `xml` / `plain` / `none`.

The final SQL-body step accepts the SQL **inline** (paste as one message) **or as a
`.sql` file attachment** (the `sql_text` parameter is flagged `accept_file`; the worker downloads
the document via `getFile`, decoding UTF-8/BOM). It then runs
**`python -m db_ops.common.cli add-sql`** with the collected values as one JSON object and replies
with the assigned `sql_id`, `sql_code`, and written path. The write engine lives in `common` (not
in the `sql_tasks` app) precisely so both this Telegram action and an operator at a shell reach it
**without one app importing another** — and since 2026-08-15 the bot reaches it the same way the
operator does, through the CLI, rather than importing the engine (see `docs/13_common.md`).
`config_admin` is a pure atomic-write helper (no DB, no execution path). Every *other* cross-app
interaction (the bot running reports/backup-restore/sre) still goes through that app's CLI as a
subprocess (see `docs/07_telegram_app.md`). The new `.sql` file lands under the worker's now
read-write `assets/` mount; sync it back to the master with `control worker-pull-data-config
--writeback-config --include-sql` (see `docs/11_control_app.md`).

> Runtime writes happen on the worker's `data/` and `assets/` mounts. The master stays authoritative
> for secrets; pull config back explicitly rather than pushing from the worker.
