import json
from pathlib import Path

from db_ops.metrics.collector import _collect_one_metric
from db_ops.metrics.definitions import load_metric_definitions
from db_ops.metrics.models import MetricDefinition, MetricTarget


TEST_WORK_DIR = Path(__file__).resolve().parents[1] / "runtime" / "test_metric_timeouts"


def test_load_metric_definitions_reads_default_timeout():
    work_dir = TEST_WORK_DIR / "definitions"
    sql_dir = work_dir / "sql"
    sql_dir.mkdir(parents=True, exist_ok=True)
    (sql_dir / "metric.sql").write_text("select 1", encoding="utf-8")
    definitions_path = work_dir / "metric_definitions.json"
    definitions_path.write_text(
        json.dumps(
            {
                "metrics": [
                    {
                        "metric_code": "INSTANCE_STATUS",
                        "db_type": "sqlserver",
                        "category": "availability",
                        "default_importance": 5,
                        "interval_seconds": 60,
                        "default_timeout": 7,
                        "active": True,
                        "sql_file": "metric.sql",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    definitions = load_metric_definitions(definitions_path, sql_dir=sql_dir)

    assert definitions[0].default_timeout == 7


def test_collect_one_metric_passes_metric_timeout_to_executor(monkeypatch):
    work_dir = TEST_WORK_DIR / "collector"
    work_dir.mkdir(parents=True, exist_ok=True)
    sql_path = work_dir / "metric.sql"
    sql_path.write_text("select 1", encoding="utf-8")
    metric = MetricDefinition(
        metric_code="INSTANCE_STATUS",
        db_type="sqlserver",
        category="availability",
        default_importance=5,
        active=True,
        default_timeout=9,
        path=sql_path,
    )
    target = MetricTarget(
        target_id="server/sqlserver/master",
        server_id="server",
        ip="127.0.0.1",
        db_type="sqlserver",
        db_name="master",
        credential_name="test",
    )
    captured = {}

    def fake_execute_metric_sql(**kwargs):
        captured.update(kwargs)
        return [
            {
                "metric_item": "server",
                "metric_value": "1",
                "metric_unit": "status",
                "status": "OK",
                "message": "Online.",
            }
        ]

    monkeypatch.setattr("db_ops.metrics.collector.execute_metric_sql", fake_execute_metric_sql)

    results = _collect_one_metric(
        metric=metric,
        target=target,
        importance=5,
        secrets={},
        collected_at="2026-05-25T00:00:00Z",
    )

    assert captured["sql_timeout_seconds"] == 9
    assert results[0].status == "OK"
