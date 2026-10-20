from __future__ import annotations

from dbabrain.lib import errors
from dbabrain.lib.coerce import as_optional_int as _optional_int, as_text
from dbabrain.lib.text_format import format_utc as _format_utc  # noqa: F401 - one definition, see that module

import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dbabrain.lib import data_sources
from dbabrain.lib.shell import SHELL_BASH, SHELL_POWERSHELL, powershell_executable
from dbabrain.lib.event_policy import (
    PHASE_CONNECT,
    PHASE_EXECUTE,
    normalize_error_signature,
    normalize_error_type,
    resolve_failure_phase,
)
from dbabrain.lib.sql_text import apply_sql_settings
from dbabrain.lib.target_profile import TargetProfile, candidate_variants, select_variant, version_matches
from dbabrain.lib.time_window import is_time_window_open, job_due
from dbabrain.lib.timezone import to_display
from dbabrain.lib.config import DbaBrainConfig
from dbabrain.lib.file_lock import FileLock, LockHeld
from dbabrain.metrics.definitions import DEFAULT_DEFINITIONS_PATH, load_max_parallel_servers, load_metric_definitions
from dbabrain.metrics import batch
from dbabrain.metrics.batch import Prepared
from dbabrain.metrics.executor import prepare_sql
from dbabrain.metrics.importance import DEFAULT_OVERRIDES_PATH, load_metric_importance_overrides, resolve_metric_importance
from dbabrain.metrics.models import CollectSummary, MetricDefinition, MetricResult, MetricTarget
from dbabrain.metrics.storage import MetricStore
from dbabrain.metrics.targets import DEFAULT_DATA_DIR, explain_no_targets, load_metric_targets


NORMALIZED_RESULT_FIELDS = ("metric_item", "metric_value", "metric_unit", "status", "message")
SUPPORTED_CMD_PLATFORMS = {"windows", "linux"}
SUPPORTED_RESULT_STATUSES = {"OK", "WARN", "WARNING", "CRITICAL", "UNKNOWN", "ERROR", "NO_DATA", "LOGGING"}


class UnsupportedCollectorType(errors.DbaBrainError, RuntimeError):
    kind = errors.KIND_CONFIG
    pass


class UnsupportedExecutionMethod(errors.DbaBrainError, RuntimeError):
    kind = errors.KIND_CONFIG
    pass


# `_safe_text` is `dbabrain.lib.coerce.as_text` since 2026-08-16 — the same five lines were here
# and in the other of `common`/`metrics`.


@dataclass(frozen=True)
class CommandExecution:
    rows: list[dict[str, Any]]
    raw_stdout: str
    raw_stderr: str
    exit_code: int
    execution_time: float


@dataclass(frozen=True)
class SqlExecution:
    """What a SQL metric's answer says beside its rows: how long it took - the connect, the
    statement and the close, as ``metric-batch`` timed it. Read where a ``CommandExecution`` is
    read; a SQL metric has no streams and no exit code, so those stay empty on its rows."""

    execution_time: float | None
    raw_stdout: None = None
    raw_stderr: None = None
    exit_code: None = None


@dataclass
class _Tally:
    """What one pass — or one batch's share of it — counted.

    The per-batch workers each fill their own tally and the parent merges them, so no counter is
    written from two threads and the totals cannot go wrong under a wider fan-out.
    """

    metric_count: int = 0
    executed_count: int = 0
    skipped_interval_count: int = 0
    skipped_window_count: int = 0
    skipped_claimed_count: int = 0
    disabled_count: int = 0
    result_count: int = 0
    ok_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    critical_count: int = 0
    no_data_count: int = 0
    messages: list[str] = field(default_factory=list)

    def merge(self, other: "_Tally") -> None:
        self.metric_count += other.metric_count
        self.executed_count += other.executed_count
        self.skipped_interval_count += other.skipped_interval_count
        self.skipped_window_count += other.skipped_window_count
        self.skipped_claimed_count += other.skipped_claimed_count
        self.disabled_count += other.disabled_count
        self.result_count += other.result_count
        self.ok_count += other.ok_count
        self.error_count += other.error_count
        self.warning_count += other.warning_count
        self.critical_count += other.critical_count
        self.no_data_count += other.no_data_count
        self.messages.extend(other.messages)


def _collect_target(
    *,
    target: MetricTarget,
    definitions: list[MetricDefinition],
    overrides: Any,
    secrets: dict[str, str],
    store: MetricStore | None,
    run_id: int | None,
    started: datetime,
    dry_run: bool,
    force: bool,
    include_windowed: bool,
    tally: _Tally,
) -> None:
    """One target's metrics on their own: planned, checked for their interval one by one, and run.

    A pass does not come through here since 0.30.0 - it plans every target, reads every interval in
    one query and runs the due metrics in batches by server and category. This is the same three
    steps for one target, kept for a caller that holds exactly one.
    """
    due: list[tuple[MetricDefinition, int]] = []
    for metric, importance in _plan_target(target=target, definitions=definitions, overrides=overrides,
                                           started=started, dry_run=dry_run,
                                           include_windowed=include_windowed, tally=tally):
        if not force and store is not None and not _metric_due(store=store, target=target, metric=metric, now=started):
            _skip_interval(target=target, metric=metric, tally=tally)
            continue
        due.append((metric, importance))
    if not dry_run:
        _run_target_items(target=target, items=due, secrets=secrets, store=store, run_id=run_id,
                          tally=tally)


def _plan_target(
    *,
    target: MetricTarget,
    definitions: list[MetricDefinition],
    overrides: Any,
    started: datetime,
    dry_run: bool,
    include_windowed: bool,
    tally: _Tally,
    quiet: bool = False,
) -> list[tuple[MetricDefinition, int]]:
    """The metrics this target may run now, with their importance - every check but the interval.

    Each metric left out is counted, and printed with its reason unless ``quiet``. The interval is
    not read here: a pass reads every pair's last collection in one query
    (``MetricStore.latest_result_times``), not two queries per pair, and the store opens a
    connection per call.
    """
    def skipped(metric: MetricDefinition, reason: str, *, dry: bool) -> None:
        if not quiet:
            _print_metric_skip(target=target, metric=metric, reason=reason, dry_run=dry)

    target_metrics = [item for item in definitions if _metric_supports_db_type(item, target.db_type, include_unsupported=True)]
    planned: list[tuple[MetricDefinition, int]] = []
    for metric in target_metrics:
        unsupported_reason = _metric_unsupported_reason(metric, target)
        if unsupported_reason:
            tally.disabled_count += 1
            tally.messages.append(f"SKIP unsupported {metric.metric_code} target={target.target_id}: {unsupported_reason}")
            skipped(metric, f"unsupported: {unsupported_reason}", dry=dry_run)
            continue
        if not _metric_enabled_for_target(metric, target):
            tally.disabled_count += 1
            tally.messages.append(f"SKIP disabled {metric.metric_code} target={target.target_id}")
            skipped(metric, "disabled by target metric config", dry=dry_run)
            continue
        importance = resolve_metric_importance(metric, target, overrides)
        if importance == 0:
            tally.disabled_count += 1
            tally.messages.append(f"SKIP disabled {metric.metric_code} target={target.target_id}")
            skipped(metric, "disabled by importance override", dry=dry_run)
            continue
        tally.metric_count += 1
        # The window is checked even under --force, because the two are answers to
        # different questions. --force means "never mind how recently this ran"; a
        # window means "this metric may only touch the database at these hours". The
        # metrics confined to 01-06h are the expensive ones - DBCC CHECKDB, index
        # fragmentation scans, a restore validation - and an on-demand report typed at
        # 15:00 was dragging every one of them onto a production instance. Ignoring a
        # window has to be asked for separately, by name.
        if not include_windowed and not _metric_window_open(metric=metric, now=started):
            tally.skipped_window_count += 1
            tally.messages.append(f"SKIP window {metric.metric_code} target={target.target_id}")
            skipped(metric, f"outside its allowed window ({_window_label(metric)})", dry=False)
            continue
        if dry_run:
            _print_line(f"DRY-RUN {metric.metric_code} target={target.target_id} importance={importance}")
            continue
        planned.append((metric, importance))
    return planned


def _skip_interval(*, target: MetricTarget, metric: MetricDefinition, tally: _Tally,
                   quiet: bool = False) -> None:
    """Count a metric left out because its interval has not elapsed, and print it unless ``quiet``."""
    tally.skipped_interval_count += 1
    tally.messages.append(f"SKIP interval {metric.metric_code} target={target.target_id}")
    if quiet:
        return
    _print_metric_skip(
        target=target,
        metric=metric,
        reason=f"interval not elapsed ({_repeat_interval_for(metric, target)}s)",
        dry_run=False,
    )


def _run_target_items(
    *,
    target: MetricTarget,
    items: list[tuple[MetricDefinition, int]],
    secrets: dict[str, str],
    store: MetricStore | None,
    run_id: int | None,
    tally: _Tally,
) -> None:
    """Run one target's due metrics in order, as few ``metric-batch`` processes as they allow.

    The metrics go to `common` together - one process per target, not one per metric (rules R03,
    R10; the start-up cost is measured in test_app_common_imports.py) - except a windowed one.
    """
    pending: list[tuple[MetricDefinition, int]] = []
    for metric, importance in items:
        tally.executed_count += 1
        if metric.schedule_window is not None:
            # A windowed metric is one of the expensive ones - DBCC CHECKDB, an index scan, a
            # restore validation - and runs in a batch of its own, so the minutes it takes never
            # hold the quick metrics' rows back from the store.
            _flush(pending, target=target, secrets=secrets, store=store, run_id=run_id, tally=tally)
            _flush([(metric, importance)], target=target, secrets=secrets, store=store, run_id=run_id,
                   tally=tally)
            continue
        pending.append((metric, importance))
    _flush(pending, target=target, secrets=secrets, store=store, run_id=run_id, tally=tally)


