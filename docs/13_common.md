# 13. Common (shared operations)

`db_ops/common/` is where the toolkit **does** things: reach a host, run SQL against one database,
move a file, rotate a credential, patch an instance, ask before something irreversible. It holds
no app of its own — it is the layer every app acts *through*, which is why no app has to import
another and why one operation has one implementation instead of five that have drifted.

> **Apps do not import this layer. They run it as a CLI.**
> `python -m db_ops.common.cli <command> '<json>'`

That is the rule, not a convention, and it is guarded by
`tests/test_app_common_imports.py`. An operation can be a process, and putting it across a process
boundary is what forces the request to be a complete, inspectable JSON object and the answer to be
a response envelope — which is what lets the same call serve a scheduled run, a chat command and a
one-off recovery against a machine that is in no inventory at all.

**One named exception**: `common.data_sources` is imported, because it is the single reader of the
`data/` folder and routing a configuration read through a subprocess would cost every caller a
process and buy nothing.

**The mirror of this layer is `lib` (ORD 14)**, which may only ever be *imported* and never run.
The split is by what a thing is: `common` **does**, `lib` **decides**. If you are looking for time
windows, severity classification, notify routing, formatting or any other pure rule, it moved to
[`14_lib.md`](./14_lib.md) in the 2026-08-15 split and is not here.

---

## What `common` is for

**Most of `db_ops/common/` is a library: built and shipped on its own, it runs anywhere with
nothing beside it — no `data/`, no `config.json`, no repo layout.** That is the goal everything
below serves, and it is true of 72 of the 86 modules. The other 14 are the resolver tier described
below; they are the exception, they are listed by name, and a new module is not one of them by
default.

The apps are front ends. They know where the config is and what a `server_id` means; the library
tier does not and must not. An app reads its config, looks up the host, decrypts the password, and
then calls `common` — passing a **JSON object** that already contains every answer.

```
app (front end)                    common (library)
  reads restore_config.json
  resolves the host from            python -m db_ops.common.cli restore-database '<json>'
  db_instances.json          ──►      { "db_type": "sqlserver",
  decrypts the password                 "source": { "path": "...", ... },
                                        "target": { "host": "...", "port": 1433,
                                                    "username": "...", "password": "..." } }
```

Calling it from a shell, from a Telegram action or from another program is the same call. There
is no second API: **the CLI is the API**, and its input is a JSON object.

### Every command returns the same JSON response

**Input is a JSON object; output is a JSON object with a fixed shape.** Always these five keys,
always present even when empty, always on stdout, whether the work succeeded or not:

```json
{
  "success": true,
  "operation": "restore-full",
  "message": "Restored SALESDB_STG to 2026-08-07 01:40:00.",
  "error": null,
  "data": { },
  "metrics": { "duration_ms": 41230 }
}
```

| Key | Means |
| --- | --- |
| `success` | Did the **work** succeed. Not "did the process run" — that is the exit code. |
| `operation` | Which command answered. In the body, so a stored or forwarded response is still self-describing. |
| `message` | One line for a human — what a Telegram message or log line quotes. |
| `error` | `null` on success; the reason on failure, in the words the operator needs. |
| `data` | The result proper. Shape is per command, documented at that command. |
| `metrics` | Numbers about the run: durations, counts, bytes. Charted; kept out of `data`. |

Build it with `db_ops/lib/response.py` (`ok()` / `fail()` / `emit()`) rather than by hand,
so no command invents a sixth key or omits `data` when it has none — an absent key and an empty one
are the same fact, and making them look different is how callers grow branches nobody tests.

**This is now measured, and it is not yet true everywhere.** The input half of the contract held
for years because a test held it; the output half held for no one, and on 2026-08-16 a guard was
written over all three dispatchers and found **23 of 43** `common` commands answering in something
else — 21 in an ad-hoc `{"ok": …}` and 2 in no JSON at all.

| Guard | Covers |
| --- | --- |
| `tests/test_common_cli_response_shape.py` | all 43 `common.cli` commands |
| `tests/test_db_cli_json_contract.py` | the 3 `db.cli` store commands |

Both carry a `NOT_YET_ENVELOPE` **shrinking baseline**: the test fails if the set grows *and* if an
entry has been fixed, so converting a command means deleting its line in the same commit.

Converted since: `add-sql` / `metric-toggle`; then `list-targets`, `inventory-summary` and
`check-credentials` — the three a program could not consume at all; then the **twelve gate
commands** plus `fetch-file` / `send-file` / `pack-files`, in one edit each, because each family
answers through a single handler (`_gate_command`, `_file_transfer_command`). The gate report is
unchanged and now sits under `data`, so `status`, `blockers`, `gates`, `facts` and `evidence_file`
are all still there for an incident review.

Then the four independent ad-hoc handlers — `run-cmd`, `rotate-password`, `metric-severity`,
`trace-session` — which nothing in the tree reads; then `check-secret` and
`queue-telegram-message` with its client; and last `run-sql`, together with all four of its
callers.

**Every command answers in the envelope — 43 in `common.cli`, 3 in `db.cli`.** Both baselines are
empty and both guards hold them there.

`run-sql` was last for a reason worth keeping: `sql_tasks`, `telegram` (twice) and
`backup_restore` all read its answer, and `lib/common_cli` carried a third reader, `run_ok`,
that existed *only* for the `{"ok": …}` shape. Converting the command without them would have
broken four call sites; converting them without it would have broken nothing and fixed nothing.
They went in one commit, and **`run_ok` is deleted** — there is one shape, so there are two
readers (`run`, which raises, and `run_allowing_failure`, which does not) and no third.

**One command's exit code is not a summary of its response, and must stay that way.** `run-cmd`
passes through the *remote* command's exit code, because `run-cmd … ; echo $?` is asking what the
command did — `2` from a remote `grep` means "no match", not "db_ops failed". The answer is still
one envelope, `success` still mirrors it, and the number is in `data.exit_code` and
`metrics.exit_code` as well. `format: raw` and `format: txt` still bypass the envelope entirely,
which is the contract working: the rendering is chosen *inside the request*.

**A converted command's caller converts with it.** `backup_restore/instance_metadata.py` unwraps
the gate report out of `data` in one place, so `summarize()` and the restore's `PHASE=` line keep
reading `ok` / `status` / `blockers` / `evidence_file` where they always did.

**`format: "txt"` is how the human rendering survives a conversion.** `list-targets` and
`check-credentials` still print their old listing on request, so pasted runbook lines keep working;
what changed is the default.

The exit code is a *summary of the response*, never a second opinion: 0 when `success` is true, 1
when it is not. A shell caller checking `$?` and a program parsing the JSON must never reach
different conclusions. Nothing is ever reported only on stderr — `check-credentials` used to put
its finding in the exit code and its detail on stderr, which is the precise split this forbids.

One command's `success` is worth reading carefully: `check-credentials` answers `success: true`
when *the check ran*, and puts the unresolvable targets in `data.problems`. A broken estate is not
a broken command. Its exit code is still 1 in that case, because a runbook reads `$?`.

**stdout carries the answer and nothing else.** `inventory-summary` printed `Wrote <file>` in front
of its JSON — a library function writing progress to the stream the contract reserves — so its
stdout parsed as neither prose nor JSON. That line is the response's `message` now, and the warning
`inventory_render` emits goes to stderr. A test asserts nothing precedes the object.

`data_sources` also answers two questions about the *reports* configuration, because more than one
app needs the same answer and a second copy would let them disagree: `report_base_url()` (where a
published page can be linked from) and `inventory_exclude_ip_prefixes()` (which server ip prefixes
the inventory pages leave out — empty unless `reports_config.json` says otherwise; it used to be a
constant inside `lib/inventory_render.py`, which meant the library decided which of an estate's
machines nobody gets to see).

**Neither has a built-in value that names a machine.** `DEFAULT_REPORT_BASE_URL` is `""`, not a
host: until 2026-08-21 it was a real internal report server, so an unconfigured install produced
links that resolved, looked right and pointed at somebody else's estate. Unset now means unset —
`report_base_url()` returns `""` rather than `"/"`, because a root-relative URL is a different
claim and one nobody made.

The same correction reached `oracle_bridge.resolve_secret()`. Its environment fallback read one
hardcoded variable name derived from a real host; it now reads a variable named by the config's
own `sql_access.secret_ref`, which is the convention the rest of the tree already documents in
`remote_exec`: *refs double as env var names across db_ops*. Besides removing the address, that
is what lets a second bridge on a second host have its own secret at all.

Secrets never appear in any key — redact before building the response, not after.

### Two tiers, and which one a new module belongs to

This layer is **not** uniformly pure, and saying it was is how the boundary got crossed nine times
without anyone noticing. It is two tiers:

| Tier | Size | What it may read | Where it is pinned |
| --- | --- | --- | --- |
| **Library** — input in, result out | 72 of 86 modules | nothing | `tests/test_common_layers.py` |
| **Resolver** — answers "which host is `ACME-192-0-2-248`", "what is that credential's password" | 14 modules, listed by name with a reason each | `config.json`, `data/*.json`, the store | `READS_LOCAL_CONFIG` in the same file |

The library tier is the **default**: a new module belongs there unless it cannot answer its
question without reading the machine it is installed on. The resolver tier is `common` rather than
app code because every app asks those same questions, and seven apps resolving a `server_id` seven
ways is the failure `common` exists to prevent.

Watch the form the boundary is actually crossed in — not a module announcing that it needs config,
but a **default argument**. `data_sources/ssh_auth.py`, `sql_run.py` and six others take the fact as a parameter and
fall back to `data_sources` when the caller passes nothing. Pass everything and it is a pure
function; pass nothing and it silently reads *this repo's* `data/`. Same code, and only one of the
two survives being packaged elsewhere. So keep passing the fact even when the default would work.

### The rules

1. **A library-tier module reads nothing.** Not `data/*.json`, not `config.json`, not the secret
   store, not the repo's folders. Packaged and dropped somewhere else, a module that reads a file
   has nothing to read. Needing to read makes it resolver tier — a named entry in
   `READS_LOCAL_CONFIG` with the reason, which is a visible diff and the argument you should have
   to make out loud.
2. **The input carries everything** — `db_type`, host, port, username, password, paths. If an
   operation needs a fact, the fact is a parameter.
3. **Looking things up is the caller's job.** The app resolves, then states the answer. The
   resolver tier exists for the cases where the lookup itself is the shared operation — it does
   not license the tier below it to look things up.
4. **Never import an app.** Enforced by `tests/test_import_boundaries.py`.
5. **`common/cli.py` routes; commands live in their own module** (`cli_restore.py`).
6. **One response shape.** Every command returns `{success, operation, message, error, data,
   metrics}` — see above. Built with `response.ok()` / `response.fail()`.
7. **Keep it plain.** No plugin registries, no indirection added for a second case that does not
   exist yet. `restore/__init__.py` picks the engine with an `if` because there is one engine.

### Worked example: `common/restore/`

| Module | Owns |
| --- | --- |
| `spec.py` | What a restore is, as data. Parsing, validation, redaction of passwords. |
| `pitr.py` | A point-in-time request is refused, never downgraded to "the newest chain". |
| `plan.py` | What a spec would do, decided without touching anything. |
| `sqlserver/chain.py` | Which backups to restore and in what order — from LSNs, not file names. |
| `sqlserver/sql.py` | The RESTORE statements, with `STOPAT` on the one log that carries it. |
| `sqlserver/timeparse.py` | Reading the moment; `STOPAT` is in the server's clock. |
| `sqlserver/runner.py` | The I/O: connect, ask the instance what is on disk, run the statements. |

It reads no file at all, pinned by `tests/test_common_restore_is_pure.py` — which walks the
package recursively and fails on an import that reaches config or an app, on a hand-rolled
`open()`, or if the package cannot be imported from an empty directory.

Three things in there are worth knowing because getting them wrong is **silent** — the restore
succeeds and the data is wrong:

- **The chain comes from LSNs.** A differential whose base full was superseded still sorts last by
  name and restores cleanly onto the wrong base. A full taken *after* the target moment sorts
  newest and already contains changes past it.
- **A moment past the end of the logs raises**, rather than rounding down to a restore that
  succeeds hours short of what was asked for.
- **`STOPAT` goes only on the final log.** SQL Server accepts it on a full or a differential and
  silently ignores it there, which reads as a point-in-time restore that never happened.

`runner.py` asks the *instance* for the file listing (`sys.dm_os_enumerate_filesystem`), not the
local filesystem: on a container target the backup path does not exist on the machine running the
code. It also sets `statement_timeout_seconds=0` explicitly — the connection layer reads `None` as
"reuse the connect timeout", so leaving it unset capped every statement at 30s and the first real
restore died mid-chain with `HYT00`, leaving the database RESTORING.

**Proven end to end** on 2026-08-06 against the `mssql2025` container on 192.0.2.249, through
`python -m db_ops.common.cli restore-database -`: seven databases restored to latest, then
`SALESDB_STG` restored to `2026-08-06 01:40:00` — which applied the 01:15/01:30/01:45 logs and stopped
before the 02:00 one. All ended `ONLINE`.

### `list-backup-files` — one question, three engines that disagree

What backups exist, classified full / diff / log, without the caller knowing which engine it is
talking to. Each answers the question the only way it can be answered correctly for that engine:

| Engine | Asked | Why not the file names |
| --- | --- | --- |
| SQL Server | the instance, `RESTORE HEADERONLY` | a `.bak` keeps its name when copied from another database |
| Oracle | RMAN, `LIST BACKUP` | an RMAN directory is flat; the level lives in the catalogue |
| PostgreSQL | the directory layout | `base/<stamp>_FULL` / `_INCR` is what the backup job wrote **on purpose** |

Where the database runs is stated, not guessed — `host.runtime` is `windows`, `linux`, `docker` or
`k8s`, and `db_ops/common/hostcmd.py` builds the command line for it (`docker exec … sh -lc`,
`kubectl exec -n … --`, `powershell -NoProfile`). A `container` field alone would leave "run on the
host" and "the caller forgot" looking identical, which must not happen when the command being
wrapped is a restore.

Two classifications were wrong on first contact with real output, both silently:

- **Oracle archivelogs.** `Piece Name:` comes *before* the `List of Archived Logs in backup set`
  line that identifies the set, so flipping a flag on that marker labelled every archivelog piece
  as whatever the previous set was — 810 "full" on a lab holding one level 0. The set is now read
  whole, then classified.
- **Controlfile autobackups** are their own kind, not `full`. A site writing one every 15
  minutes accumulates them fast: calling them full left 610 in the answer, and a caller filtering
  for `full` would pick one and fail. Excluded by default; ask for `"kinds": ["controlfile"]` to see them.

Measured on 2026-08-07: SQL Server 576 files (6 full / 570 log), Oracle 325 (6 / 8 / 311) through
`docker exec` on 203.0.113.188, PostgreSQL 21 (7 / 13 / 1) the same way.

### Reaching a host: `runtime` and `access` are two questions

`runtime` says what runs there — `windows`, `linux`, `docker`, `k8s`. `access` says how the machine
is reached — `ssh` (default) or `winrm`. They were one field until 2026-08-07, and the conflation
cost the entire Windows half of the estate: `runtime: windows` meant "a Windows host with an
OpenSSH server", and exactly one box here has one. The other thirteen SQL Servers are reached by
**WinRM** — which is what `cmd_access.method` in `db_instances.json` has said all along and what
the metrics collectors have used for months. A command that could only speak SSH could not touch
any of them.

`method` is accepted as a spelling of `access`, so a `cmd_access` block can be handed straight
through. The port follows: 22 for SSH, 5985 for WinRM, 5986 with `ssl`.

WinRM is **delegated to `remote_exec`**, not spoken here. A second WinRM client would be a second
set of quoting bugs to find, on the machines where being wrong means a production SQL Server. It
also skips `wrap()`: a WinRM session already lands in PowerShell on that host, so wrapping would
start a second one inside it.

Fixing this surfaced a bug that had been quiet for as long as WinRM has been in use. `pypsrp`'s
`execute_ps` returns a `PSDataStreams` object as its second value, not a string, and `remote_exec`
passed it through `str()` — so **every failing PowerShell script over WinRM reported
`<pypsrp.powershell.PSDataStreams object at 0x…>` instead of the reason it failed**. The error
records are now read out of the stream. Warnings are deliberately left out: they are frequent and
benign here, and folding them into stderr would make every script that prints one look like a
failure.

### `backup-database` — one run, and an honest answer about it

Ship a script to the host the database runs on, with the environment it needs, and say whether it
actually completed. The scheduling, the config and the store rows stay in `backup_restore`; what
came down here is the run itself.

**Success means the script printed `RESULT=ok`, not that it exited 0.** Exit 0 says the shell
finished. A script fed to `bash -s` can end early with a clean status and no output, and one did:
a `docker exec -i` inside it read the rest of the script as its own stdin, so the shell ran out of
work and reported success having backed up nothing. A backup that lies about succeeding is worse
than one that fails, because the failure is only found by the restore that needed it. So success is
a positive statement by the script; exit 0 without the receipt is reported as an error saying
exactly that.

The script runs **on** the host, not inside the container — these scripts `docker exec` themselves
because they need to be on the host for the directory the backup is written to. `hostcmd.run_script`
feeds it on stdin rather than passing it as an argument: a backup script is a hundred lines of shell
with its own quoting, and as an argument every character has to survive two shells and eventually
meets `ARG_MAX`.

`level` is **one word for every engine** — `full`, `diff`, `log` — translated in the spec, which is
the one place that knows the engine, so the CLI and the app cannot disagree about what "full" means
for Oracle (`BACKUP_LEVEL=0`). Oracle and PostgreSQL have no `log` on purpose: their archive/WAL
backups are a separate script with its own schedule, and asking for one is refused by name rather
than passed into a script that would read it as something else.

`env` carries **resolved values, never secret refs.** The caller decrypts, because the caller knows
which store and which passphrase; a spec holding `TOKEN_..._BACKUP_ENC` would make this layer read
the secret store, which is the lookup the split exists to remove. An **empty** env value is refused
outright — that is how a missing secret arrives, and a backup script reading it as "no passphrase"
writes an unencrypted set nobody notices until a restore needs it.

The plan (and the dry run) names `env_names` and never env values: it exists to be shown to
somebody, and half of them are passphrases.

**`server_metadata`** (SQL Server only) exports the instance's logins, server roles, permissions,
credentials, linked servers, endpoints, `sp_configure`, Database Mail and Agent jobs beside the
backup. A SQL Server backup covers user databases only — the scripts select `database_id > 4` — so
after a restore the database is back and none of the machinery around it is. Refused for Oracle and
PostgreSQL, whose physical backups carry that state inside the data; refusing beats ignoring,
because a caller that set it believes it is getting something.

A metadata failure **never** fails the backup — the data is the thing that must not be lost — and it
is skipped entirely when the backup failed, since a bundle beside a backup that did not complete is
a pair of files that look matched and are not.

#### Windows SQL Server

`db_ops/common/backup_scripts/sqlserver/mssql_backup_database.ps1` is the Windows half of the bash script: same
levels, same `<DB>\FULL\<DB>_FULL_<stamp>.bak` layout, same encryption certificate export, same
retention rule, same `RESULT=ok`. `tests/test_mssql_backup_scripts.py` pins the two to one
contract, because the restore side reads a set without being told which script wrote it — the
moment they disagree there are two backup formats and one of them has never been restored from.

Four things about it that are specific to Windows and each cost something to learn:

- **`??`, `?:` and `?.` are PowerShell 7+.** These hosts run 5.1, where they are *parser* errors —
  the script dies before its first line, so every guard in it is bypassed at once.
- **`Write-Error` raises** under `$ErrorActionPreference = 'Stop'`. `Write-Error …; $failed = 1;
  continue` abandons the loop: the difference between "one database could not be backed up" and
  "the other eleven were never attempted". It writes to the error stream directly instead.
- **Directories are created with `xp_create_subdir`, not `New-Item`.** `BACKUP TO DISK` is executed
  by the SQL Server *service account*; a directory this session can create and the service cannot
  fails with "Operating system error 5(Access is denied)" naming a directory that plainly exists.
  Same reason the certificate's existence is checked with `xp_fileexist` rather than `Test-Path`.
- **`$args` is an automatic variable.** Shadowing it inside a function works until it does not, and
  the failure looks like sqlcmd being called with no arguments.

**It owns the chain.** It writes ordinary FULL/DIFF/LOG backups, not `COPY_ONLY`, so a FULL taken
here resets the differential base for the whole instance. Every Windows SQL Server behind this path
still has its own Agent backup jobs (`Backup_Full`/`Backup_Diff` on 192.0.2.115,
`Job_Maintain_Backup_FULL`/`_LOG` on 192.0.2.245 and 192.0.2.250), and running both splits the
chain across two locations — a restore would then need both. **Disable the native jobs on an
instance before enabling this against it.** That is an operational decision, which is why no
`backup_restore.backups` entry ships for a Windows instance: the script and the command are ready,
turning one on is deliberate. A ready-to-adapt entry:

```json
{"backup_id": "ACME_MSSQL_2_115_FULL", "active": false, "db_type": "sqlserver",
 "server_id": "ACME-192-0-2-115", "backup_dir": "E:\\SQLBK\\dbops",
 "env_secrets": {"BACKUP_ENCRYPTION_PASSWORD": "<secret ref>"},
 "jobs": [{"job": "full", "active": false,
           "script": "assets/backup/sqlserver/mssql_backup_database.ps1",
           "retention_days": 14,
           "env": {"BACKUP_LEVEL": "full", "MSSQL_SERVER": "."},
           "time_window": {"from_hour": 1, "to_hour": 5,
                           "repeat_interval": 72000, "timeout": 7200}}]}
```

