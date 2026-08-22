from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

from db_ops.common import data_sources
from db_ops.lib.json_io import load_json_file
from db_ops.lib.time_window import parse_time_window_config
from db_ops.metrics.models import MetricDefinition, MetricVariant
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.paths import asset_dir, builtin_asset_root


DEFAULT_DEFINITIONS_PATH = TOOL_ROOT / "data" / "metric_definitions.json"
DEFAULT_SQL_DIR = asset_dir("metrics")
SUPPORTED_DB_TYPES = {"sqlserver", "oracle", "mysql", "postgresql", "postgres"}
MULTI_DB_TYPES = {"", "all", "multi", "*"}
REQUIRED_FIELDS = ("metric_code", "category", "default_importance", "active")
# The four collector transports:
#   sql    — database engines (sqlserver / oracle / mysql / postgresql / ...), per-engine SQL files
#   cmd    — OS metrics (Windows + Linux), per-platform script files run local/ssh/winrm
#   docker — per-container stats via the docker CLI (built-in collector; no script file)
#   k8s    — reserved for Kubernetes (scaffolded; collector not implemented yet)
SUPPORTED_COLLECTOR_TYPES = {"sql", "cmd", "docker", "k8s"}
# Collector types whose metrics are built into Python and carry no per-engine/platform script
# file or variant (the collector reads the target's own config, e.g. container_name).
BUILTIN_COLLECTOR_TYPES = {"docker", "k8s"}
SUPPORTED_PLATFORMS = {"windows", "linux"}
# Statuses a metric may declare for a failed collection (connection_error_severity /
# execution_error_severity). The full result-status set on purpose: "OK"/"LOGGING" is how an
# operator says a failure of this particular check is expected and must not alert (a mounted
# standby refusing every connect), which is the same thing severity_map does per target.
SUPPORTED_ERROR_SEVERITIES = {"OK", "LOGGING", "WARNING", "CRITICAL", "ERROR", "NO_DATA"}
# Missing => WARNING, the flat severity every collection failure used to get. A metric that
# omits the field keeps the old behavior instead of failing to load: a catalog that will not
# parse stops the whole estate's monitoring, which is worse than one metric alerting a level low.
DEFAULT_ERROR_SEVERITY = "WARNING"
SUPPORTED_EXTENSIONS = {
    "sql": {".sql"},
    "cmd": {".sh", ".ps1", ".bat", ".cmd", ".py"},
    "docker": {".sh", ".py"},
}


def load_max_parallel_servers(path: str | Path = DEFAULT_DEFINITIONS_PATH) -> int:
    """How many ``server_id`` groups a collect pass may walk at once (``collection`` block).

    Lives in the catalog rather than in code because it is a property of the estate being
    watched, not of the collector: the number that is safe here is the number of machines the
    worker may hold sessions against at once. Missing or unreadable settings mean 1 — the old
    fully serial pass — so a malformed edit degrades to the previous behavior instead of
    fanning out further than the operator intended.
    """
    data = load_json_file(Path(path))
    settings = data.get("collection")
    if not isinstance(settings, dict):
        return 1
    try:
        value = int(settings.get("max_parallel_servers", 1))
    except (TypeError, ValueError):
        return 1
    return max(1, value)



def _resolve_metric_file(sql_root: Path, file: str) -> Path:
    """Where one variant's file is — resolved **per file**, not per directory.

    The chosen root wins for the files it actually contains; anything it does not carry still
    comes from the package. Resolving per *directory* is what made adding a single metric hide
    all 189 shipped ones: creating ``assets/metrics/`` to hold one query meant every other
    variant was looked for there too, and the collector refused to start.
    """
    candidate = (sql_root / file).resolve()
    if candidate.exists():
        return candidate
    builtin_root = builtin_asset_root("metrics")
    if builtin_root is not None:
        builtin = (builtin_root / file).resolve()
        if builtin.exists():
            return builtin
    return candidate