def _flush(
    pending: list[tuple[MetricDefinition, int]],
    *,
    target: MetricTarget,
    secrets: dict[str, str],
    store: MetricStore | None,
    run_id: int | None,
    tally: _Tally,
) -> None:
    """Run the metrics gathered so far as one batch, then store and count each one, in order."""
    if not pending:
        return
    planned = list(pending)
    pending.clear()
    # No store is a test run (`collect --test-run`): the rows are printed, never inserted.
    assert store is None or run_id is not None
    for (metric, _importance), results in zip(
            planned, _collect_metric_batch(target=target, planned=planned, secrets=secrets)):
        inserted = len(results) if store is None else store.insert_results(run_id=run_id, results=results)
        tally.result_count += inserted
        tally.ok_count += sum(1 for item in results if item.status.upper() in {"OK", "LOGGING"})
        tally.error_count += sum(1 for item in results if item.status.upper() == "ERROR")
        tally.warning_count += sum(1 for item in results if item.status.upper() in {"WARNING", "WARN", "UNKNOWN"})
        tally.critical_count += sum(1 for item in results if item.status.upper() == "CRITICAL")
        tally.no_data_count += sum(1 for item in results if item.status.upper() == "NO_DATA")
        if store is None:
            _print_test_rows(target=target, metric=metric, results=results)
            continue
        _print_metric_progress(
            run_id=run_id,
            target=target,
            metric=metric,
            inserted=inserted,
            results=results,
        )


def collect_metrics(
    *,
    config: DbaBrainConfig,
    db_type: str | None = None,
    target_id: str | None = None,
    metric_code: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    include_windowed: bool = False,
    archive_days: int = 30,
    test_run: bool = False,
) -> CollectSummary:
    """One collection pass - or, with ``test_run``, a trial of the metrics on ONE named target.

    A test run is how a metric is tried on an engine version the estate does not run yet: on a lab
    instance, which the inventory keeps switched off so the scheduler never collects it. It runs
    every metric the target's engine has (the interval does not apply; a window still does unless
    ``include_windowed``), prints every row, and writes nothing - no run row, no results, no health
    rebuild, nothing for an alert to read. The estate's store is the record of the estate, and a
    lab's trial rows in it would be read by every report as a server of the estate.
    """
    if test_run and not target_id:
        raise errors.InvalidRequest("a test run names its one target: --target-id or --target-ip.")
    started = datetime.now(timezone.utc)
    started_at = _format_utc(started)
    definitions = load_metric_definitions(DEFAULT_DEFINITIONS_PATH, active_only=True)
    if metric_code:
        definitions = [item for item in definitions if item.metric_code == metric_code]
    if db_type:
        definitions = [item for item in definitions if _metric_supports_db_type(item, db_type.lower(), include_unsupported=True)]
    if metric_code and not definitions:
        raise errors.InvalidRequest(f"metric_code not found or inactive: {metric_code}")

    overrides = load_metric_importance_overrides(
        DEFAULT_OVERRIDES_PATH,
        known_metric_codes={item.metric_code for item in load_metric_definitions(DEFAULT_DEFINITIONS_PATH)},
    )
    targets = load_metric_targets(data_dir=DEFAULT_DATA_DIR, db_type=db_type, target_id=target_id,
                                  include_switched_off=test_run)
    # Dry-run never connects, so it does not need the (encrypted) secret store. Skipping the
    # load lets `collect --dry-run` run without a --key, as the docs show.
    if test_run:
        # A trial is of one version's files. With no version stated, an unknown version matches
        # every variant and the last one listed answers - for Oracle that is often the 8i file -
        # so the run would report on SQL the target will never be given.
        unversioned = [target.target_id for target in targets
                       if target.db_type and _target_major_version(target) is None]
        if unversioned:
            raise errors.NotConfigured(
                f"a test run needs the target's major_version in db_instances.json: "
                f"{', '.join(unversioned)} states none, and every variant would match it.")
    secrets = {} if dry_run else data_sources.load_secret_text(DEFAULT_DATA_DIR)
    store = None if dry_run or test_run else MetricStore.from_config(config)

    tally = _Tally()
    if not targets:
        # "Not configured" is a state, not a failure - and only if the state says WHAT. This run
        # used to end at `target_count: 0`, which is accurate and tells an operator nothing:
        # an empty inventory, an estate switched off, metrics switched off and a filter that
        # matched nothing all look identical and need four different actions.
        tally.messages.append(explain_no_targets(
            data_sources.load_db_instances(DEFAULT_DATA_DIR),
            db_type=db_type or "", target_id=target_id or "",
        ))

    # The daemon starts this command every second (APP-METRICS is `run_mode: async` since 0.30.0),
    # so a scheduled pass prints no line per metric it leaves out - ~800 pairs, every pass, into
    # metrics_runtime.log. It counts them. A pass run by hand that names what it wants still says
    # why each one was left out.
    quiet = not (dry_run or test_run or target_id or metric_code)

    # Plan every target first - every check but the interval - then read every interval at once.
    # The store has no connection pool, and two queries per (target, metric) pair were ~1,600 new
    # connections a pass on this estate.
    candidates: list[tuple[MetricTarget, MetricDefinition, int]] = []
    for target in targets:
        try:
            planned = _plan_target(target=target, definitions=definitions, overrides=overrides,
                                   started=started, dry_run=dry_run,
                                   include_windowed=include_windowed, tally=tally, quiet=quiet)
        except Exception as exc:  # noqa: BLE001 - one target must not end the pass (F3.1).
            _target_failed(target, exc, tally)
            continue
        candidates.extend((target, metric, importance) for metric, importance in planned)
    if not force and store is not None:
        candidates = _still_due(candidates, store=store, now=started, tally=tally, quiet=quiet)

    # One batch per server and category. A batch runs its targets and metrics one after another,
    # as a whole pass used to run one server; batches run at once and never wait for each other. A
    # pass used to end with its slowest server, and the daemon started no other meanwhile: the
    # nightly index-fragmentation scan (1009 s on the largest instance) held INSTANCE_STATUS of
    # every server for ~18 minutes.
    batches: dict[tuple[str, str], list[tuple[MetricTarget, MetricDefinition, int]]] = {}
    for target, metric, importance in candidates:
        key = (str(target.server_id or target.target_id), str(metric.category or ""))
        batches.setdefault(key, []).append((target, metric, importance))

    def summary(run_id: int | None, finished: datetime, message: str) -> CollectSummary:
        return CollectSummary(
            run_id=run_id,
            target_count=len(targets),
            metric_count=tally.metric_count,
            executed_count=tally.executed_count,
            skipped_interval_count=tally.skipped_interval_count,
            skipped_window_count=tally.skipped_window_count,
            skipped_claimed_count=tally.skipped_claimed_count,
            disabled_count=tally.disabled_count,
            result_count=tally.result_count,
            ok_count=tally.ok_count,
            error_count=tally.error_count,
            warning_count=tally.warning_count,
            critical_count=tally.critical_count,
            no_data_count=tally.no_data_count,
            started_at=started_at,
            finished_at=_format_utc(finished),
            duration_seconds=round((finished - started).total_seconds(), 3),
            message=message,
        )

    if not batches:
        # Nothing to run writes nothing: no run row, no archive, no health rebuild. Most passes
        # are this one now - started a second after the last, finding everything collected.
        return summary(None, datetime.now(timezone.utc), _summary_message(tally.messages))

    # Archive first: before collecting, move metric rows older than archive_days into
    # metric_results_archive so the live table stays small (rows are kept, not deleted).
    archived_count = 0 if store is None else store.archive_old_results(retention_days=archive_days)
    run_id = None if store is None else store.start_run(started_at=started_at, message="Metric collect started.")
    if archived_count:
        tally.messages.append(
            f"Archived {archived_count} metric row(s) older than {archive_days} days into metric_results_archive."
        )
    # A trial prints every row as it comes; one batch at a time keeps one metric's rows together.
    max_workers = 1 if test_run else max(1, min(load_max_parallel_servers(), len(batches)))

    def run_batch(key: tuple[str, str], items: list[tuple[MetricTarget, MetricDefinition, int]]) -> _Tally:
        batch_tally = _Tally()
        if store is None:
            _run_batch_items(items, secrets=secrets, store=store, run_id=run_id, tally=batch_tally)
            return batch_tally
        # An async command must refuse its own duplicates (lib/run_mode.py): another pass may be in
        # the middle of this very batch - that is the point of starting a pass while a slow one
        # runs. The claim is a lock on a file of this node, held by the open handle, so a pass that
        # dies releases it with its process and no claim is ever left behind.
        server, category = key
        claim = FileLock(config.runtime_dir / "metric_claims"
                         / f"{_safe_claim_name(server)}__{_safe_claim_name(category)}", wait=False)
        try:
            claim.__enter__()
        except LockHeld:
            batch_tally.skipped_claimed_count += 1
            batch_tally.messages.append(f"SKIP claimed server={server} category={category}")
            if not quiet:
                _print_line(f"METRIC_SKIP server={server} category={category} reason=claimed by another pass")
            return batch_tally
        try:
            # Read again under the claim: the plan's reading predates it, and the pass that held
            # the claim may have collected these metrics a moment ago.
            if not force:
                items = _still_due(items, store=store, now=started, tally=batch_tally, quiet=quiet)
            _run_batch_items(items, secrets=secrets, store=store, run_id=run_id, tally=batch_tally)
        finally:
            claim.__exit__(None, None, None)
        return batch_tally

    try:
        if max_workers == 1:
            for key, items in batches.items():
                tally.merge(run_batch(key, items))
        else:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="metric-batch") as pool:
                # Merged in plan order, not completion order, so the run message reads the same
                # whatever the hosts do - a pass is compared against yesterday's by eye.
                futures = [pool.submit(run_batch, key, items) for key, items in batches.items()]
                for future in futures:
                    tally.merge(future.result())
        status = "DONE"
    except Exception:
        if store is not None and run_id is not None:
            store.finish_run(
                run_id=run_id,
                finished_at=_format_utc(datetime.now(timezone.utc)),
                status="ERROR",
                target_count=len(targets),
                metric_count=tally.metric_count,
                result_count=tally.result_count,
                error_count=tally.error_count,
                warning_count=tally.warning_count,
                critical_count=tally.critical_count,
                message="Metric collect failed.",
            )
        raise

    finished = datetime.now(timezone.utc)
    message = _summary_message(tally.messages)
    if store is not None and run_id is not None:
        store.finish_run(
            run_id=run_id,
            finished_at=_format_utc(finished),
            status=status,
            target_count=len(targets),
            metric_count=tally.metric_count,
            result_count=tally.result_count,
            error_count=tally.error_count,
            warning_count=tally.warning_count,
            critical_count=tally.critical_count,
            message=message,
        )
        # Bounded by the pass's start: the rows of this run are no older, and metric_results has
        # no index on run_id - unbounded, the read was a scan of ~15 million rows every pass.
        store.rebuild_target_health(run_id=run_id, since=started_at)
    return summary(run_id, finished, message)


