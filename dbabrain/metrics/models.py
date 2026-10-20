from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbabrain.lib import sql_text
from dbabrain.lib.time_window import TimeWindow
# Re-exported: MetricResult moved to common/metric_results.py when the store moved to common, because common
# cannot import an app. Importers of dbabrain.metrics.models keep working unchanged.
from dbabrain.db.metric_results import MetricResult


from dbabrain.lib.metric_target import MetricTarget  # noqa: E402,F401 - moved to lib (0.24.0)


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
    #: How often this variant runs, in seconds, when it is not how often the metric does. The
    #: metric's interval is set by its cheapest engine: LOCK_DEADLOCK_RECENT reads SQL Server's
    #: system_health ring buffer every 150 s, and Oracle's only source is v$diag_alert_ext - an
    #: external-table scan of the whole XML alert log, which LOG_RECENT_CRITICAL reads hourly.
    repeat_interval: int | None = None
    #: This variant's own ``sql_settings``, written over the metric's when this variant runs. A
    #: number that belongs to one engine's file is stated here: ``LOCK_TRANSACTION_HOLDERS``'s
    #: lock-scan limits are SQL Server's, and stated for the whole metric they were written into
    #: its PostgreSQL and Oracle files too, which do not declare them (1.149, 2026-10-15).
    sql_settings: dict[str, Any] = field(default_factory=dict)

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
    # Numbers this metric's SQL takes from configuration instead of from a literal in the file
    # (lib.sql_text.apply_sql_settings): the file declares each with its default, and a node
    # that states another runs with that one.
    sql_settings: dict[str, Any] = field(default_factory=dict)
    # Which facts about the *database* a cmd collector is handed beside DBABRAIN_TARGET_HOST
    # (``DBABRAIN_DB_TYPE``, ``DBABRAIN_DB_PORT``, ``DBABRAIN_DB_CONTAINER``). Declared per
    # metric rather than given to every script: OS_CPU_COUNTERS has to find the engine's process
    # on the host, and no other OS collector has any use for which database is on it.
    target_env: tuple[str, ...] = ()
    # A docker metric that, on a target with no ``container_name``, runs its script on the host
    # through ``cmd_access`` instead of being skipped. POSTGRES_SERVER_LOG reads ``docker logs`` for
    # a container and the ``log_directory`` files for an install on the host - one metric, the
    # source per target - and a host install is exactly the target a docker metric skipped.
    host_fallback: bool = False
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
    # Batches skipped because another pass held the category's file claim lock.
    skipped_claimed_count: int = 0
