# Backup Restore App

## Purpose

The Backup Restore App copies backup files, restores SQL Server FULL (and optionally DIFF + LOG) backups into a DR database, verifies restore health, and records restore history. Point-in-time restore (PITR) is supported when transaction log backups are available.

## Package / Files

- `db_ops/backup_restore/`
- `db_ops/backup_restore/sql/sqlserver/`
- `data/restore_config.json`
- the runtime store declared in `data/store_config.json` (PostgreSQL in this tree; `runtime/db_ops.sqlite` when the backend is `sqlite`)

## Runtime Tables

- Writes `backup_restore_history`.
- Workflow-level events may also be written to `job_runs`.

## Config Files

`data/restore_config.json` defines source/target paths, SQL Server connection metadata, database mapping, certificate import settings, copy/delete windows, and restore verification behavior. Secrets are resolved through the same local secret pattern as other database connections.

## Data Flow

Restore config -> copy recent backup files into import location -> optionally import certificate -> locate latest FULL backup -> generate/execute SQL Server restore SQL -> verify restored database -> write `backup_restore_history` and logs -> delete old copied files when requested.

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
python -m db_ops.backup_restore.cli delete-backup --config config.json --hours 48

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

# Verify restored database with DBCC CHECKDB
python -m db_ops.backup_restore.cli verify-restore --config config.json
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

Each entry is judged with **its own `retention_days`** — the same number its own script prunes with,
so the two cannot drift — through `common`'s `prune-backup-files` (`docs/13_common.md`), `mode=age`
by default. `--retention-days` and `--mode` override for one run. Oracle and PostgreSQL entries are
listed from the filesystem; SQL Server entries are **skipped by name**, because their listing goes
through the instance and needs a login this command does not carry. Each run writes its own
`job_runs` row under `backup_restore.prune_job.<backup_id>.<job>`, so "did the cleanup run" is a
different question from "did the backup run".

Measured on 2026-08-07 across the 9 active jobs: 6 pruned, 3 skipped, 10 obsolete WAL files found
on `CLOUD_PG_WAL` at its 7-day window, nothing deleted (report mode).

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

Two things about it that were wrong first, and are now tests:

- **The chain must be judged over the whole directory, not the age-selected part.** The newest full
  is exactly what the age gate filters out, so a lone aged file was its own anchor and nothing was
  ever deleted — the cleanup became a no-op that reported success.
- **Timestamps need a tie-break.** `vm_import_unc` is an SMB share, where mtime resolution can be
  two seconds and copies land in the same tick routinely. Compared on the timestamp alone, tied
  fulls are each "not older than the newest", every one is kept, and the share fills up.

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
| **Import dir creation** | Preflight checks UNC share root; creates local dir + SMB share via PowerShell remoting (WinRM) if missing | `_prepare_linux_base_import_dir` runs `sudo mkdir -p` over SSH on first SFTP call |
| **Backup file copy** | PowerShell `Get-ChildItem` scan source + `shutil.copy2` to UNC target | Python scan source + paramiko SFTP `put` to Linux target |
| **Backup file selection** | `Path.rglob("*.bak")` over UNC mount | SSH `find ... -name "*.bak"` on remote Linux fs |
| **Restore execution** | PowerShell `Invoke-Command -ComputerName` → sqlcmd on remote Windows | SSH `sqlcmd` executed on the remote Linux host |
| **Delete old files** | PowerShell `Get-ChildItem -Include *.bak,*.trn` + `Remove-Item` over UNC | SSH `find ... -name "*.bak" -o -name "*.trn"` + `rm` |
| **Credentials** | `cmdkey /add:<host>` sets up Windows credential manager for SMB | `vm_username` + `vm_password_env` used for paramiko SSH auth |
| **Log file** | `copy_sqlbk.log` written to `vm_log_unc` | Skipped (log not written for Linux targets) |
| **Retention filter** | `*.bak` and `*.trn` only (no other files deleted) | `*.bak` and `*.trn` only |
| **Cleanup timing** | After restore (copy → restore → delete) | After restore (copy → restore → delete) |

## Running db_ops itself on Linux (containerized) — SMB source reads

The transport table above assumes db_ops runs on **Windows** and reads the backup
source over a UNC path. When db_ops runs **on Linux** (the Docker image) it cannot
read a Windows UNC share directly, so the copy step reads the source with
`smbclient` (validated end-to-end against a containerized SQL Server 2025 target):

- It authenticates with an auth file (so a password containing `%` is not split by
  `-U user%pass`), then recursively downloads recent files with `mget *`. A
  `*.bak`/`*.trn` mask is **not** used: with `recurse ON`, smbclient applies the
  mask to subdirectory names too and never descends into `FULL`/`LOG`. Pattern
  filtering happens locally afterward (`copy_file_patterns`).
- When `databases[]` is configured, only those `<db>` subdirectories are fetched.
- `smbclient` does not preserve file mtimes, so each backup's real time is
  recovered from its filename (`..._YYYYMMDD_HHMMSS`). The log-chain selection
  filters logs by time relative to the FULL backup; without this the restore would
  apply pre-FULL logs and fail with `Msg 4326` (the log "is too early to apply").
