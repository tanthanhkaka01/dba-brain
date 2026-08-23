from __future__ import annotations
from db_ops.lib.coerce import as_optional_int as _optional_int, as_text
from db_ops.lib.text_format import format_utc as _format_utc  # noqa: F401 - one definition, see that module

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db_ops.common import data_sources, remote_exec
from db_ops.lib.shell import powershell_executable
from db_ops.lib.event_policy import (
    PHASE_CONNECT,
    PHASE_EXECUTE,
    normalize_error_signature,
    normalize_error_type,
    resolve_failure_phase,
)
from db_ops.lib.target_profile import TargetProfile, candidate_variants, select_variant, version_matches
from db_ops.lib.time_window import is_time_window_open, job_due
from db_ops.config import DbOpsConfig
from db_ops.metrics.definitions import DEFAULT_DEFINITIONS_PATH, load_max_parallel_servers, load_metric_definitions
from db_ops.metrics.executor import execute_metric_sql
from db_ops.metrics.importance import DEFAULT_OVERRIDES_PATH, load_metric_importance_overrides, resolve_metric_importance
from db_ops.metrics.models import CollectSummary, MetricDefinition, MetricResult, MetricTarget
from db_ops.metrics.storage import MetricStore
from db_ops.metrics.targets import DEFAULT_DATA_DIR, load_metric_targets


NORMALIZED_RESULT_FIELDS = ("metric_item", "metric_value", "metric_unit", "status", "message")
SUPPORTED_CMD_PLATFORMS = {"windows", "linux"}
SUPPORTED_RESULT_STATUSES = {"OK", "WARN", "WARNING", "CRITICAL", "UNKNOWN", "ERROR", "NO_DATA", "LOGGING"}


class UnsupportedCollectorType(RuntimeError):
    pass


class UnsupportedExecutionMethod(RuntimeError):
    pass


# `_safe_text` is `db_ops.lib.coerce.as_text` since 2026-08-16 — the same five lines were here
# and in the other of `common`/`metrics`.


@dataclass(frozen=True)
class CommandExecution:
    rows: list[dict[str, Any]]
    raw_stdout: str
    raw_stderr: str
    exit_code: int
    execution_time: float


