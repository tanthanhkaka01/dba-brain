# Metrics Engine

## Purpose

The Metrics Engine is the component that actually goes and looks. It connects to every enabled
target in `data/db_instances.json`, runs the metrics `data/metric_definitions.json` says apply to
that engine and version, normalises what comes back into one shape, writes it to the runtime store,
and rebuilds a health summary per target.

Everything downstream reads what it produces: the reports app turns the rows into findings, and
the SLA app grades them against objectives. Neither ever connects to a monitored database itself.

| | |
| --- | --- |
| Run it | `python -m db_ops.metrics.cli --config config.json collect` |
| Look first | add `--dry-run`: it names every metric that would run against every target, and opens no connection |
| Configured by | `data/db_instances.json` (targets), `data/metric_definitions.json` (the catalogue), `data/metric_importance_overrides.json` (what matters here) |
| Least privilege | a read-only login per engine — see [`docs/security.md`](./security.md) |

---

# Distinctions that cost an incident to learn

The sections below are not edge cases. Each one is a place where two things look like one thing,
the collector treated them as one, and the result was a confident wrong answer. They are first
because they are what a reader gets wrong; the reference material follows them.

## Metrics that are collected but not reported (`report_policy.collect_only`)

Most metrics are a **signal**: a row means something needs attention. A few are an **inventory**.
`MAINTENANCE_INDEX_USAGE` emits one row per index — roughly 29,000 on a single large database — and
by design nearly all of them are status OK. Those rows belong in the store, where the inventory
report renders them and an operator queries them; sending them through the hourly warning/critical
report would bury every real alert under thousands of lines that were never alerts.

Declare it on the metric, not in the report:

```json
"report_policy": { "collect_only": true }
```

`db_ops/reports/metrics_reports.py::_filter_rows_for_reports` drops those metric codes from the
scheduled reports. Collection, storage and the inventory report are untouched.

Two safeguards, because the dangerous failure is the filter removing too much rather than too
little: a row with no `metric_code` column is **kept**, and an unreadable
`metric_definitions.json` excludes **nothing** — a report missing its rows is far worse than one
inventory metric appearing for a cycle.

## A live condition and a configuration fact are two metrics, not one

The cadence a metric needs is decided by **how often its answer can change**, and mixing two
answers with different cadences into one SQL file makes the slow one as noisy as the fast one.

`QUERY_STORE_QUERY_ISSUES` reported both "is a query misbehaving right now" (`repeat_interval`
900 — a query regression is worth knowing about within the quarter hour) and "does this database
have Query Store switched on". The second only changes when somebody changes it, so every database
with Query Store off produced a WARNING **every 15 minutes**: 96 identical alerts a day, per
database, on an estate where whole instances have it off. The alert stream became mostly that one
line, which is how a real finding gets missed.

The coverage half is now its own metric:

| Metric | SQL | Cadence | Answers |
| --- | --- | --- | --- |
| `QUERY_STORE_QUERY_ISSUES` | `sqlserver/023_...query_issues.sql` | every 15 min | is a query heavy / regressed / blocked right now |
| `QUERY_STORE_COVERAGE` | `sqlserver/070_...coverage.sql` | `repeat_interval` 72000, `from_hour` 8 → `to_hour` 10 | **every** database's Query Store state and settings — one row each, OK ones included |

A `repeat_interval` longer than the window makes the **window** the thing that schedules it: one
report each morning. Its `condition_grouping` keys on `instance_key` + `issue_type` rather than the
database, so the finding reads "9 databases have Query Store off on this instance" instead of nine
alerts — grouping per database would put the noise back one alert at a time.

The issues metric keeps its `#qs_cov` table even though it no longer reports from it: that table is
what decides which databases may be scanned, and without it the cursor reaches an AG secondary and
fails with error 976. Reporting coverage *somewhere* is still the point — a database with Query
Store off returns no rows from the issues metric, which is indistinguishable from a database with
no query problems.

### How deep a metric scans and how recent a finding must be are two windows

**`QUERY_STORE_COVERAGE` reports every database, not only the broken ones.** It emitted rows only
where Query Store was off, which raises an alert and answers nothing else: a healthy database
produced no row, so "is Query Store on for APPDB" was indistinguishable from "APPDB was never
collected". The fleet page's per-database Query Store column and the server page's Query Store
section are both built from these rows, so the OK ones are the point rather than overhead — the
same shape `DATABASE_STATUS` already has. `metric_item` is the database name (it used to be the
constant `query_store_coverage`), and the message carries the settings: desired vs **actual**
state, `readonly_reason`, storage used against the limit, capture/cleanup mode, retention.

The actual state is the one that matters. A Query Store that reaches its `max_storage_size` flips
itself to READ_ONLY and stops capturing while `sys.databases.is_query_store_on` still reads 1, so a
report built on the configured flag calls that database covered when it has recorded nothing since
the day it filled up.

`QUERY_STORE_QUERY_ISSUES` scans six hours and runs every fifteen minutes, so until 2026-08-07 it
re-reported everything the scan could still see. One SALESDB query that ran once at 14:28 produced an
identical CRITICAL at 14:32, 14:47, 15:02 … and would have gone on until 20:28 — twenty-four alerts
for one finished statement, with nothing about it changing in between.

Shortening the scan is not the fix. The scan's depth *is* the baseline: `best_logical_reads` is the
cheapest plan the query has recently used, and that is what `logical_read_ratio` divides by. Scan
only the last half hour and the regressed plan becomes its own best — ratio 1.00, regression gone.

So the SQL carries two windows:

| Variable | Length | Decides |
| --- | --- | --- |
| `@p_FromLocal` | 6 hours | how far back `#qs_raw` reads — the plan baseline `query_best` is built from |
| `@p_AlertFromLocal` | 30 minutes | which rows are *reported*: `issue_rows` keeps a plan only if its newest execution is inside it |

The alert window must be **at least the collection cadence**, or a query that finishes just after
one run and long enough before the next falls into a blind spot and is never reported at all —
30 minutes against `repeat_interval` 900 leaves none, at the cost of each finding being sent twice.
Both windows are printed in the message (`alert_window` / `alert_from` next to `checked_window` /
`checked_from`), because a six-hour window shown alone next to a two-hour-old `last_execution_time`
reads as an alert arriving late rather than as a baseline.

## One PostgreSQL database is not the cluster (`variants[].per_database`)

PostgreSQL cannot read another database's catalog from one connection. `executor._metric_database`
sends a PostgreSQL metric to `connection_info.database`, falling back to `postgres` — and **a
target that does not set it silently gets that fallback**. So every per-database PostgreSQL metric
was describing `postgres`, which on a cluster whose user tables live elsewhere holds nothing at
all, while the real data sat in a database nothing looked at. Four metrics reported OK from an empty database:

| Metric | Reported | Actually |
| --- | --- | --- |
| `POSTGRES_TABLE_BLOAT` | `bloated_tables=0` | from 0 tables |
| `POSTGRES_INVALID_INDEXES` | `SQL returned no rows` | reads as healthy |
| `POSTGRES_VACUUM_HEALTH` | `SQL returned no rows` | reads as healthy |
| `DATABASE_CONSTRAINT_HEALTH` | `postgres :: constraints` | named the database it was looking at |

A variant may now declare `"per_database": true`, and the collector runs its SQL once per database
on that target, concatenating the rows.

**It is opt-in per variant, not per engine.** Most PostgreSQL metrics are cluster-wide —
`pg_database`, `pg_stat_replication`, `pg_settings` are the same from any connection — and
iterating those would store identical rows once per database. The five that read a per-database
catalog carry the flag; the rest deliberately do not.

**PostgreSQL only, enforced at load.** `definitions.py` rejects the flag on any other engine:
SQL Server already iterates databases inside the SQL with a cursor and `USE`, so looping the
connection as well would collect everything twice.

Four rules in `executor._execute_per_database`:

- **One database failing costs its own rows and nothing else** — the same rule
  `load_metric_targets` applies to a broken `cmd_access`. A database dropped mid-run, or one this
  login cannot enter, must not take the rest of the cluster with it.
- **A failed database is reported as a row, never skipped.** A per-database metric that quietly
  visits half a cluster is the exact fault being fixed here, not an acceptable degradation.
- **`MAX_DATABASES_PER_METRIC` (50) bounds the connection cost**, and passing it emits a
  `databases :: truncated` row naming what was skipped. Each database is a login; an unbounded
  loop turns a 200-database cluster into 200 logins per metric per run.
- **The caller's target is copied, not mutated.** A metric that left it pointing at the last
  database it visited would silently redirect every metric collected afterwards.

The connected database is visited **first**, so a truncated run is a superset of the old
behaviour rather than a different arbitrary slice.

### What the SQL must then not do

A metric declared `per_database` must not emit a row calling itself a cluster total: it would be
written once per database under the same `metric_item`, and the report keeps whichever arrived
last. That is how a server with 66 indexes came to report 1. Emit the per-database summary only —
`index_report._fill_totals_from_databases` adds them up.

