# Reports App

## Purpose

The Reports App builds manual or scheduled reports from collected metric data and queues report messages for delivery through Telegram.

## Package / Files

- `db_ops/reports/`
- `data/reports_config.json`
- the runtime store declared in `data/store_config.json` (PostgreSQL in this tree; `runtime/db_ops.sqlite` when the backend is `sqlite`)

## Index usage report (`rp_index_usage_by_server`) — one per server_id

`MAINTENANCE_INDEX_USAGE` emits one row per index, around 29,000 for a single large database. That
volume fits nowhere else, so it gets its own report:

| Destination | What it carries | Why |
| --- | --- | --- |
| hourly warning/critical | **nothing** | `report_policy.collect_only` — maintenance work is not an alert |
| per-server chart series | the aggregate only | `report_policy.chart_summary_only` — there is no time series in "never read" |
| inventory summary | the counts | *how much* dead weight exists |
| **this report** | the indexes themselves | *which* indexes, and what to do about each |

**On Oracle the same page is an inventory, not a usage report, and it says so in its own title.**
Oracle records no per-index seek/scan counters unless every index is individually placed into
`ALTER INDEX ... MONITORING USAGE` — a change to the database, not to the monitor. So Oracle has
its own metric (`INDEX_INVENTORY`) and its own renderer
(`index_report._format_oracle_index_report`), and the page carries no usage columns and **no drop
candidates**: filing Oracle rows under the usage metric would render several hundred zero-seek
indexes that look unused and are not, which is the reading that gets an index dropped and a report
query broken the following month. What it does carry is what Oracle states on its own — what
exists, its size, whether it is still UNUSABLE, and how old its statistics are, with "never
analyzed" listed separately from "stale" because on 8i the first means the optimizer is costing
that object from defaults.

**PostgreSQL shares the SQL Server page rather than getting its own.** It is the one engine whose
catalog answers the question properly: `pg_stat_user_indexes.idx_scan` is a real per-index read
counter, and from PG 16 `last_idx_scan` says *when* it was last read — which SQL Server cannot
tell you at all. Its SQL (`db_ops/metrics/collectors/postgresql/072_postgresql_index_usage.sql`) emits the
same field names, so the parser and renderer are shared; only the engine-specific words differ,
and each of those is a way to print an instruction that does not work:

| | SQL Server | PostgreSQL |
| --- | --- | --- |
| Drop rule excludes | `type_desc <> NONCLUSTERED` (the clustered index *is* the table) | nothing for that reason — there is no clustered index |
| `type_desc` | `CLUSTERED` / `NONCLUSTERED` | the access method: `btree`, `gin`, `brin` |
| `is_disabled` carries | a disabled index | an **INVALID** one: the planner ignores it, every write still maintains it |
| Fix for that | `ALTER INDEX … REBUILD` | **DROP and re-create** — PostgreSQL has no REBUILD |
| Constraint-backed index | `DROP INDEX` works | `DROP INDEX` **fails**; the constraint owns it, so `DROP CONSTRAINT` |
| Counters reset on restart | **yes** — hence the short-sample warnings | **no**; they run to `pg_stat_reset()`, and `stats_reset` says when |

Keying the drop rule on the literal `NONCLUSTERED` is what made this worth a test rather than a
line of code: on PostgreSQL it matches nothing, so the page reported `droppable: 22` in its totals
and listed none of them.

**Scope is every database on the target.** PostgreSQL cannot read another database's catalog from
one connection, so the variant is declared `per_database` and the collector runs the SQL once per
database (see [`04_metrics_engine.md`](04_metrics_engine.md)). Every summary row still names the
database it covered, because the rows arrive interleaved from several connections.

The SQL therefore emits **no cluster-wide summary row** — running per database, it would be
written once per database under the same `metric_item`, and this parser keeps whichever arrived
last. On the PGLAB cluster that was three rows each claiming to be the server total, the last of
them describing a database with one index. `_fill_totals_from_databases` builds the server totals
by adding the per-database summaries instead, and only where the engine emitted none of its own —
SQL Server's is authoritative and must not be doubled by adding its per-database rows on top.

Published by **`inventory-workflow --beauty 1`**, the same step that writes
`database-inventory.html` and `server-metrics.html`. All three are pages that land in the directory
the webhost serves, so they share one schedule (`APP-REPORTS-INVENTORY-WORKFLOW`). It briefly had a
scheduled command of its own; that meant two commands reading the same store on two clocks to write
into the same directory, which can only disagree about how fresh "the reports" are.

The standalone command remains for ad-hoc use — one server, or a different window:

```bash
python -m db_ops.reports.cli --config config.json index-usage-report [--days 3] [--limit 25] [--server-id ...]
```

Rendered as tables, one recommended action **per row** — the right action differs inside a section,
so a section-level heading cannot carry it.

Two rules decide what is listed:

- **Never recommend dropping something that enforces a rule.** A primary key, a unique constraint
  and the clustered index can show zero seeks for years and still be doing their job. They are
  counted, and alerted on when disabled, but never offered as drop candidates. `droppable` is
  therefore much smaller than `cold` — on one instance, 67 against 511.
- **A disabled CLUSTERED index is an incident.** The table is unreadable until it is rebuilt, so the
  whole report is raised to `critical` for that server and the finding is listed first.

**The published page lists every index, uncapped**, including the healthy ones — a reader asking
"is THIS index used?" needs the index they came to look up, not only the problems. Each row carries
its own recommended action, `KEEP — primary key` as readily as `review, then DROP`.

The **stored** copy of the report keeps `--limit`, because that one is a database row that also
feeds Telegram, where a 29k-row body is neither storable nor sendable. Two renderings, one source.

### Running it by hand

The reports CLI accepts `--key` / `--key-base64`, like every other db_ops CLI. Without them a
manual run could not open the encrypted store: the daemon forwards `DB_OPS_SECRET_KEY` to the
children it starts, but `docker exec` does not inherit it, so forcing a report by hand was
impossible.

```bash
python -m db_ops.reports.cli --key-base64 "<b64>" --config config.json index-usage-report
python -m db_ops.reports.cli --key-base64 "<b64>" --config config.json inventory-workflow --days 7 --beauty 1
```

