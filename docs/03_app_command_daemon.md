# App Command Daemon

## Purpose

The App Command Daemon scans active app commands, checks `time_window` repeat intervals and allowed date/time ranges, avoids duplicate running commands per `app_command_id`, and starts SQL tasks, metrics, reports, Telegram, restore, and SLA apps as independent subprocesses.

## Running on request

A command runs for one of **two** reasons: it is due on its schedule, or somebody asked for it.
The second is a row in `app_command_requests`, written by the web console's "Run now" button or by
`python -m db_ops.db.cli run-app`; the daemon consults the queue on every scan and starts the
command exactly the way the schedule would have.

A request deliberately **overrides both scheduling gates** — the allowed-hours window and the
repeat interval — because "run it now" is asked at the moment somebody needs the answer, which is
usually outside the window and usually right after the last run. It does **not** override
"already running": starting a second copy of a command in flight is how two collectors write the
same metric run.

The request is claimed with a conditional `UPDATE ... WHERE status = 'pending'` before the spawn,
so two daemons on one store cannot both act on it, and it is released back to pending if the spawn
itself fails. The resulting `job_runs` row carries `run_request_id` and `requested_by` in its
metadata, so a run at an odd hour is explainable from the run history alone. The daemon closes the
request as it reaps the process, and sweeps any left `started` by a daemon that was killed first.

See [01_runtime_store.md](01_runtime_store.md) for the table and
[12_webhost_app.md](12_webhost_app.md) for the button.


## Package / Files

- `db_ops/jobs/`
- `data/app_commands.json`
- `config.json`
- `logs/jobs.log`
- `logs/jobs_runtime.log`

## Runtime Tables

- Reads `job_runs` to find the latest row for each `job_code`/`app_command_id`.
- Writes `job_runs` when each app command starts and when it finishes, errors, or times out.

## Config Files

`data/app_commands.json` is the source of truth. Important fields are `app_command_id`, `app_code`, `app_name`, `display_name`, `log_scope`, `working_dir`, `command_text`, `time_window`, and `active`.

`time_window.repeat_interval` and `time_window.timeout` are in seconds by default. `repeat_interval` is measured from the previous run's `started_at`, not from `finished_at` — and that is true of **every** app that carries a `time_window`, not only this one: the rule, the anchor column and the explanation all live in `db_ops/lib/time_window.py` (`due_from_row`, `run_anchor`, `explain_due`), and [`configuration.md` §5](./configuration.md) is the field reference. For example, if an app has `repeat_interval: 10` and the previous run started at `00:00:00` then finished at `00:00:09`, the app becomes due again at `00:00:10` — at `00:00:09.5` in fact, because a run may start up to `min(interval × 5%, 30s)` early so a scan boundary does not cost a whole cycle. If that same `app_command_id` is still running when it becomes due, the daemon skips it until the running process exits or times out. Date/time bounds use `from_*` and `to_*` names, for example `from_day`, `to_day`, `from_hour`, and `to_hour`.

**Special `0` values (shared convention).** `repeat_interval`, `retry_interval`, and `timeout` accept `0`, which is interpreted consistently everywhere a `time_window` drives scheduling — app commands (`job_due`), SQL tasks, metrics, and reports (all via `db_ops/lib/time_window.py`):

- `repeat_interval: 0` — **run once**: due only when it has never run yet; it is *not* repeated after a successful run (a failed run is still retried after `retry_interval`, and a stale `running` row is recovered). This is the careful fix for the old "repeat every 0 seconds" trap.
- `timeout: 0` — **no timeout-kill**: the daemon never terminates the process for running too long. Use this for long-running services.
- `retry_interval: 0` — **retry/restart immediately** once the previous run has exited.

A long-running service (e.g. `APP-WEBHOST`, the report web host) combines all three: `repeat_interval: 0` (started once), `timeout: 0` (never killed while serving), `retry_interval: 0` (restarted at once if it dies). Leave the `from_*`/`to_*` date-time bounds `null` for such services — do **not** set them to `0` (e.g. `to_hour: 0` would only open the window at midnight).

