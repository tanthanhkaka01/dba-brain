# Logging Engine

## Purpose

The Logging Engine provides consistent file logging for every `db_ops` app. It writes per-`log_scope` app logs, runtime stdout/stderr logs, a shared `errors.log`, and daily archives.

## Reading the logs from the console

The web console's Logging Engine page shows the current log for this node — newest line first, a
hundred at a time, older lines as you scroll — with a picker for which log. It reads the files
under `log_dir` directly; nothing ships them anywhere. The format this app writes
(`timestamp|LEVEL|app|host|function|message`) is what lets the console show level and function as
their own columns; a `*_runtime.log` line is raw stdout and is shown whole.

See [12_webhost_app.md](12_webhost_app.md).


## Package / Files

- `db_ops/logging_ops/`
- `logs/`
- `data/app_commands.json` field `log_scope`
- `config.json` fields `log_dir`, `console_level`, `file_level`

## Runtime Tables

The Logging Engine itself writes files, not tables. CLI modules and the App Command Daemon can also write operational rows to `job_runs`.

## Config Files

`config.json` controls log directory and levels. `data/app_commands.json` controls daemon-launched app log names through `log_scope`.

For `log_scope = "metrics"`, the app writes:

```text
logs/metrics.log
logs/metrics_runtime.log
```

The scopes currently configured in `data/app_commands.json` are `sql_tasks`, `metrics`, `reports_create`, `reports_inventory_workflow`, `sla_validate`, `backup`, `telegram` and `webhost`. The daemon coordinator writes `jobs.log` and `jobs_runtime.log`.

## Data Flow

Input events come from app logger calls, stdout, stderr, and exception handlers. `logging_ops` formats each line, writes the app log, writes runtime output when stdout is patched, and duplicates error-level events into the shared error log.

Log line shape:

```text
DATE|LOGTYPE|APP|HOST|FUNCTION|TEXT
```

## How to Run

Write a test log and insert a `job_runs` row:

```powershell
python -m db_ops.cli --config config.json --level warning --message "manual logging test" --job-code MANUAL-LOG-TEST --status warning
```

Show recent `job_runs` rows:

```powershell
python -m db_ops.cli --config config.json --recent 20
```

Run the daemon once to exercise per-command log scopes:

```powershell
python -m db_ops.jobs.daemon --config config.json --once
```

## Useful Manual Queries

```sql
SELECT log_id, created_at, job_code, level, status, message
FROM job_runs
ORDER BY created_at DESC, log_id DESC
LIMIT 50;
```

## Common Issues

- Missing or invalid `log_scope` in `data/app_commands.json`: the App Command Daemon fails fast before starting that command.
- Logs appear under an unexpected filename: check the command's `log_scope`, not the Python module name.
- Runtime output is missing: confirm the entrypoint calls `patch_stdout(...)` or is launched by the daemon.
- Old logs are growing: daily archive exists, but retention cleanup is not implemented in the logging engine.

