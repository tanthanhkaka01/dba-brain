from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from db_ops.common import data_sources
from db_ops.lib.target_flags import is_alerts_enabled, is_metrics_enabled, is_reports_enabled, is_target_enabled
from db_ops.lib.paths import DEFAULT_DATA_DIR, TOOL_ROOT  # noqa: F401 - one definition, see that module




def resolve_config_metric_target(
    *,
    target_ip: str | None = None,
    target_id: str | None = None,
    server_id: str | None = None,
    db_type: str | None = None,
    port: int | None = None,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    require_metrics_enabled: bool = False,
    require_reports_enabled: bool = False,
    require_alerts_enabled: bool = False,
) -> SimpleNamespace | None:
    normalized_target_ip = str(target_ip or "").strip()
    normalized_target_id = str(target_id or "").strip()
    # server_id is the unique per-instance key: prefer it. Unlike an IP, it never needs a
    # db_type/port disambiguator (several instances can share one host IP, e.g. HA lab
    # containers on the same host, different ports).
    normalized_server_id = str(server_id or "").strip()
    # db_type/port are optional disambiguators when several targets share one IP.
    # "-"/"any" = no filter.
    normalized_db_type = str(db_type or "").strip().lower()
    if normalized_db_type in ("", "-", "any", "auto"):
        normalized_db_type = ""
    normalized_port = int(port) if port not in (None, "", 0, "0") else None
    if not normalized_target_ip and not normalized_target_id and not normalized_server_id:
        return None

    targets = load_config_metric_targets(
        data_dir=data_dir,
        require_metrics_enabled=require_metrics_enabled,
        require_reports_enabled=require_reports_enabled,
        require_alerts_enabled=require_alerts_enabled,
    )
    matches = [
        target
        for target in targets
        if (not normalized_server_id or str(getattr(target, "server_id", "")) == normalized_server_id)
        and (not normalized_target_ip or target.ip == normalized_target_ip)
        and (not normalized_target_id or target.target_id == normalized_target_id)
        and (not normalized_db_type or target.db_type == normalized_db_type)
        and (normalized_port is None or getattr(target, "port", None) == normalized_port)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        matched_ids = ", ".join(sorted(target.target_id for target in matches))
        raise ValueError(f"Ambiguous configured metric target; matched target_ids: {matched_ids}.")
    return matches[0]


def load_config_metric_targets(
    *,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    require_metrics_enabled: bool = False,
    require_reports_enabled: bool = False,
    require_alerts_enabled: bool = False,
) -> list[SimpleNamespace]:
    # Through data_sources, not around it: one module reads db_instances.json.
    instances = data_sources.load_db_instances(data_dir)
    targets: list[SimpleNamespace] = []
    for item in instances:
        if not is_target_enabled(item):
            continue
        metrics_enabled = is_metrics_enabled(item)
        reports_enabled = is_reports_enabled(item)
        alerts_enabled = is_alerts_enabled(item)
        if require_metrics_enabled and not metrics_enabled:
            continue
        if require_reports_enabled and not reports_enabled:
            continue
        if require_alerts_enabled and not alerts_enabled:
            continue
        # Empty/null db_type = host with no database (OS metrics only); it is still a
        # metric target, so it stays in the list.
        item_db_type = str(item.get("db_type") or "").strip().lower()
        server_id = str(item.get("server_id") or _server_id_from_ip(item))
        service_name = str(item.get("service_name") or "")
        instance_name = str(item.get("instance_name") or "")
        server_name = str(item.get("server_name") or "").strip()
        db_name = service_name or instance_name or server_name or server_id
        resolved_target_id = str(item.get("target_id") or f"{server_id}/{item_db_type}/{db_name}")
        port_value = item.get("port")
        targets.append(
            SimpleNamespace(
                target_id=resolved_target_id,
                server_id=server_id,
                ip=str(item.get("ip") or ""),
                port=int(port_value) if str(port_value or "").strip().isdigit() else None,
                db_type=item_db_type,
                db_name=db_name,
                service_name=service_name,
                instance_name=instance_name,
                connection_info={"server_name": server_name},
                metrics_enabled=metrics_enabled,
                reports_enabled=reports_enabled,
                alerts_enabled=alerts_enabled,
                raw_config=dict(item),
            )
        )
    return targets


def _server_id_from_ip(item: dict[str, Any]) -> str:
    ip = str(item.get("ip", "")).strip()
    return ip if not ip else f"{str(item.get('site', '') or 'DB')}-{ip.replace('.', '-')}"