## A metric that cannot fail is not a check

`DATABASE_CHECKDB` returned status `OK` for every row, including when the value was `unknown`, so
an instance that has never proven its integrity looked exactly like one that proves it nightly.
Both audited targets reported `unknown` for every database while the page said OK.

`unknown` was also **two different findings wearing one label**, which is why it could not be
alerted on safely:

| Cause | It is a finding about | Now reported as |
| --- | --- | --- |
| DBINFO ran, value is the `1900-01-01` sentinel | the **database** — CHECKDB never recorded a known-good | `never`, WARNING, `issue_type=CHECKDB_NEVER` |
| DBINFO could not run (`User 'guest' does not have permission`) | the **monitoring** — the collector lacks rights | one instance-level `checkdb_coverage` row, `issue_type=CHECKDB_UNREADABLE` |

The permission case is probed once up front and emitted as a single row, because the grant is
instance-wide: reporting it per database blamed 11 databases for one missing GRANT. Age thresholds
are `DECLARE`d at the top of the SQL (the convention `063_login_health` already uses):
`@stale_warn_days = 7`, `@stale_crit_days = 30`.

**`tempdb` is excluded from the cursor (2026-08-13).** It is recreated from `model` at every
service start, so its DBINFO page — and with it `dbi_dbccLastKnownGood` — is new after every
restart. Running CHECKDB on tempdb therefore never durably records a known-good, and the row came
back `never` forever: 11 permanent WARNINGs, one per instance, that no action could clear. A check
nobody can pass is the mirror image of the bug this section is about — a metric that cannot fail is
not a check, and one that cannot pass is not either. `master`/`model`/`msdb` stay in, because their
known-good **is** durable, so `never` there is a real finding somebody can close.

The metric produces one row per database and the estate has 112 of them, so it carries
`condition_grouping` on `instance_key` + `issue_type` — the finding is "this instance never runs
CHECKDB", not 112 separate findings. Adding severity to a per-database metric **without** grouping
is how `QUERY_STORE_QUERY_ISSUES` came to emit 96 identical warnings a day per database.

Since 2026-08-05 there is a `legacy_2008r2` variant. The single `sqlserver_all` variant used
`TRY_CONVERT`, which is 2012+ and fails the whole batch at compile time, so the metric had been
switched off on the two 2008 R2 instances — an instance whose integrity is least likely to be
checked was the one not being asked. The rewrite converts one value at a time inside the cursor
rather than in a set-based `CASE`: a guard and a conversion in the same `CASE` are not ordered, so
one unparseable value would raise 241 and take every row with it. `CONVERT` is pinned to style
`121` because `dbi_dbccLastKnownGood` comes back as `'yyyy-mm-dd hh:mm:ss.mmm'`, a shape that
`datetime` reads through the session `DATEFORMAT` — under `dmy` the age would be silently wrong by
up to eleven months.

## Truncation must never be silent

The shared result cap is 100 rows (`sql_execution.MAX_RESULT_ROWS`), sized for a Telegram-shaped
answer. Inventory-shaped metrics exceed it, and a cut result set was **indistinguishable from a
complete one**: `STORAGE_FILE_PLACEMENT` reporting exactly 100 rows meant "the first 100 of an
unknown number".

Two halves to the fix, and both are needed:

- `execute_cursor_batches` now fetches one row past the cap and reports `truncated` per result set
  and overall; the metric executor logs a warning naming the metric and the cap. A future
  truncation announces itself instead of quietly deleting findings.
- Metrics whose output is an inventory declare their own `max_rows` in
  `data/metric_definitions.json`: `MAINTENANCE_STATISTICS_AGE` 20000,
  `DATABASE_CONSTRAINT_HEALTH` / `STORAGE_FILE_PLACEMENT` / `MAINTENANCE_HEAP_FRAGMENTATION` 5000
  (`MAINTENANCE_INDEX_USAGE` already had 100000).

Writing a metric that returns one row per object? Declare `max_rows`, or it silently stops at 100.

The same rule applies to what a collector reads *before* it produces rows. `OS_EVENTLOG_CRITICAL`
derives three facts — a count, the newest timestamp, the top 3 event ids — and used to read the
whole 24h window to get them. On 2026-08-09 the four AX servers `192.0.2.116-119` logged ~26,000
`Dynamics Server Azure` event 117 records in 24 hours; `@(Get-WinEvent ...)` materialised all of
them and the metric blew its 60s timeout **22 consecutive times per host**, so the event-log check
was blind exactly while the event log was screaming — the same shape of failure as the ungraded
I/O metric above. Both variants now read at most `OS_EVENTLOG_MAX_EVENTS` (default 500) of the
**newest** events and report `truncated=yes, count_is_a_floor=yes` when they hit it.
`metric_value` stays a plain number so the chart and any `warning_threshold` still parse it.

## An FCI is not an Availability Group

`AVAILABILITY_DATABASE_HEALTH` queries Always On AG DMVs. On a Failover Cluster Instance it
truthfully answers `NOT_CONFIGURED`, and that was being read as "HA is fine" — the ERP FCI
(192.0.2.115 / .113, listener `SALESCLUSTER`) had no cluster monitoring at all.

`CLUSTER_FCI_HEALTH` (`db_ops/metrics/collectors/os/windows/015_os_failover_cluster.ps1`, `collector_type:
cmd`) reports what an FCI's health actually lives in: nodes, quorum and witness, roles and which
node owns them, resources that are not Online, failover events in the last 24h, and services.

Two rules it cannot break:

- **Role-aware, or it reports a healthy cluster as broken.** Exactly one node runs SQL at a time;
  on the passive node the service is stopped and that is *correct*. The script works out whether
  this node owns the SQL role, then judges `MSSQLSERVER` / `SQLSERVERAGENT` against that.
  `ClusSvc` is the exception — it must run on every node. A naive "is MSSQLSERVER running" check
  calls the passive node down on every poll, forever.
- **Not every resource carries the service.** `SQL Server CEIP` is Microsoft telemetry and is
  routinely switched off deliberately; it is also what puts the SQL group into `PartialOnline`.
  Reporting either as CRITICAL made a serving cluster permanently red on its first run. Ancillary
  resources are LOGGING, `PartialOnline` is WARNING, and a SQL role that is genuinely `Offline` or
  `Failed` is still CRITICAL.

A host that is not a cluster node reports `NOT_CLUSTERED` / OK, the same way the AG metric reports
`NOT_CONFIGURED` — most targets are not cluster nodes and must not alert about it.

## Cumulative counters are not current performance

`sys.dm_io_virtual_file_stats` and `sys.dm_os_wait_stats` are totals **since the engine started**.
Dividing them gives an average over that whole period, and the reports showed it as the current
value: on 192.0.2.250 the engine started 2025-10-27, so a 95.97 ms write latency was nine
months of history. One bad afternoon last winter keeps the tile red forever, and a problem starting
this morning is diluted to invisibility by the good months behind it.

Read `PERFORMANCE_IO_LATENCY` with that in mind. `019_sqlserver_io_latency.sql` (both variants)
grades itself on that average, at **200 ms warning / 500 ms critical** — so it catches a file that
has been slow for as long as the instance has been up, and it does not catch a spike. The raw
counters (`io_stall_read_ms`, `io_stall_write_ms`, `reads`, `writes`, `counters_since`) are still
carried through to the message for anyone diagnosing by hand.

### Why the interval version was withdrawn

Between 2026-08-10 and 2026-08-11 the collector graded this metric on the **interval** between two
stored samples instead, which does catch spikes: on 192.0.2.115 the cumulative average read
12.84 ms while single 15-minute intervals reached 269 ms and 1736 ms, every one stored green.

It was removed on 2026-08-11 anyway, and the reason is worth keeping: making it work took a policy
module, a config file, a store lookup and a `if metric.metric_code == ...` branch in the collector
— per-metric machinery, for one metric, in the layer every metric passes through. A metric is
collected and it grades itself, in its SQL or its command. That is the contract, and one metric is
not a reason to bend it.

If interval grading returns, it returns as a capability **any** cumulative metric can declare in
`metric_definitions.json` — with severity coming from the existing per-target
`warning_threshold` / `critical_threshold` overrides, not a policy document of its own. The
arithmetic and its refusals (a counter that went backwards is a restart, not negative work; a pair
too far apart describes an average, not a moment) are in git history at 2.75.04.

### Row caps are per metric

`sql_execution.MAX_RESULT_ROWS` (100) is the shared default and is right for a signal metric: a
hundred blocking sessions is already an incident and the rest is noise. An inventory metric is the
opposite — truncating it to 100 turns a complete picture into an arbitrary sample that still looks
complete. Set `"max_rows"` on the metric definition instead of raising the global constant, which
would change every metric at once.


## Severity Policy

