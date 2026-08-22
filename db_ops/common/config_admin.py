"""Add-a-SQL-task admin engine — the shared config-write helper (db_ops.common).

This is the single, side-effect-contained engine behind ``python -m db_ops.common.cli add-sql``
and ``metric-toggle``. Both the operator at a shell and the Telegram ``add_sql_task`` action reach
it the same way — through that CLI, with one JSON object — to register a new SQL task and enable
it:

1. Write the ``.sql`` text to ``assets/tasks/<db_type>/<server_id>/<name>.sql``
   (folder keyed by the unique ``server_id``) — the conventional location resolved by
   ``runner.resolve_sql_file``.
2. Append a ``sql_commands.json`` entry (``script_type='single'``, ``active``).
3. Append a ``sql_targets.json`` entry (server/instance/credential + time window,
   ``active``) so the scheduler picks it up.

All three writes go through an atomic temp-file + ``os.replace`` so a crash never
leaves a half-written config, and the JSON files keep their existing shape. The
module is **pure config + file I/O** — it never connects to a database and it
does not import the runner's execution path, so it is safe to unit-test and safe
to call from the Telegram worker without side effects beyond the three files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from db_ops.lib import response
from db_ops.lib.json_io import (  # noqa: F401 - one definition, see that module
    atomic_write_text as _atomic_write,
    dump_json_text as _dump_json,
    looks_like_json_request,
)

from db_ops.lib.notify import NOTIFY_CHAT_LEVELS, NotifyConfigError, notify_rule_dict
from db_ops.lib import task_output
from db_ops.lib.sql_access import KNOWN_DB_TYPES  # noqa: F401 - one definition, see that module
from db_ops.lib.task_output import (  # noqa: F401 - one definition, see that module
    FILE_OUTPUT_FORMATS,
    OUTPUT_FORMATS,
    TaskOutputError,
)
from db_ops.lib.time_window import MANUAL_ONLY
from db_ops.lib.paths import DEFAULT_DATA_DIR, TOOL_ROOT  # noqa: F401 - one definition, see that module

# The notify shape (levels, rule form, validation) is owned by db_ops.lib.notify — this
# module only writes it into a sql_targets entry.
def _notify_rule_dict(*, enabled: bool, telegram_chat: str, chat_id: str | None) -> dict[str, Any]:
    try:
        return notify_rule_dict(enabled=enabled, telegram_chat=telegram_chat, chat_id=chat_id)
    except NotifyConfigError as exc:
        raise ConfigAdminError(str(exc)) from exc

# time_window keys we accept for a new target (mirrors sql_targets.json / common.time_window).
_TIME_WINDOW_KEYS = (
    "from_year", "to_year", "from_month", "to_month", "from_day", "to_day",
    "from_hour", "to_hour", "from_minute", "to_minute", "repeat_interval", "timeout",
)
_DEFAULT_TIME_WINDOW = {
    "from_year": None, "to_year": None, "from_month": None, "to_month": None,
    "from_day": 1, "to_day": 31, "from_hour": 0, "to_hour": 23,
    "from_minute": None, "to_minute": None, "repeat_interval": 300, "timeout": 1800,
}


# What a finished task run does with its result set is `db_ops.lib.task_output`'s vocabulary —
# `none` still runs the SQL and still reports success/failure, it just has no result to deliver,
# which is what a maintenance UPDATE wants. The file formats are rendered by
# `db_ops.lib.result_format`, so a task export and an ad-hoc `run-sql --format` are the same
# artifact rather than two renderings that drift.

# Where a SQL task reports, on run and on failure. Every target in `sql_targets.json` routes to
# the dedicated "System - SQL tasks" group, and a test asserts it — a task added through the bot
# has to follow the same convention, or its runs (and its xlsx export) land in "System - Logs"
# where nobody is looking for them. Not in NOTIFY_CHAT_LEVELS: that tuple is the generic set,
# while `telegram_groups.json` also defines app-specific levels (sql, sla, backup, restore).
SQL_TASK_NOTIFY_CHAT = "sql"
_SQL_TASK_CHAT_CHOICES = tuple(dict.fromkeys((SQL_TASK_NOTIFY_CHAT, *NOTIFY_CHAT_LEVELS)))

# The schedule answer that means "never run this on a timer". It is expressed in the target's
# own `time_window` as `repeat_interval = -1` (db_ops.lib.time_window.MANUAL_ONLY) rather
# than as a separate key, so a target states when it runs in exactly one place. Kept distinct
# from `active: false` on purpose: an inactive entry is hidden from /spbot_list_sql_tasks as
# "not in use", while a manual task is very much in use — it just only ever runs when someone
# asks for it by sql_id.
MANUAL_SCHEDULE = "manual"


class ConfigAdminError(ValueError):
    """Raised for any invalid add-sql request (bad db_type, empty name, ...)."""


# `resolve_target_from_server_id` — "what does db_instances.json already say about this server?" —
# moved to `db_ops.common.data_sources.target_resolve` on 2026-08-15. It is a *read of the data
# folder*, which has exactly one reader, and the Telegram app needs that answer before it calls
# this command; leaving it here meant an app importing `common` for a config lookup.
#
# `normalize_output` and the two format tuples left for `db_ops.lib.task_output` in the same pass,
# for the mirror-image reason: they are a vocabulary three components share, and a value belongs
# where every component may import it.


def slugify(text: str, *, max_len: int = 60) -> str:
    """File-safe slug: keep ASCII word chars, collapse the rest to single ``_``."""
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", str(text).strip()).strip("_")
    if not cleaned:
        cleaned = "sql"
    return cleaned[:max_len].strip("_") or "sql"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigAdminError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigAdminError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigAdminError(f"{path.name} is not a JSON object.")
    return data


def next_sql_id(commands: dict[str, Any]) -> int:
    ids = [int(item.get("sql_id", 0) or 0) for item in commands.get("sql_commands", [])]
    return (max(ids) + 1) if ids else 1


def next_target_no(targets: dict[str, Any], sql_id: int) -> int:
    nos = [
        int(item.get("target_no", 0) or 0)
        for item in targets.get("sql_targets", [])
        if int(item.get("sql_id", 0) or 0) == sql_id
    ]
    return (max(nos) + 1) if nos else 1


def normalize_time_window(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate + fill a time-window dict; unknown keys rejected, negatives rejected.

    The one accepted negative is ``repeat_interval = -1`` (manual) — the same rule the runtime
    parser applies in :mod:`db_ops.lib.time_window`, so a window this writes always survives
    being read back.
    """
    window = dict(_DEFAULT_TIME_WINDOW)
    if raw:
        for key, value in raw.items():
            if key not in _TIME_WINDOW_KEYS:
                raise ConfigAdminError(f"Unknown time_window field: {key}")
            if value is None or value == "":
                window[key] = None
                continue
            try:
                ivalue = int(value)
            except (TypeError, ValueError) as exc:
                raise ConfigAdminError(f"time_window.{key} must be an integer, got {value!r}") from exc
            if ivalue < 0 and not (key == "repeat_interval" and ivalue == MANUAL_ONLY):
                suffix = f", or {MANUAL_ONLY} for manual" if key == "repeat_interval" else ""
                raise ConfigAdminError(f"time_window.{key} must be >= 0{suffix}, got {ivalue}")
            window[key] = ivalue
    return window


