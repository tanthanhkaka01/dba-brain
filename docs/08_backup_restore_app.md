# Backup Restore App

## Purpose

The Backup Restore App copies backup files, restores SQL Server FULL (and optionally DIFF + LOG) backups into a DR database, verifies restore health, and records restore history. Point-in-time restore (PITR) is supported wherever the logs cover the moment: SQL Server transaction log backups, PostgreSQL WAL and Oracle archived logs. See *Point in time, drilled on every engine*.

## Before a restore copies anything: does it fit?

The restore preflight now asks one question first, on every platform, before a share is prepared or
a byte is moved:

```
free >= bytes_to_copy x factor
```

`bytes_to_copy` is the same file selection `copy-backup` will make — same window, same patterns —
**less the files the target already holds at their size** (0.26.0: the copy leaves those alone, so
they take no room; a forced copy to a Linux target writes each again beside itself, and is counted
the largest of them). `free` is read where the files will land (a UNC share or local path with
`disk_usage`; a Linux target with `df -Pk` over the SSH session the copy itself uses). The rule is
`db_ops/lib/restore_space.py`; the measuring is `db_ops/backup_restore/space.py`.

**That is the whole rule, on every engine: the copy, at x2** (the operator, 2026-10-02). The factor
defaults to **2.0** for SQL Server, PostgreSQL and Oracle alike (1.5 until 0.26.0), and the database
a backup restores to is not measured: the engineer who sets up a restore knows the disk has to hold
it. A compressed backup's size is not the size of the database inside it - a 56.8 GiB chain restored
to 366.6 GB on 2026-10-01 - so an entry whose target is tight can ask for more:

**`"measure_restore": true` - once more before each database's first RESTORE: does the database
fit?** (0.26.0, off unless the entry says so)

```
free >= (files_the_backup_holds - files_of_the_database_it_overwrites) x factor
```

Read in one read-only batch on the target's own SQL Server, through the `sqlcmd` the restore is about
to use: `RESTORE FILELISTONLY` of the FULL (and of the DIFF, when one is applied - its files are the
later size) for what is created, `sys.master_files` for the database's files at the two paths the
restore moves to, and `sys.dm_os_volume_stats` for the free space of the volume the data path is on.
It is asked of the instance because the path is the instance's - in a container it is not a path on
the host. A drill run again over its own last restore therefore needs only what the database grew by.

```
restore-db restore_id=DRILL database=Payroll_Main restore room: 366.6 GiB of database files to
  create, x2 = 733.2 GiB needed, 394.0 GiB free - SHORT BY 339.2 GiB on /var/opt/mssql
```

A measured shortfall fails that database before any `RESTORE` is sent, like any other step of it; the
other databases of the entry are still tried, and the staged backups are kept. **A measurement that
could not be made is held to `on_unknown`**, as the copy's is: the entry asked for it by name, so it
is refused unless the entry also says `proceed`. An instance older than 2008 R2 SP1 has no volume
view, a login without `VIEW SERVER STATE` may not read it, and a data path on a volume that holds no
database file yet is not listed - an entry for such a target leaves `measure_restore` out.

**Only a share-driven SQL Server restore can be measured.** PostgreSQL and Oracle - every
script-driven entry - cannot be asked what a backup holds yet, so `measure_restore: true` on one is
refused when the configuration is read (*not supported for a script-driven restore yet*), rather
than accepted and not kept.

**A script-driven restore onto another machine is checked in its copy (0.26.0).** The entries that
hand their backup to another host and restore it there (`restore-by-id`: PostgreSQL, Oracle, SQL
Server in a container) carried the same `space_check` in the reference - *absent means on* - and
nothing read it: the copy took whatever the chain held. The rule (the operator, 2026-10-02): the
restore script checks nothing itself; the tool has checked before the script runs. So
`common.cli copy-backup-dir` takes the entry's `space_check` and, once it knows which files it will
write and before the first one moves, asks the target `df`: `free >= bytes still to copy x factor`.
A file the target already holds is not counted, so a drill run again is asked about its new
increment. A shortfall fails the restore at its copy (*will not fit ... No file was copied*); a
target that cannot be measured is refused unless the entry says `on_unknown: proceed`. Not measured:
what the engine's own restore then builds from those files, and an in-place drill, which copies
nothing - that is the engineer's to size, as on every engine.

Per entry, in `restore_config.json`:

```json
"space_check": {"enabled": true, "factor": 2.0, "on_unknown": "refuse", "measure_restore": false}
```

These are the defaults - an entry that says nothing is checked exactly so.

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | **On for an entry that says nothing.** A check that has to be switched on protects only the entries somebody remembered |
| `factor` | `2.0` | Must be >= 1.0. `free >= bytes still to copy x factor`, the same for every engine (1.5 until 0.26.0). `1.0` asks only that it fits. Also the margin of the restore's measurement, when the entry asks for one |
| `on_unknown` | `refuse` | What to do when a number could not be read. `proceed` is allowed and says so in the log on every run |
| `measure_restore` | `false` | `true`: also measure the database each restore builds, before its first `RESTORE` (above). Share-driven SQL Server only; refused on a script-driven entry |

**The copy's check reads the source the way the copy does (0.26.0).** A copy reads its source one of
four ways (`copy_backup.copy_engine`): a Linux node through `smb-list` (`smbclient`), a Windows node
copying share to share through `smb-list` too, and the other two as a path. The check used to walk
the source as a path whatever the copy did - and to a Linux node a UNC path is no folder at all: it
found nothing, counted **0 bytes** and said *fits*, on every restore the container worker ran. It now
asks the copy's own engine for the selection and its sizes (`copy_backup.selected_backup_sizes`), and
a source that cannot be read - a share that refuses the login, a folder that is not there, a listing
with no sizes - is **unmeasured, not empty**: held to `on_unknown`, with the reason in the log line.
On a Windows node the share's login is stored before the look, as the copy stores it.

**A Linux node also needs room for what it fetches.** It cannot hand a share to the target: it
downloads what it will transfer into its own temp folder, sends it on and removes it - on a container
worker, the disk the runtime store shares. Before the copy the check says how much passes through
that folder and how much is free there, and refuses a fetch that cannot fit (*will not fit on this
node ... Nothing was copied*). Only whether it fits, without the factor: the bytes are there for the
length of the copy. `TMPDIR` names another folder.

A refusal names both numbers and what can be changed, and nothing has been copied when it fires:

```
restore_id=DRILL will not fit: 115.0 GiB to copy, x2 = 230.0 GiB needed, 92.0 GiB free
  - SHORT BY 138.0 GiB. Free 138.0 GiB on //target/import, lower space_check.factor (now 2),
  or restore somewhere else. Nothing was copied.
```

**Why it refuses rather than warns.** A warning in a scheduled run is read after the outage, when it
is evidence and not a brake. On 2026-09-17 a drill copied about 115 GB onto the host that also
carries the runtime store: `/` reached 42 MB free, PostgreSQL could not write, and the daemon went
with it 26 minutes into a soak that then had to be abandoned.