def _target_failed(target: MetricTarget, exc: Exception, tally: _Tally) -> None:
    """One target's failure, named in the run message - unless it is the store's, which ends the pass.

    A mistyped `disabled_collector_types` raises by design; it used to fail the whole pass, lose
    every other group's tally and skip the estate-wide health rebuild. Now it is this target's
    error, named in the run message (review 0.25.0, F3.1).
    """
    from dbabrain.lib import store_outage

    if store_outage.is_transient(exc):
        raise exc  # the store is down: the whole pass is, and says so
    tally.error_count += 1
    tally.messages.append(f"{target.server_id or target.target_id}: not collected - {type(exc).__name__}: {exc}")


def _still_due(
    items: list[tuple[MetricTarget, MetricDefinition, int]],
    *,
    store: MetricStore,
    now: datetime,
    tally: _Tally,
    quiet: bool,
) -> list[tuple[MetricTarget, MetricDefinition, int]]:
    """The items whose interval has elapsed, read for all of them in one store query."""
    times = store.latest_result_times(pairs=[(target.target_id, metric.metric_code) for target, metric, _ in items])
    due = []
    for target, metric, importance in items:
        last, success = times.get((target.target_id, metric.metric_code), (None, None))
        if not _is_metric_due(last=last, success=success, metric=metric, target=target, now=now):
            _skip_interval(target=target, metric=metric, tally=tally, quiet=quiet)
            continue
        due.append((target, metric, importance))
    return due


def _run_batch_items(
    items: list[tuple[MetricTarget, MetricDefinition, int]],
    *,
    secrets: dict[str, str],
    store: MetricStore | None,
    run_id: int | None,
    tally: _Tally,
) -> None:
    """One batch's items, target by target in inventory order; one target failing is its own error."""
    by_target: dict[str, tuple[MetricTarget, list[tuple[MetricDefinition, int]]]] = {}
    for target, metric, importance in items:
        by_target.setdefault(target.target_id, (target, []))[1].append((metric, importance))
    for target, target_items in by_target.values():
        try:
            _run_target_items(target=target, items=target_items, secrets=secrets, store=store,
                              run_id=run_id, tally=tally)
        except Exception as exc:  # noqa: BLE001 - one target must not end the pass (F3.1).
            _target_failed(target, exc, tally)


def _collect_one_metric(
    *,
    metric: MetricDefinition,
    target: MetricTarget,
    importance: int,
    secrets: dict[str, str],
    collected_at: str,
    store: MetricStore | None = None,
) -> list[MetricResult]:
    """One metric, run on its own (a batch of one) and graded."""
    def produce() -> tuple[list[dict[str, Any]], CommandExecution | None]:
        if metric.collector_type == "sql":
            return _run_sql_file(metric=metric, target=target, secrets=secrets)
        if metric.collector_type == "cmd":
            command_meta = _run_command_file(metric=metric, target=target, secrets=secrets)
            return command_meta.rows, command_meta
        if metric.collector_type == "docker":
            command_meta = _run_docker_metric(metric=metric, target=target, secrets=secrets)
            return command_meta.rows, command_meta
        raise UnsupportedCollectorType(f"Unsupported collector_type: {metric.collector_type}")

    return _results(metric, target, importance, collected_at, produce)


def _collect_metric_batch(
    *,
    target: MetricTarget,
    planned: list[tuple[MetricDefinition, int]],
    secrets: dict[str, str],
) -> list[list[MetricResult]]:
    """The results of each planned metric, in order, from one ``metric-batch`` for the target.

    A metric that cannot be prepared - no credential, an env secret missing, a path that does not
    resolve - fails here without being sent, exactly as it failed before running in-process.
    Every row is stamped with when *its* metric ran, not when the batch did.
    """
    prepared: list[Prepared | Exception] = []
    for metric, _importance in planned:
        try:
            prepared.append(_prepare(metric, target, secrets))
        except Exception as exc:  # noqa: BLE001 - this metric's failure is its result.
            prepared.append(exc)
    items = [entry.item for entry in prepared if isinstance(entry, Prepared)]
    answers = iter(batch.run(target, secrets, items) if items else [])
    outcomes: list[list[MetricResult]] = []
    for (metric, importance), entry in zip(planned, prepared):
        if isinstance(entry, Exception):
            outcomes.append(_results(metric, target, importance, _format_utc(datetime.now(timezone.utc)),
                                     lambda failure=entry: _raise(failure)))
            continue
        answer = next(answers)
        started = answer.get("started_at")
        collected_at = _format_utc(datetime.fromtimestamp(float(started), timezone.utc)
                                   if started else datetime.now(timezone.utc))
        outcomes.append(_results(metric, target, importance, collected_at,
                                 lambda entry=entry, answer=answer: entry.interpret(answer)))
    return outcomes


def _raise(exc: Exception) -> Any:
    raise exc


def _prepare(metric: MetricDefinition, target: MetricTarget, secrets: dict[str, str]) -> Prepared:
    """The item for one metric, whose ``interpret`` answers ``(rows, command_meta)``."""
    if metric.collector_type == "sql":
        sql = _prepare_sql_file(metric=metric, target=target, secrets=secrets)
        return Prepared(item=sql.item, interpret=lambda answer: (
            sql.interpret(answer), SqlExecution(execution_time=_optional_float(answer.get("duration_seconds")))))
    if metric.collector_type in {"cmd", "docker"}:
        prepare = _prepare_command_file if metric.collector_type == "cmd" else _prepare_docker_metric
        command = prepare(metric=metric, target=target, secrets=secrets)

        def interpret(answer: dict[str, Any]) -> tuple[list[dict[str, Any]], CommandExecution]:
            meta = command.interpret(answer)
            return meta.rows, meta

        return Prepared(item=command.item, interpret=interpret)
    raise UnsupportedCollectorType(f"Unsupported collector_type: {metric.collector_type}")


def _results(
    metric: MetricDefinition,
    target: MetricTarget,
    importance: int,
    collected_at: str,
    produce: Any,
) -> list[MetricResult]:
    """Grade what one metric produced - its rows, or the exception its collection raised."""
    try:
        rows, command_meta = produce()
        if not rows:
            if metric.empty_result_is_ok:
                return [_metric_result(metric, target, importance, collected_at, status="OK", message="SQL returned no rows.")]
            return [_metric_result(metric, target, importance, collected_at, status="NO_DATA", message="Collector returned no rows.")]
        rows = _apply_postgresql_collection_policy(metric=metric, target=target, rows=rows)
        _validate_normalized_rows(rows, context=metric.metric_code)
        return [
            _row_to_result(
                row,
                metric,
                target,
                importance,
                collected_at,
                command_meta=command_meta,
            )
            for row in rows
        ]
    except Exception as exc:  # noqa: BLE001 - one metric failure must not stop later metrics.
        kwargs = {}
        if isinstance(exc, MetricCommandError):
            kwargs = {
                "raw_stdout": exc.raw_stdout,
                "raw_stderr": exc.raw_stderr,
                "exit_code": exc.exit_code,
                "execution_time": exc.execution_time,
            }
        # A metric that failed outright must go through the same severity overrides as one that
        # returned rows. Only _row_to_result applied them before, so a target whose metrics all
        # fail — a mounted Data Guard standby refusing every connect with ORA-01033 — kept
        # alerting at full severity no matter what its severity_map said: the map could only
        # reach results that had already succeeded.
        status, message = _apply_metric_result_overrides(
            metric=metric,
            target=target,
            metric_item=None,
            metric_value=None,
            status=_exception_status(exc, metric),
            message=str(exc),
        )
        return [_metric_result(metric, target, importance, collected_at, status=status, message=message,
                               collector_failed=True, **kwargs)]


