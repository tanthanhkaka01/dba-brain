"""SLA policy models judge backup RPO freshness and restore drill RTO compliance.

Operator decision (2026-10-15): backups are graded against data/backup_policy.json
in the SLA app rather than inside metric collectors, which only report raw ages.
Under the older ALL_BACKUP_HEALTH_7D policy, BACKUP_LAST_RESULT emitted OK at any
age, so a 133-day-old log backup appeared compliant.

Similarly, backup_restore_history records every drill run, but the SLA app never
judged whether databases had been proven restorable within target RTO.

The backup_rpo model evaluates newest-cycle backup ages per target against
data/backup_policy.json, distinguishing breaches from warnings and unconfigured
states. The restore_drill model evaluates backup_restore_history over the policy
window at fleet scope, enforcing both drill freshness against
data/restore_drill_policy.json and duration against rto_target_seconds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbabrain.db.backup_restore_history import BackupRestoreHistory
from dbabrain.metrics.models import MetricResult
from dbabrain.metrics.storage import MetricStore
from dbabrain.sla.compliance import validate_sla_policies
from dbabrain.sla.models import SlaPolicy
from dbabrain.sla.policies import parse_sla_policy


def _inventory(tmp_path: Path, *targets: tuple[str, str]) -> Path:
    """Enabled-target inventory so data_dir resolves known targets."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    instances = [
        {
            "server_id": server_id,
            "db_type": db_type,
            "service_name": "app_db",
            "ip": "192.0.2.10",
            "enabled": True,
            "metrics": {"enabled": True},
        }
        for server_id, db_type in targets
    ]
    (data_dir / "db_instances.json").write_text(
        json.dumps({"db_instances": instances}),
        encoding="utf-8",
    )
    return data_dir


def _write_backup_policy(data_dir: Path, policy_dict: dict) -> None:
    (data_dir / "backup_policy.json").write_text(
        json.dumps({"backup_policy": policy_dict}),
        encoding="utf-8",
    )


def _write_restore_drill_policy(data_dir: Path, max_age_hours: int = 168) -> None:
    (data_dir / "restore_drill_policy.json").write_text(
        json.dumps({"restore_drill_policy": {"defaults": {"max_age_hours": max_age_hours}}}),
        encoding="utf-8",
    )


def _backup_metric_result(
    *,
    target_id: str = "ACME-192-0-2-10/sqlserver/app_db",
    server_id: str = "ACME-192-0-2-10",
    db_name: str = "app_db",
    backup_type: str = "FULL",
    recovery_model: str = "FULL",
    age_hours: float = 10.0,
    finish_date: str = "2026-05-28T00:00:00",
    collected_at: str = "2026-05-28T04:00:00Z",
) -> MetricResult:
    msg = (
        f"database={db_name}, recovery_model={recovery_model}, "
        f"backup_type={backup_type}, backup_finish_date={finish_date}"
    )
    return MetricResult(
        target_id=target_id,
        server_id=server_id,
        ip="192.0.2.10",
        db_type="sqlserver",
        db_name=db_name,
        metric_code="BACKUP_LAST_RESULT",
        metric_item=f"{db_name} / {backup_type}",
        metric_value=str(age_hours),
        metric_unit="hours",
        status="OK",
        importance=5,
        message=msg,
        collected_at=collected_at,
    )


def test_a_log_backup_older_than_the_policy_is_a_breach(tmp_path):
    data_dir = _inventory(tmp_path, ("ACME-192-0-2-10", "sqlserver"))
    _write_backup_policy(
        data_dir,
        {
            "defaults": {
                "types": {
                    "FULL": {"required": True, "warn_hours": 24, "critical_hours": 48},
                    "LOG": {"required_for_recovery_models": ["FULL"], "warn_hours": 2, "critical_hours": 4},
                }
            }
        },
    )

    store = MetricStore(tmp_path / "dbabrain.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            _backup_metric_result(backup_type="FULL", age_hours=10.0, finish_date="2026-05-27T18:00:00"),
            _backup_metric_result(backup_type="LOG", age_hours=52.0, finish_date="2026-05-25T20:00:00"),
        ],
    )

    policy = SlaPolicy(
        policy_id="ALL_BACKUP_RPO_24H",
        name="Backup RPO compliance",
        policy_model="backup_rpo",
        comparison_operator="<=",
        target_value=0.0,
        window_hours=24,
        target_ids=("ACME-192-0-2-10/sqlserver/app_db",),
        metric_codes=("BACKUP_LAST_RESULT",),
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "dbabrain.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=data_dir,
    )

    assert summary.status == "FAILED"
    assert len(summary.results) == 1
    result = summary.results[0]
    assert result.status == "FAILED"
    assert result.actual_value == 1.0
    assert result.compliant is False
    assert "app_db LOG" in result.reason
    assert "limit 4" in result.reason