### Where to read it

Each run publishes an HTML copy into `<runtime>/reports`, which is the directory the webhost already
serves, so the report has a URL rather than only a store row:

```
http://192.0.2.249:8080/report_dba/index-usage_<slug>.html
```

Every report carries two links at the top — the server's metrics dashboard, and the fleet inventory:

```
Server dashboard: http://192.0.2.249:8080/report_dba/server-metrics.html?server=acme-192-0-2-248
Fleet inventory:  http://192.0.2.249:8080/report_dba/database-inventory.html
```

Both point away from the page, sideways and up. A "This report:" line used to sit here pointing at
the page the reader already had open; what was actually missing was the way back to the fleet.

The dashboard link is built with `server_report.page_href`, the same helper that publishes the page,
so the slug cannot drift from the one the page was published under. The inventory link uses the
stable `database-inventory.html` name the webhost serves (`--latest`), not the run's stamped file.
The base URL lives in `data/reports_config.json` as `report_base_url` — host, port and mount are
deployment facts, and a moved webhost would otherwise publish dead links. **There is no built-in
default** (changed 2026-08-21: it used to be one estate's real report host, which handed every
other operator links to a machine they cannot reach). Unset, the HTML pages fall back to
relative hrefs — correct, because they are served from that same webhost — and the Telegram
messages leave the link out entirely, because a chat client cannot follow a relative href and
a link that 404s is worse than no link.

**The published page opens with a picker of the whole fleet** — one chip per server that has an
index report, coloured by the worst thing found on it, the current server highlighted and not a link
to itself. This report is one file per server, so unlike `server-metrics.html` it has no JavaScript
to switch servers with; carrying the same list on every page is what makes the set navigable instead
of reachable only by whoever remembers the file naming rule. Only servers whose page is being
written this run, or already on disk, are offered — a link to a 404 looks like it should work. The
picker is HTML only: the stored copy feeds Telegram, where a fleet-sized nav block would eat the
4096-character budget.


## Runtime Tables

- Reads `metric_results`.
- Reads/writes `report_types`.
- Writes/updates `reports`.
- Reads/writes `report_send_state`.
- Writes `telegram_send_messages` when pushing alerts.
- May trigger metric collection during `force-hourly-report`.

## Config Files

`data/reports_config.json` defines scheduled reports, report codes, enabled state, timing/dedupe behavior, target filters, and destination/routing behavior. Report type metadata is seeded into `report_types`.

## Data Flow

Metric history + report config -> report text generation -> `reports` row -> scheduled state update -> Telegram queue row in `telegram_send_messages` -> Telegram App sends the message.

## How to Run

```powershell
python -m db_ops.reports.cli --config config.json create-metrics-reports --summary-limit 150
python -m db_ops.reports.cli --config config.json create-metrics-reports --target-ip 192.0.2.115 --summary-limit 150
python -m db_ops.reports.cli --config config.json create-backup-health-report --days 7
python -m db_ops.reports.cli --config config.json push-report-alerts
python -m db_ops.reports.cli --config config.json queue-metrics-reports --summary-limit 150
python -m db_ops.reports.cli --config config.json run-scheduled --summary-limit 150 --backup-days 7
python -m db_ops.reports.cli --config config.json force-hourly-report --target-ip 192.0.2.115 --summary-limit 150 --dedupe-seconds 0
python -m db_ops.reports.cli --config config.json metric-history-report --server-id ACME-192-0-2-108 --metric-code SYSTEM_CPU_MEMORY --hours 24 --summary-limit 150 --dedupe-seconds 0
# Disambiguate when several targets share one IP (e.g. HA cluster nodes on different ports):
python -m db_ops.reports.cli --config config.json force-hourly-report --target-ip 192.0.2.249 --db-type postgresql --port 5433
```

`force-hourly-report` resolves the target from `--target-ip`. When more than one configured target shares that IP, add `--db-type` and/or `--port` to pick exactly one (otherwise it fails with an "ambiguous target" error). The same-IP case is the norm for SRE lab HA clusters whose primary/standbys publish different ports on one worker.

### Single-metric history report (store-local)

`metric-history-report` reports one exact `server_id` + `metric_code` pair over the UTC window from `now - hours` through `now`. It reads and formats existing `metric_results` rows only, then queues the report at Telegram's `logging` level; it does not run metric collection.

Required options are `--server-id`, `--metric-code`, and `--hours`. `--summary-limit` defaults to `150`, and `--dedupe-seconds` defaults to `0`, so an operator can re-run the same requested window without scheduled-report dedupe suppressing it.

### Inventory health & report (worker-side, store-local)

These build the architecture inventory health/report **locally on the worker** from the
metrics already in the runtime store — no SSH, no re-collection. This is the supported home for inventory
reporting (the control app's `inventory-*` commands are legacy — see
[11_control_app.md](11_control_app.md)).

```powershell
# Build only the dated health overlay from collected metrics:
python -m db_ops.reports.cli build-inventory-health --days 7

# Full workflow: build overlay -> merge into architecture/database-inventory.json -> render summary:
python -m db_ops.reports.cli inventory-workflow --days 7

# ...same, plus the styled HTML + Markdown report (TEMPLATE.html / TEMPLATE.md layout):
python -m db_ops.reports.cli inventory-workflow --days 7 --beauty 1
```

| Command | What it does |
| --- | --- |
| `build-inventory-health [--days N --output-dir --date]` | Build `<stamp>_database-inventory.json` health overlay from `metric_results`. |
| `inventory-workflow [--days N --output-dir --inventory --date --dry-run --beauty 0\|1]` | Overlay → merge into the canonical `database-inventory.json` → render `<stamp>_database-inventory-summary.md`. `--beauty 1` additionally renders `<stamp>_database-inventory-report.{html,md}` from the merged inventory (KPIs, auto-derived Priority Attention, fleet/backup/config tables, per-server detail). Default `--beauty 0` = summary only (unchanged behavior). |