def add_sql_task(
    *,
    db_type: str,
    server_id: str,
    sql_name: str,
    sql_text: str | None = None,
    sql_bytes: bytes | None = None,
    instance_name: str | None = None,
    service_name: str | None = None,
    database_name: str | None = None,
    credential_name: str | None = None,
    time_window: dict[str, Any] | None = None,
    manual_only: bool = False,
    output: str = "none",
    output_chat: str = "",
    output_chat_id: str | None = None,
    active: bool = True,
    logging_on_run: bool = True,
    alert_on_error: bool = True,
    logging_chat: str = SQL_TASK_NOTIFY_CHAT,
    error_chat: str = SQL_TASK_NOTIFY_CHAT,
    logging_chat_id: str | None = None,
    error_chat_id: str | None = None,
    version_from: str | None = None,
    version_to: str | None = None,
    data_dir: str | Path | None = None,
    tool_root: str | Path | None = None,
) -> dict[str, Any]:
    """Register a new single-script SQL task and (by default) enable it.

    Exactly one of ``sql_text`` / ``sql_bytes`` must be provided. Returns a summary
    dict with the assigned ``sql_id``, ``sql_code``, and written ``script_path``.
    Raises :class:`ConfigAdminError` for any invalid input; on success the three
    writes (``.sql`` file, ``sql_commands.json``, ``sql_targets.json``) are applied
    in order, each atomically.
    """
    db_type = str(db_type or "").strip().lower()
    if db_type not in KNOWN_DB_TYPES:
        raise ConfigAdminError(f"db_type must be one of {KNOWN_DB_TYPES}, got {db_type!r}.")
    server_id = str(server_id or "").strip()
    if not server_id:
        raise ConfigAdminError("server_id is required.")
    sql_name = str(sql_name or "").strip()
    if not sql_name:
        raise ConfigAdminError("sql_name is required.")

    if (sql_text is None) == (sql_bytes is None):
        raise ConfigAdminError("Provide exactly one of sql_text or sql_bytes.")
    if sql_bytes is not None:
        sql_text = sql_bytes.decode("utf-8-sig")
    assert sql_text is not None
    if not sql_text.strip():
        raise ConfigAdminError("SQL content is empty.")

    data_root = Path(data_dir).resolve() if data_dir else DEFAULT_DATA_DIR
    root = Path(tool_root).resolve() if tool_root else TOOL_ROOT
    commands_path = data_root / "sql_commands.json"
    targets_path = data_root / "sql_targets.json"
    commands = _read_json(commands_path)
    targets = _read_json(targets_path)
    commands.setdefault("sql_commands", [])
    targets.setdefault("sql_targets", [])

    sql_id = next_sql_id(commands)
    target_no = next_target_no(targets, sql_id)
    # Folder is keyed by server_id (already unique): assets/tasks/<db_type>/<server>/...
    server_slug = slugify(server_id, max_len=80)
    name_slug = slugify(sql_name)
    file_stem = f"{sql_id:03d}_{name_slug}"
    script_relpath = f"assets/tasks/{db_type}/{server_slug}/{file_stem}.sql"
    sql_code = f"{db_type.upper()}-{sql_id:03d}-{name_slug.upper()}"

    if any(str(item.get("script_path", "")).strip() == script_relpath for item in commands["sql_commands"]):
        raise ConfigAdminError(f"script_path already registered: {script_relpath}")

    window = normalize_time_window(time_window)
    if manual_only:
        window["repeat_interval"] = MANUAL_ONLY
    try:
        output_format = task_output.normalize_output(output)
    except TaskOutputError as exc:
        # `lib` may not know this module's exception type, so the boundary translates rather than
        # leaking a second error class out of `add_sql_task`.
        raise ConfigAdminError(str(exc)) from exc

    command_entry = {
        "sql_id": sql_id,
        "sql_code": sql_code,
        "sql_name": sql_name,
        "db_type": db_type,
        "script_type": "single",
        "script_path": script_relpath,
        "version_from": version_from,
        "version_to": version_to,
        "active": bool(active),
    }
    target_entry = {
        "sql_id": sql_id,
        "target_no": target_no,
        "server_id": server_id,
        "db_type": db_type,
        "service_name": service_name,
        "instance_name": instance_name,
        "database_name": database_name,
        "credential_name": credential_name,
        # `time_window.repeat_interval = -1` is what keeps the scheduler off a manual task; it
        # stays `active` so it keeps showing up in /spbot_list_sql_tasks, and only a forced run
        # (/spbot_run_sql_task) executes it.
        "time_window": window,
        "output": {
            "format": output_format,
            "telegram_chat": str(output_chat or logging_chat),
            "chat_id": output_chat_id or "",
        },
        "active": bool(active),
        # Canonical form: the two rules nested in the shared `notify` object, like
        # `time_window` beside it. The parser still reads the older top-level spelling, but
        # nothing new is written in it — a half-migrated file is how a convention rots.
        "notify": {
            "logging_on_run": _notify_rule_dict(
                enabled=logging_on_run, telegram_chat=logging_chat, chat_id=logging_chat_id
            ),
            "alert_on_error": _notify_rule_dict(
                enabled=alert_on_error, telegram_chat=error_chat, chat_id=error_chat_id
            ),
        },
    }

    # Write in dependency order: the .sql file first (so an enabled command never
    # points at a missing script), then commands, then targets.
    script_abs = (root / script_relpath).resolve()
    if script_abs.exists():
        raise ConfigAdminError(f"SQL file already exists: {script_relpath}")
    _atomic_write(script_abs, sql_text if sql_text.endswith("\n") else sql_text + "\n")

    commands["sql_commands"].append(command_entry)
    _atomic_write(commands_path, _dump_json(commands))

    targets["sql_targets"].append(target_entry)
    _atomic_write(targets_path, _dump_json(targets))

    return {
        "ok": True,
        "sql_id": sql_id,
        "sql_code": sql_code,
        "target_no": target_no,
        "script_path": script_relpath,
        "script_abs": str(script_abs),
        "db_type": db_type,
        "server_id": server_id,
        "active": bool(active),
        "manual_only": window["repeat_interval"] == MANUAL_ONLY,
        "repeat_interval": window["repeat_interval"],
        "output": output_format,
    }