| Situation | Status | Report section |
| --- | --- | --- |
| Metric value breaches critical threshold | `CRITICAL` | `[BACKUP CRITICAL]`, `[DISK CRITICAL]`, `[OTHER CRITICAL]` |
| SQL Agent job execution completed with Failed outcome | `CRITICAL` | `[JOB FAILED]` |
| Metric value breaches warning threshold | `WARNING` | `[host / metric_name]` group |
| Target unreachable / no connection / auth rejected | the metric's `connection_error_severity` (default `WARNING`) | by resulting status |
| Collection failure after connecting (query error, subprocess error, parse error, missing tool) | the metric's `execution_error_severity` (default `WARNING`) | by resulting status |
| No data collected within window | `NO_DATA` | warning report |
| Metric collected and within thresholds | `OK` | logging report only |

**Key rules:**

- Beyond a threshold breach, `CRITICAL` is also what a metric may declare for its *own* failure —
  see **Failure severity** below — and SQL Agent jobs whose execution completed with a `Failed`
  outcome.
- The `ERROR` status in stored results means a collection failure. For routing purposes it is treated identically to `WARNING`.
- `[JOB FAILED]` in the critical report is triggered only by metrics whose `metric_code` contains `JOB`. Authentication or connection failures never appear in `[JOB FAILED]`.

**Report format:**

```
[WARNING REPORT TITLE]
Run: ...
Duration: ...
Warning: N

[host / metric_name]
- item: detail

[OTHER WARNING]
- host / METRIC_CODE: collection failed: <full error message>
```

```
[CRITICAL REPORT TITLE]
Run: ...
Duration: ...
Critical: N

[CONNECTION ERROR]
[BACKUP CRITICAL]
[DISK CRITICAL]
[JOB FAILED]
[OTHER CRITICAL]
```

## Failure severity — `connection_error_severity` / `execution_error_severity`

Every metric in `data/metric_definitions.json` declares what a **failed collection** of it is
worth, split by which half of the attempt broke:

```json
{
  "metric_code": "INSTANCE_STATUS",
  "active": true,
  "connection_error_severity": "CRITICAL",
  "execution_error_severity": "CRITICAL"
}
```

| Field | Applies when | Typical value |
| --- | --- | --- |
| `connection_error_severity` | the collector never reached the target — connect refused, host unreachable, auth rejected, no usable credential | `CRITICAL` for availability metrics, `WARNING` elsewhere |
| `execution_error_severity` | the collector connected, then the check failed — SQL error, permission denied, non-zero exit, unparsable output | `WARNING` |

Accepted values: `OK`, `LOGGING`, `WARNING`, `CRITICAL`, `ERROR`, `NO_DATA` (`WARN` is read as
`WARNING`). A value outside that set is a load error, not a silent fall back — a typo would
otherwise downgrade the one metric the operator most wanted paged. A metric that omits the fields
defaults to `WARNING` on both, which is the flat behavior every collection failure used to get; a
missing field must never stop the catalog from parsing, because that would stop the whole estate's
monitoring.

**Why the split.** Collection failures were graded flat `WARNING` on the theory that `CRITICAL`
belonged to threshold breaches. For an availability metric that inverts the meaning of the result:
when `INSTANCE_STATUS` cannot connect, "the instance did not answer" *is* the finding, and
reporting it below a full transaction log understates the estate's most serious event. But "the
server is down" and "this check is broken" are different claims, and a capacity query failing on
one host genuinely is a warning — so the answer belongs to each metric, per phase, in config.

**Which phase a failure is.** The raiser says so; the message is only a fallback.
`db_ops/metrics/executor.py` holds the connect and the execute apart itself
(`MetricConnectionError` / `MetricExecutionError`), and the cmd/docker transports read the verdict
off the `remote_exec` exception class (auth rejected and host unreachable are the session; a
timeout is the session only when no command had been sent yet). Anything that declares no phase is
classified from its message by `db_ops/lib/event_policy.py::resolve_failure_phase` — the legacy
Oracle bridge is the real case, since it connects and queries in one call. **Unknown counts as an
execution failure**, never a connection one: the louder claim is never made on a guess.

**Per-target tuning still wins.** `metrics.severity_map` (target-level, metric-level or item-level
in `db_instances.json`) is applied *after* this grading, so an instance that is *expected* to
refuse every connect — a mounted Data Guard physical standby answering ORA-01033 — can be remapped
to `LOGGING` without weakening the metric for the rest of the estate.

Today only `INSTANCE_STATUS` is `CRITICAL`/`CRITICAL`; every other metric in the catalog is
`WARNING`/`WARNING`. `INSTANCE_STATUS` is one multi-engine metric with SQL Server, Oracle, MySQL
and PostgreSQL variants, so that setting covers every engine at once.

## Package / Files

- `db_ops/metrics/`
- `data/db_instances.json`
- `data/metric_definitions.json`
- `data/metric_importance_overrides.json`
- `db_ops/metrics/collectors/`
- the runtime store declared in `data/store_config.json` (PostgreSQL in this tree; `runtime/db_ops.sqlite` when the backend is `sqlite`)

## Runtime Tables

- Writes `metric_runs`.
- Writes `metric_results`.
- Rebuilds `target_health`.
- Reports and SLA read `metric_results`.

## Config Files

`data/db_instances.json` defines database targets, platform metadata, optional `cmd_access`, active flags, and credential references. `data/metric_definitions.json` defines metric codes, `collector_type`, variants, collection interval, and status rules. `data/metric_importance_overrides.json` can override severity/importance by target or metric.

Metric files live under `db_ops/metrics/collectors/`. SQL collectors execute `.sql` files. Command collectors execute script variants selected by `target.platform`; `target.cmd_access.method` decides whether execution is `local`, `ssh`, or `winrm`.

Four collector transports are defined (a metric declares exactly one via `collector_type`):

| `collector_type` | What it collects | How it runs | File/variant |
| --- | --- | --- | --- |
| `sql` | Database-engine metrics (SQL Server, Oracle, MySQL, PostgreSQL, …) | Connects to the DB and runs the per-engine `.sql` variant (direct, or the legacy Oracle tool for `sql_access.method="api"`/`"subprocess"`) | per-engine `.sql` under `db_ops/metrics/collectors/<engine>/` |
| `cmd` | OS metrics, **Windows and Linux** | Runs the per-platform script (`.ps1`/`.sh`) `local`, `ssh`, or `winrm` per `cmd_access.method` | per-platform script under `db_ops/metrics/collectors/os/{windows,linux}/` |
| `docker` | Per-**container** stats (CPU/memory/net/block IO, status, restarts) for a DB-in-Docker target | Runs the metric's script with the target's `container_name` in `$DOCKER_CONTAINER`; transport mirrors `cmd` — local against the worker's mounted `/var/run/docker.sock` by default, or shipped over **SSH** when the target declares an `ssh` `cmd_access` (a remote docker host, e.g. a cloud VM). `collector_type` stays `docker` (not `cmd`) so it is enable/disabled independently of OS metrics | script under `db_ops/metrics/collectors/docker/*.sh` (same JSON contract as `cmd`) |
| `k8s` | *(reserved)* Kubernetes pod/workload metrics | *not implemented yet* — the type is recognized and any `k8s` metric is skipped cleanly | a script directory alongside the others, not created yet |

Remote execution mechanisms such as SSH and WinRM are target **execution methods** (`cmd_access.method`), not collector types.

**Containerized DBs (Docker).** A database running in a Docker container on a host is modeled as
three concerns, kept separate so nothing is double-collected:

1. **Host OS once** — add a single OS-only target for the VM (`db_type: null`, `platform: linux`,
   `cmd_access.method: ssh`) so CPU/RAM/disk/network/load are collected once for the whole host.
2. **Per-container stats** — each DB-in-Docker target sets `container_name` and gets the
   `docker` metric (`DOCKER_CONTAINER_STATS`); a target with no `container_name` skips docker metrics.
3. **SQL as usual** — the DB target still collects its `sql` metrics over its published port.

So a DB-in-Docker target collects `sql` + `docker`; the shared host OS lives on the VM target;
OS (`cmd`) metrics are turned off on the container targets with `metrics.disabled_collector_types: ["cmd"]`.

## Supported Database Engines

SQL collectors dispatch by the `db_type` in `db_instances.json`; each engine uses its own driver and per-engine SQL variant under `db_ops/metrics/collectors/<engine>/`:

| `db_type` | Python driver | SQL variants |
|---|---|---|
| `sqlserver` | `pyodbc` (falls back to `pymssql`) | `db_ops/metrics/collectors/sqlserver/` (+ `legacy_2008r2/`) |
| `oracle` | `oracledb` thin; `sql_access.method="api"`/`"subprocess"` routes through the legacy 8i tool, which is installed separately (see [`13_common.md`](./13_common.md) → `oracle_bridge`) | `db_ops/metrics/collectors/oracle/` (+ `legacy_8i/`) |
| `mysql` | `pymysql` (falls back to `mysql-connector-python`) | `db_ops/metrics/collectors/mysql/` |
| `postgresql` | `pg8000` (pure-Python) | `db_ops/metrics/collectors/postgresql/` |

