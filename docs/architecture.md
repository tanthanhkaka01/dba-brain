# Architecture

Fourteen components on two shared layers, with four rules about who may call whom. Every rule has
a test beside it, which is the only reason a stated architecture is worth reading — a diagram
describes what someone intended, a guard test describes what is true this morning.

> **Naming, while the project is being renamed.** Module paths are still `db_ops.*`.
> <!-- TODO(rename): update the module paths on this page once the rename lands. -->

---

## 1. The components

Twelve apps and two shared layers. Each app is one directory under `db_ops/` with its own `cli.py`,
and each has exactly one reference page under `docs/`.

| ORD | Component | Package | What it is |
| :---: | --- | --- | --- |
| [01](./01_runtime_store.md) | Runtime store | `db_ops/db` | The database the toolkit keeps its own data in: job runs, measurements, reports, the delivery queue, restore history, and the configuration mirror. Also holds the row shapes the store persists. |
| [02](./02_logging_engine.md) | Logging engine | `db_ops/logging_ops` | One logger per app: files under `logs/`, rows in `job_runs`, and the notify level that decides which chat hears about it. |
| [03](./03_app_command_daemon.md) | App command daemon | `db_ops/jobs` | The scheduler. Reads `app_commands.json` and runs every other app on its own interval, forwarding the secret passphrase to each child process. |
| [04](./04_metrics_engine.md) | Metrics engine | `db_ops/metrics` | Collects every metric from every enabled target and writes the results. Reports and SLA both read what it produces. |
| [05](./05_sql_task_runner.md) | SQL task runner | `db_ops/sql_tasks` | Runs the scheduled SQL scripts against their configured targets and delivers the output. |
| [06](./06_reports_app.md) | Reports | `db_ops/reports` | Turns collected measurements into the scheduled reports and the inventory pages. |
| [07](./07_telegram_app.md) | Telegram | `db_ops/telegram` | Delivers the outgoing queue one message at a time, and executes the commands people send back. |
| [08](./08_backup_restore_app.md) | Backup / restore | `db_ops/backup_restore` | Runs backups, proves them by restoring, and records what was verified and when. |
| [09](./09_sla_slo_compliance_app.md) | SLA / SLO compliance | `db_ops/sla` | Validates the objectives against collected measurements and reports the error budget left on each. |
| [10](./10_sre_app.md) | SRE | `db_ops/sre` | Provisions and moves the disposable lab databases that drills and rehearsals run against. |
| [11](./11_control_app.md) | Control | `db_ops/control` | Builds and deploys the toolkit to another node, and watches the toolkit itself. |
| [12](./12_webhost_app.md) | Web host | `db_ops/webhost` | Serves the rendered reports over HTTP and hosts the console. |
| [13](./13_common.md) | **Common** — shared operations | `db_ops/common` | Reaching a host, running SQL, moving a file, handling a secret. Invoked as a CLI, never imported. |
| [14](./14_lib.md) | **Lib** — shared rules | `db_ops/lib` | Values and rules that are pure functions of their arguments. Imported everywhere, runs nothing. |

Plus three root modules that belong to no component: `db_ops/config.py` (configuration parsing),
`db_ops/levels.py` (the severity vocabulary), and `db_ops/cli.py` (the composition root for the one
command that spans two apps).

**The list is closed.** Anything new is one of the fourteen or it is a fifteenth ORD with its own
directory, its own CLI and its own doc. There is no component that quietly is neither.

> **Every component has a doc, and every doc has a component** — both directions, enforced by
> `tests/test_docs_cover_every_component.py`. A component is not finished until its doc exists.
>
> That guard exists because of `lib`. It was split out of `common`, grew to 46 modules and roughly
> 6,500 lines — the second-largest thing in the tree — and stayed undocumented for two days with a
> fully green suite, because nothing connected the two directories. The reverse direction matters
> as much: a doc whose component was deleted reads as current reference and describes nothing.

---

## 2. The two shared layers are opposites

> **`common` (ORD 13) may not be imported — it is only ever run as a CLI.**
> **`lib` (ORD 14) may not run a CLI — it is only ever imported.**

