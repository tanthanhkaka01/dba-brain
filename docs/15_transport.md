# 15. Transport (the one client of `common.cli` and `db.cli`)

`db_ops/transport/` is **the one place that starts `common.cli` or `db.cli`** and hands back what it
answered. It decides nothing and performs no operation of its own: the command is built in `lib`,
the answer is read in `lib`, and `transport` runs the process between the two.

> **`lib` builds the command. `transport` runs it. `lib` reads the answer.**

It is the smallest component in the tree, and that is the point of it. Everything that has to be
*decided* about a call - what the command line is, what goes on stdin, how the bytes are decoded,
what the envelope means - is a pure function and lives in `lib`, where it can be tested without a
process. What is left is the part that cannot be pure: starting the process.

---

## Why it exists (0.24.0, rules R07)

Until 0.24.0 the two clients every app uses to reach another component - `lib/common_cli.py` (for
`common.cli` and `db.cli`) and `lib/telegram_route.py` (for the Telegram app's routing) - started
the process themselves. `lib` is the layer everything imports and it holds values and rules; a
module there that starts processes is an operation hiding in it (R07). The operator's direction:
*`lib` does not need to run a CLI - the app runs it, and imports `lib`.* And, on the same day:
*a `db_ops` CLI is not something to run casually - what components share goes into `lib` or
`common`* (R41, R42). So `transport` starts exactly two things, the two CLIs every component is
meant to reach: `common.cli` (operations) and `db.cli` (the runtime store's commands).

The places the launch could have gone, and why each was refused:

| Where | Why not |
| --- | --- |
| `lib/process.py` | the same breach under another file name - `lib` would still start processes |
| `common/command_runner.py` | every app would import `common` (R03), and `common` would start a process (R05) |
| a copy in each app | the same function in six places (R11) - how six copies of one transport once drifted into three behaviours |
| each call site calling `subprocess` itself | 24 call sites, each repeating the error handling a caller needs, and each one a chance to get the stdin/argv rule wrong |

So the launch got a layer of its own, named for the one thing it does, with its own rules and
guards. It is not an escape hatch: it starts `common.cli` and `db.cli` and nothing else, it imports
only `lib`, and nothing below it may import it.

## Where it sits

```
          python -m db_ops.common.cli | db_ops.db.cli   (JSON on stdin, envelope on stdout)
                                   ▲
                                   │ starts
  app ──import──▶ transport ──import──▶ lib ◀──import── common
   │                                    ▲
   └──────────────import────────────────┘      (and db)
```

| Layer | May import | May not |
| --- | --- | --- |
| an app | `lib`, `transport`, `db` | `common` (R03), another app (R01) |
| `db` | `lib`, `transport` | an app |
| `transport` | `lib`, the standard library | `common`, `db`, any app (R39) |
| `common` | `lib` | `transport`, `db`, any app (R04) |
| `lib` | `lib` | anything else (R06), and it starts no process (R07) |

## The rules (in [`rules.md`](./rules.md))

- **R38 - `common.cli` and `db.cli` are started only through `transport`.** No other code starts
  them - not an app, not `lib`, not `db`.
- **R39 - `transport` imports only `lib`; `lib` and `common` never import `transport`.**
- **R40 - Inside `transport`, one function starts a process**: `transport/process.py::execute`. It
  is the only `subprocess` in the package, so there is one place to read to know how every
  cross-component call behaves.

## What is `transport`'s, and what is not

| Starting | Whose | Why |
| --- | --- | --- |
| `common.cli` / `db.cli` with a JSON request | **`transport.common_cli`** | the API every component reaches operations and the store's commands through |
| another app's CLI from an app's code (the four there were - `reports` -> `metrics.cli collect`, `telegram` -> `reports.cli`, `telegram` -> `sql_tasks.cli list-tasks`, `control` -> `sre.cli create-db-docker` - are gone since 0.24.0) | **nobody - refused (R42)** | what two apps share is a rule (`lib`) or an operation (`common`), not one app driving another's CLI |
| the Telegram routing a notification needs (`route`, `groups`) | **no process at all** - `lib.telegram_route` | it is a function of the configuration (`lib.config`), so it is read in-process; it used to cost a process per level |
| the daemon running `app_commands.json` | **not transport** - `jobs` (named exception to R42) | the command line is configuration the scheduler executes, not a call one component's code makes to another |
| the bot running its configured command templates | **not transport** - `telegram` (named exception to R42) | the same: executing a configured line is the bot's work |
| a component re-launching its own CLI (`sre` steps, the bot's detached exit) | **not transport** - the component | not another component |
| an operator's script (a Python metric collector, a SQL task's Python input) | **not transport** - the app | not a component at all |
| a host tool - `ssh`, `sqlcmd`, `docker`, `ansible` | **not transport** - `common` (R10) | reaching a host is an operation, and operations are `common`'s |

## The pieces

| Module | Holds |
| --- | --- |
| `lib/common_cli.py` | `CommandSpec` - the executable, the arguments, the stdin bytes, the deadline, whether stderr streams; `build_command()`; `read_answer()` and `CommonCliError` - the envelope read back. Pure |
| `transport/process.py` | `execute(spec)` -> `ProcessResult(returncode, stdout, stderr, error)`. The one `subprocess.run`. A process that could not start or ran past its deadline comes back with `error` set, never as an exception the caller has to know the type of |
| `transport/common_cli.py` | `run()`, `run_allowing_failure()`, `spawn()` - the same names and behaviour the `lib` module had, so a caller changed one import line |
| `lib/telegram_route.py` | `telegram_route()`, `telegram_groups()`, `chat_id_for_level()`, `clear_cache()` - routing read from the configuration in-process, cached for a minute. No process: nothing in it is `transport`'s any more |

## Decisions carried over

Each was a finding before it was a rule, and each stays exactly where the reader of the call is:

- **The payload goes in on stdin, never argv** (R14). Requests carry resolved passwords, and argv
  is readable by anyone who can list processes.
- **No deadline by default.** A restore can run for hours; the app command that schedules it carries
  the window, and the daemon owns it. `timeout_seconds` is for the caller that knows its own.
- **stderr is captured unless the caller streams it** - a lab build shows its progress live.
- **Bytes, pinned to UTF-8, strict going out and forgiving coming back.** A request that cannot be
  encoded is the caller's bug; one stray byte in a child's output must cost a character, not the
  answer (the em dash that was once reported at a position the script did not have).
- **Two readers.** `run()` raises when the command reports failure; `run_allowing_failure()` hands
  it back as data, because a failed backup is a recorded outcome, not a stop.

## Guards

- `tests/test_transport_is_the_only_client_of_common_and_db.py` - R38, R39, R40.
- `tests/test_no_app_runs_another_apps_cli.py` - R42.
- `tests/test_lib_is_pure.py::test_a_lib_module_does_not_run_a_cli` - R07, with no exception left.
- `tests/test_import_boundaries.py` - `transport` is a shared layer and imports no app (R02).
- `tests/test_docs_cover_every_component.py` - this page.