**The selection rule itself is shared, not private to this app.** Since 2026-08-19 the
db_type/platform/version filter lives in `db_ops/lib/target_profile.py`
(`candidate_variants` / `select_variant` / `version_matches`) and `metrics/collector.py` calls it.
It moved because it was the *only* version-aware tool selection in the tree, and being an app's
private function is what stopped `common.cli run-sql` from reproducing a collector run on an 8i or
2008 R2 target by hand. Behaviour
is unchanged, including the two rules worth keeping in sight: an `unsupported` variant is skipped
rather than selected, and with no `major_version` stated the metric's own `path` wins, then the
**last** supported variant (the catalog is written oldest-first).

**A variant's version bounds are a promise the SQL has to keep.** `min_major_version` /
`max_major_version` pick the file; nothing validates that the file actually runs on the versions
it claims. `LINKED_SERVER_STATUS` shipped one variant named `sqlserver_2008r2_plus` with
`min_major_version: 10`, holding SQL that used `CONCAT()` and `sys.fn_hadr_is_primary_replica()`
— both 2012-and-later. Every hourly run on the two 10.50 instances failed at compile time with
*"'CONCAT' is not a recognized built-in function name (195)"*, and because a compile error fails
the **whole batch**, the metric returned nothing at all rather than partial data. When adding a
2008 R2 floor, write the `legacy_2008r2/` file and raise the modern variant's floor to 11; a
2012+ feature reachable from a `min_major_version: 10` variant is a metric that never runs.
Verify with `run-sql` and `"autocommit": true` against a real instance of that version — see
[`13_common.md`](./13_common.md).

### Oracle 8i (`legacy_8i/`) — what it collects and what it deliberately does not

An 8.1.7 instance reaches none of the modern dictionary views, so it has its own variant of most
Oracle metrics under `db_ops/metrics/collectors/oracle/legacy_8i/`. Two rules make that split work:

- **`major_version` on the instance is not optional.** Without it the collector cannot tell 8.1.7
  from a current release: version-bounded variants stop filtering, and the metric falls back to
  the last supported variant in the list, so an instance quietly runs 9i+/12c+ SQL against views
  it does not have. On 1.236 that meant `BACKUP_AGE`, `BACKUP_LAST_RESULT`,
  `DATABASE_USER_PERMISSIONS`, `DATABASE_CONSTRAINT_HEALTH`, `MAINTENANCE_INDEX_FRAGMENTATION` and
  `MAINTENANCE_STATISTICS_AGE` all running and failing every cycle. Set it; the four version-bounded
  ones then skip cleanly.
- **The RMAN metrics have no version bound to skip on.** `BACKUP_AGE` / `BACKUP_LAST_RESULT` use
  an `oracle_rman` variant that claims every release, and their SQL is written for the 9i+ RMAN
  views (ORA-00933 / ORA-00900 on 8i). Turn them off per target with
  `report_policy.disabled_metric_codes`.

Beyond the ported core, these exist only for 8i because the failure they describe only exists
there: `SEGMENT_EXTENT_LIMIT` (a dictionary-managed segment hits `MAX_EXTENTS` and fails with
ORA-01631 while the tablespace still reports gigabytes free — locally managed tablespaces, the
default from 9i, make the ceiling unlimited), `ROLLBACK_SEGMENT_CONTENTION` (manually sized
rollback segments, replaced by automatic undo in 9i), and `LOG_FILE_SPACE`'s 8i variant, which
reads `v$archive_dest` because the Fast Recovery Area the modern variant measures did not exist
before 10g.

**Two thresholds on this instance were wrong in the same way, and the shape is worth recognising.**
Each compared a number against a ceiling that moves with it, so the check was permanently tripped
and told the operator nothing:

- `TABLESPACE_FREE_SPACE` measured free space *inside* the datafiles and called 89 MB CRITICAL on
  a tablespace whose autoextending file had 31.8 GB of headroom left. It now reports **effective**
  free — current free plus the growth remaining on autoextending files — and drives off
  `dba_data_files` with an outer join to `dba_free_space`, because a tablespace with no free extent
  at all has **no row** in `dba_free_space`: the state that most deserves CRITICAL was the state
  that reported nothing.
- `STORAGE_TEMP_SPACE` compared a sort segment's high-water mark against its own current size.
  A sort segment grows to its high-water mark and stays there, so the two are equal on every
  healthy instance. The metric is now a usage reading with status always OK; whether the temporary
  tablespace can still grow is a tablespace question, and `TABLESPACE_FREE_SPACE` answers it.

Before adding a threshold to a legacy variant, run it with `--force` against the instance and read
the row it produces. Both of these passed review and failed on first contact with real data.

PostgreSQL support (added for the SRE lab DBs) currently collects a core set: `INSTANCE_STATUS`, `DATABASE_STATUS`, `INSTANCE_CONNECTIONS`, `QUERY_LONG_RUNNING`, `LOCK_BLOCKING_SESSIONS`, `STORAGE_DATA_FILE_SPACE`, plus a dedicated `POSTGRES_REPLICATION` metric (primary: streaming replicas + byte lag; standby: replay lag). OS (`cmd`) metrics do not apply to containerized lab DBs — turn the whole class off per target with `metrics.disabled_collector_types` (see below). An HA cluster's nodes share one IP but are separate targets: give each a distinct `instance_name` (→ distinct `target_id`) **and its own `server_id`** (suffix it with the port), so their metrics do not collide under one key, and its own `port`, so the collector connects to and stores each node independently.

**Switching metrics off for one target**, in order of bluntness:

| Switch | Where | Use it for |
|---|---|---|
| `metrics.enabled: false` | `db_instances.json` | Collect nothing at all from this target. |
| `metrics.disabled_collector_types: ["cmd"]` | `db_instances.json` | **A whole class of collector.** `cmd` = every OS metric, `sql` = every database metric. Use it when the target has no OS to log into (a database in a container) — listing the OS metric codes one by one means every OS metric added *later* starts producing noise until someone remembers to extend the list. An unknown value (`["command"]`) raises rather than silently collecting everything. |
| `report_policy.disabled_metric_codes: ["BACKUP_AGE"]` | `db_instances.json` | One named metric. |
| `metric_overrides.<CODE>.enabled: false` | `db_instances.json` | Same, next to that metric's other overrides. |

A `cmd` metric is also skipped automatically when the target has no `cmd_access` at all — an OS metric needs a way onto the host, and not having one is "not applicable", not a failure.

### Which database collection connects to

**SQL Server: always `master`. The metric SQL does its own `USE <db>` when it needs another
database.** Collection is an instance-level activity — most metrics read `sys.*` DMVs that live
in every database, and the ones that walk user databases (index fragmentation, statistics age,
CHECKDB, VLF count) cursor over `sys.databases` and switch with dynamic SQL themselves. Pinning
the connection to one database would only limit where they can start.

This is why the target's `db_name` must **not** be used as the connection database on SQL
Server: in `db_instances.json` it is the *service label* (`APPDB-DEV`, `SALESDB-PROD`, `APPDB-PROD`),
not a database that exists. Passing it produced `Cannot open database "APPDB-DEV" requested by
the login. The login failed. (4060)` on every SQL Server target at once — 4 743 failed rows
across 6 servers on 2026-08-01 — because the login has no such database to open. The rule is
enforced in `metrics/executor.py::_metric_database` and pinned by a test.

The other engines are the opposite case: they have no instance-level catalog to sit in, so they
do connect to a named database — `connection_info.database` from the inventory, else the
engine's neutral default (`postgres`, `information_schema`). Oracle ignores the database name
entirely and connects by **service**.

| Engine | Connects to | Set by |
| --- | --- | --- |
| `sqlserver` | `master`, always | fixed; metric SQL does its own `USE` |
| `postgresql` | `database` from the inventory, else `postgres` | `db_instances.json` → `database` |
| `mysql` | `database` from the inventory, else `information_schema` | `db_instances.json` → `database` |
| `oracle` | the **service**, not a database | `service_name`, else `database` |

## Data Flow

Target config + metric definitions + metric files + secrets -> collector dispatch -> SQL execution or command execution -> normalized `MetricResult` rows -> `metric_runs`, `metric_results`, and `target_health` in the runtime store -> reports/SLA/manual CLI reads.

## How to Run

```powershell
python -m db_ops.metrics.cli --config config.json collect --dry-run
python -m db_ops.metrics.cli --config config.json collect
python -m db_ops.metrics.cli --config config.json collect --target-ip 192.0.2.115
python -m db_ops.metrics.cli --config config.json collect --metric-code BACKUP_AGE --force
python -m db_ops.metrics.cli --config config.json latest --limit 50
python -m db_ops.metrics.cli --config config.json report --db-type sqlserver
python -m db_ops.metrics.cli --config config.json summary-latest --db-type sqlserver
python -m db_ops.metrics.cli --config config.json health-summary-latest --db-type sqlserver
python -m db_ops.metrics.cli --config config.json alert-summary
```

