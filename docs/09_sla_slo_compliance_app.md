# SLA/SLO Compliance App

## Purpose

The SLA/SLO Compliance App turns the metric history already collected by the Metrics Engine into service-level reporting. For each policy it computes a **Service Level Indicator (SLI)** — the fraction of "good" metric samples in a time window — and evaluates it against a **Service Level Objective (SLO)** with an **error budget**. It is pure post-processing: it **never connects to a monitored database**. Its only input is the `metric_results` table; its only outputs are the `sla_runs` / `sla_results` tables and (optionally) a Telegram message and an HTML page.

## Package / Files

- `dbabrain/sla/` — `models.py`, `policies.py`, `compliance.py` (SLI/SLO compute), `storage.py` (`SlaStore`), `publish.py` (Telegram + web), `cli.py`.
- `data/sla_policies.json` — policy definitions.
- the runtime store declared in `data/store_config.json` — reads `metric_results`; writes `sla_runs` / `sla_results`.
- `runtime/reports/sla.html`, `runtime/reports/index.html` — published web pages (with `--publish-web`).

## Concepts

- **SLI** = `good_samples / total_samples * 100` over the window, per instance.
- **SLO** = `objective_percent` sustained over `window_hours`.
- **Error budget** = `100 - objective_percent`. `budget_remaining_percent` shows how much of that tolerance is left (clamped to `[0, 100]`; undefined/`0` for NO_DATA).
- **Per-instance by default**: a policy is evaluated **once per `target_id`** (one monitored instance/database). Scope (`db_types` or `target_ids`) only selects *which* instances the policy applies to; results are never lumped together unless `aggregate: true` is set.
- **Statuses**: `PASSED` (meets SLO), `AT_RISK` (still meets SLO but `budget_remaining_percent <= at_risk_budget_percent`), `BURNING` (a fast multi-window burn rate alert has fired; ranked immediately after FAILED for notifications and web display; does not fail overall run), `FAILED` (below SLO), `NO_DATA` (no samples in scope). A run's overall status is `FAILED` if any result is `FAILED` or `NO_DATA`.
- **`NOT_CONFIGURED` is the run-level answer when there is no policy at all** (2026-09-16): `data/sla_policies.json` absent, empty, or every policy in it inactive. With nothing to evaluate there is nothing to fail, and the summary used to fall out of that as **`PASSED`** — a green SLA page over an estate nobody had written a single objective for. A missing config file must never make a page say something false, the same rule `backup_policy.json` follows: *"no rule required this" and "no rule exists" must never produce the same sentence.* The summary's `reason` names the file and the word **unjudged**, the page draws `NO_DATA`'s blank marker rather than a green tick, and the command still exits 0 — `APP-SLA-VALIDATE` is in the shipped schedule, and a correct install logging a failure every cycle teaches its reader to ignore the log. A policy file the caller **names** on the command line still raises when it is missing; only the default path is a configuration state.

## Runtime Tables