# Collector classes a target can switch off wholesale (mirrors
# metrics.collector.SUPPORTED_COLLECTOR_TYPES_FOR_TARGET — kept literal here so this
# module stays free of app imports).
SUPPORTED_COLLECTOR_TYPES = ("sql", "cmd", "docker", "k8s")


def known_metric_codes(data_dir: str | Path | None = None) -> set[str]:
    """All metric_code values from data/metric_definitions.json (uppercase)."""
    data_root = Path(data_dir).resolve() if data_dir else DEFAULT_DATA_DIR
    definitions = _read_json(data_root / "metric_definitions.json")
    return {
        str(item.get("metric_code") or "").strip().upper()
        for item in definitions.get("metrics", [])
        if isinstance(item, dict) and str(item.get("metric_code") or "").strip()
    }


def set_metric_toggle(
    *,
    server_id: str,
    scope: str,
    enabled: bool,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Enable/disable metric collection for one ``server_id`` in ``db_instances.json``.

    ``scope`` selects what to switch:

    * ``all``                — the whole ``metrics.enabled`` flag of the target;
    * ``collector:<type>``   — one collector class (``sql``/``cmd``/``docker``/``k8s``)
                               via ``metrics.disabled_collector_types``;
    * ``<METRIC_CODE>``      — one metric via ``metrics.metric_overrides.<CODE>.enabled``
                               (validated against ``metric_definitions.json``). Enabling
                               also removes the code from the legacy
                               ``report_policy.disabled_metric_codes`` list, which blocks
                               collection the same way.

    The write is a single atomic replace of ``db_instances.json``. Returns a summary
    with the applied changes and any warnings (e.g. the metric is enabled but its
    whole collector class is still off). Raises :class:`ConfigAdminError` on unknown
    server_id / collector type / metric code.
    """
    server_id = str(server_id or "").strip()
    if not server_id:
        raise ConfigAdminError("server_id is required.")
    scope_text = str(scope or "").strip()
    if not scope_text:
        raise ConfigAdminError("scope is required: all, collector:<type>, or a metric_code.")

    data_root = Path(data_dir).resolve() if data_dir else DEFAULT_DATA_DIR
    instances_path = data_root / "db_instances.json"
    data = _read_json(instances_path)
    instances = data.get("db_instances")
    if not isinstance(instances, list):
        raise ConfigAdminError("db_instances.json has no db_instances list.")

    instance = next(
        (item for item in instances
         if isinstance(item, dict) and str(item.get("server_id") or "").strip().lower() == server_id.lower()),
        None,
    )
    if instance is None:
        raise ConfigAdminError(f"Unknown server_id: {server_id}")

    metrics_cfg = instance.get("metrics")
    if not isinstance(metrics_cfg, dict):
        metrics_cfg = {}
        instance["metrics"] = metrics_cfg

    changes: list[str] = []
    warnings: list[str] = []
    state = "on" if enabled else "off"

    if scope_text.lower() == "all":
        previous = bool(metrics_cfg.get("enabled", True))
        metrics_cfg["enabled"] = bool(enabled)
        changes.append(f"metrics.enabled: {previous} -> {bool(enabled)}")
        scope_label = "all"
    elif scope_text.lower().startswith("collector:"):
        collector = scope_text.split(":", 1)[1].strip().lower()
        if collector not in SUPPORTED_COLLECTOR_TYPES:
            raise ConfigAdminError(
                f"Unknown collector type {collector!r}; supported: {list(SUPPORTED_COLLECTOR_TYPES)}."
            )
        disabled = metrics_cfg.get("disabled_collector_types")
        if not isinstance(disabled, list):
            disabled = []
        normalized = [str(item or "").strip().lower() for item in disabled]
        if enabled:
            if collector in normalized:
                metrics_cfg["disabled_collector_types"] = [item for item in normalized if item != collector]
                changes.append(f"removed {collector!r} from metrics.disabled_collector_types")
            else:
                warnings.append(f"collector {collector!r} was not disabled — nothing to change.")
        else:
            if collector in normalized:
                warnings.append(f"collector {collector!r} is already disabled — nothing to change.")
            else:
                metrics_cfg["disabled_collector_types"] = normalized + [collector]
                changes.append(f"added {collector!r} to metrics.disabled_collector_types")
        scope_label = f"collector:{collector}"
    else:
        metric_code = scope_text.upper()
        codes = known_metric_codes(data_root)
        if metric_code not in codes:
            raise ConfigAdminError(f"Unknown metric_code: {metric_code}")
        overrides = metrics_cfg.get("metric_overrides")
        if not isinstance(overrides, dict):
            overrides = {}
            metrics_cfg["metric_overrides"] = overrides
        override = overrides.get(metric_code)
        if not isinstance(override, dict):
            override = {}
            overrides[metric_code] = override
        previous = bool(override.get("enabled", True))
        already_in_state = previous == bool(enabled)
        if not already_in_state:
            override["enabled"] = bool(enabled)
            changes.append(f"metric_overrides.{metric_code}.enabled: {previous} -> {bool(enabled)}")
        if enabled:
            # `disabled_reason` states why the metric is off. Left behind after it is switched
            # back on, it is a false statement that outlives the fact — and on the worker it
            # outlives it forever, because `metric_overrides` is worker-owned in the deploy
            # merge, so a master-side cleanup can never reach it. It was written together with
            # `enabled: false`; it goes with it.
            if override.pop("disabled_reason", None) is not None:
                changes.append(f"cleared metric_overrides.{metric_code}.disabled_reason")
            # An override that says only "enabled: true" is the default written out longhand.
            # Dropping it keeps the file readable and keeps the deploy merge from carrying a
            # record that means nothing. Anything else in there (severity_map,
            # metric_item_overrides, collector_env) is real config and stays.
            if list(override) == ["enabled"] and override["enabled"] is True:
                overrides.pop(metric_code, None)
                changes.append(f"removed the now-empty metric_overrides.{metric_code} entry")
        # Reported only when the call really did nothing: a metric already on whose stale
        # `disabled_reason` was just cleared has changed, and saying otherwise sends the
        # operator looking for a write that did happen.
        if already_in_state and not changes:
            warnings.append(f"{metric_code} is already {state} in metric_overrides — nothing to change.")
        if not override:
            overrides.pop(metric_code, None)
        if enabled:
            report_policy = instance.get("report_policy")
            if isinstance(report_policy, dict):
                disabled_codes = report_policy.get("disabled_metric_codes")
                if isinstance(disabled_codes, list):
                    kept = [item for item in disabled_codes if str(item or "").strip().upper() != metric_code]
                    if len(kept) != len(disabled_codes):
                        report_policy["disabled_metric_codes"] = kept
                        changes.append(f"removed {metric_code} from report_policy.disabled_metric_codes")
            disabled_collectors = {
                str(item or "").strip().lower()
                for item in (metrics_cfg.get("disabled_collector_types") or [])
            }
            if disabled_collectors:
                warnings.append(
                    f"note: collector class(es) {sorted(disabled_collectors)} are still disabled on this "
                    f"target; if {metric_code} belongs to one of them it stays off until that class is re-enabled."
                )
        if not bool(metrics_cfg.get("enabled", True)):
            warnings.append("note: metrics.enabled is false for this target — nothing collects until 'all' is on.")
        scope_label = metric_code

    if changes:
        # db_instances.json is kept in its existing 1-space-indent shape.
        _atomic_write(instances_path, _dump_json(data, indent=1))

    return {
        "ok": True,
        "server_id": str(instance.get("server_id") or server_id),
        "scope": scope_label,
        "enabled": bool(enabled),
        "changed": bool(changes),
        "changes": changes,
        "warnings": warnings,
    }


#: Statuses a severity remap may name. Mirrors `metrics.collector._OVERRIDE_STATUSES`; a value the
#: collector would silently drop must be refused here instead, because a severity map that looks
#: applied and is not reads as "the downgrade did not work" and sends someone to the wrong file.
SEVERITY_STATUSES = ("OK", "LOGGING", "WARNING", "CRITICAL", "ERROR", "NO_DATA")


def set_metric_severity_map(
    *,
    server_id: str,
    metric_code: str,
    severity_map: dict[str, str] | None,
    metric_item: str = "",
    note: str = "",
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Remap one metric's statuses for one ``server_id`` in ``db_instances.json``.

    The write behind "this alert is real but nobody is going to act on it": a standing condition
    keeps being collected and keeps its history, and stops being an alert. It is the same
    ``severity_map`` the collector reads at
    ``metrics.metric_overrides.<CODE>.severity_map`` (or ``.metric_item_overrides.<item>`` when
    ``metric_item`` is given), applied *after* the metric has graded itself.

    Existed as a hand-edit of the JSON until 2026-08-11, when downgrading six standing conditions
    at once needed it seven times and `CLAUDE.md`'s rule applied: a task that needs a throwaway
    script is a missing command. A hand-edit also skips the two checks below, both of which a
    typo silently defeats — an unknown ``metric_code`` and an unknown status are each accepted by
    the file and ignored by the collector.

    Passing ``severity_map=None`` (or an empty mapping) removes the remap and restores the
    metric's own grading.
    """
    server_id = str(server_id or "").strip()
    if not server_id:
        raise ConfigAdminError("server_id is required.")
    metric_code = str(metric_code or "").strip().upper()
    if not metric_code:
        raise ConfigAdminError("metric_code is required.")

    data_root = Path(data_dir).resolve() if data_dir else DEFAULT_DATA_DIR
    if metric_code not in known_metric_codes(data_root):
        raise ConfigAdminError(f"Unknown metric_code: {metric_code}")

    normalized: dict[str, str] = {}
    for source, destination in (severity_map or {}).items():
        src = str(source or "").strip().upper()
        dst = str(destination or "").strip().upper()
        src = "WARNING" if src == "WARN" else src
        dst = "WARNING" if dst == "WARN" else dst
        if src not in SEVERITY_STATUSES:
            raise ConfigAdminError(f"Unknown source status {source!r}; expected one of {list(SEVERITY_STATUSES)}.")
        if dst not in SEVERITY_STATUSES:
            raise ConfigAdminError(f"Unknown target status {destination!r}; expected one of {list(SEVERITY_STATUSES)}.")
        normalized[src] = dst

    instances_path = data_root / "db_instances.json"
    data = _read_json(instances_path)
    instances = data.get("db_instances")
    if not isinstance(instances, list):
        raise ConfigAdminError("db_instances.json has no db_instances list.")
    instance = next(
        (item for item in instances
         if isinstance(item, dict) and str(item.get("server_id") or "").strip().lower() == server_id.lower()),
        None,
    )
    if instance is None:
        raise ConfigAdminError(f"Unknown server_id: {server_id}")

    metrics_cfg = instance.setdefault("metrics", {})
    if not isinstance(metrics_cfg, dict):
        raise ConfigAdminError(f"{server_id}: metrics is not an object.")
    overrides = metrics_cfg.setdefault("metric_overrides", {})
    if not isinstance(overrides, dict):
        raise ConfigAdminError(f"{server_id}: metrics.metric_overrides is not an object.")
    override = overrides.setdefault(metric_code, {})
    if not isinstance(override, dict):
        raise ConfigAdminError(f"{server_id}: metric_overrides.{metric_code} is not an object.")

    item = str(metric_item or "").strip()
    if item:
        item_overrides = override.setdefault("metric_item_overrides", {})
        if not isinstance(item_overrides, dict):
            raise ConfigAdminError(f"{server_id}: {metric_code}.metric_item_overrides is not an object.")
        holder = item_overrides.setdefault(item, {})
        if not isinstance(holder, dict):
            raise ConfigAdminError(f"{server_id}: {metric_code}.metric_item_overrides.{item} is not an object.")
        scope_label = f"{metric_code}[{item}]"
    else:
        holder = override
        scope_label = metric_code

    previous = holder.get("severity_map") if isinstance(holder.get("severity_map"), dict) else {}
    changes: list[str] = []
    if normalized:
        if previous != normalized:
            holder["severity_map"] = normalized
            changes.append(f"{scope_label}.severity_map: {previous or 'none'} -> {normalized}")
        if note:
            # Why, next to what — the note is read back through Telegram and by whoever finds this
            # entry months later wondering whether the condition was fixed or just silenced.
            if holder.get("_note") != note:
                holder["_note"] = note
                changes.append(f"{scope_label}._note set")
    else:
        if previous:
            holder.pop("severity_map", None)
            holder.pop("_note", None)
            changes.append(f"{scope_label}.severity_map removed (was {previous})")
        # Leave nothing behind that means "default": an empty override is a record the deploy
        # merge would carry and a reader would have to interpret.
        if item and not holder:
            override["metric_item_overrides"].pop(item, None)
            if not override["metric_item_overrides"]:
                override.pop("metric_item_overrides", None)
        if not override:
            overrides.pop(metric_code, None)
        if not overrides:
            metrics_cfg.pop("metric_overrides", None)

    if changes:
        # db_instances.json is kept in its existing 1-space-indent shape.
        _atomic_write(instances_path, _dump_json(data, indent=1))

    return {
        "ok": True,
        "server_id": str(instance.get("server_id") or server_id),
        "metric_code": metric_code,
        "metric_item": item,
        "severity_map": normalized,
        "changed": bool(changes),
        "changes": changes,
    }


class _Parser(argparse.ArgumentParser):
    """An argparse parser whose rejections are catchable instead of fatal.

    Stock argparse answers a bad value by printing usage to **stderr** and raising ``SystemExit``,
    which through the CLI means the caller gets exit 2 and no JSON at all — indistinguishable from
    a process that crashed. Every other refusal this module makes is a response object, and a
    caller must not need a second way to read one. ``--help`` is unaffected: help exits through
    ``print_help``, not through ``error``.

    Subparsers inherit this: ``add_subparsers`` defaults ``parser_class`` to ``type(self)``, so
    ``add-sql``'s own twenty-five options refuse the same way the top level does.
    """

    def error(self, message: str):  # noqa: D102 - argparse's contract, not ours.
        raise ConfigAdminError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="db_ops.common.cli",
                     description="Register a new SQL task and (by default) enable it.")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add-sql", help="Add a single-script SQL task + target.")
    add.add_argument("--db-type", required=True, choices=KNOWN_DB_TYPES)
    add.add_argument("--server-id", required=True)
    add.add_argument("--sql-name", required=True)
    src = add.add_mutually_exclusive_group(required=True)
    src.add_argument("--sql-file", help="Path to a .sql file whose contents to register.")
    src.add_argument("--sql-text", help="Inline SQL text to register.")
    add.add_argument("--instance-name")
    add.add_argument("--service-name")
    add.add_argument("--database-name")
    add.add_argument("--credential-name")
    add.add_argument("--from-day", type=int)
    add.add_argument("--to-day", type=int)
    add.add_argument("--from-hour", type=int)
    add.add_argument("--to-hour", type=int)
    add.add_argument("--repeat-interval", type=int)
    add.add_argument("--timeout", type=int)
    add.add_argument("--inactive", action="store_true", help="Register but leave active=false.")
    add.add_argument("--manual-only", action="store_true",
                     help="Shortcut for --repeat-interval -1: never run on the schedule, only a "
                          "forced run (run-sql-id --force / /spbot_run_sql_task). Stays listed "
                          "and active.")
    add.add_argument("--output", choices=OUTPUT_FORMATS, default="none",
                     help="What to do with the result set: xlsx (send a workbook), plain (rows "
                          "as text in the message), none (status only). Default: none.")
    add.add_argument("--output-chat", choices=_SQL_TASK_CHAT_CHOICES, default=None,
                     help="Notify level the result is delivered to (default: --logging-chat).")
    # Telegram routing for the run/error notifications (object-form logging_on_run/alert_on_error).
    add.add_argument("--notify-chat", choices=_SQL_TASK_CHAT_CHOICES,
                     help="Shortcut: route BOTH the run log and the error alert to this notify level.")
    add.add_argument("--logging-chat", choices=_SQL_TASK_CHAT_CHOICES, default=SQL_TASK_NOTIFY_CHAT,
                     help=f"Notify level for the run start/finish log (default: {SQL_TASK_NOTIFY_CHAT}).")
    add.add_argument("--error-chat", choices=_SQL_TASK_CHAT_CHOICES, default=SQL_TASK_NOTIFY_CHAT,
                     help=f"Notify level for the failure alert (default: {SQL_TASK_NOTIFY_CHAT}).")
    add.add_argument("--logging-chat-id", default=None,
                     help="Explicit chat_id override for the run log (wins over --logging-chat).")
    add.add_argument("--error-chat-id", default=None,
                     help="Explicit chat_id override for the error alert (wins over --error-chat).")
    add.add_argument("--no-logging-on-run", dest="logging_on_run", action="store_false",
                     help="Do not send a Telegram log on run start/finish.")
    add.add_argument("--no-alert-on-error", dest="alert_on_error", action="store_false",
                     help="Do not send a Telegram alert on failure.")
    add.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))

    toggle = sub.add_parser("metric-toggle", help="Enable/disable metrics for one server_id in db_instances.json.")
    toggle.add_argument("--server-id", required=True)
    toggle.add_argument("--state", required=True, choices=["on", "off"])
    toggle.add_argument("--scope", required=True,
                        help="all | collector:<sql|cmd|docker|k8s> | <METRIC_CODE>")
    toggle.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    return parser