## Useful Manual Queries

```sql
SELECT *
FROM metric_runs
ORDER BY started_at DESC, run_id DESC
LIMIT 20;

SELECT result_id, collected_at, target_id, metric_code, status, importance, message
FROM metric_results
ORDER BY collected_at DESC, result_id DESC
LIMIT 100;

SELECT target_id, status, score, error_count, warning_count, critical_count, no_data_count, ok_count, collected_at
FROM target_health
ORDER BY collected_at DESC, target_health_id DESC
LIMIT 50;
```

## Maintenance and sizing metrics

These answer "is the storage healthy and is maintenance keeping up", as opposed to "is the
service up". They are scheduled sparsely and in the 01:00–06:00 window because several of them
walk every database on the instance.

| Code | Engines | What it reports | Schedule |
| --- | --- | --- | --- |
| `MAINTENANCE_INDEX_FRAGMENTATION` | sqlserver, oracle, mysql | SQL Server: indexes >1000 pages at ≥30% fragmentation (LIMITED mode). Oracle: UNUSABLE indexes/partitions. MySQL: InnoDB `data_free` as % of the tablespace | nightly |
| `MAINTENANCE_HEAP_FRAGMENTATION` | sqlserver | **Heaps** — tables with no clustered index, which `MAINTENANCE_INDEX_FRAGMENTATION` cannot see (it filters `index_id > 0`). Forwarded records and page fullness | nightly |
| `MAINTENANCE_STATISTICS_AGE` | sqlserver, oracle, mysql | Statistics never gathered, older than 30 days, or invalidated by modifications since the last gather. Oracle 8i reads `dba_tables.last_analyzed` (ANALYZE, not DBMS_STATS) and reports "never analyzed" as its own case, because on 8i that means the optimizer is on the rule-based path for that table | ~daily |
| `INDEX_UNUSABLE` | oracle (all releases) | Indexes **and index partitions** the optimizer cannot use. Not a slow index — an absent one: queries silently full-scan and DML raises ORA-01502. Checks `dba_ind_partitions` too, because a partitioned index carries its state per partition and reads `N/A` at the index level, which is exactly what partition maintenance breaks | hourly |
| `INVALID_OBJECTS` | oracle (all releases) | Stored code that will not compile, grouped by owner and object type. `collect_only`: the first real run found 76 already-invalid objects in one schema, and a standing backlog that alerts every cycle is the pattern `collect_only` exists to stop. Turn `collect_only` off once a schema is clean and a newly invalidated object becomes a real alert | hourly |
| `TOP_SEGMENT_SIZE` | oracle (all releases) | The largest tables and indexes by allocated size. `TABLESPACE_FREE_SPACE` answers "is there room left"; this answers "what used the room", which had no metric on any Oracle release. Status always OK — the value is the trend across runs | 6-hourly |
| `SEGMENT_EXTENT_LIMIT` | oracle 8i only | Dictionary-managed segments approaching `MAX_EXTENTS` | 6-hourly |
| `MAINTENANCE_INDEX_USAGE` | sqlserver | Indexes with writes and no reads, plus the optimizer's missing-index suggestions | nightly |
| `DATABASE_VLF_COUNT` | sqlserver 2016+ | Virtual log files per transaction log — the real "database-level fragmentation" on SQL Server | daily |
| `DATABASE_DATA_SIZE` | sqlserver, postgresql, mysql | Allocated size of each database, in GB | daily |
| `DATABASE_LOG_SIZE` | sqlserver | Allocated size of each transaction log, in GB | daily |
| `POSTGRES_TABLE_BLOAT` | postgresql | Dead tuples as a share of the table — really "autovacuum is behind on this table" | 12-hourly |

Three things about this set are easy to get wrong:

- **Heaps needed their own metric, not a filter change.** `forwarded_record_count` is NULL in
  `LIMITED` mode, which is the cheap mode the nightly index scan depends on. Reading it needs
  `SAMPLED` (1% of pages), so folding heaps into `MAINTENANCE_INDEX_FRAGMENTATION` would have
  made the working scan slow for every instance. Separate metric, separate schedule.
- **`DATABASE_DATA_SIZE` / `DATABASE_LOG_SIZE` have a fixed contract**: `metric_item` is the
  **database name** and `metric_value` is a plain number of GB. The inventory report matches on
  the item and parses the value with `float()` — a thousands separator or a unit suffix in the
  value makes the column read blank. (These two codes were consumed by
  `reports/inventory_health.py` long before any metric produced them, which is why the size
  columns were always empty.)
- **`MAINTENANCE_INDEX_USAGE` has two uptime thresholds, not one.** The usage DMVs reset on
  restart, so a short window makes a busy index look unused. Below **12 hours** of uptime the
  per-index detail is suppressed outright and only the summary is emitted. Between 12 hours and
  **7 days** the detail *is* reported, and every DROP recommendation carries the sample length
  inline (`— BUT the sample covers only N day(s) since <restart>`); the summary row is marked
  `SHORT SAMPLE` and `index_report.py` prints a "do not drop anything" banner above the counts.
  The two thresholds answer different questions — "is there any signal" and "is the signal safe
  to act on" — and collapsing them into one is how an instance that had just failed over showed
  nothing at all for a week. Every row states the uptime it is based on.

Size vs space are different questions and both exist: `DATABASE_DATA_SIZE` is what the files
**occupy**, `STORAGE_DATA_FILE_SPACE` is how **full** those files are. Reading them together is
what shows free space trapped inside a file.

## OS Metrics

Thirteen OS metric codes exist under `category: "os"`:

| Code | Rows it returns |
| --- | --- |
| `OS_INFO` | `os_name` (family/edition/version/build/architecture in the message), `hostname`, `timezone`, `last_boot_time` (+ `uptime_seconds`) |
| `OS_CPU_USAGE` | `cpu_usage` % (+ model/sockets/cores/logical_cpus), and `load_average` on Linux / `processor_queue_length` on Windows |
| `OS_MEMORY_USAGE` | `memory_usage` % of **physical** memory (+ total/used/available GB), and `swap_usage` / `pagefile_usage` |
| `OS_DISK_USAGE` | one row per mount/drive: used % (+ total/used/free GB, free %, filesystem, label), plus **host-wide** IO rows — `disk_read_kbps` / `disk_write_kbps` on both, `disk_iops` on Linux, `disk_queue_length` on Windows (see below) |
| `OS_NETWORK` | one row per interface: IP address (+ link, speed, bytes sent/received, errors, dropped) |
| `OS_PROCESS_TOP_CPU` | one row per process, ranked by current CPU % (Windows: `% Processor Time` divided by logical CPUs, not cumulative CPU seconds) |
| `OS_PROCESS_TOP_MEMORY` | one row per process name, ranked by resident memory; processes with the same name are summed (`process_count` in the message) |
| `OS_SERVICE_STATUS` | one row per configured service (see `collector_env` below) |
| `OS_EVENTLOG_CRITICAL` | Windows: one row per event log with the count of Critical/Error events in the window. Linux: journald priority ≤ 3. Both read at most `OS_EVENTLOG_MAX_EVENTS` (500) newest events — see below |
| `OS_REBOOT_PENDING`, `OS_UPTIME`, `OS_TIME_SYNC` | as before |
| `OS_TCP_PORT_STATUS` | one row per configured port: `OPEN` / `LOOPBACK_ONLY` / `CLOSED` (see below) |

### `OS_TCP_PORT_STATUS`: which address the probe knocks on

The check tested `127.0.0.1` and nothing else, which cannot separate two different worlds:
nothing is listening, and something is listening but bound to the address clients actually use.
Both read `CLOSED`, and only the first is an outage. The reverse blind spot was there too — a
service bound to loopback is reachable by the probe and by nobody else, and that read `OPEN`.

So the probe asks the host address first and uses loopback to *explain* a failure rather than to
produce one:

| Host address | Loopback | Value | Status | Means |
| --- | --- | --- | --- | --- |
| open | — | `OPEN` | OK | clients can reach it |
| closed | open | `LOOPBACK_ONLY` | WARNING | serving, but not where clients knock — a bind-address problem |
| closed | closed | `CLOSED` | CRITICAL | nothing is listening |

`LOOPBACK_ONLY` is deliberately not CRITICAL: a service bound to loopback on purpose (a local
agent, a tunnelled port) is configured that way, not broken.

The host address comes from **`DB_OPS_TARGET_HOST`**, injected into every cmd collector from
`cmd_access.host` (falling back to `ip`) — the address the collector itself already reached the
host on. A script never restates what the inventory records, and never guesses from the host's own
NICs, which picks the wrong one on a multi-homed box. A target that means something else by it —
clients arriving on a VIP — can set it in `collector_env`, which wins.

**Entry syntax: `[host:]port[/scheme]`**, scheme being `tcp` (default), `tls`, or `https`.

