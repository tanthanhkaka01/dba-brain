from pathlib import Path
import sys
import types

from db_ops.metrics.collector import _apply_postgresql_collection_policy, _apply_threshold_override
from db_ops.metrics.models import MetricDefinition, MetricResult, MetricTarget
from db_ops.metrics.storage import MetricStore
from db_ops.sla.compliance import validate_sla_policies
from db_ops.sla.models import SlaPolicy
from db_ops.sla.policies import parse_sla_policy
from db_ops.lib.paths import resolve_tool_path


def _target(expected: int) -> MetricTarget:
    return MetricTarget(
        target_id="lab/postgresql/pg1", server_id="lab", ip="127.0.0.1",
        db_type="postgresql", db_name="postgres", credential_name="fake",
        metrics_config={"expected_replica_count": expected},
    )


def _replication_definition() -> MetricDefinition:
    return MetricDefinition("POSTGRES_REPLICATION", "postgresql", "replication", 5, True)


def test_standalone_primary_with_zero_expected_replicas_is_healthy():
    rows = [{"metric_item": "replication", "metric_value": "0", "metric_unit": "replicas",
             "status": "WARNING", "message": "Primary has no connected streaming replicas."}]
    result = _apply_postgresql_collection_policy(metric=_replication_definition(), target=_target(0), rows=rows)
    assert result[0]["status"] == "OK"
    assert "expected_replicas=0" in result[0]["message"]


def test_primary_missing_expected_replicas_is_critical():
    rows = [{"metric_item": "replica1", "metric_value": "streaming", "metric_unit": None,
             "status": "OK", "message": "role=primary, state=streaming"}]
    result = _apply_postgresql_collection_policy(metric=_replication_definition(), target=_target(2), rows=rows)
    assert result[0]["status"] == "CRITICAL"
    assert "expected_replicas=2" in result[0]["message"]


def test_target_threshold_override_is_data_driven():
    target = _target(0)
    object.__setattr__(target, "metrics_config", {"metric_overrides": {
        "POSTGRES_REPLICATION_SLOTS": {"warning_threshold": 100, "critical_threshold": 200}
    }})
    metric = MetricDefinition("POSTGRES_REPLICATION_SLOTS", "postgresql", "replication", 5, True)
    status, message = _apply_threshold_override(
        metric=metric, target=target, metric_value="250", status="OK", message="slot"
    )
    assert status == "CRITICAL"
    assert "threshold override" in message


def test_postgresql_sql_is_read_only_bounded_and_secret_free():
    root = resolve_tool_path("assets/metrics/postgresql")
    files = sorted(root.glob("*.sql"))
    assert len(files) >= 15
    text = "\n".join(path.read_text(encoding="utf-8").lower() for path in files)
    for forbidden in ("pg_switch_wal(", "create database", "drop database", "truncate ", " password="):
        assert forbidden not in text
    assert "limit 20" in text
    assert "pg_is_in_recovery()" in text


def test_postgresql_statement_timeout_is_set(monkeypatch):
    calls = []

    class Cursor:
        def execute(self, sql, params=None):
            calls.append((sql, params))

        def close(self):
            calls.append(("close", None))

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            calls.append(("connection_close", None))

    dbapi = types.SimpleNamespace(connect=lambda **_kwargs: Connection())
    package = types.ModuleType("pg8000")
    package.dbapi = dbapi
    monkeypatch.setitem(sys.modules, "pg8000", package)
    monkeypatch.setattr("db_ops.metrics.executor.execute_cursor_batches", lambda *_args, **_kwargs: {"result_sets": []})
    from db_ops.metrics import executor

    # The per-engine connect moved to db_ops.common.db_connect, so the metrics app has one
    # execution path instead of four. The statement_timeout guarantee has to survive that move:
    # it is what bounds a catalog query when the socket stays healthy but a relation is locked.
    target = _target(0)
    object.__setattr__(target, "credential", {"username": "monitor"})
    executor._execute(target=target, sql_text="select 1", password="not-logged",
                      sql_timeout_seconds=7)
    # Inlined, not bound. pg8000's paramstyle is a module-level global with no per-connection
    # override, and the runtime store pins it to 'qmark' for its own '?' placeholders - so a '%s'
    # here failed with `syntax error at or near "%"` on every PostgreSQL target once the store and
    # the collector shared a process. An int() cannot be an injection.
    assert ("SELECT set_config('statement_timeout', '7000', false)", None) in calls
    assert not any("%s" in str(sql) for sql, _ in calls), "no format-style placeholder may remain"


