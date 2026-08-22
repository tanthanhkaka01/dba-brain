from __future__ import annotations
from db_ops.common.data_sources.metric_targets import _server_id_from_ip  # noqa: F401 - one definition, see that module

from pathlib import Path
from typing import Any

from db_ops.lib import sql_access
from db_ops.common import data_sources
from db_ops.common import oracle_bridge
from db_ops.lib.cmd_access import (
    SUPPORTED_CMD_ACCESS_METHODS,
    SUPPORTED_PLATFORMS,
    resolve_cmd_access as _resolve_cmd_access,
    resolve_cmd_credential as _resolve_cmd_credential,
    resolve_platform as _resolve_platform,
)
from db_ops.common.sql_execution import (
    load_credentials_file,
    load_database_inventory,
    load_json_file,
    load_remote_credentials_file,
)
from db_ops.lib.target_flags import is_metrics_enabled, is_target_enabled
from db_ops.metrics.models import MetricTarget
from db_ops.lib.paths import DEFAULT_DATA_DIR, TOOL_ROOT  # noqa: F401 - one definition, see that module



# Reading a target's cmd_access block is not a metrics concern: db_ops.lib.cmd_access owns it,
# because operating on the host (restart, service control, patching) needs the exact same
# resolution and a second copy would drift the moment one side gained a method or a default.
# Re-exported here since both names are part of this module's published surface.
__all__ = [
    "DEFAULT_DATA_DIR",
    "SUPPORTED_CMD_ACCESS_METHODS",
    "SUPPORTED_PLATFORMS",
    "load_metric_targets",
    "resolve_metric_target",
]