- Reads `metric_results` (written by the Metrics Engine). Intentional data dependency, not a code import — the SLA app imports no domain logic from metrics/reports/telegram.
- Writes `sla_runs` (one header row per validation) and `sla_results` (one row per policy **per instance**). Lazy column migration adds `sla_runs.result_count` and `sla_results.target_id` to stores created by earlier versions.
- With `--notify`, queues a row in `telegram_send_messages` (the Telegram app's send queue).
- When launched by the App Command Daemon, daemon-level execution is tracked in `job_runs`.

## Config Files

`data/sla_policies.json` holds a `sla_policies` array. Fields:

| Field | Meaning |
| --- | --- |
| `policy_id`, `name`, `description` | Identity and human labels. |
| `active` | Only `true` policies are loaded. |
| `target_ids` **or** `db_types` | Scope: an explicit list of instances, **or** every target of these db types. One is required. |
| `metric_codes` | Metric codes whose samples form the SLI (e.g. `INSTANCE_STATUS`, `DATABASE_STATUS`, `BACKUP_AGE`). |
| `objective_percent` | The SLO target (e.g. `99.9`). |
| `window_hours` | SLI window (e.g. `24`, `168`). |
| `good_statuses` | Statuses counted as good (default `["OK", "LOGGING"]`). Everything else is bad. |
| `at_risk_budget_percent` | AT_RISK threshold on remaining budget (default `25.0`). |
| `aggregate` | `true` = one fleet-wide result instead of per-instance (default `false`). |
| `category` | Free-form grouping label (e.g. `availability`, `backup`). |

## Data Flow

```text
sla_policies.json (active) -> per policy: fetch metric_results in scope+window
  -> group by target_id (or aggregate) -> SLI + SLO + error budget per instance
  -> SlaValidationSummary -> store sla_runs/sla_results
  -> [--notify] queue telegram_send_messages   [--publish-web] write sla.html + index.html
  -> text/JSON CLI output; nonzero exit on failure unless --allow-fail
```

## How to Run

```powershell
# Compute per-instance SLIs, evaluate SLOs, and store the run (default persists):
python -m dbabrain.sla.cli --config config.json validate --format text

# Full scheduled form: store + Telegram + web page, never fail the daemon:
python -m dbabrain.sla.cli --config config.json validate --format text --notify --publish-web --allow-fail

# Compute only, no writes:
python -m dbabrain.sla.cli --config config.json validate --no-store

# Evaluate against a fixed window end (backfill/debug):
python -m dbabrain.sla.cli --config config.json validate --window-end 2026-05-28T04:00:00Z

# Read stored history:
python -m dbabrain.sla.cli --config config.json history --limit 10
python -m dbabrain.sla.cli --config config.json history --run-id 2
```

`validate` flags: `--policies <path>`, `--window-end <iso>`, `--format json|text`, `--no-store`, `--notify`, `--notify-always`, `--publish-web`, `--web-dir <dir>`, `--allow-fail`.

`data/app_commands.json` runs `APP-SLA-VALIDATE` on the **worker**, every 5 minutes (`repeat_interval: 300`):

```text
python -m dbabrain.sla.cli --config config.json validate --format text --notify --publish-web --allow-fail
```

## Delivery Channels

### What the page shows

Three levels, top to bottom - the estate, each database engine, each instance - laid out the way
an SLO dashboard is read (overall verdict, a panel per group, a status grid, the breaches, the
detail). It was rebuilt on 2026-10-02 at the operator's word: the old page listed SLI codes per
question with no instance named (*AVAILABILITY_SUCCESS_RATIO: STALE* sixteen times) and could not
say "PostgreSQL is fine, SQL Server is not". It inlines `lib.page_style` - the stylesheet the
report pages share - plus its own few rules (engine cards, the matrix's group rows, the folded
instance blocks).

| Section | What it answers |
| --- | --- |
| Masthead | the overall verdict, *N instances on M engines, x of y checks passed*, the six counters below |
| **By database engine** | one card per engine (`lib.engine_sections`: SQL Server, Oracle, PostgreSQL, MySQL, Host only, Other): its verdict, the share of its checks that pass, failed / cannot measure / at risk / passed, *bad right now*, and the instances to look at |
| **Instance matrix** | rows grouped by engine, one per instance (`server_id`); a column per SLI area that has a check (availability, backup & recovery, replication / HA, performance, capacity, integrity & operations, monitoring); a cell is the worst check in that area with its reading, the policy, objective and data quality in its tooltip |
| **Needs attention** | every check that did not pass, worst first, naming its engine and instance, with now / measured / objective / data quality / why |
| **Instance detail** | grouped by engine, one collapsible block per instance - open when anything in it did not pass |
| Recent runs | the newest `WEB_HISTORY_LIMIT` runs |

The engine is the middle segment of `target_id` (`<server_id>/<db_type>/<service>`); a result whose
target is `*` names no machine and is listed last as *Fleet-wide*.

The headline splits four questions that used to be one red number:

| Card | Means | Who acts |
| --- | --- | --- |
| **Bad right now** | the newest collection is failing | on call, now |
| **Window breach** | the rolling objective is missed; the service may already have recovered | review |
| **Objects in backlog** | operational debt from `finding_inventory` policies | schedule work |
| **Cannot measure** | `NO_DATA` / `COLLECTION_FAILED` / `STALE` / `INSUFFICIENT_DATA` | fix the monitoring |

Each policy row carries a **Now** column beside the window figure, from `SlaPolicyResult.current_status` (the newest cycle alone). This is what stops a historical breach reading as a live incident: on `ACME-192-0-2-250`, `OS_REBOOT_PENDING` was warning 07-29 to 08-02 and OK on 08-03 and 08-04 — `2/7 = 28.57%` was correct and the host was not pending a reboot.

The page also states how the run differs from the previous one (`newly failing / recovered / worse / better / unchanged`), using the same comparison that drives the Telegram routing so the two cannot disagree. A key that vanished is reported as "no longer evaluated", never as recovered.

**Retention.** `sla.html` is the stable name everything links to. Alongside it, `archive_daily` writes **one dated copy per day** (`YYYYMMDD_sla.html`), overwritten within the day. It previously stamped every run (`YYYYMMDD_HHMMSS_sla-report.html`), which left 696 files and 422 MB in the serving directory in four weeks and grew without bound. The history table shows the newest `WEB_HISTORY_LIMIT` (15) runs and now says so on the page — the store keeps them all.

### Policy models — which question a policy answers

`policy_model` in `data/sla_policies.json` picks the evaluation semantics. It defaults to `time_slo`, which is what every policy used to get; the other two exist because a row-weighted ratio is only meaningful when the rows are time samples.

| Model | Reads | Actual value | Use for |
| --- | --- | --- | --- |
| `time_slo` (default) | the whole window | % of good samples | availability, connectivity, saturation, latency |
| `current_state` | the newest cycle per metric | binary: `compliant` / `not compliant` | reboot pending, security configuration, service state, latest CHECKDB |
| `finding_inventory` | the newest cycle per metric | count of distinct affected objects | stale statistics, fragmented indexes, risky roles, configuration exceptions |
| `backup_rpo` | the newest cycle of backup rows per target | count of breaches | backup RPO evaluated against data/backup_policy.json |
| `restore_drill` | the whole of backup_restore_history | count of databases in breach | restore drill freshness and RTO evaluated against data/restore_drill_policy.json and rto_target_seconds |

Why the split — measured on production, 2026-08-05:

- `POSTGRESQL_SECURITY_24H` reported **50% security compliance** from one warning (`postgres@cluster` holds superuser) plus one good row. As `current_state` it reads *not compliant, 1 affected*.
- `SQLSERVER_MAINTENANCE_7D` reported **4.09%** from 144 good and 3,377 bad rows covering 1,631 distinct objects. As `finding_inventory` it reads **1,631 objects affected** — the number someone has to work through, instead of a figure that measures how often the collector looked.
- `SQLSERVER_RECOVERY_CHECKDB_7D` averaged 125 snapshots of 39 databases, diluting this morning's CHECKDB with a week of the old state.

Constraints the parser enforces, because both failure modes are silent:

- An unrecognised `policy_model` **fails the config**. A typo that fell back to `time_slo` would restore the old misreporting without a log line.
- A `finding_inventory`, `backup_rpo`, or `restore_drill` policy **must** set `comparison_operator` to `<=` or `<` and **must** set `target_value`. Left at the default `>= 99`, a backlog or breach count compares as count `>= 99` and reports PASSED — healthy precisely when it is worst.
- A `restore_drill` policy **must** specify `rto_target_seconds` as a positive integer.

Coverage, freshness, `NO_DATA` and `minimum_sample_count` are always judged on the **full window**, never on the narrowed cycle: they are questions about the collector, not about the verdict. Error budget and burn rate apply to `time_slo` only — a count of objects or breaches has no budget to burn.

#### Backup RPO (`backup_rpo`)

- **What it reads**: The newest cycle of backup rows (`BACKUP_LAST_RESULT` for SQL Server and Oracle, `POSTGRES_BACKUP_LAST_RESULT` for PostgreSQL) for each target instance.
- **The rule it reuses**: Reuses `dbabrain.lib.backup_policy` (`evaluate_backup_policy`, `collect_evidence`, `policy_is_configured`) with the policy loaded from `data/backup_policy.json` via `dbabrain.lib.data_sources.load_backup_policy`.
- **Why backups are judged here**: Operator decision (2026-10-15): backups are graded against `data/backup_policy.json` in the SLA app; the metrics collectors only gather and report ages without grading them. Under the earlier `time_slo` policy `ALL_BACKUP_HEALTH_7D`, only sample statuses were counted, and `BACKUP_LAST_RESULT` reported status `OK` regardless of age — allowing a 133-day-old log backup to read as compliant. `backup_rpo` directly grades required backup types against warning and critical hours. Data protection is judged as the newest of FULL or DIFF under the `data_backup` objective (allowing weekly-FULL plus daily-DIFF plans to meet SLA), while FULL, DIFF, and LOG are bounded by their per-type thresholds. Each required database and backup type beyond its critical hours, or required with no backup on record, is a breach. If any breach exists, the result is `FAILED`; if only warnings exist, `AT_RISK`; otherwise `PASSED`. Missing policy (`NOT_CONFIGURED`) and missing backup samples (`NO_DATA`) remain distinct from `PASSED`. Findings name the database, backup type, age, and threshold limit. The counts are of databases - `total_count` the databases judged, `bad_count` those with at least one breach - while `actual_value` stays the number of breaches the policy compares with `target_value`: until 2026-10-19 the breaches of every (database, type) pair were subtracted from the cycle's rows, and a server whose databases each missed two or three types read `good_count` -14.

#### Restore drill (`restore_drill`)

- **What it reads**: every row of `backup_restore_history` (`dbabrain.db.backup_restore_history.drill_rows`, the
  query `db.cli restore-drill-status` uses too) - not the policy's window: "never drilled" and "drilled too long
  ago" are different verdicts, and a window would turn the second into the first. A store that is down fails
  the run; a table that cannot be read is `NO_DATA` with the reason. The backup RPO policy reads its rows through
  `SlaStore.fetch_metric_samples(..., with_message=True)` - the message carries each backup's recovery model and
  finish time.
- **The rule it reuses**: Reuses `dbabrain.db.restore_drill.evaluate` and `load_restore_drill_policy` from `dbabrain.db.restore_drill`.
- **Why restore drills are per database**: The `backup_restore_history` table records drills at database level with `database_name` and has no `server_id` (a single database can be restored to different hosts). The policy is therefore evaluated per database at fleet scope (`target_id = "*"`), matching how `restore-drill-status` reports.
- **A retired drill is named, not inferred**: an override in `restore_drill_policy.json` with `"required": false` leaves that database out (`restore_drill.is_drilled`), here and in `restore-drill-status`. The history keeps a switched-off drill's last success for ever, and cannot say which drills are retired - its `restore_id` is its own row number, and a whole-instance restore entry names no database. Reviewed against the estate on 2026-10-16: four databases whose entries had been off since June to August were the whole of the breach; named retired, the policy passes on the drills that run.
- **Breach criteria**: A database breaches when `restore_drill.evaluate` marks its drill as never-done, stale, or newest-attempt-failed, or when its newest successful drill took longer than the policy's `rto_target_seconds`. The `rto_target_seconds` field is required and validated as a positive integer for `restore_drill`. `actual_value` is the count of databases in breach, compared `<= 0`. Findings name the database, last success timestamp, restore duration, and RTO target.

**Telegram (`--notify`) — notifies on state change, not on schedule.** The run is every 5 minutes; the message is not. Each run's `{policy @ target: status}` map is compared against the previous stored run (`SlaStore.fetch_previous_state`) and a message is queued only when something crossed a line:

| Situation | Sent? | Message kind |
| --- | --- | --- |
| A policy newly fails, or gets worse (`AT_RISK` -> `FAILED`) | yes | `transition` |
| A policy recovers, or improves without recovering | yes | `transition` |
| Nothing moved, findings outstanding, last message < 24h ago | no | suppressed, reason logged |
| Nothing moved, findings outstanding, last message >= 24h ago | yes | `reminder` |
| Nothing moved, nothing outstanding | no | — |
| First run ever, with findings | yes | `baseline` |

The 24-hour ceiling is `notification.reminder_after_seconds` in `data/sla_policies.json`. The clock runs from the last message **actually queued** for `source_type='sla'`, not from the last run — a run that stayed silent must not postpone the reminder.

The body carries the headline movement, the `new failed / recovered / unchanged` counts, the current fleet totals, and up to six named findings per group; the standing detail stays on the web page, which the message links to. Before this change the app sent every hour and restated every non-passing row: 78 messages in 76 hours averaging 3,852 characters. A change message is typically under 700.

A key that was failing and is simply **absent** from this run is reported as "no longer evaluated", never as recovered — a retired policy must not announce a fix that never happened.

Routed by severity to `config.telegram.groups`: any `FAILED` -> `critical`/`error` group, any `AT_RISK`/`NO_DATA` -> `warning` group, else `logging` group. The message's own emoji reflects the **change** (new failure -> critical, recovery-only -> success), applied centrally by `dbabrain/telegram/severity.py`; the producer does not tag. `--notify-always` bypasses all suppression. Requires `config.telegram.enabled = true` and a group for the routed level; otherwise it logs a skip (never errors).

**Web (`--publish-web`)** — renders a self-contained themed page to `<runtime>/reports/sla.html` (plus a dated `*_sla-report.html` archive) and refreshes `<runtime>/reports/index.html`, a landing hub linking to the SLA page and the inventory report. Served by the Web host app at `http://<worker>:8080/report_dba/sla.html` and `/report_dba/`. The page is only rewritten when `validate --publish-web` runs (hourly by default), so a browser hard-refresh may be needed to see a new run.

## Useful Manual Queries

```sql
-- Latest run's per-instance results, worst first
SELECT policy_id, target_id, status, actual_percent AS sli, objective_percent AS slo,
       budget_remaining_percent AS budget_left, good_count, total_count, window_hours
FROM sla_results
WHERE sla_run_id = (SELECT MAX(sla_run_id) FROM sla_runs)
ORDER BY status, policy_id, target_id;

-- Recent runs
SELECT sla_run_id, finished_at, status, result_count,
       passed_count, at_risk_count, failed_count, no_data_count
FROM sla_runs ORDER BY sla_run_id DESC LIMIT 10;
```

## Common Issues

- Policy returns `NO_DATA`: check `target_ids`/`db_types`, `metric_codes`, `window_hours`, and whether the metrics app has collected matching samples recently.
- Policy returns `STALE`: the samples exist but are older than the freshness threshold — check daemon timing and collector failures, not the policy.
- Permission errors behind `NO_DATA` on PostgreSQL: grant only `pg_monitor` before considering broader access (see [`04_metrics_engine.md`](./04_metrics_engine.md)).
- A failing instance is hidden: it is not — each instance is a separate `sla_results` row. Use per-instance output/queries; only `aggregate: true` collapses to one number.
- No Telegram message: this is usually correct — nothing changed since the last run and the daily reminder is not due. `logs/` records the reason (`sla.notify suppressed: ...`). Otherwise verify `config.telegram.enabled` and a group for the routed level; `--notify-always` forces a send.
- Web page shows an old run: it only regenerates on the hourly `--publish-web` run; hard-refresh the browser.
- Daemon should not stop on failed compliance: use `--allow-fail`.

## Config Priority

The SLA app resolves its config file using this chain:

1. `--config <path>` CLI argument.
2. `DBABRAIN_SLA_CONFIG` environment variable.
3. `config.sla.json` next to `config.json`, or in the current working directory.
4. `config.json` shared fallback.

The selected source is printed to stderr on startup. App-specific config file: `config.sla.json`.

## The multi-engine contract

Folded in from the former `13_postgresql_metrics_and_sla.md` on 2026-08-15 — that file documented
one engine slice across two apps, so its SLA half belongs here and its metric half in
[`04_metrics_engine.md`](./04_metrics_engine.md). Existing policy fields and commands are
unchanged by the move.

Policies can scope SQL Server, Oracle, PostgreSQL, and MySQL/MariaDB. The app stays pure
post-processing across all of them: it reads `metric_results` and never connects to a monitored
database, so adding an engine is a matter of the metric codes a policy names, not of new
connection code.

`collector -> metric_results -> historical aggregation -> SLI -> SLO evaluation -> SLA rollup -> report`

**Aggregation.** Status success ratio, `minutes` (availability in whole UTC minutes), plus numeric `average`, `minimum`, `maximum`, `sum`,
`count`, `latest`, and p95 `percentile`; comparison operators; rolling-hour windows; minimum
samples; expected collection interval; freshness threshold; maintenance exclusions;
required/optional SLIs; and the missing-data policies `unknown`, `bad`, `good`, `ignore`.

**Availability counted in minutes (`minutes` aggregation).** The cloud SLA rule for uptime: a minute is considered good as long as at least one check within that UTC minute succeeded (`OK` or `LOGGING`), and bad only when every attempt within that minute failed. A minute with no observations is not judged as up or down — it forms the coverage gap (`expected_count = window_hours * 60`, `coverage = observed_minutes / expected * 100`).
Minutes are counted directly in the database store (`SlaStore.minute_counts` and `SlaStore.minute_details`), rather than retrieving raw rows into Python memory. This allows 30-day windows (~1.4 million samples) and closed previous months to be evaluated efficiently in standard SQL. Collection failures (`COLLECTION_FAILURE_TYPES`) and scheduled maintenance windows are excluded inside the query.

**Multi-window multi-burn-rate alerts (Google SRE Workbook, ch. 5).** Policies with `time_slo` and `minutes` aggregation can opt into `burn_rate_alerts`, specifying pairs of short and long windows:
- A pair fires when **both** its short and long windows burn at or above its factor (`error_rate / error_budget >= factor`).
- Fast burns (`severity: "page"`, e.g. 14.4x over 1h/5m or 6x over 6h/30m) escalate `PASSED` or `AT_RISK` policies to **`BURNING`** status. `BURNING` is ranked immediately after `FAILED` for Telegram critical routing and web presentation, alerting on call immediately when active burn begins and recovering when burn subsides. It does not fail the overall run or exit code.
- Slow burns (`severity: "ticket"`, e.g. 1x over 3d/6h) are reported on the SLA page without changing policy status.
- The shipped policy `INSTANCE_AVAILABILITY_30D` (720 hours, 99.9% target, 1440 minimum samples) configures the three standard SRE burn pairs, and `APP-SLA-VALIDATE` runs every 300 seconds (`repeat_interval: 300`) to evaluate 5-minute windows promptly.

**Calendar month availability reporting.** Policies with `calendar_month_report: true` generate an **Availability by calendar month** table on the published web report, detailing availability %, down minutes, and coverage % for both "this month to date" and "previous closed month". Month boundaries follow the local display timezone (`dbabrain.lib.timezone.display_zone()`). Because older data moves to `metric_results_archive` (which lacks secondary indexes), the closed previous month is computed once with `include_archive=True` and cached in `<runtime_dir>/sla/months/<policy_id>_<YYYY-MM>.json`; later runs read the cache file. A month with no measured minute is not cached (a store that holds none of its rows would otherwise fix it at nothing for ever), and nothing is cached when no runtime folder is named - there is no default folder. Moving a month's file away makes the next run count it again. The month table, the 30-day policy and the five burn windows read their minutes in two queries a policy; on the estate's store the 30-day pair took ~10 s, so a 5-minute run takes ~35-40 s.

**A latency SLO is a share of good samples, not a percentile of mixed rows** (1.155, 0.30.0).
`SQLSERVER_IO_LATENCY_24H` reads `PERFORMANCE_IO_LATENCY_INTERVAL`, which grades each 10-second sample
by file type (log writes 20 / 50 ms, data reads 50 / 200 ms), and asks that 99 % of samples be good -
the form Google's SRE book gives a latency SLO. A `percentile` over that metric's values would put a
log write and a data read in one number against one threshold, because a policy cannot filter items;
`percentile` stays for a metric whose rows all mean the same thing. The policy is its own, not part
of `SQLSERVER_PERFORMANCE_24H`: a time-SLO pools rows, and up to two per database every 300 s would
outweigh every other metric there.

**Missing observations are not automatically downtime.** `NO_DATA`, `STALE`,
`INSUFFICIENT_DATA`, and maintenance-excluded observations stay distinct — collapsing them into
"failed" would report an outage every time the collector itself was the thing that broke. A
critical *required* SLI fails the overall rollup; optional failures stay visible without masking
required health. Coverage is actual samples over expected samples; burn rate is observed failure
percentage over allowed failure percentage.

**Examples** live in `data/sla_policies.example.json`, which carries one policy per shape with the reasoning beside it:
standalone, primary with two replicas, standby, Docker, HA lab, target/database overrides,
maintenance, heavy scheduling, OS exclusions, and Telegram summary configuration.

A primary with `expected_replica_count=0` is healthy with no replicas; a configured expected
count that is not met is critical.

## Standalone Mode vs Full-Suite Mode

**Full-suite mode** (default): reads `config.json` and the shared runtime store written by the metrics app.

**Standalone mode**: copy `config.sla.json` and `data/sla_policies.json` next to the EXE. The store must resolve to the same database the metrics app writes. If the metrics app has never run, all policies return `NO_DATA`. Telegram/web delivery additionally need the relevant `config.json` sections (`telegram`, `runtime_dir`).

Required config keys: `log_dir`, a resolvable runtime store (plus `runtime_dir` for `--publish-web`, `telegram` for `--notify`).

## EXE Packaging Notes

- The `--policies` path defaults to `data/sla_policies.json` relative to the package. Pass it explicitly when running as EXE.
- The app reads `metric_results` and writes only its own `sla_runs` / `sla_results` (and, on request, `telegram_send_messages` and HTML files). It writes no metric data.