ADD_SQL_USAGE = (
    "usage: python -m db_ops.common.cli add-sql <json>|@<file>|-\n"
    "\n"
    'Register a SQL task + target and (by default) enable it:\n'
    '  {"db_type": "sqlserver", "server_id": "ACME-192-0-2-115",\n'
    '   "sql_name": "index_fragmentation", "sql_file": "assets/tasks/frag.sql"}\n'
    "\n"
    "Every --flag documented by `add-sql --help` is a key here, without the leading dashes\n"
    "and with underscores: --sql-name -> \"sql_name\". Store-true flags take true/false.\n"
    "\n"
    "Legacy form (still accepted): add-sql --db-type ... --server-id ...\n"
)

METRIC_TOGGLE_USAGE = (
    "usage: python -m db_ops.common.cli metric-toggle <json>|@<file>|-\n"
    "\n"
    'Enable or disable metric collection for one server_id:\n'
    '  {"server_id": "ACME-192-0-2-115", "state": "off", "scope": "collector:cmd"}\n'
    "\n"
    "Fields:\n"
    "  server_id  (required) the instance to toggle, as written in db_instances.json\n"
    "  state      (required) on | off\n"
    "  scope      (required) all | collector:<sql|cmd|docker|k8s> | <METRIC_CODE>\n"
    "  data_dir   folder holding db_instances.json (default: data/)\n"
    "\n"
    "Legacy form (still accepted): metric-toggle --server-id ... --state off --scope all\n"
)