def load_metric_targets(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    inventory_path: Path | None = None,
    db_type: str | None = None,
    target_id: str | None = None,
) -> list[MetricTarget]:
    # One reader for db_instances.json, and it is not this app. Four call sites across metrics,
    # sql_tasks and metric_targets_config each built the path and called json themselves; the
    # file is the estate inventory, so a second parse of it is a second opinion about what the
    # estate contains. `common.data_sources` owns the read (2026-08-15).
    instances = data_sources.load_db_instances(data_dir)
    # The inventory is now derived from db_instances.json; an explicit
    # inventory_path JSON/YAML file is still honored if provided.
    inventory = load_database_inventory(inventory_path) if inventory_path else data_sources.load_inventory(data_dir)
    credentials = data_sources.group_credentials_by_type(
        load_credentials_file(data_sources.users_path(data_dir))
    )
    remote_credentials = load_remote_credentials_file(data_sources.users_path(data_dir))

    targets: list[MetricTarget] = []
    for item in instances:
        if not is_target_enabled(item) or not is_metrics_enabled(item):
            continue
        metrics_cfg = item.get("metrics") or {}

        # An empty/null db_type marks a host with no database on it (OS metrics only):
        # no SQL variant matches it, so only cmd collectors run against the target.
        item_db_type = str(item.get("db_type") or "").strip().lower()
        if db_type and item_db_type != db_type.lower():
            continue

        inventory_match = _find_inventory_by_instance(item, inventory)
        server_id = str(item.get("server_id") or (inventory_match or {}).get("server_id") or _server_id_from_ip(item))
        service_name = str(item.get("service_name") or (inventory_match or {}).get("service_name") or "")
        instance_name = str(item.get("instance_name") or (inventory_match or {}).get("instance_name") or (inventory_match or {}).get("sid") or "")
        server_name = str(item.get("server_name") or (inventory_match or {}).get("server_name") or "").strip()
        database_names = _database_names(item, inventory_match)
        # db_name is retained for the existing metric_results schema. It is the
        # monitored service/instance label, not an expected user database.
        db_name = service_name or instance_name or server_name or server_id
        resolved_target_id = str(item.get("target_id") or f"{server_id}/{item_db_type}/{db_name}")
        if target_id and resolved_target_id != target_id:
            continue

        default_credential_name = str(item.get("default_credential_name") or item.get("credential_name") or "").strip()
        credential = _find_credential(
            credentials.get(item_db_type, []),
            server_id=server_id,
            db_type=item_db_type,
            service_name=service_name,
            instance_name=instance_name,
            credential_name=default_credential_name,
        )
        credential_name = str(default_credential_name or (credential or {}).get("credential_name") or "")
        port_value = item.get("port") or (inventory_match or {}).get("port")
        port = int(port_value) if port_value not in (None, "") else None
        ip = str(item.get("ip") or (inventory_match or {}).get("ip") or "")
        sqlserver_driver = str(item.get("sqlserver_driver") or "").strip()
        if not sqlserver_driver and isinstance(metrics_cfg, dict):
            sqlserver_driver = str(metrics_cfg.get("sqlserver_driver") or "").strip()
        sqlserver_major_version = item.get("sqlserver_major_version")
        major_version = item.get("major_version") or item.get(f"{item_db_type}_major_version")
        platform = _resolve_platform(item)
        # A target whose cmd_access is unusable must fail ALONE. These resolvers raise on a bad
        # method or a missing credential, and nothing caught it: one mis-configured entry
        # aborted `load_metric_targets` and with it the entire collection scan — every metric on
        # every target, database ones included, stopped. (Seen 2026-08-01 when a host was moved
        # off method=local and had no SSH credential yet.) The error is kept on the target and
        # raised when a cmd metric actually tries to run, so it still surfaces loudly, as one
        # failing target instead of an estate-wide outage.
        cmd_access_error = ""
        try:
            cmd_access = _resolve_cmd_access(item, platform=platform, host=ip)
            cmd_credential = _resolve_cmd_credential(cmd_access, remote_credentials)
        except RuntimeError as exc:
            cmd_access_error = str(exc)
            cmd_access = {"enabled": True, "error": cmd_access_error}
            cmd_credential = None
        sql_access = _resolve_sql_access(item)

        targets.append(
            MetricTarget(
                target_id=resolved_target_id,
                server_id=server_id,
                ip=ip,
                db_type=item_db_type,
                db_name=db_name,
                credential_name=credential_name,
                port=port,
                platform=platform,
                cmd_access=cmd_access,
                cmd_credential=cmd_credential,
                sql_access=sql_access,
                service_name=service_name,
                instance_name=instance_name,
                database_names=database_names,
                container_name=str(item.get("container_name") or "").strip(),
                connection_info={
                    "host": ip,
                    "port": port,
                    "service_name": service_name,
                    "instance_name": instance_name,
                    "server_name": server_name,
                    "platform": platform,
                    "sid": (inventory_match or {}).get("sid"),
                    "sqlserver_driver": sqlserver_driver,
                    "sqlserver_major_version": sqlserver_major_version,
                    "major_version": major_version,
                    f"{item_db_type}_major_version": major_version,
                    "database": item.get("database") or service_name or db_name,
                    "database_names": database_names,
                },
                credential=credential,
                metrics_config=dict(metrics_cfg) if isinstance(metrics_cfg, dict) else {},
                report_policy=dict(item.get("report_policy") or {}) if isinstance(item.get("report_policy"), dict) else {},
            )
        )
    return targets


def resolve_metric_target(
    *,
    target_ip: str | None = None,
    target_id: str | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    inventory_path: Path | None = None,
    db_type: str | None = None,
) -> MetricTarget:
    normalized_target_id = str(target_id or "").strip()
    normalized_target_ip = str(target_ip or "").strip()
    if not normalized_target_id and not normalized_target_ip:
        raise ValueError("target lookup requires --target-ip or --target-id.")

    targets = load_metric_targets(data_dir=data_dir, inventory_path=inventory_path, db_type=db_type)
    matches = [
        target
        for target in targets
        if (not normalized_target_id or target.target_id == normalized_target_id)
        and (not normalized_target_ip or target.ip == normalized_target_ip)
    ]
    if not matches:
        criteria = _target_lookup_criteria(target_ip=normalized_target_ip, target_id=normalized_target_id)
        raise ValueError(f"No matching metrics target found for {criteria}.")
    if len(matches) > 1:
        criteria = _target_lookup_criteria(target_ip=normalized_target_ip, target_id=normalized_target_id)
        target_ids = ", ".join(sorted(target.target_id for target in matches))
        raise ValueError(f"Ambiguous metrics target for {criteria}; matched target_ids: {target_ids}.")
    return matches[0]