The styled report's HTML shell is shipped as package data at
`db_ops/reports/templates/inventory_report.html`; the renderer (`db_ops/reports/inventory_report.py`)
injects the computed `SCOPE` / `SERVERS` / `TRIAGE` data the template renders from. Triage
(Priority Attention) items are **auto-derived from thresholds** (violated backup RPO, low disk,
low PLE, config drift, xp_cmdshell, un-collected hosts) plus one card per current metric problem
(see "What 'current' means" below).

## Linked servers (on `server-metrics.html`)

Its own table, not a chart and not a status chip. A linked server is not a time series — it either
answers or it does not — and the question about one is never "what was it doing on Tuesday" but
**"should this still exist"**. That needs two facts on the same row, which is exactly what
`LINKED_SERVER_STATUS` reports:

| Reachable? | Referenced by code? | Verdict | Why |
| --- | --- | --- | --- |
| no | yes | **FIX** | Fails the next time that code runs |
| no | no | **DROP** | Dead configuration, safe to remove |
| yes | no | **REVIEW** | Remove it, or find out who uses it from outside the database |
| yes | yes | **KEEP** | Answers, and something depends on it |

Neither half decides anything alone — an unreachable linked server nothing calls is cleanup; the
same one with a procedure behind it is the next outage. "Referenced" counts **every** object kind,
not only procedures: the metric's own severity uses the procedure count, so a linked server reached
only through a view read as droppable, and a view over a four-part name breaks just as loudly.

Unreachable rows also name **which machine the fix belongs on**, from the metric's `failure=`
field — `CREDENTIAL_UNREADABLE` is a local problem (this instance cannot decrypt the stored remote
login; the remote host is usually fine), `LOGIN_REJECTED` is a credential to renew on the remote
side, `UNREACHABLE` is a host/network problem. One service-master-key failure otherwise fans out
into an alert per linked server, all blaming innocent hosts.

Built in `build_linked_servers()` from the **raw store rows**, like `backup` and `freshness` and
for the same reason: the chart pipeline drops anything with fewer than `MIN_POINTS` samples, which
is right for a session id and wrong here. Feeding it the charted `series` showed 1 linked server
across an estate that has 50. The metric is also kept out of the generic status chips, so a
`REACHABLE` chip does not sit beside a table row that already says REACHABLE / KEEP / 14 procedures.

### Fleet-wide, on `database-inventory.html`

The same table, every instance at once, sitting just above **Server Detail**. Built by
`inventory_report.build_fleet_linked_servers()`, which calls the *same* `build_linked_servers()`
the per-server page uses — re-deriving the rules is how two pages start disagreeing about what is
droppable. Each row carries its `server_id`, and the legend counts the verdicts.

Fleet-wide because the estate asks a question no single server page can: a dead target is usually
referenced from several instances at once, and the drop list is a piece of work somebody schedules
once.

**`CREDENTIAL_UNREADABLE` is never a DROP.** It means the test could not be *run* — this instance
cannot decrypt the stored remote login — not that the target is dead. One service-master-key
problem on 192.0.2.111 made all 8 of its linked servers unusable at once, and five of them read
as "safe to remove" on evidence that did not exist. They are reported as FIX, with the row saying
so.

## Query Store — where a slowdown can still be investigated (on `server-metrics.html`)

One folded section per server, one row per database, built from `QUERY_STORE_COVERAGE`. Its own
section rather than a chip for the same reason Access has one: the question is per database and
the answer has settings attached.

| Column | What it says |
| --- | --- |
| State | **CAPTURING** (`READ_WRITE`), `OFF`, `READ_ONLY`, `ERROR`, `NOT_ONLINE`, `SECONDARY` |
| Storage used | percent of `max_storage_size`, with the absolute figures — this is what predicts the next database to stop capturing |
| Capture / Cleanup / Wait stats | `query_capture_mode`, `size_based_cleanup_mode`, `wait_stats_capture_mode` |
| Retention | stale-query threshold in days / aggregation interval in minutes |

**The state is the actual one, never the configured flag.** A Query Store that reaches its storage
limit switches itself to READ_ONLY and stops recording while `sys.databases.is_query_store_on`
still reads 1 — the summary counts that as `on but not capturing`, and the row says
`storage size limit reached` in place of the raw `readonly_reason=65536`.

The fleet page (`database-inventory.html`) carries the same fact as one badge per database in the
Databases table, so "which databases on this server capture nothing" is answerable without opening
the server page. Databases on engines with no Query Store (Oracle, PostgreSQL) show `—`, which is
deliberately distinct from `off`.

## Databases — what is on this instance (on `server-metrics.html`)

One row per database: state, recovery model, data size, log size **and** how much of it is in use,
compatibility level, page verify, last known-good CHECKDB, user count, Query Store, and the age of
the newest FULL / DIFF / LOG backup. Every one of these facts was already on the page before this
section existed, spread across six other sections — so "what is on this server and how is each one
doing" meant reading all six and joining them by eye.

Rows are ordered by **`database_id`** — master, tempdb, model, msdb, then user databases in
creation order — not alphabetically, which put `SALESDB` above `master` and scattered the system four
through the middle. The id is carried on `DATABASE_CONFIG` (the only per-database metric with no
`database_id > 4` filter, so the only one that has an id for the system four) and on
`DATABASE_STATUS`; PostgreSQL's `DATABASE_STATUS` reports `pg_database.oid` under the same key so
every engine orders the same way. A database whose id has not been collected yet sorts **after**
those that have, by name — folding a missing id to 0 would sort a whole target's databases ahead
of `master`.

The rows come from `inventory_health.build_database_rows`, **the same builder the fleet page's
Server Detail uses**, so the two tables cannot disagree about a database — including the order. The per-server page
passes no `static_sizes`: the canonical-inventory fallback belongs to the fleet report, which is
the only one with that file.

