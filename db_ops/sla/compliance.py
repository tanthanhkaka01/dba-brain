from __future__ import annotations

import sqlite3
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db_ops.lib.rows import row_text
from db_ops.sla.models import SlaPolicy, SlaPolicyResult, SlaValidationSummary
from db_ops.sla.storage import SlaStore


# Placeholder target_id for an aggregate (fleet-wide) result or a policy with no samples.
AGGREGATE_TARGET = "*"


def validate_sla_policies(
    *,
    sqlite_path: str | Path,
    policies: list[SlaPolicy],
    window_end: str | None = None,
    data_dir: str | Path | None = None,
) -> SlaValidationSummary:
    """For each policy, compute the SLI per instance from ``metric_results`` and evaluate it
    against the policy's SLO. Pure read: this never connects to a monitored database and never
    writes. Persisting the returned summary is the caller's job (see ``SlaStore.save_summary``).

    ``data_dir`` is where the enabled-target inventory is read from, because the evaluated
    population comes from the inventory rather than from whichever samples are in the window
    (see :func:`_expected_scope`). It is a parameter rather than a module default so a caller
    evaluating a synthetic store is not silently measured against the production estate.
    """
    end_dt = _parse_time(window_end) if window_end else datetime.now(timezone.utc)
    end_text = _format_time(end_dt)
    store = SlaStore(sqlite_path)
    results: list[SlaPolicyResult] = []
    for policy in policies:
        results.extend(_evaluate_policy(store=store, policy=policy, window_end=end_dt,
                                        data_dir=data_dir))
    results_tuple = tuple(results)
    passed_count = sum(1 for result in results_tuple if result.status == "PASSED")
    at_risk_count = sum(1 for result in results_tuple if result.status == "AT_RISK")
    failed_count = sum(1 for result in results_tuple if result.status == "FAILED")
    no_data_count = sum(1 for result in results_tuple if result.status == "NO_DATA")
    required_failure = any(result.required and result.status in {"FAILED", "NO_DATA", "STALE", "INSUFFICIENT_DATA"} for result in results_tuple)
    status = "FAILED" if required_failure else "PASSED"
    return SlaValidationSummary(
        status=status,
        policy_count=len(policies),
        result_count=len(results_tuple),
        passed_count=passed_count,
        at_risk_count=at_risk_count,
        failed_count=failed_count,
        no_data_count=no_data_count,
        window_end=end_text,
        results=results_tuple,
    )


def _evaluate_policy(*, store: SlaStore, policy: SlaPolicy, window_end: datetime,
                     data_dir: str | Path | None = None) -> list[SlaPolicyResult]:
    window_start = window_end - timedelta(hours=policy.window_hours)
    start_text = _format_time(window_start)
    end_text = _format_time(window_end)
    expected, expected_servers = _expected_scope(policy, data_dir=data_dir)
    rows = store.fetch_metric_samples(
        target_ids=policy.target_ids,
        server_ids=tuple(expected_servers),
        db_types=policy.db_types,
        metric_codes=policy.metric_codes,
        window_start=start_text,
        window_end=end_text,
    )
    if policy.aggregate:
        # One deliberate fleet-wide number across every instance in scope.
        return [_build_result(policy, AGGREGATE_TARGET, rows, start_text, end_text)]

    # The population comes from the enabled inventory, not from whichever samples happen to be in
    # the window. Grouping only the returned rows made the evaluated set move on its own: between
    # runs #688 and #689 five targets reappeared and the totals jumped 204 -> 288 results and
    # 57 -> 79 failures, which read as 22 new incidents and was a population change. It also cut
    # both ways - a target with no qualifying sample simply vanished from the policy instead of
    # saying so, and a target whose metrics had been switched off kept being evaluated for another
    # seven days off its historical samples.
    grouped: dict[str, list[sqlite3.Row]] = {target_id: [] for target_id in expected}
    for row in rows:
        target_id = str(row["target_id"] or "")
        if expected and target_id not in grouped:
            # Samples from a target that is no longer enabled (or no longer in scope). Its
            # history stays in the store; it just stops producing a verdict the moment it is
            # switched off, rather than when its last sample ages out.
            continue
        grouped.setdefault(target_id, []).append(row)

    if not grouped:
        # Nothing in scope at all -> one visible row so the policy does not disappear silently.
        return [_build_result(policy, AGGREGATE_TARGET, [], start_text, end_text)]
    return [
        _build_result(policy, target_id, target_rows, start_text, end_text)
        for target_id, target_rows in sorted(grouped.items())
    ]