def _argv_from_request(parser: argparse.ArgumentParser, command: str, request: dict[str, Any]) -> list[str]:
    """Turn a JSON request object into the flags this command's parser already understands.

    The alternative — reading the object directly in ``main`` — means restating every default,
    choice list and required-field rule a second time, and the two copies drift the first time
    a flag is added. Translating instead means the object form and the flag form cannot
    disagree: there is still exactly one parser, and ``add-sql``'s twenty-five options keep
    their validation for free.

    Keys are the flag names without dashes (``sql_name`` for ``--sql-name``). Store-true and
    store-false flags take a JSON boolean and emit the flag only when it would change
    anything, which is why ``no_logging_on_run`` is spelled ``logging_on_run: false`` — the
    key names the setting, not the flag that happens to turn it off.
    """
    subparser = None
    for action in parser._actions:  # noqa: SLF001 - argparse exposes subparsers no other way.
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            subparser = action.choices.get(command)
            break
    if subparser is None:
        raise ConfigAdminError(f"Unknown command: {command}")

    by_dest = {action.dest: action for action in subparser._actions if action.option_strings}  # noqa: SLF001
    argv: list[str] = [command]
    unknown: list[str] = []
    for raw_key, value in request.items():
        dest = str(raw_key).replace("-", "_")
        action = by_dest.get(dest)
        if action is None:
            unknown.append(str(raw_key))
            continue
        flag = action.option_strings[0]
        if isinstance(action, argparse._StoreTrueAction):  # noqa: SLF001
            if bool(value):
                argv.append(flag)
        elif isinstance(action, argparse._StoreFalseAction):  # noqa: SLF001
            # `logging_on_run: false` -> emit `--no-logging-on-run`; true is already the default.
            if not bool(value):
                argv.append(flag)
        elif value is not None:
            argv.extend([flag, str(value)])
    if unknown:
        # Refuse rather than ignore: a misspelled key in a request object is invisible
        # otherwise, and the task would be registered with a silently missing setting.
        raise ConfigAdminError(
            f"Unknown field(s) for {command}: {', '.join(sorted(unknown))}. "
            f"Valid fields: {', '.join(sorted(by_dest))}."
        )
    return argv