def _target_lookup_criteria(*, target_ip: str, target_id: str) -> str:
    parts = []
    if target_ip:
        parts.append(f"target_ip={target_ip}")
    if target_id:
        parts.append(f"target_id={target_id}")
    return ", ".join(parts) or "<empty>"


def _find_inventory_by_instance(item: dict[str, Any], inventory: list[dict[str, Any]]) -> dict[str, Any] | None:
    ip = str(item.get("ip", ""))
    db_type = str(item.get("db_type", "")).lower()
    item_service_name = str(item.get("service_name", "")).lower()
    instance_name = str(item.get("instance_name", "")).lower()
    candidates: list[dict[str, Any]] = []
    for server in inventory:
        if str(server.get("ip", "")) != ip:
            continue
        for database in server.get("databases", []) or []:
            if str(database.get("db_type", "")).lower() != db_type:
                continue
            inv_instance = str(database.get("instance_name") or database.get("sid") or "").lower()
            inventory_service_name = str(database.get("service_name", "")).lower()
            if instance_name and inv_instance and instance_name != inv_instance:
                continue
            resolved = dict(database)
            resolved["server_id"] = server.get("server_id")
            resolved["ip"] = server.get("ip")
            candidates.append(resolved)
            if not item_service_name or item_service_name == inventory_service_name:
                return resolved
    if len(candidates) == 1:
        return candidates[0]
    return None


def _find_credential(
    groups: list[dict[str, Any]],
    *,
    server_id: str,
    db_type: str,
    service_name: str,
    instance_name: str,
    credential_name: str = "",
) -> dict[str, Any] | None:
    """The target's credential, or ``None`` when it cannot be resolved.

    Selection is :func:`db_ops.common.data_sources.find_database_credential` — shared with every
    other app, and it no longer guesses: metrics used to fall back to whichever entry carried
    role DBA/SYSDBA (or simply the first one) when an instance named no credential, which is how
    a config omission turned into a silent connection as an admin login. A target that resolves
    to nothing keeps ``credential=None`` so the collector reports it per target — one
    misconfigured instance must not abort the whole collection run.
    """
    try:
        return data_sources.find_database_credential(
            groups,
            server_id=server_id,
            credential_name=credential_name,
            db_type=db_type,
            service_name=service_name,
            instance_name=instance_name,
        )
    except data_sources.CredentialNotFound:
        return None


SUPPORTED_SQL_ACCESS_METHODS = sql_access.SUPPORTED_SQL_ACCESS_METHODS


def _resolve_sql_access(item: dict[str, Any]) -> dict[str, Any]:
    """Per-target transport for SQL-collector metrics. Default ``direct`` (connect to the DB);
    ``api``/``subprocess`` run the SQL through the legacy Oracle tool (see
    db_ops/common/oracle_bridge.py). This overrides the metric's default ``collector_type``
    execution for this server only.

    The parsing itself belongs to ``common`` now that ``sql_run`` reads the same block — metrics
    only supplies the label that names the offending instance in an error.
    """
    return sql_access.normalize_sql_access(
        item.get("sql_access"), label=f"db instance {item.get('ip') or item.get('target_id')}",
    )


def _database_names(item: dict[str, Any], inventory_match: dict[str, Any] | None) -> list[str]:
    names = item.get("database_names")
    if names is None:
        names = (inventory_match or {}).get("database_names")
    if not isinstance(names, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        text = str(name or "").strip()
        if not text or text == "<not-provided>" or text.lower() in seen:
            continue
        seen.add(text.lower())
        result.append(text)
    return result