@dataclass
class _Tally:
    """What one pass — or one server_id's share of it — counted.

    The per-server workers each fill their own tally and the parent merges them, so no counter is
    written from two threads and the totals cannot go wrong under a wider fan-out.
    """

    metric_count: int = 0
    executed_count: int = 0
    skipped_interval_count: int = 0
    skipped_window_count: int = 0
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
    """Walk one target's metrics in catalog order, recording what happened into ``tally``.

    Lifted out of :func:`collect_metrics` unchanged so that a server_id's metrics keep running
    strictly one after another: only whole targets are handed to different workers.
    """
    target_metrics = [item for item in definitions if _metric_supports_db_type(item, target.db_type, include_unsupported=True)]
    for metric in target_metrics:
        unsupported_reason = _metric_unsupported_reason(metric, target)
        if unsupported_reason:
            tally.disabled_count += 1
            tally.messages.append(f"SKIP unsupported {metric.metric_code} target={target.target_id}: {unsupported_reason}")
            _print_metric_skip(
                target=target,
                metric=metric,
                reason=f"unsupported: {unsupported_reason}",
                dry_run=dry_run,
            )
            continue
        if not _metric_enabled_for_target(metric, target):
            tally.disabled_count += 1
            tally.messages.append(f"SKIP disabled {metric.metric_code} target={target.target_id}")
            _print_metric_skip(target=target, metric=metric, reason="disabled by target metric config", dry_run=dry_run)
            continue
        importance = resolve_metric_importance(metric, target, overrides)
        if importance == 0:
            tally.disabled_count += 1
            tally.messages.append(f"SKIP disabled {metric.metric_code} target={target.target_id}")
            _print_metric_skip(target=target, metric=metric, reason="disabled by importance override", dry_run=dry_run)
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
            _print_metric_skip(
                target=target,
                metric=metric,
                reason=f"outside its allowed window ({_window_label(metric)})",
                dry_run=False,
            )
            continue
        if not force and store is not None and not _metric_due(store=store, target=target, metric=metric, now=started):
            tally.skipped_interval_count += 1
            tally.messages.append(f"SKIP interval {metric.metric_code} target={target.target_id}")
            _print_metric_skip(
                target=target,
                metric=metric,
                reason=f"interval not elapsed ({metric.interval_seconds}s)",
                dry_run=False,
            )
            continue
        if dry_run:
            _print_line(f"DRY-RUN {metric.metric_code} target={target.target_id} importance={importance}")
            continue

        tally.executed_count += 1
        results = _collect_one_metric(
            metric=metric,
            target=target,
            importance=importance,
            secrets=secrets,
            collected_at=_format_utc(datetime.now(timezone.utc)),
            store=store,
        )
        assert store is not None and run_id is not None
        inserted = store.insert_results(run_id=run_id, results=results)
        tally.result_count += inserted
        tally.ok_count += sum(1 for item in results if item.status.upper() in {"OK", "LOGGING"})
        tally.error_count += sum(1 for item in results if item.status.upper() == "ERROR")
        tally.warning_count += sum(1 for item in results if item.status.upper() in {"WARNING", "WARN", "UNKNOWN"})
        tally.critical_count += sum(1 for item in results if item.status.upper() == "CRITICAL")
        tally.no_data_count += sum(1 for item in results if item.status.upper() == "NO_DATA")
        _print_metric_progress(
            run_id=run_id,
            target=target,
            metric=metric,
            inserted=inserted,
            results=results,
        )