TCP says a socket accepted the connection, not that the listener is serving: an AOS that accepts
on 443 and answers 503 to everything is indistinguishable from a healthy one at that layer.
`443/https` adds a TLS handshake (reporting `cert_expires`) and one GET; `443/tls` stops after the
handshake. Any HTTP status proves it is serving — a 302 to a sign-in page or a 401 is a working
front door — so only 5xx counts as a failure.

**`/https` needs an endpoint whose root path answers.** All four ERP AOS nodes return **HTTP 500
on `/`** — that is the D365 F&O AOS's baseline behaviour, not an outage, and configuring `443/https`
against them produced four permanent WARNINGs on the first collection. They run `443/tls` instead:
the handshake proves the listener is serving TLS without assuming anything about routing. Use
`/https` only where a root request is known to answer, or against a real health path.

**A deep-probe failure is WARNING, never CRITICAL**, and the row stays `OPEN`: the socket *is*
open, so the finding is "open but not serving". TLS and HTTP bring their own ways to fail that
have nothing to do with the service being down (client certificates, SNI, a proxy in the path, a
protocol floor the host does not offer), and TCP is the signal this metric has always been right
about. Raise it once a given endpoint has proven the probe is reliable against it.

The cadence is 900s. It was 7200s until 2026-08-07, when the ERP AOS on 192.0.2.119 dropped 443
and the first alert arrived roughly two hours later — a front door's outage lasts until the next
collection, so the collection interval *is* the detection latency.

### `OS_DISK_USAGE`: drive rows vs host-wide IO rows

The metric mixes two kinds of row, and reading one as the other is the mistake to avoid:

- **Per drive/mount** — `C:`, `D:`, `/`, `/var`. `metric_value` is used %; the message carries
  the GB figures. These are the rows the reports treat as disks.
- **Host-wide IO** — `disk_read_kbps`, `disk_write_kbps`, `disk_iops`, `disk_queue_length`.
  These are **not** mount points. `inventory_health.build_os_health` filters them out of
  `os_health.disks` by item name; a reader that does not would list a drive called
  `disk_read_kbps` whose "free space" is a throughput number.

**`disk_queue_length` is a sum over every physical disk, not one disk.** It comes from the
Windows `PhysicalDisk` `_Total` instance (`Win32_PerfRawData_PerfDisk_PhysicalDisk`), so on a
host with four disks it is the four queues added together. Two consequences:

- A fixed threshold cannot be right for every host — 16 outstanding requests is unremarkable on
  an eight-disk array and a stalled host on a single disk. The collector therefore scales the
  threshold by the physical-disk count: **WARN at 2 × disks, CRITICAL at 4 × disks** (the usual
  "~2 outstanding requests per disk" rule of thumb).
- The message states the scope explicitly, so an alert is readable on its own:
  `physical_disks`, `avg_per_disk`, `warn_at`, `critical_at`, and a `Per disk: <instance>=<queue>`
  breakdown naming the busiest disk first.

Note **physical** disks, not volumes: `C:` and `D:` often share one spindle, in which case they
appear as a single instance (`0 C: D:`) and `physical_disks=1`.

The value is one **instantaneous** sample taken once per collection interval (hourly by
default), not an average over the interval — a single high reading is a spike, not a trend. Use
the report's history chart before acting on one sample.

`OS_TOP_PROCESSES` is **retired** (`active: false`): it returned a single row whose `metric_value` was a JSON blob, which cannot be queried, thresholded, or reported per process. `OS_PROCESS_TOP_CPU` and `OS_PROCESS_TOP_MEMORY` replace it. Its history stays in `metric_results`.

These are `collector_type: "cmd"` metrics. Each has two variants — `windows_powershell` (`.ps1`) and `linux_bash` (`.sh`) — selected by `target.platform`. Scripts live under `db_ops/metrics/collectors/os/windows/` and `db_ops/metrics/collectors/os/linux/`.

**Target requirements:**

- `platform` must be set to `"windows"` or `"linux"` on the target.
- `cmd_access.enabled: true` and a valid `method` must be configured.
- A target with **no database** (`db_type: null`) runs only these cmd collectors: no SQL variant matches an empty db_type, so no database collector is dispatched to it.

**Per-target collector input (`collector_env`)**

A cmd script reads its inputs from environment variables. They are declared per target in `db_instances.json` and injected into the script before it runs (as `$env:NAME = '...'` for PowerShell, `export NAME='...'` for bash), so the same script serves every host:

```json
"metrics": {
  "enabled": true,
  "collector_env": { "OS_TOP_N": "5", "OS_EVENTLOG_HOURS": "24", "OS_TCP_PORTS": "2712" },
  "metric_overrides": {
    "OS_SERVICE_STATUS": {
      "collector_env": { "OS_SERVICE_NAMES": "*Dynamics AX Object Server*,W32Time" }
    }
  }
}
```

`DB_OPS_TARGET_HOST` is added to every cmd collector's environment automatically, from
`cmd_access.host` (falling back to `ip`) — see `OS_TCP_PORT_STATUS` above. It is the only injected
value; everything else is declared. Listing it in `collector_env` overrides it.

`metrics.collector_env` applies to every cmd metric on that target; `metric_overrides.<CODE>.collector_env` applies to one metric and wins on conflict. `OS_SERVICE_NAMES` accepts a service name, a display name, or a wildcard pattern (each match becomes its own row). Values come from the target config only — a key that looks like a secret (`password`, `token`, `key`, …) is rejected, so nothing sensitive can reach a script, a metric payload, or a log line.

**Secrets a script genuinely needs (`env_secrets`)**

A few collectors cannot work without a credential. `ORACLE_RESTORE_VALIDATION` is the case that
forced this: the RMAN backups are written with `SET ENCRYPTION ON IDENTIFIED BY ... ONLY`, so
`restore database validate` without the passphrase fails with `ORA-19913 unable to decrypt backup`
about three seconds in — and the metric reported CRITICAL on a database whose backups were
provably restorable.

`env_secrets` maps an env var to a **ref** in the encrypted store; only the ref is ever written to
config. Same field name and same contract as `restore_config.json`, so one concept covers both apps:

```json
"metrics": {
  "enabled": true,
  "env_secrets": { "BACKUP_ENCRYPTION_PASSWORD": "TOKEN_203_0_113_188_BACKUP_ENC" }
}
```

It is accepted target-wide and under `metric_overrides.<CODE>`, the per-metric block winning. A ref
that is not in the store raises rather than running the script with an empty value — a validation
that silently skips decryption is the false CRITICAL this feature exists to end. The collection run
needs the store key (`--key-base64`, or `DB_OPS_SECRET_KEY`, which the daemon already forwards).

**Execution paths:**

| Method | Platform | What runs |
| --- | --- | --- |
| `local` | windows | `powershell.exe -File <script>.ps1` on the collector machine |
| `local` | linux | `bash <script>.sh` on the collector machine |
| `ssh` | windows | SSH to target (OpenSSH Server), script sent inline as `powershell -EncodedCommand <base64>` |
| `ssh` | linux | SSH to target, script piped via `bash -s` stdin |
| `winrm` | windows | **Legacy.** `pypsrp` if installed; otherwise `Invoke-Command` via local `powershell.exe`. Prefer `ssh`. |

**SSH transport (primary for Windows and Linux remote targets):** Uses `paramiko` (listed in `requirements.txt`). Supports `auth_type: password` or `auth_type: key` (default). For Windows targets, the script is base64-encoded and sent as a single inline `-EncodedCommand` argument — the script file never needs to exist on the remote host. For Linux targets, the script is piped to `bash -s` stdin. SSH requires OpenSSH Server on Windows targets; see the setup subsections below.

**WinRM dependency (legacy):** `pypsrp` is retained in `requirements.txt` for existing WinRM targets. If it is not installed, `execute_winrm` falls back to a native `powershell.exe -EncodedCommand` wrapper that calls `Invoke-Command`. This fallback is Windows-only. Migrate Windows targets to `method: ssh` where possible.

**Script output contract:** Each script must output a JSON array of objects with `metric_item`, `metric_value`, `metric_unit`, `status`, and `message` fields. On error, scripts should catch the exception and output a single row with `status: "UNKNOWN"` rather than exiting non-zero.

### Windows — Enable OpenSSH Server

Run these commands in an **elevated PowerShell on the target server**:

```powershell
# Install (Windows 10 / Server 2019 and later)
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

# Firewall — verify or add rule
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
    -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22

# Set default shell to PowerShell 5 (built-in)
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
    -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -PropertyType String -Force
# Or PowerShell 7 if installed: -Value 'C:\Program Files\PowerShell\7\pwsh.exe'
```

Password auth is enabled by default (`C:\ProgramData\ssh\sshd_config`). For key-based auth:

```powershell
$pubKey = Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub"
$authorizedKeysPath = "C:\ProgramData\ssh\administrators_authorized_keys"
Add-Content -Path $authorizedKeysPath -Value $pubKey
icacls $authorizedKeysPath /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```

### Linux / Ubuntu — Enable SSH