The current configured commands are `APP-SQL_TASKS`, `APP-METRICS`, `APP-REPORTS-CREATE`, `APP-SLA-VALIDATE`, `APP-BACKUP-RESTORE`, `APP-TELEGRAM`, and `APP-WEBHOST`. The inventory was an app command of its own
(`APP-REPORTS-INVENTORY-WORKFLOW`) until 0.22.0; it is a report now - `rp_inventory_health` in
`reports_config.json`, built by `APP-REPORTS-CREATE`'s pass - and `upgrade-config` moves an older
node's app command there, keeping its schedule.

## Data Flow

`data/app_commands.json` -> active command filter -> local `time_window` check -> duplicate-running check by `app_command_id` -> latest `job_runs.started_at` interval check -> subprocess start with `DB_OPS_LOG_SCOPE` environment -> runtime log files -> final `job_runs` update.

The `time_window` check uses the **configured timezone** (`config.json` → `timezone`), so `from_hour: 1` means 01:00 in that zone on every node regardless of the host clock. `job_runs` timestamps are stored in **UTC (+00)** and are unaffected by it — see "Timezone convention" in [`docs/13_common.md`](./13_common.md).

## The scan interval is a floor under every schedule

The daemon wakes every `--delay-seconds` (**1** by default, and the minimum the code accepts),
reads `data/app_commands.json` and asks each command whether it is due. A command's own
`repeat_interval` is therefore only ever checked that often, and its tasks' intervals only when the
command runs — three clocks in a row, each a floor under the next:

| | Default | Decides |
| --- | :-: | --- |
| `--delay-seconds` | 1 s | how often any command is considered |
| `app_commands[].time_window.repeat_interval` | per command | how often that app is started |
| the app's own config (a SQL task's target, a backup job) | per entry | when the work actually runs |

Both of the outer two are one second for the two apps that are polled rather than scheduled —
`APP-TELEGRAM` and `APP-SQL_TASKS` — so neither is ever the answer to "why did it run then". A
scan reads one JSON file and compares timestamps, and an app with nothing due exits immediately;
the cost of asking is far below the cost of an interval nobody can see in the config they are
reading.

## How to Run

Run forever with a 1-second scan interval:

```powershell
# python -m db_ops.jobs.cli [daemon|status] is an equivalent alias (uniform <app>.cli convention)
python -m db_ops.jobs.daemon --config config.json --delay-seconds 1
```

Run one scan and wait for started commands to finish:

```powershell
python -m db_ops.jobs.daemon --config config.json --once
```

**`--once` waits for the jobs it started, and does not start a service.** A command with
`timeout: 0` is a long-running service (`AppCommand.timeout_disabled`) — `APP-WEBHOST` serves and
never completes by design — so a single pass skips it and logs
`app.daemon.command.skip_service reason=long_running_service_not_run_by_once`. Until 2026-09-11 it
started it and then waited for ever; that is why the web host shipped `active: false` from v0.4.0,
a flag hiding the defect instead of fixing it. Every shipped command is active now, and on a fresh
`db-ops init` root one `--once` pass finishes every job `status=done` in a few seconds —
`tests/test_every_app_command_runs_on_an_unconfigured_install.py` holds it to that. A service is
proved by starting the daemon normally, not by `--once`.

Use a different data directory:

```powershell
python -m db_ops.jobs.daemon --config config.json --data-dir data --once
```

## Useful Manual Queries

```sql
SELECT log_id, job_code, status, started_at, finished_at, duration_ms, error_text
FROM job_runs
WHERE job_code LIKE 'APP-%'
ORDER BY created_at DESC, log_id DESC
LIMIT 50;

SELECT job_code, max(created_at) AS latest_seen
FROM job_runs
GROUP BY job_code
ORDER BY latest_seen DESC;
```

## One at a time, or several: `run_mode`

An app command declares how the daemon calls it:

```json
{"app_command_id": "APP-SQL_TASKS", "run_mode": "async", "max_parallel": 4}
```