def test_a_required_backup_type_that_was_never_backed_up_is_a_breach(tmp_path):
    data_dir = _inventory(tmp_path, ("ACME-192-0-2-10", "sqlserver"))
    _write_backup_policy(
        data_dir,
        {
            "defaults": {
                "types": {
                    "FULL": {"required": True, "warn_hours": 24, "critical_hours": 48},
                    "LOG": {"required_for_recovery_models": ["FULL"], "warn_hours": 2, "critical_hours": 4},
                }
            }
        },
    )

    store = MetricStore(tmp_path / "dbabrain.sqlite")
    # FULL backup is present, but LOG backup has never been recorded for this FULL recovery DB.
    store.insert_results(
        run_id=1,
        results=[
            _backup_metric_result(backup_type="FULL", age_hours=10.0, finish_date="2026-05-27T18:00:00"),
        ],
    )

    policy = SlaPolicy(
        policy_id="ALL_BACKUP_RPO_24H",
        name="Backup RPO compliance",
        policy_model="backup_rpo",
        comparison_operator="<=",
        target_value=0.0,
        window_hours=24,
        target_ids=("ACME-192-0-2-10/sqlserver/app_db",),
        metric_codes=("BACKUP_LAST_RESULT",),
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "dbabrain.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=data_dir,
    )

    assert summary.status == "FAILED"
    result = summary.results[0]
    assert result.status == "FAILED"
    assert result.actual_value == 1.0
    assert "app_db LOG: required and never backed up" in result.reason


def test_the_counts_are_of_databases_and_never_go_below_zero(tmp_path):
    # 0.30.0's soak: a server whose databases each missed two or three backup types read
    # total 21, good -14, bad 35 - breaches of (database, type) counted against the cycle's rows.
    data_dir = _inventory(tmp_path, ("ACME-192-0-2-10", "sqlserver"))
    _write_backup_policy(
        data_dir,
        {
            "defaults": {
                "types": {
                    "FULL": {"required": True, "warn_hours": 24, "critical_hours": 48},
                    "LOG": {"required_for_recovery_models": ["FULL"], "warn_hours": 2, "critical_hours": 4},
                }
            }
        },
    )
    store = MetricStore(tmp_path / "dbabrain.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            _backup_metric_result(db_name="app_db", backup_type="FULL", age_hours=10.0, finish_date="2026-05-27T18:00:00"),
            _backup_metric_result(db_name="app_db", backup_type="LOG", age_hours=1.0, finish_date="2026-05-28T03:00:00"),
            # hr_db breaches twice: its FULL is old and its LOG was never taken (no row at all).
            _backup_metric_result(db_name="hr_db", backup_type="FULL", age_hours=300.0, finish_date="2026-05-15T16:00:00"),
        ],
    )
    policy = SlaPolicy(
        policy_id="ALL_BACKUP_RPO_24H",
        name="Backup RPO compliance",
        policy_model="backup_rpo",
        comparison_operator="<=",
        target_value=0.0,
        window_hours=24,
        target_ids=("ACME-192-0-2-10/sqlserver/app_db",),
        metric_codes=("BACKUP_LAST_RESULT",),
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "dbabrain.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=data_dir,
    )

    result = summary.results[0]
    assert result.status == "FAILED"
    assert (result.total_count, result.good_count, result.bad_count) == (2, 1, 1)
    assert result.actual_value == 2.0
    assert "hr_db FULL" in result.reason and "hr_db LOG: required and never backed up" in result.reason