def collect_metrics(
    *,
    config: DbOpsConfig,
    db_type: str | None = None,
    target_id: str | None = None,
    metric_code: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    include_windowed: bool = False,
    archive_days: int = 30,
) -> CollectSummary:
    started = datetime.now(timezone.utc)
    started_at = _format_utc(started)
    definitions = load_metric_definitions(DEFAULT_DEFINITIONS_PATH, active_only=True)
    if metric_code:
        definitions = [item for item in definitions if item.metric_code == metric_code]
    if db_type:
        definitions = [item for item in definitions if _metric_supports_db_type(item, db_type.lower(), include_unsupported=True)]
    if metric_code and not definitions:
        raise RuntimeError(f"metric_code not found or inactive: {metric_code}")

    overrides = load_metric_importance_overrides(
        DEFAULT_OVERRIDES_PATH,
        known_metric_codes={item.metric_code for item in load_metric_definitions(DEFAULT_DEFINITIONS_PATH)},
    )
    targets = load_metric_targets(data_dir=DEFAULT_DATA_DIR, db_type=db_type, target_id=target_id)
    # Dry-run never connects, so it does not need the (encrypted) secret store. Skipping the
    # load lets `collect --dry-run` run without a --key, as the docs show.
    secrets = {} if dry_run else data_sources.load_secret_text(DEFAULT_DATA_DIR)
    store = None if dry_run else MetricStore.from_config(config)
    # Archive first: before collecting, move metric rows older than archive_days into
    # metric_results_archive so the live table stays small (rows are kept, not deleted).
    archived_count = 0 if store is None else store.archive_old_results(retention_days=archive_days)
    run_id = None if store is None else store.start_run(started_at=started_at, message="Metric collect started.")

    tally = _Tally()
    if archived_count:
        tally.messages.append(
            f"Archived {archived_count} metric row(s) older than {archive_days} days into metric_results_archive."
        )

    # One worker per server_id — never per target. Several targets can name the same machine (a
    # database instance and the OS metrics reached through its cmd_access), and two workers on one
    # box would stack WinRM/SSH sessions and metric queries on it. Grouping by server_id keeps the
    # metrics of a machine strictly one at a time, in catalog order, while letting *different*
    # machines overlap.
    #
    # That overlap is the point. The pass used to be a single queue over every target, so one
    # unresponsive host held the whole estate behind it: a WinRM call to an ERP application server
    # was measured holding the pass for 490 of its 598 seconds, and during that window the ERP
    # database's lock metrics were never sampled — a blocking chain grew from 21s to 570s without
    # a single CRITICAL being raised, because nothing looked.
    groups: dict[str, list[MetricTarget]] = {}
    for target in targets:
        groups.setdefault(target.server_id or target.target_id, []).append(target)
    max_workers = max(1, min(load_max_parallel_servers(), len(groups)))
    if store is not None:
        # Build/upgrade the schema once, here, rather than letting every worker's first store call
        # race to do it. Each store method opens its own connection, so the workers are otherwise
        # independent — there is no shared connection or cursor between them.
        store.initialize()

    def run_group(group_targets: list[MetricTarget]) -> _Tally:
        group_tally = _Tally()
        for target in group_targets:
            _collect_target(
                target=target,
                definitions=definitions,
                overrides=overrides,
                secrets=secrets,
                store=store,
                run_id=run_id,
                started=started,
                dry_run=dry_run,
                force=force,
                include_windowed=include_windowed,
                tally=group_tally,
            )
        return group_tally

    try:
        if max_workers == 1:
            for group_targets in groups.values():
                tally.merge(run_group(group_targets))
        else:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="metric-server") as pool:
                # Merged in submission order, not completion order, so the run message reads the
                # same whatever the hosts do — a pass is compared against yesterday's by eye.
                futures = [pool.submit(run_group, group_targets) for group_targets in groups.values()]
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
    messages = tally.messages

    finished = datetime.now(timezone.utc)
    finished_at = _format_utc(finished)
    message = _summary_message(messages)
    if store is not None and run_id is not None:
        store.finish_run(
            run_id=run_id,
            finished_at=finished_at,
            status=status,
            target_count=len(targets),
            metric_count=tally.metric_count,
            result_count=tally.result_count,
            error_count=tally.error_count,
            warning_count=tally.warning_count,
            critical_count=tally.critical_count,
            message=message,
        )
        store.rebuild_target_health(run_id=run_id)
    return CollectSummary(
        run_id=run_id,
        target_count=len(targets),
        metric_count=tally.metric_count,
        executed_count=tally.executed_count,
        skipped_interval_count=tally.skipped_interval_count,
        skipped_window_count=tally.skipped_window_count,
        disabled_count=tally.disabled_count,
        result_count=tally.result_count,
        ok_count=tally.ok_count,
        error_count=tally.error_count,
        warning_count=tally.warning_count,
        critical_count=tally.critical_count,
        no_data_count=tally.no_data_count,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=round((finished - started).total_seconds(), 3),
        message=message,
    )


def _collect_one_metric(
    *,
    metric: MetricDefinition,
    target: MetricTarget,
    importance: int,
    secrets: dict[str, str],
    collected_at: str,
    store: MetricStore | None = None,
) -> list[MetricResult]:
    try:
        if metric.collector_type == "sql":
            rows = _run_sql_file(metric=metric, target=target, secrets=secrets)
            command_meta = None
        elif metric.collector_type == "cmd":
            command_meta = _run_command_file(metric=metric, target=target, secrets=secrets)
            rows = command_meta.rows
        elif metric.collector_type == "docker":
            command_meta = _run_docker_metric(metric=metric, target=target, secrets=secrets)
            rows = command_meta.rows
        else:
            raise UnsupportedCollectorType(f"Unsupported collector_type: {metric.collector_type}")
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


class MetricCommandError(RuntimeError):
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