Omit `MSSQL_USER`/`MSSQL_PASSWORD` to back up as the WinRM account (Windows auth); set both to use
a SQL login. One without the other is refused — it would otherwise fall back to Windows auth
silently and back up as whoever WinRM connected as.

Measured on 2026-08-07 against 203.0.113.188: an RMAN archivelog backup through
`docker exec`, `RESULT=ok` in 16s — the same run through `backup_restore.cli backup --force` took
17s, because it is now the same code.

### `prune-backup-files` — the part that decides, with 14 days as the default

The piece between listing and deleting: which files are obsolete. It lists through the same code
path `list-backup-files` uses — so a caller cannot be shown one set by `list` and have a different
set judged by `prune` — then applies a rule, then optionally hands the paths to `delete-files`.

**`retention_days` defaults to 14**, which is what `restore_config.json` already says for every
`database`/`full` job and what the backup scripts default to themselves.

Two rules, and **`age` is the default**:

| mode | rule |
| --- | --- |
| `age` (default) | a file finished before `now - retention_days` is obsolete — full, diff and log alike, whatever depends on it |
| `recovery_window` | keep whatever is needed to restore to *any* point in the window: the anchor is the newest FULL at or before the cutoff, and everything from there on stays. RMAN's `DELETE OBSOLETE … RECOVERY WINDOW OF n DAYS` |

They differ only when fulls are taken rarely relative to the window. Restoring to a point ten days
ago needs the FULL from before that point; under `age` it goes the moment it turns N days old, and
the newer differentials that restore onto it are kept with no base. Take a full daily against a
14-day window and the newest full is never more than a day old, so the two agree — pinned by a
test, not assumed.

Both keep a file whose `finished_at` the engine could not state: "unknown age" and "old" are not
the same fact. `recovery_window` judges **per database**, because a chain belongs to one — a single
SQL Server directory holds every database on the instance.

**Reporting is the default; `delete: true` is deliberate.** Without it the command only answers, and
`obsolete_paths` is exactly the array `delete-files` takes as `paths`, so deciding and deleting stay
two steps. With it, removal goes through `delete-files` — one file at a time, with all its refusals —
fenced to the directory being pruned. `dry_run` goes through the motions and removes nothing.

`reclaimable_bytes` is **only as good as the listing**. Oracle's comes from RMAN, which carries no
file size, so it is `0` for a directory of gigabyte pieces; the message says "size not reported by
this engine's listing" rather than printing a zero somebody might size a disk against.
`delete-files` stats each file as it goes and reports the bytes actually freed.

Measured on 2026-08-07 against `/opt/oracle/backup/dbops` (364 files): nothing obsolete at 14 days,
55 obsolete at 4 — and with `delete: true, dry_run: true` all 55 came back `skipped`, 0 deleted,
0 failed, every file still on the host.

### `delete-file` / `delete-files` — the caller chooses, the command only removes

The other half of `list-backup-files`, and deliberately the *dumber* half. It takes full paths and
deletes them; it works out nothing for itself.

That split is the whole design, because the thing it replaced did both.
`backup_restore/delete_backup.py` (now marked obsolete, still wired into `restore-workflow`) read
`restore_config.json`, globbed `*.bak`/`*.trn` under the configured import share, worked out which
were older than `copy_recent_hours`, and removed them — so "what would this delete" had no answer
short of reading the code and trusting the clock. Now the caller lists, decides, and passes back
the paths it was given. The decision is visible before it happens, and it can come from a person.

| Refusal | Why it is not a nuisance |
| --- | --- |
| a wildcard (`*`, `?`, `[`) | expanding one deletes files nobody looked at; `*.bak` under the wrong root is one typo from the full backup being restored from |
| a relative path | resolves against whatever directory the shell started in — a different file on every runtime |
| a directory | a caller that meant one file and passed its parent would lose the whole set |
| outside `must_be_under` | optional fence for a caller that already knows the one directory it may clean |

Every path in a batch is validated **before the first delete**, so a bad path stops the request
while nothing has happened yet.

**A file that is already gone is a success** (`not_found`). Delete states an end condition, not an
act, so re-running after a partial failure is safe rather than something a caller has to
special-case. It stays a separate status from `deleted` because "there was nothing there" and
"there was, and now there is not" are different facts about what the run did.

One file per command, and one connection for the batch: the per-file answer is only possible
because each file gets its own command — `rm a b c` returns one exit code for three files and
cannot say which is still there — while reconnecting per file is the 10 KB/s mistake described
below. `hostcmd.run` takes an already-open client for exactly this.

Checking and deleting happen in **one** command. Statting first and deleting second is a race with
whatever else writes to a backup directory, and on a slow link it is twice the latency per file.

**Oracle: this removes the file, not RMAN's record of it.** A backup piece deleted from disk leaves
RMAN still listing it and the next `RESTORE` picking it. Use `DELETE BACKUPPIECE` / `DELETE
OBSOLETE` for those.

Windows commands go over `-EncodedCommand`, not `-Command`. The command passes through cmd.exe
(locally, `shell=True`) or through whatever shell the Windows OpenSSH server runs, and neither
understands `shlex.quote`'s POSIX single quotes: a PowerShell literal `'C:\bak\a.bkp'` arrived as
`'''C:\bak\a.bkp'''` and PowerShell refused the whole script with "Unexpected token". Base64 has
nothing either shell treats specially. This fixed every `runtime: windows` caller, not just this
one.

### `pack-backup` / `pull-file` / `push-file` — one archive, moved and proven

A backup set is thousands of small files, and per-file SFTP across two internet hops measured
10 KB/s in the measured case — eight hours for 362 MB, with the link never the limit. So the set is
packed where it already lives and **one** archive travels.

`format` is `tar` or `zip`, and the command is written in the host's own idiom: `zip` does not
exist on Windows at all (that is `Compress-Archive`), while `tar.exe` ships with Windows 10/2019
and behaves. Picking the wrong verb fails with "not recognized as a cmdlet", which reads like a
broken host rather than a wrong command.

**The sha256 is the result, not decoration.** The archive is hashed where it was made and hashed
again where it landed, and a mismatch is *refused* — never retried, because a retry that succeeds
hides a link or a disk doing this occasionally. Size alone catches a truncated copy and nothing
else, and an archive that arrived corrupted restores and is wrong.

Remote paths use `PurePosixPath`/`PureWindowsPath` chosen by the **target's** runtime, never
`pathlib.Path`: that picks its flavour from the machine executing the code, so a Windows worker
computing a Linux target's parent turned `/b/one.bkp` into `` — a directory on neither end.

Measured on 2026-08-07, pack on 203.0.113.188 → pull to the worker → push to 203.0.113.121:
one sha256 (`7ed6a8d8…`) across all three.

### `relay-file` — one file, from one host straight to another

`fetch-file` then `send-file` already moves a file between two hosts, and that is exactly why
this exists: the two-command form stages the bytes on the orchestrator's disk, and verifies each
hop separately. A Windows master relaying a docker image bundle then has to have several GB free
for a file it never reads, and a sha256 that disagrees names the *second* hop, leaving the
operator to work out which of the two copies is the bad one.

`relay-file` opens both sessions, hashes the source **before** the stream (a file still being
written would otherwise hash one set of bytes and send another, and the network would get the
blame), pipes `cat` into `cat >` in 256 KB chunks, hashes the destination, and only then renames
the `.dbops_partial` onto the real name. A mismatch discards the copy — it does not retry, for
the same reason `pack-backup` does not.

```json
{"source":      {"target": "ACME-192-0-2-249-HOST", "path": "/tmp/bundle.tar.gz"},
 "destination": {"target": "ACME-192-0-2-11-MSSQL25-1433", "path": "/tmp/bundle.tar.gz"},
 "overwrite": false, "make_dirs": true}
```

Each side resolves like every other target here — a `server_id`/ip from `db_instances.json`, or
an inline `access` block — so no password appears in the request.

**Linux/SSH on both ends, and a Windows end is refused** rather than half-supported: the stream is
`cat`/`sha256sum` and the PowerShell equivalents do not compose into a pipe the same way. Use
`fetch-file` + `send-file` there and accept the staging.

**Both stderr streams are drained on their own threads for the whole transfer.** Nothing reads
them until `recv_exit_status()`, which cannot be reached while the copy is running — so a command
that complains on every chunk fills its stderr window, blocks on the write, stops reading stdin,
and the pipeline seizes with no error anywhere. `backup_restore.transfer` learned that by sitting
at RUNNING for two hours.

**The target does not pull from the source directly**, though it would be faster: the two hosts
are not assumed to reach each other, and an SSH trust created between two database hosts to serve
one file move is a permanent widening of access for a temporary need. The orchestrator already
holds both credentials.

First caller: `sre.move-db-docker`, which relays a docker image archive and each volume archive
between two lab hosts.

### Where the layer does not meet this yet

Stated plainly so packaging is not a surprise. These still resolve against `data/` today:
`sql_run.resolve_sqlserver_target` and `target_resolve` (read `db_instances.json`), `data_sources`
(*is* the data-folder loader), and `secret_text` (reads the encrypted store). Each moves the same
way the restore one did: take the resolved values as parameters, and let the app do the lookup.
New code takes parameters; existing code moves when it is next touched.
`sqlserver_instance.load_policy` was on this list and came off it on 2026-08-15 — the read is
`data_sources.load_sqlserver_instance_policy` now, and what remains here is a two-line translation
into this module's error type.

---

## Modules

