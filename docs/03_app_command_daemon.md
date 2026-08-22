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

`time_window.repeat_interval` and `time_window.timeout` are in seconds by default. `repeat_interval` is measured from the previous run's `started_at`, not from `finished_at`. For example, if an app has `repeat_interval: 10` and the previous run started at `00:00:00` then finished at `00:00:09`, the app becomes due again at `00:00:10`. If that same `app_command_id` is still running when it becomes due, the daemon skips it until the running process exits or times out. Date/time bounds use `from_*` and `to_*` names, for example `from_day`, `to_day`, `from_hour`, and `to_hour`.

**Special `0` values (shared convention).** `repeat_interval`, `retry_interval`, and `timeout` accept `0`, which is interpreted consistently everywhere a `time_window` drives scheduling — app commands (`job_due`), SQL tasks, metrics, and reports (all via `db_ops/lib/time_window.py`):

- `repeat_interval: 0` — **run once**: due only when it has never run yet; it is *not* repeated after a successful run (a failed run is still retried after `retry_interval`, and a stale `running` row is recovered). This is the careful fix for the old "repeat every 0 seconds" trap.
- `timeout: 0` — **no timeout-kill**: the daemon never terminates the process for running too long. Use this for long-running services.
- `retry_interval: 0` — **retry/restart immediately** once the previous run has exited.

A long-running service (e.g. `APP-WEBHOST`, the report web host) combines all three: `repeat_interval: 0` (started once), `timeout: 0` (never killed while serving), `retry_interval: 0` (restarted at once if it dies). Leave the `from_*`/`to_*` date-time bounds `null` for such services — do **not** set them to `0` (e.g. `to_hour: 0` would only open the window at midnight).

The current configured commands are `APP-SQL_TASKS`, `APP-METRICS`, `APP-REPORTS-CREATE`, `APP-SLA-VALIDATE`, `APP-BACKUP-RESTORE`, `APP-TELEGRAM`, `APP-REPORTS-INVENTORY-WORKFLOW`, and `APP-WEBHOST`.

## Data Flow

`data/app_commands.json` -> active command filter -> local `time_window` check -> duplicate-running check by `app_command_id` -> latest `job_runs.started_at` interval check -> subprocess start with `DB_OPS_LOG_SCOPE` environment -> runtime log files -> final `job_runs` update.

The `time_window` check uses the node's **local time (+07 on both master and worker)**, while `job_runs` timestamps are stored in **UTC (+00)** — see "Timezone convention" in [`docs/13_common.md`](./13_common.md).

## How to Run

Run forever with a 2-second scan interval:

```powershell
# python -m db_ops.jobs.cli [daemon|status] is an equivalent alias (uniform <app>.cli convention)
python -m db_ops.jobs.daemon --config config.json --delay-seconds 2
```

Run one scan and wait for started commands to finish:

```powershell
python -m db_ops.jobs.daemon --config config.json --once
```

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

## Stale RUNNING Job Recovery

When the daemon process is killed or crashes while a child subprocess is active, the corresponding `job_runs` row can be left in `status = 'running'` indefinitely. The daemon handles this in two ways:

**Startup recovery** — `recover_stale_running_jobs` runs once at daemon startup. It queries `job_runs` for rows where `status = 'running'` and `started_at + timeout_seconds <= now`. Any matching row is updated to `status = 'timeout'` with a `finished_at` timestamp so the command is eligible to be scheduled again on the next scan.

**Per-scan detection** — `app_command_is_due` also handles stale RUNNING rows. If the latest row for a command has `status = 'running'` and `started_at + timeout_seconds <= now`, the command is treated as due (not blocked by a live run). The daemon will start a new subprocess and insert a fresh `job_runs` row; the stale row remains in the table as historical evidence.

Together these two paths ensure that a crashed or long-gone subprocess never permanently blocks a scheduled command.

## Common Issues

- A command is not starting: check `active` and `time_window.repeat_interval` plus `from_*`/`to_*` bounds.
- A command is skipped as already running: the daemon keeps one live subprocess per `app_command_id`.
- A command starts again soon after finishing: `repeat_interval` is counted from the last `started_at`. A run that lasts most of its interval may be due shortly after it exits.
- A command exits with `error`: inspect `metadata_json`, `error_text`, and the matching `{log_scope}_runtime.log`.
- A command times out: increase `time_window.timeout` only after checking whether the child app is stuck.
- A command appears stuck in `running` after a daemon restart: `recover_stale_running_jobs` should resolve this at next startup. If the row is still `running` after restart, check that the daemon started without error.

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