| `run_mode` | The daemon | The app |
| --- | --- | --- |
| `sync` (default, and what every command did before this field) | starts it only when no run of it is in flight | may assume it is alone |
| `async` | **starts it whenever it is due, in flight or not**, up to `max_parallel` | **must refuse its own duplicate work** |

**Why the field exists.** `APP-SQL_TASKS` is one app command that works through its due list in
order, so one slow task is a stopped queue rather than a busy one: on 2026-09-19 a 22-minute SQL
task held every other SQL task for 22 minutes, and a leftover `running` row held the same queue for
30 minutes earlier that day. `APP-BACKUP-RESTORE` has the same shape. Both ship `async`.

**Why it is safe, and what makes it unsafe.** Running an app twice at once is only acceptable
because each *unit of work* takes an exclusive claim: a `running` row that a unique index will not
issue twice (`ux_sql_runs_claim` per task-and-target, `ux_job_runs_claim` per backup job, restore,
or `sync` app command). The second process is told the work is taken and moves to the next item. An
`async` app that does **not** claim its work runs the same thing twice — which is the 2026-09-08
duplicate production SQL runs and the 2026-09-14 second restore, both of which were guarded only by
a SELECT taken before the decision.

**A claim covers the work in flight, not the work done.** The unique index holds only while a
row is `running`. A run lists what is due, then works through the list; an overlapping run 30 s
later takes what the first has not reached yet. Once the second has **finished** such a job, the
first used to run it again when it got there: the claim had nothing left to refuse. Driving the
real `run_backup` showed it, `['LONG_A', 'SHORT_B (by the other run)', 'SHORT_B']` (2026-09-25),
and the SQL task scan had the same gap - on a production target, the same SQL twice.

Since then a scheduled run asks again just before each claim, with one indexed probe per key,
whether another run has started this job since the list was read:
`store.job_run_started_since` for backups and restores (`backup_restore.schedule.taken_since`),
`store.sql_run_started_since` for SQL tasks. It skips the job if so. An explicit `--force` still
runs what it was asked to.

**`max_parallel` is a brake, not a tuning knob.** `APP-SQL_TASKS` is due every second; uncapped,
`async` would start a process per second for as long as the first one runs.

It is still the number of tasks that can run at once, because each run works through its list one
task at a time. Measured on 2026-09-25 with nine SQL tasks of 10, 5 and 1 minutes, all due again
10 s after they start:

| `max_parallel` | At once | A 1-minute task waited between runs | Longest single run of the app |
| --- | --- | --- | --- |
| 4 (shipped) | 4 | up to 848 s | 966 s: it chained a 10- and a 5-minute task |
| 10 | 9, one per task | 0-5 s | one task |

Raise it to the number of long tasks that must not wait behind each other, and no higher: each run
is a process with a database session. Keep the app's `timeout` above the longest chain a run can
take, because the daemon's kill at the timeout leaves the statement running on the server
([05](05_sql_task_runner.md), *Stale running rows*). `app-command-set` changes it on a running node;
the daemon reads it at its next pass.

A `sync` command still claims its own id, so a second daemon on the host — or a child that outlived
its daemon — cannot start a duplicate either. An `async` command claims nothing, because being
called again while one is running is the whole point of it.

## Stale RUNNING Job Recovery

When the daemon process is killed or crashes while a child subprocess is active, the corresponding `job_runs` row can be left in `status = 'running'` indefinitely. The daemon handles this in two ways:

**Startup recovery** — `recover_stale_running_jobs` runs once at daemon startup. It queries `job_runs` for rows where `status = 'running'` and `started_at + timeout_seconds <= now`. Any matching row is updated to `status = 'timeout'` with a `finished_at` timestamp so the command is eligible to be scheduled again on the next scan.

**Per-scan detection** — `app_command_is_due` also handles stale RUNNING rows. If the latest row for a command has `status = 'running'` and `started_at + timeout_seconds <= now`, the command is treated as due (not blocked by a live run). The daemon will start a new subprocess and insert a fresh `job_runs` row; the stale row remains in the table as historical evidence.

