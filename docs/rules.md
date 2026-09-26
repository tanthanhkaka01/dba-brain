# The rules dbabrain keeps

**This is the one list.** Every rule the project holds itself to is here, numbered, with the test
that guards it and the exceptions that exist today. Other pages explain a rule where it matters to
them - [`architecture.md`](./architecture.md) why the layers are shaped as they are,
[`13_common.md`](./13_common.md) and [`14_lib.md`](./14_lib.md) their own layer, `CONTRIBUTING.md`
what a pull request must do - and they point here instead of keeping a second copy. A rule stated
only somewhere else is a rule nobody can be held to; add it here first.

**How to read it.** The index below is one line per rule: what it says, its mark, and what is left
to do. Each rule then has its own section - the rule in full, its guards, and where it stands today.
The page carries **counts**; the names behind a count (which commands, which files) live in the
guard test, which is the only place they cannot go stale.

`tests/test_every_rule_names_a_guard_that_exists.py` holds this page to itself: every rule is in the
index once and has a section, every guard a section names must exist, and a rule with no guard must
be on that file's list of rules still owed one - which may only shrink.

| Mark | Means |
| --- | --- |
| **absolute** | A guard test fails on any breach, and there is no exception. |
| **baseline** | A guard fails on any *new* breach; the exceptions counted exist today and are debt, each to be removed - never a permission to add another. |
| **none** | No guard yet. The rule still holds; the guard is owed. |
| **review** | A judgement a test cannot make. Held in review. |

---

## The components

**Fifteen components, of two kinds.** Every rule that says *app* or *shared layer* means exactly
these, and the guards hold the same lists (`APPS` and `SHARED_LAYERS` in
`tests/test_import_boundaries.py`).

- An **app** does one job and has its own `cli.py`. It imports `lib` (and `db`, `logging_ops`),
  reaches `common.cli` and `db.cli` only through `transport`, and never imports or runs another app.
- A **shared layer** is what the apps stand on. It never imports an app.
- **Not a component:** the root package `db_ops` - the `db-ops` / `dbabrain` entry point, which only
  dispatches (R41).