**System databases are listed, and fixing that was the point (2026-08-14).** `DATABASE_STATUS` is
the authority on which databases still exist — without it, a database dropped this morning keeps a
row until yesterday's daily size sample ages out of the window. But its SQL Server variant selects
`WHERE d.database_id > 4`, so it never names `master`, `tempdb`, `model` or `msdb`. Letting it
decide the whole list *deleted* those four from every database table on every page, while
`DATABASE_CHECKDB` went on raising warnings against them: on 192.0.2.115, three databases each
carried "no CHECKDB has ever been recorded" with no row anywhere to attach it to. The rule is now
scoped — a name in `inventory_health.SYSTEM_DATABASE_NAMES` is exempt from drop-detection, because
nobody drops `master` — and the general principle is worth keeping: **a metric's own scope must not
decide what the page is allowed to show.**

## Tablespaces & datafiles — Oracle storage (on `server-metrics.html`)

One row per tablespace, its datafile paths listed underneath, built by
`server_report.build_tablespaces()` from `TABLESPACE_FREE_SPACE`, `STORAGE_DATA_FILE_SPACE` and
`STORAGE_TEMP_SPACE`. The first thing an Oracle DBA looks up was the one thing the page could not
answer: every number was already in the store and rendered as ten sparklines all titled
"Tablespace free space" with the name only in a chart tooltip, plus the datafiles as more cards
saying `15000 MB` with nothing saying which tablespace they extend.

**The section is Oracle's without being told which engine it is looking at.** `TABLESPACE_FREE_SPACE`
has an Oracle variant only, and the datafile rows are keyed on the `tablespace=` field that only
Oracle's variant of `STORAGE_DATA_FILE_SPACE` writes — SQL Server's states `database=`/`file=`/
`used_pct=`, PostgreSQL's states a database *size*. An empty section renders nothing, so every
other engine gets the right page for free. Same rule for temp: SQL Server's `STORAGE_TEMP_SPACE`
names no tablespace, so tempdb cannot become a row here.

| Column | What it says |
| --- | --- |
| Used | Against the **maximum** the tablespace can reach, not against what is allocated today. The collector's own `(99.8% of max)` figure is preferred over recomputing, so the page and the alert cannot round one sample to two numbers |
| Free | `effective_free_mb` — free space **plus** autoextend headroom, which is what a growing segment actually has before ORA-01653 |
| Free now | Free inside the currently allocated files, the first half of the above |
| Autoextend headroom | With the count of files that can actually grow beneath it |
| Allocated / max | What exists on disk against the ceiling |
| Largest extent | What a single allocation has to fit inside |
| Temp peak | `max_used_mb` for a temporary tablespace |

Four rules decide what the row means, and each exists because one of the numbers alone reads wrong:

- **Free-right-now is not what is available.** RBS on 192.0.2.236 holds 2.3 GB free inside 65 GB
  of headroom. Reported alone it reads as nearly full, and the datafile added on that reading was
  never needed. The row therefore leads with the effective figure.
- **Headroom from files that cannot autoextend is not headroom**, so the count of autoextending
  files travels beside it and a tablespace where that count is zero is called out in the header.
- **The largest free extent is what an allocation must fit in.** Free space held in fragments too
  small still raises ORA-01653, which no percentage on the row predicts.
- **A temp tablespace reports its high-water mark**, not its idle usage. Current temp use is near
  zero between sorts, so the peak is what ORA-01652 is measured against.

Built from the raw store rows like `jobs` and `access`, reduced to the newest collection: this is
an inventory of what exists, so a datafile added this morning must appear and one dropped last week
must not linger. A datafile whose tablespace produced no free-space row this run is still listed,
separately — a tablespace missing from `TABLESPACE_FREE_SPACE` is itself the finding.

## The four Oracle sections (on `server-metrics.html`)

Eleven Oracle metrics were collecting with no table anywhere to read one in — they rendered as
chart cards whose item name lives in a tooltip, which is how `SHARED_POOL_FREE` at 9 MB free, the
instance's *only* CRITICAL finding, sat on the page as an unlabelled sparkline among thirty.

All four builders read the **raw store rows**, like `jobs` and `access`: the chart pipeline caps a
metric at `MAX_ITEMS_PER_METRIC` (it dropped 18 of the 28 top-SQL statements) and drops whole
metrics for having too few samples, which is right for a trend and wrong for an inventory. And
none of them is told which engine it is looking at — an empty section renders nothing, so a SQL
Server or PostgreSQL page is unchanged.

### Oracle instance health — `build_oracle_instance`

`SHARED_POOL_FREE`, `LIBRARY_CACHE`, `BUFFER_CACHE_HIT`, `PROCESS_LIMIT`. Two of the numbers are
ratios the collector does not compute, and each is the point of its metric: the library cache
**hit ratio** (`gethits/gets`) says whether SQL is being re-parsed, and **percent of limit** says
how much headroom is left before ORA-00020. Reporting reloads and a raw session count leaves the
reader to divide.

- A namespace nothing asked for has **no** hit ratio, not 0% — folding it would rank the untouched
  namespaces as the worst on the instance.
- The **peak** is what reaches the process ceiling, never the count the collector happened to
  sample, so both are on the row.
- Oracle's `TO_CHAR` writes a sub-one value without its leading zero (`free_mb=.59`). A parser
  requiring a digit first read the smallest pool on the instance as "not collected" — which is the
  one closest to failing. `_leading_float` accepts both.

### Segments & objects — `build_oracle_objects`

Four failures, none of them visible in the tablespace numbers:

| Table | Metric | Why it cannot be inferred from anything else |
| --- | --- | --- |
| Unusable indexes | `INDEX_UNUSABLE` | Not a slow index, an **absent** one: queries silently full-scan and DML raises ORA-01502. `OWNER.INDEX:PARTITION` keys a partition, which is a different piece of work from a whole index |
| Segments near MAX_EXTENTS | `SEGMENT_EXTENT_LIMIT` | Fails with ORA-01631 **while the tablespace still shows gigabytes free** — the one 8i failure a capacity percentage cannot predict |
| Invalid objects | `INVALID_OBJECTS` | ORA-04068 fires at *call* time, not when the object broke: a list of things that will fail on next use |
| Largest segments | `TOP_SEGMENT_SIZE` | The tablespace table answers "is there room left"; this answers **what used the room** |
| Rollback segments | `ROLLBACK_SEGMENT_CONTENTION` | The 8i write-throughput ceiling. **Shrinks and extends count even at a zero wait ratio**: the ratio only counts transactions that had to queue for a slot, so a segment regrowing on every long transaction reports zero throughout |