```bash
sudo apt update && sudo apt install -y openssh-server
sudo systemctl enable ssh && sudo systemctl start ssh
sudo ufw allow 22/tcp   # if UFW is active
```

Key-based auth: `ssh-keygen -t ed25519 && ssh-copy-id user@target-host`

### Target `cmd_access` Config

`cmd_access` fields in `data/db_instances.json`:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `enabled` | yes | — | `true` to enable OS metric collection. |
| `method` | yes | — | `ssh` (primary) \| `local` \| `winrm` (legacy) |
| `host` | no | target `ip` | Hostname or IP for SSH. |
| `port` | no | `22` (ssh), `5985` (winrm) | Port number. |
| `shell` | no | inferred from `platform` | `powershell` or `bash`. |
| `auth_type` | no | `key` | `password` or `key`. SSH only. |
| `key_file` | no | — | Private key path (key auth). Omit to use SSH agent / default key. |
| `credential_name` | cond. | — | Entry in `users.json` (`remote_credentials`). Required for `auth_type=password` and WinRM. |

Example — Windows target with password auth (192.0.2.250):

```json
{
  "platform": "windows",
  "cmd_access": {
    "enabled": true,
    "method": "ssh",
    "host": "192.0.2.250",
    "port": 22,
    "shell": "powershell",
    "auth_type": "password",
    "credential_name": "remote_100.250_svc_backup"
  }
}
```

Example — Linux target with key auth:

```json
{
  "platform": "linux",
  "cmd_access": {
    "enabled": true,
    "method": "ssh",
    "host": "192.0.2.111",
    "port": 22,
    "shell": "bash",
    "auth_type": "key",
    "credential_name": "remote_100.111_dbops"
  }
}
```

### Manual Test Commands

Test SSH connectivity before running metrics:

```bash
# Windows (192.0.2.250)
ssh svc_backup@192.0.2.250 "hostname"
ssh svc_backup@192.0.2.250 "powershell -NoProfile -Command \"Get-Date\""
ssh svc_backup@192.0.2.250 "powershell -NoProfile -Command \"Get-CimInstance Win32_OperatingSystem\""

# Ubuntu / Linux
ssh user@host "hostname"
ssh user@host "uptime"
ssh user@host "df -h"
```

Run a single OS metric via the CLI:

```powershell
# Dry run — confirms target and metric are active without executing
python -m db_ops.metrics.cli --config config.json collect --dry-run --target-ip 192.0.2.250 --metric-code OS_CPU_USAGE

# Live collection — writes the result to the runtime store
python -m db_ops.metrics.cli --config config.json collect --target-ip 192.0.2.250 --metric-code OS_CPU_USAGE --force
python -m db_ops.metrics.cli --config config.json latest --limit 10
```

### Troubleshooting OS SSH Metrics

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `SSH authentication failed` | Wrong username/password or key not authorized | Verify `credential_name`, check `users.json` (`remote_credentials`), test `ssh user@host` manually |
| `SSH connection to host:22 failed` | Port 22 closed or sshd not running | `Get-Service sshd` on target; check firewall rule |
| `SSH command timed out` | Slow network or slow command | Increase `time_window.timeout` in `metric_definitions.json` |
| `Command stdout is not valid JSON` | PowerShell error instead of JSON output | SSH to target and run the PowerShell command manually to see the raw error |
| `cmd_access.credential_name is required` | `auth_type=password` but `credential_name` missing | Add a `credential_name` entry in `users.json` (`remote_credentials`) |
| WinRM failing on other hosts | Host not yet migrated to SSH | Change `method` to `ssh` and enable OpenSSH Server on that host |

## Scheduling — `repeat_interval` vs `retry_interval`

Each metric's `time_window` drives when it is due, the same way `app_commands.json` does:

- **`repeat_interval`** — after a **successful** collection, the metric is due again this many seconds later (unchanged behavior).
- **`retry_interval`** (default **600s**) — after a **failed** collection (the most recent attempt produced only errors, or the metric never succeeded), the metric is due again after this shorter/longer back-off instead of repeat_interval. This stops a broken metric from retrying on every collector scan.

Both are per-metric and optional in `data/metric_definitions.json`'s `time_window`; omit `retry_interval` to use the 600s default. "Failed" is judged from `metric_results`: the last collection is a failure when the newest row is newer than the last non-error row for that `(target, metric_code)`.

### A metric's interval is a floor, not a promise — the pass has to reach it

`repeat_interval` only says when a metric *becomes* due. It is collected when the pass gets to it,
so the real cadence is the interval **plus** however long the pass takes to come round again. A
metric asking for 150s was measured being sampled every 200–830s for this reason.

The pass therefore walks **server_id groups in parallel, and the metrics inside one group one at a
time**, capped by `collection.max_parallel_servers` in `data/metric_definitions.json`:

```json
"collection": { "max_parallel_servers": 16 }
```

`server_id` is the unit, not `target_id`, because several targets can name one machine (a database
instance and the OS metrics reached through its `cmd_access`); two workers on one box would stack
WinRM/SSH sessions and metric queries on it. Set it to `1` to restore the old fully serial pass; a
missing or malformed `collection` block also means 1, so a bad edit degrades rather than fans out.

Why it matters: a pass used to be one queue over every target, so its cadence was the sum of all of
them and one unresponsive host set the pace for the estate. A WinRM call to an ERP application
server was measured holding a pass for 490 of its 598 seconds, and in that window the ERP
*database's* lock metrics were never sampled — a blocking chain grew from 21s to 570s and no
CRITICAL was raised, because the check that would have raised it never ran.

Each worker uses its own store connection (every `MetricStore` method opens one), fills its own
counters, and the parent merges them in submission order, so the run summary reads the same however
the hosts behaved.

**Measured on the live estate**, before and after the change:

| | Serial pass | Parallel, cap 16 |
| --- | --- | --- |
| Pass wall time | 138–598 s | **25–71 s** |
| Servers collecting at once | 1 | **16** (peak, = the cap) |
| `LOCK_BLOCKING_SESSIONS` sampling gap | 192–830 s, erratic | 226–236 s, avg 230 s |

In one 19-server pass the sum of the per-server collection windows was 300 s while the pass itself
took 31 s — the compression is the point. What was fixed is not throughput but the **blind spots**:
the old gaps were erratic because a single slow host stalled the queue, and a ten-minute hole is
where an incident escalates unobserved.

The residual ~230 s gap on a metric declaring `repeat_interval: 150` is not the collector lagging.
It is quantisation: the daemon relaunches `APP-METRICS` every 120 s, a metric only becomes due 150 s
after its last collection, and a pass that finds it at 130 s skips it until the next launch. To get
closer to a declared interval, lower the `APP-METRICS` `repeat_interval` in `data/app_commands.json`
— raising the parallel cap will not help, because the pass already finishes well inside its window.

### A metric's `timeout` is enforced by wall clock

`time_window.timeout` bounds a single metric's collection on every transport. The WinRM path needed
a guard of its own: none of pypsrp's timeouts bound how long a *command* runs
(`connection_timeout`/`read_timeout` bound one HTTP round trip, `operation_timeout` one WSMan
Receive), so `execute_ps` polls for as long as the remote script keeps going — which is how a
metric declaring `timeout: 60` ran for 490 seconds. The call now runs in a daemon thread that is
abandoned on timeout and reported as `RemoteTimeoutError`; pypsrp offers no cancel, and waiting for
it would reintroduce the stall.

Measured on the same host and metric: 490.1 s before, **exactly 60.0 s** after, reported as
`WinRM command timed out after 60 seconds on 192.0.2.116`. Note what this changed for the
operator — the host's event log genuinely is slow to query (26,152 events of one id), and that is
now a visible WARNING every pass instead of an invisible eight-minute stall for the whole estate.

## `LOCK_SLEEPING_OPEN_TRANSACTION`: sleeping is an instant, not a duration

`sys.dm_exec_sessions.status = 'sleeping'` means only that the session has no request running *at
the moment of sampling*. SQL Server sets it as soon as a statement finishes and the connection waits
for the client's next command. D365 F&O opens a TTS block and then issues many small statements with
application think-time between them, so most sessions inside a transaction sample as sleeping with
`idle_seconds = 0`. They are working, not abandoned.

Letting blocking alone decide severity therefore misfires. It raised a CRITICAL for a session whose
`last_request_end` was *later* than the report header it appeared in, mid-`INSERT`, and it was a
duplicate besides — `LOCK_BLOCKING_SESSIONS` already reports live blocking chains, which is its job.

So idle time is a **required** condition here: below `@idle_abandoned_seconds` (60 s) the session is
between two of its own statements and blocking belongs to `LOCK_BLOCKING_SESSIONS`; above it, this
metric owns the finding. Separately, the SQL now carries `transaction_begin_time`, because the one
case neither metric could see is a session busy enough that `idle_seconds` never rises while holding
a single transaction open for half an hour — every idle rule reads healthy and the locks are held
throughout. That is graded at `@tran_age_critical_seconds` (1800 s) and reported as
`tran_age_seconds` / `tran_begin` in every row.