def _run_sql_file(*, metric: MetricDefinition, target: MetricTarget, secrets: dict[str, str]) -> list[dict[str, Any]]:
    path = _resolve_metric_file_path(metric, target)
    if path is None:
        raise RuntimeError(f"Metric SQL path is not resolved: {metric.metric_code}")
    sql_text = Path(path).read_text(encoding="utf-8-sig")
    rows = execute_metric_sql(
        target=target,
        sql_text=sql_text,
        secrets=secrets,
        sql_timeout_seconds=metric.default_timeout,
        max_rows=metric.max_rows,
        per_database=_variant_is_per_database(metric, target),
    )
    # Normalize column-name case to the lowercase result contract. Oracle folds unquoted
    # aliases (AS metric_value) to UPPERCASE, so direct oracledb and the api bridge both
    # return METRIC_VALUE etc.; SQL Server already preserves the lowercase aliases (no-op).
    return [{str(key).lower(): value for key, value in row.items()} for row in rows]


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
    if not container:
        raise RuntimeError(f"No container_name configured for docker metric {metric.metric_code} on {target.target_id}.")
    path = _resolve_metric_file_path(metric, target)
    if path is None:
        raise RuntimeError(f"Docker metric script is not resolved: {metric.metric_code}")
    collector_env = {
        **_collector_env(metric=metric, target=target, secrets=secrets),
        "DOCKER_CONTAINER": container,
    }
    cmd_access = target.cmd_access or {}
    method = str(cmd_access.get("method") or "").strip().lower()
    if bool(cmd_access.get("enabled", False)) and method == "ssh":
        return execute_ssh(
            Path(path), target=target, secrets=secrets,
            timeout_seconds=metric.default_timeout, collector_env=collector_env,
        )
    return execute_local(
        Path(path),
        timeout_seconds=metric.default_timeout,
        collector_env=collector_env,
    )


