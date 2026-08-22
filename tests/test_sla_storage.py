from db_ops.metrics.models import MetricResult
from db_ops.metrics.storage import MetricStore
from db_ops.sla.compliance import validate_sla_policies
from db_ops.sla.models import SlaPolicy
from db_ops.sla.policies import parse_sla_policy
from db_ops.sla.storage import SlaStore


def _metric_result(*, status: str, collected_at: str, target_id: str = "s/sqlserver/db", db_type: str = "sqlserver") -> MetricResult:
    # server_id is the first segment of the composite target_id, exactly as the collector writes
    # it. It used to be hard-coded "s" while the target_ids said s1/s2/o1 — harmless while the
    # SLA scoped on the db_type column, and wrong the moment it scoped on the identity that
    # actually joins these rows to the inventory.
    return MetricResult(
        target_id=target_id,
        server_id=target_id.split("/")[0],
        ip="127.0.0.1",
        db_type=db_type,
        db_name="db",
        metric_code="INSTANCE_STATUS",
        metric_item="db",
        metric_value="1",
        metric_unit="",
        status=status,
        importance=5,
        message=None,
        collected_at=collected_at,
    )


def _inventory(tmp_path, *targets):
    """Write the enabled-target inventory the evaluator now reads its population from.

    The population comes from the inventory rather than from whichever samples exist, so a test
    that does not supply one is measuring a synthetic store against the production estate. Each
    entry is `(server_id, db_type)`; db_name resolves from service_name, matching the composite
    `<server_id>/<db_type>/<db_name>` the metrics collector builds.
    """
    import json

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "db_instances.json").write_text(
        json.dumps({"db_instances": [
            {"server_id": server_id, "db_type": db_type, "service_name": "db",
             "enabled": True, "metrics": {"enabled": True}}
            for server_id, db_type in targets
        ]}),
        encoding="utf-8",
    )
    return data_dir


def test_db_type_scope_evaluates_each_instance_separately(tmp_path):
    store = MetricStore(tmp_path / "db_ops.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            # s1: 1 bad of 2 -> 50% ; s2: all good -> 100%
            _metric_result(status="OK", collected_at="2026-05-28T00:00:00Z", target_id="s1/sqlserver/db"),
            _metric_result(status="ERROR", collected_at="2026-05-28T00:30:00Z", target_id="s1/sqlserver/db"),
            _metric_result(status="OK", collected_at="2026-05-28T01:00:00Z", target_id="s2/sqlserver/db"),
            _metric_result(status="OK", collected_at="2026-05-28T02:00:00Z", target_id="s2/sqlserver/db"),
            # Oracle sample must be excluded by the sqlserver-scoped SLO.
            _metric_result(status="ERROR", collected_at="2026-05-28T02:30:00Z", target_id="o1/oracle/db", db_type="oracle"),
        ],
    )
    policy = SlaPolicy(
        policy_id="SS_AVAIL",
        name="SQL Server availability",
        metric_codes=("INSTANCE_STATUS",),
        objective_percent=99.0,
        db_types=("sqlserver",),
        window_hours=24,
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "db_ops.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=_inventory(tmp_path, ("s1", "sqlserver"), ("s2", "sqlserver"), ("o1", "oracle")),
    )

    # One result per ENABLED sqlserver instance; oracle excluded entirely.
    assert summary.result_count == 2
    by_target = {result.target_id: result for result in summary.results}
    assert set(by_target) == {"s1/sqlserver/db", "s2/sqlserver/db"}
    assert by_target["s1/sqlserver/db"].status == "FAILED"
    assert by_target["s1/sqlserver/db"].actual_percent == 50.0
    assert by_target["s2/sqlserver/db"].status == "PASSED"
    assert by_target["s2/sqlserver/db"].total_count == 2
    assert summary.status == "FAILED"  # one instance failing fails the run


def test_aggregate_flag_produces_single_fleet_result(tmp_path):
    store = MetricStore(tmp_path / "db_ops.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            _metric_result(status="OK", collected_at="2026-05-28T00:00:00Z", target_id="s1/sqlserver/db"),
            _metric_result(status="OK", collected_at="2026-05-28T01:00:00Z", target_id="s2/sqlserver/db"),
            _metric_result(status="OK", collected_at="2026-05-28T02:00:00Z", target_id="s2/sqlserver/db"),
        ],
    )
    policy = SlaPolicy(
        policy_id="SS_FLEET",
        name="SQL Server fleet availability",
        metric_codes=("INSTANCE_STATUS",),
        objective_percent=99.0,
        db_types=("sqlserver",),
        window_hours=24,
        aggregate=True,
    )
    summary = validate_sla_policies(
        sqlite_path=tmp_path / "db_ops.sqlite", policies=[policy],
        window_end="2026-05-28T04:00:00Z", data_dir=_inventory(tmp_path),
    )
    assert summary.result_count == 1
    assert summary.results[0].target_id == "*"
    assert summary.results[0].total_count == 3