def main(argv: list[str] | None = None) -> int:
    """``add-sql`` / ``metric-toggle``, answering in the shared response envelope.

    **The envelope arrived on 2026-08-15**, when the Telegram app stopped importing this module
    and started calling the CLI. Until then both commands printed their bare result dict on
    success and an ``ERROR:`` line on *stderr* with exit 2 on failure — the exact split
    ``lib/response.py`` forbids, and unusable from a caller that has only stdout: a failure
    produced no JSON at all, so the app could not tell "the task was not registered" from "the
    process did not start". Nothing parsed the old shape (checked across ``data/``, the app
    commands and the tests), so the change cost no caller.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    operation = argv[0] if argv else "config-admin"
    # The JSON-object form. A leading `{`, `@` or `-` cannot begin any flag form, so the two
    # are told apart without a mode switch. See db_ops.common.cli._optional_json_request.
    if len(argv) >= 2 and looks_like_json_request(argv[1]):
        from db_ops.common.cli import _read_json_request

        usage = ADD_SQL_USAGE if argv[0] == "add-sql" else METRIC_TOGGLE_USAGE
        request, code = _read_json_request(argv[1], usage)
        if request is None:
            return code
        try:
            argv = _argv_from_request(_build_parser(), argv[0], request)
        except ConfigAdminError as exc:
            return response.emit(response.fail(operation, str(exc)))

    try:
        args = _build_parser().parse_args(argv)
    except ConfigAdminError as exc:
        return response.emit(response.fail(operation, str(exc)))
    if args.command == "add-sql":
        if args.sql_file:
            try:
                sql_bytes = Path(args.sql_file).read_bytes()
            except OSError as exc:
                # An unreadable --sql-file used to traceback, which through the CLI means a
                # caller gets no JSON and cannot tell it apart from a crashed process.
                return response.emit(response.fail("add-sql", f"sql_file: {exc}"))
            sql_text = None
        else:
            sql_bytes = None
            sql_text = args.sql_text
        window = {k: v for k, v in {
            "from_day": args.from_day, "to_day": args.to_day,
            "from_hour": args.from_hour, "to_hour": args.to_hour,
            "repeat_interval": args.repeat_interval, "timeout": args.timeout,
        }.items() if v is not None}
        # --notify-chat is a shortcut that routes both notifications to one level.
        logging_chat = args.notify_chat or args.logging_chat
        error_chat = args.notify_chat or args.error_chat
        try:
            result = add_sql_task(
                db_type=args.db_type, server_id=args.server_id, sql_name=args.sql_name,
                sql_text=sql_text, sql_bytes=sql_bytes,
                instance_name=args.instance_name, service_name=args.service_name,
                database_name=args.database_name, credential_name=args.credential_name,
                time_window=window or None, active=not args.inactive,
                manual_only=args.manual_only, output=args.output,
                output_chat=args.output_chat or "",
                logging_on_run=args.logging_on_run, alert_on_error=args.alert_on_error,
                logging_chat=logging_chat, error_chat=error_chat,
                logging_chat_id=args.logging_chat_id, error_chat_id=args.error_chat_id,
                data_dir=args.data_dir,
            )
        except (ConfigAdminError, OSError) as exc:
            return response.emit(response.fail("add-sql", str(exc)))
        return response.emit(response.ok(
            "add-sql",
            message=(f"Registered {result['sql_code']} (sql_id {result['sql_id']}) "
                     f"on {result['server_id']}, script {result['script_path']}."),
            data=result,
        ))
    if args.command == "metric-toggle":
        try:
            result = set_metric_toggle(
                server_id=args.server_id,
                scope=args.scope,
                enabled=args.state == "on",
                data_dir=args.data_dir,
            )
        except ConfigAdminError as exc:
            return response.emit(response.fail("metric-toggle", str(exc)))
        changed = "changed" if result.get("changed") else "no change"
        return response.emit(response.ok(
            "metric-toggle",
            message=(f"{result.get('server_id')} scope {result.get('scope')} "
                     f"-> {args.state}: {changed}."),
            data=result,
        ))
    return response.emit(response.fail(operation, f"Unknown command: {args.command}"))


if __name__ == "__main__":
    raise SystemExit(main())