def _expected_scope(policy: SlaPolicy, *, data_dir: str | Path | None = None) -> tuple[list[str], list[str]]:
    """The enabled targets this policy applies to, as ``(target_ids, server_ids)``.

    Both come from the same config pass because they answer different halves of one question:
    ``server_id`` says which rows to read out of the store, ``target_id`` says which verdict each
    row belongs to (one machine can serve several databases).

    Built with :func:`db_ops.common.data_sources.load_config_metric_targets`, which
    composes ``<server_id>/<db_type>/<db_name>`` with the same formula the metrics collector uses
    — a second spelling here would invent NO_DATA rows for targets that do have data.

    An empty result means "cannot tell", not "nothing applies": the caller then falls back to
    grouping whatever samples exist, because losing every verdict to an unreadable config file
    would be a worse failure than the drift this replaces.
    """
    try:
        from db_ops.common.data_sources import load_config_metric_targets

        targets = (
            load_config_metric_targets(data_dir=data_dir, require_metrics_enabled=True)
            if data_dir is not None
            else load_config_metric_targets(require_metrics_enabled=True)
        )
    except Exception:  # noqa: BLE001 - a config problem must not empty the whole SLA run.
        return [], []
    wanted_ids = {str(item).strip() for item in (policy.target_ids or ()) if str(item).strip()}
    wanted_types = {str(item).strip().lower() for item in (policy.db_types or ()) if str(item).strip()}
    out = set()
    servers = set()
    for target in targets:
        target_id = str(getattr(target, "target_id", "") or "")
        if not target_id:
            continue
        if wanted_ids and target_id not in wanted_ids:
            continue
        if wanted_types and str(getattr(target, "db_type", "") or "").lower() not in wanted_types:
            continue
        if _policy_is_switched_off_for(policy, getattr(target, "raw_config", None)):
            continue
        out.add(target_id)
        server_id = str(getattr(target, "server_id", "") or "")
        if server_id:
            servers.add(server_id)
    return sorted(out), sorted(servers)


def _policy_is_switched_off_for(policy: SlaPolicy, raw_config) -> bool:
    """True when EVERY metric this policy reads is deliberately disabled on this target.

    A target that is not measured on purpose is out of scope, not a gap. Emitting NO_DATA for it
    would be worse than the disappearance this population fix replaces: the fleet verdict counts
    NO_DATA as a required failure, so switching a metric off on one host would turn the whole
    estate red — and the operator who switched it off is exactly the person who already knows.

    Only "every" counts. A policy reading three metrics of which one still runs is still
    measurable, so it stays in scope and reports on what it has.
    """
    if not isinstance(raw_config, dict):
        return False
    codes = {str(code).strip().upper() for code in (policy.metric_codes or ()) if str(code).strip()}
    if not codes:
        return False
    metrics_config = raw_config.get("metrics") if isinstance(raw_config.get("metrics"), dict) else {}
    overrides = metrics_config.get("metric_overrides") if isinstance(metrics_config.get("metric_overrides"), dict) else {}
    report_policy = raw_config.get("report_policy") if isinstance(raw_config.get("report_policy"), dict) else {}
    disabled_codes = {
        str(code).strip().upper()
        for code in (report_policy.get("disabled_metric_codes") or [])
        if str(code).strip()
    }
    # `cmd` off means every OS_* collector is off on this host — the common case, and the one
    # that produced 12 NO_DATA rows apiece for the OS policies.
    disabled_collectors = {
        str(name).strip().lower()
        for name in (metrics_config.get("disabled_collector_types") or [])
        if str(name).strip()
    }
    for code in codes:
        if code in disabled_codes:
            continue
        override = overrides.get(code)
        if isinstance(override, dict) and override.get("enabled") is False:
            continue
        if "cmd" in disabled_collectors and code.startswith("OS_"):
            continue
        return False
    return True


#: ``error_type`` values that say the collector could not take the measurement. The metrics app
#: already classifies every failure into these (``db_ops.lib.event_policy``), so this is a
#: lookup, not a second attempt at parsing error messages - which would drift from the first.
COLLECTION_FAILURE_TYPES = frozenset({
    "AUTH_FAILED", "CONNECT_FAILED", "QUERY_FAILED", "PERMISSION_DENIED",
})


# `_row_field` is `db_ops.lib.rows.row_text` since 2026-08-16. Coverage must never be the
# thing that kills an SLA run: a stored sample from before a column existed would otherwise
# raise mid-evaluation and lose every policy in the run.