Together these two paths ensure that a crashed or long-gone subprocess never permanently blocks a scheduled command.

**A row whose process is still alive is no longer reaped on age.** The timeout used to be the whole
answer, and that is a loop: a task that legitimately outran its timeout had its row closed, and the
next scan started a second copy on top of the first. Every `running` row now records the pid and
host that own it, and the reaper asks whether that process is still there
(`db_ops/lib/run_claim.py`):

| The row | Answer |
| --- | --- |
| this host, pid alive | **held**, however old it is |
| this host, pid gone | **freed at once** — a dead process will not come back |
| another host | **freed only after its timeout plus an hour**, because this host cannot check that one's processes |
| no pid recorded (written by an older build) | age alone, exactly as before |

Closing the row is what releases its claim, so "is it stale?" and "may another run start?" are the
same question and are now answered in one place.

**And the other direction — a run that is genuinely still going blocks the next one, across a
restart.** The `running` row is tested *before* the repeat interval, which matters because almost
every command repeats far more often than its worst case takes: `APP-BACKUP-RESTORE` repeats every
30s with a 7200s timeout, since most cycles find nothing to do. Testing the interval first made
the `running` branch unreachable for exactly the commands that need it, and within a single daemon
the in-memory duplicate check hid that. Across a restart nothing hid it: on 2026-09-14 a daemon
started 47 minutes into a restore began a second restore of the same database onto the same
target. The order is now status first, interval second, which is what the daemon's own
`app.daemon.command.not_due` message had always reported.

## Common Issues

