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

## Which day an archived log is named after (0.23.0)

A live log is archived as `<name>_<YYYYMMDD>.log` under the day it was **last written**, on the
display clock - not under "yesterday". A file last written today is today's log and is left alone.
Named after yesterday whatever it held, the first process of a new root filed that root's first
lines under the wrong date, and a node back from three days down filed its last day as yesterday.

## Rotation on a Windows master, and one line per record (0.26.0)

**Windows cannot rename a file another process holds open**, and the daemon held `errors.log` and
`jobs.log` open for its whole life: the nightly rename failed - silently, because a failure there is
normally a race with another writer - and the files grew without bound on a Windows master (review
0.25.0, F2.2). On Windows `DailyArchiveFileHandler` now opens, appends and closes for each record,
as `TeeStdout` does, so no handle outlives one write, and runs the archive check before each record:
a rename that lost a race is retried on the next line, not the next day. POSIX renames an open file
and keeps its stream.

**A record is one line in the file.** A message carrying a newline - remote stderr, a driver error,
a traceback the formatter appends - became extra lines with no `DATE|LOGTYPE|APP|HOST` prefix, which
`lib/log_tail` and every parser filed under the record before (F2.3). The file handlers write each
line break as the two characters `\n` (`handlers.one_line`); the console keeps real line breaks.