def _run_command_file(*, metric: MetricDefinition, target: MetricTarget, secrets: dict[str, str]) -> CommandExecution:
    path = _resolve_metric_file_path(metric, target)
    if path is None:
        raise RuntimeError(f"Metric command path is not resolved: {metric.metric_code}")
    cmd_access = target.cmd_access or {}
    if not bool(cmd_access.get("enabled", False)):
        raise RuntimeError(f"Command access is not enabled for target: {target.target_id}")
    # A config error recorded at load time (bad method, missing credential): report it here so
    # it lands as this target's failing metric rather than having aborted the whole scan.
    if cmd_access.get("error"):
        raise RuntimeError(str(cmd_access["error"]))
    method = str(cmd_access.get("method") or "").strip().lower()
    collector_env = _collector_env(metric=metric, target=target, secrets=secrets)
    if method == "local":
        # `local` runs the script inside the collector (the worker container). Pointed at another
        # machine it does not fail — it succeeds and files THIS host's numbers under that host's
        # name, so the target looks monitored while none of its OS data is its own. Refuse
        # instead: a config error someone can fix beats data nobody can trust.
        remote_exec.assert_local_host(str(cmd_access.get("host") or ""), method=method)
        return execute_local(Path(path), timeout_seconds=metric.default_timeout, collector_env=collector_env)
    if method == "ssh":
        return execute_ssh(
            Path(path), target=target, secrets=secrets,
            timeout_seconds=metric.default_timeout, collector_env=collector_env,
        )
    if method == "winrm":
        return execute_winrm(
            Path(path), target=target, secrets=secrets,
            timeout_seconds=metric.default_timeout, collector_env=collector_env,
        )
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
    same rule as ``restore_config.json``'s ``env_secrets`` (:mod:`db_ops.backup_restore.backup`),
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
    resolved: dict[str, str] = {"DB_OPS_TARGET_HOST": _target_host(target)}
    for source in (metrics_cfg.get("collector_env"), override_cfg.get("collector_env")):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            name = str(key).strip()
            if not name or value in (None, ""):
                continue
            if sensitive_env_name(name):
                raise RuntimeError(
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
                raise RuntimeError(
                    f"{metric.metric_code} on {target.target_id}: env_secrets maps {name} to secret "
                    f"ref '{ref_name}', which is not in the secret store. Add it, or pass "
                    "--key/--key-base64 so the store can be read."
                )
            resolved[name] = value
    return resolved


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


def _shell_prelude(collector_env: dict[str, str], *, powershell: bool) -> str:
    """Assignments prepended to the script so a remote shell sees the values as env vars.
    The quoting rules live in ``common.remote_exec`` — every transport shares them."""
    return remote_exec.shell_prelude(
        collector_env,
        shell=remote_exec.SHELL_POWERSHELL if powershell else remote_exec.SHELL_BASH,
    )


def _script_with_env(path: Path, collector_env: dict[str, str], *, powershell: bool) -> str:
    return _shell_prelude(collector_env, powershell=powershell) + Path(path).read_text(encoding="utf-8-sig")


def execute_local(path: Path, *, timeout_seconds: int, collector_env: dict[str, str] | None = None) -> CommandExecution:
    command = _command_args(Path(path))
    env = os.environ.copy()
    env.update(collector_env or {})
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(Path(path).parent),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
        execution_time = round(time.monotonic() - started, 3)
    except subprocess.TimeoutExpired as exc:
        execution_time = round(time.monotonic() - started, 3)
        raise MetricCommandError(
            f"Command timed out after {timeout_seconds} seconds.",
            raw_stdout=exc.stdout or "",
            raw_stderr=exc.stderr or "",
            execution_time=execution_time,
        ) from exc
    raw_stdout = completed.stdout or ""
    raw_stderr = completed.stderr or ""
    if completed.returncode != 0:
        raise MetricCommandError(
            f"Command exited with code {completed.returncode}.",
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=completed.returncode,
            execution_time=execution_time,
        )
    try:
        parsed = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise MetricCommandError(
            f"Command stdout is not valid JSON: {exc.msg}.",
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=completed.returncode,
            execution_time=execution_time,
        ) from exc
    if not isinstance(parsed, list):
        raise MetricCommandError(
            "Command stdout must be a JSON array.",
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=completed.returncode,
            execution_time=execution_time,
        )
    rows = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            raise MetricCommandError(
                f"Command JSON row {index} must be an object.",
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
                exit_code=completed.returncode,
                execution_time=execution_time,
            )
        rows.append(item)
    return CommandExecution(
        rows=rows,
        raw_stdout=raw_stdout,
        raw_stderr=raw_stderr,
        exit_code=completed.returncode,
        execution_time=execution_time,
    )


def _cmd_access_json(target: MetricTarget) -> dict[str, Any]:
    """The target's ``cmd_access`` object, with the target IP as the default host.

    This *is* the JSON input ``db_ops.common.remote_exec`` expects — the collector does no
    translation, it just fills in the host that db_instances.json leaves implicit."""
    access = dict(target.cmd_access or {})
    access.setdefault("host", target.ip)
    return access


def _remote_shell_for(path: Path, target: MetricTarget) -> str:
    """Which shell runs this metric script: the file's own extension wins over the
    target's declared shell, so a ``.ps1`` reaches a Windows host as PowerShell even when
    ``cmd_access.shell`` was left unset."""
    if path.suffix.lower() == ".ps1":
        return remote_exec.SHELL_POWERSHELL
    declared = str((target.cmd_access or {}).get("shell") or "").strip().lower()
    return declared or remote_exec.SHELL_BASH


def _run_remote_metric_script(
    path: Path, *, target: MetricTarget, secrets: dict[str, str], timeout_seconds: int,
    collector_env: dict[str, str] | None, shell: str,
) -> CommandExecution:
    """Run a metric script on the target through ``common.remote_exec`` and adapt the
    result to :class:`CommandExecution`.

    Transport, auth and the env prelude live in ``common``; what stays here is the metrics
    contract on top of it — a non-zero exit or non-JSON stdout is a *metric* failure, so
    every transport error is re-raised as :class:`MetricCommandError` carrying the raw
    streams the metric row records."""
    started = time.monotonic()
    try:
        result = remote_exec.run_script(
            _cmd_access_json(target),
            path,
            credential=target.cmd_credential or {},
            secrets=secrets,
            shell=shell,
            timeout_seconds=timeout_seconds,
            env=collector_env or {},
            default_timeout_seconds=timeout_seconds,
        )
    except remote_exec.RemoteExecError as exc:
        raise MetricCommandError(
            str(exc),
            raw_stdout=exc.stdout,
            raw_stderr=exc.stderr,
            execution_time=exc.duration_seconds or round(time.monotonic() - started, 3),
            failure_phase=_remote_failure_phase(exc),
        ) from exc
    raw_stdout = as_text(result.stdout)
    raw_stderr = as_text(result.stderr)
    if result.exit_code != 0:
        raise MetricCommandError(
            f"Command exited with code {result.exit_code}.",
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=result.exit_code,
            execution_time=result.duration_seconds,
        )
    return CommandExecution(
        rows=_parse_command_stdout(raw_stdout, raw_stderr, result.exit_code, result.duration_seconds),
        raw_stdout=raw_stdout,
        raw_stderr=raw_stderr,
        exit_code=result.exit_code,
        execution_time=result.duration_seconds,
    )


def _remote_failure_phase(exc: remote_exec.RemoteExecError) -> str:
    """Whether a remote_exec failure happened opening the session or running the script.

    ``remote_exec`` raises one exception family for both halves, so the verdict is read off the
    subclass: auth rejected and host unreachable can only be the session. A timeout is the one
    ambiguous case — the same class covers "the connect never completed" and "the script ran too
    long" — and ``command`` separates them: it is set only once there is a command to run.
    """
    if isinstance(exc, (remote_exec.RemoteAuthError, remote_exec.RemoteConnectError)):
        return PHASE_CONNECT
    if isinstance(exc, remote_exec.RemoteTimeoutError) and not str(getattr(exc, "command", "") or ""):
        return PHASE_CONNECT
    return PHASE_EXECUTE


def execute_ssh(
    path: Path, *, target: MetricTarget, secrets: dict[str, str], timeout_seconds: int,
    collector_env: dict[str, str] | None = None,
) -> CommandExecution:
    """Run a metric script on the target over SSH (Linux, or Windows with OpenSSH Server)."""
    return _run_remote_metric_script(
        path, target=target, secrets=secrets, timeout_seconds=timeout_seconds,
        collector_env=collector_env, shell=_remote_shell_for(path, target),
    )


def execute_winrm(
    path: Path, *, target: MetricTarget, secrets: dict[str, str], timeout_seconds: int,
    collector_env: dict[str, str] | None = None,
) -> CommandExecution:
    """Run a metric script on a Windows target over WinRM.

    Legacy transport — prefer ``cmd_access.method = "ssh"`` where OpenSSH Server is
    available. WinRM needs pypsrp (or a PowerShell on the collector host) and
    Negotiate/NTLM auth, which often fails across networks and domains. Kept for targets
    that cannot run OpenSSH Server."""
    return _run_remote_metric_script(
        path, target=target, secrets=secrets, timeout_seconds=timeout_seconds,
        collector_env=collector_env, shell=remote_exec.SHELL_POWERSHELL,
    )


def _run_subprocess_command(command: list[str], *, cwd: Path, timeout_seconds: int) -> CommandExecution:
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_seconds, check=False)
        execution_time = round(time.monotonic() - started, 3)
    except subprocess.TimeoutExpired as exc:
        execution_time = round(time.monotonic() - started, 3)
        raise MetricCommandError(
            f"Command timed out after {timeout_seconds} seconds.",
            raw_stdout=exc.stdout or "",
            raw_stderr=exc.stderr or "",
            execution_time=execution_time,
        ) from exc
    raw_stdout = completed.stdout or ""
    raw_stderr = completed.stderr or ""
    if completed.returncode != 0:
        raise MetricCommandError(
            f"Command exited with code {completed.returncode}.",
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=completed.returncode,
            execution_time=execution_time,
        )
    return CommandExecution(
        rows=_parse_command_stdout(raw_stdout, raw_stderr, completed.returncode, execution_time),
        raw_stdout=raw_stdout,
        raw_stderr=raw_stderr,
        exit_code=completed.returncode,
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



def _command_args(path: Path) -> list[str]:
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


def _row_to_result(
    row: dict[str, Any],
    metric: MetricDefinition,
    target: MetricTarget,
    importance: int,
    collected_at: str,
    *,
    command_meta: CommandExecution | None = None,
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
            raise RuntimeError(f"{context} row {index} missing required field(s): {', '.join(missing)}.")
        status = str(row.get("status") or "").strip().upper()
        if status not in SUPPORTED_RESULT_STATUSES:
            raise RuntimeError(f"{context} row {index} has unsupported status: {status or '<missing>'}.")


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
        raise RuntimeError(
            f"metrics.disabled_collector_types must be a list for {target.target_id}, "
            f"e.g. [\"cmd\"]."
        )
    normalized = {str(item or "").strip().lower() for item in disabled}
    unknown = normalized - set(SUPPORTED_COLLECTOR_TYPES_FOR_TARGET)
    if unknown:
        raise RuntimeError(
            f"metrics.disabled_collector_types has unknown value(s) {sorted(unknown)} for "
            f"{target.target_id}; supported: {list(SUPPORTED_COLLECTOR_TYPES_FOR_TARGET)}."
        )
    return str(metric.collector_type or "").strip().lower() in normalized


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


def _apply_threshold_override(
    *, metric: MetricDefinition, target: MetricTarget, metric_value: str | None,
    status: str, message: str | None
) -> tuple[str, str | None]:
    """Evaluate optional numeric thresholds from the canonical per-target override."""
    config = _metric_override_config(target, metric.metric_code)
    if "warning_threshold" not in config and "critical_threshold" not in config:
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
    that skipped the interval also skipped the window. Bounds are local time; unset bounds mean
    always open, so real-time metrics are unaffected.
    """
    window = getattr(metric, "schedule_window", None)
    return window is None or is_time_window_open(window, now.astimezone())


def _window_label(metric: MetricDefinition) -> str:
    window = getattr(metric, "schedule_window", None)
    if window is None:
        return "always open"
    from_hour, to_hour = getattr(window, "from_hour", None), getattr(window, "to_hour", None)
    if from_hour is None and to_hour is None:
        return "restricted"
    return f"{'-' if from_hour is None else f'{from_hour:02d}'}-{'-' if to_hour is None else f'{to_hour:02d}'}h"


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
    last_time = datetime.fromisoformat(last.replace("Z", "+00:00"))
    last_errored = success is None or last > success
    # Shared convention via job_due: repeat_interval=0 => collect once; retry after a failure.
    return job_due(
        last_run=last_time,
        last_status="error" if last_errored else "done",
        repeat_interval=metric.interval_seconds,
        retry_interval=metric.retry_interval_seconds,
        now=now,
    )




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

    The db_type/platform/version rule itself lives in :mod:`db_ops.lib.target_profile` since
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
            raise RuntimeError(
                f"Target platform is required for cmd metric: {target.target_id}. "
                f"Set \"platform\": \"linux\" or \"windows\" on the db instance (not inside "
                f"cmd_access), or an \"os\" field it can be inferred from."
            )
        if profile.platform not in SUPPORTED_CMD_PLATFORMS:
            raise RuntimeError(f"Unsupported target platform for cmd metric: {profile.platform}")
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
    if metric.collector_type == "docker" and not str(getattr(target, "container_name", "") or "").strip():
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
