# Changelog

All notable changes to DBA Brain are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Entries are written as the work lands, not reconstructed at release time.** A release that has to
remember what went into it gets it wrong, and the notes stop being trustworthy exactly when someone
is deciding whether to upgrade. Every pull request that changes behaviour adds its line to
`Unreleased`.

Write entries for the person deciding whether to upgrade: what changed for them, and what they must
do about it. Not the internal refactor that made it possible.

## [Unreleased]

## [0.9.0] - 2026-09-05

### Added

- **`uptime` in `db-ops common self-status` / `/spbot_self_status`** — how long the machine has been
  up, in hours to two places, with the instant it came up in UTC:
  `uptime    : 165.01 h  (host up since 2026-08-29T10:30:17Z)`. The report said which build, which
  machine and how much room was left, and not how long any of it had been standing. Read from
  `/proc/uptime`, falling back to `GetTickCount64` on Windows; a platform that can answer neither
  says `unavailable` rather than `0`. The line says **host** up since on purpose: this is the
  machine's clock, not the daemon's — `self-status` is a different process from the scheduler, and
  `ops-status` is what answers whether the apps have been running.

## [0.8.1] - 2026-09-05

### Fixed

- **The web console served nothing on Windows.** Two symlinks decide whether reports are
  reachable — the `/report_dba/` mount and the fixed `database-inventory.html` link — and both need
  a privilege an ordinary Windows account does not hold. Both failed, were logged as warnings, and
  **every** report URL answered 404, the timestamped ones included. Neither symlink is required
  now: the mount prefix is resolved in the request handler and the latest link falls back to a
  copy. Linux and Docker still use symlinks and are unchanged. Measured on four deployment shapes.
- **The latest-link refresh deleted the file it publishes.** It unlinked the existing entry before
  attempting the symlink, so on a platform that refuses one, each request for the link removed what
  was there and left nothing. "Already current" is decided before anything is removed now.
- **`restore-workflow --dry-run` deleted backup files.** The flag reached the restore step and not
  the delete step, which had no parameter to receive it, so all three delete engines removed files
  during a dry run. Found by dry running a real restore drill: 26 files, all past retention.
  `run_copy_backup` still has no `dry_run`, so a dry run does still copy — stated, not fixed.
- **`/spbot_list_my_commands` and `/spbot_list_sql_runs` replied twice** — the listing, then a JSON
  envelope repeating it — which put the reply over Telegram's 4096 limit and split it in two. Both
  take `"format": "txt"` now, as every `common/cli` command already did.

## [0.8.0] - 2026-09-05

### Added

- **`/spbot_list_my_commands` — your own last 10 commands, each as one line you can run again.**
  Repeating a command meant scrolling the chat for arguments that came from a listing (`sql_id`,
  `server_id`, a date range), and for a command answered one prompt at a time there was nothing to
  scroll to: the message that started it says `/spbot_run_sql_task` and nothing else, while the
  `18` and the `0 30` are separate messages that look like conversation. The line is rebuilt from
  the answers in argument order, so `/spbot_run_sql_task 18 0 30` comes back whole. Distinct by
  that line with repeats counted, private chat only (the history spans every chat), and prompts
  that were never answered are skipped and counted rather than offered as half a command.
  Also available as `db-ops db telegram-command-history '{"user_id": "...", "limit": 10}'`.
- **`db-ops db backfill-from-sqlite --source <store> [--plan-only]`** — carry the history a
  stand-in node wrote to a local SQLite store back into the shared one. When the worker is
  stopped and the estate runs from a laptop or a fresh install, the work is real but the record
  of it lands where nobody queries it. Ids are not carried: every key is an identity column, so
  parents are written first and each child is rewritten through the old-to-new mapping before it
  lands — carrying `metric_results.run_id` verbatim would be *accepted* and point at somebody
  else's run. The window is the destination's own newest row per table, so a repeat run carries
  nothing and an interrupted one is finished by running it again.

## [0.7.2] - 2026-09-05

### Fixed

- **The identifier scan reported a tree as clean while a real machine name was in it.** Between
  knowing a term and reporting it there were four filters, and each dropped something real:
  a composite value (`server_name` is `HOST\INSTANCE`) was searched for only as the pair, so prose
  naming the machine on its own matched nothing; the `review` tier was excluded from the printed
  report as well as from the refusal; `unrecognised_addresses` was collected and never printed;
  and `SKIP_DIRS` was matched against the whole absolute path, so scanning a tree that merely lives
  under a directory called `build`, `dist` or `deploy` opened no file and reported no hits.
  All four are closed. A scan that opens **zero files now refuses**, the same rule the module
  already applied to having zero terms and for the reason it states: silence must not read as
  clean. Teaching it to split composite values added five hostnames it had never known, and its
  first run after the fix refused the tree and named a file a hand search had missed.