The split is by what a thing **is**, not by who calls it.

`common` **does** things. Reaching a host, running SQL, rotating a password: those are operations,
and an operation can be a process. Putting it across a process boundary is what forces the request
to be a complete, inspectable JSON object and the answer to be a response envelope — which is why
the same call works for a scheduled run and for a one-off recovery against a machine that is in no
inventory at all.

`lib` **decides** things from its arguments. `policy_engine` classifies every measurement — tens of
thousands of rows for one index inventory — and `time_window` is consulted on every scheduler tick.
A process per call there is not a slower design, it is a broken one. And a class does not come back
from a subprocess.

---

## 3. The rules, and the test for each

### An app never imports another app

`tests/test_import_boundaries.py::test_an_app_never_imports_another_app`

Apps talk across a process boundary — the module CLIs — or through `common`. An app that needs
another app's *configuration* asks that app's CLI for it; it never reads the other app's files.

### A shared layer never imports an app

`tests/test_import_boundaries.py::test_a_shared_layer_never_imports_an_app`

`common`, `db`, `logging_ops` and `lib` sit *below* every app, so an import pointing back up
inverts the layering. This one is checked because it is invisible at run time: Python resolves the
cycle happily, the tests pass, and the layering is gone. One audit found seven such edges, each
added by someone who needed a single function and reached for the nearest copy.

`ALLOWED_UPWARD_IMPORTS` is the escape hatch, and it is deliberately awkward to extend: adding an
entry means writing down why an app-independent operation could not be expressed without an app.
The default answer is to move the shared thing **down**. Two more tests keep the list honest —
one deletes an allowance whose module is gone, one deletes an allowance nobody uses any more.

### An app does not import `common`

`tests/test_app_common_imports.py::test_an_app_imports_no_common_module_outside_the_baseline`

It runs `python -m db_ops.common.cli <command> '<json>'` instead. The baseline is a shrinking
allow-list with a per-entry reason, and a second test fails when an entry stops being used, so the
list can only get smaller.

Three exceptions are named at the guard rather than left to be discovered:

- **`common.data_sources`** — the single reader of the `data/` folder. Routing a configuration read
  through a subprocess would buy nothing and cost every caller a process; one reader of the data
  folder is worth more than one fewer exception.
- **`control`** — the deploy tool. It builds the image and the bundle, so it necessarily knows the
  layout of everything it ships.
- **`metrics`, for the four modules it executes through.**

### `common` starts no processes

`tests/test_import_boundaries.py::test_common_never_launches_a_db_ops_cli`

A shared library that shells out to a CLI has taken a dependency on everything that CLI imports —
invisible to an import checker and just as binding. Notification routing is the worked example: the
shared layer used to run the Telegram app's CLI to read its settings. Now each app carries the
small client that makes that call, and what stayed shared is the **pure rule** for combining the
answer with an entry's `notify` block. The transport is duplicated per app; the rules are not.

### `common` looks nothing up

Everything it needs arrives in the request. A lookup inside the operations layer would make its
behaviour depend on files the caller cannot see — and it is what would stop the same call working
against a machine that is in no inventory, which is exactly the machine you need it for at 3am.

### `lib` imports nothing from `db_ops`, and runs no CLI

`tests/test_lib_is_pure.py::test_a_lib_module_imports_nothing_from_db_ops`
`tests/test_lib_is_pure.py::test_a_lib_module_does_not_run_a_cli`

The failure mode is undramatic: someone needs one helper, adds one import, and the layer every
component imports in-process quietly starts pulling configuration, a store connection, or an app
behind it. Then "apps do not import `common`" has been routed around rather than kept.

One exception, named at the guard: `notify` reads the notify-level vocabulary from
`db_ops.config`, lazily and failing open, because that vocabulary is data an operator adds by
registering a chat. `db_ops.config` is a root module — configuration parsing, imported by
everything, owning nothing — so this does not point the layer at anything above it. Any *second*
exception should be argued as hard as this one was.

