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

## [0.26.0] - 2026-10-01

### Added

- **`check-objects` names every active record that leaves a which-thing fact to a default** - a
  `fallback` notice per field, with what is assumed until then: an instance's `platform`, an SSH
  `auth_type`, a `port`, a PostgreSQL database or an Oracle service, a script restore's
  `env.MSSQL_USER`, a container restore's `sqlcmd_path`, a certificate API's token ref. Phase 1 of
  the owner's no-fallback rule: reported now, refused in the next release (review notes G).
- **A step down in how something is done is said in the answer, and the copy's can be pinned**
  (owner decision G4). `copy-backup-dir` answers `copy_mode` (`tar` / `sftp` / `none`) and
  `copy_fell_back`, and a restore's `COPY_DONE` says *file by file over SFTP* when the tar stream
  could not be used - it was a line on stderr, for a copy that takes hours instead of minutes.
  `"copy_mode": "tar"` on a script-driven restore entry (or in the request) forbids the step down;
  `"sftp"` skips tar. `relay-file` and `send-file` answer `replace_mode` (`posix-rename` /
  `remove+rename`), and `run-cmd` over WinRM answers `backend` (`pypsrp` / `powershell`). A SQL
  Server connection already said (`run-sql` `tool.actual`) and could be pinned (`sqlserver_driver`).
- **Transport switches that verify the machine** (review 0.25.0, B5.5; the defaults are unchanged):
  `"sqlserver_tls_verify": true` on an instance makes every metric and `run-sql` connect with
  `Encrypt=yes;TrustServerCertificate=no`, ODBC Driver 17/18 only, and no fallback to plaintext or
  pymssql; the PostgreSQL store's `sslmode` now means what libpq means (`require`, `verify-ca`,
  `verify-full`). `docs/security.md` §5a has the table, WinRM's `ssl`/`cert_validation` included.
- **`sre ssh --stdin`** reads the remote command from stdin and sends it as a script, so a command
  carrying a password is in no argument list here or on the bastion; the lab AG tool uses it (B8.1).
- **`max_file_bytes`** on a bot command parameter that takes a file caps the attachment (review 0.25.0,
  F8.3); absent, Telegram's own limit applies as before.
- **`python -m db_ops.sql_tasks.cli close-run --sql-run-id N --reason "..." --confirm yes`** closes
  one SQL task run left `running` by a process that no longer exists, and releases its target. It
  closes a row only while it is still `running`, and is never prompted for.

### Changed

- **A restore's space check asks for twice the bytes it copies, on every engine, and a staged file
  is not counted twice** (§1.76). `space_check.factor` defaults to **2.0** (it was 1.5) for SQL
  Server, PostgreSQL and Oracle alike; an entry that states its own factor is unchanged. The check
  counts only the files still to stage, so a drill's second run is no longer refused by its own
  chain. **Action:** *Upgrading*, step 8.
- **Measuring the database a restore builds is opt-in.** A compressed backup says little about what
  it restores to - a 56.8 GiB chain passed at x2, restored to 366.6 GB and left its target at 96% -
  but the engineer who sets up a restore knows the disk has to hold the database, so by default only
  the copy is measured. `"space_check": {"measure_restore": true}` on a share-driven SQL Server entry
  asks the target, before each database's first `RESTORE`, what the backup's files are (`RESTORE
  FILELISTONLY`), what the database being overwritten already occupies, and what is free on the data
  volume: `free >= added x factor`, or that database is refused before any statement; a measurement
  that cannot be made is held to `on_unknown`. A script-driven entry (PostgreSQL, Oracle) that sets
  it is refused when it is read - they cannot be measured yet.
- **`check-secret` no longer sends a secret to a host read from the ref's name.** A ref no
  configuration names answers `NO_TARGET` with what to add; `allow_name_host: true` is refused.
  `rotate-password` stopped in the same release (G3.4). **Action:** *Upgrading*, step 9.
- **The tool root has an identity of its own** - `runtime/node_identity`, written once. The daemon
  hands it to every process it starts, and each run records it beside its host and pid.
- **Three unreachable metrics modules are gone**: `db_ops.metrics.health`, `.message` and `.notify`
  - an old notification path no command reached, which read `data/db_instances.example.json` and
  labelled a local time `TimeUTC:`. The `db_ops.metrics` package no longer re-exports their names;
  `status_os` / `status_db` / `status_connect` in the inventory still load and are read by nothing
  (review 0.25.0, F3.3).

### Changed - action required before upgrading

- **No password is read from the environment for anything a request names** (owner decision G3.5).
  `password_env` - on an SSH or WinRM login, in `sre_config.json`'s `<name>_password_env`, in a
  credential object, `sre --password-ref/--password-env` - is now the old spelling of
  `password_ref`: a key in the secret store. A ref is no longer looked up in the environment under
  its own name either, and `run-sql` refuses a `connection.password_ref` it is not given a
  `password` for. A password kept only in an environment variable must go into the store
  (`db-ops encrypt-secret` / `common.cli secret-set`) before upgrading. Database logins the apps
  read from `users.json` still take an environment variable of the ref's name first; none may name
  one of the node's own keys.
- **`rotate-password` changes a password only on the instance the inventory names for it**
  (G3.4): `allow_name_host` is refused, and a ref no `db_instance` uses is skipped with what to add.
- **`rotate-password`, `check-secret`, `check-secret-literals` and `check-identifiers` read the data
  dir of the `--config` they are given, and refuse one that cannot be read** (G3.1). They read the
  process's default data dir whatever config was named, and fell back to it when the config could
  not be read. A named confirmation-ladder path that is missing is refused too (G3.6).
- **A full SQL Server restore over a database that is ONLINE on its target is refused unless the
  entry says `"overwrite_existing": true`** (owner decision G2.10). `REPLACE` overwrote it after
  throwing its users out, and nothing asked. Every drill that runs again over its own last restore -
  which is every scheduled drill - needs the field; without it the next run stops at its first full
  restore and names the field. A database RESTORING or absent is restored as before. Script-driven
  SQL Server entries should also state `env.MSSQL_USER`: still `sa` when absent in this release, with
  a warning in the answer, refused in the next (G2.11).