def test_numeric_sla_and_coverage(tmp_path):
    store = MetricStore(tmp_path / "db.sqlite")
    store.insert_results(run_id=1, results=[
        _metric("10", "2026-07-10T00:00:00Z"), _metric("20", "2026-07-10T00:05:00Z"),
        _metric("30", "2026-07-10T00:10:00Z"),
    ])
    policy = SlaPolicy(
        policy_id="PG_LAG", name="Replay lag", target_ids=("lab/postgresql/pg1",),
        metric_codes=("POSTGRES_REPLAY_LAG",), objective_percent=99, window_hours=1,
        sli_code="REPLICATION_REPLAY_LAG", aggregation="maximum", comparison_operator="<=",
        target_value=30, unit="seconds", minimum_sample_count=3,
        expected_collection_interval_seconds=300, freshness_threshold_seconds=600,
    )
    summary = validate_sla_policies(sqlite_path=tmp_path / "db.sqlite", policies=[policy],
                                    window_end="2026-07-10T00:15:00Z")
    result = summary.results[0]
    assert result.actual_value == 30
    assert result.compliant is True
    assert result.coverage_percent == 25.0
    assert result.data_quality_status == "OK"


def test_maintenance_samples_are_excluded(tmp_path):
    store = MetricStore(tmp_path / "db.sqlite")
    store.insert_results(run_id=1, results=[
        _metric("1", "2026-07-10T00:00:00Z", status="CRITICAL"),
        _metric("1", "2026-07-10T00:10:00Z", status="OK"),
    ])
    policy = SlaPolicy(
        policy_id="AVAIL", name="Availability", target_ids=("lab/postgresql/pg1",),
        metric_codes=("POSTGRES_REPLAY_LAG",), objective_percent=100, window_hours=1,
        maintenance_windows=(("2026-07-09T23:59:00Z", "2026-07-10T00:01:00Z"),),
    )
    result = validate_sla_policies(sqlite_path=tmp_path / "db.sqlite", policies=[policy],
                                   window_end="2026-07-10T00:15:00Z").results[0]
    assert result.actual_percent == 100
    assert result.total_count == 1


def test_policy_validation_rejects_invalid_operator():
    try:
        parse_sla_policy({"policy_id": "X", "target_ids": ["x"], "metric_codes": ["M"], "operator": "~"})
    except ValueError as exc:
        assert "invalid comparison operator" in str(exc)
    else:
        raise AssertionError("invalid operator was accepted")


def test_stale_required_sli_fails_rollup(tmp_path):
    store = MetricStore(tmp_path / "db.sqlite")
    store.insert_results(run_id=1, results=[_metric("1", "2026-07-10T00:00:00Z")])
    policy = SlaPolicy(
        policy_id="STALE", name="Stale", target_ids=("lab/postgresql/pg1",),
        metric_codes=("POSTGRES_REPLAY_LAG",), objective_percent=99, window_hours=2,
        freshness_threshold_seconds=60,
    )
    summary = validate_sla_policies(sqlite_path=tmp_path / "db.sqlite", policies=[policy],
                                    window_end="2026-07-10T01:00:00Z")
    assert summary.status == "FAILED"
    assert summary.results[0].status == "STALE"


def test_optional_failed_sli_does_not_fail_required_rollup(tmp_path):
    store = MetricStore(tmp_path / "db.sqlite")
    store.insert_results(run_id=1, results=[_metric("1", "2026-07-10T00:00:00Z", status="CRITICAL")])
    policy = SlaPolicy(
        policy_id="OPTIONAL", name="Optional", target_ids=("lab/postgresql/pg1",),
        metric_codes=("POSTGRES_REPLAY_LAG",), objective_percent=100, window_hours=1,
        required=False,
    )
    summary = validate_sla_policies(sqlite_path=tmp_path / "db.sqlite", policies=[policy],
                                    window_end="2026-07-10T00:10:00Z")
    assert summary.results[0].status == "FAILED"
    assert summary.status == "PASSED"


def _metric(value: str, collected_at: str, status: str = "OK") -> MetricResult:
    return MetricResult(
        target_id="lab/postgresql/pg1", server_id="lab", ip="127.0.0.1", db_type="postgresql",
        db_name="postgres", metric_code="POSTGRES_REPLAY_LAG", metric_item="standby",
        metric_value=value, metric_unit="seconds", status=status, importance=5,
        message="safe", collected_at=collected_at,
    )
