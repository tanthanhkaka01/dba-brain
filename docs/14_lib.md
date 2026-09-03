# 14. Lib (pure helpers)

`db_ops/lib/` is the only layer every other component may import **in-process**. 49 modules,
~8,000 lines. It holds values, vocabularies and rules about values — never operations.

It is the counterpart to [`13_common.md`](./13_common.md). The two split the shared surface between
them on one question, and the question is not "who calls it":

> Does this thing **do** something — reach a host, run SQL, move a file — or does it **decide**
> something from arguments it was handed?

An operation goes through the `common` CLI as a subprocess. A value, or a rule about values, is
imported.

---

## Why the layer exists

The rule "an app does not import `common`, it calls the `common` CLI" is right for operations,
because an operation can be a process. It cannot apply here, and the reasons are mechanical rather
than aesthetic:

* **A class does not come back from a subprocess.** `metrics` builds `MetricResult` objects;
  `NotifyConfig` and `ParsedTimeWindow` are parsed once per config load and passed around for the
  life of the run. JSON round-tripping them would mean rebuilding them at every boundary.
* **Some of it runs per row.** `policy_engine` classifies every metric row — roughly 29,000 for a
  single database's index inventory — and `time_window` is consulted on every daemon tick. A
  process per call is not a slower design, it is a broken one.

So the split is by what a thing *is*, not by who needs it.

---

## The two rules, and they are a mirror

> **`common` may not be imported — it is only ever run as a CLI.**
> **`lib` may not run a CLI — it is only ever imported.**

The two shared layers are opposites on purpose. `common` **does** things, so it is reachable only
across a process boundary, where the request is a JSON object and the answer is the envelope. `lib`
**decides** things from its arguments, so it is reachable only in-process.

Crossing either direction fails differently: importing `common` routes around the CLI contract,
while spawning a CLI from `lib` puts an operation inside the layer that everything imports.

### Rule A — a `lib` module imports nothing from `db_ops`

Not `common`, not `db`, not an app, not `config.json`. Everything it needs arrives as an argument.

Guarded by **`tests/test_lib_is_pure.py`**, which walks the AST of every module under `db_ops/lib/`
including `restore/`. The guard is three tests, not one: the import check, plus two that keep the
allowance list honest — every allowance must still name a real module, and every allowance must
still be used. An allowance that has been fixed fails the suite rather than lingering.

### The two allowances, in full

```python
ALLOWED_DB_OPS_IMPORTS = {
    "notify.py":         "db_ops.config",
    "telegram_route.py": "db_ops.config",
}
```

Both are the same shape: a **lazy, last-resort read of a root module**, failing open.

* `notify.py` reads the configured notify-level vocabulary, because that vocabulary is data an
  operator adds by registering a Telegram group — it is not knowable at import time.
* `telegram_route.py` falls back to the level → chat map in config when the Telegram app's CLI
  cannot be reached, rather than dropping the message.

`db_ops.config` owns nothing and is imported by everything, so neither allowance points the layer
at anything above it. Both are written down at the guard, not just in the module.

### Rule B — a `lib` module does not launch a CLI

Guarded by the same file, on the AST rather than a text search: roughly a third of the package
mentions `subprocess` in a docstring explaining why it is **not** one, and a grep counts those.

Two modules currently do, and they are **the same two that hold the import allowances above** —
they break the layer in both directions:

```python
KNOWN_CLI_LAUNCHERS = {
    "common_cli.py":     "the one client for db_ops.common.cli (and db.cli via module=)",
    "telegram_route.py": "falls back to db_ops.telegram.cli for the level -> chat map",
}
```

Both are **transport clients** — their job is to spawn another component's CLI and read the JSON
back, which is doing something rather than deciding something. They are recorded rather than moved
because the resolution is structural: `common_cli` is the transport *every* app uses to reach
`common`, so wherever it lives, some layer has to spawn the process. Which layer owns the transport
is an open question, recorded as violation **V7** against the architecture rules and still open. The
set may shrink and may not grow.

---

## Where a new thing goes

Three mechanisms, and the choice is not a matter of taste:

| The thing | Goes to |
| --- | --- |
| a **value or a rule** — pure, takes its inputs as arguments | `db_ops/lib/` |
| a **read of `data/`** | `db_ops/common/data_sources/` — the one reader |
| an **operation** — touches a database, host, file, or the network | a `common` CLI command |