def test_error_budget_at_risk_when_budget_nearly_exhausted(tmp_path):
    store = MetricStore(tmp_path / "db_ops.sqlite")
    # 96 good / 4 bad = 96% actual. Objective 95% -> budget 5%, bad 4% -> 80% consumed,
    # 20% remaining <= at_risk threshold 25% => AT_RISK (still meets objective).
    results = [_metric_result(status="OK", collected_at=f"2026-05-28T00:{m:02d}:00Z") for m in range(48)]
    results += [_metric_result(status="OK", collected_at=f"2026-05-28T01:{m:02d}:00Z") for m in range(48)]
    results += [_metric_result(status="ERROR", collected_at=f"2026-05-28T02:{m:02d}:00Z") for m in range(4)]
    store.insert_results(run_id=1, results=results)
    policy = SlaPolicy(
        policy_id="SS_BUDGET",
        name="Budget",
        metric_codes=("INSTANCE_STATUS",),
        objective_percent=95.0,
        db_types=("sqlserver",),
        window_hours=24,
        at_risk_budget_percent=25.0,
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "db_ops.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=_inventory(tmp_path, ("s", "sqlserver")),
    )

    result = summary.results[0]
    assert result.actual_percent == 96.0
    assert result.error_budget_percent == 5.0
    assert result.budget_consumed_percent == 80.0
    assert result.budget_remaining_percent == 20.0
    assert result.status == "AT_RISK"
    assert summary.at_risk_count == 1
    assert summary.status == "PASSED"  # AT_RISK still meets the objective


def test_save_summary_persists_run_and_results(tmp_path):
    sqlite_path = tmp_path / "db_ops.sqlite"
    store = MetricStore(sqlite_path)
    store.insert_results(
        run_id=1,
        results=[_metric_result(status="OK", collected_at="2026-05-28T00:00:00Z")],
    )
    policy = SlaPolicy(
        policy_id="SS_AVAIL",
        name="SQL Server availability",
        metric_codes=("INSTANCE_STATUS",),
        objective_percent=99.0,
        db_types=("sqlserver",),
        window_hours=24,
    )
    summary = validate_sla_policies(sqlite_path=sqlite_path, policies=[policy],
                                    window_end="2026-05-28T04:00:00Z",
                                    data_dir=_inventory(tmp_path, ("s", "sqlserver")))

    sla_store = SlaStore(sqlite_path)
    run_id = sla_store.save_summary(
        summary=summary,
        started_at="2026-05-28T04:00:00Z",
        finished_at="2026-05-28T04:00:01Z",
        message="test",
    )

    runs = sla_store.fetch_recent_runs(limit=5)
    assert len(runs) == 1
    assert runs[0]["sla_run_id"] == run_id
    assert runs[0]["passed_count"] == 1
    results = sla_store.fetch_run_results(sla_run_id=run_id)
    assert len(results) == 1
    assert results[0]["policy_id"] == "SS_AVAIL"
    assert results[0]["status"] == "PASSED"
    assert results[0]["scope"] == "db_types:sqlserver"


def test_parse_policy_requires_target_or_db_type():
    try:
        parse_sla_policy({"policy_id": "P1", "metric_codes": "M1", "objective_percent": 99.0})
    except ValueError as exc:
        assert "target_ids or db_types" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing scope")


def test_parse_policy_reads_db_types_and_at_risk():
    policy = parse_sla_policy(
        {
            "policy_id": "P1",
            "db_types": ["SQLServer", "Oracle"],
            "metric_codes": "INSTANCE_STATUS",
            "objective_percent": 99.5,
            "at_risk_budget_percent": 40.0,
            "category": "availability",
        }
    )
    assert policy.db_types == ("sqlserver", "oracle")
    assert policy.at_risk_budget_percent == 40.0
    assert policy.category == "availability"


def test_history_survives_a_db_type_change_because_scope_is_server_id(tmp_path):
    """Renaming what a machine *is* must not erase what it *did*.

    Two host records were given ``db_type: "host"`` on 2026-08-06 so the OS SLOs would cover
    them. Their 14,093 existing rows still carried the empty db_type they were collected under,
    and the SLA scoped on that column — so every OS SLO answered NO_DATA for machines with three
    weeks of history in the table. Scope is the server_id now, which does not move.
    """
    store = MetricStore(tmp_path / "db_ops.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            # Same target_id as always — only the db_type column is the stale one, which is
            # exactly the production case: the config edit renamed what the machine is, and
            # its identity (server_id, target_id) did not move.
            _metric_result(status="OK", collected_at="2026-05-28T00:00:00Z",
                           target_id="h1/host/db", db_type=""),
            _metric_result(status="ERROR", collected_at="2026-05-28T00:30:00Z",
                           target_id="h1/host/db", db_type=""),
        ],
    )
    policy = SlaPolicy(
        policy_id="OS_DISK",
        name="Disk free-space SLO",
        metric_codes=("INSTANCE_STATUS",),
        objective_percent=99.0,
        db_types=("host",),
        window_hours=24,
    )

    summary = validate_sla_policies(
        sqlite_path=tmp_path / "db_ops.sqlite",
        policies=[policy],
        window_end="2026-05-28T04:00:00Z",
        data_dir=_inventory(tmp_path, ("h1", "host")),
    )

    result = summary.results[0]
    assert result.status != "NO_DATA", "history collected under the old db_type was not read"
    assert result.total_count == 2
    assert result.actual_percent == 50.0