def test_a_data_backup_past_its_objective_is_a_breach_though_full_alone_is_within_its_own(tmp_path):
    # FULL is allowed 240 h because a weekly FULL is a normal plan; the data backup - the newest of
    # FULL or DIFF - is held to 48 h. The SLA read the types only until 2026-10-19, so a database
    # with a 96 h FULL and no DIFF passed the objective the policy exists for.
    data_dir = _inventory(tmp_path, ("ACME-192-0-2-10", "sqlserver"))
    _write_backup_policy(
        data_dir,
        {
            "defaults": {
                "data_backup": {"of_types": ["FULL", "DIFF"], "required": True, "warn_hours": 30, "critical_hours": 48},
                "types": {"FULL": {"required": True, "warn_hours": 192, "critical_hours": 240}},
            }
        },
    )
    store = MetricStore(tmp_path / "dbabrain.sqlite")
    store.insert_results(run_id=1, results=[
        _backup_metric_result(backup_type="FULL", age_hours=96.0, finish_date="2026-05-24T04:00:00"),
    ])
    policy = SlaPolicy(
        policy_id="ALL_BACKUP_RPO_24H",
        name="Backup RPO compliance",
        policy_model="backup_rpo",
        comparison_operator="<=",
        target_value=0.0,
        window_hours=24,
        target_ids=("ACME-192-0-2-10/sqlserver/app_db",),
        metric_codes=("BACKUP_LAST_RESULT",),
    )

    result = validate_sla_policies(
        sqlite_path=tmp_path / "dbabrain.sqlite", policies=[policy],
        window_end="2026-05-28T04:00:00Z", data_dir=data_dir,
    ).results[0]

    assert result.status == "FAILED"
    assert "app_db data backup (FULL or DIFF)" in result.reason and "limit 48" in result.reason
    assert (result.total_count, result.good_count, result.bad_count) == (1, 0, 1)


def test_a_backup_older_than_the_warning_threshold_only_is_at_risk(tmp_path):
    data_dir = _inventory(tmp_path, ("ACME-192-0-2-10", "sqlserver"))
    _write_backup_policy(
        data_dir,
        {
            "defaults": {
                "types": {
                    "FULL": {"required": True, "warn_hours": 24, "critical_hours": 48},
                }
            }
        },
    )

    store = MetricStore(tmp_path / "dbabrain.sqlite")
    # FULL age 30h is between warn_hours (24) and critical_hours (48) -> warning only.
    store.insert_results(
        run_id=1,
        results=[
            _backup_metric_result(backup_type="FULL", recovery_model="SIMPLE", age_hours=30.0, finish_date="2026-05-26T22:00:00"),
        ],
    )

    policy = SlaPolicy(
        policy_id="ALL_BACKUP_RPO_24H",
        name="Backup RPO compliance",
        policy_model="backup_rpo",
        comparison_operator="<=",
        target_value=0.0,
        window_hours=24,
        target_ids=("ACME-192-0-2-10/sqlserver/app_db",),
        metric_codes=("BACKUP_LAST_RESULT",),
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "dbabrain.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=data_dir,
    )

    assert summary.status == "PASSED"
    result = summary.results[0]
    assert result.status == "AT_RISK"
    assert result.actual_value == 0.0
    assert result.compliant is True
    assert "warn limit 24" in result.reason


def test_an_absent_or_unconfigured_backup_policy_file_is_not_passed(tmp_path):
    data_dir = _inventory(tmp_path, ("ACME-192-0-2-10", "sqlserver"))
    # No backup_policy.json is written to data_dir.

    store = MetricStore(tmp_path / "dbabrain.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            _backup_metric_result(backup_type="FULL", age_hours=5.0),
        ],
    )

    policy = SlaPolicy(
        policy_id="ALL_BACKUP_RPO_24H",
        name="Backup RPO compliance",
        policy_model="backup_rpo",
        comparison_operator="<=",
        target_value=0.0,
        window_hours=24,
        target_ids=("ACME-192-0-2-10/sqlserver/app_db",),
        metric_codes=("BACKUP_LAST_RESULT",),
        required=True,
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "dbabrain.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=data_dir,
    )

    assert summary.status == "FAILED"
    result = summary.results[0]
    assert result.status == "NOT_CONFIGURED"
    assert result.status != "PASSED"
    assert "no backup policy configured" in result.reason