def load_metric_definitions(
    path: str | Path = DEFAULT_DEFINITIONS_PATH,
    *,
    sql_dir: str | Path = DEFAULT_SQL_DIR,
    active_only: bool = False,
) -> list[MetricDefinition]:
    definitions_path = Path(path)
    # metric_definitions.json has one reader (common.data_sources) since 2026-08-15. This app
    # still owns what a definition *means* — validation, variants, sql_dir resolution — it just
    # no longer owns opening the file.
    raw_items = data_sources.load_metric_definition_records(definitions_path)

    sql_root = Path(sql_dir)
    seen: set[str] = set()
    definitions: list[MetricDefinition] = []
    errors: list[str] = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            errors.append(f"metrics[{index}] must be an object.")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in item]
        if missing:
            errors.append(f"metrics[{index}] missing required field(s): {', '.join(missing)}.")
            continue

        metric_code = str(item["metric_code"]).strip()
        db_type = str(item.get("db_type") or "multi").strip().lower()
        legacy_top_file = str(item.get("sql_file") or "").strip().replace("\\", "/")
        collector_type = str(item.get("collector_type") or ("sql" if "sql_file" in item or "sql_variants" in item else "")).strip().lower()
        file = str(item.get("file") or legacy_top_file).strip().replace("\\", "/")
        default_importance = _parse_int(item["default_importance"], f"{metric_code}.default_importance", errors)
        try:
            parsed_time_window = parse_time_window_config(
                item,
                context=f"metrics.{metric_code}",
                defaults={
                    "repeat_interval": item.get("interval_seconds", 300),
                    "retry_interval": item.get("retry_interval_seconds", 600),
                    "timeout": item.get("default_timeout", 5),
                },
            )
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        for warning in parsed_time_window.warnings:
            warnings.warn(warning, DeprecationWarning, stacklevel=2)
        interval_seconds = parsed_time_window.time_window.repeat_interval
        retry_interval_seconds = parsed_time_window.time_window.retry_interval
        default_timeout = parsed_time_window.time_window.timeout
        connection_error_severity = _parse_error_severity(
            item.get("connection_error_severity"), f"{metric_code}.connection_error_severity", errors
        )
        execution_error_severity = _parse_error_severity(
            item.get("execution_error_severity"), f"{metric_code}.execution_error_severity", errors
        )
        path = _resolve_metric_file(sql_root, file) if file else None
        raw_variants = _catalog_variants(item, metric_code)
        variants = _parse_variants(raw_variants, metric_code, db_type, collector_type, sql_root, errors)

        if not metric_code:
            errors.append(f"metrics[{index}] metric_code is required.")
        elif metric_code in seen:
            errors.append(f"Duplicate metric_code: {metric_code}.")
        seen.add(metric_code)
        if db_type not in SUPPORTED_DB_TYPES and db_type not in MULTI_DB_TYPES:
            errors.append(f"{metric_code}: db_type must be one of {sorted(SUPPORTED_DB_TYPES)} or multi, got '{db_type}'.")
        if collector_type not in SUPPORTED_COLLECTOR_TYPES:
            errors.append(f"{metric_code}: collector_type must be one of {sorted(SUPPORTED_COLLECTOR_TYPES)}, got '{collector_type or '<missing>'}'.")
        if default_importance is None or not 1 <= default_importance <= 5:
            errors.append(f"{metric_code}: default_importance must be from 1 to 5.")
        if interval_seconds is None or interval_seconds < 0:
            # 0 => run-once (collect a single time); negative is invalid.
            errors.append(f"{metric_code}: time_window.repeat_interval must be >= 0.")
        if default_timeout is None or default_timeout < 1:
            errors.append(f"{metric_code}: time_window.timeout must be >= 1.")
        if not file and not variants and collector_type not in BUILTIN_COLLECTOR_TYPES:
            errors.append(f"{metric_code}: variants must not be empty.")
        if file:
            _validate_extension(file, collector_type, f"{metric_code}.file", errors)
        if path is not None and not path.exists():
            errors.append(f"{metric_code}: file not found under {sql_root}: {file}.")

        if default_importance is None or interval_seconds is None or default_timeout is None:
            continue
        definitions.append(
            MetricDefinition(
                metric_code=metric_code,
                db_type=db_type,
                collector_type=collector_type,
                file=file,
                category=str(item["category"]).strip(),
                default_importance=default_importance,
                active=bool(item["active"]),
                interval_seconds=interval_seconds,
                retry_interval_seconds=retry_interval_seconds if retry_interval_seconds is not None else 600,
                default_timeout=default_timeout,
                empty_result_is_ok=bool(item.get("empty_result_is_ok", False)),
                connection_error_severity=connection_error_severity,
                execution_error_severity=execution_error_severity,
                max_rows=int(item.get("max_rows") or 0),
                report_policy=item.get("report_policy") if isinstance(item.get("report_policy"), dict) else {},
                path=path,
                variants=variants,
                schedule_window=parsed_time_window.time_window,
            )
        )

    if errors:
        raise RuntimeError("Invalid metric definitions:\n- " + "\n- ".join(errors))
    if active_only:
        return [item for item in definitions if item.active]
    return definitions