A module that does two of these is doing two jobs and gets split. That split is not hypothetical:
`backup_policy`, `capacity_forecast`, `report_archive` and `inventory_render` each arrived as one
module doing both, and in every case the pure half was the only half the apps ever imported. The
read went to `data_sources`, the judging stayed here **with the document as a required argument** —
no default that silently reaches for this repo's data folder.

---

## The modules

### Vocabulary — a name spelled once

The smallest and most-copied category. Each one exists because the same string was being spelled
differently in two places.

| Module | What it names |
| --- | --- |
| `backup_kinds.py` | the three kinds of backup |
| `backup_level.py` | what `full`/`diff`/`log` is called on each engine |
| `cmd_access.py` | the `cmd_access` vocabulary — how to reach a *host* |
| `sql_access.py` | the `sql_access` vocabulary — how to reach a *database* |
| `target_profile.py` | what a target **is** — engine, engine version, OS version, runtime — and which tool that implies |
| `connection_spec.py` | one database connection stated **in full**, so nothing has to be looked up |
| `task_output.py` | what a scheduled SQL task does with its result set |
| `instance_bundle.py` | what a SQL Server instance-metadata bundle is — layout, two phases, order |
| `ssh_errors.py` | what can go wrong reaching a host over SSH, as four names |
| `target_flags.py` | per-target on/off flags |

### Reading a value that may not be what it claims

| Module | Purpose |
| --- | --- |
| `coerce.py` | `as_bool` / `as_int` / `as_optional_int` / `as_float` / `as_text` / `as_utc_datetime` |
| `rows.py` | `row_value` / `row_text` — one column out of a store row that may not have it |
| `json_io.py` | reading a `data/*.json` the one way the whole tool reads them |
| `text_format.py` | one-line text helpers more than one component must agree on |

### Judging — the rules, as pure functions

This is where the decisions live. Each takes the document or the rows as an argument and returns a
verdict; none of them reads a file.

| Module | Question it answers |
| --- | --- |
| `policy_engine.py` | how does one metric row classify — the per-row hot path |
| `backup_policy.py` | is each database actually protected, per database and per backup type |
| `backupfiles_retention.py` | which backups the retention window no longer covers |
| `capacity_forecast.py` | when does this run out |
| `state_transition.py` | does a recurring check have anything *new* to say |
| `event_policy.py` | which events matter |
| `metric_score.py` | how a set of metric rows scores for one status — the fleet ordering rule |
| `health_model.py` | what is true about a target *now*, shared by every page that claims to say so |
| `notify_route.py` | how an entry's `notify` block narrows a node's route |
| `interval_rates.py` | reading structured fields back out of a collector's message text, and differencing two stored samples of a cumulative counter into a rate |
| `record_form.py` | how one config record becomes editable fields, and how those fields become the record again |
| `log_tail.py` | reading a log file from the end, a page at a time, and what one line means |
| `network_policy.py` | can this host's container networks take a monitored database off the map. See below |

#### `network_policy.py` — the failure that looks like nothing

Docker's default address pool is `172.17.0.0/16`–`172.31.0.0/16`, and this estate routes real
databases inside it. When a bridge claims one of those ranges the **host route table** starts
sending that traffic into the bridge, so the database vanishes from that one host while staying
reachable from everywhere else. Every symptom is an ordinary connect timeout.