**An entry may carry its own copy settings.** `copy_recent_hours` and `copy_file_patterns` on the
`backup_restore` block are the defaults; the same keys on a restore entry override them for that
entry, and `space_check` is the entry's own. All three are described on `restore_entry` in the
reference (and `space_check`'s fields as `restore_space_check`), so `check-objects` reports a wrong
value in them - until 0.25.0 they were read on the entry and described nowhere there. A source whose
FULL is weekly needs a window that reaches it: `copy_recent_hours: 192` (a week and a day), with a
`cleanup_retention` no shorter, or the staging cleanup removes what the next copy brings back.
`--copy-hours` on `workflow` / `restore-workflow` overrides it for one run; unset, each entry copies
by its own. `0` (or less) is "every matching file", and for a point-in-time restore that means every
file up to the moment. Until 0.25.0 the flag defaulted to 24 and always won, so `copy_recent_hours` was parsed
and never used by a scheduled, manual or `/spbot_restore` run.

**A SQL Server restore copies its chain, not its window (0.26.0).** Before the copy, each mapped
database's backups on the source are listed and the restore's own rule picks the chain: the newest
FULL at or before the moment (now, or the point in time), the newest DIFF after it and at or before
the moment, and the LOGs after that - for a point in time, up to and including the first LOG that
reaches it, which is written after the moment (`db_ops/lib/sqlserver_backup_chain.py`). Only those
files are copied, and the space check counts the same list. On 2026-10-01 the window copy of the
100.250 drill was **527.9 GiB** - eight days of daily DIFFs and hourly LOGs, because the window had to
reach a weekly FULL - for a chain of **56.8 GiB**, and the space check refused it. All three copy
paths take the chain: the Windows node's listing of the share, the Windows target's share listing,
and the Linux node's `smbclient` listing. `copy_recent_hours` is now the fallback for a database the
chain cannot be settled for (no FULL at or before the moment, or no `FULL` / `DIFF` / `LOG` folders),
and still how old a staged FULL the restore step takes - so keep it reaching the newest FULL.

**`copy_selection` chooses which of the two the copy takes** (the operator, 2026-10-01), on the
`backup_restore` block and, overriding it, on a restore entry: `"chain"` (the default) or
`"window"` - every file of `copy_recent_hours`, as before 0.26.0, for a target that must hold every
restore point of the range and has the room. The space check counts whichever list is copied, so a
`window` entry that will not fit is refused before the first byte. Any other value is refused when
the configuration is read (`config.COPY_SELECTIONS`).

**The free space is read where the staged files will land, even before that folder exists.** The
copy makes the staging folder, and it runs after this check - so on a target never restored to,
the folder is not there yet. Both sides measure the nearest folder above it that exists: the local
one always did, and a Linux target's `df` does since 0.25.0 (`space.linux_free_space_command`). Until
then every first restore onto a rebuilt lab was refused as *could not read the target's free
space*.

## Package / Files

- `db_ops/backup_restore/`
- `db_ops/backup_restore/sql/sqlserver/`
- `data/restore_config.json`
- `data/backup_policy.json` — owned here (`app_code: backup_restore`), read by `reports`
- the runtime store declared in `data/store_config.json` (PostgreSQL in this tree; `runtime/db_ops.sqlite` when the backend is `sqlite`)

## Runtime Tables

- Writes `backup_restore_history`.
- Workflow-level events may also be written to `job_runs`.

## Config Files

`data/restore_config.json` defines source/target paths, SQL Server connection metadata, database mapping, certificate import settings, copy/delete windows, and restore verification behavior. Secrets are resolved through the same local secret pattern as other database connections.

**Nothing configured is a state, not a failure.** `db-ops init` writes the file from
`db_ops/backup_restore/catalogue/restore_config.json`: `{"backups": [], "restores": []}` plus
`notes` giving the shape and the `cleanup_retention` contract. It ships empty because a restore
target is an estate fact and has no sensible default. An empty list, an absent `restores` key and
an absent file all load as **no entries**: the scheduled `workflow` has nothing due, reports
`configured: 0` and exits 0, while a manual command that needs one restore (`copy-backup`,
`restore-latest`, `verify-restore`, ...) fails with a message naming `data/restore_config.json`. Before
2026-09-11 they did not: `init` wrote no file at all, the loader parsed its own empty defaults as one
malformed entry, and the only thing the operator saw was `ERROR: 'prod_backup_share'` on every cycle.
`restores: []` was also refused outright, which would have made the shipped empty file fail too.

## Registering an entry: `backup-add` / `restore-add`

Both halves of `restore_config.json` were a hand-edit until 2026-09-14 — a nested object with an
array of jobs inside it, a `time_window` and a `cleanup_retention` per job, and one field
(`env_secrets`) that could only be filled by writing a password into `secrets/secret_text.json` in
the clear and running `encrypt-secret`. That last step is the one deleting the file afterwards does
not undo.

```bash
python -m db_ops.backup_restore.cli backup-add @data/new_backup.json --key-base64 "<KEY>"
python -m db_ops.backup_restore.cli restore-add @data/new_restore.json --key-base64 "<KEY>"
```

One JSON object in — inline, `@file` or `-` — the same contract `instance-add` takes. On Windows,
prefer `@file`: a single-quoted JSON argument does not survive the shell.

* **The secret never reaches the disk in the clear.** `env_secret_values: {ENV_NAME: value}` on a
  backup, and `password` / `sql_password` inside a restore's `source` and `target`, are encrypted
  into the store and replaced by the derived ref (`BACKUP_<ID>_<ENV>`, `RESTORE_<ID>_<BLOCK>_<FIELD>`).
  Give the value or the existing ref, never both.
* **What is written is validated by this app's own loader.** The candidate document goes to a
  temporary file, `load_backup_jobs` / `load_restore_configs` is pointed at *that*, and only a
  document that loads is committed. A restore is also checked by `load_script_restores`, the
  loader the scheduler and `restore-workflow` run for a script-driven entry. `load_restore_configs`
  steps over that shape, so until 2026-09-25 such an entry was checked by nothing: one routed to
  a notify level no group defines was written, and from then on `list-restores` and every
  `restore-workflow` failed on it, for every entry on the node. The check covers both shapes now — so a refusal is the exact sentence the daemon would have
  failed with at 01:00, and there is no second schema in the registration code to drift from the
  first. A bare `KeyError` from `parse_restore_config` is turned into `missing required field:
  <name>`; `'prod_backup_share'` as an entire error message cost a day in September 2026.
* **Validation runs before any secret is stored.** A document the loader rejects leaves nothing
  behind — otherwise a secret sits in the store under a ref no config mentions, invisible and
  indistinguishable from a live one.
* **`cleanup_retention` is required**, in seconds, on every job and every restore entry. Neither
  command supplies a default: it was made mandatory on 2026-09-11 because six of fourteen restore
  entries silently carried none, and an absent field reads exactly like a considered one.
* **`time_window` is required** — on a restore entry, and on every job of a backup entry. Both go
  through the same `schedule.is_due`, where a unit of work carrying no window gets an always-open
  window and `DEFAULT_REPEAT_SECONDS`. Absent is therefore not "unscheduled": it is **every 300
  seconds**. Measured 2026-09-14 on a restore entry registered without one — it restored a 183 GB
  database for 36 minutes, finished, and started again four seconds later; the target lost about
  5 GB of free disk per cycle and every run reported success, so nothing in the log read as wrong.
  A nightly full is `{"from_hour": 1, "to_hour": 4, "repeat_interval": 72000,
  "retry_interval": 1800, "timeout": 7200}`; a log backup every quarter hour is
  `{"repeat_interval": 900, "retry_interval": 300, "timeout": 1800}`. The refusal names the job
  that lacks one, because an entry carries several.
* **The app's own interval is a floor under every job in it.** A job's `time_window` is read when
  `APP-BACKUP-RESTORE` runs, and that app runs every `app_commands.json` `repeat_interval`. With the
  app at 300 s, a log backup declared at 900 s waited up to 300 s more for the app's next pass and
  ran every ~1,200 s (measured 2026-09-19). Keep the app's interval short against the shortest job:
  **the shipped default is 1 s since 0.24.0**, as `APP-SQL_TASKS` - down from 30 (2026-09-21) and 300
  before that. It shipped at 300 for two releases after this estate had already corrected it on one
  node — a fix that reached the node and not the catalogue, which is the drift a shipped default
  hides. The app exits at once when nothing is due, so a short interval costs a process start, not a
  backup, and it is safe only because `run_mode: async` is paired with the claim
  (`ux_job_runs_claim`, one `running` row per backup job). It is the same rule as the SQL task app
  ([05](05_sql_task_runner.md), *The three clocks*). A node made before 0.24.0 keeps what it has;
  `common.cli app-command-set '{"app_code": "APP-BACKUP-RESTORE", "time_window": {"repeat_interval": 1}}'`
  moves it.
* **What "at the same time" means.** One run works through its due list **in order**: all due
  backups, then all due restores. Overlap comes from the next async run, which takes what is due and
  not yet started - **so the interval decides how many due jobs run at once.** At 30 s the second
  run started 30 s after the first, and three restores due together reached two at once on the
  0.23.0 soak (1.58); at 1 s the next run starts a second later and takes the next job, up to
  `max_parallel` (4). The option weighed and not taken: running one run's jobs in parallel - more
  code in the path every backup takes, for what the interval already gives. Measured on the labs on 2026-09-25, with three single-instance
  full backups due at 13:40 +08 and their restores to the second VM at 13:43:
  - the backups ran one after another inside one run (SQL Server 3 s, PostgreSQL 2 s, Oracle
    32 s), and the next run found nothing left;
  - the restores did overlap: the first run restored SQL Server then PostgreSQL, and the next run
    took the Oracle restore at 05:44:15Z while PostgreSQL was still restoring, until 05:44:24Z.

  A job the next run has **finished** is not run again when the first run reaches it: before each
  claim, the run checks whether one started since it read its list (`schedule.taken_since`). The
  claim alone allowed that until 2026-09-25 ([03](03_app_command_daemon.md)).
* **Two shapes, and the difference is the engine, not untidiness.** A backup entry holds `jobs[]`
  and each job carries its own `time_window`; a restore entry carries one directly. All three shapes
  are described field by field in `data/shared_config_objects.json` as `backup_entry`, `backup_job`
  and `restore_entry`, so where a `weekdays` goes is answerable rather than inferred.

  **PostgreSQL's `database_full` and `database` stay in ONE entry.** `pg_basebackup --incremental`
  chains onto the newest backup in *that entry's* `backup_dir/base/`, so one entry is what makes them
  share a directory. Split across two, `backup_dir` becomes a copy-paste invariant and getting it
  wrong raises nothing: `latest` comes back empty, the script takes a FULL baseline by the rule that
  stops a first run being an error, reports success, and does that for ever — no incremental again,
  every run `done`. `list-backups` warns when an entry has an incremental with no baseline beside it.

  SQL Server is different and its `full`/`diff`/`log` **are** separate entries here: a diff needs its
  full in the same database history, not the same directory. The WAL and archivelog streams are their
  own entries too, on every PostgreSQL and Oracle registration — they are not part of a chain and run
  on a 15-minute cadence rather than nightly.

* **PostgreSQL and Oracle backups require a container.** `DOCKER_CONTAINER` is not optional and
  every command in both engine scripts goes through `docker exec`; there is no host-native path, so
  an engine installed directly on a VM cannot be backed up by db_ops. Every PostgreSQL and Oracle
  target in this estate is a container, so nothing is blocked — but the failure on a native instance
  is `DOCKER_CONTAINER is not set`, which reads like a configuration mistake rather than a missing
  capability. SQL Server has no such limit.

* **What else a job's `env` can say to an engine script.** All optional, all defaulting to the
  behaviour every entry in this estate already has:

  | Var | Engine | What it is for |
  | --- | --- | --- |
  | `PG_PORT` | PostgreSQL | The cluster's port inside the container. Unset means the image default. It travels as a `-p` argument, not as `PGPORT`: `docker exec` does not carry the calling script's environment in, so an exported `PGPORT` would reach the container host and never reach `psql`. |
  | `ORACLE_SID` | Oracle | Which instance `rman target /` connects to. Unset leaves the container login profile's own choice. Exported *inside* the container after the profile has run, because a profile that sets `ORACLE_SID` would overwrite a `docker exec -e`. |
  | `ORACLE_OS_USER` | Oracle | The OS user inside the container. Unset means the image's own, which is what both Oracle containers here run as and the only behaviour exercised. The PostgreSQL script pins `-u postgres` because a backup written by the wrong user is owned `root:root` and `0700`, which `pg_verifybackup` cannot read and PostgreSQL will not start on a restore of. |
  | `RMAN_CONFIGURE` | Oracle | `apply` (default) or `skip`. The `CONFIGURE` statements are **persistent** database settings, not options for the run: RMAN stores them in the controlfile, they govern every later connection, and they decide what this script's own `DELETE NOPROMPT OBSOLETE` removes. `skip` is for a database whose retention policy is somebody else's to set. Either way `SHOW ALL` runs first, so the values as they were are in `stdout_tail`. |

  `skip` is not free on the **archivelog** job. Its deletes name an age, but RMAN still consults the
  archivelog deletion policy before removing anything: under the policy the script sets
  (`BACKED UP 1 TIMES TO DISK`) a log that has not been backed up survives its age, and under
  `TO NONE` — the default on a fresh database — the same statement deletes it.

* **An incremental that cannot chain takes a baseline instead of failing.** Three things are asked
  before `pg_basebackup --incremental` is attempted: that a parent exists, that `summarize_wal` is
  on, and that the parent's `System-Identifier` is this cluster's. A parent from a restored or
  rebuilt cluster is refused by the server **permanently** — no retry changes which cluster it came
  from — so it is caught first and the run takes a FULL. Without that, the job answered `error`
  every night and left `base/` without a single directory, which is what five nights from
  2026-09-19 actually looked like from the other cause.

* **The level is configuration, not a weekday inside the script.** `BACKUP_LEVEL` in a job's `env`
  says which backup the script takes — `full`/`incr` for PostgreSQL, `0`/`1` for Oracle, and
  `full`/`diff`/`log` for SQL Server — and **which days that job may run on is
  `time_window.weekdays`** (ISO 1-7, read on the node's configured clock). So a weekly baseline plus
  daily incrementals is **two jobs on one entry**:

  ```jsonc
  {"job": "database_full", "env": {"BACKUP_LEVEL": "full"},
   "time_window": {"weekdays": [7], "from_hour": 1, "to_hour": 5, "repeat_interval": 72000}},
  {"job": "database",      "env": {"BACKUP_LEVEL": "incr"},
   "time_window": {"weekdays": [1,2,3,4,5,6], "from_hour": 1, "to_hour": 5, "repeat_interval": 72000}}
  ```

  Until 2026-09-21 the PostgreSQL and Oracle scripts chose the level themselves, comparing
  `${DB_OPS_WEEKDAY:-$(date +%u)}` against `7`. db_ops only began passing `DB_OPS_WEEKDAY` in
  0.20.0, so every older node fell through to the container **host's** clock — a different day from
  the one the daemon read the same job's `from_hour` on. On 2026-09-19 the host said Saturday while
  the node's own +07 said Sunday, and five incrementals ran and failed on the night the weekly full
  was due. **Keep `repeat_interval` under a day on a weekday-gated job**: `weekdays` decides which
  day and the interval decides whether it is due, so a multi-day interval lets the due moment walk
  past the window and skip whole weeks.
* **The PostgreSQL and Oracle backup scripts require a container, and one cluster per container.**
  `DOCKER_CONTAINER` is required and every command goes through `docker exec`, so an engine
  installed **natively** on a VM cannot be backed up by these scripts at all — the failure is
  `DOCKER_CONTAINER is not set`, which reads like a config mistake rather than a missing capability.
  There is also no `PGPORT`/`PGHOST` and no `ORACLE_SID` in their env contract: `psql` and
  `pg_basebackup` are called with no `-h`/`-p` and land on the container's default socket, and
  `rman target /` connects to whatever SID the image's login profile exports. So **two clusters or
  two SIDs inside one container cannot be addressed**, and the `port` in `db_instances.json` is the
  published host port, which these scripts never use. Different containers on one host, and
  different hosts, are fine and are what the estate runs.
* **An incremental that cannot chain takes a baseline instead, in the same run.** PostgreSQL's
  `pg_basebackup --incremental` needs WAL summaries covering the parent's LSN range, and a baseline
  taken before `summarize_wal` was enabled can never be chained onto — no retry can produce
  summaries for WAL that has been recycled. The script now asks `show summarize_wal` first, and
  classifies the server's refusal: a *"WAL summaries"* error on an incremental takes a FULL instead
  and the night still has a backup, while every other failure is still fatal. It never creates the
  backup directory itself — a directory the script made so the run looked successful would be a
  backup that does not restore.
* **`notify` is required too**, and for a reason that looks like the opposite of a missing
  notification. An entry without one still notifies — `BACKUP_RESTORE_NOTIFY_DEFAULTS` turns both
  rules on — but at the *neutral* levels, `logging` and `error`, rather than the entry's own chat.
  So the messages are queued, delivered, and land somewhere nobody is looking: on 2026-09-15 a
  restore reported its start and its copy phase into the Logs group while the operator watched the
  Restore group and reported that nothing had been sent. One object on a backup entry covers all of
  its jobs.
* **`replace` or nothing.** An existing `backup_id`/`restore_id` is refused rather than
  overwritten, and registering a restore leaves the backups in the same file untouched.

Verify with `list-backups` / `list-restores`, then `backup --dry-run`.

## Data Flow

Restore config -> copy recent backup files into import location -> optionally import certificate -> locate latest FULL backup -> generate/execute SQL Server restore SQL -> verify restored database -> write `backup_restore_history` and logs -> delete old copied files when requested.

### A restore is not `done` until the databases open

Two rules, both added 2026-09-16 after a drill reported `status=done` over a database `Msg 5149`
had left mid-restore and nobody could open:

1. **The verdict is computed from the per-database outcomes, never asserted beside them.** The
   workflow used to set `status: SUCCESS` next to the `failed` count it had already calculated, so
   only an exception could make a run fail. A non-success on any database now fails the run and
   **names the databases** — a message reading "1 database failed" sends its reader to the store,
   which is where this hid for two releases. A status the code does not recognise counts against
   the run: the engines do not agree on a word for failure, and "unknown" is not evidence that a
   database is usable.
2. **A `verify-restore` phase runs last**, before retention cleanup. It opens each restored
   database with a real query rather than reading a state column, because a database can read
   ONLINE and still refuse one while it finishes an upgrade step. It asks about the name on the
   **target** (`restore_database_name`), not the source's — a drill that restores `SALES` as
   `SALES_STG` would otherwise be asked about a database it was never told to create.

**Both stop before the retention cleanup, deliberately.** A restore drill is what proves the
backups are restorable; pruning them in the same run that failed to restore one is exactly
backwards.

**An entry this node holds no target login for is `SKIPPED`, with the reason** — not failed. "Not
configured" is a state, and a check that cannot run is not evidence of a broken restore. The same
applies when the secret store cannot be read at all: the restore itself got its credentials some
other way, and this check must not be more fragile than the work it is checking.

Where each engine is verified:

| Path | Engines | Verified by |
| --- | --- | --- |
| `restore-workflow` — the SMB engine flow | SQL Server | the phase above, added 2026-09-16 |
| `restore-by-id` — script-driven drills | Oracle, PostgreSQL, MySQL, and SQL Server container drills | `verify-restore` has been the planner's last step for all three engines since it was written; a failure raises, and the scheduled runner records `status=error` with the text and notifies |

So the check already existed and was already planned for every engine. What was missing was the
nightly SQL Server path calling it.

**The depth differs by engine, on purpose.** SQL Server restores one database at a time, so each
can fail on its own and the check takes a list of database names. PostgreSQL (`pg_basebackup` plus
WAL replay) and Oracle (RMAN `DUPLICATE`) restore **the whole instance**, so the instance coming up
*is* the answer and `verify-restore` takes only a host for them. An engine that restores an instance
is verified at the instance; only SQL Server needs the per-database list.

## How to Run

These commands read encrypted secrets (SMB/SQL passwords, certificate API token),
so supply the passphrase with `--key_base64 "<base64>"` (or `--key "<passphrase>"`,
or export `DB_OPS_SECRET_KEY`). It is omitted from the examples below for brevity.

```powershell
# List configured restore IDs with their source/target IPs (no secrets needed).
# This is the CLI the Telegram bot invokes for spbot_list_restore_id.
python -m db_ops.backup_restore.cli list-restores --config config.json

# Copy backup files into the local import folder
python -m db_ops.backup_restore.cli copy-backup --config config.json
python -m db_ops.backup_restore.cli copy-backup --config config.json --restore-id ACME_TO_SQLSERVER_192_168_18_31

# Delete old backup files from the import folder
# OBSOLETE — superseded by common's list-backup-files + delete-files (docs/13_common.md).
# Still here because restore-workflow calls it and the daemon schedules that; nothing new
# should use it. It computes its own set from a retention window and a *.bak glob, so what it
# would remove cannot be inspected first — which is the whole reason the replacement exists.
python -m db_ops.backup_restore.cli delete-backup --config config.json --dry-run
python -m db_ops.backup_restore.cli delete-backup --config config.json --retention-seconds 172800

# Import encryption certificate (dry-run first)
python -m db_ops.backup_restore.cli import-certificate --config config.json --source-id ACME-192-0-2-250 --dry-run

# Restore latest full backup (dry-run first to inspect SQL)
python -m db_ops.backup_restore.cli restore-latest --config config.json --dry-run
python -m db_ops.backup_restore.cli restore-latest --config config.json
python -m db_ops.backup_restore.cli restore-latest --config config.json --restore-id ACME_TO_SQLSERVER_192_168_18_31
python -m db_ops.backup_restore.cli restore-latest --config config.json --source-id ACME-192-0-2-250 --database SALESDB_Prod --target-database APP_DR

# Full restore workflow: copy-backup → restore-latest → delete-backup
python -m db_ops.backup_restore.cli restore-workflow --config config.json
python -m db_ops.backup_restore.cli restore-workflow --config config.json --restore-id ACME_TO_SQLSERVER_192_168_18_31
python -m db_ops.backup_restore.cli restore-workflow --config config.json --restore-id ACME_TO_SQLSERVER_192_168_18_31 --force
python -m db_ops.backup_restore.cli restore-workflow --config config.json --restore-id ACME_TO_SQLSERVER_192_168_18_31 --dry-run

# Point-in-time restore (PITR) — requires FULL + LOG backups covering the target time
python -m db_ops.backup_restore.cli restore-workflow --config config.json --restore-id ACME_TO_SQLSERVER_192_168_18_31 --point-in-time "2026-06-13 21:00:00 +07:00"

# Verify the restored databases open - common's check, the one the workflow's last phase runs
# (the app's own `verify-restore` went in 0.24.0, rules R43). The request carries a password: @file.
python -m db_ops.common.cli verify-restore @verify.json
#   {"db_type": "sqlserver", "database_names": ["APPDB_Prod_DR"],
#    "target": {"host": "...", "port": 1453, "username": "...", "password": "..."}}
```

## CLI as the cross-app boundary

The backup restore app owns `restore_config.json` and exposes everything other
apps need through its **own CLI** — including `list-restores`. Other apps (for
example the Telegram bot's `spbot_list_restore_id`) **invoke this CLI as a
subprocess**; they do **not** import `db_ops.backup_restore` and do **not** read
`restore_config.json` themselves. The caller only knows which command to run; the
backup restore app resolves its own config and secrets. New "expose restore data
to another app" features must follow the same rule: add a CLI subcommand here, not
a cross-app import or a second reader of `restore_config.json`.

## Where a backup actually runs (since 2026-08-07)

Executing a backup moved down to `db_ops/common/backup/` and the `backup-database` command
(`docs/13_common.md`). This app kept the half that reads data and remembers things:

| Stays here | Moved to `common` |
| --- | --- |
| `load_backup_jobs` — `restore_config.json` → `BackupJob` | shipping the script and running it |
| `select_due_backup_jobs` / `schedule` — is it due, has it timed out | judging the result (the `RESULT=ok` receipt) |
| `resolve_backup_target` — `server_id` → host, transport + container | translating `full`/`diff`/`log` into the engine's own level |
| reading the secret store for **everything the spec carries** | nothing about config, scheduling or the store |
| `job_runs` rows, events, Telegram notify | |

`spec_builder.backup_spec_from_job` is the seam: it turns a configured job plus its resolved target
and decrypted secrets into a self-contained spec, and `execute_backup_job` now does nothing but
build one, run it and translate the answer back into the `BackupRunResult` the scheduler already
speaks. `backup_level_for` is re-exported from `common.backup.spec` so the CLI, the spec and this
app cannot disagree about what "full" means for Oracle.

The point of the split is that a one-off recovery gets identical behaviour: a level 0 baseline
against a machine that is in no inventory at all is the same call the scheduler makes, minus the
lookups. Verified on 2026-08-07 — the same Oracle archivelog backup ran in 16s through
`common.cli backup-database` and 17s through `backup_restore.cli backup --force`.

**The SSH password is one of the values the app must resolve.** `spec.host.password` is a value like
any other, so `execute_backup_job` reads the secret store whenever the *spec* will need it — when the
job declares `env_secrets` **or** when the target authenticates by password (`password_ref` set, no
`key_file`). Keying that off `env_secrets` alone was right only while `execute_over_ssh` resolved
`password_ref` itself, lazily; once the transport moved down, every password-auth job that decrypted
nothing else built its spec against an empty store and failed on
`backup.host.password_ref ... is not in the secret store`. On 2026-08-08 that was 79 of 87 failures
in six hours — every `ACME_*` job, while the key-auth `CLOUD_*` ones kept running because a key needs
nothing decrypted. The condition is still a condition: a key-auth job with no `env_secrets` never
touches the store, so it keeps working on a node without the passphrase.

### SSH **or** WinRM (since 2026-08-10)

`resolve_ssh_target` accepted only `cmd_access.method = ssh` and refused everything else at config
resolution, before anything was attempted. That read as a statement about the transport and was
really a statement about what nobody had needed yet: `hostcmd.run_script` has dispatched WinRM the
whole time, delegating to the same `db_ops.common.remote_exec` the metrics collectors use against
these hosts every cycle. What it cost was a Windows SQL Server with no OpenSSH server —
192.0.2.248, like most of the Windows estate — which could not be backed up at all, and whose
only workaround was installing an SSH server to satisfy a check rather than a limitation.

`BackupTarget` now carries `access` and `ssl`, and `spec_builder` puts both on the `hostcmd.Host`:

| `cmd_access.method` | Default port | Key file |
| --- | --- | --- |
| `ssh` | 22 | honoured |
| `winrm` | 5985, or 5986 with `ssl: true` | dropped — a key is an SSH idea, and carrying one would make `spec_builder._ssh_password` suppress the password WinRM needs |
| anything else, incl. `local` | — | refused: `local` would back up the db_ops container under the target's name |

An explicit `cmd_access.port` still wins. Defaulting to 22 regardless of method was harmless while
only SSH was allowed and is not now — a WinRM target with no stated port would speak WinRM at an SSH
port and report the host unreachable, which is the least informative way to be wrong.

Nothing about the SSH path moved. The fourteen entries running when this changed were re-resolved
side by side against the old rule: same access, same port, same key file for every one.

## Retention cleanup: two commands, two conditions

### `prune-backups` — the backup side

```bash
python -m db_ops.backup_restore.cli prune-backups --config config.json          # report only
python -m db_ops.backup_restore.cli prune-backups --config config.json --apply  # delete
```

Its own step, not part of `backup`. Taking a backup and deleting one are different risks — a backup
that fails costs a run, a prune that is wrong costs what the run produced — and they want different
cadences. Reporting is the default and `--apply` is what deletes, the opposite default from
`backup`: reporting a backup that did not happen is a wasted run, reporting a deletion that did not
happen is not.

Each entry is judged with **its own `cleanup_retention`** — the same number its own script prunes
with, so the two cannot drift — through `common`'s `prune-backup-files` (`docs/13_common.md`),
`mode=age` by default. `--retention-seconds` and `--mode` override for one run (`--retention-days`
is the deprecated spelling and is multiplied on the way in). Oracle and PostgreSQL entries are
listed from the filesystem; SQL Server entries are **skipped by name**, because their listing goes
through the instance and needs a login this command does not carry. Each run writes its own
`job_runs` row under `backup_restore.prune_job.<backup_id>.<job>`, so "did the cleanup run" is a
different question from "did the backup run".

Measured on 2026-08-07 across the 9 active jobs: 6 pruned, 3 skipped, 10 obsolete WAL files found
on `CLOUD_PG_WAL` at its 7-day window, nothing deleted (report mode).

**Each database's newest full, and everything after it, is always kept** (0.25.0 review), in both
modes, with the reason `the newest full of this database - kept regardless of age`. Under `age`, a
database whose backups had been failing for longer than the window lost every backup it had left on
the next `--apply`.

### The restore staging folder — age **and** obsolete

`delete-backup` (inside `restore-workflow`) clears the import share. It has always deleted by age
(`copy_recent_hours`); since 2026-08-07 a file must **also be obsolete**, meaning *a newer full
exists*. The two are an AND, and the second only ever spares: age narrows, obsolete narrows further.
Nothing the age gate rejected can be deleted by the obsolete rule.

The point is the newest full. With a short window, age alone deletes it and the next restore starts
from nothing — which is what `copy_recent_hours=0` used to do to the whole folder.

All three delete engines (local Python, SSH, PowerShell/UNC) apply it, and the verdict is computed
**in Python for all three**: the PowerShell engine now scans first and receives an explicit
allow-list, because deciding inside the script would be a third copy of a rule that has to be one
rule.

**The Windows engine reads the chain from the share's own listing** (0.25.0), and keeps its paths
as Windows paths wherever it runs. In 0.24.0 it joined the `smb-list` names into a `Path` - on
Linux one component, no split at `\` - and read the chain by walking the UNC path, which a Linux
node cannot: every aged file was held back as `still_needed` and the share was never cleaned. Safe,
but it filled. A Windows node was unaffected, and so was a Linux target (the SSH engine). Found by
`ci` on the 0.24.0 release commit, whose runners are Linux.

**Only what this entry staged** (0.26.0). The cleanup recursed every `*.bak` / `*.trn` under the
import root, so a root set one level too high - onto the target host's own backup or data directory
- had that host's own backups deleted by age, and the guard only refused the source's folder or a
one-component path (`/data` passed). The copy writes under `<root>/<mapped source database>/`, so all
three engines now consider only files there; an entry that maps no database needs a Linux root at
least three levels below `/` (`/opt/db_ops/staging`), or it is refused before anything is listed
(review 0.25.0, B4.2).

**Only a full anchors a chain** (0.25.0 review). A SQL Server differential is a `.bak` like the full
it restores onto; classified by extension, the newest DIFF became the anchor and the FULL was
deleted as obsolete once it passed the retention. The kind is read from the folder
(`FULL`/`DIFF`/`LOG`…), the name (`_DIFF_`, `_INCR_`, `_LOG_`) and the extension (`.trn`).

**A chain is per database, not per directory.** One staging directory holds every database copied
from a source (`<source>/<database>/<FULL|LOG>/`), so "the newest full" has to be asked once per
database. Asked once for the directory, the newest full anywhere decided every database's fate: on
2026-09-14 a 5 MB `Sessions` full taken 89 seconds after a 30 GB `Orders` full retired the 30 GB
one, while the logs that restore from it were correctly kept — a chain with no anchor, which is the
one outcome this rule exists to prevent, reported as a clean success. The key is the database
folder's *name*, so an entry staging fulls and logs under different roots still counts as one
chain; a database with no full staged still answers to the newest full in the directory, as every
one did before.

Three things about it that were wrong first, and are now tests:

- **The chain must be judged over the whole directory, not the age-selected part.** The newest full
  is exactly what the age gate filters out, so a lone aged file was its own anchor and nothing was
  ever deleted — the cleanup became a no-op that reported success.
- **Timestamps need a tie-break.** `vm_import_unc` is an SMB share, where mtime resolution can be
  two seconds and copies land in the same tick routinely. Compared on the timestamp alone, tied
  fulls are each "not older than the newest", every one is kept, and the share fills up.
- **One anchor per chain, not per directory** — above. Dry-run output is what caught it, not the
  suite: every test until then used a flat folder holding one database.

## Useful Manual Queries

```sql
SELECT restore_id, database_name, backup_file, restore_start, restore_end, duration_seconds, status, error_message
FROM backup_restore_history
ORDER BY restore_start DESC, restore_id DESC
LIMIT 50;

SELECT log_id, created_at, job_code, level, status, message, error_text
FROM job_runs
WHERE job_code LIKE '%RESTORE%' OR job_code = 'APP-BACKUP-RESTORE'
ORDER BY created_at DESC, log_id DESC
LIMIT 50;
```

## Windows vs Linux Restore Target Transport

The workflow uses a different transport mechanism depending on `vm_platform` in `restore_config.json`.

| Step | Windows target (`vm_platform: "windows"`) | Linux target (`vm_platform: "linux"`) |
|---|---|---|
| **Import dir creation** | Preflight checks UNC share root; creates local dir + SMB share via PowerShell remoting (WinRM) if missing | `_prepare_linux_base_import_dir` runs `mkdir -p` over SSH, under `sudo` when the login cannot |
| **Backup file copy** | `common.cli smb-list` scans the source share (since 0.24.0; a PowerShell `Get-ChildItem` before) + `shutil.copy2` to UNC target | Python scan source + `common.cli push-file` to the Linux target, hash-checked, mtime kept |
| **Backup file selection** | `Path.rglob("*.bak")` over UNC mount | SSH `find ... -name "*.bak"` on remote Linux fs |
| **Restore execution** | PowerShell `Invoke-Command -ComputerName` → sqlcmd on remote Windows | SSH `sqlcmd` executed on the remote Linux host |
| **...run by** | `common.cli run-sqlcmd` (`via: winrm`), since 0.23.0 | `common.cli run-sqlcmd` (`via: ssh`), since 0.23.0 |
| **Delete old files** | `common.cli smb-list` + `smb-delete` of exactly the files this app chose (since 0.24.0; PowerShell `Get-ChildItem` + `Remove-Item` before, which ignored `--dry-run`) | SSH `find ... -name "*.bak" -o -name "*.trn"` + `rm` |
| **Credentials** | `common.cli smb-credential` stores the login for SMB (`cmdkey`, run in `common` since 0.24.0) | the target's `username` + `password_ref`, resolved here and sent to `common.cli` on stdin |
| **Log file** | `copy_sqlbk.log` written to `vm_log_unc` | Skipped (log not written for Linux targets) |
| **Retention filter** | `*.bak` and `*.trn` only (no other files deleted) | `*.bak` and `*.trn` only |
| **Cleanup timing** | After restore (copy → restore → delete) | After restore (copy → restore → delete) |

**The restore's statements run through `common.cli` (0.23.0), and since 0.24.0 its RESTOREs are
written there too.** Every restore of a file - the full, the differential, each log, the last log's
`STOPAT` - is `common.cli restore-full` / `restore-diff` / `restore-log` with `sqlcmd` (the same
block `run-sqlcmd` takes): this app says which file, as the target sees it, and where the data and
log files go (`move_files` - the logical names are still read on the server with `RESTORE
FILELISTONLY`), and `common/restorestep/sqlserver.py` writes the statement - the one place a SQL
Server RESTORE is written, shared with the drills (rules R43, the operator's choice). The text is
the text this app wrote before, byte for byte (`tests/test_one_sqlserver_restore_statement.py`,
against what 0.23.0 emitted), except that a quote in a name or path is now escaped for the two
literals it sits in. The recovery, the recovery model, CHECKDB and the resume probe of an
interrupted LOG chain are `run-sqlcmd` batches as before. Every value is resolved here: the
instance, the SQL login, the host login, the timeouts. `common` reads no configuration and runs exactly the command
this app used to run itself: the same `Invoke-Command` wrapper for a Windows target, the same
`export PATH=…; sqlcmd … -C -b` over SSH for a Linux one - held byte for byte by
`tests/test_the_sql_server_restore_runs_through_common.py`, because the Windows path cannot be
proven from the Linux labs. What the app still owns is every decision: which files, which
statements, what exit code 0 with *Msg 3013* in it means, when a lost connection is safe to retry
and when a RESTORE LOG's state has to be inspected first.

**The shares and the certificate go through `common.cli` too (0.24.0, rules R10).** The app starts
no process to reach a host any more. A source or target share is read, fetched from and cleaned
with `smb-list`, `smb-get` and `smb-delete` (`common.smb`: `smbclient` on a Linux node, the UNC path
after `cmdkey` on Windows - one answer shape either way), and its login stored with
`smb-credential`; the certificate import on a Windows target is its PowerShell run by `run-cmd`
over WinRM, and on this machine a `run-sqlcmd`; CHECKDB (`verify-restore`) is a `run-sqlcmd` like
every other statement. What stays here is every decision - which files, which window, what is
obsolete - through `db_ops/backup_restore/share.py`, which only states each request. Two things
changed on the way: a dry-run cleanup on a Windows target deletes nothing (the PowerShell engine
took the flag and deleted anyway), and the certificate's password no longer rides on this
machine's command line inside an `Invoke-Command` script.

**How the app reaches the Linux target (0.24.0, rules R03).** `open_ssh_connection` returns a
`lib.remote_host.RemoteHost`, not a paramiko client: every command is `common.cli run-cmd`, every
file `push-file` / `pull-file`, each its own SSH session (about half a second). The app never
imports `common`; the Windows preflight's SMB share is a `run-cmd` over WinRM for the same reason.

Proven on the labs (2026-09-25): an encrypted LABTEST full backup restored under another name
on the `.250` SQL Server container through this path, *NN percent processed* streamed as it
went; a wrong SQL password answered *Login failed for user 'sa'*, and a deadline raised the
timeout the resume logic reads. The Linux path now logs its progress events too - only the
Windows and local ones did.

## Running db_ops itself on Linux (containerized) — SMB source reads

The transport table above assumes db_ops runs on **Windows** and reads the backup
source over a UNC path. When db_ops runs **on Linux** (the Docker image) it cannot
read a Windows UNC share directly, so the copy step reads the source through
`common.cli smb-list` / `smb-get`, which use `smbclient` there (validated end-to-end against a
containerized SQL Server 2025 target):

- The login travels in the request and reaches `smbclient` in an auth file `common` writes 0600 and
  deletes after the call (so a password containing `%` is not split by `-U user%pass`, and none is
  left in the temp folder). The share is **listed first** (`smb-list`, recursive), the listing is
  filtered here by `copy_file_patterns` and the copy window, and only the selected files are
  fetched, one `smb-get` each, each size checked against the listing. A `*.bak`/`*.trn` mask is
  **not** given to `smbclient`: with `recurse ON` it applies the mask to subdirectory names too and
  never descends into `FULL`/`LOG`.
- When `database_mappings[]` is configured, only those `<db>` subdirectories are fetched - and since
  0.24.1 the same holds on a **Windows** node, whose two copy paths (the UNC scan and `smb-list`)
  read the whole share until then: an entry mapping two of a production server's databases would have staged
  the third's 30 G full and a day of its logs into a lab VM with 17 G free (2026-09-27).
- A copy that finds nothing in its window now says what the newest matching file IS and how long
  before the window it was written - "selected no files" read like a window set too narrow, and
  it meant a share nothing had written to for eight days.
- `smbclient` does not preserve file mtimes, so each backup's real time is
  recovered from its filename (`..._YYYYMMDD_HHMMSS[Z]`). A trailing `Z` (what db_ops' own
  backup scripts write since 0.20.0) means UTC; a name without it, as other tools on the source
  server write, is read in local time as before (`shell_quoting.backup_time_from_name`). The log-chain selection
  filters logs by time relative to the FULL backup; without this the restore would
  apply pre-FULL logs and fail with `Msg 4326` (the log "is too early to apply").
- The staged files are then sent to the Linux SQL Server target (`common.cli push-file`,
  hash-checked at both ends), and the mtime is set to the source's.

For this path the source `backup_share` must point at the **instance** level (for
example `\\host\SQLBK\APPDB-DB$APPDB`) and `vm_import_linux_path` must **not** include
that level, so files land at `vm_import/<db>/FULL` — exactly where the restore looks.

### When the logs are not where the fulls are

A restore cannot do point-in-time from a backup set that has no logs in it, and a source does
not always write its logs next to its fulls.

`192.0.2.248` is the case. db_ops runs only a FULL job there (`ACME_MSSQL_2_248_ALL_FULL`,
writing `.bak` to `\192.0.2.248\SQLBK_DBOPS`); the `.trn` files come from a separate SQL
Agent maintenance plan writing to `D:\DBA\SqlBK\<instance>\<db>\LOG`. A PITR attempt on
2026-08-13 failed with **"No transaction log backups found"** — correctly: db_ops' set had none.

**The answer is a second entry, not a second mechanism.** `192.0.2.250` already shows the
pattern: `ACME_TO_MSSQL2025_DOCKER` reads that host's Agent tree (`\...\SQLBK\APPDB-DB$APPDB`,
where FULL and LOG sit together) and `ACME_MSSQL_DBOPS_TO_MSSQL2025_DOCKER` reads db_ops' own.
One share each, no special support. `ACME_MSSQL_2_248_AGENT_PITR_TO_LABSQL_2_116` is the same
split for 2.248.

Pointing the *existing* entry at the Agent tree instead would work, but it silently stops
verifying db_ops' own backups — which is what that entry is for. Two entries keep both answers.

Three things a second entry against the same source must get right:

- **Its own staging directory.** Two entries sharing `vm_import_linux_path` would mix two backup
  sets in one tree.
- **Its own target database names.** Both restore the same source databases onto the same
  instance; the PITR entry maps each to `<name>_PITR`.
- **An explicit `database_mappings` list.** `database_mappings: []` means "everything found under the import
  directory", and a long-lived maintenance-plan tree accumulates: 2.248's still holds
  `SALESDB_Prod`, `APPDB_Org`, `APPDB_Prod`, `APPDB_Testing` and `GLOBEX` with fulls from 2025 and
  ~190 logs each.

**Not every database is eligible.** Measured on 2.248 on 2026-08-13, the Agent tree had no log
for `APPDB` or `DtradeProduction` and no full at all for `APPDB`. Eleven databases had a complete
current chain; those are the ones the entry names.

**Do not close the gap by adding a LOG job to db_ops instead.** A log backup *truncates* the
log, so two independent log-backup jobs on one database each take the records the other did not,
and **neither set is a restorable chain**.

**PITR reach is the other job's retention, not db_ops'.** On 2.248 `Job_Maintain_Backup_LOG`
runs every 15 minutes with Ola Hallengren's `@CleanupTime = 2` — that parameter is in **hours**,
which the FULL job's `@CleanupTime = 48` confirms (exactly two daily `.bak` survive). About
8 hours of `.trn` were on disk when measured. It is a sliding window of hours; raising
`@CleanupTime` on the source is the only thing that widens it.

#### Sub-paths deeper than one segment

A share whose backup root is *below* the share itself is addressed with a sub-path
(`\host\SQLBK\APPDB-DB$APPDB`). Staging strips that sub-path off each listed file to make the
path relative, and what remains lands under the import directory.

Until 2026-08-13 the strip only worked for a **one-segment** sub-path: `_parse_unc_share`
returns it POSIX-separated (`a/b/c`) while `smbclient` prints backslashes, and a single segment
has no separator to disagree about. Every share in use had one, so the bug was invisible until
`\192.0.2.248\D$\DBA\SqlBK\EXAMPLE-SQL$SQLEXPRESS` — a maintenance-plan tree with no
share of its own, reached through the `D$` admin share. Nothing raised: the files copied
successfully, three directories too deep, and the restore reported no backups.

## Dockerized engines: backups and the container must be kept separate

**The rule: when a database runs in Docker, its backups live on a host path that has nothing to
do with the container — not inside the container, not inside the container's own directory, and
not inside any tree another tool manages.** A backup shares a fate with whatever it is stored
next to, and the whole point of a backup is to *not* share a fate with the database.

Three separations, each of which has already failed here once:

**1. Separate from the container filesystem.** A backup on the container's writable layer dies
when the container is recreated — and these are recreated routinely: an engine upgrade, a compose
edit, `sre create-db-docker --force` (which runs `docker compose down -v`). The host path is the
real location; the container path is only a window onto it.

**2. Separate from the database's own data volume.** Do not mount the backup *underneath* the
data volume's path. The postgres image declares `VOLUME /var/lib/postgresql`, so a backup mounted
at `/var/lib/postgresql/backup` sits **inside** the data volume — and if that bind mount is ever
missing, the path still exists and is still writable, so backups silently land in the database's
own volume. Backups and data then die together, in one `docker compose down -v`. Mount somewhere
the data volume does not reach: `pg_ha_01` now uses `/opt/pgbackup`, which is why a missing mount
would fail loudly instead of quietly writing to the wrong place.

**3. Separate from the db_ops deploy root (`/opt/db_ops`).** `sre create-db-docker` defaults
`--containers-dir` to `/opt/db_ops/containers/<name>/`, which is inside the tree `control deploy`
manages, and the deploy prepares that tree by re-owning it to the SSH user. A backup mount under
it gets re-owned along with everything else — taking write access away from the **database** user
inside the container. PostgreSQL runs as uid 999; the deploy user is uid 1000.

That third one failed on 2026-07-31 and is the reason this section exists. A deploy re-owned
`/opt/db_ops/containers/pg_ha_01/backup`, PostgreSQL could no longer create files in its own
archive destination, and `archive_command` failed on every WAL segment from that moment on. The
database stayed **healthy and fully available** — only recoverability was gone, and nothing said
so. The signature:

```sql
SELECT archived_count, last_archived_time, failed_count, last_failed_time FROM pg_stat_archiver;
-- archived_count frozen, failed_count climbing into the thousands, last_archived_time hours old
```

Both the trigger and the layout are now fixed: `control deploy` no longer recurses into
`containers/` (see the note in `control.deploy.copy_bundle`), and `pg_ha_01` was relocated to
`/opt/db_backups/pg_ha_01` → `/opt/pgbackup`.

### The current layout

| Host path (the real location) | Container path | Engine |
| --- | --- | --- |
| `/opt/db_backups/pg_ha_01` | `/opt/pgbackup` | postgres (`pg_ha_01-primary`) |
| `/opt/mssql2025/backup` | `/opt/mssql2025/backup` | sqlserver (`mssql2025`) |

Neither is under `/opt/db_ops`, and neither is under the engine's data directory.

### Auditing a host

```bash
for c in $(docker ps -a --format '{{.Names}}'); do
  docker inspect -f '{{range .Mounts}}{{if eq .Type "bind"}}'"$c"' {{.Source}} -> {{.Destination}}
{{end}}{{end}}' "$c"
done | grep -i backup
```

A line needs fixing if its **source** is under `/opt/db_ops`, or its **destination** is under the
engine's data directory (`/var/lib/postgresql`, `/var/opt/mssql`, `/opt/oracle/oradata`).

### Relocating one

A bind mount is fixed for a container's lifetime, so this recreates the container — plan it as a
maintenance action, and check first whether anything else lives in that container (the db_ops
runtime store itself runs in `pg_ha_01-primary`). Keep the host path on the same filesystem and
the move is an instant rename rather than a copy of the whole archive.

```bash
docker compose stop <service>
sudo mv /opt/db_ops/containers/<name>/backup /opt/db_backups/<name>
sudo chown -R 999:999 /opt/db_backups/<name>      # the DB user inside the container, by uid
# edit docker-compose.yml: - /opt/db_backups/<name>:/opt/pgbackup
docker compose up -d <service>
```

Then repoint everything that names the **container-side** path, or archiving breaks again in the
same silent way:

* `archive_command` (PostgreSQL) — `ALTER SYSTEM SET archive_command = '...'` then
  `SELECT pg_reload_conf()`. Verify with `SHOW archive_command` in a **new** session: the session
  that ran the reload still reports the old value and will fool you.
* `data/restore_config.json` — the `backup_dir` of that entry and its nested jobs. This is the
  container-side path, and entries for *other* hosts share the same string, so change only the
  ones whose `server_id` matches.

Confirm it worked by forcing a switch and watching the counters move:

```sql
SELECT pg_switch_wal();
SELECT archived_count, last_archived_time, failed_count, last_failed_time FROM pg_stat_archiver;
-- archived_count rising, and last_archived_time NEWER than last_failed_time
```

That last comparison is the same one `db_ops/common/backup_scripts/postgresql/pg_archive_wal.sh` uses to decide
whether archiving is healthy, so it is what the alert will report too.

## Backup-Encryption Certificate and the Database Master Key

Importing the backup-encryption certificate with its private key requires a
Database Master Key (DMK) in `master`. A freshly provisioned target has none, so
`build_add_certificate_sql` now creates the DMK automatically (`CREATE MASTER KEY`)
when it is missing, before `CREATE CERTIFICATE ... WITH PRIVATE KEY`. Existing
targets that already have a DMK are unaffected.

### By thumbprint, never by name (0.24.1)

SQL Server reads an encrypted backup with whichever certificate has the right **thumbprint**. Every
import - this app's SMB restore, `common.cli restore-key` for the script path, and the shell restore
- now sends one batch (`lib.sqlserver_certificate`): the thumbprint is the SHA-1 of the `.cer`,
read by the instance; one already there is left alone; a requested name that belongs to a different
certificate becomes `<name>_<first 8 hex digits>`; nothing is dropped. Before, the SMB restore
skipped the import when the name existed and the other two dropped the name and recreated it - and
`db_ops_backup_cert` is the default everywhere, so a target dbabrain also backs up either never
received the source's certificate or lost its own (2026-09-27, a lab VM).

### The pair beside dbabrain's own backups: `source.backup_certificate` (0.24.1)

Until 0.24.1 an SMB entry took its certificate only from `certificate_api_url` (Vault). dbabrain's
own SQL Server backup job exports its certificate beside the backups instead
(`<backup_dir>\_cert\<name>.cer` + `.pvk`, the key encrypted by the backup passphrase), so a share
of dbabrain's own backups could not be restored onto an instance that did not already hold it:

```json
"source": {"backup_share": "\\\\192.0.2.250\\SQLBK_DBOPS", "...": "...",
           "backup_certificate": {"name": "db_ops_backup_cert", "password_ref": "BACKUP_ENC_REF",
                                  "source_dir": "D:\\SQLBK_DBOPS\\_cert"}}
```

The pair is read from the share with the backups' own login, into memory, and its thumbprint is
computed here. **A pair exported before 0.24.1 is readable by the SQL Server service account only**
(the engine writes it so), and the share refuses it - then `source_dir`, the same folder as the
source host sees it, is read over that host's own login (`run-cmd`, the elevated session an
administrator gets). The Windows backup job now gives the pair its folder's permissions on every
run, so after one backup on 0.24.1 the share reads it and `source_dir` is not needed. On the target
the pair is written into the staging folder, imported, and removed again.

### `target.sql_container` - a target host with no `sqlcmd` (0.24.1)

Every `sqlcmd` the restore runs - each RESTORE, the recovery, CHECKDB, the certificate import - runs
inside that container (`common.cli run-sqlcmd`'s `container`) when the target names one. A lab VM
with only Docker has no `sqlcmd` of its own. The container must bind the staging folder at the
same path, so the staged path is the path RESTORE reads.

## SQLBK_IMPORT — What It Is and When It Is Created

`SQLBK_IMPORT` is the SMB share name on a Windows restore target VM. It must exist on the target
before the restore workflow can write backup files to `vm_import_unc`.

**Config example:**
```json
"vm_import_unc":   "\\\\198.51.100.129\\SQLBK_IMPORT\\ACME-192-0-2-250",
"vm_import_local": "C:\\MSSQL\\SQLBK_IMPORT\\ACME-192-0-2-250"
```

The workflow preflight (`preflight.py`) attempts to auto-create the share if it is missing:
1. Checks if `\\198.51.100.129\SQLBK_IMPORT` is accessible (via `os.path.exists`).
2. If not, runs PowerShell `Invoke-Command -ComputerName 198.51.100.129 ...` (WinRM port 5985) to:
   - `New-Item -ItemType Directory -Path 'C:\MSSQL\SQLBK_IMPORT' -Force`
   - `New-SmbShare -Name 'SQLBK_IMPORT' -Path 'C:\MSSQL\SQLBK_IMPORT' -FullAccess 'Everyone'`

If auto-creation succeeds the workflow continues. If it fails a `PreflightError` is raised with
the exact PowerShell commands needed to fix it manually.

**Linux targets do not use SQLBK_IMPORT or any SMB share.** They use an SSH path configured
as `vm_import_linux_path`. Directory creation happens automatically over SSH.

### Requirements for Windows targets

- Port 445 (SMB/CIFS) open between the machine running `db_ops` and the Windows restore VM.
- Windows Firewall → "File and Printer Sharing" enabled on the restore VM.
- Port 5985 (WinRM HTTP) open if auto-create is needed (also required for sqlcmd execution).
- `vm_username` must have at minimum Read+Write access on the `SQLBK_IMPORT` share.

To enable WinRM on the target Windows machine (run as Administrator once):
```powershell
winrm quickconfig
Set-Item WSMan:\localhost\Client\TrustedHosts -Value "*" -Force
```

## Event Shape: One Bracket Per Run, Steps Are Phases

Every run emits **exactly one `START` and exactly one terminal event** (`END`, `ERROR` or
`TIMEOUT`), under the command that names the operation. Everything inside it is a *phase* of that
same command, never a command of its own:

```
▶️  restore-workflow START
    restore-workflow COPY_START      <- plain: a step, not a run
    restore-workflow COPY_DONE       <- plain
✅  restore-workflow END
```

`START` maps to `started` and `END` to `success` in `message_type_for`; a step phase like
`COPY_START` is deliberately **not** in `_PHASE_TYPES`, so it falls through to the level and
renders plain. That is the whole mechanism — a step is quiet because it is unregistered, and a run
is loud because it is registered. Adding a step needs no change to the type map.

Four conventions used to coexist, and the same copy step appeared three different ways depending
on how it was invoked:

| Was | Now |
| --- | --- |
| `command="restore-latest.certificate"`, `phase="START"/"END"` — a step wearing ▶️/✅ like a run | `command="restore-latest"`, `phase="CERT_START"/"CERT_DONE"/"CERT_ERROR"` |
| `command="backup.timeout"` / `"restore-workflow.timeout"`, `phase="ERROR"` | the run's own command, `phase="TIMEOUT"` |
| `prune` emitted only `END` — "how many prunes ran" was unanswerable | `START` … `END`/`ERROR` |
| `restore-by-id` emitted **nothing at all** | brackets itself, like every other entry point |

**The two that were silent are the ones that mattered.** `restore-by-id` returns before the block
that brackets every other subcommand, so a restore driven by an operator or a Telegram command
reported nothing while the same restore through the scheduler reported four events. And
`COPY_START`/`COPY_DONE` lived in `run_script_restore`, which the scheduler stopped calling in
2.69.52 when it moved to `restore_by_id` — the transfer came across, the announcements did not, so
a remote drill went quiet for 41 minutes between `START` and `END`. Both are exactly the failure
these events exist to prevent: silence that cannot be told apart from a hang.

`restore_by_id` takes an optional `on_phase(phase, message, extra)`. It decides *what happened*;
the caller decides *who hears about it*. An emit that raises is swallowed — reporting is not the
restore.

## A Restore Verdict Can Never Be Greener Than Its Databases

A per-database failure is caught on purpose so one broken chain does not cost the other five
databases their restore. That deliberate tolerance is exactly what made the reporting lie: the
verdict was hardcoded.

```python
success_count = sum(...)          # computed
failed_count  = sum(...)          # computed, logged
...
"status": "DRY_RUN" if dry_run else "SUCCESS",   # and then ignored
```

On 2026-08-08 the 02:00 `ACME_TO_MSSQL2025_DOCKER` drill restored five of six databases, never
restored `APPDB_Prod`, and sent `✅ Restore workflow finished. status=done`. The database was left
in RESTORING and unreachable, and the only record of the failure was inside
`per_database_restore_status`, which nothing read. Fixed at all three layers that had the same
shape, because fixing one still left the next one green:

| Layer | Rule now |
| --- | --- |
| `run_restore_instance` (`restore_database.py`) | `SUCCESS` only when `failed_count == 0` |
| the multi-source aggregate (`cli.py`) | `SUCCESS` only when every source is `SUCCESS`/`DRY_RUN` |
| `run_scheduled_restores` (`workflow.py`) | "did not raise" is not success — the engine verdict **and** the per-database map are both checked, and the ERROR names the databases |

Per-database status is only ever `SUCCESS`, `SUCCESS_RESUMED`, `FAILED` or `DRY_RUN` (`SKIPPED` is
a *step* status, never a database one), so this cannot turn a healthy run red.

## Restore SQL Behaviour: RESTORING State Guard

When `run_restore_full` generates the RESTORE DATABASE SQL, it first checks whether the target database needs to be switched to single-user mode. The generated guard is:

```sql
IF DB_ID(N'<db>') IS NOT NULL
    AND DATABASEPROPERTYEX(N'<db>', N'Status') != N'RESTORING'
BEGIN
    ALTER DATABASE [<db>] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
END;
```

The `DATABASEPROPERTYEX` check was added to handle the case where a previous run left the database in RESTORING state (i.e. `RESTORE DATABASE … WITH NORECOVERY` completed but `RESTORE WITH RECOVERY` never ran). A database in RESTORING state has no active connections, so skipping `SET SINGLE_USER` is safe. Without this guard, `ALTER DATABASE` raises an error and the restore is aborted.

The container-to-container drill (`db_ops/common/restore_scripts/sqlserver/mssql_restore.sh`) needs the same guard
and now carries it, written against `sys.databases.state` (`0` = ONLINE) since that script drops and
rebuilds each database rather than restoring over it:

```sql
IF DB_ID('<db>') IS NOT NULL
BEGIN
    IF (SELECT state FROM sys.databases WHERE name = '<db>') = 0
        ALTER DATABASE [<db>] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE [<db>];
END
```

Without it the drill could not recover from its own failures: a run that died between NORECOVERY and
RECOVERY left the database in RESTORING, and because `run_sql` uses `sqlcmd -b`, the rejected
`ALTER DATABASE` failed the *next* run too — so every later drill inherited the wreckage of the first.

## Which Backup File the Drill Picks (`pieces()`)

The container drill has no backup catalog: it finds each database's FULL/DIFF/LOG chain by listing
`<backup_dir>/<db>/<LEVEL>/` and ordering the names, so **the naming convention is the chain
metadata**. A piece is therefore eligible only when its name is exactly what the backup job writes,
`<db>_<LEVEL>_<YYYYMMDD>_<HHMMSS>.<bak|trn>`; `pieces()` enforces that with a single regex and
everything downstream consumes its output.

The rule is narrow because both ways of loosening it silently restored the wrong thing, and both were
live on the CLOUD lab at once:

- **A file belonging to another database.** A stray `test_db_01_FULL_01.bak` left in
  `mssql_ha_db/FULL/` sorted after every `mssql_ha_db_FULL_2026*.bak`, so `sort | tail -1` handed
  RESTORE a *different database's* backup. It restored happily — as `mssql_ha_db`, with data files
  named `test_db_01.mdf` — and the genuine `test_db_01` restore then failed with
  `Msg 1834: the file '/var/opt/mssql/data/test_db_01.mdf' cannot be overwritten. It is being used
  by database 'mssql_ha_db'`.
- **A file with the right prefix but no timestamp.** `test_db_01_LOG_01.trn` survived the
  stamp-extracting `sed` unchanged, so the "is this log newer than the FULL?" comparison tested the
  literal filename against `20260805_085205` — which any letter wins — and a pre-FULL log was always
  selected, failing with `Msg 4326` ("the log in this backup set terminates at LSN …, which is too
  early to apply").

Note what the drill does *not* do: it never asks SQL Server what is inside a file (`RESTORE
HEADERONLY` / `FILELISTONLY`). Reading the header would be authoritative where the filename is only a
convention, at the cost of one round trip per candidate file. That is a reasonable future change; the
name rule is what is implemented today, so a file that does not follow the convention is ignored
rather than guessed at.

## A Physical Restore Brings the Source's Logins With It

Oracle RMAN duplicate and PostgreSQL `pg_basebackup` restores are *byte* copies, so the restored
instance carries the **source's** users and passwords — for PostgreSQL, `pg_authid` and everything in
it. Two consequences bite immediately after a drill and neither is a failure of the restore:

- **The target's own credential in the secret store goes stale.** After `CLOUD_PG_TO_CLOUD2`, the
  restored `pg_ha_cloud2-primary` no longer answers to `POSTGRE_203_0_113_121_POSTGRES`; it answers
  to `POSTGRE_203_0_113_188_POSTGRES`, the source's. Verified by comparing `md5(rolpassword)` from
  `pg_authid` on both hosts — identical for `postgres` and for `replicator`.
- **Replication on the target stops.** The target's standbys still present *their* old `replicator`
  password to a primary that now expects the source's, so the log fills with
  `FATAL: password authentication failed for user "replicator"`. The cluster is a single restored
  node until it is rebuilt, which is what the `restore_config.json` note for these entries means by
  "losing its replication is acceptable".

So a drill is verified against the *source's* credentials, and "cannot log in to the target with the
target's stored password" after a restore is the expected outcome, not a finding.

## What `RESULT=ok` Proves — Liveness Is Not Enough

A script restore (Oracle, PostgreSQL) is only reported `done` when the script exits 0 **and** prints
`RESULT=ok`; `run_script_restore` requires both. What that line is allowed to mean has been
tightened, because the original checks were satisfied by a target that had not been restored at all:

| Check | What it rules out | What it does **not** rule out |
| --- | --- | --- |
| engine answers a query | a dead instance | yesterday's restore still running |
| Oracle `open_mode = READ WRITE`, `dba_objects > 0` | an unopened or empty database | a DUPLICATE that silently did nothing |
| PostgreSQL `relations > 0`, not in recovery | a cluster stuck in recovery | an empty cluster — `initdb` alone reports 415 relations |

On a lab whose source cluster holds no user tables, `databases=1 relations=415` was printed by every
successful drill *and* would have been printed by a restore that did nothing. The drill could not
tell the two apart, and would have reported success every day either way.

Each script now also has to prove the database in front of it **was produced by this run**:

- **Oracle** — RMAN DUPLICATE ends in OPEN RESETLOGS, so `v$database.resetlogs_time` is the moment
  this run created the database. It must fall inside this run's own window (elapsed seconds plus a
  15-minute margin). The arithmetic is done inside Oracle against `SYSDATE`, never against the host
  clock: the container's timezone is not the host's, and comparing the two drifts the check by hours.
- **PostgreSQL** — two facts from `pg_controldata`. The **system identifier** of the running cluster
  must equal the one in the chain's FULL backup directory (a base backup is a byte copy, so it
  carries the source's identifier, and `initdb` mints a new one) — read from the backup itself, so no
  access to the source host is needed. And **`Time of latest checkpoint`** must be at or after the
  newest piece in the restored chain.

Both failures are hard: a drill that cannot prove freshness reports `error`, never a pass. If the
control file or `resetlogs_time` cannot be read, that is also an error — "unproven" must not render
as "proven".

Note the asymmetry with the SQL Server (non-script) path: there `status = "done"` means
`run_restore_workflow` did not raise, and the engine's own verdict is carried separately as
`output_status`. The two are not the same thing.

## Every Message Names Its Run (`backup_id` / `restore_id`)

Every backup/restore message — file log, `job_runs` row, and Telegram — carries the id of
the run it belongs to. Without it an alert cannot be tied back to a config entry, a run row
or a log file, which is the difference between an actionable message and noise.

This is enforced in one place, `emit_backup_restore_event` in `backup_restore/events.py`,
so no call site can publish a message without one:

- **Which id.** `backup` commands are keyed by `backup_id`; everything restore-side
  (`restore-latest*`, `restore-workflow`, `copy-backup`, `delete-backup`, `verify-restore`)
  by `restore_id` — `events.run_id_key(command)`.
- **Where it looks.** `restore_id` → `restore_ids` → inside the per-entry lists a
  multi-entry workflow carries (`mappings`, `per_restore_results`). De-duplicated, joined
  with commas for a multi-entry run.
- **Where it appears.** In the message text itself (so the `job_runs` row and the log line
  carry it), then on its **own line** in the Telegram body — an id living only inside the
  JSON payload would land in whichever part of a long message it fell into.
- **What the payload carries.** The run's *result*, not its work (`events.telegram_metadata`):
  each source's verdict and per-database statuses and errors, and the file lists as counts -
  never the statements or the files themselves, and never more than
  `MAX_TELEGRAM_PAYLOAD_CHARS`. The whole output stays on the run's `job_runs` row. A
  13-database `restore-latest` END once carried everything, 181,174 characters in 49 parts,
  and the Telegram workflow timed out sending it (2026-09-26). `telegram_send_messages.source_id` is
  `<command>:<id>`, so a queued row is traceable without parsing the text.
- **When it is missing.** The event says `restore_id=<unknown>` rather than omitting it.
  Silence is what let this go unnoticed: a message with no id looked perfectly normal.

## Per-Job Telegram Routing (`notify`)

`notify` is a **shared config object**, not a backup_restore invention: the same shape SQL
targets have always used, owned by `db_ops.lib.notify` the way `time_window` is owned by
`db_ops.lib.time_window`. The full contract is in
[`docs/13_common.md`](./13_common.md); this section covers
where backup_restore puts it.

```jsonc
"notify": {
  "logging_on_run": { "enabled": true, "telegram_chat": "", "chat_id": "" },
  "alert_on_error": { "enabled": true, "telegram_chat": "", "chat_id": "" }
}
```

| Placed on | Applies to |
| --- | --- |
| a `backups[]` entry | every sub-job of that entry (the default) |
| a `backups[].jobs[]` sub-job | that job only — **merged rule by rule over the entry's object**, so a job overrides one rule without restating the other |
| a `restores[]` entry (SQL Server or script-driven) | that restore entry's events |

Severity picks the rule: `warning`/`error`/`critical` → `alert_on_error`, everything else →
`logging_on_run`. `telegram_chat: ""` (the default) means "follow the event's own severity",
so adding the object changes no destination until a rule is actually set.

Rules worth repeating here:

- **The node gates, the entry narrows.** A `notify` object can silence or redirect; it can
  never switch on a level the node switched off (`common/notify_route.notify_chat_id`).
- **Silence is an alerting choice, not a logging one.** The file log and the `job_runs` row
  are always written. A silenced job is still fully recorded and still shows up in reports.
- **A multi-entry `restore-workflow` event** switches a rule off only when every selected
  entry switches it off — one entry's preference must not delete a message another entry is
  waiting for (`config.merge_notify_configs`).

**As shipped:** the object sits on each backup **sub-job**, because the right answer differs
between jobs of the same entry. A `database` job (base/full backup) runs once a day and
reports every run; the `archivelog` / `wal` job beside it runs every 15 minutes and has
`logging_on_run.enabled = false` so it does not drown the group. `alert_on_error` stays on
everywhere, so a failed backup — and a timeout — still alerts.

## The Oracle Drill Aborts Its Target Instead of Shutting It Down Politely

`DUPLICATE ... BACKUP LOCATION` needs the auxiliary instance in NOMOUNT, so the drill takes the
target down first — with `SHUTDOWN ABORT`, deliberately:

- **A restore target is the one instance whose clean shutdown buys nothing.** The next statement
  rebuilds the whole database from the backup set.
- **`SHUTDOWN IMMEDIATE` is not bounded.** It waits for the PDBs to close, and on 2026-08-05 that
  wait never ended: the alert log reached `alter pluggable database all close immediate` and went
  silent, `sqlplus` sat at 0 s of CPU for half an hour, and the run moved again only after an
  operator issued `shutdown abort` from a second session.
- **Nothing would have rescued it unattended.** The `|| true` on that line covers a shutdown that
  *fails*, not one that *hangs*; and `time_window.timeout` marks the `job_runs` row TIMEOUT without
  killing the shell running the script (measured: a hung transfer was reaped at 7243 s and kept
  running regardless). On the entry's own 02:00–05:00 window that is a drill that hangs until
  morning, every time the target lands in that state.

`tests/test_oracle_restore_shutdown.py` guards it, including a regression test that fails if
`SHUTDOWN IMMEDIATE` is reintroduced anywhere in the script.

## The Drill's Own Machines Must Be in the Monitoring Estate

A restore drill depends on two hosts having disk, and neither is a database the estate was watching.
Until 2026-08-05 both were invisible: `CLOUD2-203-0-113-121-HOST` had `metrics.enabled: false`, and
the source host had **no `*-HOST` record at all** while every CLOUD database instance sets
`disabled_collector_types: ["cmd"]`. So `OS_DISK_USAGE` had never run on either machine. The target's
disk reached 100 % with nobody informed, and the backup directory on the source grew to 90 GB the
same way.

Both now carry a host record with OS collectors on, modelled on `ACME-192-0-2-249-HOST`. That is
the whole fix — the estate already had the machinery (`OS_DISK_USAGE` hourly at WARN 85 % /
CRITICAL 95 %, plus `capacity_policy.json` `days_to_full` forecasting at 90/30 days). Nothing was
built; the drill's hosts were simply outside it.

**Do not "clean up" these records by turning metrics off again.** A drill whose hosts are unmonitored
fails at 3 a.m. as a hung transfer, which is the most expensive shape a failure can take here.

### A restore names both machines by `server_id` (since 0.22.0)

Every restore entry carries `server_id` (the machine the backup is read from) and
`target_server_id` (the instance it is restored onto), on the entry itself. The script-driven
entries always did; the SQL Server engine entries put an `id` inside `source` and `target`, and
three of those `target.id` values were labels with an address baked in (`LABSQL-DOCKER-…`) that
matched nothing in `db_instances.json`. `source` and `target` now hold connection details only;
`source.id` / `target.id` are still read, and lose to the entry's own ids when both are present.

`check-references` holds both ids to the inventory (`restore source -> instance`,
`restore target -> instance`). A target nobody monitors is registered **inactive** —
`instance-add` with `"active": false` — rather than named by a label: the restore then points at a
record that says what the machine is, and switching it on later is one field.

### Nothing about the target is taken from the source (0.25.0 review)

A script-driven entry states its target in full, or it does not load:

| Entry | Required |
| --- | --- |
| every entry | `server_id`, `db_type`, `target_server_id`, `backup_dir`, `script` |
| target on another machine | `target_backup_dir` (its own, ≥ 3 levels deep, not shared or nested with another entry on that target), `source_backup_host_dir` |
| target on the source's machine (`target_server_id` = `server_id`) | `target_container`, `target_visible_dir`; SQL Server also `env.MSSQL_PORT` |

**An entry that is switched off does not stop the others (0.26.0).** The table is enforced on an
**active** entry, and an active entry that fails it refuses the whole file - it is about to run.
An entry with `"active": false` runs nothing, so one that fails the table is kept out of the list
instead (`ScriptRestores.unusable`): `list-restores` shows it under *Inactive and incomplete* with
what it lacks, asking for it by id (`restore-workflow --restore-id`, `/spbot_restore`) answers with
that reason, and every other entry loads. Until then one retired drill with no `target_server_id`,
or a two-level staging folder, stopped every script-driven restore on the node - the scheduled
pass, the listing, and registering a new entry. Where an active entry and an inactive one share a
staging folder, the inactive one yields. An entry being registered (`restore-add`) is held to the
table whether it is active or not. A duplicate `restore_id` is still refused for the whole file.

Optional, on an entry whose target is another machine: **`copy_mode`** - `auto` (the default: one
`tar` stream, and file by file over SFTP when tar cannot be used on either end), `tar` or `sftp`.
The copy's answer and the `COPY_DONE` message say which way ran; pinned to `tar`, a stream that
cannot be used fails the copy instead of taking hours file by file. Any other value is refused when
the entry is read (0.26.0, owner decision G4 - `docs/13_common.md`, *A fallback in how*).

The SMB-path drill (`restore-workflow`, `restore_config.json`) compares its hosts the same way
since 0.26.0: the source's `credential_target` and share host against the target's
`credential_target`, import share and `restore_sql_instance_on_vm` are one machine when the strings
match **or** a resolved address is shared - `\\PRODSQL\...` against `192.0.2.50,1433` passed while
both were the production server (review 0.25.0, B4.6). Loopback never counts (`localhost` is the
target VM), and a name that does not resolve is compared as a string, as before.

Before any step, `restore_by_id` refuses a target that is the source instance: the same machine
(by name or resolved address) with the same container or none, or - for SQL Server - the source's
port. An entry with only `target_container` used to mean "a container on the source host", and the
SQL Server plan then connected to the source's own port and ran `RESTORE ... REPLACE` there.

**The staging directory is marked.** The copy mirrors the source and the prune deletes by age, so
both act only on a directory holding `.dbops-staging`. An empty directory is marked on first use, and
a staging folder from before 0.25.0 is adopted by itself: it shares files with its source, so the
copy marks it and goes on. Only a directory holding files the source does not have, with no marker,
is refused (nothing copied, nothing deleted), with the `touch` command that marks it.

**An unknown chain is not copied whole.** A PostgreSQL source with no `_FULL`, or an Oracle source
whose RMAN preview names no piece, fails the transfer with the reason.

## The Transfer Copies the Chain, Not the Backup History

A drill needs the pieces it will actually restore from, and nothing else. How that set is decided
differs per engine, because the engines state it differently — and the difference is deliberate:

| Engine | How the chain is decided | Implemented in |
| --- | --- | --- |
| PostgreSQL | From the directory layout: newest `<stamp>_FULL`, every `_INCR` sorting after it, and `wal/` whole | `_postgresql_chain_include` |
| Oracle | **By asking RMAN**: `RESTORE DATABASE PREVIEW` names the datafile pieces, then the catalog returns every piece recorded from that level 0 onward | `_oracle_chain_include` |
| SQL Server | Not narrowed — the copy is already small, and the restore script picks its own chain per database | — |

**Oracle must never be narrowed by reading file names.** An RMAN directory is flat and its chain is
a property of the catalog, not of `FREE_L0_<date>_...`; deriving it from names is a second, weaker
copy of logic RMAN owns, and being wrong does not fail loudly — `DUPLICATE` restores to whatever
point the pieces present allow, so a chain missing its incrementals still reports success, just at
an older point than the operator believes. Every failure path therefore falls back to copying the
whole directory: un-narrowed costs bandwidth, wrongly narrowed costs the restore.

Measured on the CLOUD lab (2026-08-05): 453 files / 17 GB in the directory, **61 pieces / ~5 GB**
in the chain. Before an RMAN retention sweep that same directory was 90 GB, of which ~10 GB was
controlfile autobackups alone — one every 15 minutes at ~45 MB, for a database whose data is 3.5 GB.
Since the restore script also `docker cp`s the staged directory *into* the target container, the
drill needed roughly twice the directory in free space, and at 90 GB it stopped fitting on a 193 GB
disk at all.

### The container has to be able to open the path, not just the host

The chain is **listed on the target host** and **read inside the target container** — two namespaces
for one directory. When a volume serves the staging path the two are the same directory and there is
nothing to do; when none does, the pieces must be copied in (`docker cp`) before anything reads them.

The shell script has always done this (`stage_into_target`). `db_ops.common.restorestep.postgresql`
did not when the scheduled restore moved onto the common primitives in 2.69.52, and the failure was
a quiet one: `pg_ha_cloud2-primary` still held a copy an older script-driven run had left behind, so
the combine read the stale pieces happily and died only on the one that was new —
`pg_combinebackup: could not open file ".../20260807T180432Z_INCR/PG_VERSION"` for a directory
sitting on the host in plain sight. Four nights of `CLOUD_PG_TO_CLOUD2` failed that way before
2.69.67. Two rules make it safe:

- **A path a volume already serves is never copied and never deleted.** The "previous copy" that
  would be cleared first is the source backups themselves.
- **A previous copy is replaced, not reused.** `docker cp` into an existing directory *nests* it, and
  keeping the old one pins the restore to whatever was staged first — pieces deleted at the source
  stay and get read hours later.

The WAL directory has the same need: `restore_command` is run by the *server*, inside the
container, so a configuration pointing at a host path produces a cluster that starts and silently
replays nothing.

**Since 0.23.0 a PostgreSQL restore into a container copies nothing in.** The throwaway container
that combines the chain (see the drill below) mounts every path no volume serves **read-only at the
same path**, and copies the WAL into the target's data volume itself - no copy to go stale, nothing
that can write to the backups. `docker cp` staging, under the two rules above, remains for an Oracle
duplicate's backup location and for a separate LOG step into a running container.

### A SQL Server drill onto a fresh machine (0.23.0)

The first cross-machine SQL Server drill between two labs (2026-09-24: encrypted FULL/DIFF/LOG
from one lab host restored into another) failed six ways before any data arrived. What holds now, each measured on that drill:

- **The target folder is created before it is read.** The transfer lists what the target already
  has; a folder a first run had not made yet counted as "could not be read by the SSH user", so
  every new cross-machine drill failed its first run.
- **A source folder that is not there is said to be missing, not unreadable (0.25.0).** A restore
  scheduled before its lab's first backup was told the SSH user could not read
  `/opt/db_ops/backup/<lab>` - a folder no backup had created yet. The copy now answers *does not
  exist on that host - no backup has been written there yet*; a folder removed while it is being
  listed (retention) is named as such; only a folder the SSH user really cannot read is a
  permission error. All three still stop the copy.
- **The staged pieces are opened to the engine** (`chmod -R a+rX`, no sudo - the SSH user owns what
  it just wrote). SQL Server reads them as its own user (uid 10001 in the image), and a copy left at
  the source's `0660` gave `Operating system error 5` (Msg 3201) on every piece.
- **The backup certificate is imported before the backups are listed.** `RESTORE HEADERONLY` cannot
  read an encrypted backup without it (Msg 33111), and the import used to be queued behind the
  listing that needed it. It is idempotent (drop and recreate), so every run does it; a dry run
  does not, and may fail its listing for that reason.
- **A backup that cannot be read is named, not skipped.** Only a file SQL Server says is not a
  backup (Msg 3241-3243 - the exported certificate beside the set) is skipped; anything else fails
  the listing with the file's path. Both faults above used to read *no databases found*.
- **The plan connects to the target's own port** - `env.MSSQL_PORT`, or the target's own
  `db_instances.json` record. It was always 1433, which on a host running a 1433 and an 11433 lab is
  the other instance. Since the 0.25.0 review there is no default at all: a target whose port is not
  stated is refused, and so is the source's own port on the source's machine.
- **A failed restore says why.** The reason is appended to the event message and shown as
  `error_text=` under *Restore workflow FAILED.*; before, it was only in `job_runs.error_text`.

The source side needs two things of the backup job, both now in `mssql_backup_database.sh`: the
backup folder writable **by the engine** (taken over as root inside the container when it is not -
a lab's bind mount belongs to the SSH user), and the pieces relaxed to `a+rX` after every run, as the
Oracle and PostgreSQL jobs always did, because the transfer reads them as the SSH user and its
`sudo chmod` is silent where sudo wants a password. Every backup script also checks that its
container is **running**, not only that it exists: a stopped one used to read *no sqlcmd found;
install mssql-tools*.

A cross-machine drill needs its source's backups on a host path (`source_backup_host_dir`). A lab
built by `create-db-docker` has one when it was given `--backup-mount` (SQL Server by default), HA
labs included, and is then backup-ready as built: its folder is `<mount>/<lab name>` - see
`docs/10_sre_app.md`.

**A piece that cannot be read as a backup** - a `.bak` / `.trn` SQL Server calls *not a backup*
(3241-3243), such as a truncated newest FULL - is passed over and the restore uses the newest chain
it can read, but it finishes **done with a warning** naming the file, at `warning` level; it used to
say nothing and go back in time silently.

### A PostgreSQL drill onto a lab machine (0.23.0)

The same drill for PostgreSQL, onto a host reached as a docker-group user whose sudo asks for a
password, found five more; each is fixed and was measured on it:

- **The WAL is replayed.** The recovery configuration (`restore_command`, `recovery.signal`) is
  written into the combined directory **before the container's first start**, the WAL is copied
  into the data volume (`<staging parent>/dbops_wal`) by the same step, and the step waits until
  `pg_is_in_recovery()` is false. It used to be a separate step run after the server had already started on the combined
  data: no WAL was replayed, every PostgreSQL drill stopped at its last base or incremental backup
  and reported verified, and the `recovery.signal` left behind would have replayed the source's WAL
  onto the diverged cluster at its next restart. A container target has no separate LOG step now.
- **The target's SSH password travels** in the host block when it logs in by password (the
  key-login cloud hosts never showed the gap).
- **`sudo` is a fallback, not a prefix**: plain `docker` when the SSH user may run it,
  `sudo docker` only when it may not (`db_ops.lib.shell.docker_cli`) - a sudo that asks for a
  password cannot be answered.
- **The target need not be running.** It is stopped first, and the combine and the data swap run
  in throwaway containers (`docker run --rm -u postgres --volumes-from <target>`, the target's own
  image, its own paths) - never `docker exec` into it, never as root on the host against Docker's
  volume folders. The combine used to run inside the live target, so a drill that failed once and
  left it crash-looping made every later restore fail on *Container ... is restarting* until it
  was repaired by hand. The target is started again whether or not a step failed, and a failed
  step swaps nothing.
- **The backup jobs take their folder over**: when the folder a PostgreSQL or Oracle job writes is
  not writable by the database's user, it is made as root where the database runs and handed over,
  recursively - as the SQL Server job does.

A PostgreSQL or Oracle lab built with `--backup-mount` is backup-ready as built - `archive_mode`,
`archive_command` into `<backup_dir>/wal` and `summarize_wal` for PostgreSQL, ARCHIVELOG for Oracle
(`docs/10_sre_app.md`). One built without it, or before 0.23.0, is not: those are the DBA's to set,
and the WAL job says so (*archive_mode is 'off'*) until they are.

**Oracle, the same drill:** RMAN reads the `BACKUP LOCATION` inside the target container, and a
location copied to the host and served by no volume was invisible to it - *RMAN-05579: CONTROLFILE
backup not found*. The duplicate now stages it in first, by the PostgreSQL step's rule (a path a
volume serves is left alone; otherwise it is copied in, replacing a previous copy). Measured: the
duplicate restores everything up to the last archived-log backup, and nothing after it.

### Point in time, drilled on every engine (0.23.0)

`/spbot_restore <id> "<moment>"` (`restore-workflow --point-in-time`) was drilled on the labs for each
engine: a marker row written, the moment noted, a second row written, a log / WAL / archived-log
backup taken, then the restore. Every engine now keeps the first row and not the second. Before
this, none of them reached the moment:

- **The moment is read once, into the server's clock**: `db_ops.lib.restore.moment.server_clock_text`,
  naive `YYYY-MM-DD HH:MM:SS`, UTC unless the server's offset is stated. Every container target runs
  on UTC. A server on another clock needs the moment without an offset, in its own clock. The reasons:
  - SQL Server's `STOPAT` refuses an offset (Msg 3217), and so does Oracle's `TO_DATE`.
  - The backup listings cut every stamp to 19 characters and dropped its offset with it. A moment
    in +08:00 chose backups eight hours off. PostgreSQL's finish times come from `stat`, in the
    host's clock (+07:00 on the labs), and they were seven hours off even against a UTC moment.
- **SQL Server: the log that holds the moment is in the chain.** The chain used to stop at the last
  log finished *before* the moment, which leaves out the one containing it. With `STOPAT` on the
  previous log, the restore reached that log's end and reported success.
  `restore_by_id._logs_through` keeps every log up to and including the first one that finishes
  after the moment.
- **PostgreSQL's chain is read by name, and dated by its manifest.** The incrementals are every
  `_INCR` whose stamp sorts after the chosen full's, which is `pg_combinebackup`'s own rule. They
  used to be the ones that finished *after* the full, and on a staging copy those times are the
  copy's: a full and its incremental staged in the same second tied, and the incremental was
  dropped. A backup's time is its `backup_manifest`'s mtime, which pg_basebackup writes last and
  the copy keeps. The listing gives it in the host's clock (`finished_at`, which retention reads)
  and in UTC (`finished_at_utc`). A point in time is compared with the UTC one; against the
  host's +07 clock it chose an older full than it needed.
- **PostgreSQL promotes at the target.** `recovery_target_action` defaults to `pause`, which left the
  data right but the server still in recovery. The step waits for `pg_is_in_recovery()` to turn
  false, so it ran its 30 minutes and then failed. The configuration now says `promote`, and a
  replay that pauses anyway (`pg_is_wal_replay_paused()`) fails the wait at once.
- **Oracle: `SET UNTIL TIME` goes in a `RUN` block with the duplicate.** Outside one, RMAN refuses
  it (RMAN-03031). Every gvenzl lab carries the image's DBID and incarnation, so RMAN cannot tell
  two labs' pieces apart. Three things keep the duplicate to its own source's backups:
  - **`BACKUP LOCATION` ends in `/`.** Without the slash RMAN reads the path as a prefix:
    `.../ora_restore_from_249` also read `.../ora_restore_from_249ha`, the Data Guard lab's staging.
    The point-in-time duplicate then recovered through that lab's logs and asked for a sequence its
    own source never reached (RMAN-06054). The newest-point duplicate had got away with it.
  - **`NORESUME`.** RMAN otherwise resumes onto any datafile a failed earlier duplicate left, if it
    carries the right DBID.
  - **The nomount step removes `$ORACLE_HOME/dbs/arch*.dbf`.** Those are archived logs an earlier
    duplicate restored there. Media recovery would otherwise take a same-named file for this
    source's.
- **The staging copy mirrors the source** (next section): a piece from the source's previous life no
  longer sits beside the new ones.

## What a restore reports, step by step (0.23.0)

Every restore that runs through `restore_by_id` reports each step as it starts and ends. That
covers PostgreSQL, Oracle and container SQL Server, whether the scheduler, `/spbot_restore` or
`restore-by-id` started it. Each step is its own `common.cli` command:

| Event | Step | `common.cli` |
| --- | --- | --- |
| `START` | the run | - |
| `COPY_START` / `COPY_DONE` | the staging copy, remote restores only: pieces, bytes, already there, removed | `backup-chain`, `copy-backup-dir` |
| `METADATA_*` | instance logins, roles and Agent jobs before the databases; an entry without `server_metadata` says why, once. The app states the instance's `connection`, the instance `policy` and the bundle's `secrets` (`lib.data_sources.request_fill`) - `common.cli` reads no configuration | `sqlserver-replay-instance` |
| `RESTORE_START` / `RESTORE_DONE` | what goes in (`base backup X + 2 incremental(s), then WAL replay`, `APPDB: full X + 4 log(s)`, `RMAN DUPLICATE of FREE from ...`) and to which point; how long it took | `restore-full` / `-diff` / `-log` |
| `VERIFY_START` / `VERIFY_DONE` | whether the restored databases open: *N checked, M unusable* | `verify-restore` |
| `METADATA_*` | the post-database phase, only after a restore that worked | `sqlserver-replay-instance` |
| `DELETE_START` / `DELETE_DONE` | the staging folder past the entry's retention, only after a verify that passed | `prune-staged-backups` |
| `END` / `ERROR` | `restored=`, `verified=` and every `warning=` in the message | - |

Every one of these messages carries `restore_mode`: `LATEST`, or `POINT_IN_TIME` with
`point_in_time=` (as typed) and `point_in_time_utc=`. Until the .251 drill (2026-09-25) a
point-in-time run's messages all said `LATEST`, and only RESTORE_START named the moment.

A failure stops at the step it happened in, and the ERROR follows it: nothing is verified after a
failed restore step, and nothing is deleted or replayed after a failed verify.

Until 2026-09-25 a PostgreSQL drill in Telegram read *started*, *copy started*, *copy finished*,
*finished status=done*, and nothing between or after:
- no restore or verify event;
- the staging cleanup ran inside the copy, before the restore, and even for a drill that then failed;
- no instance metadata was replayed on this path at all;
- END never showed what was restored, or the warning a *done with a warning* was raised for.

The same day also changed two refusals. A refused unknown id used to be named `backup_id=<unknown>`;
it now carries the id that was typed.

## Host-to-Host Transfer: Failures Must Surface, Not Stall

The copy from source host to target host is one `tar` stream through the orchestrator
(`common.cli copy-backup-dir`, `db_ops/common/backup_copy.py` - `transfer.py` in this app until 0.23.0),
which makes it fast and makes its failure modes quiet — there is no per-file round trip in which an
error can surface. Two guards exist for that, both added after a run hung for its full two-hour
timeout with nothing to show for it:

- **The target directory is probed for writability before anything streams** (`_assert_writable`).
  A staging directory the SSH user cannot write is invisible to every other check: it lists fine and
  simply refuses every file. Observed on CLOUD2 when `/opt/db_ops/ora_restore_stage` was recreated
  with `sudo` and came back `root:root` while the SSH user is `ubuntu` — the source pushed 8.25 GB
  into the pipe, the target extracted **zero** files, and neither end reported anything.
  **When clearing a staging directory by hand, restore its ownership** (`chown ubuntu:ubuntu`), or
  the next transfer fails the preflight.
- **Both ends' stderr are drained by a thread while the copy runs** (`_drain`). They used to be read
  only after `recv_exit_status()`, which cannot be reached while the transfer is still in flight — so
  a `tar` complaining once per file filled its stderr window, blocked on the write, stopped reading
  stdin, and seized the whole pipeline with no error anywhere and no timeout to break it.
- **The copy is a mirror (0.23.0).** Before anything is copied, a staged file the source no longer
  has is removed and counted in `removed_absent_at_source`. The copy used to only add. A source
  rebuilt under the same name left its previous life's pieces beside the new ones, and a restore
  could not tell them apart: the lab images repeat WAL names and Oracle DBIDs. What counts is the
  whole source listing, not the part `include` limits one run to, because an older point in time
  needs pieces a newest-chain copy skips.

**On duration.** Every byte crosses the orchestrator twice (SFTP down, SFTP up) because the two
database hosts are not assumed to reach each other. Measured on the CLOUD → CLOUD2 pair:
**~1.5 MB/s**, so a full 17 GB RMAN set takes ~3.3 hours. A repeat drill is minutes because
same-size files are skipped — the long run is the one that follows a wiped staging directory.
Note the transfer itself is **not** bounded by the entry's `time_window.timeout`; that timeout
governs the script step. A run longer than the timeout still completes, but the reaper will mark
its `job_runs` row `TIMEOUT` in the meantime.

## Retention (`cleanup_retention`)

**One field, one unit, both halves.** `cleanup_retention` is **seconds**, it is **mandatory** on
every restore entry and every backup job, and each side acts on it differently:

| Side | Where | What it prunes, and when |
| --- | --- | --- |
| backup | `backups[].jobs[].cleanup_retention` | the directory that job **wrote to**, after the backup |
| restore | `restores[].cleanup_retention` | what the restore **staged on the target**, after the restore |

It was two fields in two units until 2026-09-11 — `retention_days` on a backup job and
`target_retention_seconds` on a restore entry — which an operator asked about directly: *"why
both?"* One idea wearing two costumes reads as an inconsistency because it is one. Both old
spellings still load (days are converted, not guessed at) so a config written before this does not
stop working, but nothing writes them any more.

Seconds also removed a rounding nobody could see: the delete engine spoke hours and the configured
seconds were converted with `max(1, seconds // 3600)` on the way in, so any retention under an hour
silently became one hour. A setting the tool quietly replaces is worse than one it refuses.

`0` is a real value and means **no age gate** — not "keep everything". Every file becomes a
candidate and the chain rule alone decides: never the newest full, nor anything at or after it.

The restore side is the *target's* retention and has nothing to do with the source's: the source
decides how far back it can recover from, the target only needs enough to run its next restore.
Without it a staging directory holds everything the source still keeps that a restore ever copied.
The transfer removes what the source dropped (it did not before 0.23.0), but the source's own
retention can be far longer than the target needs, and the extra copies can fill the disk the
target restores onto.

| Entry | Value | Why |
| --- | --- | --- |
| `ACME_TO_MSSQL2025_DOCKER` | `86400` (1 day) | Daily FULL at the source; the copy step already takes a 24h window, so the import folder holds exactly what this run copied. |
| `CLOUD_*_TO_CLOUD2` | `691200` (8 days) | Default. |

**Why 8 days is the default, and why one number is enough.** The full backup is weekly, so the
newest full is never more than 7 days old and an 8-day cutoff always keeps it *together with every
incremental chained to it*. A shorter per-level rule (say full 8 days, diff 2, log 1) breaks
Oracle and PostgreSQL: their incrementals are **differential** — `BACKUP INCREMENTAL LEVEL 1`
without `CUMULATIVE`, and `pg_basebackup --incremental` against the most recent backup — so each
one needs every link back to the full. Deleting mid-chain leaves a set that looks present and
restores nothing. (SQL Server differentials are cumulative, so per-level ages would be safe there
— but one rule that is correct everywhere beats three that need per-engine reasoning.)

Over-deleting is recoverable rather than destructive: the next transfer compares against the
source and re-copies whatever is missing. A restore in between would fail loudly, which is why the
margin is deliberate rather than tight.

Pruning runs **after** the copy, never before — pruning first would delete files the run is about
to need and the copy would fetch them again over the same slow link.

`--delete-retention-seconds` on `workflow` / `restore-workflow` overrides it for one run, and
`delete-backup` takes `--retention-seconds`. Unset means "use each entry's own value", which is
now always something. The `--delete-hours` / `--hours` spellings still work and are multiplied by
3600 in the open (`cli._retention_override`), rather than the configured seconds being divided
down somewhere inside the workflow.

Whole days are still derived at the one edge that only speaks days: `RETENTION_DAYS` in the
backup scripts' environment. It is not a second setting — `BackupJob.retention_days` is a read-only
property over this one. The prune planner (`db_ops/lib/backupfiles_retention.py`) takes the seconds
as they are since 0.26.0: converted to whole days, anything under a day became 0 and 0 became the
14-day default, so a two-hour lab retention kept two weeks of backups without a word (review 0.25.0,
B4.4). Its answer names the window in the unit it was given (`window`: `"8-day"`, `"7200-second"`).

Both phases announce themselves to the store (`DELETE_START` / `DELETE_DONE`) with
`files_considered` / `deleted` / `skipped`. Until 2026-09-11 the cleanup wrote nothing at all: across
254 recorded workflow runs there was no row saying whether retention had ever pruned anything, and
`SUCCESS` covered both "pruned thirteen files" and "scanned nothing".

---

## Timeout: an Abandoned Run Is Closed and Reported

`time_window.timeout` on a backup job or restore entry does two things:

1. **Stale grace for the next due check** — `schedule.is_due` → `time_window.job_due`: a
   run still marked RUNNING is not restarted until its timeout has elapsed, so a live run
   is never doubled up against the same database.
2. **Reaping** — `schedule.reap_stale_runs`, called at the top of `run_backup` and
   `run_scheduled_restores` before the due check. Any row still RUNNING past its timeout is
   closed as `status=TIMEOUT` and pushed as a CRITICAL Telegram alert naming the run.

(2) exists because a run that dies *without raising* never reaches the code that reports its
own failure — the daemon killing the process at its own timeout, a container restart, an
OOM. Before it, the row stayed RUNNING forever and the operator saw the last step that
succeeded followed by silence. All entries are considered, not only the due ones, and a
stale row is still reaped after a newer run has overtaken it (it is no longer the latest row
for its `job_code`, but it is still open).

The reaper **does not stop anything**. Process control belongs to the daemon, which already
kills a command that overruns its own timeout; stopping a restore mid-`RESTORE DATABASE`
would leave the database in RESTORING and require manual recovery.

> **Keep the timeouts consistent.** A restore entry cannot outlive the app command that
> runs it. If `APP-BACKUP-RESTORE` in `app_commands.json` has `timeout: 7200`, an entry in
> `restore_config.json` with `timeout: 21600` never gets its 6 hours — the daemon kills the
> parent process at 2. The app command's timeout must be ≥ the longest entry timeout.

`timeout: 0` means never time out, the same convention `time_window` uses everywhere else.

## Restore Workflow Telegram Notifications

`restore-workflow` emits three Telegram events: START, END, and ERROR. Each message uses the format:

```
LEVEL|hostname|Short description.
restore_id=<id>
target_id=<id>
target_host=<host>
restore_mode=LATEST|POINT_IN_TIME
point_in_time_utc=<UTC timestamp>   (POINT_IN_TIME only)
point_in_time_original=<raw value>  (POINT_IN_TIME only)
```

The `LEVEL` field is `INFO` for START/END success, `ERROR` for END failure and for ERROR events. The `restore_mode` and `restore_id` are derived from the CLI arguments before the workflow starts and are included in all three event types, even when the workflow fails before a database is touched.

END messages additionally include a per-database summary when the workflow completes (successful count, failed count, total elapsed).

### A script drill onto another machine sends four, because the copy is long

A remote drill (`target_server_id` set) adds two boundary events around the transfer, so one run
reports **START → COPY_START → COPY_DONE → END**:

```
LOGGING|host|Restore CLOUD_ORA_TO_CLOUD2 (oracle): copy started CLOUD-...-ORA-1521 -> CLOUD2-...-HOST.
LOGGING|host|Restore CLOUD_ORA_TO_CLOUD2 (oracle): copy finished - 61 piece(s), 5368709120 bytes, 192 already present.
```

The copy is the long half — measured at ~1.5 MB/s across the orchestrator, so a full RMAN chain runs
into hours — and with only START and END the run was silent for all of it. "Still copying" and "hung"
then look identical from Telegram until the timeout reaper speaks, which it does only after the
entry's whole `timeout` has elapsed (7200 s on these entries). That is how a transfer that had
extracted *zero files* went unnoticed for two hours.

`COPY_DONE` states what actually moved (`copied` / `bytes_copied` / `skipped`). Those numbers are the
cheap check on the chain narrowing: a copy that skipped everything and moved nothing is what a
wrongly narrowed chain looks like from the outside.

Two properties are deliberate:

- **In-place drills stay at two events.** They share the backup directory through a mount, so there
  is no copy; announcing one would report work that never ran.
- **Announcing never fails a restore.** `_announce` swallows what the callback raises: a Telegram
  queue that is down is a bad reason to fail a restore that worked, and `job_runs` plus the file log
  still hold the record either way.

## Common Issues

- No backup file found: check source path, file age filter, source ID, and database name mapping in `restore_config.json`.
- Restore SQL is wrong: run `restore-latest --dry-run` first and inspect generated SQL/log output.
- Certificate problem: run `import-certificate --dry-run` and verify certificate config.
- Restore succeeded but verification failed: the workflow's `verify-restore` phase names each
  database that does not open; rerun `common.cli verify-restore` against it to see why.
- *N database(s) restored and recovered, but the integrity check (DBCC CHECKDB) failed*: the data was
  restored; the `DBCC CHECKDB` run on each database afterwards failed, and the database is recorded
  `CHECK_FAILED` (not `FAILED`). It still fails the run and holds back retention cleanup. Msg 1823 /
  7928 on a SQL Server container means the check could not create its internal snapshot on that
  volume. **`"checkdb": false` on the restore entry skips the check** (default `true`); the run then
  logs `dbcc-checkdb skipped … reason=checkdb_false_on_the_entry` and its step says so. Until
  2026-09-24 the check had no switch and a failed one was reported as *restore failed*.
- *[DB] exists and is ONLINE on this server; a full restore would overwrite it* (0.26.0): the
  database is ONLINE on the target and the entry does not say `"overwrite_existing": true`. A full
  restore `REPLACE`s the database after setting it `SINGLE_USER WITH ROLLBACK IMMEDIATE`, so an entry
  aimed at the wrong server threw out its users and overwrote their data with nothing asked (owner
  decision G2.10). The check runs inside the restore's own batch, before anything else; a database
  RESTORING from an earlier run, or absent, is restored as before. A drill that runs again over its
  own last restore states `"overwrite_existing": true` - SMB and script entries alike.
- *env.MSSQL_USER is not stated, so the restore logs in as sa* (a warning in the answer, 0.26.0): a
  script-driven SQL Server entry still logs in as `sa` when it names no login, and the next release
  refuses it (G2.11). State `"env": {"MSSQL_USER": "..."}`.
- PITR fails with "no log backups found": log backups are required in the import folder covering the target point in time; verify that log files were copied with `copy-backup` before using `--point-in-time`.
- PITR fails with "cannot parse point-in-time": use the exact format `YYYY-MM-DD HH:MM:SS +HH:MM` (space before the timezone offset, not a colon-less `+HHMM`).
- `--restore-id` not found: the value must match the `restore_id` key exactly (case-sensitive) in `restore_config.json`.
- `APP-BACKUP-RESTORE` fails with the whole error text `'prod_backup_share'`: the node runs a build from before 2026-09-11 and has no restore configured. Nothing is broken in the config; an upgrade reports it as "nothing configured" instead. Until then, copy the shipped empty `restore_config.json` into `data/`, or set the command `active: false` on that node. From 2026-09-11 the command ships **active**: with both lists empty it finishes `status=done` and does nothing.
- `[WinError 53] The network path was not found: \\host\SQLBK_IMPORT\`: the SMB share does not exist on the target Windows VM. The preflight will attempt auto-create via WinRM. If auto-create fails, see the `SQLBK_IMPORT` section above for manual fix steps and WinRM setup instructions.
- Ubuntu/Linux target gets `[WinError 53]` or UNC error: check `vm_platform` is `"linux"` in `restore_config.json`; Linux targets must not have a UNC `vm_import_unc` — use `vm_import_linux_path` instead.
- Database left in RESTORING state by a failed run: the next `restore-workflow` run automatically handles this — the RESTORING state guard skips `SET SINGLE_USER` and `RESTORE DATABASE WITH REPLACE` overwrites the stale database.

## Config Priority

The backup restore app resolves its config file using this chain:

1. `--config <path>` CLI argument.
2. `DB_OPS_BACKUP_RESTORE_CONFIG` environment variable.
3. `config.backup_restore.json` next to `config.json`, or in the current working directory.
4. `config.json` shared fallback.

The selected source is printed to stderr on startup. The restore source definitions (`backup_restore` section) are read from the same resolved config file.

App-specific config file: `config.backup_restore.json`

## Standalone Mode vs Full-Suite Mode

**Full-suite mode** (default): the app reads `config.json`, writes to the shared runtime store, and emits structured job events visible to the jobs daemon.

**Standalone mode**: copy `config.backup_restore.json` next to the EXE. The file must contain both the shared config keys and the `backup_restore` source definitions. Point the store at a local path - a standalone EXE is the one layout where `sqlite_path` is still the natural setting, because it has no shared server. No other app needs to be running.

Required config keys: `log_dir` and a resolvable runtime store. The `backup_restore` section may be empty: `workflow` then has nothing due and exits 0, and a manual restore command says that no entry is configured.

## Optional Integrations

The backup restore app has no optional integrations. It writes `backup_restore_history` and `job_runs` events to the runtime store and exits. No other sub-app is called or depended upon.

## EXE Packaging Notes

- Network share paths (`prod_backup_share`, `vm_import_unc`) must be accessible from the machine running the EXE.
- `sqlcmd` must be on `PATH` for restore and verify commands.
- Certificate API calls require network access to the configured certificate endpoint.