- A command is not starting: check `active` and `time_window.repeat_interval` plus `from_*`/`to_*` bounds.
- A command is skipped as already running: the daemon keeps one live subprocess per `app_command_id`.
- A command starts again soon after finishing: `repeat_interval` is counted from the last `started_at`. A run that lasts most of its interval may be due shortly after it exits.
- A command is not starting and nothing says why: the `app.daemon.command.not_due` log line carries `reason=` and `next_retry_at=`, both taken from the verdict that made the decision. The same answer without the daemon is `python -m db_ops.common.cli due-check '<json>'`.
- A command exits with `error`: inspect `metadata_json`, `error_text`, and the matching `{log_scope}_runtime.log`.
- A command times out: increase `time_window.timeout` only after checking whether the child app is stuck.
- **The store goes away for a moment**: a failure the store classifies as transient — the PostgreSQL SQLSTATEs `08xxx` (connection), `57P01`–`57P03` (shutting down / starting up) and `53300` (too many connections), plus a socket that never reached the server — is waited out rather than raised, 2 s then 4, 8, 16 and 30, for at most **10 minutes per outage**. Every wait is logged as `app.daemon.store_outage` with the code and the budget left, so the gap in `job_runs` carries its own explanation. Past the budget, or for any other error, the daemon exits as it always did. Before this, a two-second restart of the store's container ended the process on `FATAL 57P03` and nothing restarted it — nineteen hours of silence, and candidate 0.18.0 abandoned at hour 21.8. The rule is `db_ops/lib/store_outage.py`; SQLite's `database is locked` keeps its own older backoff beside it.
- A command appears stuck in `running` after a daemon restart: `recover_stale_running_jobs` should resolve this at next startup. If the row is still `running` after restart, check that the daemon started without error.
- **The daemon starts and runs nothing at all**, logging `app.daemon.nothing_scheduled`: every command is declared for a `node_role` this process does not have. A process that was not told otherwise is `master`, and an estate exported from a worker declares `worker` on all of them. Set `DB_OPS_NODE_ROLE=worker` on the process, or `node_role: "all"` in `data/app_commands.json`. This is the first thing to check on a machine that has just imported an estate — see [`docs/configuration.md` §9](./configuration.md#9-moving-a-whole-estate-to-another-machine).

## Config Priority

The daemon resolves its config file using this chain:

1. `--config <path>` CLI argument.
2. `DB_OPS_JOBS_CONFIG` environment variable.
3. `config.jobs.json` next to `config.json`, or in the current working directory.
4. `config.json` shared fallback.

The selected source is printed to stderr on startup.

App-specific config file: `config.jobs.json`

## Standalone Mode vs Full-Suite Mode

**Full-suite mode** (default): the daemon reads `config.json` and `data/app_commands.json` alongside all other apps. Subprocesses inherit the same working directory and resolve their own configs independently.

**Standalone mode**: copy `config.jobs.json` and `data/app_commands.json` next to the daemon EXE. Set `log_dir` and `runtime_dir` to local absolute paths and point the store at a local file (`sqlite_path` is still the natural setting for a standalone EXE, which has no shared server). The daemon spawns child processes by `command_text`; each child must also be able to resolve its own config independently.

Required config keys: `log_dir`, `runtime_dir`, plus a resolvable runtime store (`store_config_file`, an inline `store` block, or `sqlite_path`).

## `working_dir` and the `tools/db_ops` alias

Every entry in `app_commands.json` carries `"working_dir": "tools/db_ops"`. That is **not** a
folder in this repository — `db_ops` is a standalone repository with `db_ops/`, `data/`, and
`assets/` at its root. The daemon treats the exact string `tools/db_ops` as a **logical alias
for the tool root** and resolves it to `TOOL_ROOT`, wherever the project physically lives
(`resolve_working_dir` in `db_ops/jobs/daemon.py`):

| Where it runs | `tools/db_ops` resolves to |
| --- | --- |
| Local checkout on the master | the repository root (e.g. `D:\Projects\db_ops`) |
| `db_ops_daemon` container on the worker | `/app/tools/db_ops` (the image's fixed layout) |

This is why the same `app_commands.json` is deployed to both sides unchanged. Any other
relative `working_dir` is tried against `REPO_ROOT`, `TOOL_ROOT`, then `data_dir`, and must
exist; absolute paths are used as-is.

## Optional Integrations

The daemon spawns all other apps as subprocesses. If a child app binary or config is missing, the scheduled command records an `error` in `job_runs` and the daemon continues running. No other sub-app crashes as a result.

## EXE Packaging Notes

- `--data-dir` controls where `app_commands.json` is loaded from. Pass it explicitly when running outside the repo.
- `working_dir` entries in `app_commands.json` are resolved relative to `REPO_ROOT`, `TOOL_ROOT`, or `data_dir`. Use absolute paths in `working_dir` when packaging as EXE.


## Stale run recovery, and what is worth waking someone for

At startup the daemon reconciles `job_runs` rows still marked `running`. It owns no child
processes at that moment, so every open row belongs to a life that has ended.

Three rules, each of which has been got wrong at least once:

1. **Reconcile every open row, not the newest per job code.** A stale run overtaken by a newer one
   is no longer the latest for its code but is still open, so it could never be closed again. They
   accumulated to 177 `job_runs` and 104 `metric_runs`, the oldest from 2026-05-18, which makes
   "is anything running now" unanswerable.
2. **A long-running service (`timeout == 0`, `timeout_disabled`) is closed too — quietly.** Its row
   being open at startup is expected, not a crash. It is closed as `timeout` specifically, because
   `job_due` only restarts a run-once entry from an error status; any other status reads as
   "finished, never repeat" and `APP-WEBHOST` would never come back up. Skipping it instead leaked
   one row per restart.
3. **Alert on recent crashes only, and once.** The push used to sit inside the loop, which was
   survivable only while recovery could see one row per job code. The moment it reconciled the real
   backlog it sent 171 near-identical error messages — including rows from two weeks earlier,
   each formatted as a fresh incident. Rows older than
   `STALE_RECOVERY_ALERT_MAX_AGE_SECONDS` (24h) are reconciled silently and counted; the rest
   produce **one** message listing up to 10 of them.

A failed run also records **why**: `error_text` carries the exit code plus the tail of the child's
stderr (falling back to stdout), bounded by `FAILED_RUN_ERROR_TEXT_CHARS`. It previously held only
`Command exited with return code 1`, so diagnosing the PostgreSQL NUL-byte failure meant reading
source comments and correlating deploy timestamps.