def _parse_error_severity(value: Any, name: str, errors: list[str]) -> str:
    """Read one of the two failure-severity fields, normalizing ``WARN`` to ``WARNING``.

    A typo is an error rather than a silent fall back to the default: "critcal" would otherwise
    read as WARNING forever on the one metric the operator most wanted paged.
    """
    if value in (None, ""):
        return DEFAULT_ERROR_SEVERITY
    severity = str(value).strip().upper()
    if severity == "WARN":
        severity = "WARNING"
    if severity not in SUPPORTED_ERROR_SEVERITIES:
        errors.append(f"{name} must be one of {sorted(SUPPORTED_ERROR_SEVERITIES)}, got '{value}'.")
        return DEFAULT_ERROR_SEVERITY
    return severity


def _parse_int(value: Any, name: str, errors: list[str]) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        errors.append(f"{name} must be an integer.")
        return None


def _catalog_variants(item: dict[str, Any], metric_code: str) -> Any:
    if "variants" in item:
        return item.get("variants")
    if "sql_variants" in item:
        warnings.warn(
            f"{metric_code}: sql_variants is deprecated; use variants instead.",
            DeprecationWarning,
            stacklevel=3,
        )
        return item.get("sql_variants")
    return None


def _parse_variants(
    value: Any,
    metric_code: str,
    parent_db_type: str,
    collector_type: str,
    sql_root: Path,
    errors: list[str],
) -> list[MetricVariant]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{metric_code}.variants must be a list.")
        return []
    if not value:
        # Built-in collectors (docker/k8s) legitimately carry no per-engine variant.
        if collector_type in BUILTIN_COLLECTOR_TYPES:
            return []
        errors.append(f"{metric_code}.variants must not be empty.")
        return []
    variants: list[MetricVariant] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            errors.append(f"{metric_code}.variants[{index}] must be an object.")
            continue
        name = str(item.get("name") or "").strip()
        db_type = str(item.get("db_type") or parent_db_type).strip().lower()
        platform = str(item.get("platform") or "").strip().lower()
        supported = bool(item.get("supported", True))
        file = str(item.get("file") or item.get("sql_file") or "").strip().replace("\\", "/")
        min_version = _optional_int(item.get("min_major_version"), f"{metric_code}.{name}.min_major_version", errors)
        max_version = _optional_int(item.get("max_major_version"), f"{metric_code}.{name}.max_major_version", errors)
        unsupported_reason = str(item.get("unsupported_reason") or "").strip()
        per_database = bool(item.get("per_database", False))
        path = _resolve_metric_file(sql_root, file) if file else None
        if not name:
            errors.append(f"{metric_code}.variants[{index}].name is required.")
        if db_type not in SUPPORTED_DB_TYPES and db_type not in MULTI_DB_TYPES:
            errors.append(f"{metric_code}.variants[{index}].db_type must be one of {sorted(SUPPORTED_DB_TYPES)} or multi, got '{db_type}'.")
        if per_database and db_type not in {"postgresql", "postgres"}:
            errors.append(
                f"{metric_code}.variants[{index}].per_database is PostgreSQL-only "
                f"(got db_type '{db_type}'): every other engine reaches its databases from one "
                f"connection, so iterating would collect the same rows N times.")
        if platform and platform not in SUPPORTED_PLATFORMS:
            errors.append(f"{metric_code}.variants[{index}].platform must be one of {sorted(SUPPORTED_PLATFORMS)}, got '{platform}'.")
        if collector_type == "cmd" and supported and not platform:
            errors.append(f"{metric_code}.variants[{index}].platform is required for cmd metrics.")
        if supported and not file:
            errors.append(f"{metric_code}.variants[{index}].file is required.")
        if file:
            _validate_extension(file, collector_type, f"{metric_code}.variants[{index}].file", errors)
        if supported and path is not None and not path.exists():
            errors.append(f"{metric_code}: variant file not found under {sql_root}: {file}.")
        variants.append(
            MetricVariant(
                name=name,
                db_type=db_type,
                platform=platform,
                supported=supported,
                file=file,
                min_major_version=min_version,
                max_major_version=max_version,
                unsupported_reason=unsupported_reason,
                path=path,
                per_database=per_database,
            )
        )
    return variants


def _validate_extension(file: str, collector_type: str, context: str, errors: list[str]) -> None:
    suffix = Path(file).suffix.lower()
    supported = SUPPORTED_EXTENSIONS.get(collector_type, set())
    if supported and suffix not in supported:
        errors.append(f"{context} extension must be one of {sorted(supported)}, got '{suffix or '<none>'}'.")


def _optional_int(value: Any, name: str, errors: list[str]) -> int | None:
    if value in (None, ""):
        return None
    return _parse_int(value, name, errors)