### Redo & archiving — `build_oracle_redo`

`log_mode=NOARCHIVELOG` means **no point-in-time recovery exists for this instance** — the only
restore possible is to the moment of the last full backup. That is the most consequential fact
about 192.0.2.236 and no page stated it: the value sat inside a `LOG_FILE_SPACE` card titled
"Log file space", a name that on every other server means per-database log usage. The section is
therefore placed **above Backup**, because it is the question the backup verdict is read against.

`summary.pointInTimeRecovery` is its own field so the page does not re-derive the verdict from the
mode string. The unarchived-log count is never shown without the mode: a backlog under ARCHIVELOG
is archiving falling behind and heading for a frozen instance, while under NOARCHIVELOG no group
is ever archived and the same number is normal.

**Selected by item, not by engine** (`_ORACLE_REDO_ITEMS`): SQL Server writes per-database log
usage under the same metric code, and the databases table already renders it.

### Top SQL — `build_oracle_top_sql`

`TOP_DISK_READ_SQL` and `TOP_BUFFER_GETS_SQL`, two lists because they name different problems —
disk reads is the statement making storage the bottleneck, buffer gets is the one burning CPU on
logical I/O, and a statement can top one while being absent from the other. Ranked by **total**,
which is what the instance pays; **per execution** is on the row too, because that is what says
whether the fix is a plan or a caller — 378k reads over 3 executions is one bad plan, the same
total over 4 million executions is a statement doing its job.

The statement text is read with `_tail_field`, not the shared `key=value` parser: a statement is
full of commas and that parser ends a value at the first one, so every multi-column SELECT
rendered as its first column. Both variants write `sql=` **last** for exactly this reason.

## Which rows a level report is allowed to mark as reported

The warning and critical reports both read **unreported** rows only (`daily_report_created = 0`),
which is what stops the same finding being sent every three minutes. What a report may then mark
is narrower than what it fetched, and getting that wrong loses alerts silently.

`create_metric_report_for_level` fetches **every** status in its freshness window, then renders
only the statuses belonging to its level (`_rows_for_level`). It used to mark the whole fetch —
scope-marked as `metric_codes × target_ids` up to the newest row it had seen. So a warning run
consumed the critical rows collected before it, and the critical run minutes later never saw them.

**2026-08-17 on 192.0.2.115**: SPID 831 became a head blocker holding an open transaction. It
went CRITICAL at 16:15:57 (`LOCK_BLOCKING_SESSIONS`, 43 sessions blocked) and 16:16:10
(`LOCK_SLEEPING_OPEN_TRANSACTION`). The warning report ran at 16:17:26 and marked both. The
critical report 44 seconds later reported `Raw Critical Rows: 5` with neither in it — the only
critical rows that survived were three collected *after* the warning run's cutoff. The alert that
went out described the incident through the mildest of them, `LOCK_TRANSACTION_HOLDERS`:
*"3 lock(s), blocked_sessions=1"*, while 43 sessions were blocked and climbing to 73.

Two rules now:

- **Mark what you reported, by `result_id`** — `level_rows`, not the fetch. `health_rows` is
  excluded too: it carries every row of an unhealthy target, including its OK ones.
- **Sweep the backlog older than the freshness window**, still by scope. A row that has aged out
  can never be reported by any level again, so clearing it is what stops `unreported_only`
  dragging ancient rows back in — the behaviour the scope marking was added for. The bound is the
  window start, not the newest row fetched; everything *inside* the window still belongs to
  whichever level's report has not run yet.

`create_metrics_reports` (the per-target all-levels report) keeps marking its whole fetch, which
is correct there: it reports every level in one pass.

## What "this database is online" means

`DATABASE_STATUS` is collected for every engine and each words the answer differently: SQL Server
reports `sys.databases.state_desc` (`ONLINE`) and PostgreSQL follows it, while Oracle reports
`v$database.open_mode`, whose healthy values are `READ WRITE` and `READ ONLY`. Comparing against
the literal `ONLINE` made **every Oracle instance in the fleet read `0/1 databases online`**,
permanently, and be graded WARNING on it — while the database was open and serving.

The rule is `db_ops.lib.inventory_render.is_database_online()`, in `lib` because the fleet page's
`online/total` count, the per-server database table and the summary's "Databases not ONLINE" line
all decide it, and two pages disagreeing about one database is worse than either being wrong
alone. The page's own JavaScript carries the same list as `ONLINE_STATES`; a test asserts the two
match rather than trusting them to stay in step.

## Scheduled jobs — what this instance runs on a schedule (on `server-metrics.html`)

One table, both engines, fed by `SQL_AGENT_JOB_INVENTORY`: SQL Server Agent jobs come from
`msdb`, Oracle's from `dba_jobs` (8i / DBMS_JOB) or `dba_scheduler_jobs` (10g+). Its own section
for the reason the linked-server and access tables have one — a job is not a time series, the
metric is collected once a night, and the chart pipeline drops it for having too few samples. The
page could report a server perfectly healthy while the nightly job producing its backups had been
failing for a week.

It is a folded section that opens by itself when a job is failing — the same rule the backup,
linked-server and Query Store sections above it use.