It has cost three outages on the same SQL Server — 2026-08-05, 2026-08-14, 2026-08-26 — and each
was diagnosed by hand. The answer to the first two was *pinning*, in three places that all still
matter: `db_ops.sre.docker_db.models.lab_network_subnet` (every generated lab compose file),
`docker-compose.runtime.yml` (db_ops's own network), and `db_ops/sre/host_config/docker-daemon.json`
(the host's own allocation). Pinning works — but only where somebody applied it, which is why the
third happened on a worker VM built after the second.

So this module does not pin anything. It **detects**, from what a host's networks actually are:

| Finding | Means |
| --- | --- |
| `HIJACK` | a container network holds a monitored address *now* — the outage, in progress |
| `OVERLAP` | it overlaps a routed range with nothing monitored inside yet — the next instance there vanishes on arrival |
| `UNCONFINED` | it is outside every declared container range — Docker chose the range, so this host's pool is unpinned |

`UNCONFINED` is the one that pays for the module: it fires on a host where nothing is broken yet,
which is the only moment the fix is free. What it checks against is
[`data/network_reservations.json`](../data/network_reservations.json) — the estate's routed ranges
as data, not literals — and `db_ops.control.cli worker-status` is what runs it.

### Config objects parsed once

`notify.py` and `time_window.py` are the two shared config blocks described in
[`13_common.md`](./13_common.md). They are parsed **once** per config load and passed around as
objects. A per-app copy of either is a bug — the reason they are here rather than in `common` is
exactly the class-does-not-survive-a-subprocess point above.

* `notify` — `logging_on_run` / `alert_on_error` → Telegram level → chat.
* `time_window` — `repeat_interval`, `timeout`, allowed hours. Consulted on every daemon tick.

### Rendering and formatting

| Module | Purpose |
| --- | --- |
| `result_format.py` | render one result set the way the caller needs to read it |
| `xlsx_export.py` | dependency-free XLSX writer for a single result-set sheet |
| `xlsx_import.py` | read one sheet out of an XLSX, build a table on any engine db_ops knows |
| `delimited_import.py` | the same, for tab / comma / semicolon / pipe text |
| `inventory_render.py` | merge a health overlay into the canonical inventory and render it. Hides nothing by default — see below |
| `listing.py` | what a `/spbot_list_*` reply shows |
| `report_archive.py` | naming and daily archiving of published reports — path in, path out |
| `telegram_text.py` | fit a body into Telegram's limit **by splitting, never by cutting** |
| `telegram_severity.py` | the severity emoji, applied once at the send layer |
| `powershell.py` | quoting, encoding, and the `Invoke-Command` wrapper |
| `sql_text.py` | SQL text and result limits — the parts of running a query that are not the running |

`telegram_text` and `telegram_severity` are order-dependent and the order is load-bearing:
splitting must happen **before** decoration, because `telegram_severity` tells a first chunk from a
continuation by the `[part i/n]` marker that the splitter writes.

### Clients to another component's CLI

Two modules are the in-process face of a subprocess boundary. They are pure in the sense the guard
means — they build a request and read a response — and they are the only two.

* **`common_cli.py`** — the one client for `db_ops.common.cli`.
* **`telegram_route.py`** — this app's client for the Telegram app's routing commands.

### Infrastructure

| Module | Purpose |
| --- | --- |
| `response.py` | the one response shape every `common` CLI command returns |
| `paths.py` | where the tool is on disk, and — separately — where its configuration is. See below |
| `shell.py` | cross-platform shell helpers |
| `secret_text.py` | encrypted secret-text storage, and `set_secret_everywhere` — the one writer that updates **both** the encrypted store and the plaintext source the deploy regenerates it from |
| `web_auth.py` | password hashing and session tokens for the web console |
| `data_files.py` | what is in `data/`, and how each file moves between the master and the worker — the list every transfer consults first. See below |
| `config_bundle.py` | what a portable configuration bundle *is* — one JSON file that carries a whole estate to a machine that has never seen this project. See below |

#### `data_files.py` — the list every transfer reads first

Four places in this tree answered "which files are configuration", and none of them was the whole
answer: `config_catalog.json` (what syncs into the store), `NOT_SYNCED` in a *test file* (what
deliberately does not), `REQUIRED_IN_BUNDLE` in `control.deploy` (what a bundle must carry), and —
the one that cost something — `sftp.listdir` in `control.worker_data`, which is not a list at all.

`data/data_files.json` is now the one that decides, and the rule it states is: **a file that is not
in the manifest does not travel, in either direction.** The full table of `transfer` values, and
the defect that made it necessary, are in
[`docs/11_control_app.md`](./11_control_app.md#datadata_filesjson--the-list-every-transfer-reads-first).

What belongs here rather than in `control`: the parsing and the queries — `pushed_names`,
`pullable_names`, `local_only_names`, `required_in_bundle` — because they are a function of the
manifest and nothing else. What stays in `control.worker_data`: the merge *rules*, which key
identifies a record and which leaves the worker owns, beside the code that applies them. The
manifest holds only the decision that a file merges at all, and `tests/test_data_files_manifest.py`
fails if the two ever describe different sets.

Refused, never defaulted. The default anybody reaches for on a malformed manifest is "do not move
it", and that reads as a working deploy that quietly ships less — which is the failure this module
exists to make impossible.

#### `config_bundle.py` — one estate, one file

The sentence it exists for is the acceptance test: on a machine that has never seen this project,
`pip install dbabrain` then `db-ops import-data <bundle>` leaves the tool running **identically**
to the machine the bundle came from. `db-ops export-data` writes the file.

**The file list is derived, never enumerated here.** `data/config_catalog.json` is already the
allow-list that decides what counts as configuration — `db_ops.db.config_sync` reads the same file
to decide what may enter the runtime store — so this module walks it. A config file added next
month and catalogued crosses with no edit here, and one the catalog does not name does not cross.
That is what keeps `database-inventory.json` (generated output, rebuilt on the new host against
its own estate) and `sre_test_config.json` (a fixture) out of a bundle that "copy the data folder"
would have taken.

Five roles cross, and the role is what `--no-secrets` / `--no-assets` filter on:

| Role | What |
| --- | --- |
| `tool_config` | `config.json`. Every path in it is relative to the tool root by design, so a copied root stays self-consistent and nothing is rewritten on arrival |
| `config_catalog` | `data/config_catalog.json` itself — it is not listed among its own `config_sources`, so walking the catalog cannot pick it up |
| `config_source` | the catalogued `data/*.json` |
| `secret_store` | `data/encrypted_secret_text.json`, as **ciphertext**. The passphrase is not in the file and there is no field for it to hide in: the importing machine sets `DB_OPS_SECRET_KEY` |
| `estate_asset` | `assets/**` and `data/ssh_keys/**` — config names these by path, so config without them points at nothing |

Two rules the format exists to enforce, both of them about a file that arrived from somewhere
else:

1. **Every entry carries a checksum and the whole bundle is verified before anything is
   written.** A truncated transfer leaves the target untouched. Half-applying one produces a tree
   that is neither the old estate nor the new one, which is worse than either.
2. **A path inside a bundle is data, not an instruction.** `_safe_relative` refuses absolutes,
   drive letters, UNC roots and `..`. Writing wherever a received file asks to be written is the
   archive-extraction defect, and a bundle is an archive.

Import also refuses, whole-bundle, to replace a file that already exists with different content
unless `--force`: the machine being imported into may already be somebody's working install, and
its `db_instances.json` may be the only copy. A file that already matches is *identical*, not a
conflict — which is what lets an interrupted import be finished by running it again.

A `json` entry carries the **parsed document** rather than the source bytes, so a bundle stays
readable and two estates can be diffed by a person. Byte fidelity is then bought back, because the
first design did not have it and that was wrong: measured on this tool root, all 26 catalogued
files are CRLF and two-space indented, so re-emitting the canonical form would have put 26
whole-file diffs in the next commit anybody made. Two things restore it:

- `layout` records how the source file was written — indent, line ending, trailing newline, BOM,
  and whether it was escaped to ASCII — and the entry is re-emitted through it. It is validated
  like every other field on the way in: an indent of a million is a way to turn a four-line config
  into gigabytes on the importing machine, and a `newline` of arbitrary text rewrites the
  document's own separators.
- `verbatim` carries the source text for the file no layout describes. In this estate that is
  exactly one of 74 — `config_catalog.json`, hand-formatted with each collection on a single line
  inside an indented array, which `json.dumps` emits at no setting. `verbatim` is what lands, and
  it is required to parse to the same document as `content`, so the readable half stays a faithful
  summary of the written half rather than a claim about it.

Measured end to end on the real tool root: 74 files exported and imported into an empty root,
**0 byte differences**, and `db-ops check-credentials` gives the imported root the identical
answer — 30 targets checked, same one pre-existing problem.

**A bundle is a credential.** It names hosts, accounts and chat ids and carries ciphertext. It
must never be committed; `.gitignore` refuses the names `export-data` suggests.

#### `inventory_render.py` — `EXCLUDE_IP_PREFIXES` is empty on purpose

It was `("198.51.100.",)` until 2026-08-21: one estate's management subnet, written into the
rendering library. Every inventory page anyone rendered silently dropped every server in that
range, and nothing on the page said so — a reader counting servers got a wrong number with no way
to notice.

Hiding a machine is a statement about *an* estate, which makes it configuration. The library's
default is now to hide nothing, the list arrives as an argument (`exclude_ip_prefixes`), and the
apps read it from `reports_config.json` through
`db_ops.common.data_sources.inventory_exclude_ip_prefixes()`. The reader lives in `common` rather
than here for the usual reason: reading the data folder is an operation, and `lib` is only ever a
function of its arguments.

`tests/test_inventory_exclude_ip_prefixes.py` pins both halves — the library shows everything
unless told, and an absent config key stays an empty filter rather than becoming an accidental
one.

### `paths.py`

`TOOL_ROOT`, `REPO_ROOT` and `DEFAULT_DATA_DIR`, stated once instead of `parents[2]` written out
in thirty-two files at four different depths.

**The package's own location is no longer the answer** (changed 2026-08-21). It used to be:

```python
TOOL_ROOT = Path(__file__).resolve().parents[2]
```

which is right in a dev checkout and right in the container, because the package sits beside
`data/` in both. It is wrong for an installed copy: a wheel built from this tree and installed
into a clean virtualenv resolved its data directory to `site-packages/data`, a path that does not
exist and never will. The tool imported cleanly and could not have found one config file.

So the answer has an order, and the package's location is the last entry in it:

| | Source | |
| :---: | --- | --- |
| 1 | `DB_OPS_HOME` | what the operator stated. A value pointing at a directory that does not exist **raises** — falling through would swallow a typo and then read a different estate's configuration |
| 2 | the working directory | only if it carries a `ROOT_MARKERS` entry (`data/` or `config.json`). Any directory would otherwise be accepted, inventing an answer for a user who said nothing |
| 3 | `PACKAGE_ROOT` | the fallback that keeps the checkout and the container working unchanged |

`DB_OPS_DATA_DIR` points the data folder somewhere of its own, because once the tool is installed
the two move independently: the code goes where pip puts it, the configuration stays where the
operator keeps configuration.

**Shipped assets follow a different rule, because two owners share one vocabulary.** Metric
collectors and the backup, restore and host scripts implement shipped capabilities — the same for
everyone, and an install that cannot find them is not an install. `tasks/` and
`sql_telegram_commands/` are written per operator and per server, and the deploy mirrors the
worker's copy of `tasks/` back to the master. So `asset_candidates()` asks twice in a stated order
— **the operator's `assets/<kind>/`, then the package directory that owns that kind** — and
`asset_dir()` returns the first that exists. That order is what lets an operator fix a query for
their own environment without forking, and when neither exists the *operator's* path is reported,
so "not found" names a directory they can create.

`BUILTIN_ASSET_ROOTS` is where the two vocabularies meet, and it is the reason no configuration
file had to change when the shipped files moved on 2026-08-22:

| Configuration says | The files live in |
| --- | --- |
| `assets/metrics/...` | `db_ops/metrics/collectors/` |
| `assets/backup/...` | `db_ops/common/backup_scripts/` |
| `assets/restore/...` | `db_ops/common/restore_scripts/` |
| `assets/host/...` | `db_ops/sre/host_config/` |

The right-hand column is derived from this module's own location, and legitimately: a built-in
asset follows the code, the way configuration follows the operator. `OPERATOR_ASSET_KINDS` names
the kinds with no built-in half at all — `tasks`, `sql_telegram_commands` — so a miss there returns
only the operator's path rather than offering one inside `site-packages` that can never exist.

Until that move the shipped half sat in a second directory *also* called `assets`, inside the
package. One name for two owners cost three defects in two days, so the shipped files now live
with the component that runs them and `BUILTIN_ASSET_ROOTS` carries the translation. The lookup
that reads it is unchanged; only the right-hand column moved.

`resolve_tool_path()` is the same rule for a path *a config gave*: configuration names a shipped
script the way an operator sees the tree (`"assets/backup/sqlserver/mssql_backup_database.sh"`),
and it should not have to know where the installer put it. Operator tree first, package second, so
a local override of a shipped script still wins.

**It resolves per file, and `asset_dir()` per directory — the difference is not cosmetic.** A
directory-granular answer means an operator who creates `assets/metrics/` to add one query hides
every shipped one, which is exactly what happened: the metric catalogue joined all 189 variants
onto a single chosen root and refused to start. Anything that names individual files goes through
`resolve_tool_path()`; `asset_dir()` is for the callers that genuinely want a directory.

`tests/test_asset_lookup.py` holds the order down, and its last test refuses any module that
spells an `assets/` path out for itself.

`resolve_tool_root()` and `resolve_data_dir()` take the environment, the working directory and the
package location as **arguments**, defaulting to the real ones. That is what lets
`tests/test_tool_root_resolution.py` describe the rule without building a wheel — the original
failure was found by building one, and finding the next should not require it.

`TOOL_ROOT` and `DEFAULT_DATA_DIR` remain module-level values, resolved once at import. They
deliberately do not raise when nothing is stated and nothing is found: an unimportable package is
a worse failure than a missing config file, and the config loader is where a missing file gets an
error naming it.

#### `record_form.py`

`flatten(record)` -> the rows the web console draws; `rebuild(submitted)` -> the record again. It
is here rather than in `webhost/` because it is a pure function of its argument and because the
property it exists for has to be testable without a browser:

```
rebuild(flatten(record)) == record
```

Every leaf of the record becomes a field carrying the JSON type it came from, so nothing is
dropped and nothing changes type — the two ways a generated form quietly corrupts config. See
`docs/12_webhost_app.md` for what it looks like and `tests/test_record_form.py` for the check
against every record in `data/`.

#### `web_auth.py`

Password encoding, verification and session-token generation for the web console
(`docs/12_webhost_app.md`). It belongs here and not in `webhost/` for the usual reason: it is a
pure function of its arguments, it opens nothing, and there must be exactly one answer to "is this
the right password" no matter who is asking — the HTTP layer, the store, or a CLI creating an
account.

It imports `PBKDF2_ITERATIONS` and the salt length **from `secret_text.py`** rather than spelling
them again. db_ops has one KDF setting; two copies would agree today and diverge the first time
one of them is raised, leaving passwords hashed at the old cost with nothing to say so.

The cost is read from the module attribute at call time, which makes that constant a single
switch — and lets a test run the KDF at its floor instead of spending its whole runtime proving
arithmetic.

### `lib/restore/`

The one subpackage: `spec.py`, `plan.py`, `pitr.py` — what a restore *is* and what it would do,
with no ability to perform it. Held to the same purity rule as the flat modules, and additionally
covered by `tests/test_common_restore_is_pure.py`.

---

## `response.py` — the shape everything answers in

Every command in the `common` and `db` CLIs returns the same envelope, built here:

```json
{
  "success":   true,
  "operation": "run-sql",
  "message":   "2 row(s) from ACME-192-0-2-111.master",
  "error":     null,
  "data":      { },
  "metrics":   { }
}
```

`ok()` and `fail()` build it; `emit()` prints it and returns the exit code, which is a **summary of
the response**, not a second channel. All 46 commands answer this way.

Two details that look like bugs and are not:

* `emit()` prints with `indent=1`. Compact JSON turned Telegram emergency replies — which forward
  a command's stdout verbatim — into one unreadable line.
* Commands still emit `txt` / `csv` / `xml` / `xlsx` / `raw` **when the request asks for it**. The
  renderer is chosen inside the request object, so this is the JSON-object contract working, not an
  exception to it.

## `common_cli.py` — two readers, and only two

```python
common_cli.run(command, request, *, timeout_seconds=None)                 # raises on failure
common_cli.run_allowing_failure(command, request, *, timeout_seconds=None) # -> (success, data, error)
common_cli.spawn(command, request, *, module=DEFAULT_MODULE, timeout_seconds=None)
```

`spawn` carries a `module` parameter so `db/queue_message.py` can reach the `db` CLI through the
same transport instead of keeping its own subprocess copy.

There was briefly a third reader, `run_ok`, for the old `{"ok": …}` shape. It was **deleted** when
the last command moved to the envelope — one response shape means one pair of readers, and a third
would immediately start growing its own answer for a command that printed nothing. The deadline
`run_ok` carried moved onto both survivors, so a caller states the wait it is actually doing (the
instance-metadata replay asks for 1,800 s).

---

## `target_profile.py` — which tool, and who decided

Added 2026-08-19 to close a measured gap: every entry point in `common` knew the engine and the OS *family*, and none of them knew
a **version**, on an estate holding Oracle 8.1.7 next to Oracle 23 and Windows Server 2003 next to
2025. `run-sql` handed an 8i target to python-oracledb, which speaks 12.1 and newer, and failed
with `DPY-3010` — a driver code naming neither the cause nor the fix.

Meanwhile the rule that answers it already existed, in production and tested, inside
`metrics/collector.py`: pick the variant whose `min_major_version`/`max_major_version` window
contains this target. **85 of 89 metrics** use it. One app could select by version and nothing else
could — the definition of a rule in the wrong layer. It now lives here and `metrics` imports it.

### The three vocabularies, kept apart

| Field | Question | Values |
| --- | --- | --- |
| `platform` | which OS family, so which shell | `windows` / `linux` |
| `os_major` / `os_minor` | how old, so which cmdlets exist | NT version — `5.2` = Server 2003, `6.2` = 2012, `10.0` = 2016+ |
| `runtime` | what the command runs *inside* | `host` / `docker` / `k8s` |

`hostcmd.Host.runtime` collapses the first and the third into one field, which is why `run-cmd`
cannot target a container while `backup-database` can. `hostcmd_runtime()` translates at the
boundary so the profile stays precise and the existing callers keep the string they parse.

### Precedence, in one place

> explicit request → per-target config → profile rule → engine default

`TargetProfile.merge()` applies it for facts and every `select_*` reports it as `chosen_by` for
tools. The caller is looking at the server; `db_instances.json` is a file somebody typed. A merged
profile carries a `sources` map naming which side supplied each field, so an answer can be
attributed without reopening the inventory.

### What it will and will not refuse

**Unknown is never a guess and never a refusal.** Half the inventory has no `major_version`, so a
rule that behaved differently on an unknown version would have changed production behaviour the day
it shipped. Every function here falls back to the pre-2026-08-19 behaviour when a fact is missing;
what it refuses is what is *known* to be impossible:

* Oracle below 12.1 with thin-mode `oracledb` — raises with both ways out named (the bridge, or
  `oracle_client_mode: "thick"`).
* `select_powershell_dialect` reports `cim` vs `wmi` (PowerShell 3.0 arrived with NT 6.2) and
  `windows_management_transport_available` is False for NT 5.x — the predicate that should
  eventually replace the nine hand-listed `OS_*` codes on `ACME-192-0-2-235` / `-236`.
* SQL Server driver selection is **advisory**: it names the choice and leaves the existing
  ODBC → pymssql fallback alone. That fallback reaches the 2008 R2 servers by catching a TLS error
  string rather than by knowing the target is old — an accident that works, and re-ordering the
  driver chain under fourteen live instances needs its own change with its own verification.

---

## `connection_spec.py` — the request that needs no inventory

The SQL counterpart of `common/backup/spec.py` (*"everything a backup run needs, stated in the
request rather than looked up"*), added 2026-08-19 alongside `target_profile.py`.

`run-sql` had one door: name a `target`, and the resolver reads `db_instances.json` for the host
and `users.json` for the login. That is the right default and stays the default. It made two things
impossible — reaching a machine that is in no inventory, and running without inheriting two file
reads the caller may not want.

A request may now carry a `connection` block instead, and then **nothing is read**:

```json
{"connection": {"db_type": "sqlserver", "host": "192.0.2.5", "port": 1433,
                "username": "monitor", "password": "…", "major_version": 16,
                "label": "lab-mssql"},
 "sql": "SELECT 1"}
```

Three fields are required because no default can invent them — **which engine, which machine,
which login**. The version rides in the same block, so a request that is self-contained about the
connection is self-contained about the *tool* as well.

The one thing that can still touch a file is the password, and only by request: `password` is
literal and reads nothing, `password_ref` is resolved from the environment first and the encrypted
store second. That is material rather than config, and it is opt-in.

`to_resolved()` returns the *same* dict `sql_run.resolve_sqlserver_target` returns, so
`connect_target`, the legacy-bridge branch and the answer builder cannot tell which door the
request came in through — otherwise each of them would grow two code paths.

---

## Where this layer does not meet its own bar yet

Recorded here rather than left to be rediscovered. Neither is a rule violation — the guard passes —
but both are gaps against the house style in `CLAUDE.md`.

* **Four modules have no module docstring:** `event_policy.py`, `policy_engine.py`,
  `target_flags.py`, `time_window.py`. Two of them (`policy_engine` at 376 lines, `time_window` at
  303) are among the largest and most load-bearing in the package. The convention is that a module
  docstring says *why the behaviour matters*.
* **`secret_text.py` sits alongside the value modules** and is the only one whose subject is
  material rather than a rule. It is pure by the guard's definition, but a reader scanning the
  index will not expect encryption here.

---

## See also

* [`13_common.md`](./13_common.md) — the operations layer and its CLI contract.
* [`architecture.md`](./architecture.md) — the thirteen ordinals and how a request crosses them.