- **A restore names its target; nothing about the target is taken from the source.** Every
  script-driven entry in `restore_config.json` needs `db_type` and `target_server_id` - also for a
  restore onto the source's own machine, where it is the source's id, stated. Such an in-place entry
  also needs `target_container`, `target_visible_dir` (the backup directory as the target container
  sees it) and, for SQL Server, `env.MSSQL_PORT`. An entry with only `target_container` used to mean
  "the source host", and a SQL Server restore then connected to the **source's** port and ran
  `RESTORE ... REPLACE` on production. `restore_by_id` - the scheduler's path - now refuses a target
  that is the source instance (same machine and same or no container, or the source's SQL port)
  before any step runs. The SQL port is never guessed (no 1433 default), and the staged directory is
  never the source's `backup_dir`. The loader names the entry and the missing field: run
  `backup-restore list-restores` after upgrading and fix what it names, one entry at a time. An
  **inactive** entry that lacks them stops nothing: it is listed under *Inactive and incomplete*
  with its reason, and the rest of the file loads.
- **A cross-machine restore stages only into a directory marked as db_ops's own.** The copy mirrors
  the source (a staged file the source no longer has is deleted) and the prune deletes by age, so a
  `target_backup_dir` pointed at anything else was emptied. A new, empty directory is marked
  (`.dbops-staging`) on first use, and an existing staging copy of the same source (it shares a file
  with the source) is adopted and marked by itself - nothing to do on upgrade. A directory that holds
  files the source does not have, and no marker, is refused with the `touch` command that marks it.
  `target_backup_dir` must be at least three levels deep, and two entries on one target may not
  share or nest one.
- **A transfer whose restore chain is unknown is refused, not copied whole**: a PostgreSQL source
  with no `_FULL`, an Oracle source whose RMAN preview names no piece (or no source container).

- **The daemon's passphrase goes in its environment, not its command line.** Start it with
  `DB_OPS_SECRET_KEY` exported and `-e DB_OPS_SECRET_KEY` (docs/11, step 3); `control deploy` does
  this for you. Children get the key in `DB_OPS_SECRET_KEY` only, never `--key` in their argv.
- **Report pages need a console login** (`web.reports_require_login`, default true) and directories
  are never listed. A link to a report from Telegram asks for a login once per browser. Same
  accounts as the console. A store with no account gets `admin` / `admin` on first run, and that
  first sign-in must change the password before any page or report opens.
- **Console changes to what runs, or to who may run it, need admin level and the password again**:
  `app_commands` command/working dir/env/role, `cmd_access`, SQL task scripts and inputs, bot
  actions, metric SQL variants, restore scripts, and every change to `telegram_users.json`,
  `users.json`, `emergency_operations.json`, `webhost_config.json`, `store_config.json`,
  `data_files.json`. Logs are admin-only; `min_level_view` is enforced.
- **Bot actions that run free SQL sit higher**: `/spbot_add_sql` 100, `/spbot_sql_to_xlsx` and
  `/spbot_sql_export` 50 in the shipped catalogue. A node's own file is used as written; one that
  still says 10 gets a warning in the log naming the fix.
- **The Vault certificate fetch verifies TLS by default**; name an internal CA with
  `certificate_api_ca_file`.
- **A request's `rules` can only make an operation harder to confirm.**
- **Two restore entries on one machine need a staging folder each, whichever instances they
  restore onto.** The check compared `target_server_id`; it now compares the machine
  (`cmd_access.host`, else `ip`, of each target in the inventory). Two **active** script-driven
  entries that share or nest a `target_backup_dir` on one host are refused when the file is
  loaded - they were deleting each other's staged files on every run. Give each its own folder
  before upgrading; `list-restores` names the pair.
- **A backup job's `cleanup_retention` under a day now deletes.** It was handed to no script, so
  each kept its own default of fourteen days. Read the value on every job that states less than
  86400 before upgrading: it is about to be applied.

### Security
- Estate data in a report (database, job, login names) can no longer close the page's `<script>`
  block and run script on the console's origin.
- The login no longer redirects to another site (`next=//evil.example`).
- No password is an argument any more: `sqlcmd` (`SQLCMDPASSWORD`), the SQL Server backup/restore
  scripts (secret batches on stdin or a private temp file), the SRE bastion scripts and MySQL check
  (stdin), the daemon and deploy (environment). `user-password-show` no longer writes the password
  into `webhost_runtime.log`. An `input` fetcher no longer inherits `DB_OPS_SECRET_KEY`.
- A password typed to the bot is redacted in the store and deleted from the chat.
- A bot value cannot add keys to the JSON request it is put in; a request cannot name the node's
  own keys as a password; service names are validated; PowerShell quoting doubles typographic
  quotes; `delete-files` refuses `..`.
- The secret stores are written atomically, under a lock, and created `0600`.

### Fixed

- **A backup job's retention under a day was never applied.** The scripts take `RETENTION_DAYS`,
  whole days; under a day they were handed nothing and kept fourteen. A lab host on an hourly cycle
  with `cleanup_retention: 7200` held 77 GB after 31 hours. The window now reaches the script
  exactly, as `RETENTION_SECONDS`, and each engine applies it its own way: SQL Server by file age
  in minutes, never past the newest full; PostgreSQL by dropping whole chains on a nearer cutoff,
  with `pg_archivecleanup` behind it; Oracle through RMAN - the policy stays a one-day recovery
  window, the smallest RMAN states, and `DELETE BACKUP COMPLETED BEFORE
  'SYSDATE-<RETENTION_SECONDS>/86400'` runs after each level 0 that succeeded, never reaching into
  that run. `RETENTION_DAYS` is at least 1 for such a job, never absent.
- **Three lab restores onto one machine deleted each other's staged backups.** See *Changed -
  action required*: 21 PostgreSQL restores in 24 failed in `pg_combinebackup` on a soak, each for a
  base backup another entry's copy had just removed.
- **The retention report judged a backup's age on the wrong clock.** `finished_at` carries no
  zone and the cutoff was the node's wall clock: a node at +08 read a backup one minute old on a
  UTC host as eight hours old. Every listing now asks the machine that stamped the files what time
  it is there (`age_seconds`), and the cutoff is on that clock. `prune-backups` also lists a SQL
  Server entry, through the instance with the job's own login, and says by name that it does not
  delete a PostgreSQL backup - a directory in a chain, which it had planned and then failed on.
- **On a Linux node the restore copy's space check measured nothing.** It walked the source share as
  a path, which a Linux node cannot do: it found no files, counted 0 bytes and said *fits* on every
  share-driven SQL Server restore the container worker ran. The check now asks the copy's own engine
  for its selection and sizes (`smb-list` on a Linux node), a source that cannot be read is
  *unmeasured* - held to `space_check.on_unknown` - instead of empty, and what a Linux node fetches
  must fit in its own temp folder before it starts. **Action:** see *Upgrading*, step 6, in the
  release note - entries that passed unmeasured are now held to their factor.
- **A restore onto another machine checks the room before it copies.** A script-driven entry's
  `space_check` was described as on by default and read by nothing: the copy between the two hosts
  took whatever the chain held. `copy-backup-dir` now takes the entry's `space_check` and refuses,
  before the first file moves, a copy whose files do not fit the target's free space times the
  factor - the tool checks, the restore script does not. **Action:** *Upgrading*, step 7.
- **`QUERY_STORE_QUERY_ISSUES` sees a small query on a worse plan, run often**, and stops repeating
  itself. New finding `QUERY_PLAN_REGRESSED_FREQUENT`: a plan with 20 or more recent executions that
  burned 300 s of CPU at 5 times the average of the cheapest other plan of the same query (WARNING;
  CRITICAL at 1,200 s and 10 times), against a seven-day baseline read only for queries already past
  the CPU threshold - so a bad plan that survives the night does not become its own baseline. Every
  finding is judged on the recent rows instead of the six-hour maximum: one heavy execution at 09:00
  was reported again every 15 minutes for six hours. Averages are weighted by execution count, the
  server's own UTC offset replaces a hard-coded time zone, and the query text - copied for every row
  and never shown - is no longer read.
- **The PostgreSQL store's `sslmode` was read and never used**: every store connection was plaintext
  whatever it said. It is passed to the driver now; `prefer`, the default, still connects without TLS,
  as before (B5.5).
- **A link can no longer sign anyone out of the console.** `/logout` acted on a GET with no token; it
  is a POST carrying the session's CSRF token now (B6.3).
- **Result files no longer pile up.** Query results, xlsx and config exports under `runtime/output`
  were never removed; the daemon now deletes them after `output_retention_days` (`config.json`,
  default 7; 0 keeps them) (review 0.25.0, F4.3).
- **A long report alert is a duplicate of itself.** The dedupe window matched `source_id` exactly, and
  a message queued in parts is stored as `<id>:part:<n>`, so it never matched; the parts match now
  (F7.2).
- **One address can no longer lock somebody else's console account.** Eight wrong passwords from
  anyone locked any account, the only admin's included, repeatably. An address is refused after
  `max_failed_logins_per_ip` failures (default 5, below the account's 8); both limits count failures
  inside `lockout_minutes`, so an expired lock no longer relocks on the next miss; a refusal is not
  itself counted, so retrying while refused does not keep an address refused; and a locked
  account costs the same password check as any failure (review 0.25.0, B3.3).
- **Logs rotate every night on a Windows master.** The daemon held `errors.log` and `jobs.log` open, and
  Windows refuses to rename an open file, so the nightly archive failed silently and the files grew
  without bound. Records are written open-append-close there, and a failed rename is retried on the
  next line (review 0.25.0, F2.2).
- **A log record is one line.** A newline in a message or an appended traceback made unprefixed lines
  that the log tail filed under the record before; the files write it as `\n` (F2.3).
- **A threshold override no longer turns a CRITICAL, ERROR or NO_DATA verdict into OK.** Thresholds
  grade the number of a row the metric left at OK, LOGGING or WARNING; to lower a verdict, use
  `severity_map` (F3.2).
- **A container restart months ago no longer warns on every pass.** `DOCKER_CONTAINER_STATS` warns while
  the current run is younger than `DOCKER_RESTART_WARN_MINUTES` (60), and a tab or newline in a docker
  field no longer makes the metric's output invalid JSON (F3.4).
- **A WinRM command with no deadline is no longer cut at 30 seconds.** "No timeout" became the
  connect timeout over WinRM only, so a patch step or a host operation that set none stopped half a
  minute in; the connect timeout now bounds one round trip (review 0.25.0, B5.3).
- **Secret and state files are replaced whole.** The deploy's secret merge, the docker-db registry, the
  store declaration, the daemon's state file and scaffolded files were truncated first and written in
  place - a failure mid-write lost the file - and a new plaintext secret source was created
  world-readable. All go through the atomic writer now; secret files are created `0600`, and any
  other new file with the mode a plain write gives it, so the configuration `init` creates stays
  readable by a daemon running as another user (F6.2, B9.2).
- **A `--remote-dir` or `--container` with a space in it no longer breaks a deploy half way**: every
  name reaches the worker's shell quoted (F6.3).
- **A restore target's cleanup deletes only what its own copy staged.** It recursed every `*.bak` /
  `*.trn` under the import root, so a root set one level too high had the target host's own backups
  deleted by age; `/data` passed the guard. Only `<root>/<mapped database>/` is cleaned now, and an
  entry mapping no database needs a root three levels below `/` (review 0.25.0, B4.2).
- **A `cleanup_retention` under one day is kept as given.** The prune planner reasoned in whole days, so
  7200 s became 0 and then the 14-day default, without a word; it takes the seconds now (B4.4).
- **A restore target that is the source under another name is refused.** The SMB-path guard compared
  strings, so the production server as a name in one field and as its IP or FQDN in another passed;
  hosts are compared by resolved address too, loopback excepted (B4.6).
- **A timed-out app command stops with everything it started.** The daemon killed the shell it
  launched and the app - on a Windows master behind `cmd.exe` - kept running with its `common.cli`
  children after the run was closed and its claim released. Each app now starts as the head of its own
  process tree, and a timeout, a refused claim and a daemon stop end the whole tree - only a tree
  whose head still carries the start time read at launch, so a reused PID is never walked (review
  0.25.0, B2.3, F1.1).
- **A stopping daemon stops its children before closing their rows**, a SIGTERM during start-up no
  longer fails, and a `running` row of a removed app command is closed at start-up (B2.5).
- **A `common.cli` answer behind a native tool's output line is read**, not lost (F1.2).
- **A long Telegram message cut by a rate limit resumes where it stopped** instead of showing the chat
  the delivered parts again, and a failed queue row keeps its `document_path` and buttons (review
  0.25.0, B1.2, B3.2).
- **An operator's `group-level` / `user-level` change is no longer overwritten** by the Telegram
  workflow writing the same file a moment later: both take the file's lock (F8.2).
- **A Telegram document with a long caption is delivered** - a caption over 1024 characters (a SQL
  task's workbook with its status block) was refused, retried and failed, and the file was lost. The
  caption is cut at a line inside the limit and the whole text follows as a message; a rate limit on
  that text resumes at the text and never sends the file again (review 0.25.0, B1.1).
- **The bot's background-task poller never kills a stranger's process, and a timeout stops the whole
  command.** It trusted a bare PID - reused once the task ended - and killed whatever held it at the
  timeout; it killed only the wrapper and left the command running. The exit-code file is read
  first, the process start time recorded at launch must match, and a timeout stops the tree (B1.6).
- **An interrupted bot command is never run again by itself.** A command whose pass was killed mid-run
  was re-run fifteen minutes later with nobody asking - destructive ones included. It is closed and
  its sender told to check and send it again (B3.1).
- **A staging cleanup no longer deletes the full a newer differential restores onto.** A SQL Server
  differential is a `.bak` too, and was taken for the chain's full: once the full passed
  `cleanup_retention`, every run deleted it. Only a full anchors a chain now (by folder, name and
  extension).
- **`prune --apply` never deletes a database's newest full**, nor anything after it, in either
  retention mode. Under `age`, a database whose backups had been failing for longer than the
  window lost every backup it had left.
- **One item that fails in an unexpected way no longer stops the rest of its pass**: a backup whose
  `common.cli` child crashed (its row was also left `RUNNING`), a SQL task whose SQL file is missing
  (now recorded and alerted as that task's failure), a metrics target with a config error (the pass
  failed and `target_health` was rebuilt for nobody), a report that raised. A store outage still
  stops the pass.
- **`rotate-password` stores each new password before it changes the next one**, and rolls the
  change back if storing fails; the refs after it are not started. It changed every password first
  and stored them afterwards, so one store error lost the new password of every ref after it.
- **The Telegram send pass has a budget again: half of the workflow's own timeout**, not the fixed
  180 s of 0.24.0: past it no chat starts another message and the rest stay queued, in order, for
  the next pass. The daemon now tells every app command the timeout it will be killed at, in
  `DB_OPS_APP_TIMEOUT_SECONDS`; a pass run by hand has no budget (review 0.25.0, A2).
- **A message Telegram accepted is never sent a second time because the store was busy.** Recording
  "sent" shared the send's retry loop, so a store write that failed (`database is locked`, likelier
  with ten send threads) sent the message again.
- **A message a killed pass left half-sent is delivered.** A row the daemon killed the pass in the
  middle of stayed "in flight" for good; a pass now puts such rows back in the queue after 15
  minutes (or twice the workflow's timeout), accepting that Telegram may already have had one.
- **One chat that fails a pass no longer costs the others** their counts or the pauses Telegram had
  just imposed on them.
- **The bot keeps hearing commands when a later step of its workflow fails.** The update offset was
  saved only when the whole workflow returned, so a failure after the updates were read, or the
  daemon's timeout, re-read the same 20 updates on every run and never the ones behind them. It is
  saved as soon as the updates are stored. `telegram_config.json`, `telegram_groups.json` and
  `telegram_users.json` are rewritten atomically.
- **One app command that cannot be started no longer stops the daemon.** A missing `working_dir` or a
  failed spawn ended the scheduler - and, under `restart: unless-stopped`, looped so that no command
  listed after the bad one ever ran. It is recorded as that command's `error` run and retried on its
  `retry_interval`. A child that outlives SIGKILL for five seconds no longer ends the daemon either.
- **A point-in-time restore of an entry with `copy_recent_hours` 0 copies every file up to the
  moment**; it built a zero-wide window and copied nothing.
- **A container recreated under a new host name frees the runs its predecessor left open at once.**
  They used to hold their task's target for the run's timeout plus an hour, because the new host
  name read as another machine whose processes cannot be checked. Rows written by 0.25.0 and earlier
  carry no identity, so the upgrade *to* 0.26.0 still waits for them (or use `close-run`).
- **A SQL task run past its timeout is over.** The timeout reached the driver as a per-call query
  timeout, which restarts on every batch and result set; the `run-sql` process had no deadline; and
  a `running` row whose process was alive was never closed - one run went 13 hours on a 2-hour
  timeout, and its target did not run again until the container was stopped. Now, when a task is
  next scanned (or run by hand), a run of it still `running` past its `timeout` is closed as an
  *error by timeout* and its process stopped first, so no second copy starts on top of it; a run
  nothing else looks at is still stopped by its own deadline at twice the timeout plus the connect
  timeout. **Action:** `timeout` now bounds the whole run of a target - *Upgrading*, step 10.
- **A SQL Server restore copies only the chain it applies** - the newest FULL at or before the moment,
  its newest DIFF and the LOGs after (for a point in time, up to the LOG that carries it) - instead of
  every backup written in `copy_recent_hours`. A weekly FULL made that eight days of DIFFs and LOGs:
  527.9 GiB copied for a 56.8 GiB chain, and the space check refused. The space check counts the chain.
  `copy_recent_hours` is the fallback for a database whose chain cannot be settled.
- **`copy_selection` on a restore entry** (and on the `backup_restore` block): `"chain"` (default)
  copies only the backups the restore applies; `"window"` copies every file of `copy_recent_hours`,
  as before. The space check counts the same list.
- **A restore's instance metadata replays onto a lab target.** The replay target was looked up
  among the monitored instances only, so an inactive lab with metrics off was never found and
  no login was replayed; it is now looked up in the whole inventory, and a container is matched on
  `container_name` too.
- **A SQL task whose statement runs out of time says so** - *a statement ran past this target's
  timeout* - instead of *could not reach ... the instance is down* (ODBC reports both under HYT00).
- **`create-db-docker --install-docker` installs Docker on a host that cannot reach get.docker.com**
  from its distro packages (`docker.io` + `docker-compose-v2`). A failed download used to pass as a
  finished install, and the command then called Docker installed-but-unusable on a host that had none.
- **The long-running and long-waiting request metrics name the client's address** - `client_ip=`
  in `QUERY_LONG_RUNNING` and `QUERY_LONG_WAITING_OR_ROLLBACK_REQUESTS` on SQL Server (both variants).
  0.25.0 added it to the metrics that already joined `sys.dm_exec_connections`, and these two had the
  join only in a commented-out draft.
- **The SQL Server 2008 R2 sleeping-open-transaction metric names the client's address**
  (`client_ip=`), as the current variant has since 0.25.0.

## [0.25.0] - 2026-09-30

### Added

- **`examples/an-estate-by-command-on-a-new-machine.md`** - a whole estate rebuilt on a new machine
  from `pip install dbabrain`, one command per item (secrets, bot, inventory, host logins, SQL tasks,
  backups, restores, schedule), then the checks and the clock. Written from a real move carried out
  by an AI agent, with what each step measured.

### Changed

- **SQL Server blocking and lock-holder metrics name the client's address.** `LOCK_BLOCKING_SESSIONS`
  (009), the sleeping-open-transaction metric (024) and the lock-holder metric (025, and its
  2008 R2 variant) add `client_ip=` - `sys.dm_exec_connections.client_net_address` - beside
  `host=`. `host=` is whatever name the client sends, and an application can send any name or
  none; the address is where SQL Server saw the connection come from. The showcase scrub
  learns `client_ip=` as an address, so a published page does not carry it.
- **The Telegram send pass takes the oldest 5 messages of each chat** (`send_per_chat` in
  `telegram_config.json`), every chat in turn, every second - not the oldest 50 of the whole queue.
  One chat's backlog no longer holds any other chat, the bot's replies to its commands included.
- **The Telegram send pass sends to 10 chats at once** (`send_threads` in `telegram_config.json`);
  each chat's own messages still go one after another, in order. One chat at a time, a pass through
  a backlog in six chats took ~40 s, and the bot - which reads its commands once per pass - answered
  a minute late.
- **The send pass has no 180 s budget any more.** It is bounded by the Telegram workflow's
  `time_window.timeout` in `app_commands.json`, like every other app.
- **A Telegram rate limit (HTTP 429) pauses that chat, not the pass.** The row stays queued, the
  pass carries on with the other chats, and the chat is left alone for the seconds Telegram asked
  (kept in `runtime/telegram_chat_pauses.json`). A pass no longer sleeps out another chat's limit.
- **One way to run anything on a host** (rules R44). `common.cli run-cmd`'s executor,
  `remote_exec`, now runs every command, script, stream and file transfer on a host; the backup
  scripts, `run-sqlcmd`, the restore staging copy and the file relay no longer carry SSH or
  PowerShell executors of their own.
- **`list-databases` on SQL Server says whether the login can open each database** (`has_access`,
  from `HAS_DBACCESS`). The instance export, the patch gate, the restore check and the PostgreSQL
  metrics now list databases through the same query, and a SQL task's "database does not open"
  diagnosis asks `list-databases` instead of a query of its own.
- **Every schedule shown is the one the scheduler reads.** The status page, the web console, the
  bot's SQL task listing, `ops-status` and the server report read a `time_window` through the
  scheduler's own parser: an older field name shows its value instead of nothing, and a window the
  scheduler refuses is shown as unset (the bot says it is invalid) instead of as a schedule. The
  bot's task listing now shows the `weekdays` a task is limited to. A registrar's refusal of a bad
  `time_window` value is worded by that parser.
- **`db_ops.config` is removed; import `db_ops.lib.config`.** It has been an alias of
  `db_ops.lib.config` since 0.24.0, and nothing in DBA Brain imports it any more (rules R41: the
  root package holds only its entry point). Code of yours that imports `db_ops.config` changes that
  one line.
- **`db-ops --help` lists apps and shared layers apart**, as the README does: `common` and `db` are
  shared layers, and `daemon` is marked as the `jobs` app. The docs name the scheduler "`jobs` (run
  as `db-ops daemon`)" and the README counts fifteen components everywhere.
- **A restore copies as far back as its entry's `copy_recent_hours` says.** `workflow` and
  `restore-workflow` passed `--copy-hours`, default 24, over every entry's setting, so the setting
  never took effect - a source with a weekly FULL copied a day of LOGs and had no chain to restore.
  The flag is now an override only.
- **The reference describes a restore entry's own copy settings.** `copy_recent_hours`,
  `copy_file_patterns` and `space_check` were read on a restore entry and described only on the
  `backup_restore` block, or nowhere; `check-objects` now reports a wrong value in them.
- **The reference describes every key every `common.cli` command answers** (rules R16). Sixteen
  were missing, among them `move-db-docker`'s `ok` and `copy-schema`'s `mode`; a script that reads
  an answer can now look up each key it gets.

### Fixed

- **Registering SQL from the Telegram bot works again.** The bot sends the task's name as
  `display_name`, the field `add-sql` documents, and `add-sql`'s JSON form refused it as an unknown
  field: every registration from the bot failed. Every spelling of a flag is a key now, so
  `display_name` and the older `sql_name` both name the task.
- **`list-schemas` answers.** Since 0.22.0 every successful run came back as a failure,
  `KeyError: 'database'`: its message still read the answer key 0.22.0 renamed to `database_name`.
  The Telegram spreadsheet upload's schema prompt uses it.
- **`add-sql` and `sql-target-add` register a task on a new install.** A task's chats defaulted to
  the `sql` level, which exists only where a Telegram group defines it, so on a fresh install
  both refused their own default. They default to `sql` where the group exists and to
  `logging` / `error` otherwise.
- **A script run on a host is a file, never the shell's stdin.** A command inside it that read
  stdin - `docker compose exec` does by default - swallowed the rest of the script, and the run
  ended with exit 0 and its tail never run. Scripts are now placed in the login's home, private,
  run with stdin closed and removed; a PowerShell script is a `.ps1` run with `-File` instead of
  `-EncodedCommand`, which ran out of command line past ~8 KB.
- **`run-sqlcmd` over WinRM reports sqlcmd's own exit code.** Through pypsrp a failed batch could
  read as a success: it reports only whether the error stream was written.
- **A Linux node cleans a Windows target's import share.** In 0.24.0 it held every aged file back
  as `still_needed` and deleted nothing (released as a known limitation). The chain is now read from
  the share's own listing.
- **The first restore onto a new Linux target is no longer refused by its own space check.** `df`
  ran on the staging folder, which the copy only creates after the check, and answered "no such
  file"; the check now measures the nearest folder above it that exists, as it already did locally.
- **A restore whose source has no backup folder yet says so.** It was told the SSH user could not
  read `/opt/db_ops/backup/<lab>` - a folder no backup had created. A missing folder, one removed
  while it was being listed, and one the SSH user cannot read are now three different messages.
- **A report file sent while its chat is rate-limited is no longer lost.** `sendDocument`'s 429 was
  a plain error: three immediate retries, then the row failed. It is a pause now, as a message's is.

## [0.24.0] - 2026-09-26

### Added

- **An SMB restore can read the certificate dbabrain's own backups were encrypted with** -
  `source.backup_certificate` (`name`, `password_ref`, `source_dir`): the `.cer` / `.pvk` pair the
  SQL Server backup job exports beside the backups, read from the share, or over the source host's
  own login from `source_dir` when the share refuses it. Until now an SMB entry took a certificate
  only from a Vault URL, so a share of dbabrain's own encrypted backups could not be restored onto an
  instance that did not already hold it.
- **`target.sql_container`, and `run-sqlcmd`'s `container`** - every `sqlcmd` of the restore runs
  inside the target's SQL Server container (`docker exec`), for a host with no `sqlcmd` of its own.
- **`examples/docker-node/`: a node in Docker, installed and upgraded** - the published image under
  `docker compose`, from `init` to a running node, then upgraded to the next release (back up, keep
  the old image, `upgrade-config` in the new image while stopped) and rolled back. The compose file
  pins the container's `hostname`: left to Docker it is the container id, so every upgrade was a new
  node to the store, and the runs the old container left open waited out their timeouts.

### Changed

- **`db-ops init`, `guide`, `encrypt-secret`, `export-data` and `import-data` take one JSON object,
  like every `common.cli` command.** Typed bare they work as before (`db-ops init` still prints what
  it created). An option is now a key - **breaking** for scripts that pass flags:
  - `init --force` -> `db-ops init '{"force": true}'`; `--app-name X` -> `"app_name"`
  - `guide --write` -> `db-ops guide '{"write": true}'`
  - `export-data FILE --root D --no-secrets --no-assets --force` -> `{"bundle": "FILE", "root": "D",
    "include_secrets": false, "include_assets": false, "force": true}`
  - `import-data FILE --root D --plan --force` -> `{"bundle": "FILE", "root": "D", "plan_only": true,
    "force": true}`
  - `encrypt-secret --source S --dest D` -> `{"source": "S", "dest": "D"}`; the passphrase stays
    `DB_OPS_SECRET_KEY` or `--key-base64` after the JSON, never in the request.
  An old flag is refused with the key it became. Add `"format": "txt"` for the text the flags printed.
- **`db-ops check-credentials` takes a JSON object, like every `common.cli` command.** Bare it works
  as before and checks this node. The folder it took positionally is now a key - **breaking** for a
  script that passes one: `check-credentials data` -> `check-credentials '{"data_dir": "data"}'`
  (the old form is refused with the request it became). A Telegram `sql_execute` command's login is
  now resolved against the folder being checked, not always this node's own.
- **`common.cli` reads no configuration: 20 commands take the login, the policy and the price in
  their request** - **breaking** for a script that sent only a `server_id`. `shrink-log`,
  `kill-spid`, `start-job`, `disable-job`, `list-databases`, `list-schemas`, `list-jobs`,
  `db-status`, `create-table-from-xlsx`, `trace-session` and `copy-schema` (per side) need
  `"connection"` - the SQL login, complete: `db_type`, `host`, `port`, `username`, `password`.
  `host-facts`, `host-service` and `host-restart` need `"access"` (the host's `cmd_access` with its
  password or key file) and take `"policy"` (the maintenance policy). The three SQL Server patch
  commands need both. `sqlserver-export-instance` / `-replay-instance` need `"policy"` (the instance
  policy's content), and a replay takes its bundle's `"secrets"`. Every command behind the
  confirmation gate, and `authorize`, is priced by the request's `"rules"`, else by the ladder the
  package ships - never by this node's `data/emergency_operations.json`. A request missing the field
  is refused with its name. `credential_name`, `user_ref` and `data_dir` are no longer read by
  these commands. The bot, `sql_tasks` and `backup_restore` send complete requests on stdin; a
  person at a shell writes the request to a file and passes `@file`, or pipes it with `-`.
- **`run-sql`, `run-cmd`, `probe-host` and the file transfers read no configuration either** -
  **breaking** for a script that sent only a `server_id` (or an ip). `run-sql` needs
  `"connection"`, and an Oracle 8i bridge's signing secret comes in `"secrets"`; `run-cmd`,
  `fetch-file`, `send-file` and `pack-files` need `"access"`, and `relay-file` one per side;
  `probe-host` needs `"host"`. `target` is only the label the answer carries. `run-sql` no longer
  reads `credential_name`, `user_ref` or `data_dir`, and its `--key` changes nothing (still accepted).
- **One command per job - four commands that repeated another's are gone** - **breaking** for a
  script that calls them; `upgrade-config` (step `moved-commands`) rewrites a configured command
  line that names the two that moved:
  - `control inventory-summary` -> `common.cli inventory-summary`; `control inventory-workflow` ->
    the worker's `reports.cli inventory-workflow` (from the master: `control worker-run -- ...`).
  - `common.cli timezone` -> `db.cli timezone`, which now answers without opening the store unless
    asked to `--record` / `--list`, answers with no readable `config.json`, takes `--format txt`,
    and still takes the JSON request the common command did.
  - `db.cli self-status` -> `common.cli self-status`, which takes each app command's last run in
    its request (`last_runs`); `/spbot_self_status` fills it from the node's store as before.
  - `backup_restore.cli verify-restore` -> `common.cli verify-restore` (the restore workflow's last
    phase already asked that one).
  - **A SQL Server RESTORE is written in one place.** The nightly SMB restore (`restore-latest`, and
    `restore-workflow` for an SMB entry) asks `common.cli restore-full` / `restore-diff` /
    `restore-log` for each step instead of writing its own statements; the text is unchanged except
    that a quote in a database name or path is now escaped correctly. The steps gain `move_files`
    (the data and log paths, logical names read on the server) and `sqlcmd` (run where the SQL
    Server is, one file per call). A point-in-time `STOPAT` is written `YYYY-MM-DDTHH:MM:SS` for every
    caller - the form SQL Server reads the same under any login language. Gone: `common.cli
    restore-database` (a third chain restore nothing called), `backup_restore`'s hidden
    `restore-full` / `restore-diff` / `restore-log`, and `restore-by-id` for an SMB entry - use
    `restore-workflow --restore-id <id>` (latest, or `--point-in-time`).
- **A host login in a `common.cli` request is used as stated.** A `key_file` given as a bare file
  name is refused - state its full path - and a `password_ref` must be among the request's
  `"secrets"` or in the environment: nothing under `common.cli` looks a key up in `data/ssh_keys/`
  or opens the secret store for a host any more. The apps (the bot, `sre`, `metrics`, backup and
  restore) resolve both from their own `data/` before they call, as before.
- **The SQL Server restore reaches its shares through `common.cli`: four new commands, `smb-list`,
  `smb-get`, `smb-delete` and `smb-credential`** (stdin only - each carries the share's password).
  `smbclient` on Linux, the UNC path after `cmdkey` on Windows, one answer shape. The restore no
  longer starts `smbclient`, `cmdkey`, a local PowerShell or `sqlcmd` itself: a Windows target's
  certificate import runs through `run-cmd` over WinRM, a local one and CHECKDB through
  `run-sqlcmd`. Two behaviours change: a **dry-run cleanup on a Windows target now deletes
  nothing** (it deleted before), and `verify-restore` works on a Linux target (it could not start
  there). The `smbclient` login file is deleted after each call instead of left in the temp folder.
- **The root command no longer has a logging/store smoke test** (`db-ops --message`, `--recent`,
  `--export-sqlite-schema`). The schema export is `db-ops db export-sqlite-schema`.
- **Notification routing is read in-process** - no process per level is started to learn a chat.
  `telegram.cli route` / `groups` still answer for a person at a shell. `telegram.cli
  queue-metrics-reports`, an unused alias of `reports.cli queue-metrics-reports`, is removed.
- For code that imports db_ops: `db_ops.config`, `db_ops.__version__` and `db_ops.lib.common_cli`'s
  `run` / `run_allowing_failure` keep working under new homes - `db_ops.lib.config`,
  `db_ops.lib.version`, `db_ops.transport.common_cli` (a new layer, docs/15_transport.md).
  `db_ops.levels` is `db_ops.lib.levels`; `db_ops.common.data_sources` is `db_ops.lib.data_sources`.
  `db_ops.metrics.targets` is `db_ops.lib.data_sources.collection_targets` (the same module object)
  and `MetricTarget` is `db_ops.lib.metric_target.MetricTarget`, still importable from
  `db_ops.metrics.models`.
- **`sre` reaches the lab through `common.cli run-cmd`, and needs the `ssh` extra.** Its ssh and
  ansible calls - `ssh`, `run-bastion-*`, `check-*` - no longer start an `ssh` of their own; the
  request goes to `run-cmd` on stdin, so the passwords a check carries are off this machine's process
  list. Install `'<package>[ssh]'` (paramiko) where `sre` runs. The key is still
  `sre.credentials.ssh_identity_file`, the connect timeout still 10 seconds, and a step that never
  reached the bastion still exits 255. `--dry-run` prints the `run-cmd` request instead of an `ssh`
  line. The SQL Server AG orchestrator copies its archive with `push-file` (sha256 checked on both
  ends) instead of `scp`.
- **Metric collection runs its SQL and scripts through `common.cli metric-batch`, one process per
  target.** It ran them in-process until now. A target's due metrics go out together; a metric with
  a time window (CHECKDB, an index scan, a restore validation) runs in a batch of its own, so it
  never holds the quick metrics' rows back from the store. What a metric stores is unchanged -
  status, message, error type, raw streams - and each row is still stamped with when its own metric
  ran. A batch that stops answering altogether now fails its items with the reason after a bound,
  instead of holding that server's worker for the rest of the pass. For code that imports db_ops:
  `metrics.executor.execute_metric_sql` and `collector.execute_local/_ssh/_winrm` keep working (a
  batch of one); `collector._shell_prelude`, `_script_with_env` and `_remote_failure_phase` are
  gone (`common.remote_exec.shell_prelude`, `common.metric_batch`); `load_database_inventory` is
  `db_ops.lib.inventory_file` (still importable from `common.sql_execution`).
- **`job_runs_history` and `metric_results_archive` have primary keys** - the id each row kept
  (`log_id`, `result_id`). A new store has them at once. **An existing store needs one command, run
  when it can take it:** `python -m db_ops.db.cli archive-keys` reports rows, missing ids and
  duplicate ids; `--apply` adds each key the report found clean. No app does it on its own - on a
  long-running store the archive is millions of rows, and the key reads all of them.
- **The shipped app-command catalogue now carries the values this estate runs**, apart from
  `node_role`, which stays `all` so a single-machine install works as copied:
  - `APP-BACKUP-RESTORE`: `retry_interval` 30 -> 60 and `timeout` 7200 -> 18000 s;
  - `APP-SQL_TASKS`: `timeout` 1800 -> 18000 s;
  - `APP-REPORTS-CREATE`: `timeout` 2400 -> 1800 s.
  The `data/app_commands.example.json` that ships is the catalogue itself now (it still said 300 s
  and an older control command). A node made before this keeps its own values.
- **`APP-BACKUP-RESTORE` runs every second by default, as `APP-SQL_TASKS` does** - it was 30 s. One
  run works through its due jobs one at a time and the next run takes what is left, so the interval
  decided how many jobs due together could run at once: at 30 s, three restores due at the same
  moment reached two at once. A node made before this keeps its own value;
  `app-command-set '{"app_code": "APP-BACKUP-RESTORE", "time_window": {"repeat_interval": 1}}'`
  moves it.
- **`force-hourly-report` (and `/spbot_report_hourly_metrics`) reports the stored results; it
  collects nothing.** It used to run `metrics.cli collect --force` for the target first. The report
  is now at most one collection cycle old and takes seconds. The bot command's `full` word is gone
  with it; `--include-windowed` is refused with the command that replaces it - `metrics.cli collect
  --target-id <id> --force --include-windowed`, then the report. The workflow's JSON says `stored`
  where it said `collect`.
- **`control worker-create-db-docker` builds on the worker's host through `common`, and registers on
  this node.** It used to run `sre.cli create-db-docker` inside the worker container, with the
  secret-store passphrase on that command line. The record (`created_by:
  db_ops.control.worker-create-db-docker`) and a `--password-text` are written here and reach the
  worker with the next deploy; `--container` and `--pull-config` do nothing any more. The SSH user
  must be able to run `docker` on the host - `--install-docker` arranges it. New:
  `--overwrite-secret`. For code that imports db_ops: `db_ops.sre.docker_db` is
  `db_ops.lib.docker_db_registry`.
- `run-cmd` with an inline `access` block that is a key login no longer reads `users.json` - it
  never used what it read, and a missing or broken file stopped a complete request.

### Fixed

- **Importing a backup certificate never drops one any more, and finds it by thumbprint.**
  `restore-key` and the SQL Server script restore dropped any certificate of the requested name and
  created their own - on a target dbabrain also backs up, that was its own `db_ops_backup_cert` -
  and the SMB restore skipped the import when the name existed, so the source's never arrived. All
  three send one batch now: a certificate already there is left alone, and a name that belongs to
  another certificate becomes `<name>_<first 8 hex digits of the thumbprint>`. `restore-key` answers
  `imported: false` when it was already there, and can run through `sqlcmd` like `restore-full`.
- **The Windows backup job leaves the exported certificate pair as readable as the backups** -
  the engine writes it for its service account alone, and a restore reading the share was refused.
- **An SMB restore on a Windows node copies only the mapped databases**, as a Linux node always
  did; it copied every recent file on the whole share.
- **"copy-backup selected no files" names the newest matching file and how old it is**, so a share
  nothing writes to any more reads as that and not as a copy window set too narrow.
- **A restore checks its whole backup chain in one SSH session**, not one per file, and a session
  that never opened (refused, reset, timed out) is tried again twice before the step fails.
- **A full disk under the runtime store no longer ends the daemon.** A write that failed with
  `53100` (and the store out of memory, `53000`/`53200`) is waited out like a restart, and the
  budget for one outage is 6 hours instead of 10 minutes: the full disk that ended a soak took a
  person an hour to clear, and the daemon that had given up was all that stayed down. A store that
  answers a definite error - a wrong password, a missing database - still ends it at once.
- **A dead SQL-task run is reported once.** Ten scans run at once, and each closed the same
  abandoned `running` row and sent its alert; the close is a claim now, and only the scan whose
  close lands reports it.
- **A restore's Telegram message carries its result, not its work.** A 13-database
  `restore-latest` put its whole output in its END message - 181,174 characters in 49 parts - and
  the Telegram workflow timed out sending it. The message now summarises each source's verdict and
  per-database statuses (at most about three parts); the whole output stays on the run's
  `job_runs` row.
- **A Telegram pass stops starting messages after 180 s** and leaves the rest queued for the next
  pass, instead of being killed at the daemon's 300 s timeout with everything behind it waiting.
- **`run-sql` answered a traceback instead of JSON when its target dropped mid-statement** (seen on
  a PostgreSQL SQL task in 0.23.0). Closing the dead connection raised and hid the reason; the run
  now fails with the statement's own error, and nothing `run-sql` meets reaches its caller as a
  traceback.
- **`/spbot_self_status` said "last run unknown" on a node upgraded from before 0.23.0.** Its
  command line ran `common.cli self-status`, which never reads the store. The bot now states each
  app command's last run in the request, so that line works as it stands; one that 0.23.0 pointed at
  `db.cli self-status` is pointed back by `upgrade-config`'s new step, `moved-commands`: a command
  line naming a command that moved to another CLI (`self-status`, `timezone`, and `ops-status`,
  `queue-telegram-message`, `restore-drill-status` from 2026-08-15) is pointed at it, the file's
  layout kept. Run `upgrade-config` after upgrading, as always.
- **No app imports `common` any more** (rules R03, absolute since this version). `control`: the
  export's identifier scan runs `common.cli check-identifiers`, and the deploy's config-drift
  question runs the new `common.cli ask`; a scan that refuses (nothing to search for, nothing to
  read) now answers `data.refused: true`, so the export still says SKIPPED for it and stops for any
  other failure. **`control`'s SSH session to the worker and `backup_restore`'s to a Linux restore
  target** are a `lib.remote_host.RemoteHost`: every command is `common.cli run-cmd`, every file
  `push-file` / `pull-file`, each its own session (about half a second); many files travel as one
  tar. What you see: a deploy's remote output now appears when each command ends rather than as it
  streams, and `worker-run --sudo` is `run-cmd`'s sudo (the line under `sudo -S`, the SSH password
  on stdin). A certificate's private key is written on the target from memory - it never touches
  the local disk. The Windows preflight's SMB share is a `run-cmd` over WinRM.
- **Two `common.cli` answers carried a key the reference did not describe**:
  `check-references` answers `data_dir` (the root it checked) and `list-backup-files` answers
  `unreadable` (files named like backups the engine could not read). `shared_config_objects.json`
  now describes both.
- **`sre ... --dry-run` printed the MySQL admin password.** The hop to a node quotes the command a
  second time, and the redaction's `--password=` pattern did not match the doubled quoting. A dry run
  now masks every password it carries as `***` where it is built - the MySQL admin password, the
  guest password a bastion script receives, and the base64 PowerShell payload of resolved
  credentials.
- **An Oracle SQL task with `parameters` runs on a direct connection, and a PostgreSQL task can
  have parameters.** Parameters were T-SQL `DECLARE @name` lines in front of the script: on a
  direct Oracle connection they reached Oracle as they were and the task failed at its first run,
  and a PostgreSQL task could declare none. On both engines the script now says `:name` and the
  value is **bound by name** - a quote in it is data, never SQL. On Oracle `&name` is still a
  SQL*Plus substitution, as on an 8i bridge target, so one command means the same on both. SQL
  Server is unchanged.
  - **What to do:** say each parameter in the script as `:name` (or `&name` on Oracle). On
    PostgreSQL the value arrives as text and the server types it from where it stands; where
    nothing says (`:d IS NULL`), write a cast - `CAST(:d AS date)`.
  - `sql-command-add` refuses an Oracle or PostgreSQL parameter no script of the task says: it
    would take a value and bind it to nothing.
  - `run-sql` takes `named_params` (`{"name": value}`) on Oracle and PostgreSQL, bound where the
    SQL says `:name` in the driver's own placeholder. A name no statement says is refused before
    connecting.

## [0.23.0] - 2026-09-26

0.22.0 was built and soaked, then abandoned on 2026-09-24 before its day was out (the operator:
skip 0.22, fix and run again). Its content ships in the next release, with what follows it.

### Added

- **A SQL task can run on PostgreSQL.** `sql-command-add` and `add-sql` accepted only
  `sqlserver` and `oracle`. `postgresql` is now in `SQL_TASK_DB_TYPES` and in the reference.
  `run-sql` runs a PostgreSQL script one statement at a time
  (`lib.sql_text.split_postgresql_statements`): pg8000 returns one result set per execute, so a
  script of two SELECTs and an INSERT came back as one merged set with `affected_rows` 0. A `;`
  inside a string, a quoted name, a comment or a `$$` body does not split. A PostgreSQL task with
  `parameters` is refused at registration, because they are T-SQL `DECLARE` lines.
- **`examples/lab-sql-tasks.md`**: scheduled SQL tasks on SQL Server, PostgreSQL and Oracle
  labs - a 10-, a 5- and a 1-minute task on each, overlapping - with how each engine splits a
  script and what `max_parallel` does to nine long tasks, measured.
- **`examples/lab-create-backup-restore.md`**: a lab database built with `dbabrain sre
  create-db-docker`, registered, backed up, restored onto a second machine, then restored to a
  moment, for SQL Server, PostgreSQL and Oracle (Oracle XE too). It covers what MySQL can and
  cannot do, and was written from the 0.23.0 lab drill.
- **`checkdb` on a restore entry** (default `true`): `false` skips the `DBCC CHECKDB` after each
  restored database, and the run says it did.
- **`upgrade-config`: one command after `pip install --upgrade`.** It moves your `data/*.json`
  to the shapes this version writes: it refreshes the shipped reference files, renames fields,
  and moves a restore's machine ids. By default it only shows the plan. `{"dry_run": false}`
  applies it and copies every changed file to `runtime/config_upgrade/<UTC stamp>/` first. A
  second run changes nothing. `init` and `import-data` say when there is something to move.
- **`AGENTS.md` is the operating guide for an AI agent.** `init` writes it into the tool root:
  300-400 lines covering the rules, the secret key and the clock, adding a database with
  `instance-add`, every config file and the command that writes it, `describe-object` /
  `check-objects` / `check-references`, schedules, the daemon, the status questions, and the
  commands that change a database or a host. **Every `init` replaces it with the installed
  version's guide**, so it always matches the build; a copy somebody edited is saved to
  `runtime/agents_guide/` first. `dbabrain guide --write` puts it in the current directory before a
  root exists.
- **`/spbot_self_status` lists what the node schedules**: one line per app command with its
  `run_mode`, interval, hours, weekdays and last run. `python -m db_ops.db.cli self-status` is the
  front door with the last-run column; `common.cli self-status` still answers without a store.
- **Fifteen more records are described field by field** in `shared_config_objects.json`:
  `sql_command`, `sql_target`, `db_instance`, `telegram_support_command`, `telegram_cli_execute`,
  `metric_definition`, `metric_variant`, the logins in `users.json` (database and OS, group and
  credential), `telegram_group`, `telegram_user`, `report_entry`, `webhost_app`, and every other
  config file in `data/` - policies, store, Telegram settings, SLA, docker connections, the SRE
  lab. `check-objects` now reports a misspelled key on a whole record instead of ignoring it.
- **The shared-object reference describes itself.** Four entries - `reference_entry`,
  `reference_field`, `reference_site`, `reference_constraint` - so `check-objects` holds
  `shared_config_objects.json` to the same rules as every other file, and reports a key it does not
  name. Their first run found two `legacy_fields` written as a list and twelve empty `rule`s, fixed.

### Changed

- **A restore's copy and staging cleanup are `common.cli` commands**: `backup-chain` (which files
  the restore needs), `copy-backup-dir` (host to host, one tar stream, mirroring the source) and
  `prune-staged-backups`. All three are stdin only. They ran inside the backup_restore app, over
  SSH sessions it opened itself, so a restore was one long call with two steps nobody could run or
  watch alone. The app now resolves the logins and calls each step in turn. The code moved from
  `db_ops.backup_restore.transfer` to `db_ops.common.backup_copy`.
- **`create-db-docker` and `move-db-docker` run in `common.cli`** (JSON on stdin, JSON out), like
  backup and restore. `sre.cli`'s commands, flags and `/spbot_create_db_docker` are unchanged: `sre`
  stores and resolves the database password and the SSH logins, calls `common.cli`, and writes the
  connection record; `common` reads no configuration. Progress streams to stderr as it happens.
  The code moved: the spec and engine facts to `db_ops.lib.docker_db_spec` (was
  `db_ops.sre.docker_db.models`), the provisioner, mover and templates to
  `db_ops.common.docker_db` (was `db_ops.sre.docker_db`).
- **The SMB SQL Server restore runs its statements through `common.cli run-sqlcmd`** - every
  RESTORE, recovery, CHECKDB and resume probe, with the values resolved by the app and the same
  `Invoke-Command` (Windows) or SSH `sqlcmd` (Linux) command as before. The Linux path now logs
  its progress events, as the Windows one always did.
- **`backup-database` refuses `server_metadata`.** It resolved its instance out of the inventory -
  the one configuration read behind the command. Export with `sqlserver-export-instance` after the
  backup, which is what the backup_restore app already does for an entry with the block.
- **`db_ops.common.restore.sqlserver.timeparse` is `db_ops.lib.restore.moment`**, with
  `server_clock_text`: every engine's restore reads a moment through it.
- **`common.cli` is held to the operator's rule by tests**: backup, restore and the lab-docker
  commands read no configuration (named, without exception), `common` imports only `lib`, launches
  no CLI by any spelling, and the resolver tier that still reads config may only shrink.
- **`sql-command-add` and `sql-target-add` refuse a key they do not know** instead of dropping it,
  check the record against the reference before writing, and now write `progress_per_file`, a
  target's `sql_access` and a `notify` block given as a block.
- **One name per concept.** The standard names are `active`, `environment`, `sort_order`,
  `database_name` and `major_version` on an instance, `sort_order` on app and Telegram commands,
  `database_mappings` on a restore entry, and `display_name` on a SQL task (was `sql_name`; also
  the `/spbot_add_sql` parameter and `add-sql --display-name`). Every reader accepts both
  spellings, every writer
  writes the standard one, and the shipped examples use it.
  **To move your own files:** `python -m db_ops.common.cli upgrade-config '{}'` shows the
  plan; `'{"dry_run": false}'` writes it. A record whose two spellings disagree is reported and its
  file left untouched. Do this only once every node that reads these files runs this version: an
  older one does not know `active` and reads an instance switched off as switched on.
- **A restore entry names its machines as `server_id` and `target_server_id`**, on the entry,
  like the script-driven entries already did. `source.id` / `target.id` are still read;
  `restore-add` moves them. `check-references` now fails a restore that names a machine the
  inventory does not have.
- **A Telegram chat or person is switched on by `active: true`**, like every other record,
  instead of `status: "active"`. Both are read; `upgrade-config` moves your files. A credential's
  `notes` is `note`, and a console block's `ord` is `sort_order`.
- **A secret ref is `password_ref` everywhere.** A restore's `source` / `target` blocks say
  `password_ref` / `sql_password_ref`, a docker connection `password_ref`, and `create-db-docker`
  takes `--password-ref`. `password_env` now means only what it says - an environment
  variable's name. The old spellings are still read and still accepted as flags.
- **A docker connection says `db_type` and `database_name`** (was `engine`, `database`); the
  store and the backup and restore-drill policy overrides say `database_name`; an SLA policy's
  title is `display_name` (was `name`).
- **Every request and answer of `common.cli` is described** in `shared_config_objects.json` (kinds
  `input` and `output`, 149 entries in all); `describe-object` answers for them like for config.
- **Requests use the configuration's names too**, and the old ones still work: one database is
  `database_name` (was `database`), a list of names `database_names` (was `databases`), the SQL
  `sql_text` (was `sql`), `destination` (was `dest`), `job_name` (was `job`), `file_path` /
  `file_base64` (were `xlsx_path` / `xlsx_base64`), `refs` (was `password_refs`); a store
  block's `database_name` (was `database`) and `db use-store --database-name`. The tool's own
  callers - the Telegram commands, the restore steps - send the new names.
- **`common.cli` answers use the same names as the configuration.** Renamed outright, with every
  in-tree consumer: a byte count is `size_bytes` (was `size` in list-backup-files, delete-file(s),
  pack-backup, pull-file, push-file, and `bytes` in fetch/send/pack/relay-file), a duration is
  `duration_ms` (was `elapsed_seconds`), one database is `database_name` (was `database` in
  run-sql, list-databases/-schemas/-jobs, create-table-from-xlsx, trace-session, list-backup-files,
  verify-restore and restore-drill-status), a restore step names its engine `db_type` (was
  `engine`), run-sql's profile is `target_profile` (was `engine`), and app-command-set lists what
  it changed under `changes` (was `changed`, which elsewhere is a yes/no). Also: delete-file(s)
  report `freed_bytes` (was `bytes_freed`), pack-backup `file_count` (was `packed`), run-cmd
  `duration_ms` (was `duration_seconds`), copy-schema's plan and lift-example `destination` (was
  `dest`), a restore-database plan `database_names` (was `databases`). **A script parsing these
  answers must follow.**
- **The inventory is a report, not an app.** `APP-REPORTS-INVENTORY-WORKFLOW` is gone; the inventory
  is the report `rp_inventory_health` in `reports_config.json`, built by the reports app's pass
  like the metrics and backup reports. `upgrade-config` moves an existing node's app command into
  it - keeping its schedule and switch - raises `APP-REPORTS-CREATE`'s timeout by the inventory's,
  and removes the app command, so the inventory is never built twice. `reports.cli
  inventory-workflow` still runs it by hand.
- **`instance-add` takes the database as `database_name`.** `db_name` and `database` are still
  accepted and written as `database_name`.
- **A restore entry's list of `{source_database, target_database}` is `database_mappings`.**
  `databases` is still read; `restore-add` accepts either and writes `database_mappings`.

### Fixed

- **A PostgreSQL, Oracle or container SQL Server restore reported its copy and nothing else.** In
  Telegram it read *started*, *copy started*, *copy finished*, *finished status=done*. It now
  announces every step: `COPY_*`, `METADATA_*`, `RESTORE_START` (what goes in, to which point),
  `RESTORE_DONE`, `VERIFY_START` / `VERIFY_DONE` (databases checked, unusable), `DELETE_START` /
  `DELETE_DONE`. END carries `restored=`, `verified=` and every `warning=`; a *done with a
  warning* used to drop the warning's text.
- **The same restores never replayed instance metadata**, and cleaned their staging inside the
  copy. The move onto `restore_by_id` left the logins, roles and Agent jobs behind, so a
  container SQL Server entry with `server_metadata` on came back without them. The staging
  cleanup ran before the restore, even for a drill that then failed. Metadata is replayed before
  the databases and after them, and the cleanup runs after a verify that passed, as on the SQL
  Server engine path.
- **`check-identifiers` never searched for a Telegram id or username.** It passed the data folder
  where the Telegram loaders take a file path, the read failed, and the failure was skipped as an
  optional file. A person's or chat's id also counts as a hit now, not an ordinary word to review.
- **`check-secret-literals` searches for the passphrase itself**, as typed and as base64, and both
  checks read `tests/`, `CHANGELOG.md` and `.github/` by default: they ship, and were not read.
- **`check-identifiers` reported Oracle Free's SID as an estate name.** An Oracle Free lab's
  record says `"sid": "FREE"`, and the scan matched every `FREE_SPACE` in the tree. It is a vendor
  constant now, like `FREEPDB1`.
- **A PostgreSQL statement longer than the connect timeout failed with `timed out`.** pg8000
  keeps its `timeout` on the socket for the connection's whole life. `db_connect` passed the
  connect timeout there, so `run-sql`, a SQL task or a metric query running past it (30 s for a
  task) died, reported as *could not reach* a reachable server. The connect keeps its deadline;
  after it the socket waits the statement timeout plus 60 s (`PG_SOCKET_GRACE_SECONDS`).
- **An Oracle SQL task with a PL/SQL block failed every run.** `run-sql` stripped the trailing `;`
  from every Oracle batch, because the SQL parser refuses it (ORA-00911). The PL/SQL parser requires
  the one after `END`, so `BEGIN DBMS_SESSION.SLEEP(60); END;` raised PLS-00103. A block (a
  `BEGIN` / `DECLARE`, or the `CREATE` of a stored unit) now keeps it
  (`lib.sql_text.oracle_statement`), and a SQL*Plus `/` line after it is not sent.
- **A scheduled backup, restore or SQL task could run twice.** `APP-BACKUP-RESTORE` and
  `APP-SQL_TASKS` are async: while one run works through its list, the next takes what it has not
  reached yet. The claim stops the two from overlapping, but it held only while a job was RUNNING.
  So a job the second run had already *finished* was run again when the first run got to it: a
  second full backup, a second restore, the same SQL twice on the same target. Before each claim, a
  scheduled run now checks whether another run started the job since it read its list
  (`store.job_run_started_since`, `store.sql_run_started_since`, one indexed probe each). `--force`
  still runs.
- **Every message of a point-in-time PostgreSQL, Oracle or container SQL Server restore said
  `restore_mode=LATEST`.** The moment appeared only inside RESTORE_START's text. Those messages now
  carry `restore_mode=POINT_IN_TIME`, `point_in_time=` and `point_in_time_utc=`, as the SQL Server
  SMB path's have.
- **A PostgreSQL restore could leave out the incremental of its own chain**, and a point in time
  chose its full by the wrong clock. The planner took the incrementals that finished after the
  full, and on a staging copy both are dated by the copy: staged in the same second, they tied,
  and the restore used the full alone. The moment was compared with the host's +07 clock, so an
  older full than necessary was chosen; on a host behind UTC it would have chosen one finishing
  after the moment. The chain is now read by name (`pg_combinebackup`'s rule) and dated by
  `backup_manifest`. The listing adds `finished_at_utc`, which a point in time is compared with.
- **A point-in-time restore to a moment before the newest full could not be staged.** The copy
  took only the newest chain: PostgreSQL's newest full and its incrementals, or RMAN's preview of
  the newest level 0. A moment before that full needs an older chain. With a moment, the whole
  directory is copied.
- **A refused unknown id was named `backup_id=<unknown>`** in its own refusal. It now carries the
  id that was typed.
- **`restore-add` wrote a script-driven restore that the loader then refused.** Only the SQL
  Server loader checked the entry, and it steps over a PostgreSQL, Oracle or container restore.
  One routed to a notify level no Telegram group defines was written, and from then on
  `list-restores` and every `restore-workflow` failed on it, for every entry on the node. The
  entry is now also loaded by the script-restore loader the scheduler runs.
- **A point-in-time restore did not reach its moment on any engine** (`/spbot_restore <id>
  "<moment>"`, drilled on the labs with marker rows either side of the moment):
  - SQL Server refused the statement - *Invalid value specified for STOPAT parameter* (Msg 3217) -
    because the moment went in as typed, offset and all; and the log chain stopped at the last log
    finished *before* the moment, leaving out the one that holds it.
  - PostgreSQL restored the right data and then paused at the target (the server's default), so the
    restore waited its 30 minutes and called it a failure.
  - Oracle's `TO_DATE` refused the offset, and `SET UNTIL TIME` outside a `RUN` block is refused
    (RMAN-03031).
  - Every backup listing compared stamps cut to 19 characters, the offset with them: a moment typed
    in +08:00 chose backups eight hours off, and PostgreSQL's finish times (`stat`, in the host's
    clock) were off by the host's offset.

  The moment is turned into the server's clock for all three, and the listings compare in one
  clock. The SQL Server chain takes the log holding the moment, PostgreSQL promotes at the target
  (a recovery that pauses anyway fails at once), and Oracle's duplicate runs in a `RUN` block.
- **An Oracle duplicate read another lab's backups.** RMAN takes `BACKUP LOCATION` as a prefix, so
  `.../ora_restore_from_249` also read `.../ora_restore_from_249ha`. Every gvenzl lab shares the
  image's DBID, so RMAN could not tell the two labs' pieces apart. The newest-point restore got away
  with it; the point-in-time one recovered through the other lab's logs (RMAN-06054). The location
  now ends in `/`. The duplicate also never resumes onto datafiles an earlier one left (`NORESUME`),
  and it clears the archived logs an earlier duplicate restored into `$ORACLE_HOME/dbs`.
- **The staging copy of a cross-machine restore only ever added files**, so a source rebuilt under
  the same name left its previous life's pieces beside the new ones. The copy now mirrors the
  source: a staged file the source no longer has is removed and counted in
  `removed_absent_at_source`. `include` limits what one run copies, not what the staging folder may
  keep.
- **`/spbot_create_db_docker` did not offer `oracle-xe`**, one of the provisioner's five engines.
  An Oracle XE lab built with a backup mount also came up NOARCHIVELOG, because the first-start
  script was Oracle Free's only. XE is now backup-ready as built.
- **The MySQL ha-lab could not be built**: `bitnami/mysql` has left Docker Hub, and the lab now
  runs `bitnamilegacy/mysql`. The image check had passed anyway, because it asked only about the
  single-mode image. It now asks about every image the compose file pulls and names the template
  whose image has gone.
- **A failed `create-db-docker` left the lab's password in the store.** The password is checked as
  storable before the build and stored only after the build succeeds. A `{worker_host}` placeholder
  nothing filled in is never recorded as a lab's host.
- **Every generated compose file pointed its reader at `sre/docker_db/models.py`**, gone since the
  spec moved to `lib`. `common.cli`'s usage listed `create-db-docker` out of its column.
- **`create-db-docker --install-docker --dry-run` installed Docker** before looking at the dry
  run. A dry run connects to nothing now.
- **`move-db-docker` reported 0 bytes transferred** for every move: it read a key the relay never
  answered.
- **`move-db-docker` could not move a lab named in capitals** - this estate's convention - and said
  *no containers belong to compose project*: compose lower-cases the project name, and the mover
  asked Docker for the name as typed.
- **Non-ASCII text in a `common.cli` answer arrived as U+FFFD on a Windows node** - an em dash in an
  error message, a Vietnamese name: the answer left in the ANSI code page and its one client reads
  UTF-8. It leaves as UTF-8 when stdout is a pipe.
- **`create-db-docker` called any registry failure "Image not found".** A rate limit or a timeout
  read *Image not found: postgres:18 … Valid tags include: 18*. *Not found* is now said only when docker
  says the tag is not there; otherwise the message quotes docker and says the registry did not answer.
- **The Oracle Data Guard lab's standby crash-looped after a restart.** It ran under the image's own
  start, which opens every database as a primary and failed its check on a physical standby - under
  `restart: unless-stopped`, for ever, after one host reboot. Once converted it now starts through its
  own script (mount, never open, clean shutdown on stop), and its healthcheck reports a mounted
  standby as healthy instead of *unhealthy* for the life of the lab.
- **The Oracle Data Guard lab's shipper never shipped.** It read the standby's `resetlogs_id` and
  archive-log format once, when it started - before the setup had converted the standby - and skipped
  every cycle after; the standby held only what the setup shipped itself. It reads them every cycle.
- **`install_docker = yes` prepared only one of the two folders a lab writes.** `/spbot_create_db_docker`
  then failed with *Cannot create /opt/db_ops/backup … Permission denied* for an SSH user with full
  sudo rights: the backup bind mount is created later over SFTP, without sudo, inside a
  `/opt/db_ops` the preparation had left root-owned. Both folders are now created and handed to the
  SSH user with sudo - the backup mount at its top only, so the engines keep the backups they wrote
  beneath it - and a preparation that fails says so instead of surfacing later.
- **A SQL task refused a database it could have run on, in the wrong words.** Before connecting it
  compared `database_name` case-sensitively with the instance record's `database_names` - a list no
  code writes - so `APPDB_PROD` failed against `APPDB_Prod` and a database created yesterday would have
  failed too, with *Target database not found in database-inventory.json*, a file never read, and a
  `service_name` SQL Server does not have in the path. It now looks up the instance only (ignoring
  case on SQL Server), connects, and asks the server when the database does not open: *does not
  exist ... did you mean*, *use the server's spelling*, *this login cannot open it*, a refused login,
  an unreachable instance. An instance the server lacks is named with the ones it has - and
  `sql-target-add` refuses it. SQL Server messages say `server/instance.database`, `master` when no
  database is named.
- **A restore whose `DBCC CHECKDB` failed was reported as a failed restore.** The database is
  `CHECK_FAILED`, and the message says *restored and recovered, but the integrity check failed*, with
  SQL Server's message numbers. It still fails the run and holds back retention cleanup.
- The shipped `sql_commands` example carried a `postgresql` task, which no runner executes.
- **A SQL Server restore with a database list was never verified.** The check after a restore read
  the wrong field names off each mapping, found no database to ask about, and reported the
  verification as not configured. It now asks the target about every mapped database under its
  target name. Expect restore runs that listed databases to start reporting their real state.
- **A PostgreSQL or MySQL instance registered with `db_name` connected to its label.**
  `instance-add` asked for `db_name`, but no connection read that field, so it fell back to
  `service_name`. It now writes `database_name`, the field the connection reads.
- **`check-objects` reported every instance registered by `instance-add` as broken**: the
  reference marked `metrics` as required, and `instance-add` deliberately writes none (collection
  follows `active`).
- **A Telegram group with no `status` field could run commands but was never sent an alert.**
  The permission check read it as active and the level routing as inactive; both now read it
  as active.
- **`check-secret` guessed a docker connection's engine from the ref's name**: it read a field
  the file did not carry.
- **The status report showed a stopped web host as enabled.** It read `enabled` on the app
  command, which only has `active`.
- **`check-objects` re-read the reference for every record** - 1 MB, parsed about 4,400 times once
  the reference described itself (50 s). It is loaded once per walk: 0.13 s. A nested object was
  also looked up in the default data dir's reference rather than the one being checked.

- **A SQL Server warning failed the run.** pyodbc raises a warning (SQLSTATE class `01`, e.g. 8153
  *Null value is eliminated by an aggregate*) out of `nextset()`, and every result loop treated it as
  an error: a task that had finished and committed was recorded `error`. A warning now ends the
  reading without failing: `run-sql` answers it in `warnings` and says so in its message, a SQL task
  is `done` at level `warning` with *SQL task done with a warning* in Telegram. Nothing after the
  warning can be read - the driver runs the rest of the batch on the server and hides even a later
  error - so a run under a wrapping transaction is rolled back with that reason instead of
  committed. A warning mixed with a real error is still an error.
- `run-sql`'s message named no database since 0.22.0 renamed its answer key; it says
  `<server_id>.<database_name>` again.

- **Every script-driven restore failed after its first step** (since 0.22.0): *restore-full
  failed: 'engine'*. The step ran, then its summary read an answer key 0.22.0 had renamed, and the
  database it had just restored was left RESTORING. It hit every SQL Server container, PostgreSQL and
  Oracle drill.
- **A new cross-machine restore failed its first run**: the target folder was read before it was
  created, and a folder that did not exist yet counted as unreadable.
- **A SQL Server drill onto another machine could not read what it had copied.** The pieces arrived
  `0660`, owned by the SSH user, and SQL Server reads them as its own user (*Operating system error
  5*, Msg 3201). They are opened to the engine after the copy.
- **An encrypted SQL Server backup could not be restored onto a machine without its certificate.**
  The backups were listed before the certificate import that listing needed (Msg 33111); it is now
  imported first.
- **An unreadable SQL Server backup was reported as "no databases found".** Only a file SQL Server
  calls not a backup (Msg 3241-3243) is skipped now; any other error names the file.
- **A SQL Server restore planned against port 1433 whatever the target.** It uses the target's own
  port from its inventory record, or `env.MSSQL_PORT`.
- **A failed restore did not say why** - *Restore workflow FAILED.* and *finished: error.*, with the
  reason only in the run row. The log line and the alert carry it now.
- **The SQL Server backup job could not write into a backup folder another user had made**, which
  is what a lab's bind mount is: it takes the folder over for the engine's user. It also relaxes its
  pieces to `a+rX` after every run, as the Oracle and PostgreSQL jobs always did, so a copy to
  another machine can read them.
- **A backup of a stopped container blamed missing tools** (*no sqlcmd found; install
  mssql-tools*). All five backup scripts now say the container is not running.
- **`create-db-docker --backup-mount` was dropped without a word by most templates.** PostgreSQL,
  Oracle and MySQL single now mount it, and so do the HA labs (below).
- **A PostgreSQL restore never replayed WAL.** The recovery configuration was written by a step run
  after the server had already started on the combined data, so every PostgreSQL drill stopped at
  its last base or incremental backup and reported verified - and left a `recovery.signal` in the
  running cluster. It is written before the first start now, the WAL is copied into the data volume
  with it, and the step waits for recovery to end. The file it wrote also ended in a stray `n`.
- **A PostgreSQL restore into a container needed the target running**, so a target that a failed
  restore had left crash-looping failed every later one (*Container ... is restarting*) until it was
  repaired by hand. The target is stopped first and the whole rebuild runs in throwaway containers
  from its image, with the backups mounted into them read-only rather than copied in; it is started
  again whether or not a step failed.
- **A PostgreSQL or Oracle restore onto a machine that logs in by password failed at planning**
  (*needs either a password or a key_filename*); the host block carries the password.
- **Restores used `sudo docker` outright**, which fails where sudo asks for a password. Plain
  `docker` is used when the user may run it, `sudo` only as the fallback; the PostgreSQL data swap
  runs in a throwaway container instead of as root on the host.
- **The PostgreSQL and Oracle backup jobs could not write into a folder another user had made**
  (a lab's bind mount); they take it over for the database's user, as the SQL Server job does.
- **An Oracle restore into a container without a backup mount could not find its backups**
  (*RMAN-05579: CONTROLFILE backup not found*): the pieces were copied to the host and never into
  the container. The duplicate stages them in first.
- **A lab built with a backup mount can be backed up as built, HA labs included.** Its backups go
  under `<mount>/<lab name>` (printed); PostgreSQL archives WAL there with WAL summaries on, Oracle
  turns ARCHIVELOG on at its first start, and the HA primaries mount the folder, so an HA lab can be
  the source of a cross-machine restore.
- **A newest backup SQL Server cannot read was passed over without a word.** The restore still uses
  the newest readable chain, and now finishes *done with a warning* naming the file.
- **A SQL task's credential was hidden by a `service_name` that did not match its group** - on SQL
  Server a label - with *Credential not found* and the credential in `users.json`. The label no
  longer narrows the lookup there, and a credential in an unmatched group is located in the message.
- **The first process of a day archived the live log as yesterday**, whatever day its lines were
  from; it is named after the day it was last written.
- **`/spbot_restore` could not run a PostgreSQL, Oracle or container SQL Server drill**:
  `restore-workflow` refused every script-driven entry and named a CLI command instead. It runs them,
  on demand, by the scheduler's own runner.
- **A restore could replay another cluster's WAL**: the copy took a same-named, same-size file for the
  same file, so after the source lab was rebuilt the previous cluster's WAL stayed and the target
  crash-looped (*WAL file is from different database system*) while the restore waited 30 minutes.
  Size and modification time are compared; the wait ends at once, with the log, when the container
  stops or restarts.
- **`/spbot_create_db_docker` asks whether the lab should be backup-ready** (`backup_ready`), which
  passes `--backup-mount`.
- **Seventeen shell scripts and templates shipped with CRLF line endings** (the working tree the
  export copies held them so); a test now fails a tree that would export one.
- **`run-sql` with `autocommit` still ran PostgreSQL inside a transaction** when a statement timeout
  was set - *CREATE DATABASE cannot run inside a transaction block* - and so did every metrics
  connection. Autocommit is switched on before the first statement.

## [0.21.0] - 2026-09-22

### Added

- **`time_window` now has a day-of-week dimension: `weekdays`.** An array of ISO weekdays, `1` =
  Monday to `7` = Sunday, on every object that carries a `time_window` — app commands, SQL targets,
  metrics, reports, backup jobs and restore entries. Absent means any day. `[]` means no day is
  permitted, so the record never runs on a schedule, which is different from `repeat_interval: -1`
  (still runnable on request) and from `active: false` (not listed at all). `0` is refused rather
  than ignored — it is the cron spelling of Sunday, and dropping it would silently leave `[]` — and
  a day listed twice is refused, because it is a set. `due-check` explains a weekday verdict the
  same way it explains an hour one.
  - **It gates due-ness, it does not grant it**: `repeat_interval` still decides *whether*, and
    `weekdays` decides *whether today*. Keep the interval well under a day on a weekday-gated
    record — with a multi-day interval the due moment walks, and the week it lands after the window
    has closed the record skips a whole cycle.

### Fixed

- **A weekly backup no longer reads two different clocks.** The PostgreSQL and Oracle backup
  scripts chose FULL by comparing `${DB_OPS_WEEKDAY:-$(date +%u)}` against `7`. db_ops only began
  passing `DB_OPS_WEEKDAY` in 0.20.0, so on any older node the fallback read the **container
  host's** clock — a different day from the one the daemon read the same job's `from_hour` on. On
  2026-09-19 the host said Saturday while the node's own +07 said Sunday, and five incrementals ran
  and failed on the night the weekly full was due. The level is now `BACKUP_LEVEL` in the job's
  `env` and the day is `time_window.weekdays`, read on the node's configured clock.
  - **If you have a PostgreSQL or Oracle backup entry, it needs the two-job shape** — one job
    pinning `BACKUP_LEVEL` to the full level with `weekdays: [7]`, one pinning the incremental level
    with `weekdays: [1,2,3,4,5,6]`. A job that pins no `BACKUP_LEVEL` now takes the **incremental**
    every day instead of a full on Sundays. SQL Server entries already pinned their level and are
    unaffected. See [08](docs/08_backup_restore_app.md).
- **A PostgreSQL incremental that cannot chain now takes a baseline instead of producing nothing.**
  An incremental needs WAL summaries covering its parent's LSN range, so a baseline taken before
  `summarize_wal` was enabled can never be chained onto — and the job failed every night, leaving
  `base/` without a single new directory. It now asks `show summarize_wal` before trying, and when
  the server refuses an incremental for want of summaries it takes a FULL in the same run. Every
  other failure is still fatal, and the script never creates the backup directory itself.
- **`APP-BACKUP-RESTORE` now ships at `repeat_interval: 30`**, down from 300. The app's interval is
  a floor under every job inside it, so at 300 a log backup declared at 900 s ran every ~1,200 s.
  An existing `app_commands.json` keeps whatever it says; `init` writes the new default.
- **The daemon now waits out a store restart that reaches it wrapped.** A store outage is classified
  from the SQLSTATE the server sends, and every *connect* failure arrives as a `PostgresStoreError`
  raised from the driver's exception — so the code was one link down the chain and was not read.
  `57P03` therefore counted as permanent and the daemon exited on its first attempt instead of
  waiting, which is the failure the tolerance was added to prevent. The chain is now walked, a
  wrapped permanent code is still permanent, and `"the database system is in recovery mode"` — the
  other wording of `57P03` — is recognised from its text as well.
- **A failing WAL archiver now says which fault it is.** The PostgreSQL WAL job correctly refuses to
  pass a broken archiver, but reported only that archiving had failed, and the three causes have
  three different fixes. It now prints the instance's `archive_command` and the archiver's stats
  window, and names the cause it can prove: a destination file that already exists — with its size,
  and a note when it is short of a full segment, because an `archive_command` of the form
  `test ! -f DEST && cp SRC DEST` exits non-zero when `DEST` is present and the server then retries
  the same segment for ever — an archive directory the database user cannot write, or neither, in
  which case it reports the free space and points at the server log.

- **A SQL task can no longer be registered on an engine no task can run on.** `sql-command-add` and
  `add-sql` accepted `postgresql` and `mysql` — valid engines for this estate, which metrics collect
  from and `backup_restore` backs up — and the task then failed at its first scheduled run with
  `Unsupported db_type`. On 2026-09-21 three such tasks sat in a scheduler for nine hours before the
  first one came due and said so. Both commands now refuse at registration and name what a task can
  run on; nothing about PostgreSQL or MySQL *instances* changes.
- **A PostgreSQL incremental no longer chains onto a backup of a different cluster.** `latest` was
  only the newest directory under `base/`; nothing checked it belonged to the database being backed
  up. A cluster that has been restored, re-`initdb`'d or replaced gets a new system identifier, and
  the server refuses the old parent **permanently** — no retry changes which cluster it came from, so
  the job answered `error` every night with `base/` untouched. The parent's `System-Identifier` is
  now read before the attempt and a mismatch takes a FULL baseline, as does a parent too old to carry
  one (manifests only record it from PostgreSQL 17). The server's own refusal is still recovered from
  as a second line of defence.
- **The engine scripts now say which database they mean.** A job's `env` takes `PG_PORT` for
  PostgreSQL and `ORACLE_SID` for Oracle; unset keeps today's behaviour in both cases. Neither could
  previously be expressed at all: `docker exec` does not carry the calling script's environment into
  the container, so an exported `PGPORT` would never have reached `psql`, and `rman target /` took
  whatever SID the container's login profile happened to export. On a host running one instance per
  container — every entry in this estate — both were right by luck; on two they were a coin toss that
  reported success against the wrong database.
- **An Oracle backup no longer rewrites a database's persistent RMAN configuration in silence.** The
  four `CONFIGURE` statements are stored in the controlfile, govern every later connection, and
  decide what the same script's `DELETE NOPROMPT OBSOLETE` removes — so on a database db_ops did not
  create, the first run replaced somebody else's retention and archivelog deletion policy with
  nothing in the output to say so. `SHOW ALL` now runs first, unconditionally, so the values as they
  were are in `stdout_tail`; `RMAN_CONFIGURE=skip` leaves the database's own configuration alone. The
  default stays `apply`, because changing a default silently is the same fault from the other side —
  and on the archivelog job `skip` is not free: its deletes name an age, but RMAN still consults the
  deletion policy, and under `TO NONE` the same statement removes a log that was never backed up.
- **`deploy --merge` now says which of the worker's records the master is about to overwrite.** The
  rule — master wins a shared key — has not changed, but it was applied without a word: a file whose
  only news was three discarded worker records printed `SAME`, and the upload that followed applied
  the master's copy anyway. Each one now prints one `OVERRIDE` line naming the fields, and an
  override that would switch off a task currently `active` on the worker says so in those words. It
  is reported, not counted: the master keeps what it had, so nothing is written on this side. A
  record that differs only in per-node state — `node_role` is resolved per node, so it differs on
  every node there has ever been — is not reported, because a report that is nine-tenths inevitable
  is one its reader learns to skip; the field is dropped from the line, never the line from the
  report. A file with overrides and nothing to carry back reads `KEPT`, not `SAME`.
- **A config drift report names the fields that differ**, not just the record. `sql_targets[29|1]
  changed` was the whole of what an operator chose `--on-config-drift keep` against `adopt` from, and
  those are opposite actions — `keep` is obviously right when the difference is a note the store has
  not been synced with, and obviously wrong when it is `active`.

- **A container is no longer required to back up PostgreSQL, Oracle or SQL Server on Linux.**
  `DOCKER_CONTAINER` is optional in all four engine scripts: set it for an engine inside a container,
  leave it unset for one installed on the host. Each script chooses one wrapper once and every command
  goes through it, so a step written for one path cannot work in testing and fail on the other. The
  host path still runs as the database OS user, because the container path's `-u` exists for a reason
  a host install shares: files owned by the wrong user cannot be read back by a restore. Before this,
  an engine installed directly on a machine could not be backed up at all, and the failure read
  `DOCKER_CONTAINER is not set` — which looks like a configuration mistake rather than a missing
  capability. Windows SQL Server was already covered by its own `.ps1` and is unaffected.
- **`app-command-set`: the first command that edits an app command.** `data/app_commands.json` holds
  the schedule of every app on the node and nothing could change it, so the only way was to open the
  file on whichever node you were looking at — which is how this estate, a test node and the shipped
  catalogue came to hold three different schedules for `APP-BACKUP-RESTORE` at once. A `time_window`
  edit is partial, so retuning an interval cannot drop a `weekdays` somebody else set; the answer names
  the old and new value of every field it touched; and it refuses to change what the command *is*
  (`command_text` names the module that runs, so that is a release) or to invent a tenth record.

### Changed

- **`data/shared_config_objects.json` describes three more records**: `backup_entry`, `backup_job` and
  `restore_entry`. They live in one file and are described separately because they share only two field
  names (`active`, `note`) and because the schedule sits in a different place in each — a backup entry
  has **no** `time_window` at all, since every one of its `jobs` carries its own, while a restore entry
  carries one directly. That asymmetry is real, follows from the engines rather than from untidiness,
  and was previously something you had to infer from an example.
- **`data/shared_config_objects.json` describes two more things**: `time_window.weekdays`, with an
  item range a program can evaluate rather than only prose, and **`app_command`** — all fourteen
  fields of an app command, two of them required, including which ones are read at run time and
  which are not. `check-objects` now validates app command records as well.

## [0.20.0] - 2026-09-20

### Fixed

- **The restore verification asked the wrong SQL Server**, connecting on port 1433 whatever the
  entry said. The port now comes from the entry's own `sql_instance`; a named instance is skipped
  with a reason rather than guessed at. Oracle and PostgreSQL are likewise told which instance and
  which cluster to look at.
- **A verification that reached nothing no longer reports a healthy state.** A PostgreSQL check that
  could not run was labelled `ACCEPTING`; it now reads `NO ANSWER`, and both engines report the
  error they were given rather than an empty line.
- **A slow SQL task or backup no longer holds up every other one.** `APP-SQL_TASKS` and
  `APP-BACKUP-RESTORE` now declare `"run_mode": "async"`, so the daemon starts them when they are
  due instead of waiting for the run in flight to finish. A 22-minute task used to stop every other
  SQL task for 22 minutes. `"max_parallel"` caps how many run at once (4 by default); a command that
  says nothing keeps the old behaviour of one at a time.
- **The same unit of work can no longer be started twice.** One `running` row per task-and-target,
  per backup job and per restore is now enforced by the store itself, so two overlapping runs cannot
  both decide the work is free — the second is told it is taken and moves on. This is what makes
  running an app twice at once safe, and it also closes a way two nodes could double a production
  SQL task or start a second restore into a database mid-restore.
- **A run whose process is still alive is no longer timed out and restarted.** Every run records the
  process that owns it; a task that legitimately outruns its timeout keeps its place instead of
  having a second copy started on top of it. A run whose process is gone is released at once.
- **A restore checks that the files fit before it copies them.** The preflight adds up the backup
  files it is about to move, reads the free space where they are going, and refuses unless
  `free >= size x 1.5`. Per entry, `"space_check": {"factor": 2.0}` asks for more room — use it when
  the restored database will live on the same filesystem as the staged files — and
  `{"enabled": false}` turns the check off. A restore whose numbers cannot be read is refused too,
  and says which number was missing. Before this, a drill could fill the disk it was restoring onto
  and take everything else on that host with it.
- **The daemon survives a store restart.** A failure the store reports as transient — a connection
  lost, a server starting up or shutting down, too many connections — is now waited out with a
  backoff for up to ten minutes per outage instead of ending the process. A two-second restart of
  the database holding the runtime store used to stop scheduling entirely, with nothing to restart
  it and no sign but the absence of rows. Any other error, and an outage longer than the budget,
  still exit as before.
- **A command's answer is no longer lost to one byte of noise.** When a child process printed a
  character the machine's code page produced but UTF-8 cannot read, the reply was discarded and the
  caller was told the command *"exited 0 without a JSON response"* — while the command had in fact
  succeeded. Output is now read tolerantly; requests are still written strictly.

- **SQL tasks and reports measured `repeat_interval` from the wrong instant.** A SQL target with
  `repeat_interval: 300` whose task took 240 s ran every 540 s. Every scheduler now measures from
  the previous run's start.
- **A SQL task file export carries every row, not the first result set.** Result sets with the same
  columns are merged into one table; `txt`, `csv` and `json` also keep sets of different shapes.
- **`plain` output shows one table per shape, not one per batch.**
- **A batched SQL task no longer sends a progress message per batch.** It reports start and finish;
  `"progress_per_file": true` on the command restores per-batch messages.
- **`/spbot_list_my_commands` shows an uploaded file by name**, not its base64 content, and
  shortens any other very long argument.
- **SQL Server backup names mark their UTC stamp** (`..._20260919_072256Z.bak`). Restores accept
  names with and without the `Z`.
- **The PostgreSQL and Oracle weekly full follows the node's configured timezone**, not the host's
  clock.

### Added

- **`describe-object`, `due-check`, `check-objects`, `check-references`** — what a shared
  configuration block means, whether a job is due and why, whether a node's configuration obeys the
  reference, and whether its pointers land anywhere.
- **`data/shared_config_objects.json` and `data/config_references.json`**, written by `init`.
- **`run_mode` on an app command** — `sync` (the default) or `async` with a `max_parallel` cap, so
  a long job no longer holds up the rest. `APP-SQL_TASKS` and `APP-BACKUP-RESTORE` ship `async`.
- **`space_check` on a restore entry** — `{"factor": 2.0}` asks for double the room; on at 1.5 when
  the entry says nothing.
- **`worker-run --on-host` and `--sudo`** — run a command on the worker host rather than in the
  container, with the same login.

### Changed

- **`output` is a shared configuration object.** `sql-target-add` accepts the block itself as well
  as the flat `output` / `output_chat` form.
- **A SQL target may run slightly more often**, because its interval no longer includes the task's
  own duration.
- **The runtime store gained two columns and two unique indexes**, so a unit of work is claimed
  rather than checked. The first start upgrades it and reconciles any key already holding two
  `running` rows — the newest stays, older ones are closed as `timeout`, nothing is deleted.

## [0.19.0] - 2026-09-19

### Fixed

- **A restore's verdict is computed from the state of the databases it touched**, not asserted by
  the step that ran last. The workflow ends with a `verify` phase that queries each database; one
  that will not answer is a failed restore.
- **`restore-add` accepts a script-driven restore.** It required `source` and `target`, which the
  script-driven shape does not carry.
- **`user-level --pending` grants a level to a username the node has not seen yet**, adopted on that
  user's first message. Without the flag an unknown name is still refused.
- **Reports print addresses in full.** A render-time abbreviation produced forms that appeared in no
  configuration file and passed every identifier check.
- **Multi-part reports survive Telegram's rate limit.** Parts are paced, HTTP 429 is waited out, and
  a refused message returns to the queue.
- **A forced SQL task run started without a terminal no longer waits indefinitely.** The prompt
  gives up after 120 seconds; pass `--assume-yes` for an unattended run.
- **`check-credentials` and `check-secret` see a legacy-Oracle target's secrets.** Both reported
  clean while every collection for that target failed.
- **`telegram route` refuses a JSON request** instead of answering with an empty chat id.
- **An SLA run with no policy reports `NOT_CONFIGURED`**, not `PASSED`.
- **`metrics collect` states why it collected nothing.**
- **The command menu order reaches a new install.**
- **`sla validate --notify` works on a node that has never queued a message.**
- **`/spbot_list_sql_runs` answers when called with no arguments.** A default declared on the
  parameter is now applied.
- **A SQL task whose Python step is killed reports what is known:** the task ran no SQL of its own,
  and work the program had already done is not rolled back.
- **A task runs on its own schedule.** The daemon scan and the SQL task app interval are one second,
  so neither rounds a task's `repeat_interval` up.

### Added

- **`db-status`** — whether a server is up and usable, at three depths: instance, database, schema;
  SQL Server, PostgreSQL and Oracle.
- **`sql-command-add` and `sql-target-add`** — register a SQL task in two calls: what it runs, and
  where. They cover what `add-sql` cannot express: a task fed by a Python program, an array or
  folder of scripts, and additional targets for an existing task. The SQL is given as a file path or
  as text, in which case the file is written for you.
- **`{target_server_id}` / `{target_database}` in a Python step's arguments**, substituted per
  target, so one command serves several targets.
- **`telegram group-add`** — register a group that has never posted.
- **`timezone`**, and `self-status` names the clock the node runs on.
- **A shipped seed for every catalogued configuration file.**

### Changed

- **A restore that previously reported success may now report failure.** The verdict is computed
  rather than asserted, so restores that were failing silently now say so.


## [0.17.0] - 2026-09-16

### Added

- **`db timezone --set <ZONE>`: set the clock a node's schedules are read against.** Every
  `time_window.from_hour` is a *local* hour read against `config.json`'s `timezone`, and `init`
  ships `UTC` — so a node built beside an estate on another zone keeps a different set of hours and
  neither file says so. Until now the only way to change it was to edit `config.json`. The name is
  validated before it is written: an unknown zone does not fail loudly, it falls back.

- **`reports use-base-url`: point a node at the address its own pages are published on.** Fixed before release: the
  handler asked for `args` and then for a `config.data_dir`, neither of which the reports CLI
  provides, so the command failed on every invocation until it was run through its own front door. The third
  field of its shape, after `store_config.json` (`db use-store`, v0.14.0) and `bot_telegram.json`
  (`telegram use-bot`). All three travel inside a config bundle, so an imported node carries the
  source's identity and says nothing about it; this one made a node publish links to the machine it
  was cloned from, which `self-status` reported correctly and nobody could act on. Takes a URL,
  `--this-node`, or `--clear` to fall back to the derived answer. A URL with no scheme is refused —
  a browser reads it as a relative path — and `--this-node` is refused in a container, where the
  address the node can see is not the one anyone reaches it on.

- **`telegram use-bot --ref <SECRET_REF>`: point a node at its own Telegram bot, the mirror of
  `db use-store`.** `data/bot_telegram.json` travels inside a config bundle exactly as
  `store_config.json` does, so an imported node arrives on the bot the bundle came from — the
  estate's own — and says nothing about it. Two pollers on one token is 4,735 calls refused with
  HTTP 409, measured in the 0.16.0 cycle. The id and username are read back from `getMe` rather
  than typed, a ref the secret store does not hold is refused by name, and the bot that is active
  is printed afterwards — because the mistake being prevented is *believing* the node is on the
  other one.

### Fixed

- **A background command that was killed is no longer reported as having succeeded.** A restore
  dispatched from Telegram was interrupted fourteen minutes into a 33 GB copy; it wrote no
  completion record, no success marker and no exit code, and the chat was told *"Restore workflow
  completed"*. Nothing had run. The poller read "the process is gone and left no evidence" as
  success, because reading it as failure had previously reported a *successful* SQL task as
  `Exit code: 1`. Both are wrong, and which one applies depends on the command: one that declares
  how it reports completion — a store record to look up, or a marker in its output — and then
  leaves nothing at all is reported as **failed**, with `unknown` where the exit code goes rather
  than a fabricated `1`. A command that declares neither is still given the benefit of the doubt.

- **A `--once` pass no longer deletes a running daemon's state file.** One tool root can hold both
  at a time, and they shared `runtime/daemon_state.json`: the single pass recorded over it on the
  way in and removed it on the way out, so `self-status` reported *"not running (no daemon has
  started in this tool root)"* for a daemon that had been up 15 hours — the one line an operator
  reads to confirm it is running. A single pass now claims nothing, and a clean stop removes
  only the record it wrote.

- **`self-status` reports the running daemon's `node_role`, not its own process's.** The role is
  an environment variable and `self-status` runs in its own environment — a shell where nobody
  exported it, or over Telegram in a worker the daemon spawned — so a node whose daemon came up as
  `worker` answered `master (default)` on the one line anyone reads to find out. With no daemon
  running, this process's environment is still all there is.

- **A failing `webhost` command now says why.** Its catch-all passed the wrong keyword to the
  logger, so the error handler for *every* webhost command raised `TypeError: log_function_error()
  got an unexpected keyword argument 'error'` — and took the line that prints the real error with
  it. `user-add` answered that instead of "password must be at least 8 characters."

- **`remote-credential-add` no longer switches a deliberately disabled `cmd_access` back on.**
  `enabled: false` is a decision — two Windows hosts on this estate carry it because their WinRM
  auth fails — and re-registering the host's OS login turned it back on, putting failing collectors
  into every scan. An existing block keeps its `enabled`; a block written for the first time still
  defaults to on, and `enabled: true` in the request still turns one back on.

- **`instance-add` refuses a PostgreSQL or MySQL target that names no database.** Every engine but
  SQL Server connects to a named database, and a record without one falls back to `service_name`,
  then to the `server_id` — so a *label* is handed to the server as a database. The failure then
  quotes the label (`database "ACME-STORE" does not exist`) and reads as a missing database rather
  than a field used for the wrong thing. The refusal names both choices: the engine's neutral
  database to monitor the instance, or the database the target is actually about. Oracle is exempt
  — it connects by service — and SQL Server does not take one.

- **`self-status` names the PostgreSQL schema, not just the database.** On PostgreSQL the database
  is routinely shared and the schema is what tells two stores apart — one estate can keep its
  production store and every test node in a single database. The line stopped at the database name,
  so the one command you read to find out which store a node is on could not answer it.

- **A failed backup says why, wherever you read it.** The script's own words were written to the
  run row's `error_text` and the logged message carried only `finished: error (exit 1)` — so
  `backup.log`, the Telegram alert and the workflow summary all reported a failure with no cause,
  and the cause was reachable only by writing SQL against the store. The reason is now appended to
  the message on a failure, trimmed to 300 characters.

- **`backup-add` and `restore-add` now require a `notify` object.** Without one an entry still
  notifies — the loader defaults it on, deliberately — but to the neutral `logging` and `error`
  levels instead of the entry's own chat. From whichever group you are watching that reads as
  nothing having been sent: a restore posted *"Restore workflow started"* into the Logs group while
  the operator watched the Restore group and reported silence. Both messages had been queued and
  delivered. One `notify` on a backup entry covers all of its jobs.

- **`backup-add` and `restore-add` now require a `time_window`.** Without one a unit of work is
  not "unscheduled" — backup jobs and restore entries share one due check, which gives a job
  carrying no window an always-open window and a 300-second repeat. A restore entry registered
  without one restored a 183 GB database for 36 minutes, finished, and started again four seconds
  later, costing the target about 5 GB of free disk per cycle while every run reported success.
  The refusal prints a window that can be pasted in and names the job that lacks one.
- **`backup-add` and `restore-add` document every field a live entry uses.** `server_metadata` and
  a job's plain `env` on a backup, `notify` on a restore: all three were accepted and passed
  through, and named nowhere, so the only way to find them was to read an entry somebody else had
  hand-written.

- **Staging cleanup no longer deletes one database's full backup because another database has a
  newer one.** A restore staging directory holds every database copied from a source, and the
  "never delete the newest full" rule was applied once across the whole directory instead of once
  per database. On a tree holding four, a 5 MB full taken 89 seconds after a 30 GB one retired the
  30 GB one - while the logs that restore from it were correctly kept, leaving a chain with no
  anchor and the next restore with nothing to start from. The rule is per chain now, keyed on the
  database folder so that fulls and logs staged under different roots still count as one. A chain
  with no full of its own still answers to the newest full staged, as every chain did before.

- **Restarting the daemon no longer starts a second copy of a job that is still running.** A
  command's repeat interval is normally far shorter than its worst-case run - the backup/restore
  workflow repeats every 5 minutes with a 2-hour timeout, because almost every cycle finds nothing
  to do and the rare real one runs for an hour. The due rule tested the interval before the stored
  status, so once the interval elapsed the `running` row below it was unreachable and the command
  counted as due. Within one daemon that is caught in memory; across a restart nothing caught it.
  Measured 2026-09-14: a daemon started 47 minutes into a restore began a second restore of the
  same database onto the same target, one second after logging `startup.running_within_timeout`
  about the row it went on to ignore. A run still inside its timeout now blocks the next one,
  which is what the daemon's own "not due" message had been reporting all along.

- **`instance-add` keeps a server's other database logins.** It replaced the whole
  `database_credentials` group to add one, so a server carrying two — a monitor account beside a
  DBA account, or `sys` beside an application user — lost the other. Four of this estate's servers
  do. Visible only later, as a target resolving to the wrong login or to none. The credential is
  replaced inside its group now, which is the rule `remote-credential-add` was written with.
- **A node no longer reports a Telegram bot it is not authenticating as.** Three faults, and the
  third is the dangerous one: `init` wrote a `telegram_config.json` naming `data/bot_telegram.json`
  — a file `init` did not create; it also pre-filled `telegram_bot_token_ref`, and a value *there*
  wins over the bot file, so a fresh node could not change its bot by editing the file its own
  notes point at; and the id and username were still read from the bot file, so the node announced
  one bot while using another's token. `init` names the bot file and no longer pins the ref, and
  the identity is taken from `bot_telegram.json` only when that file also supplied the token.

- **`init` writes every shipped default, including the new backup policy.** `PACKAGED_DEFAULTS`
  said what ships and `_files()` repeated the list by hand, so adding `data/backup_policy.json` to
  the map was not enough: the file was in the wheel, `packaged_default()` found it, and a fresh
  root still came up without it - the exact hole this release exists to close, reproduced by the
  release itself. `_files()` derives its list from the map now, so a new catalogue file needs one
  entry rather than two. Found by standing a node up, not by the suite: the test that covered it
  asserted the map and never ran `init`. It now runs `init` and reads the file off disk.
- **`check-credentials` stops demanding a database login from a machine with no database.** A
  `db_type: "host"` record has no database and therefore no database credential, but the skip was
  written `if not target.db_type` - right while a host carried `null`, and silently wrong the day
  those records were normalised to `"host"`, the spelling this release documents. Four correct
  entries were reported as problems on the one command whose whole value is being believed.
  `HOST_ONLY_DB_TYPE` and `is_host_only()` now live in `db_ops.lib.sql_access`, which owns the
  `db_type` vocabulary, and accept both spellings.

- **An incomplete restore entry names its missing fields.** `parse_restore_config` read six fields
  by subscript, so the first one absent was the entire error text — `'prod_backup_share'`, an
  internal key naming neither the entry, nor the file, nor anything the operator had written. It
  now names every missing field at once, the entry they belong to, and the spelling each is
  actually written in (`source.backup_share`, `target.vm_import_linux_path`). The 2026-09-11 fix
  covered "nothing is configured"; this is the other half, for an entry that exists and is
  incomplete.
- **A missing backup policy no longer reads as "every database compliant".** With no
  `data/backup_policy.json`, no backup type was required of anything, so every database graded OK
  and the fleet page printed `15/15 DB within policy` over a server whose newest transaction-log
  backup was 168 days old — no Priority Attention card, a green *Compliant* badge, and nothing
  anywhere naming the missing file. Two nodes holding identical backup evidence disagreed about
  nine servers because of it. Without a policy the reports now grade nothing: the verdict is
  `UNKNOWN`, the coverage cell reads **No policy configured**, the badge is grey *Unverified*, and
  Priority Attention carries a card naming the file and the databases left unjudged. **On upgrade:**
  a node that really has no policy file will show that card and lose its (meaningless) green backup
  column until the file is restored — which is the point. A policy that deliberately requires
  nothing is unaffected; it is a configured policy and still grades OK.

### Added

- **`db-ops init` writes `data/backup_policy.json`.** It ships with the common plan already in
  force — daily FULL, and a LOG backup every couple of hours on any database in full or bulk-logged
  recovery — so a fresh install grades backups from its first collection instead of waiting to be
  told how. Overrides ship empty; `data/backup_policy.example.json` has three worked ones.
- **`sync-config` names every catalogued file the node does not have.** The count was already in
  `totals["missing"]` and printed nowhere, so a node missing ten catalogued config files reported
  `ok` on every sync.
- **A SQL task can be fed by a Python program: `input_type`.** `script_type` goes on saying what
  the SQL half is (`single`/`array`/`folder`); the new `input_type` says where the task's rows come
  from — `none` (the default, and every task that came before) or `python`. A python task runs one
  program from `assets/tasks/python/`, reads a single JSON document from its stdout, and hands the
  rows to its own SQL as a bound `nvarchar(max)` parameter, in batches, through the same executor
  and the same credential as every other task. So data that starts in an HTTP API or a vendor
  export no longer needs a script outside db_ops holding its own copy of the connection. The
  program's contract is four lines and they are all refusals: one JSON object on stdout, rows as a
  list under `rows_path`, diagnostics on stderr, and exit 0 or the task fails having sent nothing
  (`accept_exit_codes` opts into a partial pull deliberately). See
  `assets/tasks/python/README.md` and `docs/05_sql_task_runner.md`.
- **`final_script_paths` on a SQL task: SQL that runs once, after the last batch.** A step that
  rolls the loaded rows onward is not a row consumer, and listed in `script_paths` it would run
  once per batch — 29 times for a window that arrives in 29 batches. It is handed no payload, for
  the same reason. A task without it plans exactly as before.
- **`backup-add` and `restore-add`** register one entry in `data/restore_config.json` from a JSON
  request object, the way `instance-add` registers a database. Both were hand-edits, and one field
  — `env_secrets` — could previously only be filled by writing a password into
  `secrets/secret_text.json` in the clear; give `env_secret_values` (or `password` /
  `sql_password` inside a restore's `source`/`target`) and it is encrypted straight into the store.
  What is written is loaded back through the app's own loader before it is committed, so a refusal
  names the field the scheduled run would have failed on. `cleanup_retention` stays required, in
  seconds, on every job and every restore entry.
- **`remote-credential-add`** registers a host's OS login — the `users.json` `remote_credentials`
  entry, the secret behind it, and the `cmd_access` block on the instance that names it. There was
  no command for this at all, so a machine registered with `instance-add` was a target nothing
  could log in to. It refuses the three ways the hand-edit went wrong: `method: "local"` pointed at
  a remote host (it reports the container's own CPU under that host's name), an ssh password with
  no explicit `auth_type` (it defaults to `key`, and a key-auth block resolves to no credential, so
  the password is never read), and `platform` written inside `cmd_access` instead of on the record.
- **`db use-store` can name the PostgreSQL store, not only select the backend.** `--host`,
  `--port`, `--database`, `--schema`, `--username` and `--password-ref` re-point the node in the
  same call, so moving one onto a store of its own — its own schema on the shared server, say — is
  a command rather than the hand-edit of `data/store_config.json` that `use-store` exists to
  remove. `connection_string` is rebuilt whenever the target moves: it is authoritative when
  non-empty, so changing `schema` beside it used to alter the breakdown and nothing about where
  the node actually wrote.
- **`instance-add` documents `db_type: "host"`**, the machine-with-no-database record. It always
  accepted one; its help listed only the four engines, so every host-only record in this estate had
  been hand-edited. It also parses `--key` / `--key-base64`, which its usage line has advertised
  since the day it was written and which were accepted and silently ignored — an operator who
  passed one got "no passphrase is available" while looking straight at it. A database `username`
  on a host record is now refused, naming `remote-credential-add` instead.

## [0.16.0] - 2026-09-12

### Changed

- **A registered instance now appears on the fleet inventory report.** The canonical
  `database-inventory.json` is seeded once and the health overlay only updates servers it already
  lists, so anything registered afterwards never reached the page — collected, alerted on, given its
  own index report, and invisible on the fleet. Every run now adopts what is missing. **On upgrade:**
  a server you deleted from that file by hand returns if it is still registered and enabled. Say it
  with `reports: {"enabled": false}` on the instance instead — that keeps it collected and off the
  page; `enabled: false` stops both.
- **`examples/` ships with the release**, to the public repository and to the PyPI sdist, and now
  contains `examples/showcase/` — real report pages from a live estate with every identifier
  replaced. A GitHub Pages workflow publishes them, because no HTML in a repository is viewable by
  clicking it.

### Added

- **The per-server index report is linked from every page's head.** It was being published nightly
  with nothing pointing at it.

### Fixed

- **`build-showcase` output renders.** It now copies the data a page fetches (`server-metrics.html`
  is one page and one `fetch` per server), never rewrites the page's own stylesheet, JSON keys or
  template-literal expressions, and reads object names from both shapes a report writes them in —
  so schema, table, index and stored-procedure names are scrubbed rather than shipped. Pages are
  named after the moment they state, in UTC.
- **`check-identifiers` no longer reads a measurement as a machine.** Its two-octet shorthand tier
  matched `"sharePct": 0.2` and `version: 2.53.1.0`; it now requires the shorthand to touch a name.
  A request's `allow` list applies to the address backstop as well.

### Changed

- **Every app command in the default schedule now ships active** — the inventory workflow, the web
  host and backup/restore included, which shipped off. On a root where `init` has run and nothing
  else, all of them finish `status=done` and report what is missing (no instance, no token, no
  restore entry) instead of failing. **On upgrade:** your existing `data/app_commands.json` is not
  touched; this changes what a new `init` writes. Set `active: false` for anything you do not want.
- **Telegram alerts are on by default for a new install** (`enabled: true` in the
  `telegram_config.json` that `init` writes). Storing the bot token and giving a group its level is
  all it takes; nothing is sent before both exist. **On upgrade:** your existing file is not touched.
- **`daemon --once` skips long-running services** (`timeout: 0`, e.g. the web host) and logs
  `long_running_service_not_run_by_once`. It used to start the web host and then wait for it for
  ever, which is why that command shipped off.

### Added

- **`db-ops telegram user-level --user @someone --level 100`** — give a Telegram user the level
  that lets them run commands. Every sender is recorded at level 0, so until now a new node answered
  its own operator with "Permission denied" and the only fix was editing `telegram_users.json`.
- **`python -m db_ops.common.cli secret-set -`** — store one secret (a bot token, an API key)
  encrypted, with no plaintext file on the way. The request is read from **stdin only**. Its answer
  warns when a later `encrypt-secret` would drop the secret — which, on a fresh node, it would.
- **Transaction log by database and Top queries on `server-metrics.html`**, under Workload. Two new
  SQL Server metrics, `PERFORMANCE_LOG_BY_DATABASE` (every 15 min) and `PERFORMANCE_TOP_QUERIES`
  (every 30 min): which database generates the log and how long its log writes take, and the top 20
  statements by CPU, duration, logical reads, physical reads, logical writes and executions per
  window. Read from `metric_results` only. No percentiles — the engine keeps none, and the page says
  so rather than approximating one.

### Fixed

- **Backup/restore on an install with nothing configured** failed every cycle with the error text
  `'prod_backup_share'`. It now reports "nothing configured" and finishes `done`; `init` writes an
  empty `data/restore_config.json`; `restores: []` is accepted. A manual restore command with no
  entry now says so instead of `IndexError`.
- **`workflow` raised `TypeError: run_workflow() got an unexpected keyword argument
  'delete_retention'`** on builds between the retention rename and its fix.
- **The inventory workflow seeds its canonical inventory from `data/db_instances.json`** on a node
  that has none, instead of failing with `[Errno 2]` and leaving `database-inventory.html` a 404. An
  explicit `--inventory` path that does not exist is still an error.

## [0.14.0] - 2026-09-09

### Added

- **Back, Skip and Cancel on every prompt of every Telegram conversation**, as buttons and as
  typed words. Cancel ends the run and executes nothing; Back re-asks the previous question; Skip
  is offered only where a step allows it. Before this, a value mistyped at step 4 of a 14-step
  workflow could not be taken back — the only remedies were finishing a wrong run or abandoning it.
- **Branching steps.** `"ask_when": {"parameter": "remote_auth", "equals": "secret_ref"}` asks a
  step only when an earlier answer matches, so a command stops asking questions that do not apply.
  A real `/spbot_create_db_docker` run had two of three credential answers as `-` for a credential
  that had already been given.
- A step schema that can grow: `input_type`, `options` (`{label, value}`), `allow_text_input`,
  `allow_skip`, `skip_value`. Buttons are a reply keyboard, so tapping and typing arrive the same
  way and nothing about update intake changed.
- **`telegram_workflow_steps`** — one row per asked step: the prompt shown, the options offered,
  the answer (masked for `secret` steps), how it arrived, and `status='active'` on exactly one row
  per run. Answers given inline are recorded too, as `answer_kind='inline'`. **Store schema 3 → 4;
  the table is created on first start and there is no migration to run.**
- **`db-ops db use-store sqlite|postgresql`** — points a node at its own store instead of the one
  its config bundle came from. `store_config.json` travels inside a bundle, so an import
  faithfully aims a machine that has never run at the shared production store, and every procedure
  that stood up a node used to end with "now edit that file by hand". The section not switched to
  is kept, so the way back is not a retyping exercise.
- **`db-ops control pull-node-config --from <node>/data [--merge-secrets]`** — carries back what a
  **local** node created. The bot and console create config on whichever node runs the estate;
  `deploy --merge` and `worker-pull-data-config` did this for the worker container over SSH, and a
  node that is a directory on a PC had no command. Same merge rules, because it is the same
  function underneath. `store_config.json` and `telegram_config.json` are never carried back: a
  node's store declaration and its `getUpdates` cursor are per-node state.

### Fixed

- **A pasted SQL body was silently mangled.** A `consume_rest` tail was rebuilt by joining shlex
  tokens with single spaces, so `WHERE name = 'Tan Thanh'` reached the CLI as
  `WHERE name = Tan Thanh`, and a multi-line paste was flattened until a `--` comment swallowed the
  rest of the statement. Both were silent: the query stayed valid, it was simply not the one
  anybody wrote. The tail is now taken from the raw message, byte for byte.
- **Answers are validated at the step they are given.** Validation ran only when the command
  finally executed, so a value mistyped at step 2 of a 14-step workflow was reported after step 14.

### Changed / breaking

- **`/spbot_create_db_docker` parameters were renumbered** (`remote_auth` inserted at position 10).
  Inline invocations written before this release have their later arguments shifted by one and
  **must be retyped**. Answering the prompts is unaffected, and `command_argv` / `conditional_args`
  are unchanged because everything reads parameters by name.
- Every conversation prompt gains a trailing line naming the words that work
  (`Type back / cancel at any point.`) and a keyboard. Any integration asserting the exact text of
  a prompt will see the extra line; the question itself is unchanged.
- An **optional** step is now asked when it declares `allow_skip: true`. Optional steps that do not
  declare it keep the previous behaviour of never being prompted for.

## [0.12.0] - 2026-09-08

### Added

- **`dbabrain` is now a console script**, alongside `db-ops`. The distribution is named `dbabrain`
  and the only command was `db-ops`, so the first thing anyone typed after `pip install dbabrain`
  answered "command not found" — in a directory holding `.venv` and nothing else.
- **`dbabrain guide`** prints the getting-started document and writes nothing. Until `init` runs
  there is no file to read.
- **A first-run banner.** Bare, with no tool root, the toolkit now says what to type instead of
  listing twelve apps that cannot run yet. `--help` and the in-a-tool-root listing are unchanged.
- The **sdist** now carries `docs/`, `README.md` and `CHANGELOG.md`.

### Fixed

- `AGENTS.md` — the only document a fresh install ships — claimed backup/restore validation, SLA
  checks, scheduled SQL, reports, provisioning and the web console were "not in this release". All
  six are in `db-ops --help`. It also never mentioned `timezone` (required since 0.10.0, and its
  absence silently moves schedules to UTC) or the daemon and `DB_OPS_NODE_ROLE=worker`, without
  which a reader ends with one manual collection and nothing scheduled.

### Known not to work

- The **wheel** still ships no `docs/`; `pip install` uses the wheel, so the full reference is a
  URL. Relocating `docs/` under the package is a refactor of 121 references, not a packaging line.

## [0.11.0] - 2026-09-08

### Added

- **`db-ops db backfill-from-sqlite --source-schema <name>`** — carry a stand-in node's history home
  when its store was another **schema** on this PostgreSQL server rather than a local SQLite file.
  `--source` (a file) and `--source-schema` are mutually exclusive and one is required.

  One command rather than two on purpose: the difficult part is not reading rows, it is that ids
  cannot be carried (every key is an identity column) so each child link must be rewritten through
  its parent's new mapping. `sla_results.sla_run_id` would be *rejected*; `metric_results.run_id`
  is unenforced and would be silently **wrong**. Those rules live once, in `TABLES`.

  The source session is opened `READ ONLY` — a bug that wrote to the store being read would be
  writing to one somebody is still deciding whether to trust.

  Refused, each verified: carrying a schema into itself (the watermark would come from the table
  being written), naming both sources, naming neither, and naming a schema when the destination is
  SQLite.

### Changed

Nothing an existing tool root must do. `--source <path>` behaves exactly as before. The command
keeps its `backfill-from-sqlite` name: `backfill-from-store` reads better, but the old name is in
the move procedure, in two dated run records and in operators' notes.

## [0.10.1] - 2026-09-07

### Fixed

- **`Run:` in every metrics report carried no offset** — `Run: 2026-09-07 14:44:14` reached Telegram
  as a wall clock on no named clock at all, which is exactly the defect 0.10.0 was written to
  remove. It borrowed the *column* renderer, which may omit the offset because a table header states
  it once. Found by reading the messages an upgraded node actually sent; no test asserted on it.
- The **maintenance-window refusal** named an hour and not a clock. When a tool blocks a host
  restart on the grounds of what time it is, that is the one thing it must not be vague about.
- The **restore-drill failure reason** rendered UTC while everything beside it rendered the
  operator's clock — correct, but costing the reader a conversion mid-CRITICAL.
- **`sre move-db-docker`** used the host clock for the moved image's tag and the manifest's
  `created_at`. Not scheduled and not silently wrong, but disagreeing with every other timestamp.

### Added

- A guard for the class, not the instance: `strftime("%Y-%m-%d %H:%M…")` with no offset now fails
  the suite. Two files are allowed, each named with its reason — the column renderer, and the
  retention cutoff, which is a comparison key rather than a rendered time.

## [0.10.0] - 2026-09-07

### Changed

- **BREAKING — add `timezone` to `config.json` before upgrading.** `time_window` bounds
  (`from_hour`, `to_hour`, `from_day`, ...) were evaluated in the node's *local* time; they are now
  evaluated in the timezone declared in `config.json`. The field defaults to `UTC` when absent, with
  a warning, and the tool still starts — so a root on a host set to `+07` that upgrades without
  adding it moves every hour-bounded schedule seven hours, silently. Set it to the zone the host was
  already on and behaviour is identical to 0.9.1. An IANA name (`Asia/Ho_Chi_Minh`) or a fixed
  offset (`+07:00`); `DB_OPS_TIMEZONE` overrides it per node.
- Every rendered timestamp now carries its offset, in one format: `2026-09-07 07:32:56 +07`. This
  replaces four spellings, including `UTC+07:00` in Telegram alerts and the report headers that
  printed a wall clock with no zone at all. **Stored timestamps are unchanged** — still UTC
  `%Y-%m-%dT%H:%M:%SZ` on both backends; nothing was migrated.
- `docker-compose.yml` and `docker-compose.runtime.yml` no longer pin `TZ`. That line was the real
  timezone configuration and it shipped in the published image; the `timezone` field replaces it.
- `tzdata` is now a core dependency — IANA names need a zone database and Windows ships none.
- `DbOpsStore.report_exists_on_local_date()` takes `utc_offset_minutes` with no default, replacing
  a `utc_offset_hours=7` default argument.

### Added

- `timezone` in `config.json`, and `db_ops/lib/timezone.py` as its single implementation.
- `runtime_nodes` (store schema 3, additive): which clock each node is running on. Master and worker
  share one store and each reads its own config, so nothing could answer "is the estate on one
  clock?".
- `common.cli timezone` reports this node's resolved zone and touches nothing;
  `db.cli timezone --record --list` writes it to `runtime_nodes` and reads the cluster back.

### Fixed

- A `+07` offset compiled into the reports app decided the backup-health window and its header.
- Report filename stamps came off the host clock while the row describing them was UTC.
- Log files carried two interleaved clocks: `logging`'s `asctime` was host-local while the daemon's
  own lines were not. The rotation boundary now agrees with both.
- Backup retention kept ~7 extra hours at `+07`: a UTC cutoff compared against server-local mtimes.
- Backup ages were measured host-local against server-local finish times.
- A backfilled report covered a UTC day rather than the operator's.
- `server_report.html` rendered one axis in the *viewer's browser* zone and one label in UTC.
- Six listings stripped the `Z` from a UTC timestamp and left an unlabelled wall clock.

### Documentation

- `docs/01_runtime_store.md` now states who creates what: SQLite creates its own file; PostgreSQL
  creates **neither** the database nor the schema and needs a login already in the secret store —
  it builds only its tables. The schema name is free; `db_ops` is a default, not a requirement.

## [0.9.1] - 2026-09-05

### Added

- **`db_ops up` in `self-status`** — how long the daemon of this tool root has been running, beside
  the machine's uptime `0.9.0` added. The question people ask `self-status` is about the
  installation, and that was the one it could not answer: it runs as a separate short-lived process
  and opens no store on purpose. The daemon now leaves a small state file in `runtime/`, and the
  reader believes it only while its pid is alive — a hard kill leaves the file behind, and trusting
  the timestamp alone would report a daemon as up since a stop days ago. Reports `running`,
  `not running` (stopped cleanly, or never run here), and `not running … pid is gone` (died without
  unwinding) as distinct answers. The line reads `not running` until the daemon is restarted on this
  version, because an older daemon never wrote the file.

### Fixed

- **`daemon --once` left its state file behind**, so the next reader would have called a deliberate
  single pass a daemon that died without unwinding.

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