**The shared `severity_policy` in `data/metric_definitions.json` does not apply to this variant.**
Its rules grade `idle_minutes` / `is_blocking` / `has_x_lock`, which the Oracle and legacy-2008R2
variants emit deliberately; the modern SQL Server variant emits `idle_seconds` / `blocked_sessions`
and grades itself in SQL. Editing those thresholds will not change SQL Server behaviour — change
`db_ops/metrics/collectors/sqlserver/024_sqlserver_sleeping_open_transaction.sql`. The split is pinned by
`tests/test_sleeping_open_transaction_grading.py` so it cannot rot into a silent no-op.

## Common Issues

- No rows collected: run `collect --dry-run` and confirm targets and metrics are active and due.
- Connection failures: check target credential names and secret references.
- Missing metric file: confirm the variant `file` in `data/metric_definitions.json` exists under `db_ops/metrics/collectors/`.
- Command failures: check `target.platform`, `cmd_access.method`, `cmd_access.credential_name`, and `data/users.json`.
- Reports show old data: check `metric_results.collected_at` and whether the metric was skipped by interval rules.

## Config Priority

The metrics app resolves its config file using this chain:

1. `--config <path>` CLI argument.
2. `DB_OPS_METRICS_CONFIG` environment variable.
3. `config.metrics.json` next to `config.json`, or in the current working directory.
4. `config.json` shared fallback.

The selected source is printed to stderr on startup.

App-specific config file: `config.metrics.json`

## Standalone Mode vs Full-Suite Mode

**Full-suite mode** (default): the app reads `config.json` and `data/db_instances.json` alongside all other apps. `metric_results` are read by the reports and SLA apps from the shared runtime store.

**Standalone mode**: copy `config.metrics.json`, `data/db_instances.json`, `data/metric_definitions.json`, and relevant `db_ops/metrics/collectors/` files next to the EXE. Point the store at a local path - a standalone EXE is the one layout where `sqlite_path` is still the natural setting, because it has no shared server. The reports and SLA apps will not have access to metric data unless they read the same store.

Required config keys: `log_dir`, plus a resolvable runtime store (`store_config_file`, an inline `store` block, or `sqlite_path`).

## PostgreSQL: catalog, privileges, and the backup metric

Folded in from the former `13_postgresql_metrics_and_sla.md` on 2026-08-15. It was the only doc
in `docs/` that was neither an app nor the shared layer — an engine slice split across two apps,
which meant the PostgreSQL half of the metrics contract was documented away from the metrics
contract. The SLA half moved to [`09_sla_slo_compliance_app.md`](./09_sla_slo_compliance_app.md).

### Least-privilege setup

Use a dedicated login with `CONNECT` on the monitoring database and grant `pg_monitor` where
session, replication, WAL receiver, or settings visibility is required. Catalog-only metrics may
work with fewer rights. Superuser is not required, and no optional extension is. SQL text,
`archive_command`, passwords, and connection strings are never emitted.

```sql
CREATE ROLE dbops_monitor LOGIN PASSWORD '<set-outside-source-control>';
GRANT CONNECT ON DATABASE postgres TO dbops_monitor;
GRANT pg_monitor TO dbops_monitor;
```

Store the password only through the encrypted secret reference `db_instances.json` names — never
in the inventory file itself.

### Catalog

PostgreSQL SQL supports versions 10 and later. Version-dependent `pg_stat_io`, newer
replication-slot columns, logical-replication error columns, and exact bloat scans are not
collected.

| Metric code | Description | Unit | Source | Role | Min version | Privilege | Cost | Default | SLA domain |
| --- | --- | --- | --- | --- | ---: | --- | --- | --- | --- |
| `POSTGRES_IDENTITY_ROLE` | Version, role, read-only state, uptime | status | settings/functions | both | 10 | CONNECT | LIGHT | enabled | availability |
| `POSTGRES_REPLICATION` | Primary streams or standby replay health | bytes/status | `pg_stat_replication`, recovery functions | both | 10 | pg_monitor | LIGHT | enabled | replication |
| `POSTGRES_REPLICATION_SLOTS` | Activity and retained WAL | bytes | `pg_replication_slots` | primary | 10 | pg_monitor | LIGHT | enabled | replication/capacity |
| `POSTGRES_WAL_ARCHIVE` | Archive successes and failures | count | `pg_stat_archiver` | both | 10 | pg_monitor | LIGHT | enabled | data protection |
| `POSTGRES_BACKUP_LAST_RESULT` | Newest FULL / DIFF / LOG backup, one row per type | hours | `$PG_BACKUP_DIR/base/*_FULL\|_INCR` + `pg_stat_archiver` | primary | 10 | docker + pg_monitor | NORMAL | enabled/60m | backup |
| `INSTANCE_CONNECTIONS` | Connection saturation | sessions | `pg_stat_activity` | both | 10 | pg_monitor | LIGHT | enabled | capacity |
| `LOCK_BLOCKING_SESSIONS` | Bounded blocking detail | seconds | `pg_stat_activity` | both | 10 | pg_monitor | LIGHT | enabled | performance |
| `POSTGRES_LONG_TRANSACTIONS` | Bounded old transaction detail | seconds | `pg_stat_activity` | both | 10 | pg_monitor | LIGHT | enabled | operational health |
| `POSTGRES_VACUUM_HEALTH` | Dead tuples and vacuum freshness | count | `pg_stat_user_tables` | both | 10 | CONNECT | NORMAL | enabled/30m | operational health |
| `POSTGRES_XID_WRAPAROUND` | Frozen-XID age against freeze limit | transactions | `pg_database` | both | 10 | CONNECT | LIGHT | enabled | integrity |
| `POSTGRES_DATABASE_HEALTH` | Size and database counters | bytes | `pg_stat_database` | both | 10 | CONNECT | LIGHT | enabled/15m | capacity |
| `POSTGRES_INVALID_INDEXES` | Invalid/not-ready indexes, limited to 50 | boolean | `pg_index` | both | 10 | CONNECT | HEAVY | enabled/hourly | integrity |

High-cardinality queries are bounded and normal collection does not expose query text. Disable any
metric per target with `metrics.metric_overrides.<CODE>.enabled=false`;
`report_policy.disabled_metric_codes` stays canonical for broad report/OS exclusions. Numeric
defaults can be replaced per target with `warning_threshold`, `critical_threshold`, and
`higher_is_worse` under the same override. The PostgreSQL statement timeout is set from the metric
definition's timeout before the SQL runs.

### `POSTGRES_BACKUP_LAST_RESULT` — a **docker** collector, not SQL

A base backup is a directory on disk, and `pg_stat_archiver` is the only backup fact reachable
over a connection — so this metric reads the filesystem, not the database. WAL archive health
alone does not prove recoverability.

- It emits the same message contract as the SQL Server `BACKUP_LAST_RESULT` (`database=`,
  `recovery_model=`, `backup_type=`, `backup_finish_date=`); both codes are listed in
  `db_ops.lib.backup_policy.BACKUP_LAST_RESULT_CODES`, so the policy engine and the fleet
  report read them as one metric.
- `PG_BACKUP_DIR` (the target's `collector_env`) must match the `backup_dir` that cluster's backup
  job writes to in `restore_config.json`. A mismatch makes the metric report "never backed up" for
  a cluster that is backed up nightly — the ACME lab uses `/opt/pgbackup/dbops`, the CLOUD lab
  `/var/lib/postgresql/backup/dbops`.
- It is turned **off** on streaming replicas
  (`metric_overrides.POSTGRES_BACKUP_LAST_RESULT.enabled: false`). A replica holds no base backup
  of its own, so it would truthfully report "never backed up" and paint the fleet card critical
  for a cluster whose backups run on the primary. Backup coverage is a property of the primary.
- `archive_mode` is mapped to the SQL Server recovery model whose meaning matches (`on`/`always` →
  `FULL`, `off` → `SIMPLE`) so one `backup_policy.json` covers every engine — that mapping is what
  makes a stalled archiver an RPO violation rather than silently "not required".

### Not collected yet

Logical replication, `pg_stat_statements`, `pg_stat_io`, restore-test ingestion, capacity
forecasting, and exact bloat analysis.

## Optional Integrations

The metrics app has no optional integrations. It writes to the runtime store and exits. Other apps (reports, SLA) consume its output independently.

## EXE Packaging Notes

- `DEFAULT_DATA_DIR` and `DEFAULT_DEFINITIONS_PATH` are resolved relative to the Python package location. Pass `--data-dir` explicitly when running outside the repo.
- Secrets (`data/encrypted_secret_text.json`, decrypted at runtime with the `--key_base64`/`--key` passphrase or `DB_OPS_SECRET_KEY`) and inventory (`data/db_instances.json`) are resolved from the `data/` directory via `db_ops/common/data_sources/`. Both degrade gracefully to empty if absent — targets relying on them will fail to connect.