| Column | Where it comes from |
| --- | --- |
| Last outcome | `sysjobservers.last_run_outcome`; on Oracle, the job's `state` (10g+) or whether `failures` is 0 (8i) |
| Job | Job name, with its `syscategories` category (SQL Server) or `job_class` (Oracle 10g+) beneath it. Two Oracle-only notes appear here when they apply: the consecutive-failure count (the scheduler breaks a job at 16) and 8i's `schema_user`, shown only when it differs from the submitting `log_user` — a job resolving against a different schema than its owner is how "it worked yesterday" starts |
| Command | Its own column. The first step's `sysjobsteps.command` (SQL Server, with `step 1 of N` beneath it) or the `what` / `job_action` PL/SQL, truncated to 200 characters — enough to recognise the job, not to review its code |
| Schedule | msdb's `freq_type`/`freq_subday_type` decoded into words (`daily at 00:00:00, repeat every 1 hour(s)`); on Oracle the `interval` expression or `repeat_interval` |
| Next / Last run | `sysjobschedules.next_run_date`, `sysjobhistory`; `next_date` / `last_date` on Oracle |
| Duration | Whichever of the three the engine gave, labelled: SQL Server's worst run of the **last 7 days** (`longest, 7d`), Oracle 10g+'s `last_run_duration` INTERVAL (`last run`), 8i's `total_time` (`total, lifetime`). A job whose window holds no history reports `n/a` and the cell stays empty — reading that as 0 would rank the never-run jobs as the fastest ones |
| History | SQL Server: attempts, successes and failures over the **last 7 days** from `sysjobhistory` (`step_id = 0`). Oracle scheduler: `run_count` / `failure_count`, which are **lifetime** counters. Each row states the window it covers — averaging the two would invent a comparison the data does not support |
| Owner | `SUSER_SNAME(sysjobs.owner_sid)`; `owner` (10g+) or `log_user` (8i) on Oracle |

Four things about this section are deliberate:

- **Only enabled jobs are rows.** The disabled ones are a count in the header. A job that is
  switched off is not a schedule, and listing it beside the live ones stops the table being the
  answer to "what runs here". They stay in the *metric*, though — `inventory_health.py` builds
  `disabled_jobs` from exactly these rows, so filtering them in the SQL would empty that block
  with nothing reporting an error.
- **Failing jobs sort first**, because that is why the table is opened.
- **Every field the metric collects has a place on the page.** The message is the only carrier
  between the SQL and the table, so a field parsed into the payload and then never rendered is
  indistinguishable from one that was never collected — the section spent its first week showing
  `—` in every column because the enriched SQL had shipped and nothing on the page read it.
- **`command` is the last key in every variant's message, and the parser reads values up to the
  next known key.** A job step is full of commas and equals signs; the shared `_message_kv` parser
  ends a value at the first comma, which would render a job as running something it does not run.
  The parser also has to cross newlines: an 8i `dba_jobs.what` is the PL/SQL block as submitted,
  and a pattern that stops at end-of-line made the field go *missing* rather than truncated.

## Access — who can reach this instance (on `server-metrics.html`)

Two tables in one folded section: **Instance logins** and **Database users**. Its own section for
the reason the linked-server table has one — a principal is not a time series, and the questions
brought to this page ("who has sysadmin here", "which user has no login behind it") cannot be
answered by a chip that says OK.

| Table | Metric | One row per |
| --- | --- | --- |
| Instance logins | `SECURITY_SERVER_PRINCIPALS` | server principal: type, disabled, server roles, notable server-scope permissions, default database, password age, `check_policy` |
| | | **Every login on the instance, disabled ones included.** They are listed with a `disabled` flag rather than hidden — a disabled sysadmin is still an account somebody can re-enable — and the section header says how many, so "31 logins" is not read as 31 usable ones. |
| Database users | `DATABASE_USER_PERMISSIONS` | (database, user): type, the login it maps to, database roles |

`SECURITY_SERVER_PRINCIPALS` exists because `SECURITY_LOGIN_HEALTH` is **exception-based** — it
reports the logins with an old password, a long session, failed attempts or dormancy, plus a
summary row. A healthy login never appears in it, so it can never answer "who can connect". The
new metric's status is always `OK` on purpose: it is the list, and the alerting on the same
principals already belongs to login-health and failed-logins. Grading `sysadmin` as WARNING would
raise one on every instance in the estate, for `sa` and the engine's own service accounts, for
ever.

**`ORPHANED` is the row worth the section.** A database user lives inside the database, so it
survives a restore — but it carries the *source* instance's SID, so it resolves to no login on the
target until one is created with that same SID. Nothing else on the page can show this: the
database is ONLINE, its size is normal, its backups are fine. The 2026-08-10 migration onto
192.0.2.11 landed 13 databases whose 55 users were all in exactly that state, and it was found
by querying `sys.database_principals` by hand.

`HIGH` marks a role or permission that can change who else gets in — `sysadmin`, `securityadmin`,
`serveradmin`, `setupadmin`, `CONTROL SERVER` at server scope; `db_owner`, `db_securityadmin`,
`db_accessadmin`, `db_ddladmin` at database scope. It is read from the metric's own
`HIGH_PRIVILEGE` marker **or** re-derived from the role list, so a bundle collected by an older
metric build still sorts and colours correctly.

Both metrics separate their fields with ` | ` and their list members with `,`, which the shared
`_message_kv` cannot parse: it ends a value at the first comma, and `re.findall` then resumes past
everything that value swallowed. `_pipe_fields` splits on the pipe first; `_bracket_list` reads a
`key=[a,b,c]` field straight off the message. Without the second one,
`roles=[db_datareader,db_datawriter,db_ddladmin]` rendered as a plain reader — the one row an
operator would stop on, shown as the one kind that is harmless.

Rows are ordered orphaned first, then privileged, then alphabetically, and the section opens
folded unless something is orphaned.

## Volumes — how large the storage is and how full (on `server-metrics.html`)

The Disk space tile reduces a whole host to one percentage. On `192.0.2.250` that percentage is
`28%`, green, and true — the fullest of its two volumes is a 2 TB data disk at 28.4%. What the tile
cannot say is that the other volume is 199 GB, or that 28% of the first one leaves 1.4 TB of
headroom while the same percentage on the second would leave 143 GB. **Size is what turns a
percentage into a decision**, and it was collected all along: `total_gb`, `used_gb`, `free_gb`,
`filesystem` and the device or volume label ride in the message of both
`db_ops/metrics/collectors/os/windows/003_os_disk_usage.ps1` and `db_ops/metrics/collectors/os/linux/003_os_disk_usage.sh`.
The report parsed out the percentage and discarded the rest.

`server_report.build_volumes()` renders one row per volume, fullest first: size, used, free, used %
with a fill bar, and which collector answered. It reads the **raw store rows** reduced to each
metric's newest collection (like `linkedServers` and `queryStore`) — a volume is an inventory of
what exists now, so one unmounted this morning must stop being listed.