class MetricCommandError(errors.OperationFailed):
    def __init__(
        self,
        message: str,
        *,
        raw_stdout: str = "",
        raw_stderr: str = "",
        exit_code: int | None = None,
        execution_time: float | None = None,
        failure_phase: str = PHASE_EXECUTE,
    ) -> None:
        super().__init__(message)
        self.raw_stdout = as_text(raw_stdout)
        self.raw_stderr = as_text(raw_stderr)
        self.exit_code = int(exit_code) if exit_code is not None else None
        self.execution_time = execution_time
        # Which half of the attempt broke, for connection_error_severity vs
        # execution_error_severity. PHASE_EXECUTE by default because that is what a raise from
        # here usually means: the script ran and its output was wrong (non-zero exit, non-JSON
        # stdout). Only the transport wrapper below knows about a failed session and says so.
        self.failure_phase = failure_phase


def _run_sql_file(*, metric: MetricDefinition, target: MetricTarget,
                  secrets: dict[str, str]) -> tuple[list[dict[str, Any]], SqlExecution]:
    """One SQL metric on its own: its rows, and how long it took (as a batch item reports it)."""
    return batch.run_one(target, secrets, _prepare(metric, target, secrets))


def _prepare_sql_file(*, metric: MetricDefinition, target: MetricTarget, secrets: dict[str, str]) -> Prepared:
    path = _resolve_metric_file_path(metric, target)
    if path is None:
        raise errors.InvalidConfig(f"Metric SQL path is not resolved: {metric.metric_code}")
    sql_text = Path(path).read_text(encoding="utf-8-sig")
    # The numbers configuration states for this metric, written into the DECLAREs that carry
    # their defaults. A setting the file does not declare raises - and is recorded on this
    # metric for this target, like any other failure to prepare it.
    sql_text = apply_sql_settings(sql_text, _sql_settings_for(metric, path))
    sql = prepare_sql(
        target=target,
        sql_text=sql_text,
        secrets=secrets,
        sql_timeout_seconds=metric.default_timeout,
        max_rows=metric.max_rows,
        per_database=_variant_is_per_database(metric, target),
        item_id=metric.metric_code,
    )

    # Normalize column-name case to the lowercase result contract. Oracle folds unquoted
    # aliases (AS metric_value) to UPPERCASE, so direct oracledb and the api bridge both
    # return METRIC_VALUE etc.; SQL Server already preserves the lowercase aliases (no-op).
    def interpret(answer: dict[str, Any]) -> list[dict[str, Any]]:
        return [{str(key).lower(): value for key, value in row.items()} for row in sql.interpret(answer)]

    return Prepared(item=sql.item, interpret=interpret)