- Values scrubbed from the shipped surface: a machine name in a collector comment, in three tests
  and in one component doc; two database names used as sample rows; and an estate's routed subnets
  used as network fixtures — moved off those ranges while staying inside Docker's default pool,
  because that containment is what those tests assert. The scanner's own comment no longer names
  real databases to explain why its tiers exist.


## [0.7.1] - 2026-09-05

### Fixed

- `job_runs` never pruned. The archive sweep moves aged rows into `job_runs_history` and then
  deletes them, but `app_command_requests.job_run_id` is a foreign key with no `ON DELETE`, so a
  single finished "run now" request pointing into the batch failed the whole delete. The daemon
  swallows a failed sweep on purpose — housekeeping must not stop the scheduler — so the busiest
  table in the store stopped pruning and reported it only in one log line per sweep interval. The
  referencing requests now move into `app_command_requests_history` in the same transaction, before
  the runs they name. Nothing is lost: the record of who asked for a run outlives the run, which is
  what it is for.
- ...and it still never pruned, because the archive table did not exist yet. `RunRequestStore`
  creates `app_command_requests_history`, but only when the console runs, and the daemon's sweep
  does not run the console — so every store that had used the console on an earlier build had the
  requests table, the foreign key, and no archive, and the fix above read that as "nothing to
  move". Measured on both live stores. The sweep now creates the table when it finds one missing,
  once before the first batch rather than inside one. **Both backends behaved identically here** —
  SQLite refuses the delete with `FOREIGN KEY constraint failed` exactly as PostgreSQL refuses it
  with `23503`.
- A WinRM failure no longer reads as the host's fault when the cause is a missing dependency.
  Without the `[winrm]` extra there is no `pypsrp`, and `remote_exec` falls back to driving
  `Invoke-Command` through a local PowerShell — which cannot authenticate to some Windows hosts
  and hands back Windows' own wording (`0x8009030e ... A specified logon session does not exist`,
  *"add the server name to the TrustedHosts list"*). Nothing said the call had run on the weaker
  of two backends. It cost one estate two days of backups on an instance whose WinRM was working
  the whole time. The fallback now appends one line naming the extra to install, and only when it
  fails to authenticate. `docs/installation.md` now says to name every transport the estate uses,
  not just its databases.
- The daily-report guard raised on PostgreSQL. `report_exists_on_local_date` compared with
  SQLite's `datetime(created_at, '+7 hours')`, which PostgreSQL has no equivalent for and the
  dialect translator does not rewrite, so `db-ops reports create-backup-health-report` without
  `--force` failed with `42883 function datetime(text, unknown) does not exist` on a PostgreSQL
  store while working on SQLite. The local day is now computed in Python and bound as a plain UTC
  range — the same answer on both engines, and one an index on `created_at` can serve. A
  `local_date` that is not a date is now refused rather than answered `False`, because "no report
  today" is the answer that lets a duplicate out.

## [0.7.0] - 2026-09-04

### Added

- `db-ops common authorize`: the confirmation gate on its own, for a caller that performs the work
  itself. How hard an operation is to confirm still comes from `emergency_operations.json`, and an
  operation that file does not list costs two answers rather than none.
- `run-sql-task` is a named operation at level 50: one typed `yes`, with a prompt that names the
  task, how many targets it will touch and whether the task is inactive.

### Changed

- **A forced SQL task run is refused unless it is confirmed.** `run-sql-id --force` asks at a
  terminal, accepts `--confirm yes` when a human answered elsewhere (how the chat command passes
  the reply it collected), and **requires `--assume-yes` when nothing can be asked** — so a script
  that forces a task from a scheduler must add that flag or it will now stop. The scheduled scan is
  unaffected and `--dry-run` is never asked.
- The shipped `/spbot_run_sql_task` moves to clearance 50 and takes a `confirm` argument, typed
  between the id and the task's own values: `/spbot_run_sql_task <sql_id> yes [values]`. Sending the
  id alone still asks one question at a time.

### Fixed