**Two sources, merged per volume** (`VOLUME_SECTION_CODES`), because each covers a host the other
cannot:

| Source | Metric | The hosts it is the only answer for |
| --- | --- | --- |
| `OS` | `OS_DISK_USAGE` | Anything with no database on it — the Ubuntu worker, the four Service Fabric nodes. Also the only source of file system and device. |
| `engine` | `STORAGE_DISK_FREE_SPACE` | An instance whose `disabled_collector_types` turns `cmd` off (`192.0.2.253`), or one with no OS credential. |

Three things about this data are easy to get wrong, and each is a test in
`tests/test_server_report_volumes_section.py`:

- **The same Windows volume arrives twice.** `sys.dm_os_volume_stats` returns the mount point `C:\`
  and `[System.IO.DriveInfo]` is trimmed to `C:`. `_volume_key()` drops a *trailing* separator only
  — on Linux the root mount **is** `/`, and trimming that would merge root into its neighbour. The
  OS row wins a shared volume; it is the one that knows the file system.
- **`OS_DISK_USAGE` is not only volumes.** Throughput, IOPS and queue length share the metric code
  and have no size; they belong to the Storage activity area and are excluded by name plus a
  "reported a `total_gb`" check.
- **A size the host does not know is not a size of zero.** A SQL Server too old for
  `sys.dm_os_volume_stats` falls back to `xp_fixeddrives`, which returns free space and
  `total_gb=unknown` — all three volumes on `192.0.2.253` read that way. Those rows are listed
  (free space is the column that decides anything), and the **host-level ratio is withheld** rather
  than computed over a partial sum: the header says `N with no size reported` instead of a
  percentage that understates the machine.

## Health-area tiles: a value that breaks the rule printed beside it

Every area tile states the rule that decided its status. When `metrics.metric_overrides` lowers a
metric's severity on one server, the tile used to show only the result — so the CPU tile on
`ACME-192-0-2-249-HOST` read `90.67%` under its own `WARN ≥ 80% · CRITICAL ≥ 90%` in the ordinary
colour of a healthy area, because the stored status was `LOGGING` (the deliberate 2026-08-14
override for that host's one-second CPU sample). Both halves were true and the tile as a whole said
something false.

`health_model.downgraded_from()` reads the note the collector already writes into the message
(`Policy: severity remapped CRITICAL->LOGGING by metric_overrides.`) and returns the severity the
remap **removed** — only when it was genuinely lowered; a remap that raises a status already shows
in the colour. `build_areas()` puts it on the tile as `downgradedFrom`.

The tile **keeps the status config asked for**. Repainting it red would re-raise exactly what
somebody decided not to be alerted about, and the next person deletes the override to make the page
quiet again. Instead it renders with a dashed border in the severity it would have been, a
`silenced` badge, and a line saying the value is real and the silence is a decision. Area member
selection gained a matching key — `(severity, downgraded severity, priority, value)` — so a
silenced finding is not hidden behind a genuinely healthy neighbour (`OS_CPU_USAGE` carries load
average beside CPU percentage, and `load_average 0.89` is a true, useless answer to why the tile is
marked).

## Capacity forecast — when does this run out

The current free-space number was already on the page; what was missing was its **direction**, so a
volume two days from full and one flat for a month rendered identically. The store already held the
answer: `192.0.2.115`'s `L:` fell 618 GB → 163 GB in three days while `SALESDB`'s log grew
21 → 428 GB.

The analysis is [`db_ops/lib/capacity_forecast.py`](../db_ops/lib/capacity_forecast.py) — in
`common`, because the fleet page and the server page must not disagree about which volume is about
to fill. `server_report.build_capacity()` reads the **raw store rows** (like `backup` and
`linkedServers`): the chart pipeline drops and caps series for display, and a slope wants every
sample it can get.

Three rules stop the forecast being worse than no forecast:

- **Median of pairwise slopes (Theil–Sen), not least squares.** Capacity series are punctuated by
  shrink/regrow cycles; one 300 GB step dominates a fitted line and can report *growth* on a volume
  that is emptying. The median steps over a minority of jumps.
- **A resize restarts the measurement.** Free space jumping up means something was shrunk, moved or
  deleted — growth measured across that instant averages two different worlds. A reset is detected
  as an *outlier* step (many times the typical step), not as a fraction of the series' range:
  range-based detection trimmed an 8-day history to "12 resets seen" on volumes nobody had touched,
  because a nearly-flat series has a tiny range and ordinary jitter clears any fraction of it.
- **Refuse to guess.** Under `MIN_POINTS` samples or `MIN_SPAN_HOURS` of span the answer is
  `insufficient_history`, rendered as itself. A confident "0 GB/day, never full" from two samples is
  the most dangerous thing this table could say.

Horizons and per-volume reserves live in `data/capacity_policy.json` — config is data, and a 4 TB
data volume is not the same problem as a 60 GB system volume. "Full" means reaching the reserve,
not zero.

## Reading a report as it was on a past date

All three pages answer `?date=YYYY-MM-DD`; see [`12_webhost_app.md`](./12_webhost_app.md) for the
resolution rules. The inventory gets it free (it is published per run under a stamped name); the
other two keep stable names, so each run also writes one dated copy per calendar day via
`db_ops.lib.report_archive.archive_daily()` — the page **and** its series files, since reading
the page at a past date means fetching that date's series, not today's.

Adding a new published file that should be readable by date? Archive it in the same pass, or
`?date=` will silently serve today's build under a past date's URL.

### Backfilling days that were never archived

The dated copies only start existing the day archiving is switched on, so earlier days are
unreachable. The data is not missing — `metric_results` is append-only — so those days can be
re-rendered from the rows collected at the time:

```bash
python -m db_ops.reports.cli --config config.json backfill-dated-reports \
    --date 2026-08-01 --date 2026-08-02 --days 7