| Module | Responsibility | Key public API |
| --- | --- | --- |
| `shell.py` | Resolve the PowerShell executable at runtime so the same code runs on Windows and inside the Linux container. Prefers cross-platform `pwsh`, then `powershell.exe`. | `powershell_executable()`, `is_powershell_executable(name)`; env override `DB_OPS_POWERSHELL` |
| `secret_text.py` | Encrypt/decrypt the secret file at rest. PBKDF2-HMAC-SHA256 → 32-byte key, sealed with Fernet (AES-128-CBC + HMAC), random per-file salt. The passphrase is supplied at runtime; never stored. | `encrypt_secret_text`, `decrypt_secret_text`, `resolve_key`, `resolve_cli_key`, `decode_key_base64`, `set_key_env`; env `DB_OPS_SECRET_KEY` |
| `sql_execution.py` | SQL Server connection + execution helpers: driver selection (ODBC 18/17/…), TLS-error fallback, output converters for types pyodbc cannot decode (`datetimeoffset`), batch splitting/execution, JSON-safe row coercion, and credential/secret loading. | `connect_sqlserver`, `build_sqlserver_conn_str`, `choose_sqlserver_driver`, `sqlserver_driver_candidates`, `register_output_converters`, `decode_timestampoffset`, `execute_cursor_batches`, `split_sql_batches`, `resolve_password`, `load_credentials_file`, `load_secret_text`; `MAX_RESULT_ROWS`, `SQL_SS_TIMESTAMPOFFSET` |
| `sql_run.py` | **Single source of truth** for *running SQL against one database target*, on **any** engine (sqlserver / postgresql / mysql / oracle): resolve the target (`server_id` or `<db_type> <ip> [port]`), connect (via `db_connect`; **SQL Server always lands in `master`** unless the request names a database — scripts `USE` themselves), run the batches, capture the first result set with a row cap, and roll back unless `commit` is asked for. **Input is a JSON object** — the same shape a config file, a Telegram command, or the CLI passes through untranslated. See [the section below](#running-sql-on-one-database-sql_run). | `run_sql`, `SqlRunRequest.from_json`, `json_safe_result`, `resolve_sqlserver_target`, `connect_target`, `execute_capture_first`; `SqlRunError`; `DEFAULT_MAX_ROWS`, `DEFAULT_TIMEOUT_SECONDS` |
| `data_sources.py` | **Single entry point** for loading connection inputs from `data/` (`users.json` = `database_credentials` + `remote_credentials` + `monitor_users`, plus `db_instances.json` and the encrypted secret file), **and for choosing which credential a target runs as** — required, never inferred (see [Credentials](#which-login-a-target-runs-as-credentials)). Apps import loaders here instead of reaching to the repo root. | `load_secret_text`, `load_credentials(db_type)`, `load_all_credentials`, `load_remote_credentials`, `load_db_instances`, `group_credentials_by_type`, `find_database_credential`; `CredentialNotFound`; path helpers `users_path` / `db_instances_path` / `secret_text_path` |
| `db_connect.py` | **Single source of truth** for *opening a connection to one database*, on every engine db_ops supports (sqlserver / postgresql / mysql / oracle) — what `remote_exec` is for reaching a VM. Owns driver import (lazy, per engine), default port and database per engine, and the in-server statement timeout each engine spells differently (`statement_timeout` / `call_timeout` / `read_timeout` / `command_timeout`). This code used to live inside the metrics app, so `sql_run` could not reuse it and supported SQL Server only — which is why `/spbot_sql_to_xlsx` refused a PostgreSQL target. Running the SQL is separate and already shared (`sql_execution.execute_cursor_batches`). | `connect_engine`, `normalize_db_type`, `default_database`, `parameter_style`; `DbConnectError`; `SUPPORTED_DB_TYPES` |
| `ssh.py` | **Single source of truth** for the SSH *connection*: opening a paramiko client (key or password, no silent agent/interactive fallback unless asked). Connect failures are classified where the paramiko exception type is still available, so callers never pattern-match a message to tell "wrong password" from "host unreachable". Auth **resolution** left on 2026-08-15 — `resolve_ssh_key` (a bare name resolves inside **`data/ssh_keys/`**) and `resolve_ssh_password` (value > env var > encrypted-secret ref) are `data_sources.ssh_auth`, and the four exception names are `lib.ssh_errors`, because four app modules were importing this transport for a key path or one word. All of it is re-exported here. | `open_ssh_client`; re-exported: `resolve_ssh_password`, `resolve_ssh_key`, `ssh_keys_dir`, `SSH_KEYS_DIRNAME`, `SshError` + `SshAuthError` / `SshConnectError` / `SshTimeoutError` |
| `remote_exec.py` | **Single source of truth** for *reaching a VM and running a command on it*, over `ssh` (paramiko), `winrm` (pypsrp, or a local `Invoke-Command` wrapper) or `local` (subprocess). **Input is a JSON object** — the same `cmd_access` shape stored in `db_instances.json`, optionally plus a `remote_credentials` entry — so config travels into the API untranslated; output is a `RemoteResult` that is JSON-shaped too. Also owns the `Invoke-Command` *builder* for callers that compose a remote script and run it through their own runner. See [the section below](#reaching-a-vm-remote_exec). | `open_session`, `run_command`, `run_script`, `RemoteAccess.from_json`, `RemoteResult`, `shell_prelude`, `build_invoke_command_script` / `build_invoke_command_argv`, `resolve_secret_value`; `RemoteExecError` and its subclasses `RemoteAuthError` / `RemoteConnectError` / `RemoteTimeoutError` |
| `time_window.py` | **Single source of truth** for the scheduling convention shared by the daemon, sql_tasks, metrics, and reports: allowed time windows, `repeat_interval`, retry/recover, and the `RUN_ONCE` (0) / `MANUAL_ONLY` (-1) semantics — run-once still runs the first time, manual never runs unless forced. `from_* > to_*` is a **wrapping range** on every dimension (month/day/hour/minute): `from_hour=22, to_hour=6` = 22:00 through 06:00 the next morning; `from_day=25, to_day=5` = 25th through the 5th of the next month. No app may parse, evaluate, or explain a time window with its own comparisons — always these functions. | `parse_time_window_config`, `is_time_window_open`, `time_window_closed_reason` (human-readable skip reason), `repeat_due`, `job_due`; constants `RUN_ONCE = 0`, `MANUAL_ONLY = -1`, `ERROR_STATUSES` |
| `listing.py` | **Single source of truth** for what a `/spbot_list_*` reply shows: the entries an operator can act on. A disabled entry is dropped (it cannot be run, and offering it invites someone to type an id that will be refused), and the listing then says **how many** it dropped — hiding without accounting is indistinguishable from losing. Five commands implement this rule (backups, restores, server targets, sql_tasks, metrics); before this module they spelled "off" four different ways and two of them showed disabled entries as if they were runnable. | `active_only(items, key=...) -> (kept, hidden)`, `hidden_note(hidden, noun=...)`, `is_active(item)` |
| `policy_engine.py` | Report/severity policy: normalize a metric row's status, apply per-metric and per-instance severity overrides, and render policy events. Drives how reports and alerts classify a row. | `apply_report_policy`, `render_policy_event`, `normalize_status`, `row_status`; `STATUS_ORDER` (SUPPRESS<OK/LOGGING<NO_DATA<WARNING/ERROR<CRITICAL) |
| `event_policy.py` | Normalize error messages into stable **error types** and **signatures**, and derive a report `event_code` from `(collector_type, category, error_type, metric_code)` for dedup/notification. | `normalize_error_type`, `normalize_error_signature`, `report_event_code`; pattern sets `CONNECT_FAILED_PATTERNS`, `AUTH_FAILED_PATTERNS`, `PERMISSION_DENIED_PATTERNS` |
| `target_flags.py` | Read per-target feature toggles uniformly (dict or object), with sensible defaults, so every app agrees on whether a target has metrics/reports/alerts enabled. | `is_target_enabled`, `is_metrics_enabled`, `is_reports_enabled`, `is_alerts_enabled` |
| `json_io.py` | **The one JSON reader.** `load_json_file` (BOM-tolerant `utf-8-sig`, root must be an object) and `looks_like_json_request` (does this CLI argument carry a JSON request?). Three byte-identical copies of the loader existed across `sql_execution`, `jobs/daemon` and `metrics/definitions` until 2026-08-06; `sql_execution` re-exports it so existing imports still work. | `load_json_file`, `looks_like_json_request` |
| `telegram_severity.py` | The severity-emoji vocabulary and the tagging applied once at the Telegram send layer. Lives here, not in the Telegram app, because `telegram_queue` validates every queued `message_type` against it and `common/cli` echoes the resolved type — the shared layer may not import an app. `db_ops/telegram/severity.py` remains a re-export shim. | `decorate_message`, `classify_message`, `normalize_message_type`; `MESSAGE_TYPES`, `SEVERITY_EMOJI`, `STATUS_EMOJI_CHARS`, `PLAIN` |
| `telegram_queue.py` | **Single source of truth** for putting one outgoing message into `telegram_send_messages`. Validates the `message_type` against the severity vocabulary and derives it from level/phase/status when a caller does not state one, so every producer queues the same row shape. | `queue_telegram_message`, `message_type_for` |
| `backup_policy.py` | Reads `data/backup_policy.json`: per-server backup expectations (which backup types, how often, how stale is too stale) that the backup metrics and the restore-drill status both judge against. | policy loading and per-server lookup |
| `restore_drill.py` | Was a restore actually *proven* for this database inside its policy window — the question behind the `restore-drill-status` CLI. A backup that exists is not a backup that restores. | drill status per database |
| `capacity_forecast.py` | Growth//headroom projection from stored metric history — when a disk or a database is expected to run out, rather than only whether it is full now. | forecast helpers |
| `session_trace.py` | Every open transaction on a SQL Server, with the **application user** behind it. On a three-tier estate every session reads `login=<service account> host=<app server>`, naming nobody; Dynamics AX writes the caller into `sys.dm_exec_sessions.context_info` and this decodes it, then resolves the id through `USERINFO`. Returns age, log bytes written, locks held and who is blocked — `log_bytes_used` is what separates "a client walked away mid-transaction" from "work in progress", which need opposite responses. Runs through `sql_run`, so it is read-only by construction. **`sql_id` 18 runs a similar query as a SQL task, and that is deliberate, not duplication** — see `docs/05_sql_task_runner.md`. | `trace_sessions`, `describe` |
| `interval_rates.py` | Pulling a collector's `key=value` fields back out of the message that carried them, and differencing two stored samples of a cumulative counter into a rate. Shared so the report and the alert split the same sample the same way. The arithmetic was withdrawn on 2026-08-11 with `PERFORMANCE_IO_LATENCY`'s interval *grading* and came back on 2026-09-03 for interval *reporting*, which no collector calls and no severity depends on — see `docs/04_metrics_engine.md`. | `message_fields`, `interval_delta`, `window_delta`, `per_second` |
| `health_model.py` | The shared health roll-up: how individual metric statuses combine into a target's overall health. | health aggregation |
| `state_transition.py` | Detecting that a status *changed* (OK -> CRITICAL and back), so alerting can fire on transitions rather than on every repeated observation. | transition detection |
| `report_archive.py` | Retention/archival of generated reports under `runtime/reports`, so the folder does not grow without bound. | archive/prune helpers |
| `metric_targets_config.py` | Resolve/enumerate config-defined metric targets from the data folder, honoring the `target_flags` toggles; used by the metrics and reports apps. | `resolve_config_metric_target`, `load_config_metric_targets` |
| `metric_store.py` | The metric store (`metric_runs` / `metric_results` / archive), on SQLite or PostgreSQL via `db/backend.py`. **Moved here from `metrics/storage.py`** because three other apps read it — `jobs.status`, `reports.inventory_health`, `reports.server_report` — which made it the codebase's only standing exception to "apps depend on `common`, never on each other". Shared API living inside an app is what forces such an exception, so the store moved and the exception went away. `db_ops/metrics/storage.py` remains as a re-export shim. The row shape it persists moved with it, to `common/metric_results.py` (in `db_ops/db/` until that package was folded in on 2026-08-15), since `common` may not import an app. | `MetricStore`, `cutoff_text`, `ensure_sqlite_column`; `METRIC_SCHEMA_VERSION`, `SCHEMA_SQL` |
| `sla_store.py` | The SLA store (`sla_runs` / `sla_results`). **Moved here from `sla/storage.py`** on 2026-08-11 for the reason above, plus a concrete one: `db/cli.py` composes the runtime store's schema from the four classes that own its tables, so two of them living in apps forced the `db` layer to import *up*. Its persisted shapes moved to `common/sla_results.py`; `SlaPolicy` stayed in the app, because a policy is config the app parses, not a row this writes. | `SlaStore`; `SLA_SCHEMA_VERSION`, `SCHEMA_SQL` |

### The four shape modules — a tier of their own inside `common`

`job_runs.py`, `metric_results.py`, `metric_definitions.py` and `sla_results.py` are the row
shapes and predicates that a store and its writer both have to agree on. They were the package
`db_ops/db/` until **2026-08-15**, folded in because a whole package for 238 lines of
frozen dataclasses was one layer more than the tree needed.

**What did not change in the fold, and must not:** each of these four imports **nothing but
stdlib** — no `db_ops` module at all. That is why `db/store.py` can name `JobRun` without dragging
the shared library in behind it (`import db_ops.db.store` still reaches 7 modules, none of them
library code). `common` imports `db` in three places; if a shape ever imported a helper, the store
would start depending on that helper and the two lowest layers would become mutually entangled
for real. A shared data shape goes in `common` as a dependency-free module of its own — never
with an import in it.

| Module | Responsibility | Key public API |
| --- | --- | --- |
| `job_runs.py` | One job-run record, and the metadata a Telegram-triggered run files. `metadata` is free-form JSON at the storage layer, which is why the audit shape is pinned here — it is what an operator greps `job_runs` with when asked "who ran this, against what". Two apps built the same dict independently until 2026-08-06. | `JobRun`, `telegram_log_metadata` |
| `metric_results.py` | One collected metric row, shared between the metrics app and `metric_store.py`. | `MetricResult`, `rows_by_target` |
| `metric_definitions.py` | What a metric definition promises to anyone filtering on engine type. The metrics app loads and validates definitions, but Reports also filters them (one section per engine) and may not import an app — both sides had the predicate written out twice until 2026-08-06. | `definition_supports_db_type` |
| `sla_results.py` | The shapes the SLA app persists, shared with `sla_store.py`. `SlaPolicy` deliberately stayed in `sla/models.py`: a policy is config the app parses, not a row anything writes. | `SlaPolicyResult`, `SlaValidationSummary`, `state_key` |
| `backup_restore_history.py` | The backup/restore history store (`backup_restore_history`). **Moved here from `backup_restore/history.py`** on 2026-08-11, same move and same reason. | `BackupRestoreHistory`; `HISTORY_SCHEMA_VERSION` |
| `inventory_render.py` | Merging a health overlay into the canonical inventory and rendering the dated summary — shared by the master-side `control` app and the worker-side `reports` app, which each held a copy (265 identical lines) until 2.33.00. The copies had already drifted; the reports version was a strict superset (reads the newer `backup_evidence` block, takes disks from `merged_drives`, surfaces curated `findings`) and is the one kept, so the master-side output gained those sections rather than losing any. What stays app-side is what genuinely differs: `control` SSHes to the worker and SFTPs the overlay back, `reports` runs store-local. | `build_inventory_summary`, `merged_drives`, `merged_sql_resources`; `DEFAULT_INVENTORY`, `HEALTH_BLOCKS`, `DBTYPE_LABEL`, `DISK_WARN_PCT`, `DISK_CRIT_PCT` |
| `secret_check.py` | **Single source of truth** for *proving a secret still logs in somewhere* — the read-only sibling of `password_rotation`, sharing its target resolution so an audit and a rotation can never disagree about where a secret lives. Resolves a ref by walking **every** config that can name it (`db_instances` database login or `cmd_access`, `docker_db_connections` — which carries the published non-default port — `restore_config`, `users.json` `remote_credentials`) before falling back to the standard key name. When `cmd_access` does not state a method the protocol is **probed** (SSH 22, then WinRM 5985/5986) rather than assumed. Distinguishes the four things that all used to read "unreachable": `UNREACHABLE` (nothing answers), `NO_MANAGEMENT_PORT` (host is up on RDP but has no scriptable way in), `AUTH_FAILED` (the credential is wrong), `NOT_A_LOGIN` (key material or a service token). **Input is a JSON object**; `{}` checks the whole store. | `check`, `check_ref`, `resolve_check_target`, `oracle_service_for_host`; `SecretCheckError`; `NOT_A_LOGIN`, `HTTP_LOGINS`, `SSH_PORT`, `WINRM_PORTS` |
| `password_rotation.py` | **Single source of truth** for *changing a database login's password* — on the server **and** in the secret store, as one operation. A rotation done as two steps drifts: an `ALTER LOGIN` nobody records leaves db_ops authenticating with a dead password; a store edit nobody applies leaves a password the server never accepted. Fixed order per target: connect with the current password (a target whose current password already fails is **skipped**, never guessed at), issue the engine's change statement, **re-authenticate on a new connection** (the session that issued the change stays valid, so checking on it proves nothing), then store. A failed verify is rolled back inline with the value the process still holds. Every target gets its **own** generated password. **Input is a JSON object**, like `sql_run` and `remote_exec`. See [the section below](#rotating-a-login-password-password_rotation). | `rotate`, `rotate_ref`, `persist_rotated`, `strip_secrets`, `generate_password`, `build_change_statement`, `select_refs`, `resolve_ref_target`, `target_from_ref_name`; `PasswordRotationError`; `SUPPORTED_ENGINES`, `DEFAULT_PASSWORD_LENGTH = 28`, `MIN_PASSWORD_LENGTH = 12` |
| `evidence.py` | The **gate model** every runbook-style operation reports through, and its JSON evidence file. One named check, a verdict (`OK` / `WARN` / `FAIL`), a sentence an operator can act on, and two independent flags: `blocking` (a failure stops the run) and `override` (a blocking failure an operator may accept deliberately). Gates are echoed as they run — a 30-minute restart that speaks only at the end is indistinguishable from a hung one — and written to `runtime/evidence/<operation>/<run_id>.json`, one file per run, never overwriting an older one. | `GateReport` (`add`, `note`, `say`, `blockers`, `passed`, `status`, `to_dict`, `write`), `Gate`, `new_run_id`; `OK`, `WARN`, `FAIL`, `SKIP`, `DEFAULT_EVIDENCE_ROOT` |
| `confirm.py` | **The one place db_ops asks a human before doing something it cannot undo.** Every dangerous operation — restart, service stop, cumulative update, and whatever is added next — calls `require_confirmation`, so the control behaves identically everywhere. Two locks that answer different questions: `"confirm": true` is *intent* (this payload means to change a machine), typing `yes` at the prompt is *presence* (a human is reading **this** target now). The prompt names the target and the consequence — "are you sure?" with no content trains people to answer without reading. With no terminal the run is refused unless the request declares `"assume_yes": true`. See [the section below](#asking-before-something-irreversible-confirm). | `require_confirmation`, `banner`, `is_interactive`, `open_terminal`, `read_answer`; `CONFIRM_WORD = "yes"` |
| `sqlserver_instance.py` | **Single source of truth** for *what a SQL Server backup leaves behind*. Oracle and PostgreSQL are backed up physically — RMAN `DUPLICATE` rebuilds from the datafiles and users live inside the database; `pg_basebackup` copies the cluster and roles live in `pg_authid` — so a restore answers as the source did. SQL Server is backed up per user database (`WHERE database_id > 4`), so `master`/`msdb`/`model` and everything in them are absent after a restore: logins, server roles and permissions, credentials, linked servers, endpoints, `sp_configure`, Database Mail and the whole of SQL Agent. This exports them as deterministic, guarded SQL and replays them onto a newly installed instance, including a newer major version. **Input is a JSON object.** See [the section below](#sql-server-instance-metadata-sqlserver_instance). | `export_instance`, `replay_instance`, `verify_instance`; `load_policy` (a translation over `data_sources.load_sqlserver_instance_policy`), `read_bundle`, `resolve_secrets`, `major_version`; `SqlServerInstanceError`. The bundle's *shape* — `PRE_DATABASE`, `POST_DATABASE`, `SERVER_DIR`, `MANIFEST_NAME`, `artifacts_in_order` — is `lib.instance_bundle` since 2026-08-15: `backup_restore` reads all five while validating config, before there is anything to connect to |
| `host_ops.py` | **Single source of truth** for *operating on a host* — read its state, control its services, restart it and prove it came back — on **Windows and Linux alike**. `remote_exec` reaches a machine; this is what maintenance actually asks for on top. `cmd_access` resolution used to live here too and is `lib.cmd_access` since 2026-08-15 — pure functions over a config block that `metrics` reads while loading its target list; the names are re-exported here because operating on a host is where callers look for them. **Input is a JSON object**; output is a `GateReport` dict. See [the section below](#operating-on-a-host-host_ops). | `host_facts`, `service_control`, `restart_host` (the JSON entry points); `resolve_host`, `open_host_session`, `read_facts`, `service_states`, `wait_for_services`, `wait_for_port`, `is_service_up`, `check_maintenance_window`, `load_maintenance_policy`, `parse_json_output`; re-exported from `lib.cmd_access`: `resolve_cmd_access`, `resolve_cmd_credential`, `resolve_platform`, `SUPPORTED_CMD_ACCESS_METHODS`, `SUPPORTED_PLATFORMS`; `HostTarget`, `HostOpsError`; `DEFAULT_POLICY` |
| `db_catalog.py` | **Single source of truth** for *what is in a server*: its databases and their state, and the schemas inside one database. Both were being answered ad hoc — four spellings of `SELECT name FROM sys.databases` across four engines — and the Telegram spreadsheet upload needs all of them just to prompt. Each engine answers in **its own vocabulary** rather than a flattened common one: SQL Server's `state_desc` and recovery model, PostgreSQL's owner/encoding/`datallowconn`, Oracle's **containers** (a CDB reports root + seed + every PDB with its `open_mode`; `kind` says which is which). Oracle has two shapes and the caller must not care: `v$containers` is 12c+, so a non-CDB falls back to `v$database` — and that query is itself tried richest-first, because `database_role` is 9i+ and an 8.1.7 host answers ORA-00904 for it. Oracle's upper-cased column names are folded so `row["name"]` reads the same on every engine. System objects are hidden unless asked for. **Input is a JSON object**; resolution and connection are `sql_run`'s, not this module's. | `list_databases`, `list_schemas`; `DbCatalogError` |
| `xlsx_import.py` | **Single source of truth** for *reading a spreadsheet* — the mirror of `xlsx_export`, and **standard library only** for the same reason: openpyxl would be a new image dependency, so loading a file would need a rebuild and a redeploy first. Handles what the format actually does rather than what it looks like it does: Excel **omits empty cells**, so rows are placed by their `A1` reference (reading positionally shifts every later value one column left, on that row only); text is usually a pointer into a shared string pool and a formatted heading is split across runs; `sheet1.xml` is not reliably the first tab. Dates are the one interpretation: a date is *a number plus a style*, so `xl/styles.xml` is parsed far enough to know which cells are date-formatted and those are rendered ISO — otherwise the most common column in an operational sheet arrives as `45678`. Header blanks become `column_N` and duplicates get a suffix, because a header row is written for a human and column names cannot be either. | `read_sheet`, `header_names`, `decode_payload`, `generate_table_name`, `column_index`; `XlsxImportError`; `MAX_TEXT_LENGTH`, `DEFAULT_MAX_ROWS` |
| `delimited_import.py` | **Single source of truth** for *reading a delimited text file* as a header row plus data rows — the sibling of `xlsx_import`, same output shape, same promise that every value stays text. It exists because what operators attach is rarely a clean `.xlsx`: it is a block selected in Excel and pasted into Notepad, or an export from a tool that only writes text, and those were refused with *"Not an XLSX file (it is not a zip)"* — true, and useless against a file that looks exactly like a spreadsheet. Three things are guessed, each with a wrong guess that corrupts silently: the **encoding** (Excel's "Unicode Text" is UTF-16 with a BOM and decodes under UTF-8 without raising, into NUL-riddled column names; plain "Text (Tab delimited)" is the Windows codepage; cp1252 is the last resort because it maps every byte), the **delimiter** (counted outside quotes on the header line, tab first — a comma is data in half the files that use tabs), and where the **header** is (the first line that says anything). Quoting is `csv`'s, not `str.split`'s: a cell containing the delimiter is written quoted by everything that writes these files. A trailing delimiter on the header does **not** become `column_N` (a pasted line always ends with one), but a blank heading *between* named ones does — dropping it would shift every column after it. A row with more values than the header names is **refused with its line number**, because padding or clipping loads the file one column out of step and says nothing. Header naming is `xlsx_import.header_names`, shared, so the same data gives the same table whichever format it arrived in. | `read_table`, `decode_text`, `sniff_delimiter`, `resolve_delimiter`, `describe_delimiter`; `DelimitedImportError` |
| `table_load.py` | **Single source of truth** for *building a table from an uploaded file and loading it*, on any engine — an `.xlsx` **or** a delimited text file, decided by the file's first bytes and never by its name (a workbook is a zip; a renamed attachment is routine), then read through `xlsx_import` or `delimited_import` so nothing below this point knows which arrived. The file key is `file_base64` / `file_path`; `xlsx_base64` / `xlsx_path` still mean the same thing, because that is what the shipped Telegram command config and every saved shell payload say. Owns exactly three things `sql_run` does not: quoting an identifier per engine, binding values per driver, and the create-then-load transaction. **Every column is text** (`NVARCHAR(4000)` / `varchar(n)` / `VARCHAR2(n CHAR)` / `TEXT`) — a type guessed from the first twenty rows is wrong on row two thousand in a way nobody notices until a join returns nothing. Two hazards it exists to contain: a **column name comes out of a file somebody was emailed** and cannot be a bind parameter, so quoting with a doubled closing quote is the whole defence; and the **placeholder depends on the process, not the engine** — pg8000 reads `paramstyle` from a module global that `db/backend.py` sets to `qmark`, so PostgreSQL wants `?` in the daemon and `%s` in a bare CLI run (asked of `db_connect.parameter_style`, never assumed). An existing table is **never** overwritten unless `if_exists` says so, and an over-long value is refused naming its row and column rather than clipped. | `create_table_from_xlsx`, `read_source`, `quote_identifier`, `placeholder_style`, `build_placeholders`, `two_placeholders`, `build_create_table`; `TableLoadError`; `BATCH_SIZE`, `IF_EXISTS_CHOICES`, `DEFAULT_TEXT_LENGTH` |
| `schema_copy.py` | **Single source of truth** for *reproducing one SQL Server schema on another instance*. `table_load.py` covers a file into one table; this covers "make schema `X` on instance B look like schema `X` on instance A". Nine phases in dependency order — partition function/scheme, change tracking on the database, tables, change tracking per table, indexes, checks, data, modules, foreign keys — with **FKs after data**, so load order cannot violate them. Every phase is **idempotent** (`IF OBJECT_ID(...) IS NULL`, `IF NOT EXISTS`, `CREATE OR ALTER`), because a run that dies in phase 4 has to resume by being run again rather than by being repaired. `plan` prints counts and statements and writes nothing; `apply` takes an `sp_getapplock` around the whole operation, because the "already has rows" guard is read-then-write and two appliers really did run against one target. Data moves **through the client** in batched `executemany` with `IDENTITY_INSERT` per table — `INSERT ... SELECT FROM [OtherDb]...` only works when both databases share an instance. **Input is a JSON object**, like `run-sql` and `rotate-password`. See [the section below](#copying-a-schema-between-instances-schema_copy). | `copy_schema`, `build_plan`, `apply_plan`, `plan_steps`, `verify_copy`, `copy_table_data`, `select_tables`, `select_modules`, `assert_destination`, `application_lock`, `format_plan`; `SchemaCopyError`; `SchemaCopyRequest`, `Endpoint`, `Step`; `PHASES`, `DEFAULT_BATCH_SIZE = 2000`, `DEFAULT_TIMEOUT_SECONDS = 900`, `DEFAULT_LOCK_TIMEOUT_SECONDS = 300`, `DEFAULT_MODULE_PASSES = 4` |
| `schema_catalog.py` | **Single source of truth** for *what SQL Server's catalogue views say a schema contains* — the read half of `schema_copy`, kept apart because reading a catalogue and writing DDL fail differently and are worth testing separately. Also owns the answer to the question the feature request called worth as much as the copying: **`unsupported_features` lists what a copy will silently drop**. Scripting from `sys.tables` alone loses partitioning (a UAT hop shipped 0 of 32 partitioned indexes), change tracking (a `CREATE PROCEDURE` failed with Msg 22105 mid-deploy), filegroups, compression, temporal tables, extended properties and permissions. Reporting them is not the same as carrying them, and saying which is which is the point. | `unsupported_features`, and the per-object readers `schema_copy` plans from |
| `result_format.py` | **Single source of truth** for *how a result set is rendered*: `json` (default, the only one a program should parse), `txt` (aligned table for a terminal), `csv` (RFC 4180 via the stdlib writer, header row included), `xml` (structure without a JSON parser), `xlsx` (writes a workbook, via `xlsx_export`), `raw` (values only, tab-separated, no header — so `\| cut -f2` works). Chosen inside the JSON request as `"format"`, never a flag, so a config file can carry it. `xlsx` was already here but reachable only from `sql_tasks` config; the rest existed nowhere and were being improvised by piping JSON into whatever the operator remembered. **A SQL NULL stays distinguishable from an empty string in every text format** — rendering both as nothing silently answers a question nobody asked. `csv` does it PostgreSQL's way (`COPY ... WITH CSV`): an empty *unquoted* field is NULL, `""` is the empty string; numbers stay unquoted so a spreadsheet reads them as numbers. Column names become XML *attributes*, not tags: SQL returns columns called `1` or `count(*)` and neither is a legal element name. `write_result` is the single entry point for "put this result set in a file", whatever the format — callers get one call and no branch, which is what `sql_tasks` now uses for all of `xlsx`/`csv`/`txt`/`xml`. | `render_result`, `write_result`, `normalize_format`; `ResultFormatError`; `RESULT_FORMATS`, `NULL_TEXT` |
| `file_transfer.py` | **Single source of truth** for *moving one named file* between this host and a remote one, and for *packing a set into one archive* so it can be moved as one. The apps that move files do it inside a larger job — `backup_restore.transfer` syncs a whole backup directory between two remote hosts, `backup_restore.copy_backup` pulls a window of backups off an SMB share — and neither answers "put **this** file **there**", so that kept being typed by hand as `ssh`/`scp`/`docker cp`. Every transfer is size-verified and a short copy deletes what it wrote; overwriting must be asked for and lands atomically; a same-size destination is skipped; mtime is preserved (the restore log-chain filter reads it). `pack_files` builds the archive **on the host that already holds the files** and returns its `sha256` — size catches a truncated copy, not a corrupted one. **Not** for staging a backup set: per-file SFTP across two internet hops measured 10 KB/s, which is why `backup_restore.transfer` streams a directory as one `tar`. **Input is a JSON object.** | `fetch_file`, `send_file`, `pack_files`; `FileTransferError`; `STATUS_COPIED` / `STATUS_REPLACED` / `STATUS_SKIPPED_EXISTS`, `PARTIAL_SUFFIX` |
| `sqlserver_patch.py` | The SQL-Server-specific half of a cumulative update: the "is this instance safe to patch" gate set, the unattended `setup.exe /Action=Patch` contract with its exit-code rules (**3010 = applied, restart required — never re-run**), and the build verification that reads `SERVERPROPERTY` first and registry **`PatchLevel`** (not `Version`) as corroboration. Everything platform-generic underneath is `host_ops`. See [the section below](#patching-a-sql-server-instance-sqlserver_patch). | `precheck`, `apply_cu`, `verify_build` (the JSON entry points); `patch_arguments`, `patch_exit_verdict`, `sqlserver_service_names`, `sqlserver_registry_key`, `setup_log_root`, `version_tuple`; `SqlServerPatchError`; `EXIT_SUCCESS_RESTART_REQUIRED = 3010` |
| `ops_status.py` | **Is db_ops itself running** — the one question no other app here asks. Everything in db_ops watches databases; nothing watched db_ops, and on 2026-08-12 a NameError in the SQL task scanner made every scheduled scan exit 1 once a minute for a day while the daemon stayed up, the container stayed up and the metric reports kept arriving. Not one scheduled SQL task ran, and a person found it by noticing an absence. Reads `job_runs` and `app_commands.json` and answers two things the estate's own monitoring cannot: **overdue** (an app that stopped being *scheduled* writes no failure row at all, so "no errors" is not health) and **failed since the last alert went out** (the alert's own queue row is the watermark, so an app broken since Tuesday does not message the group every minute for three days — the standing failures ride the periodic summary instead, the same split `sla_policies.json` makes with `reminder_after_seconds`). That question is asked **over an interval, never sampled at an instant**: the first version compared the two newest `job_runs` rows at the moment it happened to run and in seven weeks never sent one alert, because APP-CONTROL runs once a minute while APP-TELEGRAM runs every four seconds — a failure that came and went between two checks was invisible (2026-08-14). Both ends of a **restart** are excused, the daemon's shutdown rows and the stale-`running` rows it closes on startup, or every deploy would raise an incident. The summary's working-hours window is **local** and constrains only the summary; the failure alert ignores the clock, because an app that breaks at 03:00 is news at 03:00. Its state is the queue row it already writes (`source_type=ops_status`), so there is no state table to drift. CLI face: `common.cli ops-status`, scheduled as `APP-CONTROL`. | `build_ops_status`, `format_summary`, `format_failure_alert`, `summary_is_due`, `last_summary_sent_at`, `last_failure_alert_at`, `load_app_commands`; `FAILED_STATUSES`, `NON_FAULT_MARKERS`, `SOURCE_TYPE`, `SUMMARY_NOTE`, `FAILURE_NOTE` |
| `oracle_bridge.py` | **Single source of truth** for *reaching a legacy Oracle (8i / 8.1.7)*, which no driver db_ops can install speaks to — `oracledb` thin needs 12.1+, an 11.2 client refuses 8.1.7. The one combination that works (Oracle Client 10.2 + `cx_Oracle` 5.1.2 + Python 2.7 32-bit) lives apart as a self-contained tool — installed beside the project rather than shipped inside the package — that takes one JSON request on stdin; this module is everything that decides *what the tool is asked to do*. Two transports, per target via `sql_access.method`: **`subprocess`** runs the tool here (nothing to keep running, request never leaves the host — the request goes on **stdin**, never in argv, because an argument is readable in the process table) and **`api`** POSTs to the HTTP bridge that runs it on another host, sending `connect + mode + minute + nonce` as a short-lived (~90s) HMAC-signed token instead of the password. **The connect string is assembled per run** from the target's `users.json` credential + the encrypted store — never stored (`connect_ref` still overrides; the two that existed were deleted for duplicating a password). `schema_prelude` issues `ALTER SESSION SET CURRENT_SCHEMA` so an application's unqualified script runs under a DBA login instead of ORA-00942. | `run_query`, `run_bridge_query`, `normalize_sql_access`, `is_legacy`, `build_connect_string`, `resolve_connect`, `resolve_secret`, `connect_mode`, `schema_prelude`, `prepare_sql`, `subprocess_argv`, `encrypt_token`; `LegacyOracleError`; `SUPPORTED_SQL_ACCESS_METHODS`, `DEFAULT_TTL_SECONDS = 90`, `DEFAULT_TOOL_DIR` |

`event_policy`, the `PolicyEvent` output of `policy_engine`, and the metric-target
loaders together form the report/alert classification path; see
[`docs/06_reports_app.md`](./06_reports_app.md) and
[`docs/04_metrics_engine.md`](./04_metrics_engine.md) for how the apps consume them.

---

## Reaching a VM (`remote_exec`)

Four apps needed a machine they do not run on, and each had grown its own answer to the
same three questions — *how do I authenticate*, *how do I open the session*, *how do I run
a command and read back rc/stdout/stderr*. `remote_exec` answers them once.

### The JSON input

The access object is the `cmd_access` block already stored in `db_instances.json`, so a
caller passes its config straight through. Everything except `method` (and `host`, for a
remote method) has a default:

```jsonc
{
  "method": "ssh",            // ssh | winrm | local
  "host": "192.0.2.5",
  "port": 22,                 // default: ssh 22, winrm 5985 (5986 when ssl)
  "username": "ubuntu",       // may instead come from the credential object
  "platform": "linux",        // decides the default shell
  "shell": "bash",            // bash | powershell | cmd
  "auth_type": "key",         // ssh: key | password
  "key_file": "worker.key",   // bare name -> data/ssh_keys/, or an absolute path
  "password_ref": "vm_pw",    // or password_env (env var name), or password (literal)
  "timeout_seconds": 30,      // opening the session
  "command_timeout_seconds": null,  // running a command; null = unbounded
  "ssl": false                // winrm
}
```

**`timeout_seconds` and `command_timeout_seconds` are different questions and never share a
number.** Opening a session to a reachable host takes seconds; the command it then runs may
legitimately take an hour (`docker compose up` pulling a 1.5 GB image, an RMAN duplicate, a
restore). So a command is **unbounded by default**, like a plain `ssh host cmd`. A caller
that wants a bound passes `timeout_seconds=` to `run()`/`run_script()` — metrics does, per
metric — or sets `command_timeout_seconds` on the access object for the whole session.

A second JSON object — a `remote_credentials` entry — can carry `username` and the
password fields; the access object wins on any key both define.

### Using it

```python
from db_ops.common import remote_exec

# one command, connection opened and closed for you
result = remote_exec.run_command(access, "systemctl is-active docker", secrets=secrets)
result.exit_code, result.stdout, result.stderr, result.duration_seconds

# several commands over one connection
with remote_exec.open_session(access, credential=credential, secrets=secrets) as session:
    session.run(["docker", "compose", "up", "-d"], cwd="/opt/db_ops/containers").check()
    session.run_sudo("systemctl restart docker", sudo_password=pw)
    session.write_text("/opt/db_ops/containers/pg_01/.env", env_text)   # SFTP, ssh only

# ship a whole script (text or a local file) and run it there
remote_exec.run_script(access, Path("db_ops/metrics/collectors/os/windows/005_os_service_status.ps1"), env={"OS_SERVICE_NAMES": "W32Time"})
```

Rules worth knowing:

- **`result.check()`** turns a non-zero exit into a `RemoteExecError` carrying the output;
  without it a non-zero exit is data, not an exception.
- **Errors are typed.** `RemoteAuthError` (credential rejected) vs `RemoteConnectError`
  (host unreachable) vs `RemoteTimeoutError` — different fixes, so no caller has to
  pattern-match a message.
- **`RemoteAccess.to_dict()` redacts the password**, so an access object is always safe to
  put in a log line or an event payload.
- **Env vars travel inside the script.** SSH does not forward the environment and WinRM
  starts a fresh session, so `env=` is rendered into a prelude (`shell_prelude`) prepended
  to the script text, quoted per shell.
- **`local` is a method**, not a special case — an app that may or may not be remoting
  writes one code path.

### Who uses which face

| Caller | Face used |
| --- | --- |
| `metrics.collector` (`execute_ssh` / `execute_winrm`) | `run_script` — the metric contract (JSON stdout → rows) stays in the collector |
| `sre.remote.RemoteUbuntuHost` | `SshSession` for run + SFTP; the class adds the `CompletedProcess` shape the provisioner expects |
| `backup_restore.copy_backup.open_ssh_connection` | `open_session(...).client` — a raw paramiko client, because the restore paths use SFTP and incremental channel reads directly |
| `backup_restore.preflight` (remote SMB share) | `run_script` over WinRM |
| `backup_restore.restore_database` / `certificate` | `build_invoke_command_argv` — they compose the remote script and run it through their own runner (retry, progress logging, target-context guards) |
| `control._support.ssh_connect` | `common.ssh.open_ssh_client` directly: `ssh_run` streams output live off the channel, which the request/response sessions do not do |

---

## Asking before something irreversible (`confirm`)

Restarting a host, stopping a database service and applying a cumulative update are different
operations with the same failure mode: **the right command aimed at the wrong machine**. They
therefore share one control — `confirm.require_confirmation` — rather than one each. A safety
control that is spelled differently per command is one an operator cannot rely on.

### Two locks, and they are not the same lock

| Lock | What it proves | Who provides it |
| --- | --- | --- |
| `"confirm": true` in the request | **Intent** — whoever composed this payload meant to change a machine | the caller |
| Typing `yes` at the prompt | **Presence** — a human is reading *this* target, right now | the operator |

A payload is a file, a shell history entry, a Telegram action: it can be replayed, copied, or
written for another host. A person cannot. That is why intent alone is not enough at a terminal.

```
========================================================================
  DANGEROUS OPERATION — this changes a live system
========================================================================
  Operation : host-restart
  Target    : ACME-192-0-2-250 (windows, ssh) — 192.0.2.250
  Effect    : APPDB-DB will be REBOOTED now
              every session, service and running job on it is interrupted
              services that must come back: MSSQL$APPDB, SQLAgent$APPDB
              it has been up 281.2 days
  Reason    : clear PendingFileRenameOperations before CU26
========================================================================
  Type "yes" to proceed (anything else aborts):
```

The prompt names the target and the consequence on purpose. A bare "are you sure?" carries no
information, so people learn to answer it without reading — which is the behaviour a
confirmation exists to prevent. `apply-cu` adds the one fact that decides whether the night is
recoverable: *a cumulative update cannot be uninstalled; rollback means restoring a snapshot.*

### The rules

- **The whole word `yes`.** Not `y` — that is what a hand types while reading something else.
  Case is ignored, surrounding whitespace is stripped, everything else aborts.
- **Ctrl-C, EOF or a closed stdin mean no.** There is no input that means "carry on".
- **`dry_run` is never asked to confirm.** Rehearsing is not performing.
- **Automation must declare itself.** With no terminal to ask on, the operation is **refused**
  unless the request carries `"assume_yes": true`. A scheduled job that forgot to say "no human
  will be asked here" fails loudly instead of quietly rebooting production at 03:00.
- **A piped request is not the unattended case.** `... host-restart - < request.json` leaves
  stdin exhausted, so the question is asked on the controlling terminal (`/dev/tty`, or `CON` on
  Windows) — which the pipe did not take away.
- **The authorization is recorded**, on the gate and in `facts.authorization`: a typed
  confirmation and an unattended one are different facts and must not read the same afterwards.

New dangerous operation? Call `require_confirmation` — do not write a second prompt.

### `authorize` — the gate for work this CLI does not perform

Every other gate belongs to the operation it guards: `kill-spid` confirms and then kills. A forced
SQL task run is the first case where the *operation* lives in an app — `sql_tasks` runs it — and
only the authorization belongs here. An app may not import `common` (ORD 13), and the alternative
to a command of its own is a second prompt written app-side, which is the thing this module exists
to prevent.

```bash
python -m db_ops.common.cli authorize @data/request.json
```

```json
{"operation": "run-sql-task", "target_id": "24",
 "target_label": "sql_id 24 payroll engine",
 "effects": ["runs on 1 target: ACME-192-0-2-115", "the active flag is skipped"],
 "confirm": "yes", "reason": "asked over telegram",
 "authorized_by": {"channel": "telegram"}}
```

`operation` is the row in `emergency_operations.json` that prices it — an operation the file does
not list costs **two** answers, not none. `effects` is what the caller knows and this layer does
not: which targets, which task, whether it is inactive. Exit 0 means authorized, and the gate
report is the JSON on stdout; the caller then performs the work itself.

One detail the caller does not see: the prompt is written to the controlling terminal rather than
to stderr, because `db_ops.lib.common_cli` captures both streams. A question written into a
captured pipe is invisible until after the answer was due, which is indistinguishable from a hang.

---

## Operating on a host (`host_ops`)

`remote_exec` answers *how do I reach a machine and run a command*. `host_ops` answers the
next question — *what state is this host in, are its services up, and can I restart it and
know it came back* — and answers it the same way for **Windows and Ubuntu/Linux**.

It exists because the answer had been written once, inside a single-purpose script: a SQL Server
cumulative-update patcher, hard-wired to one host, one instance and one KB. A task that only needed
a host restart could not reach any of it. That script's own execution report asked for the split,
capability by capability; this is it.

### The three capabilities

| Entry point | CLI | What it does | Platforms |
| --- | --- | --- | --- |
| `host_facts(request)` | `host-facts` | Read-only. Uptime, disks, services, pending reboot, identity — **one fact shape on both platforms** | Windows + Linux |
| `service_control(request)` | `host-service` | `status` (read-only) / `start` / `stop` / `restart`, then wait for the end state | Windows services + systemd units |
| `restart_host(request)` | `host-restart` | Restart and prove it came back: state before → wait down → wait up → wait services → state after | Windows + Linux |

### Two behaviours that are not the caller's business

- **Waiting for the transport to answer is not waiting for the host to be ready.** `sshd` is
  back long before SQL Server is. Sampling the services once when the port opens reported
  healthy services as down on *both* restarts of a completely successful CU run.
  `wait_for_services` polls to a budget and, on timeout, says how long it actually waited.
- **Without a down-wait, "it's back" is indistinguishable from "it never left."** A host that
  has not begun shutting down answers exactly like one that already returned, so `restart_host`
  waits for the port to close first (a miss is a warning, not a blocker — a very fast restart
  looks the same from here).

### Safety contract

One contract for every operation that changes a host, so there is nothing per-command to learn:

- **`confirm: true` *and* a typed `yes`** — see
  [the confirmation control](#asking-before-something-irreversible-confirm) below. An
  unauthorized change is refused with a gate, not silently downgraded to a dry run.
- **`dry_run: true`** resolves the target, runs every check, and prints what it *would* do —
  and is never asked to confirm, because rehearsing is not performing.
- **`window`** gates the operation on its approved maintenance window — either
  `{"start": "...", "end": "..."}` (the change-request form) or any `time_window` block, which
  is evaluated by `time_window` and nothing else. `ignore_window: true` still records the
  breach: the evidence shows both that the operation ran outside its window and that someone
  chose to.
- **`overrides`** accept a *named* blocking gate (`allow-stale-backup`, `allow-pending-reboot`,
  `allow-ha`, `ignore-window`). Overriding changes the verdict, never the record.
- **Timing budgets are config**, in [`data/maintenance_policy.json`](../data/maintenance_policy.example.json):
  built-in defaults < file `defaults` < `servers.<server_id>` < the request's `wait` block.

### Using it

```bash
# what state is this host in (read-only)
python -m db_ops.common.cli host-facts \
  '{"target": "ACME-192-0-2-250", "services": ["MSSQL$APPDB"]}' --key-base64 "<b64>"

# restart it and wait for its services — Windows or Ubuntu, same request
python -m db_ops.common.cli host-restart \
  '{"target": "ACME-192-0-2-250", "services": ["MSSQL$APPDB", "SQLAgent$APPDB"],
    "reason": "clear PendingFileRenameOperations before CU26", "confirm": true}' --key-base64 "<b64>"
```

Progress goes to **stderr**, the JSON gate report to **stdout**, so an operator can watch a
30-minute restart while a caller still pipes the result into `jq`. Exit 0 unless a blocking
gate failed.

---

## Patching a SQL Server instance (`sqlserver_patch`)

The Windows/SQL-Server-specific half. Everything under it — reaching the host, reading its
state, restarting, waiting for services, the gate/evidence model — is `host_ops` + `evidence`,
so a task that only needs a restart does not drag a patcher in. The full CU is a composition:

```
sqlserver-precheck -> host-restart -> sqlserver-precheck -> sqlserver-apply-cu -> host-restart -> sqlserver-verify-build
```

| Entry point | CLI | What it does |
| --- | --- | --- |
| `precheck(request)` | `sqlserver-precheck` | Read-only. Host gates (admin rights, disk, pending reboot, no setup running), installer gates (present, Authenticode, SHA-256, ProductVersion), SQL gates (standalone, sysadmin, databases ONLINE, recent full backup, running jobs, sessions), maintenance window |
| `apply_cu(request)` | `sqlserver-apply-cu` | Runs **every precheck gate again**, then the unattended patch, then reads setup's `Summary.txt` |
| `verify_build(request)` | `sqlserver-verify-build` | Read-only. `SERVERPROPERTY` first, registry as corroboration, services, databases |

Three rules are encoded here rather than left to the next operator:

- **`SERVERPROPERTY('ProductVersion')` is the verdict; the registry corroborates it — and the
  registry value is `PatchLevel`, not `Version`.** `Version` is the build *originally installed*
  and never moves when a CU is applied, so an RTM instance patched to CU26 keeps
  `Version = 16.0.1000.6` forever. Comparing it against the target build reported `FAIL` on
  every successful CU on every instance, which (with one other false failure) made a clean run
  exit non-zero — and an exit code nobody trusts is an exit code nobody reads.
- **Exit code `3010` is a success with one outstanding action**: the patch was applied and the
  host must be restarted. Reported as a failure it invites a second setup run on an
  already-patched instance; reported as a bare warning it invites skipping the restart.
  `patch_exit_verdict()` says both halves in one sentence.
- **The pending-reboot gate is a real blocker.** SQL Server setup evaluates
  `PendingFileRenameOperations` in its `RebootRequiredCheck` rule and refuses to patch while it
  is populated — 307 print-spooler entries cost the CU26 window its first restart. `precheck`
  catches it read-only, before anything is written.

`apply_cu` re-running every gate is deliberate and must not be relaxed: a precheck from an hour
ago proves nothing about a host that has since started a Windows Update, and the gates are cheap
compared to a half-applied CU.

---

## Running SQL on one database (`sql_run`)

`remote_exec` answers "reach this VM and run a command"; `sql_run` is its database twin —
"connect to this one database, run this SQL, hand me the rows". It exists for the same
reason: the Telegram `spbot_sql_to_xlsx` command had grown a private copy of target
resolution, the driver fallback, the row cap and the rollback contract, and any other caller
would have grown a second one.

### The JSON input

```jsonc
{
  "target": "ACME-192-0-2-115",   // server_id, or "<db_type> <ip> [port]"
  "sql": "SELECT TOP 10 * FROM sys.objects",   // or "sql_file": "assets/tasks/query.sql"
  "database": "SALESDB",               // optional; default = the instance's database
  "credential_name": "sqlserver_2.115_..._readonly",  // optional; alias "user_ref";
                                    // default = the instance's default_credential_name
  "max_rows": 50000,                // optional; the result is truncated past this
  "timeout_seconds": 30,            // optional; connect timeout
  "commit": false,                  // optional; default false = the batch is rolled back
  "autocommit": false,              // optional; true = no transaction at all
  "params": [505, "SALESDB"],          // optional; BOUND to the placeholders in the SQL
  "prelude": "DECLARE @spid int = ?;",   // optional; prepended to EVERY batch
  "capture": "first",               // optional; first (default) | all
  "max_result_sets": 20,            // optional; capture:all only, 0 = no cap
  "define": {"JOB_NO": "AA2503/00818"},  // optional; SQL*Plus &substitutions
  "sql_access": {"method": "api", "bridge_url": "..."},  // optional transport override
  "data_dir": null                  // optional data/ folder override (tests)
}
```

Rules worth knowing:

- **The request decides *which login*, and a login must be named.** With no `credential_name`,
  the run uses the instance's `default_credential_name` from `db_instances.json`, resolved in
  that server's `database_credentials` group in `users.json` (password from the encrypted secret
  via `password_ref`) — see [Credentials](#which-login-a-target-runs-as-credentials). If neither
  names one, the run is **refused**; nothing is inferred. That default is frequently a **DBA**
  account — `ACME-192-0-2-115` defaults to `dba_user` (role DBA) — so a read-only caller
  should name a least-privilege credential instead of inheriting it. The resolved
  `credential_name`/`username` come back in the result, so a log line can record who connected.
- **Values are bound, never pasted** (`params`, added 2026-08-15). The list goes to
  `cursor.execute` as a sequence, so the placeholders in the SQL must be the ones that target's
  driver reads — `?` for pyodbc, `%s` for pg8000 and pymssql. An **object is refused by name**:
  named binding is `:name` on Oracle, `%(name)s` on pg8000 and unsupported on pyodbc, and a dict
  silently coerced to a list would bind by insertion order and swap the moment somebody reordered
  the JSON. A request with no `params` still reaches the driver as `execute(sql)` with one
  argument — an empty list is "do not bind", not "bind nothing", because pg8000 raises on the
  latter. `prelude` is SQL prepended to **every** batch, for the T-SQL reason that a variable does
  not survive a `GO`; build it with `lib.sql_text.build_parameter_prelude`, which validates every
  name and type before either reaches the text. Neither works through the legacy Oracle bridge,
  which binds nothing — such a request is refused rather than run with the values dropped.
  This is what let the Telegram `sql_execute` commands, which write to production rows with
  arguments typed into a chat, stop opening their own connection.
- **One result set by default, all of them on request** (`capture`, added 2026-08-16). The
  default keeps the first and **drains** the rest — their rows are never fetched, only their
  rowcounts counted into `affected_rows` — because an export has one sheet and fetching a set
  nobody asked for is the unbounded read `max_rows` exists to prevent. `"capture": "all"` keeps
  them, and the answer carries `"result_sets": [{columns, rows, row_count, truncated}, ...]`.
  That key is **always present**, holding the single set under the default, so a caller reads one
  shape either way; `columns`/`rows`/`row_count`/`truncated` at the top still mean the first set,
  unchanged. Both caps report themselves: `truncated` per set for `max_rows`,
  `result_sets_truncated` at the top for `max_result_sets`. How many sets exist to keep is the
  **driver's** answer — `nextset` is probed, so three `SELECT`s come back as three sets through
  pyodbc and as one through pg8000, which has no `nextset`.
- **Rollback is the default, not a sandbox.** With `commit: false` the whole batch is undone,
  so `SELECT ... INTO #tmp` + a final `SELECT` works and a stray write to a real table is
  reverted (SQL Server rolls back DDL too). It does **not** undo non-transactional side
  effects — `xp_cmdshell`, `sp_configure` + `RECONFIGURE`, `BACKUP`/`RESTORE`, linked-server
  writes, `sp_send_dbmail`, `KILL`. The real control is the login you connect with.
- **Only the first result set is returned**; later sets are drained, and the rowcount of any
  batch that returned no result set is summed into `affected_rows` (reported, not rejected).
- **`GO` splits batches** (`split_sql_batches`), so a script pasted out of SSMS runs as-is.
- **`rows` holds native driver values** (`datetime`, `Decimal`); `json_safe_result()` returns
  a serializable copy.
- **`database` beats `USE <db>;`** — pin the database in the request instead of opening the
  SQL with a `USE`, so the run is one batch and the target is visible in the request object.
- **An Oracle 8i target is not connected to at all.** When the instance (or the request) sets
  `sql_access.method` to `api`/`subprocess`, the SQL goes to the legacy tool via
  [`oracle_bridge`](#modules) and comes back in this same result shape, plus `transport`
  and `db_version` — so an export does not care which path answered it. `committed` is False
  and `affected_rows` 0 because there is no connection to commit, not because they went
  unmeasured. On this transport **`database` means *schema*** (Oracle connects to a service):
  it issues `ALTER SESSION SET CURRENT_SCHEMA`, which is what lets a DBA login run an
  application's unqualified script instead of hitting ORA-00942.
- **`define` expands SQL*Plus `&substitutions`.** An archived `.sql` opens with
  `DEFINE JOB_NO = '...'` and refers to `&JOB_NO`; both are *client* syntax SQL*Plus resolves
  before the server sees the statement, so any driver fails on the literal `&`. The file's own
  `DEFINE` lines are the defaults and `define` overrides them, so the stored script stays the
  shipped one. A variable nobody defined is left as-is, so the error names it rather than the
  query silently changing meaning.

### Using it

```python
from db_ops.common import sql_run

result = sql_run.run_sql({"target": "ACME-192-0-2-115", "database": "SALESDB",
                          "sql": "SELECT query_id, last_execution_time FROM sys.query_store_query"})
result["columns"], result["rows"], result["row_count"], result["truncated"]
```

```bash
python -m db_ops.common.cli run-sql '{"target": "ACME-192-0-2-115", "sql": "SELECT 1 AS x"}'
python -m db_ops.common.cli run-sql @request.json     # or - to read the object from stdin
```

The CLI prints the JSON result and exits 0; a failure prints `{"ok": false, "error": ...}`
and exits 1, so a shell caller reads both outcomes from the same JSON.

**Reproducing a metric: pass `"autocommit": true`.** By default the batch runs in a transaction
and is always rolled back. Metric SQL catches per-database errors inside a cursor, and inside a
transaction one caught error dooms the whole thing — error 3930, *"the current transaction cannot
be committed"* — so every later statement fails. `db_ops.metrics.executor` connects with
`autocommit=True` for exactly that reason. Without the flag, running a metric's `.sql` here
reports a failure the collector would never have hit, which sends the reader after the wrong bug.
Nothing is rolled back in this mode, so use it for read-only SQL:

```bash
python -m db_ops.common.cli run-sql '{"target": "ACME-192-0-2-245",
  "sql_file": "db_ops/metrics/collectors/sqlserver/legacy_2008r2/069_sqlserver_linked_server_status.sql",
  "timeout_seconds": 240, "autocommit": true}'
```

### Which tool runs it, and who chose it

Added 2026-08-19. Until then `run-sql` knew the engine and never the *version*, so it could not
tell an Oracle 8.1.7 instance from a 23c one: it handed both to python-oracledb, which speaks 12.1
and newer, and an 8i target failed with `DPY-3010`. The rule now lives in
[`lib/target_profile.py`](./14_lib.md) and both `run-sql` and `run-cmd` read it.

**The request states the facts.** Every field is optional and empty means *use `db_instances.json`*,
so a request that states nothing behaves exactly as it always has:

| Field | For | Effect |
| --- | --- | --- |
| `major_version` | run-sql | the field that decides a driver |
| `driver` | run-sql (SQL Server) | name the driver — the counterpart `sql_access` already gave the transport |
| `oracle_client_mode` | run-sql (Oracle) | `thin` (default) / `thick` — thick is how a pre-12.1 server is reached without the bridge |
| `platform`, `os`, `os_major`, `os_minor` | both | which OS, and how old — `Get-CimInstance`/`ConvertTo-Json` are PowerShell 3.0 (NT 6.2) |
| `runtime` | both | `host` (default) / `docker` / `k8s` |
| `profile` | both | the same keys as one block, for a caller that has them all |

**The answer says what they selected.** `run-sql` returns `engine` (the merged profile, with a
`sources` map naming request-vs-config per field) and `tool` — `{tool, chosen_by, reason}` — on
*both* the direct and the legacy-bridge path, so one shape reads either transport. `run-cmd`
returns `host_profile`, `shell` and `shell_dialect`. `chosen_by` is `request | config | rule |
default`: it names where to go and edit when the choice surprises someone, which is the half that
was missing when pyodbc and pymssql were indistinguishable from the outside — and they bind
parameters differently.

**The precedence is one line**: explicit request → per-target config → profile rule → engine
default. What the caller states wins, because the caller is looking at the server and
`db_instances.json` is a file somebody typed.

**Unknown is never a guess.** 10 of 21 SQL Server instances carry no `major_version`; a rule that
behaved differently on an unknown version would have changed production behaviour the day it
shipped. What is refused is what is *known* to be impossible — Oracle below 12.1 in thin mode, with
both ways out named in the message:

```bash
python -m db_ops.common.cli run-sql '{"target": "ACME-192-0-2-136", "major_version": 8,
  "sql": "select * from v$version"}'
# Oracle 8 cannot be reached by python-oracledb in thin mode (it speaks 12.1 and newer).
# Either route this target through the legacy bridge with sql_access {"method": "api", ...},
# or install an Oracle client and set "oracle_client_mode": "thick".
```

A target already carrying `sql_access.method: "api"` is never asked the driver question at all —
it opens no driver, and refusing an 8i instance for being 8i is exactly what its bridge config
exists to avoid.

**`tool.actual` is what really answered**, next to the plan. On SQL Server the plan is `auto` and
only the connection knows whether Driver 18 opened it first time or Driver 17 picked it up after a
TLS refusal — with every attempt and every loser's error kept:

```json
"tool": {"tool": "auto", "chosen_by": "default",
         "actual": {"driver": "ODBC Driver 18 for SQL Server", "encryption": "optional",
                    "fell_back": false, "attempts": [...]}}
```

That report earned its keep immediately. A version-based driver re-ordering — Driver 17 /
`Encrypt=no` first for 2008 R2 and older — was written, measured against all four 10.50 instances
here, and **reverted**: every one completes on Driver 18 with `Encrypt=optional` at the first
attempt, so the rule would have saved no round trip and downgraded four production connections
from encrypted-when-offered to plaintext. `sqlserver_driver_candidates` carries the note next to
the order itself.

**`engine.observed_version`** is what the *server* said, read off the open connection at no round
trip, and `engine.version_drift` appears when it disagrees with the config. Until this existed,
`major_version` was a number somebody typed that nothing ever checked.

### A request that reads nothing at all

`target` names a server and the inventory answers — the right default for a runbook or a scheduled
task. A **`connection` block** replaces it and then no inventory file is opened
([`lib/connection_spec.py`](./14_lib.md)):

```bash
python -m db_ops.common.cli run-sql '{"connection": {"db_type": "sqlserver", "host": "192.0.2.5",
  "username": "monitor", "password_ref": "MSSQL_…", "major_version": 16, "label": "lab-mssql"},
  "sql": "SELECT 1"}'
```

`run-cmd` has the same door and always half-had it: an inline `access` block skipped the inventory
but still needed `users.json` for the credential. A block carrying its own `username` plus
`password`/`password_ref` is now answered from itself, and the credentials file is not even loaded.
A named `credential_name` still wins when both are present — naming an entry is asking for *that*
entry.

**`runtime`: `host` (default) | `docker` | `k8s`.** `run-cmd` can now put the command *inside* a
container, which only `backup-database` could do before. It wraps only when the **request** states
the runtime: a `container_name` on the instance is enough to describe the target and is deliberately
not enough to redirect a command, or every existing `run-cmd` against a containerised instance would
silently move off the host. A **Windows** host composes the same `docker exec` / `kubectl exec` under
PowerShell quoting instead of `shlex`, with `container_shell` (default `sh -lc`) naming the shell
inside — that is a property of the image, not of the host, so a Linux container on a Windows Docker
host still wants `sh`. Implemented and not verified live: no Windows container host was available to test it against,
so the guarantee is the command text.

```bash
run-cmd '{"target": "ACME-192-0-2-249-PGLAB-5433", "runtime": "docker",
          "command": "psql --version", "confirm": true, "assume_yes": true}'
#   -> psql (PostgreSQL) 18.4          … stated runtime: inside the container
# without "runtime": bash: psql: command not found   … the host, unchanged
```

### Which PowerShell the host has

`host-facts` picks its script from the OS version, not from hope. `Get-CimInstance` and
`ConvertTo-Json` arrived with PowerShell 3.0 / NT 6.2; below that the modern script does not return
less, it fails at cmdlet lookup with a message that reads like a permissions problem. The `wmi`
variant (`_WINDOWS_FACTS_PS2`) uses `Get-WmiObject` and `Win32_Service`, and emits **line records
rather than JSON** for the reason already written above the Linux script: a hand-rolled JSON
printer breaks on the first value containing a quote, and PowerShell 2.0 has no `ConvertTo-Json`.
One parser reads both, so the two shapes cannot drift.

Implemented and **not verified against a live host of that vintage** — nothing in the estate is
both that old and reachable. The guarantee is the script text and the parser, pinned by tests.

`host-facts` also gained a **`host.os_matches_inventory`** gate. The `os` caption is not
decoration: it decides the dialect above and the "can this host be managed at all" verdict below.
It is compared on the NT version rather than the caption text, because `Windows NT 6.2 (Build
9200)` and `Microsoft Windows Server 2012 Standard 6.2.9200` are one machine and a textual diff
would cry wolf on every host. It found one on the first run: `ACME-192-0-2-245` was recorded as
NT 6.2 and answers **Windows Server 2016 10.0.14393**.

### `probe-host` — what a machine listens on, and what that rules out

```bash
python -m db_ops.common.cli probe-host '{"target": "ACME-192-0-2-236"}'
# interactive_only - answers on RDP 3389 and no management port, and none is possible:
# Windows Server 2003 ships no WinRM and cannot run the OpenSSH server.
```

Written because three throwaway socket loops had answered the same question three ways — the case
The project's rule names it: a task needing a scratch script belongs in a `common` CLI command.
The verdict is the point, not the port list: `manageable` / `interactive_only` / `service_only` /
`unreachable`, and with a known OS the detail says whether a missing WinRM is even fixable. Each
port reports `open` / `refused` / `timeout` separately, because on a live host a refusal means the
service is off and a timeout means a filter — the distinction that told `.235`/`.236` apart from a
firewalled box.

### Which login a target runs as (credentials)

`data_sources.find_database_credential()` is the **only** answer to "which login does this
target use", shared by metrics, sql_tasks, the Telegram commands and `sql_run`. Before it, each
app answered differently, and they disagreed on the case that matters — a target that names no
credential. Metrics picked whichever entry carried role `DBA`/`SYSDBA`; other paths took the
first entry in file order. Either way a config omission still connected, as a login decided by
role or by file order rather than by anyone.

The rule now: **`credential_name` is required.** Unnamed or unknown raises `CredentialNotFound`,
listing the names that server does have. Matching narrows only on the keys the caller supplies —
`server_id` always, then `db_type`/`service_name`/`instance_name` when given (case-insensitive;
an empty value on either side is not a constraint, because inventories fill these unevenly).

Callers keep their own failure shape: metrics leaves `credential=None` so the collector reports
that one target instead of aborting the run; sql_tasks returns `None` and fails the target; the
Telegram command and `sql_run` raise.

Verify the config satisfies this before a deploy:

```bash
python -m db_ops.cli check-credentials          # exit 1 lists every target with no login
```

### `datetimeoffset` and other ODBC types

pyodbc cannot decode `SQL_SS_TIMESTAMPOFFSET` (type **-155**) on its own: one
`datetimeoffset` column — every Query Store view has several — used to fail the whole query
with `ODBC SQL type -155 is not yet supported. column-index=N type=-155 (HY106)`.
`sql_execution.connect_sqlserver` now registers an output converter for it on every
connection it opens, so no caller has to know the type code. A driver without
`add_output_converter` (pymssql) is left untouched.

---

## Describing this installation (`self_status`)

`self-status` is the process reporting on itself and the machine under it. It is deliberately the
odd one out among the status answers, and the distinction is the whole reason it exists:

| Command | Describes | Reaches |
| --- | --- | --- |
| `db.cli ops-status` | whether the **apps** ran on schedule | the store |
| `common.cli host-facts` | a **monitored host** | that host, over its `cmd_access` |
| `control.cli worker-status` | the **worker**, from the master | SSH |
| `common.cli self-status` | **this installation and this machine** | nothing |

Reaching nothing is the point: it still answers when the store is unreachable, which is one of the
times somebody most wants to know what version they are talking to.

```bash
python -m db_ops.common.cli self-status '{}'              # JSON envelope
python -m db_ops.common.cli self-status '{"format":"txt"}' # the chat listing
```

Three things it takes care to get right rather than merely report:

- **The product, before the version.** The published `dbabrain` wheel and a private `db_ops` build
  are the same toolkit under two distribution names and two numbering schemes (`0.5.0` against
  `2.87.01`). A version with no product beside it cannot be decoded, and on 2026-09-03 both were
  running the same estate within an hour. A tree that was never pip-installed says that instead of
  claiming a name.
- **Memory as *this process* experiences it.** Inside a container `/proc/meminfo` is the host's
  memory, not the cgroup limit; the cgroup is read first, an "unlimited" cgroup sentinel falls
  through instead of reporting 8 EiB, and every figure carries the source it came from.
- **Docker or the OS, and which OS.** `platform.platform()` answers "Linux" to both Ubuntu and RHEL,
  which does not help anyone deciding whether a package name applies.

No third-party dependency does any of this — the package has two, and neither is `psutil`. What the
standard library cannot answer on a platform is reported as unavailable rather than guessed.

## API index — what already exists here

**Read this before writing a helper in an app.** If a function below covers what you need,
use it; an app-private reimplementation of anything in this table is a defect, not a style
preference (see *Design rules*). Names are the public API of `db_ops.common.<module>`.

| Module | Public API |
| --- | --- |
| `config_admin` | `add_sql_task()`, `set_metric_toggle()`, `set_metric_severity_map()`, `known_metric_codes()`, `next_sql_id()`, `next_target_no()`, `normalize_time_window()`, `slugify()`; `ConfigAdminError`; `MANUAL_SCHEDULE`. Its CLI face is `common.cli add-sql` / `metric-toggle`, and both answer in the response envelope. `normalize_output()` and `OUTPUT_FORMATS` moved to `lib.task_output`, `resolve_target_from_server_id()` to `data_sources.resolve_sql_target_fields()` — 2026-08-15, so an app needs neither of them from here |
| `data_sources` | `load_credentials()`, `load_all_credentials()`, `load_remote_credentials()`, `load_db_instances()`, `load_inventory()`, `load_secret_text()`, `group_credentials_by_type()`, `find_database_credential()`, `resolve_sql_target_fields()`; `CredentialNotFound`, `TargetResolveError`; paths `users_path()`, `db_instances_path()`, `secret_text_path()` |
| `event_policy` | `normalize_error_type()`, `normalize_error_signature()`, `report_event_code()` |
| `confirm` | `require_confirmation()`, `banner()`, `is_interactive()`, `open_terminal()`, `read_answer()`; `CONFIRM_WORD` |
| `evidence` | `GateReport` (`add()`, `note()`, `say()`, `blockers()`, `passed()`, `status()`, `counts()`, `to_dict()`, `render()`, `write()`), `Gate`, `new_run_id()`, `symbol()`; `OK`, `WARN`, `FAIL`, `SKIP`, `DEFAULT_EVIDENCE_ROOT` |
| `host_ops` | `host_facts()`, `service_control()`, `restart_host()`, `resolve_host()`, `open_host_session()`, `read_facts()`, `service_states()`, `wait_for_services()`, `wait_for_port()`, `is_service_up()`, `check_maintenance_window()`, `load_maintenance_policy()`, `parse_json_output()`; `HostTarget`, `HostOpsError`; `DEFAULT_POLICY`. The `cmd_access` readers — `resolve_cmd_access()`, `resolve_cmd_credential()`, `resolve_platform()`, `infer_platform_from_os()`, `SUPPORTED_PLATFORMS`, `SUPPORTED_CMD_ACCESS_METHODS` — are `lib.cmd_access`, re-exported here |
| `sqlserver_patch` | `precheck()`, `apply_cu()`, `verify_build()`, `patch_arguments()`, `patch_exit_verdict()`, `sqlserver_service_names()`, `sqlserver_registry_key()`, `setup_log_root()`, `version_tuple()`; `SqlServerPatchError`; `EXIT_SUCCESS`, `EXIT_SUCCESS_RESTART_REQUIRED` |
| `result_format` | `render_result()`, `write_result()`, `normalize_format()`; `ResultFormatError`; `RESULT_FORMATS`, `NULL_TEXT` |
| `file_transfer` | `fetch_file()`, `send_file()`, `pack_files()`; `FileTransferError`; `STATUS_COPIED`, `STATUS_REPLACED`, `STATUS_SKIPPED_EXISTS`, `PARTIAL_SUFFIX` |
| `metric_targets_config` | `resolve_config_metric_target()`, `load_config_metric_targets()` |
| `listing` | `active_only()`, `hidden_note()`, `is_active()` |
| `notify` | `parse_notify_config()`, `parse_notify_rule()`, `notify_rule_dict()`; `NotifyConfig`, `NotifyRule`, `NotifyConfigError`; `NOTIFY_CHAT_LEVELS`, `NOTIFY_RULE_NAMES` |
| `notify_route` | `notify_chat_id()`, `alert_chat_id()`, `telegram_route()`, `telegram_groups()`, `clear_cache()` |
| `oracle_bridge` | `run_query()`, `run_bridge_query()`, `normalize_sql_access()`, `is_legacy()`, `build_connect_string()`, `resolve_connect()`, `resolve_secret()`, `connect_mode()`, `schema_prelude()`, `prepare_sql()`, `subprocess_argv()`, `encrypt_token()`; `LegacyOracleError` |
| `policy_engine` | `apply_report_policy()`, `render_policy_event()`, `normalize_status()`, `row_status()`, `row_context()`, `extract_fields()`; `PolicyEvent`, `PolicyResult` |
| `remote_exec` | `open_session()`, `run_command()`, `run_script()`, `shell_prelude()`, `resolve_secret_value()`, `build_invoke_command_script()`, `build_invoke_command_argv()`, `encode_powershell_command()`, `quote_powershell()`; `RemoteAccess`, `RemoteResult`, `RemoteSession`/`SshSession`/`WinrmSession`/`LocalSession`; `RemoteExecError` + `RemoteAuthError`/`RemoteConnectError`/`RemoteTimeoutError` |
| `secret_text` | `encrypt_secret_text()`, `decrypt_secret_text()`, `is_encrypted_blob()`, `resolve_key()`, `resolve_cli_key()`, `decode_key_base64()`, `set_key_env()`, `add_key_argument()`, `load_secret_text()`, `load_secret_text_file()`, `set_secret_text()`, `encrypt_secret_text_file()` |
| `shell` | `powershell_executable()`, `is_powershell_executable()` |
| `sql_execution` | `connect_sqlserver()`, `build_sqlserver_conn_str()`, `choose_sqlserver_driver()`, `sqlserver_driver_candidates()`, `register_output_converters()`, `decode_timestampoffset()`, `execute_cursor_batches()`, `split_sql_batches()`, `make_json_safe()`, `odbc_value()`, `resolve_password()`, `load_credentials_file()`, `load_remote_credentials_file()`, `load_database_inventory()`, `load_json_file()` |
| `sql_run` | `run_sql()`, `json_safe_result()`, `resolve_target()` (alias `resolve_sqlserver_target()`), `connect_target()`, `execute_capture_first()`, `split_batches_for()`; `SqlRunRequest`, `SqlRunError` |
| `db_connect` | `connect_engine()`, `normalize_db_type()`, `default_database()`; `DbConnectError`; `SUPPORTED_DB_TYPES` |
| `ssh` | `open_ssh_client()`. Re-exported from `data_sources.ssh_auth`: `resolve_ssh_key()`, `resolve_ssh_password()`, `ssh_keys_dir()`; from `lib.ssh_errors`: `SshError` + `SshAuthError`/`SshConnectError`/`SshTimeoutError` |
| `target_flags` | `is_target_enabled()`, `is_metrics_enabled()`, `is_reports_enabled()`, `is_alerts_enabled()` |
| `target_resolve` | `parse_target_spec()`, `resolve_target_instance()`, `list_target_instances()`, `format_target_list()`, `normalize_db_type()`; `TargetResolveError` |
| `time_window` | `parse_time_window_config()`, `is_time_window_open()`, `time_window_closed_reason()`, `repeat_due()`, `job_due()`; `TimeWindow`, `ParsedTimeWindow`; `RUN_ONCE`, `MANUAL_ONLY`, `ERROR_STATUSES` |
| `xlsx_export` | `write_result_set_xlsx()`; `MAX_CELL_TEXT` |

The shared **JSON config objects** these parsers read (`time_window`, `notify`, `cmd_access`,
`sql_access`) are specified in
[Shared config objects](#shared-config-objects-the-json-contracts) below.

---

## Design rules

- **A shared function wins over a private one.** If `common` already answers a question,
  an app must call it — writing an app-private version of something in the *API index*
  above is a defect. Two copies of a rule diverge the moment one of them is fixed, and the
  bug then lives in whichever app nobody looked at. When designing a new app, read the API
  index first and design against it; when you need something `common` almost has, extend
  the `common` function rather than forking it locally. Concretely, this rule has already
  been applied to: SSH connects (`ssh.open_ssh_client`), remote command execution
  (`remote_exec`), notify routing (`notify` + `notify_route`), scheduling
  (`time_window`), data loading (`data_sources`) and crypto (`secret_text`).
- **No cross-app imports.** Apps depend on `common`, never on each other. New shared
  logic goes here, not into one app that another app then imports.
  - The **add-SQL config-write engine** lives here for exactly this reason:
    `db_ops/common/config_admin.py` performs the atomic JSON/file writes that register a new SQL
    task (`.sql` file + `sql_commands.json` + `sql_targets.json`). It is shared logic — reached
    by an operator at a shell **and** by the Telegram `add_sql_task` action, both through
    `python -m db_ops.common.cli add-sql` — so it sits in `common`, not in `sql_tasks` where
    Telegram would have to import it. It connects to no database and imports no execution path.
    Two things changed on 2026-08-15 and both are the rule tightening rather than moving: the
    `db_ops.sql_tasks.config_admin` shim that offered a second name for the command was
    **deleted**, and Telegram stopped importing the engine — an app calls the CLI. That last step
    required `add-sql` and `metric-toggle` to start answering in the **response envelope**; they
    had printed a bare dict on success and `ERROR:` on stderr with exit 2 on failure, which a
    caller holding only stdout cannot read.
- **Config-driven, no hardcoded secrets.** Every credential/secret is loaded through
  `data_sources` / `secret_text` / `sql_execution.resolve_password` from the data folder
  and the encrypted secret file — never inlined. Defaults use the
  `values.get(...) or "<default>"` pattern.
- **Cross-platform by construction.** Windows-only assumptions (PowerShell path) are
  resolved at runtime via `shell.py`; the same code runs on the Windows host and in the
  Linux container.
- **A one-off you had to write is a command you were missing.** If answering a question or
  performing an operation needed a script — auditing every secret, rotating a password, probing
  what a host actually listens on — that work belongs in a `common` module with a
  `db_ops.common.cli` face, in the same pass. A throwaway script answers the question once and
  takes its target resolution, its edge cases and its ordering rules to the grave; the next person
  writes it again, slightly differently, and the two disagree about where a secret lives. Both
  `password_rotation` and `secret_check` exist because they were written as scratch scripts first,
  and the scripts got the port, the protocol and the parameter binding wrong in ways the shared
  version now cannot.
- **A `common` CLI command takes a JSON object.** Not flags, not positional arguments: one object,
  passed inline, as `@path/to/request.json`, or on stdin (`-`). It is the shape config already has,
  so a `data/*.json` block, a Telegram action and a shell caller hand the same payload through
  untranslated, and adding a field never breaks an existing caller. All 22 commands follow it, and
  `tests/test_common_cli_json_contract.py` holds them to it — a new command that invents its own
  flag vocabulary fails there rather than shipping. Parsing is shared: `_read_json_request` in
  `db_ops/common/cli.py` is the only reader, so every command accepts the same three input forms
  and reports a bad payload identically. Six commands (`add-sql`, `metric-toggle`, `list-targets`,
  `check-credentials`) predate the rule and still accept their
  original flag/word form as compatibility; `_optional_json_request` tells the two apart.
- **One convention per concern.** `time_window` is the only scheduling authority;
  `data_sources` is the only data-folder loader; `secret_text` is the only crypto path;
  `remote_exec` (over `ssh`) is the only way to reach another machine; `confirm` is the only
  way to ask a human before something irreversible. Duplicating any of
  these in an app is a defect — a second paramiko `connect()` or a second hand-built
  `Invoke-Command` string in an app is exactly the drift this layer exists to prevent.

## Finding this estate's names in files that ship (`identifier_scan`)

`check-identifiers` answers one question: **which of the identifiers this estate actually uses
appear in the files that leave this repository.** It is the check behind go-live gate `G-02`, and
it is the only command in `common` that opens nothing — no host, no database, no secret. It reads
configuration to learn what to look for, then reads files. A gate that can *do* something is a gate
nobody lets run unattended.

```bash
python -m db_ops.common.cli check-identifiers '{}'                       # the shipping surface
python -m db_ops.common.cli check-identifiers '{"paths": ["db_ops/sre"]}'
python -m db_ops.common.cli check-identifiers '{"extra_terms": ["CLOUD"]}'
```

Finding something is the **answer**, not a failure: the command exits 0 and the count is in
`data.hits`. It exits non-zero only when it could not run — the same distinction `check-secret`
makes between "the estate is like this" and "db_ops could not look".

### It reads your inventory instead of maintaining a map

The terms are not a pattern list and not a table in `CONTRIBUTING.md`. They come from
`db_instances.json`, which already names every address, `server_id`, service, database and
credential you run, plus the Telegram files, which name the people. Two consequences, and both are
the reason it is built this way:

- **A hit cannot be a false positive by construction.** Every term is a value this operator uses.
- **The answer improves on its own.** A machine added to the inventory is searched for from that
  moment. A hand-maintained map is a second copy of the estate, and the two disagree the first time
  somebody adds a server without updating the map — which is precisely how the 2026-08-21 scrub was
  recorded as complete while 80 identifiers remained.

### One identifier, three spellings

This project writes an address three ways, and each hid from a different earlier grep:

| Written | Where |
| --- | --- |
| `192.0.2.248` | configuration |
| `192-0-2-248` | inside a `server_id` — `ACME-192-0-2-248-MSSQL-1433` |
| `192_0_2_248` | inside a secret ref — and `\b` in a grep does **not** match here, because `_` is a word character |

All three are one machine. A scan that knows only the dotted form reports the tree clean while two
thirds of it are still there. That is not a hypothetical: it is how `ORACLE_..._SYS` survived the
first scrub, and how a `server_id` kept its address after its organisation half was replaced.

### Three tiers, because the inventory contains ordinary English

This estate genuinely has databases called `Export`, `Inventory`, `Damage` and `Maintenance`. The
first run of this module matched terms as substrings and reported **1,550 hits** — every occurrence
of the word "inventory" in `inventory_report.py` was a finding. The same tree reports **80** once
the tiers and word boundaries are applied.

| Tier | What it is | How it is matched | Counted in `hits`? |
| --- | --- | --- | :---: |
| `certain` | an address, or a token carrying both a digit and a separator | substring, case-insensitive — it must be found *inside* a `server_id` | yes |
| `likely` | a distinctive token: all-caps, or carrying an underscore | whole word, case-sensitive | yes |
| `review` | an ordinary word that is also a database name | whole word, case-sensitive | **no** |

`review` is reported in `data.review` and deliberately kept out of the number a gate acts on.
Rewriting those mechanically is how a scrub renames a Python function called `export`.

### What is never reported, and why each one is there

Two lists, and every entry carries its reason — an unexplained exclusion is how a scan quietly
stops covering something, and the next reader cannot tell a decision from an annoyance.

- **`ALWAYS_ALLOWED`** — matched against the surrounding text, not the token, because what makes
  `172.17.0.0` allowed is the `/16` after it. It holds the RFC 5737 documentation ranges in all
  three spellings, Docker's default address pool, and the Oracle version numbers `11.2.0.2` and
  `8.1.7.0` that a naive IPv4 regex corrupts.
- **`GENERIC_TERMS`** — configured values that identify nothing: environment labels, engine names,
  system databases, and **vendor defaults**. `MSSQLSERVER` is how Windows registers a default
  instance and `FREEPDB1` is Oracle Free's default PDB; they read as estate names, are identical in
  every install, and scrubbing them would break the code that depends on the vendor's spelling.

The RFC 5737 exclusion is worth its own line: without it the scan **flags its own output**. The
first scrub script did exactly that, and a checker that reports success as failure is one the next
person switches off.

### The failure it must never have

`load_db_instances` is best-effort — a missing file yields `[]` rather than raising, which is right
for a collector that must not stop the estate's monitoring over one bad path, and exactly wrong
here. **Zero terms means every tree scans clean.** So `collect_identifiers` refuses an empty
inventory and names the file it expected, because the one failure this checker cannot afford is the
one it would report as success.

## Refreshing an example from your own file (`example_lift`)

```bash
python -m db_ops.common.cli lift-example '{"source": "data/metric_definitions.json"}'
python -m db_ops.common.cli lift-example '{"source": "data/sla_policies.json", "write": false}'
```

Every file under `data/` has a `*.example.json` beside it, and **the examples ship** — they are
what a stranger copies to get a working tool root and what the documentation points at. So the two
drift in one direction: your file gains records as the estate grows, the example does not, and
nobody notices until the shipped suite runs against the shipped examples and finds the metric
catalogue describing ten of the ninety collectors the package carries.

`dest` defaults to the `*.example.json` beside the source. `write: false` reports what would happen
and writes nothing.

### It refuses; it does not scrub

The command copies, then runs `check-identifiers` over what it *would* write. If anything real
would cross it writes nothing and names the terms:

```json
{"identifier_hits": 5, "terms": ["198.51.100.20", "SALESDB", "SALESCLUSTER"], "written": false}
```

(The terms above are the documentation placeholders. Printing the *real* ones here is how this
page failed its own scan the first time it was written — an explanation of a pattern must not
contain the pattern, and that is the second time this session that rule was learned the hard way.)

That is the design and not a limitation. A tool that quietly rewrote what it found would be a
second scrubber with its own opinions about which spelling of a hostname is which, and this project
has one answer to that question. The fix belongs in the source: on the first real run all five
findings were inside `note` fields — prose explaining *why* an interval or a timeout is what it is,
which happened to name the machine it was learned on. The reasoning survives the machine's removal
and the note gets better, so the source is where it was fixed.

### And it refuses a source that would not load

Every path a record names — `path`, `file`, `script` — is resolved before anything is written, in
**both** vocabularies these files use: `assets/backup/...` through `resolve_tool_path`, and
`sqlserver/001_....sql` relative to the shipped collectors. A lifted catalogue naming a variant that
is not there refuses to load, and it does so on somebody else's machine, at collection time, hours
after the mistake. An earlier hand-written lift shipped exactly that — four invented filenames in a
catalogue nothing had ever loaded.

### Why this is a command and not a script

It had been done by hand three times, and each hand-lift got something different wrong: one
invented those filenames, another carried a server id inside a Telegram prompt string that nobody
would think to read. The rule fires precisely here — the moment a task needs a throwaway
script it belongs in `common` with a CLI — and the reason is visible in the outcome: the fourth
lift moved 90 records and checked 183 file references in one command, and refused twice before it
was allowed to write.

## Checking a secret still works (`secret_check`)

The read-only half of the pair. `rotate-password` changes a password; this proves one, and both
resolve a target the same way so an audit and a rotation cannot disagree about where a secret lives.

### "Cannot check this" must be a fact about the estate

An audit that reports a secret as untestable invites someone to delete it, so the burden is on this
module to look everywhere and to ask the right question. Resolution order:

1. `db_instances.json` — `default_credential_name` (a database login) or `cmd_access` (an OS login,
   which also states `method`, so the protocol is known rather than guessed);
2. `docker_db_connections.json` — carries the **published, non-default port** a container listens on
   (5442, 1522, 5435). Skipping this source is how a working PostgreSQL secret gets probed on 5432,
   answers nothing, and is written up as unusable;
3. `restore_config.json` — `password_env` / `sql_password_env` on a backup or restore job;
4. `users.json` `remote_credentials` — an OS account no instance references;
5. the standard key name, which carries the IP **and an optional port**
   (`ORACLE_203_0_113_121_1522_SYS`). A name is a label, so it is last.

When the method is not stated the protocol is **probed** — SSH 22, then WinRM 5985/5986 — because
the estate is mixed and asking an Ubuntu host over WinRM reports it unreachable when that is only
the wrong question.

### Four statuses that used to be one word

| Status | Means | Follow-up |
| --- | --- | --- |
| `UNREACHABLE` | nothing answers on any management port | decommission the entry, or fix the network |
| `NO_MANAGEMENT_PORT` | host answers on RDP 3389 but neither SSH nor WinRM listens | enable WinRM, or accept it is administered interactively |
| `AUTH_FAILED` | the service rejected the credential | rotate it — this is the one that is actually broken |
| `NOT_A_LOGIN` | key material or a service token; there is no session to open | verify through the system that uses it |

`NO_TARGET` is separate again: a config names the ref but carries no host — or, for Oracle, no
`service_name` is declared anywhere for that IP, which is reported in those words because a raw
`DPY-6005` sends the reader to the network instead of to `users.json`.

### Using it

```bash
python -m db_ops.common.cli check-secret '{}' --key-base64 "<b64>"          # the whole store
python -m db_ops.common.cli check-secret '{"match": "DBA_USER"}'            # one family
python -m db_ops.common.cli check-secret '{"refs": ["MSSQL_192_0_2_248_DBA_USER"]}'
```

One attempt per secret, no retries, so a wrong stored password never walks an account into a
lockout policy.

---

## Rotating a login password (`password_rotation`)

Changing a database password touches two places that must agree: the server, and
`data/encrypted_secret_text.json`. Doing them separately is how a rotation goes wrong —
and it goes wrong quietly, surfacing hours later as a metric that stopped collecting.
This module does both, and records nothing it has not proven.

### The order, and why it is not negotiable

1. **Connect with the current password.** A target whose current password already fails is
   `SKIPPED`, never guessed at: `ALTER LOGIN ... OLD_PASSWORD` needs it, and a failure here means
   the store was already wrong — a fact worth reporting, not overwriting.
2. **Issue the engine's change statement.** These are DDL and every supported engine rejects a
   parameter marker in this position; SQL Server answers `Incorrect syntax near '@P1'`. So the
   statement is built with literals, and the quoting helpers double any embedded quote.
3. **Re-authenticate on a brand-new connection.** The session that issued the change stays
   authenticated afterwards, so re-running a query on it proves nothing about the new password.
4. **Only then** hand the value back for storage.

If step 3 fails, the change is rolled back inline using the new password, which the process still
holds. That is the single window in which a host could be left with a password nothing recorded,
which is why the rollback is not optional and not deferred.

### The JSON input

```json
{"match": "DBA_USER_DBA", "dry_run": true}
```

| Field | Meaning |
| --- | --- |
| `refs` | list of `password_ref` names to rotate |
| `match` | regex matched against `password_ref` **names** — never values, so choosing a set never decrypts one |
| `dry_run` | connect and report `READY`, change nothing. Do this first. |
| `password_length` | generated length (default 28, minimum 12) |
| `passwords` | `{password_ref: value}` when an external policy dictates the value |
| `host_overrides` | `{password_ref: ip}` to pin which node of a clustered instance to use |
| `allow_name_host` | for a ref no `db_instance` uses, take the host from the standard key name (`MSSQL_<ip>_<login>`). **Off by default** — a key name is a label, not configuration, so deriving a target from it is an explicit operator choice. |
| `timeout_seconds` | connect timeout (default 10) |

Statuses: `SUCCESS` · `READY` (dry run) · `SKIPPED` (not attempted, with the reason) ·
`FAILED` (attempted, nothing kept). Passwords never appear in the output, the logs, or the results.

### Why every target gets its own password

Reusing one new value across an estate rebuilds the weakness a rotation usually exists to remove:
with a shared password a leak anywhere is a leak everywhere, and the blast radius of the next
incident is the whole estate rather than one host. The generator draws from an alphabet with no
quote, semicolon, backslash or brace — the characters that turn a password into a truncated
connection string or a syntax error weeks later, on one unlucky draw.

### Both stores, or the next deploy undoes it

`persist_rotated` writes the encrypted blob **and** the gitignored plaintext source. The deploy
regenerates the blob from the plaintext, so updating only the encrypted file is silently reverted
the next time anyone deploys.

### Using it

```bash
# always dry-run first: proves reachability and that the stored password still works
python -m db_ops.common.cli rotate-password '{"match": "DBA_USER_DBA", "dry_run": true}' --key-base64 "<b64>"

# then rotate
python -m db_ops.common.cli rotate-password '{"match": "DBA_USER_DBA"}' --key-base64 "<b64>"

# one ref that no db_instance references, host taken from its standard name
python -m db_ops.common.cli rotate-password '{"refs": ["MSSQL_192_0_2_8_DBA_USER_DBA"], "allow_name_host": true}'
```

After rotating, **deploy with `--no-merge-worker`**: the normal deploy merges the worker's secrets
by union, which would restore the pre-rotation values.

---

## Copying a schema between instances (`schema_copy`)

Reproduce SQL Server schema `X` from one instance on another. Raised from a real deployment
where the whole thing was done by hand
outside db_ops, and every requirement below is something that cost time on that run.

```bash
python -m db_ops.common.cli copy-schema '{
  "source": {"target": "SRC-SERVER-ID", "database": "APPDB_TEST", "schema": "schedule"},
  "dest":   {"target": "DST-SERVER-ID", "database": "APPDB_PROD", "schema": "schedule"},
  "assert_dest_instance": "APPHOST\INSTANCE",
  "exclude_tables": ["dataLock", "*Staging"],
  "with_data": ["sql", "sql_version", "CalendarDay"],
  "exclude_modules": ["usp_ORD38_*"],
  "plan": true
}'
```

**`plan` is the default posture, not a flag you remember to add.** It reports counts, the ordered
statements and what it will *not* carry, and writes nothing. `"plan": false` applies.

**Nine phases, in dependency order**, each idempotent:

| # | Phase | Why here |
| :-: | --- | --- |
| 1 | `partitions` | A partition scheme has to exist before a table can sit on it |
| 2 | `change_tracking_database` | `ALTER DATABASE ... SET CHANGE_TRACKING` precedes any per-table setting |
| 3 | `tables` | `IF OBJECT_ID(...) IS NULL`, so a resumed run skips what exists |
| 4 | `change_tracking_tables` | Per table, and **before modules**: a procedure referencing `CHANGETABLE` on a table without tracking fails with **Msg 22105**, mid-deploy |
| 5 | `indexes` | |
| 6 | `checks` | |
| 7 | `data` | `IF NOT EXISTS (SELECT 1 FROM <t>)`, per table in `with_data` |
| 8 | `modules` | Re-tried in passes: views over views and functions used by functions resolve at create time, so one ordered pass is not enough. Each pass must reduce the failure count or the loop stops |
| 9 | `foreign_keys` | **After data**, so load order cannot violate them |

**A run that dies resumes by being run again.** Every phase is written to be re-runnable —
`IF OBJECT_ID(...) IS NULL`, `IF NOT EXISTS`, `CREATE OR ALTER` — because the deployment this came
from crashed in phase 4 and needed no repair, and that is the property worth keeping.

**Two guards that exist because their absence was survived by luck:**

- **`sp_getapplock` around the whole operation.** The "destination already has rows" check is
  read-then-write and does not stop a second applier. Two ran against one target on 2026-08-22;
  nothing was corrupted only because every catalogue table had a primary key.
- **`assert_dest_instance`.** `DB_NAME()` is not an address — the same database name exists on
  several instances. The request may demand a specific `SERVERPROPERTY('ServerName')` and abort
  otherwise.

**Data moves through the client**, batched `executemany` with `IDENTITY_INSERT` per table. The
obvious implementation, `INSERT ... SELECT FROM [OtherDb].[schema].[table]`, only works when both
databases share an instance.

**What it will not carry, it says.** `report_unsupported` (on by default) lists the features
catalogue-based scripting silently drops — partitioning, change tracking, filegroups, compression,
temporal tables, extended properties, permissions. Two of those had already cost a deployment each
before anyone noticed they were missing, which is why naming them is worth as much as copying the
rest.

---

## CLI

The shared layer has its own entrypoint, like every app: `python -m db_ops.common.cli`.
It is a thin facade — logic stays in the common modules it fronts.

**The store travels in the request, like every other value.** `common` performs work and reads
nothing; that already held for a target database (`run-sql` carries host, login and password) but
not for the *runtime store*, where a caller could only say "config.json" and let this side go and
read it. `db_ops/db/declaration.py` closes that:

```bash
python -m db_ops.db.cli queue-telegram-message - <<'EOF'
{"store": {"backend": "postgresql",
           "postgresql": {"host": "...", "port": 5433, "database": "db_ops", "schema": "db_ops",
                          "username": "postgres", "password": "<resolved by the caller>"}},
 "chat_id": "-100...", "text": "...", "level": "logging", "phase": "START",
 "source_type": "backup_restore_events", "source_id": "backup:CLOUD_PG_DB"}
EOF
```

- `password` is a **value, not a `password_ref`**: resolving a ref is a lookup, and lookups are the
  caller's half. `declaration.describe()` does it on the app side; `declaration.redact()` is what
  goes in a log line.
- **On stdin, never as an argv word** — the payload carries that password, and argv is readable by
  any process on the box.
- Omitting `store` still falls back to reading `config.json`, which is right for a shell caller
  that has no store to state and wrong for an app that already knows.

This is what lets a caller name a store that is **not this node's own** — a test writing to a temp
SQLite file, or a run against another deployment. An in-process call could hand over a live store
object; a subprocess cannot, and that gap is what blocked moving message queueing onto the CLI
until the store could describe itself.

```bash
python -m db_ops.common.cli add-sql '<json>'        # config_admin: register + enable a SQL task
python -m db_ops.common.cli metric-toggle '<json>'  # config_admin: enable/disable metrics per server_id
python -m db_ops.common.cli metric-severity '<json>' # config_admin: remap a metric's statuses per server_id
python -m db_ops.common.cli trace-session '<json>'  # session_trace: who holds an open transaction
python -m db_ops.common.cli list-targets            # target_resolve: the /spbot_list_server_id listing
python -m db_ops.telegram.cli groups              # the whole level -> chat_id map
python -m db_ops.telegram.cli route <level>       # {enabled, alert, chat_id} for a notify level
python -m db_ops.common.cli run-sql '<json>'        # sql_run: run SQL on one database target
python -m db_ops.common.cli run-cmd '<json>'        # host_ops: one shell command on a configured host
python -m db_ops.common.cli probe-host '<json>'     # host_probe: what a host listens on, and what that allows
python -m db_ops.cli check-credentials       # data_sources: every target resolves a login
python -m db_ops.db.cli queue-telegram-message '<json>'  # telegram_queue: queue one outgoing message
python -m db_ops.db.cli restore-drill-status '<json>'    # restore_drill: was a restore actually proven
python -m db_ops.common.cli fetch-file '<json>'     # file_transfer: copy one file from a host to here
python -m db_ops.common.cli send-file '<json>'      # file_transfer: copy one file from here to a host
python -m db_ops.common.cli pack-files '<json>'     # file_transfer: pack files into one archive + sha256
python -m db_ops.common.cli relay-file '<json>'     # file_transfer: copy one file host->host, hash-verified
python -m db_ops.common.cli rotate-password '<json>'  # password_rotation: change a login's password
python -m db_ops.common.cli check-secret '<json>'     # secret_check: prove each secret still logs in
python -m db_ops.common.cli inventory-summary '<json>'  # inventory_render: merge overlay + render summary
python -m db_ops.common.cli host-facts '<json>'       # host_ops: one host's state (read-only)
python -m db_ops.common.cli host-service '<json>'     # host_ops: start/stop/restart services + wait
python -m db_ops.common.cli host-restart '<json>'     # host_ops: restart a host and prove it came back
python -m db_ops.common.cli sqlserver-precheck '<json>'      # sqlserver_patch: safe to patch? (read-only)
python -m db_ops.common.cli sqlserver-apply-cu '<json>'      # sqlserver_patch: apply a staged CU
python -m db_ops.common.cli sqlserver-verify-build '<json>'  # sqlserver_patch: did it reach the build
python -m db_ops.common.cli sqlserver-export-instance '<json>'  # sqlserver_instance: server metadata -> SQL
python -m db_ops.common.cli sqlserver-replay-instance '<json>'  # sqlserver_instance: apply a bundle to a target
python -m db_ops.common.cli sqlserver-verify-instance '<json>'  # sqlserver_instance: orphaned users + counts
python -m db_ops.common.cli backup-database '<json>'    # backup: run ONE backup, script + host + env
python -m db_ops.common.cli list-backup-files '<json>'  # backupfiles: an engine's backups as full/diff/log
python -m db_ops.common.cli prune-backup-files '<json>' # retention: which are obsolete (default 14 days)
python -m db_ops.common.cli delete-file '<json>'        # deletefiles: remove ONE file by full path
python -m db_ops.common.cli delete-files '<json>'       # deletefiles: remove each named file, one connection
```

Listing and deleting are one workflow — list, decide, delete the paths you were given:

```bash
python -m db_ops.common.cli list-backup-files '{"db_type": "oracle", "path": "/opt/oracle/backup/dbops",
  "kinds": ["log"], "before": "2026-08-01 00:00:00", "host": {...}}'
# or let prune decide, and delete in the same step:
python -m db_ops.common.cli prune-backup-files '{"db_type": "oracle", "path": "/opt/oracle/backup/dbops",
  "retention_days": 14, "delete": true, "dry_run": true, "host": {...}}'

# then, with the paths that came back:
python -m db_ops.common.cli delete-files '{"paths": ["/opt/oracle/backup/dbops/x.bkp"],
  "must_be_under": "/opt/oracle/backup", "dry_run": true, "host": {...}}'
```

The six host/patch commands share one handler and therefore one contract: a JSON object in
(inline, `@file`, or `-`), a gate report as JSON on stdout, live progress on stderr, and exit 0
unless a blocking gate failed.

## Timezone convention

Two different clocks are in play; do not mix them up:

- **`time_window` bounds (`from_*`/`to_*`) are evaluated in the node's local time**
  (`datetime.now().astimezone()`), not UTC. Both nodes are configured to **+07**: the
  master PC runs Windows timezone `SE Asia Standard Time`, and the worker container sets
  `TZ: Asia/Ho_Chi_Minh` in `docker-compose.yml` / `docker-compose.runtime.yml`. So
  `from_hour: 1` means 01:00 +07 on either node. If a node's OS/container timezone were
  ever changed, every window would shift with it.
- **Store timestamps are written in UTC (+00) on either backend**. Every `created_at` / run-time column in
  the runtime store is written as `YYYY-MM-DDTHH:MM:SSZ` — Python code goes through
  `utc_now_text()` (`datetime.now(timezone.utc)`) in `db_ops/db/store.py`, and the
  schema defaults use `strftime('%Y-%m-%dT%H:%M:%SZ','now')`, which is also UTC. Add +07
  when reading `sql_runs` / `job_runs` rows manually.
- `repeat_interval` / due comparisons are done in UTC (stored row vs `datetime.now(timezone.utc)`),
  which is offset-safe; only the window open-check uses local time.

## Shared config objects (the JSON contracts)

Merged in from the former `13_common.md` on 2026-08-15. It was a separate numbered
doc for something that is not a component: these objects have no app of their own, their parsers
all live in `db_ops/common/`, and every one of them is listed in the API index above. A contract
documented away from the module that enforces it is the drift this file exists to prevent.

Some JSON objects appear in **several config files, for several apps**, and mean the same thing
everywhere: `time_window` schedules a unit of work whether it is a SQL task, a backup job or a
metric; `notify` says who gets told about it; the remote-access object says how to reach a
machine. This is the one place those shapes are specified.

> **Why here and not in `data/`.** `data/` holds *values* — the per-node config that is
> bind-mounted, partly git-ignored, and shipped to the worker. A spec is a *contract*, not
> data: it is reviewed with the code that enforces it, and the worker has no use for it at
> runtime. Keeping it out of `data/` also avoids the trap of a spec file sitting beside the
> files it describes and drifting from them silently.

**The parser is the authority, this document is its description.** Each object below names the
`db_ops.common` function that reads it. If the two ever disagree, the parser is right and this
file is the bug — every one of these objects is validated at load time, so a config that
contradicts the spec fails loudly rather than behaving oddly.

Adding a new shared object: put the shape and its parser in `db_ops/common/`, list the parser in
the API index above, and add a subsection here. An object used by exactly one app is not shared
and belongs in neither.

### `time_window` — when a unit of work runs

**Parser:** `db_ops.lib.time_window.parse_time_window_config(entry, context=...)` →
`ParsedTimeWindow`. **Used by:** the app daemon, sql_tasks, metrics, reports, backup_restore.

```jsonc
"time_window": {
  "from_year": null, "to_year": null,      // null on any bound = unbounded
  "from_month": null, "to_month": null,
  "from_day": 1,  "to_day": 31,
  "from_hour": 1, "to_hour": 5,
  "from_minute": null, "to_minute": null,
  "repeat_interval": 72000,                // seconds between runs; 0 = RUN_ONCE, -1 = MANUAL
  "retry_interval": 3600,                  // seconds to wait after a failure
  "timeout": 7200                          // seconds; 0 = never time out
}
```

- **`from_* > to_*` is a wrapping range on every dimension.** `from_hour: 22, to_hour: 6`
  means 22:00 through 06:00 the next morning; `from_day: 25, to_day: 5` means the 25th
  through the 5th of the next month.
- **Bounds are evaluated in the node's local time** (both nodes are +07); store timestamps
  are UTC on either backend. See the timezone section above.
- **`repeat_interval: 0` is RUN_ONCE**, not "run constantly": a successful run never
  repeats, a failed one retries after `retry_interval`. Note run-once **does** run — it is due
  while it has never run — so `0` cannot express "only on demand".
- **`repeat_interval: -1` is MANUAL**: the scheduler never starts it, not even the first time,
  and neither the retry-on-failure nor the stale-running path revives it. The entry stays
  `active` (so it is still listed) and runs only when something forces it — for SQL tasks that
  is `run-sql-id --force` / `/spbot_run_sql_task`. Its day/hour bounds are kept but never
  consulted; `timeout` still applies to the forced run. `-1` is the **only** accepted negative;
  any other still raises, so a typo cannot silently disable a task.
- **`timeout` does two things** — it is the grace after which a run still marked RUNNING is
  considered dead (so the next run may start), *and* in backup_restore the point at which
  that abandoned row is closed as TIMEOUT and reported. `0` disables both.
- A child timeout can never exceed the timeout of the app command that runs it — the daemon
  kills the parent process first. See [`docs/08_backup_restore_app.md`](./08_backup_restore_app.md).

Deprecated aliases still read: `interval_seconds` → `repeat_interval`, `timeout_seconds` /
`default_timeout` / `check_error_interval_seconds` → `timeout`.

### `notify` — who gets told, and where

**Parser:** `db_ops.lib.notify.parse_notify_config(entry, context=..., defaults=...,
inherit=...)` → `NotifyConfig`. **Resolution:**
`db_ops.lib.notify_route.notify_chat_id(level, notify)`. **Used by:** backup_restore
(entries and sub-jobs), sql_tasks (SQL targets).

```jsonc
"notify": {
  "logging_on_run": { "enabled": true,  "telegram_chat": "",      "chat_id": "" },
  "alert_on_error": { "enabled": true,  "telegram_chat": "error", "chat_id": "" }
}
```

Two rules, one per kind of event. Apps emit by **severity**, and the mapping is made once in
`NotifyConfig.rule_for_level()`: `warning`/`error`/`critical` → `alert_on_error`, everything
else → `logging_on_run`.

| Field | Meaning |
| --- | --- |
| `enabled` | Report this kind of event at all. `false` = stay silent. |
| `telegram_chat` | The notify level whose group receives it: `logging`, `warning`, `critical`, `error`, `test`, `private`. **`""` (the default) means "follow the event's own severity"** — so adding a `notify` object changes no destination until a rule is actually set. |
| `chat_id` | An explicit chat that wins over `telegram_chat`, for work that must reach one specific chat regardless of the level map. |

Rules that hold everywhere:

- **The node gates, the entry narrows.** Whether a level may alert at all is the node's
  answer: `config.telegram.enabled` plus the **level → chat map** (`data/telegram_groups.json`
  group `notify_level`s, overridden/extended by `level_chat_map` in
  `data/telegram_config.json`). **A level with a chat sends; a level with no chat does not** —
  having a chat *is* the permission, so muting a level means clearing its chat, and there is
  no second allow-list to keep in sync. (There was one, `alert_levels`; every level name had
  to be spelled in both places and a typo there muted a level with no error anywhere — which
  is exactly what happened to `private`.) A `notify` object can silence a level or redirect
  it — it can **never** switch on a level the node switched off, or a per-entry block would be
  a way to leak messages out of a deliberately muted node.
- **Levels are data.** A Telegram group carrying `notify_level: "sla"` defines the level
  `sla`; no code change is needed to route to a new group. `python -m db_ops.telegram.cli
  groups` prints the whole resolved map — the Telegram app is the single reader of these
  settings, and every app asks it through `db_ops.lib.notify_route`.
- **Silence is an alerting choice, not a logging one.** The file log and the `job_runs` row
  are always written. A silenced job is still fully recorded and still appears in reports.
- **`"notify": {}` means "the caller's defaults"** — an app that notifies by default keeps
  doing so, one that does not, does not.
- **Inheritance is rule by rule.** Where a config nests work (a backup entry and its
  `jobs[]`), the parent's object is the child's default and the child overrides one rule
  without restating the other.
- **An event covering several entries** overrides a rule only when every entry agrees, and
  stays silent only when every entry silences it — one entry's preference must not delete a
  message another entry is waiting for.

Legacy spellings are still **read** — the two rules at the top level of an entry instead of
nested under `notify`, and the boolean form (`"logging_on_run": true`) — but nothing new is
**written** in them: `config_admin.add_sql_task` emits the canonical object, and both shipped
config files have been migrated to it. A half-migrated file is how a shared convention
quietly stops being one.

**Worked example** — a backup entry whose full backup reports every run while the WAL job
beside it, running every 15 minutes, would drown the group:

```jsonc
"jobs": [
  { "job": "database", "notify": { "logging_on_run": { "enabled": true,  "telegram_chat": "", "chat_id": "" },
                                   "alert_on_error": { "enabled": true,  "telegram_chat": "", "chat_id": "" } } },
  { "job": "wal",      "notify": { "logging_on_run": { "enabled": false, "telegram_chat": "", "chat_id": "" },
                                   "alert_on_error": { "enabled": true,  "telegram_chat": "", "chat_id": "" } } }
]
```

### Remote access (`cmd_access`) — how to reach a machine

**Parser:** `db_ops.lib.cmd_access` — `resolve_platform`, `resolve_cmd_access`,
`resolve_cmd_credential` read the block as *config*, and
`db_ops.common.remote_exec.RemoteAccess.from_json(access, credential=..., secrets=...)` turns the
resolved block into a session. **Used by:** metrics (cmd/docker collectors), host_ops, sre,
backup_restore, control.

The split is the same one `sql_access` has below: reading the block is a rule about values that
`metrics` applies to 42 targets before anything is connected to, so it is in `lib` where every
component may import it; opening the session is an operation and stays in `common`.

```jsonc
"cmd_access": {
  "enabled": true,
  "method": "ssh",            // ssh | winrm | local
  "host": "192.0.2.5",
  "port": 22,                 // default: ssh 22, winrm 5985 (5986 with ssl)
  "platform": "linux",        // decides the default shell
  "shell": "bash",            // bash | powershell | cmd
  "auth_type": "key",         // ssh: key | password
  "key_file": "worker.key",   // bare name -> data/ssh_keys/, or an absolute path
  "credential_name": "...",   // -> a users.json remote_credentials entry
  "timeout_seconds": 30,            // opening the session
  "command_timeout_seconds": null,  // running a command; null = unbounded
  "ssl": false                // winrm
}
```

The two timeouts are separate on purpose: a session opens in seconds, but the command it
runs may take an hour. Sharing one number silently kills exactly the long operations this
object exists to drive.

The credential object (`username` + `password` / `password_env` / `password_ref`) may be
passed alongside; the access object wins on any key both define. Secret resolution order is
`password` → `password_env` (an env var name) → `password_ref` (the encrypted store).
Full behaviour — sessions, script shipping, error classification — is in
[Reaching a VM (`remote_exec`)](#reaching-a-vm-remote_exec) above.

### `sql_access` — how to run SQL against a target

**Parser:** `db_ops.common.oracle_bridge.normalize_sql_access` (promoted from
`metrics.targets._resolve_sql_access` when `sql_run` became the second reader).

```jsonc
"sql_access": {
  "method": "direct",         // direct | api | subprocess
  "bridge_url": "...",        // required when method = api (the Oracle 8i HTTP bridge)
  "mode": "sysdba",           // legacy only; default: sysdba when the credential says role SYSDBA
  "tool_dir": "tools/python32_legacy",   // subprocess only; where the legacy tool lives
  "python_exe": "tools/Python27-32/python.exe",  // subprocess only, relative to tool_dir
  "launcher": ["docker", "run", "--rm", "-i", "legacy-oracle-8i"],  // subprocess, non-local tool
  "schema": "LTR",            // legacy only: ALTER SESSION SET CURRENT_SCHEMA before the SQL
  "timeout_seconds": 30
}
```

`direct` connects to the database. `api` and `subprocess` both run the SQL through the legacy
Oracle tool instead — the only way to reach an Oracle 8i host, since no driver db_ops can install
speaks 8.1.7. **The tool is not part of the package**: `tool_dir` and `python_exe` say where you
installed it, and both default to the layout this project happened to use. They differ only in *where the tool runs*: `subprocess`
starts it on this machine, `api` POSTs to a host that has it. Read by `metrics` (SQL-family
collection) and by `common.sql_run` (`run-sql`, `/spbot_sql_to_xlsx`); a `run-sql` request may
carry its own `sql_access` block to override the instance's for one run.

**No credential belongs in here.** The connect string is assembled per run from the target's
`users.json` credential and the encrypted store. `connect_ref` (a secret holding a whole
`user/pass@host/service` string) is still honoured for a login that exists nowhere else, but the
two that used to exist were deleted for duplicating a password.

### Where each object appears

| File | Objects |
| --- | --- |
| `data/restore_config.json` | `time_window`, `notify` (on `backups[]`, on `backups[].jobs[]`, on `restores[]`) |
| `data/sql_targets.json` | `time_window`, `notify` |
| `data/sql_commands.json` | `time_window` |
| `data/db_instances.json` | `cmd_access`, `sql_access` |
| `data/app_commands.json` | `time_window` |
| `data/metric_definitions.json` | `time_window` |
| `data/reports_config.json` | `time_window` |

---

## Consumers

| `common` module | Primary consumers |
| --- | --- |
| `shell` | backup_restore, metrics (PowerShell-driven Windows targets) |
| `remote_exec`, `ssh` | metrics (cmd/docker collectors), sre (lab provisioner), backup_restore (Linux targets, remote share preflight), control (worker host). The `Invoke-Command` builders and PowerShell quoting are `lib.powershell` — building a script is not running one |
| `secret_text`, `data_sources` | every app + the daemon (key forwarded to children) |
| `sql_execution` | metrics, sql_tasks, backup_restore, sla |
| `sql_run` | telegram (`spbot_sql_to_xlsx`), the `run-sql` CLI |
| `db_connect` | `sql_run` (so telegram + the CLI), metrics (`executor`) |
| `xlsx_export` | telegram (`spbot_sql_to_xlsx`), sql_tasks (a target with `output.format = "xlsx"`) |
| `xlsx_import`, `delimited_import`, `table_load` | telegram (`spbot_xlsx_to_table`), the `create-table-from-xlsx` CLI |
| `db_catalog` | the `list-databases` / `list-schemas` CLIs; the prompts behind `spbot_xlsx_to_table` |
| `time_window` | jobs/daemon, sql_tasks, metrics, reports |
| `policy_engine`, `event_policy`, `target_flags`, `metric_targets_config` | metrics, reports, sla, telegram |
| `oracle_bridge` | metrics (SQL-family collection for Oracle 8i targets), sql_run (`run-sql`, `/spbot_sql_to_xlsx` on an 8i target) |
| `password_rotation` | the `rotate-password` CLI (operator-driven; no app calls it on a schedule) |
| `secret_check` | the `check-secret` CLI, and any audit of the secret store |
| `metric_store` | metrics (collector, cli), reports (inventory_health, server_report), jobs (status), sla |
| `inventory_render` | control (`inventory-workflow`), reports (`build-inventory-workflow`), the `inventory-summary` CLI |
| `confirm` | `host_ops` (restart, service stop/restart), `sqlserver_patch` (apply-cu) — and every dangerous operation added later |
| `evidence` | `host_ops`, `sqlserver_patch` — and any future operation that changes a production host |
| `host_ops` | the `host-facts` / `host-service` / `host-restart` CLIs, `sqlserver_patch`. Metrics reads the `cmd_access` resolution from `lib.cmd_access` directly |
| `sqlserver_patch` | the three `sqlserver-*` CLIs (operator-driven, inside a maintenance window) |

---

*Part of the db_ops component documentation set (`docs/NN_*`). This library underpins
ORD 01–12; changes here can affect every app, so run the shared-behavior tests
(`tests/`) after modifying it.*
## SQL Server instance metadata (`sqlserver_instance`)

### The gap this closes

| Engine | Backup | Does the restored server carry the source's server-level state? |
| --- | --- | --- |
| Oracle | `BACKUP INCREMENTAL LEVEL n DATABASE`, restored with `DUPLICATE` | **Yes** — users, roles, grants, profiles and password hashes live inside the database (`SYS.USER$`), so they come with the datafiles. |
| PostgreSQL | `pg_basebackup` of the whole cluster | **Yes** — roles and their password hashes are cluster-global, in `pg_authid` inside `PGDATA`. |
| SQL Server | `BACKUP DATABASE [db]`, user databases only (`WHERE database_id > 4`) | **No** — `master`, `msdb` and `model` are excluded on purpose. Everything in them is lost. |

So the restored SQL Server instance has the data and none of the machinery: no logins, no server
roles or permissions, no credentials, no linked servers, no endpoints, no `sp_configure`, no
Database Mail, and no SQL Agent at all.

The sharpest symptom is **orphaned users**. A restored user database keeps its
`sys.database_principals` rows carrying the *source's* SIDs. A login recreated by name has a new
SID, so it resolves nothing — it appears in every listing and still cannot connect to a single
restored database. That is why this module preserves SIDs, and why `verify` reports the orphan
count as its headline number.

Restoring `master`/`msdb` instead is not an option: SQL Server does not support it across
versions, and those databases carry host-specific state anyway. Generated SQL can be read,
diffed, reviewed, and applied to a **newer** build, which is the actual requirement.

### The three commands

```bash
# Read the instance, write server/*.sql + manifest.json. Read-only.
python -m db_ops.common.cli sqlserver-export-instance \
  '{"target": "ACME-192-0-2-115", "output_dir": "runtime/instance_bundles/2-115"}'

# Apply a bundle to a target, in dependency order, with version/edition gates.
python -m db_ops.common.cli sqlserver-replay-instance \
  '{"target": "NEW-HOST", "bundle_dir": "runtime/instance_bundles/2-115",
    "phase": "pre-database", "dry_run": true}'

# Compare a target against a bundle. Read-only.
python -m db_ops.common.cli sqlserver-verify-instance \
  '{"target": "NEW-HOST", "bundle_dir": "runtime/instance_bundles/2-115"}'
```

They share `_gate_command` with the `host-*` and `sqlserver-*` patch commands, so the contract is
identical: one JSON object in, a `GateReport` dict out, progress on stderr, JSON on stdout, exit 0
unless a blocking gate failed, evidence under `runtime/evidence/`.

### An export taken by a login that cannot see everything

Both of these were found by the first real migration through this path (192.0.2.248 → the
MSSQL25 container, 2026-08-10) and neither is exotic — they are what happens when the export login
is a DBA account rather than `sysadmin`, which is the normal case.

**One refused artifact used to destroy the whole bundle.** `msdb.dbo.sysmail_server` denied SELECT
to `dba_user`, the exception escaped the per-artifact loop, and `manifest.json` — written last
— never happened. Seven `.sql` files sat on disk and the restore's metadata replay reported the
directory *is not an instance bundle*: the database was restored and every user in it stayed
orphaned. Each artifact now runs in its own `try`, and a refusal is recorded twice — a `WARN` gate
naming the engine's own words, and **`artifacts_failed` in the manifest**. Named rather than merely
absent from `artifacts`, because a reader six months later has no other way to tell *this instance
had no linked servers* from *the export was not allowed to look*. On that instance 11 artifacts
exported and 3 (`agent_jobs`, `db_mail`, `proxies`) did not.

**`CREATE LOGIN ... WITH PASSWORD = NEWID()` is not valid T-SQL.** `PASSWORD` takes a string
literal, not an expression, so SQL Server rejects the statement at parse time with
`Incorrect syntax near 'NEWID'`. The branch only runs when the source's password hash could not be
read, so it had never executed — on the instance the code was written against, the hash always
could be. Exported by a login without `VIEW ANY DEFINITION`, all 35 SQL logins took it and all 35
failed. The password is generated into a variable and the statement executed through
`sp_executesql` now; the four-character suffix on the GUID is what satisfies `CHECK_POLICY = ON`,
where a hex-only string can fail complexity and take the login with it.

### The export login decides whether the migration is usable

`sys.sql_logins.password_hash` requires **CONTROL SERVER**. A `securityadmin` login sees every row
and `NULL` in that column — no error, no warning, just the random-password branch taken for every
login. On 192.0.2.248 the metrics login `dba_user` is exactly that:

| Export login | `sysadmin` | `CONTROL SERVER` | `password_hash IS NULL` | Result on the target |
| --- | ---: | ---: | ---: | --- |
| `dba_user` | 0 | 0 | **32 / 32** | 29 logins created, none loggable-into, 3 msdb artifacts refused |
| `svc_backup` | 1 | 1 | **0 / 33** | 30 logins with the source's own passwords, 14 / 14 artifacts |

So the `server_metadata` block takes **`credential_name`**, overriding the instance's default login
for this step only. Any key the block does not recognise flows through `extras` into the request
`sqlserver-export-instance` receives, which is what makes this a config change rather than a code
one. Point it at a login that has CONTROL SERVER; the alternative — granting CONTROL SERVER to the
monitoring account — is sysadmin in all but name, on a production instance, to make one export
work.

**Re-running the export does not repair logins already replayed.** Every `CREATE LOGIN` is guarded
by `IF NOT EXISTS`, deliberately (see below), so a second replay skips them and their random
passwords stay. Fixing a migration that was exported by the wrong login means dropping the logins
the first replay created on the target and replaying again — safe because the bundle preserves
SIDs, so the database users re-map the moment the logins come back.

### What a replay does **not** do to the target's passwords

Worth stating because the failure mode would be silent and expensive:

- **`sa` is never exported.** `data/sqlserver_instance_policy.json` lists it in
  `logins.skip_names` (with `distributor_admin`), and `skip_name_prefixes` drops `##`,
  `NT AUTHORITY\`, `NT SERVICE\` and `BUILTIN\`. Replaying onto a db_ops-provisioned container
  therefore cannot disturb the `sa` password db_ops itself issued and stores.
- **A login that already exists keeps its own password.** Every `CREATE LOGIN` is wrapped in
  `IF NOT EXISTS`, and the generator emits no `ALTER LOGIN ... WITH PASSWORD` at all. The replay
  *adds* logins; it does not reconcile them.
- **One asymmetry to know about:** `ALTER LOGIN … DISABLE` is emitted *outside* the guard, so a
  login disabled on the source is disabled on the target even if it already existed and was
  enabled. There is no matching `ENABLE`, so a replay can disable but never re-enable. Harmless
  onto a fresh container; worth checking before replaying onto a populated instance.

### Two phases, and why replay is not one pass

`phase` is not a convenience. Half the bundle must be applied **before** the user databases are
restored and half **after**:

| Phase | Artifacts, in order | Why this side |
| --- | --- | --- |
| `pre-database` | `sp_configure`, `credentials`, `logins`, `server_roles`, `permissions`, `endpoints`, `linked_servers` | Logins must exist before the databases arrive, or every restored database's users are orphaned. `sp_configure` is first because it enables the features later steps need (Agent XPs, Database Mail XPs). |
| `post-database` | `db_mail`, `operators`, `proxies`, `agent_schedules`, `agent_jobs`, `alerts`, `model_options` | Agent job steps name databases, which have to exist. A job replayed too early fails on its first schedule — quietly, at 02:00. |

The order lives in `data/sqlserver_instance_policy.json`, not in Python: which settings are
portable between two instances is a per-estate decision, and it changes with the SQL Server
version.

### Secrets

Two categories, and they are genuinely different:

- **Recoverable as ciphertext.** SQL login password hashes (`sys.sql_logins.password_hash`) are
  replayed verbatim with `CREATE LOGIN ... WITH PASSWORD = 0x... HASHED, SID = 0x...`. The login
  keeps its password *and* its SID, so applications keep their connection strings and restored
  databases have no orphans. Windows logins carry no hash and their SID belongs to the domain, so
  they are created `FROM WINDOWS` with no SID clause.
- **Not retrievable at all.** Credential secrets, linked-server remote passwords, proxy passwords
  and Database Mail SMTP passwords are encrypted with the service master key and have no read
  path. The export emits a `{{secret:NAME}}` placeholder and records it in `manifest.json`; it
  never invents a value. Replay resolves them from `data/encrypted_secret_text.json` and **fails
  closed**, listing every unresolved reference, *before* executing anything — otherwise a
  credential is created holding the literal placeholder and fails at first use, in a way nobody
  traces back to the replay.

No plaintext is ever written to an artifact. Certificates (endpoint, TDE) are files with private
keys, not settings: they must be moved separately, and the export says so rather than omitting
them silently.

### Host-specific settings are shown, not applied

`max server memory`, `max degree of parallelism`, `affinity mask` and their neighbours describe
the **machine**, not the workload. Replaying a 256 GB source's memory ceiling onto a 32 GB target
is worse than not replaying it. They are exported **commented out**, with the source value
visible, and reported as skipped:

```sql
-- EXEC sp_configure 'max server memory (MB)', 262144;  -- SKIPPED: host-specific; source value 262144
```

An option the policy does not classify is treated the same way — an unclassified option is one
nobody has decided about, and applying it by default is how a setting nobody chose reaches a
production rebuild. `"on_unsupported": "fail"` turns every skip into a hard stop for callers that
want no surprises; the default `"skip"` suits a DR rebuild that wants the 90% applied and the
10% listed.

### Gates

Evaluated before anything executes: major version (downgrade refused outright, upgrade allowed
with per-artifact minimums), edition (Express has no SQL Agent, so the Agent artifacts are skipped
with a stated reason rather than attempted and failed), and server collation (reported loudly but
not blocking — it does not stop logins working, it changes comparison semantics for everything
created afterwards, and it is the kind of thing discovered six months later).

Within a file, one failing batch does not abort the rest: a rebuild wants the 90% that applied
plus a list of what did not, not a stop at the first linked server whose provider is missing.

### What the live trials changed

Exported from `CLOUD-203-0-113-188-MSSQL-1433` (SQL Server 2022, build 16.0.4265.3) and replayed
onto `CLOUD2-203-0-113-121-MSSQL-1433` (build 17.0.4065.4) on 2026-08-06 — a real cross-version
upgrade path, first against a bare instance and then against one seeded with logins, a database
user mapping, a user-defined server role, server permissions, a credential, a linked server, an
operator, a schedule and a two-step Agent job.

Nine defects, none of which the offline tests could have found:

| Symptom | Cause | Fix |
| --- | --- | --- |
| Export died on the first query | `SERVERPROPERTY()` returns `sql_variant`, which the ODBC driver refuses to hand to pyodbc (`ODBC SQL type -16 is not yet supported`) | `CAST(... AS NVARCHAR)` server-side |
| `CREATE SERVER ROLE [public]` emitted | SQL Server reports `is_fixed_role = 0` for `public` | explicit built-in role list |
| `sp_configure` rejected entirely | `CONFIG statement cannot be used inside a user transaction` — the connection defaulted to `autocommit=False` | replay connects with `autocommit=True` |
| One bad statement took its whole file down | no `GO` separators, so each file was a single batch | one batch per statement; the next run applied 20 of 22 `sp_configure` settings with 2 rejected |
| `ALTER SERVER ROLE ... ADD MEMBER [sa]` | `sa` is a *special* principal (error 15405) | membership skips the same set the logins exporter does |
| Replay left `show advanced options = 1` behind | the file turns it on to set advanced options and never turned it back | it is restored to the source's value at the end |
| Agent job replayed as broken fragments | the `IF ... BEGIN ... END` block was split by the new `GO` separators, so each step ran alone against a job that had not been created | a job is emitted as one batch |
| `model_options` failed on `TRUSTWORTHY` | SQL Server exposes `is_trustworthy_on` and refuses `ALTER DATABASE model SET TRUSTWORTHY` (error 15309) | removed from the portable list |
| **`verify` reported "no orphaned users" for databases it never read** | `USE db; SELECT ...` returns after the `USE`, whose result set does not exist; `_rows` raised, the caller recorded `0`, and the gate summed zeros | three-part naming; an unreadable database is now `None`, and the gate says how many were **not** checked |

That last one is the serious one. The orphaned-user count is the single number this capability
exists to produce, and it was structurally incapable of being anything but zero. It now reads
`no orphaned users across N database(s)` — with the count of databases, so a clean verdict states
how much was actually looked at.

Two behaviours also became contract:

- **"Not supported by this edition" is a skip, not a failure.** `xp_cmdshell` and
  `Ole Automation Procedures` exist on Windows and not on Linux, so a Windows-to-Linux replay
  meets them every time. A `FAIL` line that is usually wrong is one nobody reads.
- **An artifact with nothing in it reports `0 ok`, not `1 ok`.** The header comment block used to
  execute as a batch of its own.

### Measured results

Replaying the seeded bundle put every object on the target, and the property that matters held:

| | source | target | |
| --- | --- | --- | --- |
| `dbops_app_user`, `dbops_report_user`, `dbops_disabled_user` | 3 logins | 3 logins | **SID and password hash byte-identical** |
| disabled state, default database | preserved | preserved | |
| server role + membership, 5 server permissions | 6 | 6 | |
| credential, linked server (+ remote login) | 2 | 2 | secrets resolved from the store |
| operator, schedule, 2-step Agent job | 3 | 3 | |
| `sys.configurations` after two replays | — | **identical to its pre-replay state** | idempotency, measured |

The SID result is the whole point: a login recreated by name gets a new SID and resolves nothing,
so every user in a restored database is orphaned. These logins resolve.

### Encryption does not travel, and the export says so

The trial also created a Database Master Key, a certificate, a symmetric key and an Always
Encrypted CMK/CEK pair. **None of them are in the bundle, and none should be** — they are
database-scoped, so they belong to the user-database backup. But three of them do not survive a
restore onto a different instance either, and nothing in db_ops said so. The export now writes
`server/crypto_prerequisites.txt` and raises a non-blocking gate:

- A **Database Master Key** is encrypted by the instance's Service Master Key as well as by its
  password. The target has a different SMK, so after a restore it opens only with the password —
  which has to be recorded *before* the restore, not discovered afterwards.
- **Certificates and symmetric keys** travel inside the database, and work only once that master
  key is open.
- An **Always Encrypted Column Master Key** is only metadata here: provider name and key path.
  The key lives in a Windows certificate store or a key vault, outside SQL Server, and is read by
  the *client*. The restored database keeps the metadata and the encrypted CEK; the data stays
  unreadable until that key reaches the client.
- **TDE certificates in `master`** are excluded by the user-database backup, and a TDE database
  will not mount without them. The scan found two real certificates on the lab instance —
  including `db_ops_backup_cert`, which `mssql_backup_database.sh` already exports to
  `$BACKUP_DIR/_cert/`, so that one is covered.

### Turning it on for a backup or restore

Opt-in, per entry, in `data/restore_config.json`. **An entry with no `server_metadata` block, or
one that says `enabled: false`, behaves exactly as it did before this existed** — that is the
design constraint, not a nicety: the scheduled MSSQL flows have been running for months and must
not change because a capability appeared next to them. Every SQL Server entry ships with the block
present; the CLOUD pair (`CLOUD_MSSQL_FULL`, `CLOUD_MSSQL_DIFF` and the
`CLOUD_MSSQL_TO_CLOUD2` drill that consumes them) has it on, the rest are `enabled: false`.

On a **backup** entry — it reads its own instance, so it needs no target:

```json
"server_metadata": {
  "enabled": true,
  "artifacts": ["logins", "server_roles", "permissions", "agent_jobs"]
}
```

On a **restore** entry — note there is no target to state:

```json
"server_metadata": {
  "enabled": true,
  "artifacts": [],
  "phases": ["pre-database", "post-database"]
}
```

| Field | Means |
| --- | --- |
| `artifacts` | The array of what to include. Empty or omitted = every artifact the policy declares. It exists so an operator can take logins and Agent jobs without also taking `sp_configure`, the setting most likely to be unwanted on a different machine. |
| `phases` | Which halves to run. Omitted = both. Naming only `pre-database` is a legitimate split — restore the logins now, the jobs later. |
| `on_unsupported` | `skip` (default) or `fail`. |
| `target` | **Optional override, not normally set.** See below. |

**The target instance is derived, not restated.** A restore entry already says where it restores
to, and the inventory already says what lives there:

```
target_server_id: CLOUD2-203-0-113-121-HOST   ->  ip 203.0.113.121
target_container: mssql_ha_cloud2-primary        ->  instance_name in db_instances.json
                                                  =   CLOUD2-203-0-113-121-MSSQL-1433
```

`resolve_replay_target` walks that chain: an explicit `target` wins; failing that, a
`target_server_id` that already names a SQL Server instance is used directly; otherwise the
host + container pair is matched against the inventory. A field repeating the answer would be a
second place for the same fact to live, and the two would drift the first time a container was
renamed. An ambiguous or absent match raises rather than guessing — replaying logins onto the
wrong instance is not an error anyone notices quickly. `target` stays only for the case the chain
cannot answer: an instance that is not in `db_instances.json` at all.

The block is parsed when the config is **loaded**, not when the restore runs: a misspelled
artifact name would otherwise sit in the file until the 02:00 restore that needed it, and surface
as a missing login rather than as a bad config line.

Failure handling differs by side, deliberately. An export failure is *returned*, never raised —
the data backup must not be lost to a metadata step that could not read `sys.credentials`. A
replay failure is returned as a failed result the caller acts on, because swallowing it would
leave a restored database nobody can log into while the restore reports success.

### Where the bundle lives

`runtime/instance_bundles/<source server_id>/server/`, **on the machine db_ops runs on** — the
worker, in production. Not `<backup_dir>/server`, which is what an earlier round of these notes
promised: `backup_dir` is a path *inside the database container on the remote host*
(`/var/opt/mssql/backup/dbops` on 203.0.113.188), reachable only over SSH by the backup script.
The export is not a shell step — it is a TDS connection made from the db_ops node, writing with a
local `Path.write_text` — so aiming it at the container path would have quietly created that
directory on the worker instead, next to nothing.

Keying the directory on the **source** `server_id` is what lets the two halves meet without
another config field: the backup entry exports under its `server_id`, and a restore entry's
`server_id` *is* that same source instance, so the restore finds the bundle by asking the same
question. `runtime/` is a bind mount on the worker, so a bundle outlives the container.

The one mismatch this can be configured into is `server_metadata` on for a restore and off for the
same instance's backup. The restore then finds no bundle; it skips the phase with a warning naming
that backup entry, rather than reporting a bare "bundle not found" that sends an operator to
inspect the half that is correct.

### Replaying onto an instance that already has its own objects

Measured on the lab pair, not assumed. Five objects were created on the CLOUD2 target that do
not exist in the CLOUD bundle (a login, a server role, an operator, a job and its step), then
the whole bundle was replayed **three times**:

| | before | after 1st | 2nd | 3rd |
| --- | --- | --- | --- | --- |
| total server objects on the target | 47 | 47 | 47 | 47 |

All five target-only objects survived intact, there were no duplicates, and nothing at all
changed between the first replay and the third. Every generated statement is guarded with
`IF NOT EXISTS` and **nothing is ever dropped**, so replay adds what is missing and leaves
everything else — including objects the source has never heard of — untouched.

### Where the scheduled run calls it

`run_backup` exports **after** a backup whose status is `done`, and never for one that failed: a
bundle is only meaningful beside the backup it was taken with, and writing a fresh one after a
failed backup would pair tonight's logins with a backup that does not exist. The result is
summarised into the run row's `metadata.server_metadata`, and a failed export is named in the
run's message — it cannot change the backup's verdict, but it must not be silent either.

`run_script_restore` replays `pre-database` immediately before the restore script executes (after
any transfer) and `post-database` after it, and only when the restore itself succeeded — Agent job
steps name databases, so replaying them onto a failed restore would create jobs pointing at
databases that are not there. Both print a `PHASE=metadata-<phase> ...` line into the restore's
stdout, alongside the script's own phases, so the run row's `stdout_tail` and the Telegram message
carry them.

**Neither phase can fail the restore.** A missing bundle, an unresolvable target, a replay that
errored — each returns a `SKIPPED`/`FAILED` line and a warning event, and the restore keeps its
own verdict. The databases are the deliverable of a drill; failing the run over the metadata would
report that nothing was restored when in fact everything was. This is the one place where the
module-level rule above ("a replay failure is returned as a failed result the caller acts on")
meets a caller that deliberately acts on it by warning: see `replay_metadata_phase`.

For a one-off outside the schedule, the three CLI commands (`sqlserver-export-instance`,
`sqlserver-replay-instance`, `sqlserver-verify-instance`) still drive the same machinery by hand.

### Both restore paths call the same replay

There are two SQL Server restore paths and they share no execution machinery — the engine path
(`restore_database.py`, SMB share + `sqlcmd`, PITR with `STOPAT`) selects its chain in Python; the
script path (`mssql_restore.sh`) selects its chain in bash. They cannot be merged, and merging them
is not the goal.

What they *do* share is the decision around the restore — same bundle, same two phases, same
ordering rule, same failure policy — and that is one function, `server_metadata.replay_phase`. Both
call it; neither owns a copy. A second copy would drift in the direction nobody notices, because a
phase quietly not running looks exactly like a phase that ran and found nothing to do.

The one difference is how each names the instance to replay onto, and it is a real difference in
what the entries know:

| | names the target by | resolved through |
| --- | --- | --- |
| script path | `target_server_id` + `target_container` | host ip + `instance_name` |
| engine path | `target.credential_target` (an ip) | one SQL Server instance at that ip |

`resolve_replay_target` handles both, and refuses rather than guesses when an ip runs more than one
instance — the same rule the container branch already followed.

**A bundle still has to exist.** It is written by the *backup* entry for that source instance, so a
restore whose source db_ops does not back up has nothing to replay: `ACME_TO_MSSQL2025_DOCKER`
restores from 192.0.2.250, whose backups are written by the customer's own SQL Agent onto an SMB
share, and no db_ops backup entry covers it. Its block is therefore present and `enabled: false` —
turning it on would warn once per run and change nothing else. Closing that needs a bundle source
first: a db_ops backup entry for the instance, or an export taken from the live source at restore
time.