- The clearance check for `/spbot_run_sql_task` asserted that every command able to execute SQL
  carried the same number, so tightening one of them turned the suite red on a correct change. It
  now asserts the floor and the ordering, which tuning cannot break.
- An abandoned SQL task run stayed `running` for good once a later run replaced it: the sweep read
  the latest row per task rather than every run that never ended, so a death inside the target's
  timeout window became invisible. It now judges every `running` row on its own age. The alert
  carries both the run's start and the time the message was written, UTC with the offset shown.
- Every confirmed Telegram command was refused on a fresh install (v0.4.0-v0.6.0): `init` never
  wrote `emergency_operations.json` and the package carried none, so each operation was priced at
  the strictest level - two answers - while the commands collect one. The ladder now ships, `init`
  writes it, and the gate falls back to the packaged copy when a tool root has none.
- `export-public` deleted the target's `.git` when its identifier scan refused the tree, taking the
  history and the remote of the repository it was exporting into. A refusal now empties the copy
  and keeps the repository; a target that is not a repository is still removed outright.

## [0.6.0] - 2026-09-04

### Added

- `db-ops db sql-run-history` and Telegram `/spbot_list_sql_runs`: the most recent SQL task runs and
  how each ended, newest first, with the reason for any that failed. `list-tasks` says what is
  configured; this says what actually ran. Reads `sql_runs` through the shared `common` layer.