```

It runs the same builders with the window **closed at the end of that day** (`as_of`), which is
what `?date=` means everywhere else. Two rules keep it safe against a live report directory:

- **It never writes a live file name** (`archive_only`) — only `YYYYMMDD_<name>`. A backfill that
  touched `server-metrics.html` would publish a past day's data on the URL read as "now".
- **It never files store reports.** Re-inserting a past day's index findings would re-alert on
  things dealt with days ago. `created` therefore stays 0 while `published` counts the pages.

`now` is the date being rebuilt, not the wall clock — judging 1 August against today would mark
every one of that day's collections stale and paint the whole fleet UNKNOWN.

**What a backfill cannot recover** is the fleet *roster* of that day: the server list comes from
`database-inventory.json`, a living file with no per-day history. The metric data on each rebuilt
page is genuinely that day's; the list of servers is today's.

## What "current" means

Both report pages — the fleet `database-inventory.html` and the per-server `server-metrics.html`
— claim to describe the present, and they must agree. Three rules make that true; breaking any
of them produces a page that is confidently wrong rather than obviously broken.

**1. Current state is the newest collection snapshot per `(server, metric_code)`,
NULL-item rows included.** Not the newest row per item. When a metric's condition clears, the
collector writes one row with `metric_item = NULL` ("SQL returned no rows"); that lands in a
different partition from the item it cleared, so per-item recency kept a CRITICAL lock row on the
fleet page for the whole window after blocking had ended. Implemented in
`db_ops/lib/health_model.py` (`latest_snapshot`), applied by
`MetricStore.fetch_severity_by_server`, `MetricStore.fetch_current_problems` and
`inventory_health.index_by_server`. `metric_results` stays append-only — timelines and charts
read the history; a verdict never does.

**2. Every metric reports its own freshness.** A page-wide "data age" is the *newest* metric on
the server, which hides every late one behind it. `MetricStore.fetch_metric_freshness` returns
`last_attempt` / `last_success` / current error per `(server, metric_code)`;
`server_report.build_freshness` turns that into `OK` / `LATE` / `FAILED` against the cadence in
`data/metric_definitions.json`, plus `notCollected` for catalog metrics with no evidence at all.
A health area whose own metric is LATE or FAILED reports UNKNOWN, not the last value that worked.

A *success* is decided by `error_type`, not by status: `CHECK_FAILED` means the collector ran and
did not like the answer, everything else in `event_policy.COLLECTOR_FAILURE_ERROR_TYPES` means it
never got one. Target severity maps downgrade auth/connect failures to WARNING, so status alone
cannot tell the two apart.

**3. Health areas select items, not metric codes.** `server_report.AREAS` carries `selectors`
(`{code, items, exclude, units}`) in priority order, and the tile picks by
`(severity, selector priority, value)`. Comparing raw values first is what let disk read
throughput in KB/s decide an area measured in percent, and SQL memory % stand in for CPU.

**Backup is judged per database against a policy**, never from the newest backup evidence on the
instance — see `data/backup_policy.json` and `db_ops/lib/backup_policy.py`. Each database gets
the types its recovery model and the policy require, with `compliant/eligible` counts per type; a
type the policy does not require reads *not required*, not *missing*. A stale LOG backup is
reported as an **RPO violation**, never as a broken chain: chain continuity needs backup LSNs and
recovery-fork ids that no collector gathers, and a fresh FULL taken on a false "broken" reading
discards a working restore path.

## Useful Manual Queries

```sql
SELECT report_id, report_code, report_type, report_level, status, created_at, pushed_at, telegram_send_message_id
FROM reports
ORDER BY created_at DESC, report_id DESC
LIMIT 50;

SELECT *
FROM report_send_state
ORDER BY updated_at DESC;

SELECT send_tlgmsg_id, row_ins_date, tlgchat_id, send_status, source_type, source_id
FROM telegram_send_messages
WHERE source_type LIKE '%report%'
ORDER BY row_ins_date DESC, send_tlgmsg_id DESC
LIMIT 50;
```

## Common Issues

- Report has no useful content: collect metrics first and inspect `metric_results`.
- Scheduled report does not run: check `data/reports_config.json` enabled state and `report_send_state`.
- Report exists but was not sent: check `reports.status` and pending `telegram_send_messages`.
- Duplicate suppression blocks a manual test: use `force-hourly-report --dedupe-seconds 0`.
- Single-metric history is empty: verify the exact `server_id` and `metric_code`, then confirm `metric_results.collected_at` has rows inside the requested UTC window.

## Config Priority

The reports app resolves its config file using this chain:

1. `--config <path>` CLI argument.
2. `DB_OPS_REPORTS_CONFIG` environment variable.
3. `config.reports.json` next to `config.json`, or in the current working directory.
4. `config.json` shared fallback.

The selected source is printed to stderr on startup.

App-specific config file: `config.reports.json`

## Standalone Mode vs Full-Suite Mode

**Full-suite mode** (default): the reports app reads `config.json` and reads `metric_results` from the shared runtime store written by the metrics app.

**Standalone mode**: copy `config.reports.json` and `data/reports_config.json` next to the EXE. The store must resolve to the same database the metrics app writes, or reports will generate empty summaries. Push commands write to `telegram_send_messages` in that same store; the Telegram app must read it to deliver messages.

Required config keys: `log_dir`, plus a resolvable runtime store (`store_config_file`, an inline `store` block, or `sqlite_path`).

## Optional Integrations

**Metrics data prerequisite**: `create-metrics-reports` and `push-report-alerts` read `metric_results`. If no metrics have been collected, reports are created with empty or no-data content. The app does not crash — it returns `created: 0`.

**Telegram delivery**: `push-report-alerts` writes to `telegram_send_messages`. If the Telegram app is not running or `telegram.groups` has no configured levels, rows are written but never sent. The reports app does not import or call the Telegram app directly.

**`force-hourly-report`**: internally calls the metrics CLI as a subprocess passing the resolved config path. If the metrics app binary is absent, this command raises `ReportWorkflowError` with a non-zero exit code.

## EXE Packaging Notes

- `config_path` is passed through to the metrics subprocess call inside `force-hourly-report`. Ensure both EXEs use a consistent config path when running together.
- `data/db_instances.json` is used for target resolution in `force-hourly-report`. Place it next to the EXE or pass the data path explicitly.