def test_a_restore_drill_older_than_the_drill_policy_is_a_breach(tmp_path):
    data_dir = _inventory(tmp_path, ("ACME-192-0-2-10", "sqlserver"))
    _write_restore_drill_policy(data_dir, max_age_hours=168)

    history = BackupRestoreHistory(tmp_path / "dbabrain.sqlite")
    history.initialize()
    # 240 hours ago (> 168h limit)
    with history.store.connect() as conn:
        conn.execute(
            """
            INSERT INTO backup_restore_history
            (database_name, backup_file, restore_start, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("app_db", "app_db.bak", "2026-05-18T04:00:00Z", 120.0, "SUCCESS"),
        )

    policy = SlaPolicy(
        policy_id="RESTORE_DRILL_RTO_30D",
        name="Restore drill RTO compliance",
        policy_model="restore_drill",
        comparison_operator="<=",
        target_value=0.0,
        window_hours=720,
        rto_target_seconds=7200,
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "dbabrain.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=data_dir,
    )

    assert summary.status == "FAILED"
    result = summary.results[0]
    assert result.status == "FAILED"
    assert result.actual_value == 1.0
    assert "app_db" in result.reason
    assert "7200s" in result.reason
    assert "120s" in result.reason


def test_a_newer_failed_restore_attempt_outranks_an_older_success(tmp_path):
    data_dir = _inventory(tmp_path, ("ACME-192-0-2-10", "sqlserver"))
    _write_restore_drill_policy(data_dir, max_age_hours=168)

    history = BackupRestoreHistory(tmp_path / "dbabrain.sqlite")
    history.initialize()
    with history.store.connect() as conn:
        # Older success 48h ago
        conn.execute(
            """
            INSERT INTO backup_restore_history
            (database_name, backup_file, restore_start, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("app_db", "app_db.bak", "2026-05-26T04:00:00Z", 120.0, "SUCCESS"),
        )
        # Newer attempt failed 24h ago
        conn.execute(
            """
            INSERT INTO backup_restore_history
            (database_name, backup_file, restore_start, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("app_db", "app_db.bak", "2026-05-27T04:00:00Z", None, "FAILED"),
        )

    policy = SlaPolicy(
        policy_id="RESTORE_DRILL_RTO_30D",
        name="Restore drill RTO compliance",
        policy_model="restore_drill",
        comparison_operator="<=",
        target_value=0.0,
        window_hours=720,
        rto_target_seconds=7200,
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "dbabrain.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=data_dir,
    )

    assert summary.status == "FAILED"
    result = summary.results[0]
    assert result.status == "FAILED"
    assert result.actual_value == 1.0
    assert "FAILED" in result.reason


def test_a_successful_restore_drill_slower_than_rto_target_seconds_is_a_breach(tmp_path):
    data_dir = _inventory(tmp_path, ("ACME-192-0-2-10", "sqlserver"))
    _write_restore_drill_policy(data_dir, max_age_hours=168)

    history = BackupRestoreHistory(tmp_path / "dbabrain.sqlite")
    history.initialize()
    # Fresh success 12h ago, but took 8500s against target of 7200s
    with history.store.connect() as conn:
        conn.execute(
            """
            INSERT INTO backup_restore_history
            (database_name, backup_file, restore_start, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("app_db", "app_db.bak", "2026-05-27T16:00:00Z", 8500.0, "SUCCESS"),
        )

    policy = SlaPolicy(
        policy_id="RESTORE_DRILL_RTO_30D",
        name="Restore drill RTO compliance",
        policy_model="restore_drill",
        comparison_operator="<=",
        target_value=0.0,
        window_hours=720,
        rto_target_seconds=7200,
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "dbabrain.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=data_dir,
    )

    assert summary.status == "FAILED"
    result = summary.results[0]
    assert result.status == "FAILED"
    assert result.actual_value == 1.0
    assert "8500s" in result.reason
    assert "7200s" in result.reason


def test_a_restore_drill_policy_without_rto_target_seconds_is_refused():
    with pytest.raises(ValueError, match="requires rto_target_seconds"):
        parse_sla_policy(
            {
                "policy_id": "RESTORE_DRILL_RTO_30D",
                "policy_model": "restore_drill",
                "window_hours": 720,
                "comparison_operator": "<=",
                "target_value": 0,
            }
        )


def test_the_existing_three_policy_models_behave_identically_to_their_established_semantics(tmp_path):
    data_dir = _inventory(tmp_path, ("ACME-192-0-2-10", "sqlserver"))
    store = MetricStore(tmp_path / "dbabrain.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id="ACME-192-0-2-10/sqlserver/app_db",
                server_id="ACME-192-0-2-10",
                ip="192.0.2.10",
                db_type="sqlserver",
                db_name="app_db",
                metric_code="INSTANCE_STATUS",
                metric_item="service",
                metric_value="1",
                metric_unit="",
                status="OK",
                importance=5,
                message=None,
                collected_at="2026-05-28T01:00:00Z",
            ),
            MetricResult(
                target_id="ACME-192-0-2-10/sqlserver/app_db",
                server_id="ACME-192-0-2-10",
                ip="192.0.2.10",
                db_type="sqlserver",
                db_name="app_db",
                metric_code="INSTANCE_STATUS",
                metric_item="service",
                metric_value="1",
                metric_unit="",
                status="ERROR",
                importance=5,
                message=None,
                collected_at="2026-05-28T02:00:00Z",
            ),
            MetricResult(
                target_id="ACME-192-0-2-10/sqlserver/app_db",
                server_id="ACME-192-0-2-10",
                ip="192.0.2.10",
                db_type="sqlserver",
                db_name="app_db",
                metric_code="INDEX_FRAGMENTATION",
                metric_item="tbl_orders.idx_orders_date",
                metric_value="45",
                metric_unit="percent",
                status="WARNING",
                importance=5,
                message=None,
                collected_at="2026-05-28T03:00:00Z",
            ),
        ],
    )

    # 1. time_slo: 1 of 2 INSTANCE_STATUS good -> 50%
    time_policy = SlaPolicy(
        policy_id="TIME_AVAILABILITY",
        name="Availability SLO",
        policy_model="time_slo",
        target_ids=("ACME-192-0-2-10/sqlserver/app_db",),
        metric_codes=("INSTANCE_STATUS",),
        objective_percent=99.0,
        window_hours=24,
    )
    # 2. current_state: latest cycle of INSTANCE_STATUS is ERROR -> not compliant (0.0)
    state_policy = SlaPolicy(
        policy_id="CURRENT_SERVICE_STATE",
        name="Current state SLO",
        policy_model="current_state",
        target_ids=("ACME-192-0-2-10/sqlserver/app_db",),
        metric_codes=("INSTANCE_STATUS",),
        objective_percent=99.0,
        window_hours=24,
    )
    # 3. finding_inventory: counts distinct bad items in latest cycle (1 fragmented index)
    inventory_policy = SlaPolicy(
        policy_id="INDEX_FRAGMENTATION_BACKLOG",
        name="Index backlog SLO",
        policy_model="finding_inventory",
        target_ids=("ACME-192-0-2-10/sqlserver/app_db",),
        metric_codes=("INDEX_FRAGMENTATION",),
        comparison_operator="<=",
        target_value=0.0,
        window_hours=24,
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "dbabrain.sqlite",
        policies=[time_policy, state_policy, inventory_policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=data_dir,
    )

    results = {r.policy_id: r for r in summary.results}

    assert results["TIME_AVAILABILITY"].actual_percent == 50.0
    assert results["TIME_AVAILABILITY"].status == "FAILED"

    assert results["CURRENT_SERVICE_STATE"].actual_value == 0.0
    assert results["CURRENT_SERVICE_STATE"].status == "FAILED"

    assert results["INDEX_FRAGMENTATION_BACKLOG"].actual_value == 1.0
    assert results["INDEX_FRAGMENTATION_BACKLOG"].status == "FAILED"