def _apply_postgresql_collection_policy(
    *, metric: MetricDefinition, target: MetricTarget, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply role-aware expectations that SQL cannot safely infer from the cluster."""
    if target.db_type not in {"postgresql", "postgres"} or metric.metric_code != "POSTGRES_REPLICATION":
        return rows
    expected = int((target.metrics_config or {}).get("expected_replica_count", 0))
    primary_rows = [row for row in rows if "role=primary" in str(row.get("message") or "")]
    no_replica_rows = [row for row in rows if str(row.get("metric_item") or "") == "replication"]
    connected = len(primary_rows)
    if no_replica_rows and expected == 0:
        for row in no_replica_rows:
            row.update(
                metric_value="0",
                metric_unit="replicas",
                status="OK",
                message="role=primary, connected_replicas=0, expected_replicas=0; standalone primary is healthy.",
            )
    elif primary_rows and connected < expected:
        for row in primary_rows:
            row["status"] = "CRITICAL"
            row["message"] = f"{row.get('message')}; connected_replicas={connected}, expected_replicas={expected}"
    elif no_replica_rows and expected > 0:
        for row in no_replica_rows:
            row["status"] = "CRITICAL"
            row["message"] = f"role=primary, connected_replicas=0, expected_replicas={expected}"
    return rows


def _run_docker_metric(*, metric: MetricDefinition, target: MetricTarget, secrets: dict[str, str]) -> CommandExecution:
    return batch.run_one(target, secrets, _prepare_docker_metric(metric=metric, target=target, secrets=secrets))


def _prepare_docker_metric(*, metric: MetricDefinition, target: MetricTarget, secrets: dict[str, str]) -> Prepared:
    """Collect one container's stats/state by running the metric's docker script with the
    target's container name in ``DOCKER_CONTAINER``.

    Transport mirrors the cmd collector — ``docker inspect``/``docker stats`` is just a shell
    command, so it runs wherever the docker daemon lives. When the target declares an SSH
    ``cmd_access`` (a remote docker host, e.g. a cloud VM whose containers the worker cannot see
    on its own socket) the inspect script is shipped over SSH and run against that host's daemon;
    otherwise it runs locally against the worker's mounted ``/var/run/docker.sock`` (the default,
    and the pre-existing behavior for containers that live on the worker's own docker host).

    ``collector_type`` stays ``docker`` (not ``cmd``) on purpose: it is the on/off + scope switch
    that lets a target collect container stats while its OS/``cmd`` metrics stay disabled. Same
    script + JSON contract as the cmd/OS collectors, so the logic is a visible/editable asset."""
    container = str(target.container_name or "").strip()
    cmd_access = target.cmd_access or {}
    method = str(cmd_access.get("method") or "").strip().lower()
    if not container and not _runs_on_host(metric, target):
        raise errors.NotConfigured(f"No container_name configured for docker metric {metric.metric_code} on {target.target_id}.")
    path = _resolve_metric_file_path(metric, target)
    if path is None:
        raise errors.InvalidConfig(f"Docker metric script is not resolved: {metric.metric_code}")
    collector_env = _collector_env(metric=metric, target=target, secrets=secrets)
    if not container:
        # The host-install half of a ``host_fallback`` metric: the same script, no DOCKER_CONTAINER,
        # run on the host the target's cmd_access logs into - never on the worker, whose files are
        # not the target's.
        return _prepare_remote(Path(path), target=target, timeout_seconds=metric.default_timeout,
                               collector_env=collector_env, shell=_remote_shell_for(Path(path), target),
                               item_id=metric.metric_code)
    collector_env["DOCKER_CONTAINER"] = container
    if bool(cmd_access.get("enabled", False)) and method == "ssh":
        return _prepare_remote(Path(path), target=target, timeout_seconds=metric.default_timeout,
                               collector_env=collector_env, shell=_remote_shell_for(Path(path), target),
                               item_id=metric.metric_code)
    return _prepare_local(Path(path), timeout_seconds=metric.default_timeout,
                          collector_env=collector_env, item_id=metric.metric_code)


def _run_command_file(*, metric: MetricDefinition, target: MetricTarget, secrets: dict[str, str]) -> CommandExecution:
    return batch.run_one(target, secrets, _prepare_command_file(metric=metric, target=target, secrets=secrets))


def _prepare_command_file(*, metric: MetricDefinition, target: MetricTarget, secrets: dict[str, str]) -> Prepared:
    path = _resolve_metric_file_path(metric, target)
    if path is None:
        raise errors.InvalidConfig(f"Metric command path is not resolved: {metric.metric_code}")
    cmd_access = target.cmd_access or {}
    if not bool(cmd_access.get("enabled", False)):
        raise errors.NotConfigured(f"Command access is not enabled for target: {target.target_id}")
    # A config error recorded at load time (bad method, missing credential): report it here so
    # it lands as this target's failing metric rather than having aborted the whole scan.
    if cmd_access.get("error"):
        raise errors.InvalidConfig(str(cmd_access["error"]))
    method = str(cmd_access.get("method") or "").strip().lower()
    collector_env = _collector_env(metric=metric, target=target, secrets=secrets)
    if method == "local":
        # `local` runs the script inside the collector (the worker container). Pointed at another
        # machine it does not fail — it succeeds and files THIS host's numbers under that host's
        # name, so the target looks monitored while none of its OS data is its own. `common`
        # refuses it: a config error someone can fix beats data nobody can trust.
        return _prepare_local(Path(path), timeout_seconds=metric.default_timeout,
                              collector_env=collector_env, item_id=metric.metric_code,
                              local_host=str(cmd_access.get("host") or ""))
    if method == "ssh":
        return _prepare_remote(Path(path), target=target, timeout_seconds=metric.default_timeout,
                               collector_env=collector_env, shell=_remote_shell_for(Path(path), target),
                               item_id=metric.metric_code)
    if method == "winrm":
        return _prepare_remote(Path(path), target=target, timeout_seconds=metric.default_timeout,
                               collector_env=collector_env, shell=SHELL_POWERSHELL,
                               item_id=metric.metric_code)
    raise UnsupportedExecutionMethod(f"Unsupported cmd_access.method: {method or '<missing>'}")


def _collector_env(
    *, metric: MetricDefinition, target: MetricTarget, secrets: dict[str, str] | None = None
) -> dict[str, str]:
    """Per-target inputs for a cmd collector, as environment variables the script reads.

    Declared in db_instances.json, target-wide and/or per metric (the per-metric block wins)::

        "metrics": {
          "collector_env": {"OS_SERVICE_NAMES": "W32Time"},
          "env_secrets": {"BACKUP_ENCRYPTION_PASSWORD": "TOKEN_203_0_113_188_BACKUP_ENC"},
          "metric_overrides": {
            "OS_SERVICE_STATUS": {"collector_env": {"OS_SERVICE_NAMES": "AOS60$01,W32Time"}}
          }
        }

    ``collector_env`` values come from the target config only and may never name a secret —
    the config is committed, so a passphrase written there would live in git forever.

    ``env_secrets`` is the way a script that genuinely needs one gets it: it maps an env var to
    a **ref** in the encrypted store, and only the ref is ever written to config. Same shape and
    same rule as ``restore_config.json``'s ``env_secrets`` (:mod:`dbabrain.backup_restore.backup`),
    so one concept covers both apps. It exists because ORACLE_RESTORE_VALIDATION could not do its
    job without it: the backups are RMAN password-encrypted, so ``restore ... validate`` without
    the passphrase fails with ORA-19913 on every run — the metric reported CRITICAL for weeks
    while the backups were in fact fine, which is the worst thing a monitor can do.
    """
    metrics_cfg = target.metrics_config if isinstance(target.metrics_config, dict) else {}
    override_cfg = _metric_override_config(target, metric.metric_code)
    # The address the inventory already reaches this host on, so a script never has to restate
    # it. OS_TCP_PORT_STATUS needs it: probing 127.0.0.1 cannot tell "nothing is listening" from
    # "listening, but bound to the NIC address clients actually use" - and on an app server the
    # second is the case that matters. Config first in the dict, so a target that means something
    # else by it can still override.
    resolved: dict[str, str] = {"DBABRAIN_TARGET_HOST": _target_host(target)}
    # The database's identity, for the collectors that declare they read it (``target_env``).
    # Empty values are left out, so a script tells "not set" from "set to nothing" by presence.
    if metric.target_env:
        identity = _target_identity(target)
        for name in metric.target_env:
            if identity.get(name):
                resolved[name] = identity[name]
    for source in (metrics_cfg.get("collector_env"), override_cfg.get("collector_env")):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            name = str(key).strip()
            if not name or value in (None, ""):
                continue
            if sensitive_env_name(name):
                raise errors.InvalidConfig(
                    f"collector_env must not carry secrets: {name} ({target.target_id}). "
                    "Use env_secrets, which takes a ref into the encrypted store instead of a value."
                )
            resolved[name] = str(value)
    for source in (metrics_cfg.get("env_secrets"), override_cfg.get("env_secrets")):
        if not isinstance(source, dict):
            continue
        for key, ref in source.items():
            name, ref_name = str(key).strip(), str(ref or "").strip()
            if not name or not ref_name:
                continue
            value = str((secrets or {}).get(ref_name) or "")
            if not value:
                # Fail loudly rather than run the script without it: a validation that silently
                # skips decryption is exactly the false CRITICAL this feature exists to end.
                raise errors.NotConfigured(
                    f"{metric.metric_code} on {target.target_id}: env_secrets maps {name} to secret "
                    f"ref '{ref_name}', which is not in the secret store. Add it, or pass "
                    "--key/--key-base64 so the store can be read."
                )
            resolved[name] = value
    return resolved


def _target_identity(target: MetricTarget) -> dict[str, str]:
    """Every name :data:`definitions.TARGET_ENV_NAMES` allows, for this target."""
    return {
        "DBABRAIN_DB_TYPE": str(target.db_type or "").strip().lower(),
        "DBABRAIN_DB_PORT": str(target.port or "").strip(),
        "DBABRAIN_DB_CONTAINER": str(target.container_name or "").strip(),
    }


def _target_host(target: MetricTarget) -> str:
    """The address a script should probe this host on.

    ``cmd_access.host`` wins over ``ip`` because it is the one already proven reachable — it is
    what the collector itself connected through to run the script. Empty rather than a guess when
    neither is set: a script that falls back to loopback on its own is honest about it, whereas an
    invented address would produce a confident CLOSED for a host nobody ever probed.
    """
    cmd_access = target.cmd_access if isinstance(target.cmd_access, dict) else {}
    return str(cmd_access.get("host") or target.ip or "").strip()


def sensitive_env_name(name: str) -> bool:
    lowered = name.strip().lower()
    return any(word in lowered for word in ("password", "secret", "token", "key", "credential"))


#: The shells ``common.remote_exec`` knows, by the names it knows them.


def execute_local(path: Path, *, timeout_seconds: int, collector_env: dict[str, str] | None = None) -> CommandExecution:
    """A metric script run on this machine (in the worker container), on its own."""
    return batch.run_one(None, {}, _prepare_local(Path(path), timeout_seconds=timeout_seconds,
                                                  collector_env=collector_env))


def _prepare_local(
    path: Path, *, timeout_seconds: int, collector_env: dict[str, str] | None,
    item_id: str = "", local_host: str | None = None,
) -> Prepared:
    item = {"id": item_id, "kind": "local", "path": str(path), "argv": _command_args(path),
            "env": dict(collector_env or {}), "timeout_seconds": int(timeout_seconds),
            "require_local_host": local_host is not None, "host": local_host or ""}
    return Prepared(item=item, interpret=_command_execution)


def _command_args(path: Path) -> list[str]:
    """The command line that runs a local metric script - this app's decision, sent in the item."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(path)]
    if suffix in {".bat", ".cmd"}:
        return ["cmd.exe", "/c", str(path)]
    if suffix == ".ps1":
        return [powershell_executable(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
    if suffix == ".sh":
        # Always invoke via bash rather than executing the file directly: a bind-mounted
        # script (e.g. the docker collector on the Linux worker) may not carry the +x bit,
        # which would fail with "Permission denied" if run as ./script.sh.
        return ["bash", str(path)]
    return [str(path)]


def _cmd_access_json(target: MetricTarget) -> dict[str, Any]:
    """The target's ``cmd_access`` object, with the target IP as the default host.

    This *is* the JSON input ``common.remote_exec`` expects — the collector does no translation,
    it fills in the host that db_instances.json leaves implicit, and resolves a key named by its
    bare file name to the file, so ``common`` is not left to look it up (rules R09)."""
    access = dict(target.cmd_access or {})
    access.setdefault("host", target.ip)
    key_file = str(access.get("key_file") or "").strip()
    if key_file and not Path(key_file).is_absolute():
        try:
            access["key_file"] = data_sources.resolve_ssh_key(key_file)
        except Exception:  # noqa: BLE001 - left as named: `common` refuses it in the same words.
            pass
    return access


def _remote_shell_for(path: Path, target: MetricTarget) -> str:
    """Which shell runs this metric script: the file's own extension wins over the
    target's declared shell, so a ``.ps1`` reaches a Windows host as PowerShell even when
    ``cmd_access.shell`` was left unset."""
    if path.suffix.lower() == ".ps1":
        return SHELL_POWERSHELL
    declared = str((target.cmd_access or {}).get("shell") or "").strip().lower()
    return declared or SHELL_BASH


def _prepare_remote(
    path: Path, *, target: MetricTarget, timeout_seconds: int,
    collector_env: dict[str, str] | None, shell: str, item_id: str = "",
) -> Prepared:
    """A metric script shipped to the target, its environment prepended there by ``common``.

    Transport, auth and the env prelude live in ``common``; what stays here is the metrics
    contract on top of it — a non-zero exit or non-JSON stdout is a *metric* failure, and every
    transport error comes back as :class:`MetricCommandError` carrying the raw streams the metric
    row records."""
    item = {
        "id": item_id, "kind": "script", "access": _cmd_access_json(target),
        "credential": target.cmd_credential or {},
        # utf-8-sig: PowerShell scripts authored on Windows carry a BOM.
        "script": Path(path).read_text(encoding="utf-8-sig"), "shell": shell,
        "env": dict(collector_env or {}), "timeout_seconds": int(timeout_seconds),
    }
    return Prepared(item=item, interpret=_command_execution)


def execute_ssh(
    path: Path, *, target: MetricTarget, secrets: dict[str, str], timeout_seconds: int,
    collector_env: dict[str, str] | None = None,
) -> CommandExecution:
    """Run a metric script on the target over SSH (Linux, or Windows with OpenSSH Server)."""
    return batch.run_one(target, secrets, _prepare_remote(
        Path(path), target=target, timeout_seconds=timeout_seconds, collector_env=collector_env,
        shell=_remote_shell_for(Path(path), target)))


def execute_winrm(
    path: Path, *, target: MetricTarget, secrets: dict[str, str], timeout_seconds: int,
    collector_env: dict[str, str] | None = None,
) -> CommandExecution:
    """Run a metric script on a Windows target over WinRM.

    Legacy transport — prefer ``cmd_access.method = "ssh"`` where OpenSSH Server is
    available. WinRM needs pypsrp (or a PowerShell on the collector host) and
    Negotiate/NTLM auth, which often fails across networks and domains. Kept for targets
    that cannot run OpenSSH Server."""
    return batch.run_one(target, secrets, _prepare_remote(
        Path(path), target=target, timeout_seconds=timeout_seconds, collector_env=collector_env,
        shell=SHELL_POWERSHELL))


def _command_execution(answer: dict[str, Any]) -> CommandExecution:
    """A script's answer, held to the metrics contract: exit 0, and a JSON array of objects."""
    error = answer.get("error")
    if error:
        if error.get("kind") in {"timeout", "remote"}:
            raise MetricCommandError(
                str(error.get("message") or ""),
                raw_stdout=error.get("stdout") or "",
                raw_stderr=error.get("stderr") or "",
                execution_time=error.get("duration_seconds"),
                failure_phase=str(error.get("failure_phase") or PHASE_EXECUTE),
            )
        batch.raise_other(error)
    raw_stdout = as_text(answer.get("stdout"))
    raw_stderr = as_text(answer.get("stderr"))
    exit_code = int(answer.get("exit_code") or 0)
    execution_time = answer.get("duration_seconds")
    if exit_code != 0:
        raise MetricCommandError(
            f"Command exited with code {exit_code}.",
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=exit_code,
            execution_time=execution_time,
        )
    return CommandExecution(
        rows=_parse_command_stdout(raw_stdout, raw_stderr, exit_code, execution_time),
        raw_stdout=raw_stdout,
        raw_stderr=raw_stderr,
        exit_code=exit_code,
        execution_time=execution_time,
    )


def _parse_command_stdout(raw_stdout: str, raw_stderr: str, exit_code: int, execution_time: float) -> list[dict[str, Any]]:
    raw_stdout = as_text(raw_stdout)
    raw_stderr = as_text(raw_stderr)
    try:
        parsed = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise MetricCommandError(
            f"Command stdout is not valid JSON: {exc.msg}.",
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=exit_code,
            execution_time=execution_time,
        ) from exc
    if not isinstance(parsed, list):
        raise MetricCommandError(
            "Command stdout must be a JSON array.",
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=exit_code,
            execution_time=execution_time,
        )
    rows = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            raise MetricCommandError(
                f"Command JSON row {index} must be an object.",
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
                exit_code=exit_code,
                execution_time=execution_time,
            )
        rows.append(item)
    return rows


def _row_to_result(
    row: dict[str, Any],
    metric: MetricDefinition,
    target: MetricTarget,
    importance: int,
    collected_at: str,
    *,
    command_meta: CommandExecution | SqlExecution | None = None,
) -> MetricResult:
    metric_item = _optional_text(row.get("metric_item"))
    metric_value = _optional_text(row.get("metric_value"))
    metric_unit = _optional_text(row.get("metric_unit"))
    status = str(row.get("status") or "OK").upper()
    message = _optional_text(row.get("message"))
    status, message = _apply_threshold_override(
        metric=metric, target=target, metric_value=metric_value, status=status, message=message
    )
    status, message = _apply_metric_result_overrides(
        metric=metric,
        target=target,
        metric_item=metric_item,
        metric_value=metric_value,
        status=status,
        message=message,
    )
    status, message = _apply_data_validity_policy(metric=metric, metric_item=metric_item, metric_value=metric_value, status=status, message=message)
    return _metric_result(
        metric,
        target,
        importance,
        collected_at,
        metric_item=metric_item,
        metric_value=metric_value,
        metric_unit=metric_unit,
        status=status,
        message=message,
        raw_stdout=command_meta.raw_stdout if command_meta else None,
        raw_stderr=command_meta.raw_stderr if command_meta else None,
        exit_code=command_meta.exit_code if command_meta else None,
        execution_time=command_meta.execution_time if command_meta else None,
    )


def _metric_result(
    metric: MetricDefinition,
    target: MetricTarget,
    importance: int,
    collected_at: str,
    *,
    metric_item: str | None = None,
    metric_value: str | None = None,
    metric_unit: str | None = None,
    status: str,
    message: str | None = None,
    raw_stdout: str | None = None,
    raw_stderr: str | None = None,
    exit_code: int | None = None,
    execution_time: float | None = None,
    collector_failed: bool = False,
) -> MetricResult:
    # `error_type` says whether the MONITORING failed or the TARGET did, and only the caller
    # knows which: it is a collector failure exactly when the collector raised.
    #
    # It used to be guessed by keyword-matching the message of any non-OK row, so a *successful*
    # collection whose finding happened to contain the wrong word was filed as a broken collector.
    # Real examples on this estate: 200 LOG_RECENT_CRITICAL rows became CONNECT_FAILED because the
    # metric's own SQL comment says "timeout", and 27 INSTANCE_STATUS rows became AUTH_FAILED
    # because the finding they were reporting was failed logins on the target.
    #
    # That is not cosmetic. `COLLECTOR_FAILURE_ERROR_TYPES` drives
    # `MetricStore.fetch_metric_freshness`, so those rows made the Metric coverage panel report
    # healthy metrics as failing — the monitoring lying about itself, which is the one thing it
    # may never do.
    is_finding = str(status or "").upper() in {"ERROR", "WARNING", "WARN", "NO_DATA", "CRITICAL"}
    if not is_finding:
        error_type = None
    elif collector_failed:
        error_type = normalize_error_type(message)
    else:
        # The collector connected, ran, and did not like the answer: a finding about the target.
        error_type = "CHECK_FAILED"
    signature = normalize_error_signature(message) if error_type else None
    return MetricResult(
        target_id=target.target_id,
        server_id=target.server_id,
        ip=target.ip,
        db_type=target.db_type,
        db_name=target.db_name,
        metric_code=metric.metric_code,
        metric_item=metric_item,
        metric_value=metric_value,
        metric_unit=metric_unit,
        status=status,
        importance=importance,
        message=message,
        collected_at=collected_at,
        raw_stdout=raw_stdout,
        raw_stderr=raw_stderr,
        exit_code=exit_code,
        execution_time=execution_time,
        collector_type=metric.collector_type,
        category=metric.category,
        error_type=error_type,
        normalized_error_signature=signature,
    )


def _validate_normalized_rows(rows: list[dict[str, Any]], *, context: str) -> None:
    for index, row in enumerate(rows, start=1):
        missing = [field for field in NORMALIZED_RESULT_FIELDS if field not in row]
        if missing:
            raise errors.OperationFailed(f"{context} row {index} missing required field(s): {', '.join(missing)}.")
        status = str(row.get("status") or "").strip().upper()
        if status not in SUPPORTED_RESULT_STATUSES:
            raise errors.OperationFailed(f"{context} row {index} has unsupported status: {status or '<missing>'}.")


# Progress lines are the only record of a pass in the daemon log, and several server workers write
# them at once. Without this, two lines interleave mid-token and the log stops being greppable.
_PRINT_LOCK = threading.Lock()


def _print_line(text: str) -> None:
    with _PRINT_LOCK:
        print(text, flush=True)


def _print_metric_progress(
    *,
    run_id: int,
    target: MetricTarget,
    metric: MetricDefinition,
    inserted: int,
    results: list[MetricResult],
) -> None:
    status = _worst_status(results)
    _print_line(
        "METRIC "
        f"run_id={run_id} "
        f"target={target.target_id} "
        f"metric={metric.metric_code} "
        f"status={status} "
        f"rows={len(results)} "
        f"inserted={inserted}"
    )


def _print_test_rows(*, target: MetricTarget, metric: MetricDefinition, results: list[MetricResult]) -> None:
    """A test run's answer: every row, as the metric graded it - what the store would have kept."""
    lines = [f"TEST target={target.target_id} metric={metric.metric_code} "
             f"status={_worst_status(results)} rows={len(results)}"]
    for item in results:
        detail = " ".join(str(item.message or "").split())
        if len(detail) > 400:
            detail = detail[:400] + " ..."
        lines.append(f"  {item.status} item={item.metric_item or ''} value={item.metric_value or ''}"
                     f"{' ' + item.metric_unit if item.metric_unit else ''}"
                     f"{' error_type=' + item.error_type if item.error_type else ''} | {detail}")
    _print_line("\n".join(lines))


def _print_metric_skip(*, target: MetricTarget, metric: MetricDefinition, reason: str, dry_run: bool) -> None:
    prefix = "DRY-RUN " if dry_run else ""
    _print_line(
        f"{prefix}METRIC_SKIP "
        f"target={target.target_id} "
        f"metric={metric.metric_code} "
        f"reason={reason}"
    )


def _worst_status(results: list[MetricResult]) -> str:
    priority = {
        "CRITICAL": 4,
        "ERROR": 3,
        "WARNING": 3,
        "WARN": 3,
        "UNKNOWN": 3,
        "NO_DATA": 2,
        "OK": 1,
        "LOGGING": 0,
    }
    if not results:
        return "NO_DATA"
    return max((item.status.upper() for item in results), key=lambda value: priority.get(value, 0))


def _exception_status(exc: Exception, metric: MetricDefinition) -> str:
    """The severity of a metric that failed to collect — the metric's own answer, per phase.

    Every collection failure used to be a flat WARNING, on the theory that CRITICAL belonged to
    threshold breaches. That reasoning does not survive contact with an availability metric: when
    INSTANCE_STATUS cannot connect, "the instance did not answer" *is* the finding, and reporting
    the estate's most serious event one level below a full transaction log was the single loudest
    way this monitor could understate an outage. Meanwhile a capacity query failing on one host
    genuinely is a WARNING, so the answer cannot be global — it belongs to each metric, in
    data/metric_definitions.json, as connection_error_severity / execution_error_severity.

    The per-target ``severity_map`` still runs after this (see :func:`_collect_one_metric`), so an
    instance that is *expected* to refuse connects — a mounted Data Guard standby — can still be
    quieted without weakening the metric for the rest of the estate.
    """
    if resolve_failure_phase(exc) == PHASE_CONNECT:
        return metric.connection_error_severity
    return metric.execution_error_severity


def _apply_data_validity_policy(
    *,
    metric: MetricDefinition,
    metric_item: str | None,
    metric_value: str | None,
    status: str,
    message: str | None,
) -> tuple[str, str | None]:
    if metric.metric_code != "SYSTEM_CPU_MEMORY":
        return status, message
    item = str(metric_item or "").lower()
    if "memory" not in item:
        return status, message
    value = _optional_float(metric_value)
    if value is not None and value > 100:
        detail = f"METRIC_DATA_INVALID: physical memory percent is {value:g}, expected <= 100."
        return "WARNING", _append_policy_note(message, detail) if message else detail
    return status, message


SUPPORTED_COLLECTOR_TYPES_FOR_TARGET = ("sql", "cmd", "docker", "k8s")


def _runs_on_host(metric: MetricDefinition, target: MetricTarget) -> bool:
    """A ``host_fallback`` docker metric on a target with no container and an SSH way onto its host."""
    if metric.collector_type != "docker" or not getattr(metric, "host_fallback", False):
        return False
    if str(getattr(target, "container_name", "") or "").strip():
        return False
    cmd_access = target.cmd_access or {}
    return (bool(cmd_access.get("enabled", False))
            and str(cmd_access.get("method") or "").strip().lower() == "ssh")


def _metric_disabled_by_collector_type(metric: MetricDefinition, target: MetricTarget) -> bool:
    """Turn off a whole class of collector on one target.

    Declared in db_instances.json next to the rest of the metric switches::

        "metrics": { "enabled": true, "disabled_collector_types": ["cmd"] }

    Listing every OS metric code in ``report_policy.disabled_metric_codes`` was the only way to
    say "this target has no OS to look at" — which meant every OS metric added later started
    producing noise until someone remembered to extend that list (it happened: the five new OS
    metrics made 61 warning rows a day on the PostgreSQL lab targets). The switch belongs on the
    thing they have in common, which is the collector type.

    An unknown value is an error, not a silent no-op: a typo like ["command"] would quietly
    collect everything while the operator believed it was off.
    """
    metrics_cfg = target.metrics_config if isinstance(target.metrics_config, dict) else {}
    disabled = metrics_cfg.get("disabled_collector_types") or []
    if not isinstance(disabled, list):
        raise errors.InvalidConfig(
            f"metrics.disabled_collector_types must be a list for {target.target_id}, "
            f"e.g. [\"cmd\"]."
        )
    normalized = {str(item or "").strip().lower() for item in disabled}
    unknown = normalized - set(SUPPORTED_COLLECTOR_TYPES_FOR_TARGET)
    if unknown:
        raise errors.InvalidConfig(
            f"metrics.disabled_collector_types has unknown value(s) {sorted(unknown)} for "
            f"{target.target_id}; supported: {list(SUPPORTED_COLLECTOR_TYPES_FOR_TARGET)}."
        )
    # A docker metric reading a host install logs into the host: the switch that turns host access
    # off on this target turns it off too.
    effective = "cmd" if _runs_on_host(metric, target) else str(metric.collector_type or "").strip().lower()
    return effective in normalized


def _metric_enabled_for_target(metric: MetricDefinition, target: MetricTarget) -> bool:
    if _metric_disabled_by_collector_type(metric, target):
        return False
    if _metric_disabled_by_report_policy(metric.metric_code, target):
        return False
    metric_cfg = _metric_override_config(target, metric.metric_code)
    return bool(metric_cfg.get("enabled", True))


def _metric_disabled_by_report_policy(metric_code: str, target: MetricTarget) -> bool:
    report_policy = target.report_policy if isinstance(target.report_policy, dict) else {}
    disabled_metric_codes = report_policy.get("disabled_metric_codes") or []
    if not isinstance(disabled_metric_codes, list):
        return False
    normalized_metric_code = str(metric_code or "").strip().upper()
    return normalized_metric_code in {str(item or "").strip().upper() for item in disabled_metric_codes}


def _apply_metric_result_overrides(
    *,
    metric: MetricDefinition,
    target: MetricTarget,
    metric_item: str | None,
    metric_value: str | None,
    status: str,
    message: str | None,
) -> tuple[str, str | None]:
    """Remap a result's severity per the target's ``metric_overrides`` before it is written to
    SQLite. The override is a pure status->status map (``severity_map``); it never recomputes the
    metric from thresholds. Item-level maps win over the metric-level map, which wins over the
    target-level map, key-by-key. The report app reads SQLite as-is, so the metrics app is the
    single place severity is adjusted."""
    metric_cfg = _metric_override_config(target, metric.metric_code)
    severity_map = _resolve_severity_map(metric_cfg, metric_item, target=target)
    if not severity_map:
        return status, message
    current = _normalize_override_status(status)
    mapped = severity_map.get(current)
    if not mapped or mapped == current:
        return status, message
    return mapped, _append_policy_note(message, f"severity remapped {current}->{mapped} by metric_overrides")


def _metric_override_config(target: MetricTarget, metric_code: str) -> dict[str, Any]:
    metrics_cfg = target.metrics_config if isinstance(target.metrics_config, dict) else {}
    metric_overrides = metrics_cfg.get("metric_overrides") or {}
    if not isinstance(metric_overrides, dict):
        return {}
    matched = metric_overrides.get(metric_code)
    if matched is None:
        for key, value in metric_overrides.items():
            if str(key).upper() == metric_code.upper():
                matched = value
                break
    return matched if isinstance(matched, dict) else {}


#: Verdicts a threshold override never overrules. The thresholds grade a *number*; these are what the
#: metric decided without one - the check failed, there was nothing to read, the database is offline
#: and reports `0`. Re-graded by value, each became OK the moment any override with thresholds
#: existed for that metric on the target (review 0.25.0, F3.2). Lowering one stays possible, said
#: outright: `severity_map` runs after this.
_VERDICTS_THRESHOLDS_DO_NOT_OVERRULE = frozenset({"CRITICAL", "ERROR", "NO_DATA"})


def _apply_threshold_override(
    *, metric: MetricDefinition, target: MetricTarget, metric_value: str | None,
    status: str, message: str | None
) -> tuple[str, str | None]:
    """Evaluate optional numeric thresholds from the canonical per-target override.

    They grade a row the metric itself left at OK, LOGGING or WARNING - raising or lowering it by
    the number - and leave a CRITICAL, ERROR or NO_DATA verdict as the metric gave it.
    """
    config = _metric_override_config(target, metric.metric_code)
    if "warning_threshold" not in config and "critical_threshold" not in config:
        return status, message
    if str(status or "").upper() in _VERDICTS_THRESHOLDS_DO_NOT_OVERRULE:
        return status, message
    value = _optional_float(metric_value)
    if value is None:
        return status, message
    warning = _optional_float(config.get("warning_threshold"))
    critical = _optional_float(config.get("critical_threshold"))
    direction = str(config.get("higher_is_worse", True)).lower() not in {"false", "0", "no"}
    if warning is not None and critical is not None:
        if direction and warning > critical:
            raise ValueError(f"{metric.metric_code}: warning_threshold must be <= critical_threshold")
        if not direction and warning < critical:
            raise ValueError(f"{metric.metric_code}: warning_threshold must be >= critical_threshold")
    critical_hit = critical is not None and (value >= critical if direction else value <= critical)
    warning_hit = warning is not None and (value >= warning if direction else value <= warning)
    resolved = "CRITICAL" if critical_hit else "WARNING" if warning_hit else "OK"
    return resolved, _append_policy_note(message, f"threshold override evaluated value={value:g}")


def _metric_item_override_config(metric_cfg: dict[str, Any], metric_item: str | None) -> dict[str, Any]:
    item_overrides = metric_cfg.get("metric_item_overrides") or {}
    if not isinstance(item_overrides, dict) or not metric_item:
        return {}
    matched = item_overrides.get(metric_item)
    if matched is None:
        for key, value in item_overrides.items():
            if str(key).lower() == metric_item.lower():
                matched = value
                break
    return matched if isinstance(matched, dict) else {}


# Statuses a severity_map remap may use as a source or destination.
_OVERRIDE_STATUSES = {"OK", "LOGGING", "WARNING", "CRITICAL", "ERROR", "NO_DATA"}


def _resolve_severity_map(
    metric_cfg: dict[str, Any], metric_item: str | None, *, target: MetricTarget
) -> dict[str, str]:
    """Merge the target-level ``metrics.severity_map`` with the metric-level map and the matching
    item-level map; the more specific map wins key-by-key, so one metric (or one item) can be tuned
    without restating the others.

    The target-level map exists for a target where *every* metric of a kind is expected to fail:
    a mounted Data Guard physical standby refuses each connect with ORA-01033, so all 39 Oracle SQL
    metrics report the same technical error. Restating the map under all 39 codes would be the same
    trap ``disabled_collector_types`` was added to avoid - every metric added later starts alerting
    again until someone remembers to extend the list.
    """
    metrics_cfg = target.metrics_config if isinstance(target.metrics_config, dict) else {}
    merged = _coerce_severity_map(metrics_cfg.get("severity_map"))
    merged.update(_coerce_severity_map(metric_cfg.get("severity_map")))
    item_cfg = _metric_item_override_config(metric_cfg, metric_item)
    merged.update(_coerce_severity_map(item_cfg.get("severity_map")))
    return merged


def _coerce_severity_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        src = _normalize_override_status(str(key))
        dst = _normalize_override_status(str(value))
        if src in _OVERRIDE_STATUSES and dst in _OVERRIDE_STATUSES:
            out[src] = dst
    return out


def _normalize_override_status(value: str) -> str:
    status = str(value or "").strip().upper()
    return "WARNING" if status == "WARN" else status


def _append_policy_note(message: str | None, note: str) -> str:
    if not message:
        return note.capitalize() + "."
    return f"{message} Policy: {note}."


def _metric_window_open(*, metric: MetricDefinition, now: datetime) -> bool:
    """Whether a metric is allowed to run *at this hour at all*.

    Deliberately separate from :func:`_metric_due`, which answers "has enough time elapsed".
    Keeping the window inside the due check made it collateral damage of ``--force``: anything
    that skipped the interval also skipped the window. Unset bounds mean always open, so
    real-time metrics are unaffected.

    Bounds are hours in the **configured** timezone (``config.json`` -> ``timezone``), not on
    the host's clock. ``now.astimezone()`` read the machine's zone, so the 22 metrics declared
    ``from_hour: 1, to_hour: 6`` - the heavy overnight ones - ran during the working day on
    any node whose OS clock was not set to the operator's zone.
    """
    window = getattr(metric, "schedule_window", None)
    return window is None or is_time_window_open(window, to_display(now))


def _window_label(metric: MetricDefinition) -> str:
    window = getattr(metric, "schedule_window", None)
    if window is None:
        return "always open"
    from_hour, to_hour = getattr(window, "from_hour", None), getattr(window, "to_hour", None)
    if from_hour is None and to_hour is None:
        return "restricted"
    return f"{'-' if from_hour is None else f'{from_hour:02d}'}-{'-' if to_hour is None else f'{to_hour:02d}'}h"


def _safe_claim_name(name: str) -> str:
    """Sanitize server or category names for filesystem use (anything outside [A-Za-z0-9._-] becomes _)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name))


def _is_metric_due(
    *,
    last: str | None,
    success: str | None,
    metric: MetricDefinition,
    target: MetricTarget,
    now: datetime,
) -> bool:
    """Whether a metric is due based on its last collected time and last successful collected time.

    Like ``app_command_is_due``: a metric is due ``repeat_interval`` after a successful
    collection, but only ``retry_interval`` after a failed one. The last collection counts as
    failed when there is no successful row at all, or the newest row is newer than the last
    success (i.e. the most recent attempt produced only errors).
    """
    if last is None:
        return True  # never collected -> due now
    last_time = datetime.fromisoformat(last.replace("Z", "+00:00"))
    last_errored = success is None or last > success
    # Shared convention via job_due: repeat_interval=0 => collect once; retry after a failure.
    return job_due(
        last_run=last_time,
        last_status="error" if last_errored else "done",
        repeat_interval=_repeat_interval_for(metric, target),
        retry_interval=metric.retry_interval_seconds,
        now=now,
    )


def _metric_due(*, store: MetricStore, target: MetricTarget, metric: MetricDefinition, now: datetime) -> bool:
    """Like ``app_command_is_due``: a metric is due ``repeat_interval`` after a successful
    collection, but only ``retry_interval`` after a failed one. The last collection counts as
    failed when there is no successful row at all, or the newest row is newer than the last
    success (i.e. the most recent attempt produced only errors).

    The allowed *window* is not checked here — see :func:`_metric_window_open`.
    """
    last = store.latest_result_time(target_id=target.target_id, metric_code=metric.metric_code)
    if last is None:
        return True  # never collected -> due now
    success = store.latest_successful_result_time(target_id=target.target_id, metric_code=metric.metric_code)
    return _is_metric_due(last=last, success=success, metric=metric, target=target, now=now)


def _repeat_interval_for(metric: MetricDefinition, target: MetricTarget) -> int:
    """The metric's interval, or the one the variant that runs on this target states for itself."""
    try:
        path = _resolve_metric_file_path(metric, target)
    except errors.DbaBrainError:
        return metric.interval_seconds      # the collection itself reports why it cannot run
    for variant in metric.variants:
        if path is not None and variant.path == path and variant.repeat_interval is not None:
            return variant.repeat_interval
    return metric.interval_seconds




def _sql_settings_for(metric: MetricDefinition, path: Path | None) -> dict[str, Any]:
    """The metric's ``sql_settings`` with those of the variant that runs ``path`` written over them.

    A metric-wide setting is applied to every variant, and a variant that does not declare it fails,
    which is the point of the rule. A number that is one engine's goes on that engine's variants:
    ``LOCK_TRANSACTION_HOLDERS``'s lock-scan limits stated for the metric failed every PostgreSQL
    and Oracle collection of it the day those variants were added (1.149, 2026-10-15). The variant
    is found by path, as :func:`_variant_is_per_database` finds it.
    """
    settings = dict(metric.sql_settings)
    for variant in metric.variants:
        if path is not None and variant.path == path:
            settings.update(variant.sql_settings)
            break
    return settings


def _variant_is_per_database(metric: MetricDefinition, target: MetricTarget) -> bool:
    """Whether the variant chosen for this target asks to be run once per database.

    Resolved by path rather than re-running the selection rules: `_resolve_metric_file_path`
    already applies the db_type, platform and version filters, and a second copy of them here
    would eventually disagree with it about which variant is in force.
    """
    path = _resolve_metric_file_path(metric, target)
    if path is None:
        return False
    return any(variant.per_database for variant in metric.variants if variant.path == path)


def _resolve_metric_file_path(metric: MetricDefinition, target: MetricTarget) -> Path | None:
    """Which SQL/script file this metric runs on this target.

    The db_type/platform/version rule itself lives in :mod:`dbabrain.lib.target_profile` since
    2026-08-19 — it was the only version-aware tool selection in the tree, and being an app's
    private function is what stopped ``common.cli run-sql`` from reproducing a collector run on an
    8i or 2008 R2 instance by hand. What stays here is the *metric's* own fallback: a definition
    with a top-level ``path`` uses it when no variant fits, and that is a property of the catalog
    rather than of the target.
    """
    profile = _target_profile(target)
    if metric.collector_type == "cmd":
        if not profile.platform:
            # Names the field and where it goes: this fires on a target that has `cmd_access`
            # configured and working, so the reader's attention is on that block — and `platform`
            # is not in it. It belongs on the target itself, because it describes the machine
            # rather than the way in.
            raise errors.InvalidConfig(
                f"Target platform is required for cmd metric: {target.target_id}. "
                f"Set \"platform\": \"linux\" or \"windows\" on the db instance (not inside "
                f"cmd_access), or an \"os\" field it can be inferred from."
            )
        if profile.platform not in SUPPORTED_CMD_PLATFORMS:
            raise errors.InvalidConfig(f"Unsupported target platform for cmd metric: {profile.platform}")
        variants = candidate_variants(metric.variants, profile, match_platform=True)
    else:
        variants = candidate_variants(metric.variants, profile)
    if not variants:
        return metric.path
    chosen = select_variant(variants, profile)
    if profile.major_version is None:
        # The catalog's own file wins over an unversioned guess; only when there is none does the
        # last supported variant answer.
        return metric.path or (chosen.path if chosen is not None else None)
    return chosen.path if chosen is not None else metric.path


def _metric_supports_db_type(metric: MetricDefinition, db_type: str, *, include_unsupported: bool) -> bool:
    normalized = db_type.lower()
    if metric.variants:
        return any(
            variant.db_type in {normalized, "all", "multi", "*", ""} and (include_unsupported or variant.supported)
            for variant in metric.variants
        )
    return metric.db_type in {normalized, "all", "multi", "*"}


def _metric_unsupported_reason(metric: MetricDefinition, target: MetricTarget) -> str:
    # A cmd collector needs a way onto the host. A database in a container has none by design
    # (there is no OS to log into — the engine is the whole target), so an OS metric there is
    # not a failure, it is not applicable: skip it instead of recording an error/warning row
    # every cycle. Without this, every OS metric added later starts producing noise on those
    # targets until someone remembers to add its code to a per-target disable list.
    if metric.collector_type == "cmd" and not bool((target.cmd_access or {}).get("enabled", False)):
        return "no cmd_access is configured on this target (an OS metric needs a way onto the host)"

    # A docker metric needs a container to inspect. Non-containerized targets (bare-metal /
    # VM DBs) have none, so it is not applicable there — skip, don't error every cycle.
    if (metric.collector_type == "docker" and not str(getattr(target, "container_name", "") or "").strip()
            and not _runs_on_host(metric, target)):
        if getattr(metric, "host_fallback", False):
            return ("no container_name and no SSH cmd_access on this target (this metric reads a "
                    "container's log, or a host install's log files through the host login)")
        return "no container_name is configured on this target (a docker metric needs a container)"

    # k8s is a reserved fourth collector transport; the collector is scaffolded but not
    # implemented yet, so any k8s metric is skipped cleanly rather than erroring.
    if metric.collector_type == "k8s":
        return "k8s collector is not implemented yet (reserved collector_type)"

    variants = [
        variant
        for variant in metric.variants
        if variant.db_type in {target.db_type, "all", "multi", "*", ""}
    ]
    if metric.collector_type == "cmd" and target.platform:
        variants = [variant for variant in variants if not variant.platform or variant.platform == target.platform]
    if not variants:
        if metric.db_type in {target.db_type, "all", "multi", "*"}:
            return ""
        return f"db_type {target.db_type} is not defined for this metric"
    major_version = _target_major_version(target)
    matched_any = False
    unsupported: list[str] = []
    for variant in variants:
        if not version_matches(variant, major_version):
            continue
        matched_any = True
        if variant.supported:
            return ""
        unsupported.append(variant.unsupported_reason or f"variant {variant.name} is marked unsupported")
    if matched_any and unsupported:
        return "; ".join(unsupported)
    if major_version is None:
        return ""
    return f"no supported {target.db_type} variant for major_version={major_version}"


def _target_profile(target: MetricTarget) -> TargetProfile:
    """This target as the shared profile the selection rules read.

    Built here rather than on ``MetricTarget`` itself because the loader's job is to produce a
    target from config and this is a *derived* view of it — and because ``connection_info`` is
    where the version actually lives (``targets.py`` puts both spellings there), which nothing
    outside metrics should have to know.
    """
    return TargetProfile.from_json({
        "db_type": target.db_type,
        "major_version": _target_major_version(target),
        "platform": target.platform,
        "container_name": target.container_name,
    })


def _target_major_version(target: MetricTarget) -> int | None:
    return (
        _optional_int(target.connection_info.get(f"{target.db_type}_major_version"))
        or _optional_int(target.connection_info.get("major_version"))
    )


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any, default: str | None = None) -> str | None:
    if value is None or value == "":
        return default
    return str(value)


def _summary_message(messages: list[str]) -> str:
    if not messages:
        return ""
    return "\n".join(messages[:20])
