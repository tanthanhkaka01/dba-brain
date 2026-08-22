from db_ops.metrics.models import MetricResult
from db_ops.metrics.storage import MetricStore
from db_ops.sla.compliance import validate_sla_policies
from db_ops.sla.models import SlaPolicy
from db_ops.sla.policies import parse_sla_policy


def test_validate_sla_policy_passes_when_actual_meets_objective(tmp_path):
    store = MetricStore(tmp_path / "db_ops.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            _metric_result(status="OK", collected_at="2026-05-28T00:00:00Z"),
            _metric_result(status="WARNING", collected_at="2026-05-28T01:00:00Z"),
            _metric_result(status="OK", collected_at="2026-05-28T02:00:00Z"),
            _metric_result(status="OK", collected_at="2026-05-28T03:00:00Z"),
        ],
    )
    policy = SlaPolicy(
        policy_id="BACKUP_75",
        name="Backup SLO",
        target_ids=("server/sqlserver/db",),
        metric_codes=("BACKUP_AGE",),
        objective_percent=75.0,
        window_hours=24,
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "db_ops.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
    )

    assert summary.status == "PASSED"
    assert summary.results[0].actual_percent == 75.0
    assert summary.results[0].failures_by_status == {"WARNING": 1}


def test_validate_sla_policy_fails_when_actual_is_below_objective(tmp_path):
    store = MetricStore(tmp_path / "db_ops.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            _metric_result(status="OK", collected_at="2026-05-28T00:00:00Z"),
            _metric_result(status="ERROR", collected_at="2026-05-28T01:00:00Z"),
        ],
    )
    policy = SlaPolicy(
        policy_id="BACKUP_99",
        name="Backup SLO",
        target_ids=("server/sqlserver/db",),
        metric_codes=("BACKUP_AGE",),
        objective_percent=99.0,
        window_hours=24,
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "db_ops.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
    )

    assert summary.status == "FAILED"
    assert summary.failed_count == 1
    assert summary.results[0].actual_percent == 50.0


def test_validate_sla_policy_reports_no_data(tmp_path):
    policy = SlaPolicy(
        policy_id="NO_DATA",
        name="No data SLO",
        target_ids=("server/sqlserver/db",),
        metric_codes=("BACKUP_AGE",),
        objective_percent=99.0,
        window_hours=24,
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "db_ops.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
    )

    assert summary.status == "FAILED"
    assert summary.no_data_count == 1
    assert summary.results[0].status == "NO_DATA"


def test_parse_sla_policy_accepts_string_lists():
    policy = parse_sla_policy(
        {
            "policy_id": "P1",
            "name": "Policy",
            "target_ids": "target-1,target-2",
            "metric_codes": "M1,M2",
            "objective_percent": 99.5,
        }
    )

    assert policy.target_ids == ("target-1", "target-2")
    assert policy.metric_codes == ("M1", "M2")
    assert policy.objective_percent == 99.5


def _metric_result(*, status: str, collected_at: str) -> MetricResult:
    return MetricResult(
        target_id="server/sqlserver/db",
        server_id="server",
        ip="127.0.0.1",
        db_type="sqlserver",
        db_name="db",
        metric_code="BACKUP_AGE",
        metric_item="db",
        metric_value="1",
        metric_unit="hour",
        status=status,
        importance=5,
        message=None,
        collected_at=collected_at,
    )