| Kind | ORD | Component | Package | Note |
| --- | :---: | --- | --- | --- |
| **app** | 03 | App command daemon | `jobs` | The scheduler; runs the command lines `app_commands.json` configures (R42's named exception) |
| **app** | 04 | Metrics engine | `metrics` | |
| **app** | 05 | SQL task runner | `sql_tasks` | |
| **app** | 06 | Reports | `reports` | |
| **app** | 07 | Telegram | `telegram` | Runs the command templates the bot is configured with (R42's named exception) |
| **app** | 08 | Backup / restore | `backup_restore` | |
| **app** | 09 | SLA / SLO compliance | `sla` | |
| **app** | 10 | SRE | `sre` | |
| **app** | 11 | Control | `control` | The deploy tool - an app like the others, no exemption (R03) |
| **app** | 12 | Web host | `webhost` | |
| **shared layer** | 01 | Runtime store | `db` | The store and its row shapes (R08); `db.cli` is started through `transport` |
| **shared layer** | 02 | Logging engine | `logging_ops` | Imported; no CLI |
| **shared layer** | 13 | Common - operations | `common` | Run as a CLI, never imported by an app (R03); imports only `lib` (R04) |
| **shared layer** | 14 | Lib - values and rules | `lib` | Only imported, never run (R07); imports nothing outside `lib` (R06) |
| **shared layer** | 15 | Transport - the one client | `transport` | The only starter of `common.cli` and `db.cli` (R38); imports only `lib` (R39) |

---

## Index

### 1. Layers - who may import, run or reach whom

R38-R43 keep their numbers and stand with the other layer rules; R43 is R11's sibling.

| # | Rule | Mark | Left |
| --- | --- | --- | --- |
| R01 | [An app never imports another app, nor runs its CLI](#r01) | absolute | - |
| R02 | [A shared layer never imports an app](#r02) | absolute | - |
| R03 | [An app never imports `common` - `control` included](#r03) | absolute | - |
| R04 | [`common` imports nothing but `common` and `lib`](#r04) | absolute | - |
| R05 | [`common` never launches a CLI or a Python module](#r05) | absolute | - |
| R06 | [`lib` imports nothing from `db_ops` outside `lib`](#r06) | absolute | - |
| R07 | [`lib` never starts a process](#r07) | absolute | - |
| R08 | [The four row shapes in `db` import nothing](#r08) | absolute | - |
| R09 | [A `common.cli` command works from its request alone](#r09) | absolute | - |
| R10 | [An app does not reach a host itself](#r10) | absolute | - |
| R11 | [One function, one place - no two apps implement the same operation](#r11) | absolute | - |
| R12 | [Where a thing goes: value -> `lib`, operation -> `common.cli`, `data/` read -> the data reader](#r12) | review | - |
| R38 | [`common.cli` and `db.cli` are started only through `transport`](#r38) | absolute | - |
| R39 | [`transport` imports only `lib`; nothing below imports `transport`](#r39) | absolute | - |
| R40 | [One function in `transport` starts a process](#r40) | absolute | - |
| R41 | [The root package only dispatches](#r41) | absolute | - |
| R42 | [An app never runs another app's CLI from its code](#r42) | absolute | - |
| R43 | [No two commands, in any CLI, do the same job](#r43) | absolute | - |

### 2. The `common.cli` contract

| # | Rule | Mark | Left |
| --- | --- | --- | --- |
| R13 | [Every command takes one JSON object, never flags](#r13) | absolute | - |
| R14 | [A request with a password comes on stdin only](#r14) | absolute | - |
| R15 | [Every answer is one envelope, nothing else on stdout](#r15) | absolute | - |
| R16 | [Every request and answer is described in the reference](#r16) | **baseline** | 61 commands' answers not yet run |

### 3. Configuration and data

| # | Rule | Mark | Left |
| --- | --- | --- | --- |
| R17 | [Every config file and edited record is described, once](#r17) | absolute | - |
| R18 | [The reference's three copies are the same bytes](#r18) | absolute | - |
| R19 | [Configuration is data, never a literal in code](#r19) | review | - |
| R20 | [A shared config object is parsed once, in `lib`](#r20) | absolute | - |
| R21 | [`server_id` is the only key for a machine](#r21) | absolute | - |
| R22 | [Every store table has its keys; config rows are switched off, never deleted](#r22) | absolute | a store made before 0.24.0: `db.cli archive-keys` |
| R23 | ["Not configured" is a state, not a failure](#r23) | absolute | - |

### 4. Behaviour every engine and app keeps

| # | Rule | Mark | Left |
| --- | --- | --- | --- |
| R24 | [SQL Server is reached in `master` unless a database is named](#r24) | absolute | - |
| R25 | [`cmd_access.method: local` aimed at another machine is refused](#r25) | absolute | - |
| R26 | [One bad target never aborts a scan](#r26) | absolute | - |
| R27 | [A value from a chat or config is bound, never pasted into SQL](#r27) | absolute | - |
| R28 | [Telegram severity is tagged once, centrally; messages split under 4096](#r28) | absolute | - |
| R29 | [The Telegram queue moves one row per call](#r29) | absolute | - |
| R30 | [An `async` run never repeats claimed work](#r30) | absolute | - |

### 5. Code, tests and documentation

| # | Rule | Mark | Left |
| --- | --- | --- | --- |
| R31 | [A file Linux executes has LF line endings](#r31) | absolute | - |
| R32 | [The test suite is offline](#r32) | absolute | - |
| R33 | [Every component has a doc, every doc a component](#r33) | absolute | - |
| R34 | [A change updates its doc and `CHANGELOG.md` in the same change](#r34) | review | - |
| R35 | [`AGENTS.md` names only real commands, never a secret on a command line](#r35) | absolute | - |
| R36 | [Everything written is English](#r36) | absolute | - |
| R37 | [Comments say why; tests read as prose](#r37) | review | - |

---

## 1. Layers - who may import, run or reach whom

The components are listed, with their kind, in [The components](#the-components) above.

**R03, R04, R09 and R16 are non-negotiable by the operator's word (2026-09-25):** `common` is the API, reached only as `python -m db_ops.common.cli <command> '<json>'`; a command works from its request alone; `common` imports nothing but `lib`. Where one of them is still marked *baseline*, the debt is the 0.24.0 work, not a permission.

R38-R43 were added after the list was numbered and keep their numbers; they are layer rules and stand here with the others.

### R01

An app never imports another app - nor runs its CLI (R42). What two apps share is a rule (`lib`) or an operation (`common`).

**Guard:**

- `tests/test_import_boundaries.py::test_an_app_never_imports_another_app`

**Mark:** absolute.

### R02

A shared layer (`common`, `lib`, `db`, `logging_ops`) never imports an app.

**Guard:**

- `tests/test_import_boundaries.py::test_a_shared_layer_never_imports_an_app`

**Mark:** absolute.

### R03

**An app never imports `common`.** It reaches `common.cli` through `transport` (R38), and imports `lib` for what it needs in-process.

**Guard:**

- `tests/test_app_common_imports.py::test_an_app_imports_no_common_module_outside_the_baseline`

**Mark:** absolute since 0.24.0 - the guard's `REMAINING` is empty, and any import of `common` from an app fails it.

- **`control` is an app like the others** (the operator, 2026-09-26): *control does not import `common` either - it runs `common.cli`*. It used to be left out of the guard as the deploy tool; it is not exempt.
- The last seven, gone on 2026-09-26: the export's identifier scan runs `common.cli check-identifiers` (a refusal answers `refused`, so the export still says SKIPPED for it and stops for anything else); the config gate's prompt runs `common.cli ask`; and the SSH clients - `control`'s session to the worker and `backup_restore`'s to a Linux restore target - are a `lib.remote_host.RemoteHost`, whose every call is `run-cmd`, `push-file` or `pull-file`. Preflight's WinRM share is a `run-cmd` too.
- **Open: `db/cli.py`** (5 imports: `common.cli` and four modules it answers from). `db` is not an app; whether R03 covers it is undecided.
- Done in 0.24.0: `data_sources` moved to `lib` (eight apps imported it), `metrics`' four execution modules went behind `common.cli metric-batch`, `sre`'s last two went to `lib`; the root `db_ops/cli.py` imports only `lib` (R41).

### R04

**`common` imports nothing but `common` and `lib`** - no app, no `db`, no `db_ops.config`, no root package.

**Guard:**

- `tests/test_import_boundaries.py::test_common_imports_only_common_and_lib`
- `tests/test_import_boundaries.py::test_the_shared_layers_do_not_import_each_other_both_ways`

**Mark:** absolute. Absolute since 0.24.0: `common/cli.py` imported `db_ops.config` and the root package's `__version__`; both are `lib`'s now (`lib/config.py`, `lib/version.py`).

### R05

`common` never launches a CLI or a Python module, by any spelling. It is the callee.

**Guard:**

- `tests/test_import_boundaries.py::test_common_never_launches_a_db_ops_cli`
- `tests/test_import_boundaries.py::test_common_launches_no_python_module_by_any_spelling`

**Mark:** absolute.

### R06

`lib` imports nothing from `db_ops` outside `lib`.

**Guard:**

- `tests/test_lib_is_pure.py::test_a_lib_module_imports_nothing_from_db_ops`

**Mark:** absolute. Absolute since 0.24.0: `notify.py` and `telegram_route.py` read `db_ops.config` until the parser moved into `lib/config.py`.

### R07

`lib` never starts a process - it is only imported. It may *build* a command (`lib.common_cli.CommandSpec`) and *read* an answer; starting it is `transport`'s (R38).

**Guard:**

- `tests/test_lib_is_pure.py::test_a_lib_module_does_not_run_a_cli`
- `tests/test_lib_is_pure.py::test_no_new_module_starts_launching_a_cli`

**Mark:** absolute. Absolute since 0.24.0: `common_cli.py` builds and reads, `transport` starts; `telegram_route.py` reads the configuration in-process.

### R08

The four row shapes in `db` (`job_runs`, `metric_results`, `metric_definitions`, `sla_results`) import nothing.

**Guard:**

- `tests/test_import_boundaries.py::test_a_shape_module_imports_nothing_at_all`

**Mark:** absolute.

### R09

**A `common.cli` command works from its request alone.** It takes a JSON object, does its work entirely from that object, and answers in JSON - so it runs by hand on a node whose configuration is completely empty. There are two kinds of command and no third (the operator, 2026-09-26):

- **A command whose job is reading or writing a configuration file may do so** - it registers, edits, stores, audits, lists or renders what the node configured (`sql-command-add`, `secret-set`, `check-objects`, `rotate-password`, `self-status` ...). It too runs on an empty configuration: it creates the file it writes, or says in words what is not configured yet.
- **A command that takes input, works and answers never looks anything up in configuration** - not a host, a login, a password, a key, a path, a threshold, a policy, a list of names. Every fact it needs is in the request, stated by the app that calls it (`lib.data_sources.request_fill` finishes a request from the app's own `data/`). A request missing one is refused with a sentence naming the field - never a lookup.

**Guard:**

- `tests/test_a_common_command_works_from_its_request_alone.py::test_an_operation_runs_from_a_complete_request_on_a_broken_configuration`
- `tests/test_a_common_command_works_from_its_request_alone.py::test_an_operation_refuses_an_empty_request_without_opening_the_configuration`
- `tests/test_a_common_command_works_from_its_request_alone.py::test_a_command_that_edits_the_configuration_runs_on_an_empty_one`
- `tests/test_a_common_command_works_from_its_request_alone.py::test_an_empty_configuration_is_said_in_words_not_as_a_raw_error`
- `tests/test_a_common_command_works_from_its_request_alone.py::test_the_commands_still_looking_up_only_shrink`
- `tests/test_common_layers.py::test_the_resolver_tier_only_shrinks`
- `tests/test_common_layers.py::test_a_common_module_reads_no_local_state_unless_it_is_listed`
- `tests/test_common_layers.py::test_a_backup_restore_or_docker_module_reads_no_config_at_all`

**Mark:** absolute since 0.24.0.

- **By command.** The guard runs the real CLI against an empty root and a poisoned one (every configuration file present and unreadable). Every command is in one of the two kinds - `CONFIGURATION_IS_ITS_JOB` names the first (30), and every other command is held to its request (56). None looks a fact up (`STILL_LOOKS_UP` is empty; 22 did when 0.24.0 opened, plus four lookup doors - `run-sql`, `run-cmd`, `probe-host` and the file transfers resolved a bare `server_id` until they took `connection` / `access` / `host`).
- **By module.** 9 of `common`'s 90 modules read configuration (`READS_LOCAL_CONFIG`), each named with its reason, and every one is the first kind: `cli.py`, which routes those commands, 5 registrars, and the audits of the store and of the estate's names (`password_rotation`, `secret_check`, `identifier_scan`). The list may only shrink. The scan follows an import into `lib`, so a read moved one import away still counts: that is how `remote_exec` read the store through `lib.secret_value` until 0.24.0.
- **The transports read nothing** since 0.24.0: `sql_run`, `remote_exec` and `ssh` found a key name under `data/ssh_keys/`, a `password_ref` in the store or a `server_id` in the inventory when the caller passed nothing. A caller states the login; the store-backed lookup is `lib.data_sources`' (`resolve_secret_value`, `ssh_login`, `request_fill`), for the apps.
- **Reviewed against the restated rule (2026-09-26):** `metric-severity` writes a remap into `db_instances.json` and `lift-example` writes a `data/*.example.json` - the first kind, as they were classed; `self-status` and `timezone` report this node, its configuration included - moved to the first kind. `rotate-password` and `check-secret` find a ref's server in the inventory because the store is their job - they fill that target the way the apps do.
- Backup, restore and the lab-docker commands are config-free by name, without exception.

### R10

An app does not reach a host itself - no app-owned `ssh`, `scp`, `smbclient`, `sqlcmd`, `robocopy`, `cmdkey`, PowerShell remoting or `ansible`, and no library that opens a session to a host. It asks a `common.cli` command, through `transport`.

**Guard:**

- `tests/test_no_app_reaches_a_host_itself.py::test_an_app_starts_no_process_this_file_does_not_account_for`
- `tests/test_no_app_reaches_a_host_itself.py::test_an_app_imports_no_library_that_opens_a_session_to_a_host`
- `tests/test_no_app_reaches_a_host_itself.py::test_the_host_reaches_left_only_shrink`

**Mark:** absolute since 0.24.0 - `HOST_REACH_LEFT` is empty (23 starts measured when the guard was written).

- **Every app reaches a host one way** (the operator, 2026-09-26: *an app does not connect to a host directly - through `lib` or `common.cli`, so every app does it the same way*).
- The last eleven were the SQL Server SMB/Windows restore in `backup_restore`: its shares go through `smb-list` / `smb-get` / `smb-delete` / `smb-credential` (`smbclient` on Linux, the UNC path after `cmdkey` on Windows - `common.smb`), its certificate import on a Windows target through `run-cmd` over WinRM, its local `sqlcmd` and CHECKDB through `run-sqlcmd`.
- Before that, in 0.24.0: `sre` (ssh and ansible through `run-cmd`, scp through `push-file`), `metrics` (SQL and scripts through `metric-batch`), `control` and `backup_restore`'s Linux target (`lib.remote_host.RemoteHost`).
- Not a host reach, and named in the guard's `NOT_A_HOST_REACH`: the daemon's and the bot's configured command lines, `sre` re-launching itself, `sre`'s VMware PowerShell run on this machine, the lab orchestrator's `ssh-keygen`, a SQL task's Python input, the export's `git`, the deploy tool's local image build.

### R11

One function name, and one function body, exists in exactly one place. No two apps implement the same operation.

**Guard:**

- `tests/test_no_duplicate_definitions.py::test_no_new_definition_is_duplicated`
- `tests/test_no_duplicate_definitions.py::test_no_new_function_body_is_duplicated_under_another_name`

**Mark:** absolute.

### R12

Where a thing goes: a value or a rule -> `lib`; an operation -> a `common.cli` command; a read of `data/` -> the data reader. A one-off script means a missing `common.cli` command: build it in, in the same change.

**Guard:** review

**Mark:** review.

### R38

`common.cli` and `db.cli` are started only through `transport` - never by an app, `lib` or `db` directly.

**Guard:**

- `tests/test_transport_is_the_only_client_of_common_and_db.py::test_nothing_outside_transport_starts_common_or_db_cli`

**Mark:** absolute.

### R39

`transport` imports only `lib`; `lib` and `common` never import `transport`.

**Guard:**

- `tests/test_transport_is_the_only_client_of_common_and_db.py::test_transport_imports_only_lib`
- `tests/test_transport_is_the_only_client_of_common_and_db.py::test_lib_and_common_never_import_transport`

**Mark:** absolute.

### R40

Inside `transport`, one function starts a process: `transport/process.py::execute`.

**Guard:**

- `tests/test_transport_is_the_only_client_of_common_and_db.py::test_one_function_in_transport_starts_a_process`

**Mark:** absolute.

### R41

**The root package holds nothing of its own.** `db_ops/cli.py` - the `db-ops` / `dbabrain` command - only dispatches: `db-ops <component> ...` to that component's CLI, and the words in its `ALIASES` table (`init`, `guide`, `encrypt-secret`, `export-data`, `import-data`) to the `common.cli` command of the same name. A command it answers itself belongs to `common` (an operation) or `lib` (a rule).

**Guard:**

- `tests/test_the_root_package_only_dispatches.py::test_the_root_cli_answers_no_command_of_its_own`
- `tests/test_the_root_package_only_dispatches.py::test_the_root_cli_imports_only_lib`
- `tests/test_the_root_package_only_dispatches.py::test_the_root_holds_only_its_entry_point`

**Mark:** absolute. Absolute since 0.24.0. Moved out of the root: the five tool-root commands to `common/cli_tool_root.py`, `check-credentials` to `common/cli_check_credentials.py` (its loaders to `lib.data_sources`), `scaffold.py` and `agents_guide.md` to `common`, `levels.py` to `lib`.

### R42

**An app never runs another app's CLI from its code.** What two apps share is a rule (`lib`) or an operation (`common`). Two named exceptions, because running configured command lines is their job: the daemon (`jobs`, `app_commands.json`) and the bot (`telegram`, its command templates) - the lines come from configuration, not from code.

**Guard:**

- `tests/test_no_app_runs_another_apps_cli.py::test_an_app_does_not_start_another_apps_cli`
- `tests/test_no_app_runs_another_apps_cli.py::test_the_calls_left_only_shrink`

**Mark:** absolute. Absolute since 0.24.0 - the four calls measured on 2026-09-25 are gone: `telegram`'s alias of `reports.cli`; the bot's SQL task listing (now `lib.sql_task_catalog`); `reports` collecting through `metrics.cli collect` (*a report does not run metrics*); `control worker-create-db-docker` running `sre.cli` in the worker (it builds through `common`, registers with `lib.docker_db_registry`).

### R43

**No two commands, in any CLI, do the same job** (the operator, 2026-09-26). One job is one command; an app that needs the job calls that command. R11 holds the *code* to one place and cannot see two commands reaching the same code from different doors - `control.cli inventory-summary` and `common.cli inventory-summary` both render with `lib.inventory_render`, so R11 holds while a person has two commands to learn and keep in step.

**Guard:**

- `tests/test_no_two_commands_do_the_same_job.py::test_a_command_name_in_two_clis_is_a_front_door_two_jobs_or_a_listed_duplicate`
- `tests/test_no_two_commands_do_the_same_job.py::test_a_front_door_hands_its_request_to_the_common_command_of_its_name`
- `tests/test_no_two_commands_do_the_same_job.py::test_the_duplicates_only_shrink`
- `tests/test_no_two_commands_do_the_same_job.py::test_every_listed_duplicate_still_exists`

**Mark:** absolute since 0.24.0 - the guard's `DUPLICATES` is empty.

- A test cannot read what a command does, so it holds what can be counted: every command name across every CLI - an argparse sub-command, one registered from a loop over a literal list (three hidden `backup_restore` commands were, unseen by the first scan), a JSON command in `db.cli`'s table, and `common.cli`'s contract list. A name in two CLIs is an app's **front door** to the `common.cli` command of that name - it finishes the request from the app's `data/` and hands it on, which the guard checks in the code (`sre`'s `create-db-docker`, `move-db-docker`) - or **two jobs sharing a word**, each named (`init`, `user-level`). Anything else is a duplicate.
- **6 duplicates measured 2026-09-26, all resolved the same day** (the operator chose which stays): the inventory summary is `common`'s and the inventory workflow `reports`' (`control`'s two went); this node's clock is `db timezone` (`common`'s went); this node's status is `common self-status` (the bot states the last runs; `db`'s went); verifying a restore is `common verify-restore` (`backup_restore`'s went); and **a SQL Server RESTORE is written once**, in `common/restorestep/sqlserver.py` - `restore-latest` asks `restore-full` / `-diff` / `-log` for each step, the nightly's text byte for byte, and the extra routes went (`common restore-database`, `backup_restore`'s hidden step commands, `restore-by-id` for an SMB entry). `upgrade-config`'s `moved-commands` step rewrites a command line naming one that moved.
- A duplicate under two different names is only found by a person: one found later goes into `DUPLICATES` with the decision it waits on, and the rule is baseline again until it is resolved.
- Not a duplicate: `run-sql` and `run-sqlcmd` (a driver from this node / `sqlcmd` on the SQL Server host); `metric-batch` and `run-sql` (a batch of metric items, whose single-statement execution should share `run-sql`'s code - R11's question, not this rule's); `restore-latest` and `restore-by-id` (the chain of an SMB entry and of a script entry, both applied through the same `common` steps; `restore-workflow` runs either).

## 2. The `common.cli` contract

### R13

Every `common.cli` command takes one JSON object - inline, `@file` or `-` for stdin - never flags for the payload.

**Guard:**

- `tests/test_common_cli_json_contract.py::test_a_json_array_is_refused_by_every_command`
- `tests/test_common_cli_json_contract.py::test_the_command_list_matches_the_dispatcher`

**Mark:** absolute.

### R14

A request that carries a password is taken on stdin only, never argv.

**Guard:**

- `tests/test_common_cli_json_contract.py::test_a_stdin_only_command_refuses_the_other_forms_and_says_why`
- `tests/test_the_agent_guide_names_only_real_commands.py::test_the_guide_never_teaches_a_secret_on_the_command_line`

**Mark:** absolute.

### R15

Every `common` and `db` command answers in one envelope - `success`, `operation`, `message`, `error`, `data`, `metrics` - with nothing else on stdout, and an exit code that summarises `success`.

**Guard:**

- `tests/test_common_cli_response_shape.py::test_a_command_answers_in_the_response_envelope`
- `tests/test_common_cli_response_shape.py::test_nothing_is_printed_in_front_of_the_json`
- `tests/test_every_json_the_tool_reads_or_writes_is_described.py::test_the_envelope_is_exactly_what_the_response_builder_writes`
- `tests/test_a_common_command_works_from_its_request_alone.py::test_a_broken_configuration_is_answered_in_the_envelope_never_a_traceback`

**Mark:** absolute. An unexpected error is answered in the envelope too (`common/cli.py::main`).

### R16

**Every `common.cli` request and answer is described in the reference** (`shared_config_objects.json`): one `input_` and one `output_` entry per command, every key a parser reads is a described field, every described field is a key its module names.

**Guard:**

- `tests/test_every_json_the_tool_reads_or_writes_is_described.py::test_every_command_is_described_by_exactly_one_entry_of_each_kind`
- `tests/test_every_json_the_tool_reads_or_writes_is_described.py::test_every_key_a_request_parser_reads_is_a_described_field`
- `tests/test_every_json_the_tool_reads_or_writes_is_described.py::test_every_described_field_is_a_key_its_module_names`
- `tests/test_a_common_command_works_from_its_request_alone.py::test_every_key_a_successful_answer_carries_is_described`

**Mark:** baseline.

- **Requests: absolute.** Every key a parser reads is a described field, and every described field is a key its module names.
- **Answers: 61 commands left, of 86.** Every key a real successful answer carries must be a described field. Only a real answer shows such a key, so the check runs on the answers R09's guard already produces: 25 commands answer there with nothing configured and nothing reachable. The other 61 answer only from a live database or host, or have no complete offline request yet - named in the guard's `ANSWER_NOT_YET_SEEN`, held equal to what the runs show; a command leaves it when a run answers it.
- Found by the answer check in 0.24.0, and described: `check-references` answering `data_dir`, `list-backup-files` answering `unreadable`, `timezone` answering `config_error`.

## 3. Configuration and data

### R17

Every configuration file, and every record a person edits, is described in the reference, under its own id; no two entries describe the same fields.

**Guard:**

- `tests/test_every_json_the_tool_reads_or_writes_is_described.py::test_every_config_file_the_catalogue_names_is_described`
- `tests/test_every_json_the_tool_reads_or_writes_is_described.py::test_no_two_objects_describe_exactly_the_same_fields`
- `tests/test_the_reference_describes_every_record_a_person_edits.py::test_a_misspelled_key_on_a_whole_record_is_reported`

**Mark:** absolute.

### R18

The reference's three copies (`db_ops/common/catalogue/`, `data/` and `data/*.example.json`) are the same document.

**Guard:**

- `tests/test_shared_config_objects_reference.py::test_all_three_copies_are_the_same_bytes`

**Mark:** absolute. The copies that exist are compared (a public tree has two).

### R19

Configuration is data: a threshold, target, route, schedule or policy lives in `data/*.json`, never as a literal in code.

**Guard:** review

**Mark:** review.

### R20

A shared config object (`time_window`, `notify`, `cleanup_retention`) is parsed once, in `lib`. A per-app copy is a bug.

**Guard:**

- `tests/test_no_duplicate_definitions.py::test_no_new_definition_is_duplicated`

**Mark:** absolute.

### R21

`server_id` is the only key for a machine: one machine, one id; group and join on it, never on an ip.

**Guard:**

- `tests/test_a_machine_has_one_server_id.py::test_every_record_has_a_server_id_and_none_is_used_twice`
- `tests/test_instance_add.py::test_a_duplicate_server_id_is_refused_rather_than_overwritten`
- `tests/test_metric_collection_parallelism.py::test_two_metrics_of_one_server_are_never_in_flight_at_the_same_time`

**Mark:** absolute. Absolute for the inventory and the registrar (guarded since 0.24.0). *Never joining on an ip* is read in review: no test can see every join.

### R22

Every runtime-store table has its keys - primary, foreign, unique. A configuration row is switched off, never deleted.

**Guard:**

- `tests/test_store_tables_have_their_keys.py::test_every_store_table_has_a_primary_key`
- `tests/test_store_tables_have_their_keys.py::test_no_code_deletes_a_configuration_row`
- `tests/test_archive_tables_take_their_keys.py::test_a_new_store_declares_both_keys`

**Mark:** absolute - for what the code creates and deletes. The guard's first run (0.24.0) found `job_runs_history` and `metric_results_archive` keyless; a store made since declares both, keyed by the id each row kept (`log_id`, `result_id`).

- **Left on a store made before 0.24.0:** both archives stay keyless until `python -m db_ops.db.cli archive-keys --apply` runs on it. `CREATE TABLE IF NOT EXISTS` changes nothing that exists, and no app does it on starting: a long-running store's archive is millions of rows, adding the key reads every one, and one duplicated id would fail it. The command reports first - rows, missing ids, duplicate ids - and keys only a table it found clean. See [`01_runtime_store.md`](./01_runtime_store.md).

### R23

"Not configured" is a state, not a failure: every shipped app command runs on an empty install and says what is missing.

**Guard:**

- `tests/test_every_app_command_runs_on_an_unconfigured_install.py::test_each_job_exits_cleanly_with_nothing_configured`

**Mark:** absolute.

## 4. Behaviour every engine and app keeps

### R24

SQL Server is reached in `master` unless a database is named; a target's `db_name` / `service_name` is a label, never a database.

**Guard:**

- `tests/test_common_db_connect.py::test_sqlserver_connects_to_master_even_if_the_inventory_names_something_else`
- `tests/test_a_sql_task_connects_first_and_asks_the_server_on_failure.py::test_no_database_on_sql_server_is_master`

**Mark:** absolute.

### R25

`cmd_access.method: "local"` pointed at another machine is refused - it would report this machine under that one's name.

**Guard:**

- `tests/test_local_method_guard.py::test_local_pointed_at_another_machine_is_refused`

**Mark:** absolute.

### R26

One bad target never aborts a scan: it is recorded on that target and the rest run.

**Guard:**

- `tests/test_metric_targets.py::test_a_broken_cmd_access_does_not_take_the_other_targets_down`

**Mark:** absolute.

### R27

A value from a chat or a config file is bound, never pasted into SQL; where it must be text (a SQL*Plus substitution) it is checked first.

**Guard:**

- `tests/test_sql_task_parameters.py::test_the_value_is_bound_and_never_written_into_the_sql`
- `tests/test_an_oracle_or_postgresql_task_binds_its_parameters_by_name.py::test_a_colon_name_is_bound_and_an_ampersand_name_is_substituted_on_a_direct_oracle`
- `tests/test_legacy_oracle.py::test_a_parameter_value_that_could_change_the_statement_is_refused`

**Mark:** absolute.

### R28

Telegram severity marks are applied once, centrally, from the header line; producers never tag, and a message is split under Telegram's 4096 characters.

**Guard:**

- `tests/test_telegram_severity_emoji.py::test_severity_is_read_from_the_header_not_the_body`
- `tests/test_telegram_severity_emoji.py::test_an_over_long_message_is_split_and_the_tag_leads_the_first_part`

**Mark:** absolute.

### R29

The Telegram queue marks, acts on and updates one row per call - never a batch.

**Guard:**

- `tests/test_the_telegram_queue_moves_one_row_at_a_time.py::test_each_message_is_marked_sent_before_the_next_one_is_touched`

**Mark:** absolute. Guarded since 0.24.0: the store is read at the moment each message goes out.

### R30

A scheduled `async` run never repeats a job another run has started or finished; it claims each unit of work.

**Guard:**

- `tests/test_an_async_run_never_repeats_a_job_another_run_finished.py::test_a_scheduled_backup_does_not_repeat_a_job_another_run_finished`
- `tests/test_an_async_run_never_repeats_a_job_another_run_finished.py::test_a_scheduled_sql_scan_does_not_repeat_a_task_another_scan_finished`

**Mark:** absolute.

## 5. Code, tests and documentation

### R31

A file Linux executes has LF line endings.

**Guard:**

- `tests/test_what_linux_runs_has_lf_line_endings.py::test_a_file_linux_runs_has_no_crlf`

**Mark:** absolute.

### R32

The test suite is offline: no database, no Telegram, no driver imported at module scope.

**Guard:**

- `tests/test_suite_needs_no_database_driver.py::test_no_test_module_imports_a_database_driver_at_module_scope`

**Mark:** absolute.

### R33

Every component has a doc, and every doc has a component.

**Guard:**

- `tests/test_docs_cover_every_component.py::test_every_component_has_a_doc`
- `tests/test_docs_cover_every_component.py::test_every_doc_belongs_to_a_component`

**Mark:** absolute.

### R34

A change to a component updates its doc in the same change, and a change a user would notice has its line in `CHANGELOG.md` `[Unreleased]` as it lands.

**Guard:** review

**Mark:** review.

### R35

The agent guide (`AGENTS.md`) names only commands that exist, and never teaches a secret on a command line.

**Guard:**

- `tests/test_the_agent_guide_names_only_real_commands.py::test_every_subcommand_the_guide_names_exists`

**Mark:** absolute.

### R36

Everything written is English - code, docs, tests, commit messages, and the `note` / `description` fields in `data/*.json`.

**Guard:**

- `tests/test_everything_written_is_english.py::test_python_comments_and_docstrings_are_english`
- `tests/test_everything_written_is_english.py::test_markdown_is_english`
- `tests/test_everything_written_is_english.py::test_the_notes_in_json_are_english`

**Mark:** absolute. Absolute for prose (guarded since 0.24.0): comments, docstrings, Markdown, the notes in JSON. String data a test feeds through an encoding is data; an operator's words are quoted translated; `audits/` is out of scope - an old audit is never edited.

### R37

Comments say why, not what; tests read as prose, a docstring saying why the behaviour matters and test names that are sentences.

**Guard:** review

**Mark:** review.

---

## Where the rules are kept, and what is not here

- **This page is the list** of the software's rules. When a rule changes, change it here and in its
  guard in the same change. A page elsewhere that restates a rule links here by number.
- **Not here: how the software is published.** What may leave the maintainers' tree - no real
  identifier, no secret, a named ship list - and the commit and release gates are the maintainers'
  own process, kept in their internal release checklist. They are rules for the people publishing,
  not properties of dbabrain.
- **Open** (recorded 2026-09-25, brought up to date 2026-09-26): whether R03 covers `db`, which
  imports `common` and is not an app; the remaining baselines of R16 (61 commands whose answers no
  offline run produces).
  Closed in 0.24.0: every rule has a guard or is marked review; R03, R04, R06, R07, R09, R10, R41, R42 and R43 are absolute.
