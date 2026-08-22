# SLA/SLO Compliance App

## Purpose

The SLA/SLO Compliance App turns the metric history already collected by the Metrics Engine into service-level reporting. For each policy it computes a **Service Level Indicator (SLI)** — the fraction of "good" metric samples in a time window — and evaluates it against a **Service Level Objective (SLO)** with an **error budget**. It is pure post-processing: it **never connects to a monitored database**. Its only input is the `metric_results` table; its only outputs are the `sla_runs` / `sla_results` tables and (optionally) a Telegram message and an HTML page.

## Package / Files

- `db_ops/sla/` — `models.py`, `policies.py`, `compliance.py` (SLI/SLO compute), `storage.py` (`SlaStore`), `publish.py` (Telegram + web), `cli.py`.
- `data/sla_policies.json` — policy definitions.
- the runtime store declared in `data/store_config.json` — reads `metric_results`; writes `sla_runs` / `sla_results`.
- `runtime/reports/sla.html`, `runtime/reports/index.html` — published web pages (with `--publish-web`).

## Concepts

- **SLI** = `good_samples / total_samples * 100` over the window, per instance.
- **SLO** = `objective_percent` sustained over `window_hours`.
- **Error budget** = `100 - objective_percent`. `budget_remaining_percent` shows how much of that tolerance is left (clamped to `[0, 100]`; undefined/`0` for NO_DATA).
- **Per-instance by default**: a policy is evaluated **once per `target_id`** (one monitored instance/database). Scope (`db_types` or `target_ids`) only selects *which* instances the policy applies to; results are never lumped together unless `aggregate: true` is set.
- **Statuses**: `PASSED` (meets SLO), `AT_RISK` (still meets SLO but `budget_remaining_percent <= at_risk_budget_percent`), `FAILED` (below SLO), `NO_DATA` (no samples in scope). A run's overall status is `FAILED` if any result is `FAILED` or `NO_DATA`.

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
python -m db_ops.sla.cli --config config.json validate --format text

# Full scheduled form: store + Telegram + web page, never fail the daemon:
python -m db_ops.sla.cli --config config.json validate --format text --notify --publish-web --allow-fail

# Compute only, no writes:
python -m db_ops.sla.cli --config config.json validate --no-store

# Evaluate against a fixed window end (backfill/debug):
python -m db_ops.sla.cli --config config.json validate --window-end 2026-05-28T04:00:00Z

# Read stored history:
python -m db_ops.sla.cli --config config.json history --limit 10
python -m db_ops.sla.cli --config config.json history --run-id 2
```

`validate` flags: `--policies <path>`, `--window-end <iso>`, `--format json|text`, `--no-store`, `--notify`, `--notify-always`, `--publish-web`, `--web-dir <dir>`, `--allow-fail`.

`data/app_commands.json` runs `APP-SLA-VALIDATE` on the **worker**, hourly (`repeat_interval: 3600`):

```text
python -m db_ops.sla.cli --config config.json validate --format text --notify --publish-web --allow-fail
```

## Delivery Channels

### What the page shows

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
| `time_slo` (default) | the whole window | % of good samples | availability, connectivity, saturation, backup RPO, latency |
| `current_state` | the newest cycle per metric | binary: `compliant` / `not compliant` | reboot pending, security configuration, service state, latest CHECKDB |
| `finding_inventory` | the newest cycle per metric | count of distinct affected objects | stale statistics, fragmented indexes, risky roles, configuration exceptions |

Why the split — measured on production, 2026-08-05:

- `POSTGRESQL_SECURITY_24H` reported **50% security compliance** from one warning (`postgres@cluster` holds superuser) plus one good row. As `current_state` it reads *not compliant, 1 affected*.
- `SQLSERVER_MAINTENANCE_7D` reported **4.09%** from 144 good and 3,377 bad rows covering 1,631 distinct objects. As `finding_inventory` it reads **1,631 objects affected** — the number someone has to work through, instead of a figure that measures how often the collector looked.
- `SQLSERVER_RECOVERY_CHECKDB_7D` averaged 125 snapshots of 39 databases, diluting this morning's CHECKDB with a week of the old state.

Constraints the parser enforces, because both failure modes are silent:

- An unrecognised `policy_model` **fails the config**. A typo that fell back to `time_slo` would restore the old misreporting without a log line.
- A `finding_inventory` **must** set `comparison_operator` to `<=` or `<` and **must** set `target_value`. Left at the default `>= 99`, a backlog of 1,631 objects compares as `1631 >= 99` and reports PASSED — healthy precisely when it is worst.

Coverage, freshness, `NO_DATA` and `minimum_sample_count` are always judged on the **full window**, never on the narrowed cycle: they are questions about the collector, not about the verdict. Error budget and burn rate apply to `time_slo` only — a count of objects has no budget to burn.

**Telegram (`--notify`) — notifies on state change, not on schedule.** The run is hourly; the message is not. Each run's `{policy @ target: status}` map is compared against the previous stored run (`SlaStore.fetch_previous_state`) and a message is queued only when something crossed a line:

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

Routed by severity to `config.telegram.groups`: any `FAILED` -> `critical`/`error` group, any `AT_RISK`/`NO_DATA` -> `warning` group, else `logging` group. The message's own emoji reflects the **change** (new failure -> critical, recovery-only -> success), applied centrally by `db_ops/telegram/severity.py`; the producer does not tag. `--notify-always` bypasses all suppression. Requires `config.telegram.enabled = true` and a group for the routed level; otherwise it logs a skip (never errors).

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
2. `DB_OPS_SLA_CONFIG` environment variable.
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

**Aggregation.** Status success ratio, plus numeric `average`, `minimum`, `maximum`, `sum`,
`count`, `latest`, and p95 `percentile`; comparison operators; rolling-hour windows; minimum
samples; expected collection interval; freshness threshold; maintenance exclusions;
required/optional SLIs; and the missing-data policies `unknown`, `bad`, `good`, `ignore`.

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
count that is not met is critical. Calendar week/month windows remain future work.

## Standalone Mode vs Full-Suite Mode

**Full-suite mode** (default): reads `config.json` and the shared runtime store written by the metrics app.

**Standalone mode**: copy `config.sla.json` and `data/sla_policies.json` next to the EXE. The store must resolve to the same database the metrics app writes. If the metrics app has never run, all policies return `NO_DATA`. Telegram/web delivery additionally need the relevant `config.json` sections (`telegram`, `runtime_dir`).

Required config keys: `log_dir`, a resolvable runtime store (plus `runtime_dir` for `--publish-web`, `telegram` for `--notify`).

## EXE Packaging Notes

- The `--policies` path defaults to `data/sla_policies.json` relative to the package. Pass it explicitly when running as EXE.
- The app reads `metric_results` and writes only its own `sla_runs` / `sla_results` (and, on request, `telegram_send_messages` and HTML files). It writes no metric data.
