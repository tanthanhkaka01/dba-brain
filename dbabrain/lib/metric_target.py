"""A target metric collection runs against - one database instance or host, resolved from config.

``lib``'s since 0.24.0, with the loader that builds it (``lib.data_sources.collection_targets``):
``common.cli check-credentials`` checks every configured target resolves to a login, and ``common``
may import only ``lib`` (rules R04). ``metrics.models`` imports it back, so it is one class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MetricTarget:
    target_id: str
    server_id: str
    ip: str
    db_type: str
    db_name: str
    credential_name: str
    port: int | None = None
    platform: str = ""
    cmd_access: dict[str, Any] = field(default_factory=dict)
    cmd_credential: dict[str, Any] | None = None
    # Per-target transport override for SQL-collector metrics: {"method": "direct"|"api", ...}.
    # "direct" connects to the DB; "api" runs the SQL through the legacy Oracle bridge.
    sql_access: dict[str, Any] = field(default_factory=lambda: {"method": "direct"})
    service_name: str = ""
    instance_name: str = ""
    database_names: list[str] = field(default_factory=list)
    # Name of the Docker container backing this target (a DB-in-Docker instance). Set for
    # docker-collector metrics; empty for non-containerized targets (they skip docker metrics).
    container_name: str = ""
    connection_info: dict[str, Any] = field(default_factory=dict)
    credential: dict[str, Any] | None = None
    metrics_config: dict[str, Any] = field(default_factory=dict)
    report_policy: dict[str, Any] = field(default_factory=dict)