def _is_collection_failure(row) -> bool:
    """True when this row describes the monitoring system, not the monitored service."""
    try:
        error_type = row["error_type"]
    except (KeyError, IndexError, TypeError):
        # Older stored samples predate the column; treat them as measurements, which is what
        # they were read as before this split existed.
        return False
    return str(error_type or "").strip().upper() in COLLECTION_FAILURE_TYPES


def _latest_cycle(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """The newest collection of each metric, and nothing older.

    "Is this configuration compliant?" and "how many objects are affected?" are both questions
    about now. Answering them from a window mixes a problem that was fixed on Monday into
    Friday's verdict, and — worse for the inventory model — counts the same object once per
    snapshot, which is how 1,503 stale statistics were reported as 3,377 failures.

    Per metric_code, not globally: a policy reading two metrics on different cadences would
    otherwise lose the slower one entirely the moment the faster one ran.
    """
    newest: dict[str, str] = {}
    for row in rows:
        code = row_text(row, "metric_code")
        collected = row_text(row, "collected_at")
        if collected > newest.get(code, ""):
            newest[code] = collected
    return [row for row in rows if row_text(row, "collected_at") == newest.get(row_text(row, "metric_code"))]


def _build_result(
    policy: SlaPolicy,
    target_id: str,
    rows: list[sqlite3.Row],
    window_start: str,
    window_end: str,
) -> SlaPolicyResult:
    rows = [row for row in rows if not _is_maintenance_sample(str(row["collected_at"]), policy.maintenance_windows)]
    good_statuses = {status.upper() for status in policy.good_statuses}

    # An observation the collector could not take is not an observation of the service. Splitting
    # it out is the difference between "backups are failing" and "we cannot log in to look at the
    # backups" - the same row used to say both. On 192.0.2.250 all 26 bad observations in
    # SQLSERVER_BACKUP_JOB_7D were authentication failures recorded while the collector could not
    # connect; none of them was a backup-job result. Counting them as service failures also
    # multiplies ONE collector incident across every policy that touches that target.
    #
    # The rows are not discarded: they drive the data-quality verdict and the coverage figure, so
    # a target nobody can reach reads as unmeasured rather than as compliant OR as failing.
    collection_rows = [row for row in rows if _is_collection_failure(row)]
    measured_rows = [row for row in rows if not _is_collection_failure(row)]

    # Which rows answer this policy's question. A time SLO reads the whole window because the
    # samples ARE time; the other two read only the newest cycle, because "was this config wrong
    # last Tuesday" and "how many objects are affected" are both answered by the present, and
    # averaging them over a window produces a number with no referent.
    #
    # window_rows stays the FULL measured set no matter which model is in play, because coverage
    # and freshness are questions about collection, not about the verdict. Narrowing them to the
    # newest cycle would report every current_state policy as ~1% covered — one observed cycle
    # against a day of expected ones — and turn a working collector into a data-quality alarm.
    window_rows = measured_rows
    latest_rows = _latest_cycle(measured_rows)
    # The present tense, kept for EVERY model — including time_slo, whose window figure is
    # exactly the one that reads as an active incident when the incident is already over.
    current_status = ("" if not latest_rows
                      else "OK" if all(str(row["status"] or "").upper() in good_statuses for row in latest_rows)
                      else "BAD")
    if policy.policy_model in ("current_state", "finding_inventory"):
        measured_rows = latest_rows

    total_count = len(measured_rows)
    good_count = sum(1 for row in measured_rows if str(row["status"] or "").upper() in good_statuses)
    bad_rows = [row for row in measured_rows if str(row["status"] or "").upper() not in good_statuses]
    # Distinct objects, not observations. On 192.0.2.115 the maintenance backlog was 1,503
    # statistics seen across repeated collections, reported as 3,377 bad rows; an operator has
    # 1,503 things to fix, and the larger number only measures how often we looked.
    affected_objects = len({row_text(row, "metric_item") or row_text(row, "metric_code") for row in bad_rows})
    actual_value = _aggregate_value(policy, measured_rows, good_count)
    if policy.policy_model == "current_state":
        # Binary, not a share of objects. "Compliant" means nothing in the newest look is wrong;
        # one host pending a reboot out of thirty is not 96.7% compliant with a reboot policy, it
        # is non-compliant with one host to fix — and affected_objects says how many.
        actual_value = 0.0 if bad_rows else 100.0
    if policy.policy_model == "finding_inventory":
        # The answer is a count of open findings, not a share of rows. Comparing it against a
        # percentage objective is what turned a real backlog into "4.09% maintenance compliance".
        actual_value = float(affected_objects)
    actual_percent = round(actual_value, 4) if actual_value is not None else 0.0
    # No measured sample is NO_DATA even when collection rows exist - especially then. A policy
    # whose every sample was an auth failure knows nothing about the service, and reporting that
    # as 100% compliant would be worse than the old behaviour, not better.
    no_data = not window_rows
    # Coverage counts COLLECTION CYCLES, not rows. One collection writes one row per database,
    # index or login it found, so MAINTENANCE_INDEX_USAGE on 192.0.2.8 is 6,685 rows from three
    # cycles: comparing rows against expected cycles reads as 100% covered on a metric that ran
    # three times in a day. A cycle is one (metric_code, collected_at) pair, and a policy reading
    # several metrics expects one cycle of each.
    #
    # Cycles that produced only collection failures do not count as covered — that is the whole
    # point of separating them: an hour nobody could log in is an hour not measured.
    observed_cycles = len({
        (row_text(row, "metric_code"), row_text(row, "collected_at")) for row in window_rows
    })
    expected_count = 0
    if policy.expected_collection_interval_seconds:
        per_metric = max(1, int(policy.window_hours * 3600 / policy.expected_collection_interval_seconds))
        expected_count = per_metric * max(1, len(policy.metric_codes or ()))
    if expected_count:
        coverage = round(min(100.0, observed_cycles / expected_count * 100), 2)
    elif collection_rows:
        # No configured interval to measure against, but we know some attempts failed: report the
        # share that produced a measurement rather than the flat 100% that made a target nobody
        # could log into read as fully covered.
        attempted = len(window_rows) + len(collection_rows)
        coverage = round(len(window_rows) / attempted * 100, 2) if attempted else 0.0
    else:
        coverage = 100.0 if window_rows else 0.0
    freshness = None
    if rows:
        freshness = max(0.0, (_parse_time(window_end) - max(_parse_time(str(row["collected_at"])) for row in rows)).total_seconds())
    stale = bool(policy.freshness_threshold_seconds is not None and freshness is not None and freshness > policy.freshness_threshold_seconds)
    # Sample sufficiency is about the window too: a current_state policy reads one cycle by
    # design, and judging that single cycle against a 20-sample minimum would mark every one of
    # them INSUFFICIENT_DATA forever.
    insufficient = bool(window_rows and len(window_rows) < policy.minimum_sample_count)
    objective_value = policy.target_value if policy.target_value is not None else policy.objective_percent
    compliant = None if no_data or stale or insufficient or actual_value is None else _compare(actual_value, objective_value, policy.comparison_operator)

    # Error budget: how much non-compliance the SLO tolerates, and how much is left.
    # Consumed is clamped to [0, 100] so a large breach reads as "budget exhausted"
    # rather than an unbounded negative number. For NO_DATA the budget is undefined.
    # Only a time SLO has an error budget: the budget is "how much of the window may be bad", and
    # neither a present-tense yes/no nor a count of affected objects is measured in window time.
    budgeted = policy.aggregation == "success_ratio" and policy.policy_model == "time_slo"
    error_budget_percent = round(100.0 - policy.objective_percent, 4) if budgeted else 0.0
    bad_percent = round(100.0 - actual_percent, 4) if budgeted else 0.0
    if no_data:
        budget_consumed_percent = 0.0
        budget_remaining_percent = 0.0
    else:
        if error_budget_percent > 0:
            raw_consumed = (bad_percent / error_budget_percent) * 100
        else:
            # A 100% objective has no budget: any bad sample fully consumes it.
            raw_consumed = 0.0 if bad_percent <= 0 else 100.0
        budget_consumed_percent = round(min(100.0, max(0.0, raw_consumed)), 2)
        budget_remaining_percent = round(100.0 - budget_consumed_percent, 2)

    # Collection loss is reported as a data-quality verdict, never as a service verdict. It ranks
    # above STALE/INSUFFICIENT because being unable to look at all is the more basic problem.
    quality_status = (
        "NO_DATA" if no_data
        else "COLLECTION_FAILED" if collection_rows
        else "STALE" if stale
        else "INSUFFICIENT_DATA" if insufficient
        else "OK"
    )
    if no_data and policy.missing_data_policy in {"good", "ignore"}:
        compliant = True
        quality_status = "MISSING_EXCLUDED"
    elif no_data and policy.missing_data_policy == "bad":
        compliant = False
    status = _resolve_status(
        no_data=no_data and policy.missing_data_policy == "unknown",
        meets_objective=bool(compliant),
        budget_remaining_percent=budget_remaining_percent,
        at_risk_budget_percent=policy.at_risk_budget_percent,
    )
    if stale:
        status = "STALE"
    elif insufficient:
        status = "INSUFFICIENT_DATA"
    burn_rate = round(bad_percent / error_budget_percent, 4) if error_budget_percent > 0 and not no_data else None
    return SlaPolicyResult(
        policy_id=policy.policy_id,
        name=policy.name,
        target_id=target_id,
        status=status,
        objective_percent=policy.objective_percent,
        actual_percent=actual_percent,
        error_budget_percent=error_budget_percent,
        budget_consumed_percent=budget_consumed_percent,
        budget_remaining_percent=budget_remaining_percent,
        total_count=total_count,
        good_count=good_count,
        bad_count=len(bad_rows),
        no_data=no_data,
        window_hours=policy.window_hours,
        window_start=window_start,
        window_end=window_end,
        scope=_scope_text(policy),
        category=policy.category,
        failures_by_status=_count_by_status(bad_rows),
        sli_code=policy.sli_code or policy.policy_id,
        domain=policy.category,
        aggregation=policy.aggregation,
        policy_model=policy.policy_model,
        affected_objects=affected_objects,
        current_status=current_status,
        comparison_operator=policy.comparison_operator,
        actual_value=actual_value,
        objective_value=objective_value,
        unit=policy.unit,
        compliant=compliant,
        expected_sample_count=expected_count,
        coverage_percent=coverage,
        data_quality_status=quality_status,
        data_freshness_seconds=freshness,
        reason=_quality_reason(quality_status, total_count, policy.minimum_sample_count),
        required=policy.required,
        error_budget_total=error_budget_percent,
        error_budget_consumed=min(error_budget_percent, bad_percent),
        error_budget_remaining=max(0.0, error_budget_percent - bad_percent),
        burn_rate=burn_rate,
    )


def _aggregate_value(policy: SlaPolicy, rows: list[sqlite3.Row], good_count: int) -> float | None:
    if not rows:
        return None
    if policy.aggregation == "success_ratio":
        return (good_count / len(rows)) * 100
    values = []
    for row in rows:
        try:
            value = float(row["metric_value"])
            if math.isfinite(value):
                values.append(value)
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    operations = {
        "average": lambda: sum(values) / len(values), "minimum": lambda: min(values), "maximum": lambda: max(values),
        "sum": lambda: sum(values), "count": lambda: float(len(values)), "latest": lambda: values[-1],
        "percentile": lambda: _percentile(values, policy.percentile),
    }
    return round(float(operations[policy.aggregation]()), 6)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _compare(actual: float, objective: float, operator: str) -> bool:
    return {">=": actual >= objective, ">": actual > objective, "<=": actual <= objective,
            "<": actual < objective, "==": actual == objective, "!=": actual != objective}[operator]


def _is_maintenance_sample(collected_at: str, windows: tuple[tuple[str, str], ...]) -> bool:
    return any(start <= collected_at <= end for start, end in windows)


def _quality_reason(status: str, count: int, minimum: int) -> str:
    if status == "NO_DATA":
        return "No observations were collected in the evaluation window."
    if status == "STALE":
        return "The newest observation exceeds the configured freshness threshold."
    if status == "INSUFFICIENT_DATA":
        return f"Only {count} samples are available; at least {minimum} are required."
    if status == "MISSING_EXCLUDED":
        return "Missing observations were excluded by policy."
    return "Observations are sufficiently fresh and complete for evaluation."


def _resolve_status(
    *,
    no_data: bool,
    meets_objective: bool,
    budget_remaining_percent: float,
    at_risk_budget_percent: float,
) -> str:
    if no_data:
        return "NO_DATA"
    if not meets_objective:
        return "FAILED"
    if budget_remaining_percent <= at_risk_budget_percent:
        return "AT_RISK"
    return "PASSED"


def _scope_text(policy: SlaPolicy) -> str:
    if policy.target_ids:
        return "targets:" + ",".join(policy.target_ids)
    if policy.db_types:
        return "db_types:" + ",".join(policy.db_types)
    return ""


def _count_by_status(rows: list[sqlite3.Row]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row["status"] or "").upper()
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _parse_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
