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