- `db-ops common self-status` and Telegram `/spbot_self_status`: what this installation is and how
  much room it has left - distribution (published `dbabrain` vs a private `db_ops` build) and
  version, Docker vs OS and which OS, host/ip, node role, store, cpu, memory, disk. Reads itself, so
  it answers even when the store is down; memory names its source (cgroup vs the host's `/proc`).

### Fixed

- `import-data` refuses to unpack an estate into `site-packages` when no `--root` is given, instead
  of writing it there and printing `imported into …/site-packages`. Names the directory and both
  ways forward.
- `import-data` reports, at import time, when every command in the imported schedule is for a
  `node_role` this process does not have (a worker estate imported onto a default `master` process
  would otherwise start and run nothing).
- A legacy-TLS connection failure now names its real cause - the TLS policy of the machine running
  the toolkit, not the instance or credential - and the two settings that fix it, with a distinct
  message for the `MinProtocol = TLSv1.0` spelling trap that breaks every connection on OpenSSL 3.5.
- The container image sets `MinProtocol = TLSv1` (was `TLSv1.0`, which OpenSSL 3.5 rejects).

### Changed

- `db-ops init` writes 28 Telegram commands (was 26).
- `docs/installation.md` documents the OpenSSL TLS-1.0 prerequisite for monitoring old SQL Server
  instances from a pip install on Linux (the container has always done this at build time).

## [0.5.0] - 2026-09-03

### Added

- Three collectors that record cumulative counters and grade nothing:
  `PERFORMANCE_WORKLOAD_COUNTERS` (900 s, `sys.dm_os_performance_counters` +
  `sys.dm_resource_governor_resource_pools` + `sys.dm_io_virtual_file_stats`),
  `PERFORMANCE_WAIT_TOTALS` (900 s, `sys.dm_os_wait_stats`, a fixed watch list) and
  `PERFORMANCE_QUERY_STATS_TOTALS` (1800 s, `sys.dm_exec_query_stats`). The catalogue goes from
  90 metrics to 93.
- A workload section on both report pages, built from `db_ops/reports/workload.py`: latest
  interval, last hour and last 24 hours on `server-metrics.html`, a per-server chip strip on
  `database-inventory.html`. Each header states the span that was measured, not the one asked
  for. It answers "how much did this instance do in the last hour", which no page could answer
  before — every counting DMV reports a total since the engine started.
- `db_ops/lib/interval_rates.py` differences two stored samples. A pair whose `counters_since`
  markers disagree is refused: after a restart the totals begin at zero, and a delta across one
  is the new absolute value. No pair, no column.
- `019_sqlserver_io_latency` reports bytes read and written, not only operation counts.

### Fixed

- A SQL task run whose process was killed now alerts. `mark_stale_running_sql_runs` closed the
  run out as `error` and logged it, and nothing else — `alert_on_error` was only wired into the
  exception handler a killed process never reaches. The message says the SQL may still be
  executing on the server, which is what a reaped run leaves behind.
- `as_epoch` was defined twice, byte for byte, in `capacity_forecast` and `interval_rates`. It is
  `db_ops.lib.coerce.as_epoch` now.
- The stdin JSON contract tests handed the CLIs an `io.StringIO`, which has no `.buffer` — the
  attribute the pinned UTF-8 read needs. CI was red on 3.12 and 3.13; the stand-in is now a text
  stream over bytes.
- The encoding-mismatch test wrote a payload cp1252 cannot represent, which raised on Linux and
  deadlocked on Windows (the stdin writer thread dies without closing the child's stdin). It now
  encodes the payload itself, and asserts the failure the estate actually hit: an em dash sent as
  `0x97` and refused by a UTF-8 reader.

## [0.4.3] - 2026-08-27

### Added

- `db-ops export-data` / `db-ops import-data` — write a whole estate's configuration to one JSON
  file and apply it on another machine. What travels comes from `data/data_files.json`. The secret
  store crosses as ciphertext; the passphrase is not in the file, so the receiving machine supplies
  `DB_OPS_SECRET_KEY` itself. Every entry carries a sha256 and the bundle is verified before
  anything is written, so a truncated transfer leaves the target untouched. `--plan` changes
  nothing.
- `data/data_files.json` — the inventory of `data/`: every live file, its owning app, and how it
  moves between master and worker. `deploy`, `worker-pull-data-config` and the merges read it
  first, and a file that is not listed does not travel in either direction.
- `json` as a SQL task `output.format`.
- `worker-status` reports container network reservations: `HIJACK` when a monitored address sits
  inside a container bridge, `OVERLAP` when a routed range does, `UNCONFINED` when Docker chose a
  network rather than you. Declare yours in `data/network_reservations.json`; the example explains
  what to put there.
- Six commands now ship that previously did not: `/spbot_start_job`, `/spbot_disable_job`,
  `/spbot_restart_server`, `/spbot_shrink_log`, `/spbot_trace_session`, `/spbot_list_metrics`.
- `MAINTENANCE_STATISTICS_AGE` grades its finding in a summary row instead of emitting every stale
  object at `WARNING`, and names the bands (`over_90d`, `stale_and_modified`).
- `SQLSERVER_WAIT_STATS` measures the interval between passes instead of the total since the engine
  started.

### Fixed

- `/spbot_kill_spid` sent `{"session_id": N}` where `common.cli kill-spid` takes `{"spid": N}`, so
  the shipped command failed in every installation.
- A detached background command that finished was reported as `Exit code: 1` on Windows, because
  the poller could not read the exit code of a process whose PID had been released and returned a
  fixed value. The command records its own code now, and "not recorded" reads as finished.
- `db_ops.control.deploy` could not be imported on a fresh install: it read `data/data_files.json`
  at import time.
- The `config_catalog.json` written by `db-ops init` was two entries behind, so a fresh install's
  console never showed network reservations.
- `db-ops init` wrote `data/ops_status_request.json`, which no manifest listed, so nothing carried
  it.
- The daemon ignored `--key-base64` when `DB_OPS_SECRET_KEY` was already in its environment.
- `APP-CONTROL` failed every cycle on Windows: its command wrapped inline JSON in POSIX single
  quotes, which `cmd.exe` passes through literally.

### Changed

- **`output` and `notify` are now required on every entry in `sql_targets.json`.** An absent
  `output` used to mean `plain`, and a target without one is refused now. Add
  `{"format": "plain", "telegram_chat": "sql", "chat_id": ""}` to reproduce the old behaviour.
  Tasks registered through `db-ops common add-sql` already have both.
- `/spbot_trace_session` takes a server and a database as its first two arguments; it had them
  fixed in its own configuration.

### Removed

- `/spbot_json_exp_ticket_detail`, which pinned one installation's `--sql-id 14` in its own
  arguments. Use `/spbot_run_sql_task <id>` with that task's `output.format` set to `json`.


## [0.4.2] - 2026-08-25

### Added

- `db-ops common check-secret-literals` — scans files for literal values held in the secret store.
  The store is decrypted and its values are matched exactly; a finding reports the ref, the file and
  the line, and never the value. A passphrase is required; without one the command exits with an
  error rather than reporting a result.

### Fixed

- `spbot_create_db_docker` and `spbot_report_inventory` contained a hard-coded worker address. Both
  now resolve `{worker_host}` from `config.json`, and render it empty when no worker is configured.
  An existing `data/telegram_support_commands.json` is not modified.
- `check-identifiers` did not match names of the form `<name>_<octet>_<octet>`, because `_` was
  treated as a word boundary. The boundary now excludes an adjacent digit, and a separator followed
  by a digit.
- `data/telegram_groups.example.json` now ships placeholder group ids and titles.

## [0.4.1] - 2026-08-24

### Fixed

- `check-identifiers` did not read `.html` or `.j2` files, which were absent from its extension
  list, so report templates were not scanned. Both types are now included.
- `check-identifiers` matched full addresses only. It now also derives and matches the two-octet
  short form, bounded so that it does not match inside the address it was derived from.
- A Telegram command tied to a single instance was removed from the catalogue written by
  `db-ops init` and from its example copy.

## [0.4.0] - 2026-08-24

### Added

- `db-ops common copy-schema` — reproduce one SQL Server schema on another instance.

### Fixed

- Six of the fourteen packages could not complete a scheduled cycle on a clean install. All
  fourteen now run from a fresh tool root.
- `db-ops init` did not write `webhost_config.json` or `data/config_catalog.json`, so the web
  console rendered no applications. Both are now written, and an empty dashboard names the command
  that populates it. (Listed under `[0.3.4]` below, which was never published; the fix was released
  here.)
- `db-ops daemon --once` did not return, because the default schedule enabled the web console.
- `sla validate` and `sql-tasks` failed to start when their configuration files were absent.
- Telegram polling failed once per second when unconfigured, because `db-ops init` wrote a
  placeholder token ref instead of leaving it unset.
- `ops-status` failed on Windows, where its scheduled command quoted the request in single quotes.
- `control inventory-summary` raised `FileNotFoundError` for a file it should report as absent.
- Shipped examples now use documentation-range addresses and placeholder names.

### Changed

- The default schedule enables six commands and disables three. Backup/restore and the inventory
  commands require configuration that does not exist on a fresh install.

## [0.3.4] - 2026-08-24

### Fixed

- **The web console showed no apps at all on a fresh install.** Five zeroed tiles and "nothing is
  failing" — which is what a healthy console with nothing wrong looks like, so it did not read as a
  fault. Nothing was wrong with the data: `db-ops init` wrote nine app commands, all active.

  The console reads its layout from `webhost_config.json` **through the config store**, not from
  `data/` directly, and `init` did not write that file — so there were no blocks to hang the
  commands off, and the dashboard came out empty. The repair, `db-ops db sync-config`, then refused
  outright because `data/config_catalog.json` was missing too. The console could neither be
  populated nor say why.

  `init` writes both now (14 files, up from 12), and an empty dashboard says which command fills it
  instead of rendering zeros — for the tool roots created before this release, which stay empty.

## [0.3.3] - 2026-08-23

Three defects that only appeared when the **scheduler** ran the commands. Every one of them worked
when run by hand, which is why none had been caught: measured by installing the wheel into an empty
virtualenv, pointing it at a database, and starting `db-ops daemon`.

### Fixed

- **The daemon ran the wrong Python.** Every scheduled command begins `python -m db_ops...`, which
  resolves through `PATH` - and `PATH` is not where the toolkit is installed. After `pip install`
  into a virtualenv, the daemon started from the venv while its children got a system Python
  without the package, so all of them failed with `ModuleNotFoundError: No module named 'db_ops'`,
  once a minute, in a child process whose output nobody watches. A bare `python` now becomes the
  interpreter the daemon is running under; a command naming a specific interpreter is left alone.
- **`db-ops init` wrote one of the four files the daemon needs.** `reports_config.json`,
  `telegram_support_commands.json` and `app_commands.json` were missing, so scheduled reports and
  the Telegram workflow failed every cycle on a missing file. All four now ship as package data
  beside the component that owns them, and `init` writes them.
- **The shipped `app_commands.example.json` could not run on one machine.** Every entry was
  `node_role: "worker"` and a default daemon is `master`, so a single-machine install ticked
  forever with `active_commands=0` and no explanation. The example says `"all"` now, and when a
  daemon has nothing to run it says which of the three reasons it is, and names the fix.

## [0.3.2] - 2026-08-23

### Added

- **The distribution is now the whole toolkit: all fourteen packages.** `control` was the last one
  withheld — it builds and deploys the toolkit to another node, bumps the version, and runs the
  export that produces this repository. `db-ops` gains `bump-version`, `build-image`, `deploy`,
  `worker-status` and the inventory commands.
- **`db_ops/sre/data_folder/install_sql_server.example.json`** — a documented input for the
  SQL Server Always On lab script, using documentation-range addresses and a
  `sudo_password_ref` into the secret store. It is the script's default, replacing a captured
  record of one real install that could not ship.

### Changed

- **`export-public` reports a skipped identifier scan instead of raising.** With no inventory to
  derive search terms from, the scanner refuses rather than calling a tree clean on the strength
  of having looked for nothing — correct, and in a fresh checkout it is the normal case. It now
  says the copy was written and nothing was verified, rather than printing a traceback after a
  successful export.

## [0.3.1] - 2026-08-23

### Added

- **A release now publishes a container image** to `ghcr.io/tanthanhkaka01/dbabrain`, tagged
  `X.Y.Z`, `X.Y` and `latest` — `latest` follows a real release and never a prerelease. Until now a
  `v*` tag published to PyPI and produced no image, while the documentation described
  `docker run ghcr.io/.../dbabrain:<v>` as a way to use the toolkit. The job checks the image it
  just pushed: it runs, and it does not run as root.
- **`docker run <image> --help`** prints what the image can do. It used to fall through to
  `exec --help` and die on "command not found" — the first documented command failing.
- **A password in `sre_config.json` can live in the secret store.** `<name>_password_ref` (a store
  key) and `<name>_password_env` (an environment variable) are read alongside the literal, in that
  precedence. The literal still works: these values configure a lab machine that is created and
  destroyed. The case it did not cover is the lab that gets kept.

### Fixed

- **The image carried a stale second copy of the package.** `pip install .` runs the build backend
  in-tree, leaving `build/lib/db_ops` on the import path inside the image. Removed in the same
  layer.

## [0.3.0] - 2026-08-23

### Added

- **`db-ops init` now writes the whole metric catalogue — 90 metrics, up from three.** The
  collectors were always in the wheel (150 SQL queries, 29 shell and PowerShell scripts); nothing
  named them, so an installed package carried every Oracle, MySQL, PostgreSQL, Docker and OS
  collector on disk and could reach none of them. The full catalogue ships as package data and is
  what a first run gets.
- **The 14 OS metrics are reachable, and documented.** CPU, memory, disk, uptime, load, service
  state, listening ports, time sync, top processes — none of which need a database credential or a
  database at all. They need `cmd_access` on the target; `first_run.md` §2.8 has the SSH and WinRM
  shapes, verified end to end from a bare `init` against a live host.

### Changed

- **The container image runs as a normal user (uid 10001), not root.** A monitoring daemon needs no
  privilege in its container. Bind-mounted directories have to be writable by that user — `chown -R
  10001:10001 data logs runtime` — or run with `--user 0:0` to keep the previous behaviour. An
  unwritable mount is now reported by name before anything starts.
- **The image installs the package instead of only copying it**, so `db-ops` is on the PATH and
  every command in the documentation works from any directory in the container. Previously they
  worked only from the directory the daemon starts in, which is not where a reader would be.

### Fixed

- **Two errors on the OS-metric path now name the setting and the fix.** SSH `auth_type` defaults
  to `key`, so a `cmd_access` block with a password and no `auth_type` sent no credential at all
  and failed with paramiko's `No authentication methods available` — which reads as a server
  refusing you. It now says a credential was not sent, and which field to set. Likewise a target
  missing `platform` failed with `Target platform is required for cmd metric` without saying that
  the field goes on the instance rather than inside `cmd_access`.

## [0.2.1] - 2026-08-23

Three things that only appeared once `v0.2.0`'s packages were built and tested on Linux, which is
where this toolkit actually runs.

### Fixed

- **Restore paths on a Linux orchestrator were built with the wrong separator.** A path naming a
  location on a Windows VM — the import share, the data file to restore to, the `robocopy /LOG:`
  target — was joined with the *local* separator, so on a Linux worker it came out as
  `E:\SQLBK_IMPORT/APPDB/FULL/latest.bak`. Windows tolerates that; a tool given it as an argument
  need not. Those joins are now `PureWindowsPath`, which is the type that means "a Windows path,
  wherever this is running".
- The web console listed every app the configuration described, whether or not it was installed —
  so a distribution carrying a subset offered pages that could not open.
- The secret scan reported a key-derivation cost parameter as a leaked key. `.gitleaks.toml` now
  carries that allowance, scoped to the constant's own name and with its reason written out, so a
  real secret in the same file is still found.

## [0.2.0] - 2026-08-23

**The whole toolkit ships.** `v0.1.0` claimed one path — one SQL Server, metrics, a Telegram alert
— and carried the seven packages that path needed. This release carries twelve more capabilities
that were already written and already tested, and were held back only because a first release is
easier to stand behind when it claims less.

### Added

- **Scheduled reporting** (`db-ops reports`) — turns collected metrics into periodic reports and
  queues them for delivery, deduplicating and splitting messages over Telegram's length limit.
- **Backup and restore validation** (`db-ops backup-restore`) — runs backups from a declared
  policy and *proves* them by restoring, including point-in-time where the engine supports it.
- **SLA/SLO evaluation** (`db-ops sla`) — checks objectives against real measurement history rather
  than against a promise.
- **Scheduled SQL tasks** (`db-ops sql-tasks`) — your own SQL, per target, on a window.
- **The SRE toolkit** (`db-ops sre`) — host provisioning, Ansible and VMware automation, and
  database-in-Docker for building the lab you test restores against.
- **The web console and report host** (`db-ops webhost`) — serves the generated reports over HTTP
  and a console that edits configuration and runs an app on demand.
- Documentation for all of it: the seven per-component pages that were held back ship with their
  components.

### Changed

- The distribution is no longer thin. One package is still withheld and will stay withheld:
  `control`, which builds and deploys the master/worker pair and contains the export that produces
  this repository. The thing that makes the public tree does not belong in it.

### Fixed

- The report and automation templates now travel with the wheel. Twenty-seven `.j2` and `.html`
  files were being refused as unrecognised binaries, and without them `reports` renders a blank
  page and the Docker/Ansible automation writes empty files.
- The shipped example configuration no longer contradicts itself: the backup and SQL-task examples
  route to Telegram levels the example `telegram_config.json` did not define, so the example set
  could not be loaded as a set.
- The shipped metric catalogue describes the collectors the package actually carries — ninety
  metrics, not ten.

## [0.1.1] - 2026-08-22

Three defects in the **first two commands anybody runs**, all found by installing `0.1.0` from PyPI
into an empty directory and following the `AGENTS.md` that `db-ops init` writes there.

### Fixed

- **`AGENTS.md` documented the wrong shape for `secrets/secret_text.json`.** It showed
  `{"secrets": {"REF": "..."}}`; the file is flat — `{"REF": "the secret"}` — which is what the
  scaffolded file itself says. Following the guide produced a store with nothing usable in it.
- **`encrypt-secret` accepted that wrong shape and reported success.** It stringified the nested
  object into a single secret named `secrets` and printed `Encrypted 1 secret(s)`; the failure then
  surfaced two commands later as `Password ref not found`, naming a reference the plaintext file
  appears to define. It now refuses the file and shows the shape it needs.
- **`check-credentials` reported a pass for having checked nothing.** Given `--key-base64` — which
  every other command accepts — it read the flag as a folder name, walked a directory that does not
  exist, and printed `checked 0 target(s); 0 without a resolvable credential`. It now refuses a flag
  where a folder belongs, and refuses a folder that is not there. It needs no passphrase: it reads
  the store through the same resolution as everything else.

## [0.1.0] - 2026-08-22

**The first release.** One database engine claimed — SQL Server — and one path proven end to end:
install, `db-ops init`, describe one instance in JSON, collect metrics, send the finding to
Telegram. Oracle, PostgreSQL and MySQL collectors ship and are documented, but SQL Server is what
`v0.1.0` says it does.

The apps this release does **not** carry — scheduled reporting, backup/restore drills, SLA
checking, SQL task running, the SRE lab builder and the web console — are the `v0.2.0` scope. Where
the documentation names one, it says so.

> **Verified, not asserted.** The suite is green — 2,895 passed, 0 failed — on Python 3.11, 3.12
> and 3.13, with nothing skipped. The monitoring path was run end to end against a real SQL Server
> and against a throwaway container: install, `db-ops init`, one instance, metrics collected, the
> finding delivered to Telegram. The least-privilege grants in `docs/security.md` were measured
> against live instances of all four engines rather than read off the collector SQL.

### Added

- **`db-ops init`** — the first command anybody runs. Turns a directory into a working tool root:
  a SQLite store, an empty inventory, a starter metric catalogue, and an `AGENTS.md` next to the
  JSON explaining what to put in each file. Without it the toolkit could be installed and not
  started.
- **`db-ops encrypt-secret`** — turn `secrets/secret_text.json` into the store the toolkit reads.
  It was previously only in the deploy tooling, which this distribution does not ship.
- The store defaults to **SQLite** on a first run, so nothing has to be installed to hold the
  results of monitoring. Moving to PostgreSQL is an edit in `data/store_config.json`.

- Apache-2.0 `LICENSE` and `NOTICE`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, this
  changelog, and `.env.example` — the project-governance set required before the first public
  release.
- `pyproject.toml`: the toolkit is installable. Database drivers moved behind extras
  (`[mssql]`, `[oracle]`, `[postgres]`, `[mysql]`, `[ssh]`, `[winrm]`, `[all]`), so an install
  no longer drags in an ODBC driver and an Oracle client for someone who runs only PostgreSQL.
- `DB_OPS_HOME` and `DB_OPS_DATA_DIR` for telling an installed copy where its configuration is.

### Fixed

- **The package now requires Python 3.12, and 3.11 is no longer claimed.** It never worked: the CSV
  writer uses `csv.QUOTE_STRINGS`, which arrived in 3.12, so every CSV export raised
  `AttributeError` on 3.11. The floor was a policy choice; the test matrix ran on it and disagreed.
- The test suite passes on a clean install. Ten tests failed because they read configuration files
  this distribution does not ship. Eight of them were asking *is my configuration still correct* —
  a question only the maintainer's estate can answer — and have moved to a suite that stays there;
  the other two were fixed, and still ship. CI runs the whole suite on 3.11, 3.12 and 3.13 with
  nothing skipped and nothing allowed to fail.
- The documented first run could not be completed. Two commands the guides told you to run are not
  in this distribution: `db_ops.control.cli encrypt-secret-text`, which moved to
  `db-ops encrypt-secret`, and `db_ops.reports.cli queue-metrics-reports`, whose app is not shipped
  yet — so the alert step failed whichever spelling you used. The guides now name what exists, and
  say plainly that the scheduled reporting path arrives in `v0.2.0`.
- **Alerts have a supported path again**: `db-ops metrics alert-summary` builds the text from the
  results already collected — it reads the store, not the instance, so it needs no passphrase and
  costs the monitored server nothing — and `db-ops telegram send-message` sends it.
- `db-ops init` printed a next step that fails. It suggested
  `db-ops metrics collect --key-base64 …`, but the key is parsed by the app rather than by its
  subcommand, so it errored with `unrecognized arguments` — a wrong position reported as a wrong
  flag. It now prints the form that works.
- An installed copy could not find its configuration. The tool derived its project root from the
  package's own file path, which is correct only when the package sits beside `data/` — true in a
  checkout and in the container, false for every `pip install`, where it resolved to
  `site-packages/data`. The root is now resolved as: `DB_OPS_HOME`, then the working directory if
  it holds `data/` or `config.json`, then the package location as a fallback. Checkout and
  container behaviour is unchanged.
- Documentation examples throughout the code now use the addresses RFC 5737 reserves for
  documentation, instead of addresses from a private range that a reader cannot tell apart from
  their own network.
- Report links no longer default to one particular server. `report_base_url` had a built-in
  fallback pointing at a real internal host, so an install that never configured it produced links
  that resolved and were wrong. Unset now means unset: HTML pages use relative hrefs and chat
  messages omit the link.
- The Oracle bridge reads its shared secret from the environment variable named by
  `sql_access.secret_ref`, the same convention the rest of the toolkit uses. It previously read one
  fixed variable name, which meant a second bridge on a second host could not be given its own
  secret.
- Inventory pages no longer hide servers nobody asked them to hide. A subnet prefix was a constant
  in the rendering library, so every inventory page silently dropped those servers with nothing on
  the page saying so. The default now hides nothing; set `inventory_exclude_ip_prefixes` in
  `reports_config.json` to exclude a range deliberately.
- Ten further modules resolved their own default config, log and runtime paths the same way, so
  each of them pointed into the install directory too: the restore config, the SLA policies, the
  metric definitions, the Telegram support commands, the runtime log, the schema export directory
  and the working directory of two subprocess calls. All of them now share the one resolution.

<!--
Section order, and what belongs in each:

### Added        new capability a user can invoke
### Changed      behaviour that differs from the previous release
### Deprecated   still works, will be removed; say in which version
### Removed      gone; say what replaces it
### Fixed        a bug, described by its symptom
### Security     anything with a security consequence, with the advisory link

At release: rename [Unreleased] to [X.Y.Z] - YYYY-MM-DD, add a fresh empty [Unreleased],
and make sure the version here, the version in pyproject.toml, and the git tag agree —
the release workflow fails if they do not.
-->
