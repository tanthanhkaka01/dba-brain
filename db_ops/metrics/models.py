from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db_ops.lib import sql_text
from db_ops.lib.time_window import TimeWindow
# Re-exported: MetricResult moved to common/metric_results.py when the store moved to common, because common
# cannot import an app. Importers of db_ops.metrics.models keep working unchanged.
from db_ops.db.metric_results import MetricResult


@dataclass(frozen=True)
class MetricVariant:
    name: str
    db_type: str
    platform: str = ""
    supported: bool = True
    file: str = ""
    min_major_version: int | None = None
    max_major_version: int | None = None
    unsupported_reason: str = ""
    path: Path | None = None
    #: Run this SQL once per database on the target, instead of once per target.
    #:
    #: PostgreSQL only, and it exists because that engine cannot read another database's catalog
    #: from one connection. Every per-database PostgreSQL metric was therefore describing whichever
    #: single database the target happened to connect to — `postgres` for every target in this
    #: estate, which holds no user tables at all. Four metrics reported OK from an empty database
    #: - four metrics reporting OK from a database with nothing in it.
    #:
    #: SQL Server needs nothing here: it iterates databases inside the SQL with a cursor and USE.
    per_database: bool = False

    @property
    def sql_file(self) -> str:
        return self.file

    @property
    def sql_path(self) -> Path | None:
        return self.path


MetricSqlVariant = MetricVariant


@dataclass(frozen=True)
class MetricDefinition:
    metric_code: str
    db_type: str
    category: str
    default_importance: int
    active: bool
    collector_type: str = "sql"
    variants: list[MetricVariant] = field(default_factory=list)
    file: str = ""
    interval_seconds: int = 300
    retry_interval_seconds: int = 600
    default_timeout: int = 5
    empty_result_is_ok: bool = False
    # What a *failed collection* of this metric is worth — the severity of the row written when
    # the collector never got an answer, split by which half of the attempt broke:
    #   connection_error_severity — could not reach/log into the target at all
    #   execution_error_severity  — connected, but the query/script failed
    # Every collection failure used to be a flat WARNING, which is wrong in both directions: an
    # unreachable production instance (INSTANCE_STATUS) is an outage and must page, while a
    # capacity query failing on one host is not. The two phases are separate because they mean
    # different things — "the server is down" vs "this check is broken" — and an estate usually
    # wants a different answer for each. Defaults are WARNING so a catalog that predates these
    # fields keeps its old behavior.
    connection_error_severity: str = "WARNING"
    execution_error_severity: str = "WARNING"
    # Rows captured from the metric's first result set. Defaults to the shared
    # sql_text.MAX_RESULT_ROWS (100), which is right for a metric that reports exceptions:
    # a hundred blocking sessions is already an incident, and the rest are noise. An INVENTORY
    # metric is the opposite - MAINTENANCE_INDEX_USAGE has one row per index, ~29k on SALESDB, and
    # truncating it to 100 silently turns a complete picture into an arbitrary sample. So the cap
    # is per metric rather than global: raising it globally would change every metric at once.
    max_rows: int = 0
    report_policy: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    # Optional allowed collection window (from_hour/to_hour etc.). When set, the collector only
    # schedules this metric while the window is open (local time). Real-time metrics leave it
    # unset (bounds None => always open). Supports midnight-wrapping ranges (e.g. 22->6).
    schedule_window: "TimeWindow | None" = None

    @property
    def sql_file(self) -> str:
        return self.file

    @property
    def sql_path(self) -> Path | None:
        return self.path

    @property
    def sql_variants(self) -> list[MetricVariant]:
        return self.variants


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


@dataclass(frozen=True)
class CollectSummary:
    run_id: int | None
    target_count: int
    metric_count: int
    executed_count: int
    skipped_interval_count: int
    disabled_count: int
    result_count: int
    ok_count: int
    error_count: int
    warning_count: int
    critical_count: int
    no_data_count: int
    started_at: str
    finished_at: str
    duration_seconds: float
    message: str
    # Metrics left alone because the clock is outside their allowed window. Counted apart
    # from skipped_interval_count so "we did not run CHECKDB, it is 15:00" stays legible.
    skipped_window_count: int = 0