---

## 4. Two things every layer may touch

### The runtime store

ORD 01 owns it, it is the toolkit's own bookkeeping, and its connection is declared in
`data/store_config.json` rather than resolved from an inventory — so there is nothing for the
operations layer to resolve and nothing an operator could point at the wrong host. An app imports
`db_ops.db`, calls a store method or writes SQL against it, and does not go through a CLI to do so.

The rule that matters is about the *monitored* estate: a customer database, a host over SSH, a
messaging endpoint. Those an app never reaches itself.

**The store describes itself, too.** A caller used to be able to say only "config.json" and let the
other side go and read it — so the one thing every app writes through was the one thing that could
not be stated in a request. Now a store declaration is a value: backend, host, database, schema,
user and the already-resolved password, travelling in the payload like anything else. Two things
follow, and both were problems before: a test can build one and point a subprocess at a temporary
store, and the password is in the payload, which is why every consumer takes its request on
**stdin** and never as an argv word.

### The row shapes

`tests/test_import_boundaries.py::test_a_shape_module_imports_nothing_at_all`

The four shapes the store persists live beside the store that writes them, and import nothing at
all — not even from `lib`. A shape with a dependency stops being a shape.

---

## 5. The shape of a request

Every `common` command takes **one JSON object** — inline, from a file, or on stdin — with the same
shape as the configuration files, and answers with a response envelope
(`success` / `operation` / `message` / `error` / `data` / `metrics`).

Never ad-hoc flags for the payload. That is what lets a configuration file, a chat command and a
shell caller pass the same request through untranslated, and it is what makes a failing scheduled
run reproducible by hand: the request is a value you can copy.

```
app  → telegram.cli route <level>                    # another app's config, via that app's CLI
app  → db.cli queue-telegram-message -           # complete request, on stdin
         {"store": {...}, "chat_id": ..., "text": ..., "level": ..., "phase": ...}
           → the store CLI connects with what it was handed, and inserts
```

The corollary, and it is a house rule rather than a nicety: **if you had to write a throwaway
script to do something, it belongs in `common` with a CLI command instead**, in the same change. A
scratch script answers once and takes its edge cases with it; the next person rewrites it and gets
a different answer. Several commands in this tree exist precisely because the scratch versions got
the port, the protocol or the parameter binding wrong.

---

## 6. How a run actually flows

```text
scheduler (db_ops/jobs) — reads data/app_commands.json
  ├─ metrics.cli collect
  │     resolves targets from data/db_instances.json + users.json
  │     runs common.cli for each measurement           → writes metric_results
  ├─ sql_tasks.runner                                  → writes sql_task_runs, queues output
  ├─ reports.cli run-scheduled
  │     reads metric_results, applies reports_config   → writes runtime/reports, queues findings
  ├─ sla.cli validate
  │     reads metric_results, applies sla_policies     → writes sla_runs / sla_results
  ├─ backup_restore.cli workflow                       → writes backup_restore_history
  └─ telegram.cli run-workflow
        delivers ONE queued message, processes ONE incoming command, per pass
```

Two boundaries in that picture are load-bearing:

- **Producing a finding and delivering it are separate steps.** Metrics collect facts and never
  send a message; reports create content and queue it; the Telegram app owns delivery. Turn
  delivery off and everything upstream still works — which is why the toolkit sending nothing
  anywhere is a setting and not a promise.
- **One row at a time.** The delivery queue and the command processor deliberately mark, act on,
  and update a single row per call — never fetch a batch, act, then update statuses in bulk. See
  [`docs/07_telegram_app.md`](./07_telegram_app.md) for why.

---

## 7. Where the configuration comes from

Not from where the code sits. `DB_OPS_HOME`, then the working directory if it holds `data/` or
`config.json`, then the package location — guarded by `tests/test_tool_root_resolution.py`, with
`tests/test_no_self_derived_project_root.py` refusing the `Path(__file__).parents[n]` idiom
everywhere except the one module allowed to ask where its own code lives.

Full detail in [`docs/configuration.md`](./configuration.md).