- The staged files are then sent to the Linux SQL Server target over SFTP, which
  also preserves the mtime.

For this path the source `backup_share` must point at the **instance** level (for
example `\\host\SQLBK\APPDB-DB$APPDB`) and `vm_import_linux_path` must **not** include
that level, so files land at `vm_import/<db>/FULL` — exactly where the restore looks.

### When the logs are not where the fulls are

A restore cannot do point-in-time from a backup set that has no logs in it, and a source does
not always write its logs next to its fulls.

`192.0.2.248` is the case. db_ops runs only a FULL job there (`ACME_MSSQL_1_248_ALL_FULL`,
writing `.bak` to `\192.0.2.248\SQLBK_DBOPS`); the `.trn` files come from a separate SQL
Agent maintenance plan writing to `D:\DBA\SqlBK\<instance>\<db>\LOG`. A PITR attempt on
2026-08-13 failed with **"No transaction log backups found"** — correctly: db_ops' set had none.

**The answer is a second entry, not a second mechanism.** `192.0.2.250` already shows the
pattern: `ACME_TO_MSSQL2025_DOCKER` reads that host's Agent tree (`\...\SQLBK\APPDB-DB$APPDB`,
where FULL and LOG sit together) and `ACME_MSSQL_DBOPS_TO_MSSQL2025_DOCKER` reads db_ops' own.
One share each, no special support. `ACME_MSSQL_1_248_AGENT_PITR_TO_MSSQL25_100_115` is the same
split for 2.248.

Pointing the *existing* entry at the Agent tree instead would work, but it silently stops
verifying db_ops' own backups — which is what that entry is for. Two entries keep both answers.

Three things a second entry against the same source must get right:

- **Its own staging directory.** Two entries sharing `vm_import_linux_path` would mix two backup
  sets in one tree.
- **Its own target database names.** Both restore the same source databases onto the same
  instance; the PITR entry maps each to `<name>_PITR`.
- **An explicit `databases` list.** `databases: []` means "everything found under the import
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
`\192.0.2.248\D$\DBA\SqlBK\SERVER-TAP$SQLEXPRESS` — a maintenance-plan tree with no
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
  carry it), then on its **own line** in the Telegram body — the JSON payload is truncated
  at 3900 characters, so an id living only inside it would be cut off exactly on the
  longest, most urgent messages. `telegram_send_messages.source_id` is
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

The WAL directory goes through the same step: `restore_command` is run by the *server*, inside the
container, so a configuration pointing at a host path produces a cluster that starts and silently
replays nothing.

## Host-to-Host Transfer: Failures Must Surface, Not Stall

The copy from source host to target host is one `tar` stream through the orchestrator (`transfer.py`),
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

**On duration.** Every byte crosses the orchestrator twice (SFTP down, SFTP up) because the two
database hosts are not assumed to reach each other. Measured on the CLOUD → CLOUD2 pair:
**~1.5 MB/s**, so a full 17 GB RMAN set takes ~3.3 hours. A repeat drill is minutes because
same-size files are skipped — the long run is the one that follows a wiped staging directory.
Note the transfer itself is **not** bounded by the entry's `time_window.timeout`; that timeout
governs the script step. A run longer than the timeout still completes, but the reaper will mark
its `job_runs` row `TIMEOUT` in the meantime.

## Target Retention (`target_retention_seconds`)

How long a restore keeps the backup files it staged **on the machine it restores onto**. This is
the *target's* retention and has nothing to do with the source's: the source decides how far back
it can recover from, the target only needs enough to run its next restore. Without it a staging
directory only grows — the transfer copies what the source has and never removes what the source
dropped — until it fills the disk it restores onto.

Set per restore entry, in seconds. `0` means never delete.

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

`--delete-hours` on `workflow` / `restore-workflow` overrides it for one run (in hours). Unset
means "use each entry's own value".

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
- Restore succeeded but verification failed: run `verify-restore` and inspect SQL Server CHECKDB output.
- PITR fails with "no log backups found": log backups are required in the import folder covering the target point in time; verify that log files were copied with `copy-backup` before using `--point-in-time`.
- PITR fails with "cannot parse point-in-time": use the exact format `YYYY-MM-DD HH:MM:SS +HH:MM` (space before the timezone offset, not a colon-less `+HHMM`).
- `--restore-id` not found: the value must match the `restore_id` key exactly (case-sensitive) in `restore_config.json`.
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

Required config keys: `log_dir`, a resolvable runtime store, and at least one entry in the `backup_restore` sources array within the config.

## Optional Integrations

The backup restore app has no optional integrations. It writes `backup_restore_history` and `job_runs` events to the runtime store and exits. No other sub-app is called or depended upon.

## EXE Packaging Notes

- Network share paths (`prod_backup_share`, `vm_import_unc`) must be accessible from the machine running the EXE.
- `sqlcmd` must be on `PATH` for restore and verify commands.
- Certificate API calls require network access to the configured certificate endpoint.
