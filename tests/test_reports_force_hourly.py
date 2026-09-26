from pathlib import Path
from types import SimpleNamespace

import pytest

from db_ops.reports import cli as reports_cli
from db_ops.reports import service


def _sqlite_store_config(sqlite_path):
    """SQLite store declaration for a test config stand-in (mirrors DbOpsConfig.store)."""
    from pathlib import Path as _Path

    from db_ops.config import SqliteStoreConfig, StoreConfig

    return StoreConfig(sqlite=SqliteStoreConfig(path=_Path(str(sqlite_path))))


def config(tmp_path):
    return SimpleNamespace(
        sqlite_path=tmp_path / "runtime.sqlite",
        store=_sqlite_store_config(tmp_path / "runtime.sqlite"),
        telegram=SimpleNamespace(groups={"logging": "-100"}),
    )


def target():
    return SimpleNamespace(target_id="server/sqlserver/db", ip="192.0.2.115")


def collect_summary():
    return SimpleNamespace(
        run_id=1,
        target_count=1,
        metric_count=2,
        executed_count=2,
        result_count=2,
        error_count=0,
        warning_count=0,
        critical_count=0,
        duration_seconds=0.1,
    )


def test_reports_cli_force_hourly_accepts_server_id():
    # --server-id is the preferred unique key; --target-ip is optional now.
    args = reports_cli.parse_args(["force-hourly-report", "--server-id", "ACME-192-0-2-248"])
    assert args.server_id == "ACME-192-0-2-248"
    assert args.target_ip is None


def test_force_hourly_report_requires_server_id_or_target_ip(tmp_path):
    # Neither given -> a clear runtime error (argparse no longer enforces it).
    with pytest.raises(service.ReportWorkflowError, match="requires --server-id or --target-ip"):
        service.force_hourly_report(config=config(tmp_path))


def test_resolve_report_target_uses_config_before_runtime_rows(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text(
        """
        {
          "db_instances": [
            {
              "site": "ACME",
              "ip": "192.0.2.115",
              "db_type": "sqlserver",
              "service_name": "MSSQL",
              "enabled": true,
              "metrics": {"enabled": true}
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    target = service.resolve_report_target(
        sqlite_path=tmp_path / "runtime.sqlite",
        target_ip="192.0.2.115",
        data_dir=data_dir,
    )

    assert target.target_id == "ACME-192-0-2-115/sqlserver/MSSQL"


def test_resolve_report_target_by_server_id(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text(
        """
        {
          "db_instances": [
            {"server_id": "ACME-192-0-2-249-MSSQLAG-1533", "site": "ACME", "ip": "192.0.2.249",
             "port": 1533, "db_type": "sqlserver", "service_name": "AG1", "enabled": true, "metrics": {"enabled": true}},
            {"server_id": "ACME-192-0-2-249-MSSQLAG-1534", "site": "ACME", "ip": "192.0.2.249",
             "port": 1534, "db_type": "sqlserver", "service_name": "AG2", "enabled": true, "metrics": {"enabled": true}}
          ]
        }
        """,
        encoding="utf-8",
    )
    # Both share the IP; the server_id disambiguates with no db_type/port needed.
    target = service.resolve_report_target(
        sqlite_path=tmp_path / "runtime.sqlite",
        server_id="ACME-192-0-2-249-MSSQLAG-1534",
        data_dir=data_dir,
    )
    assert target.server_id == "ACME-192-0-2-249-MSSQLAG-1534"
    assert target.target_id == "ACME-192-0-2-249-MSSQLAG-1534/sqlserver/AG2"


def test_resolve_report_target_unknown_server_id_raises(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text('{"db_instances": []}', encoding="utf-8")
    with pytest.raises(service.ReportWorkflowError, match="No configured metric target for server_id"):
        service.resolve_report_target(
            sqlite_path=tmp_path / "runtime.sqlite",
            server_id="NOPE",
            data_dir=data_dir,
        )


def test_resolve_report_target_fails_when_reports_disabled(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text(
        """
        {
          "db_instances": [
            {
              "site": "ACME",
              "ip": "192.0.2.115",
              "db_type": "sqlserver",
              "service_name": "MSSQL",
              "enabled": true,
              "metrics": {"enabled": true},
              "reports": {"enabled": false}
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(service.ReportWorkflowError, match="report disabled for target"):
        service.resolve_report_target(
            sqlite_path=tmp_path / "runtime.sqlite",
            target_ip="192.0.2.115",
            data_dir=data_dir,
        )


def test_stored_metric_summary_fails_when_metrics_disabled_and_no_rows(tmp_path):
    with pytest.raises(service.ReportWorkflowError, match="no stored metric rows.*collection is disabled"):
        service.stored_metric_summary(sqlite_path=tmp_path / "runtime.sqlite", target_id="server/sqlserver/db")


def test_an_enabled_target_with_no_rows_yet_is_told_to_wait_for_the_schedule(tmp_path):
    """Two states with two different fixes: switch collection on, or wait for its first pass."""
    with pytest.raises(service.ReportWorkflowError, match="no stored metric rows.*its own schedule"):
        service.stored_metric_summary(sqlite_path=tmp_path / "runtime.sqlite",
                                      target_id="server/sqlserver/db", metrics_enabled=True)


def test_force_hourly_report_reads_stored_results_then_reports_and_pushes_in_order(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        service,
        "resolve_report_target",
        lambda **kwargs: calls.append(("resolve", kwargs)) or target(),
    )
    monkeypatch.setattr(
        service,
        "stored_metric_summary",
        lambda **kwargs: calls.append(("stored", kwargs)) or collect_summary(),
    )
    monkeypatch.setattr(
        service,
        "create_hourly_metrics_report",
        lambda **kwargs: calls.append(("create", kwargs)) or {"created": 1, "report_ids": [10]},
    )
    monkeypatch.setattr(
        service,
        "push_hourly_report_alerts",
        lambda **kwargs: calls.append(("push", kwargs)) or {"queued": 1, "queued_ids": [20]},
    )

    result = service.force_hourly_report(
        config=config(tmp_path),
        target_ip="192.0.2.115",
        summary_limit=150,
        dedupe_seconds=0,
    )

    assert [name for name, _ in calls] == ["resolve", "stored", "create", "push"]
    assert calls[1][1]["target_id"] == "server/sqlserver/db"
    assert calls[2][1]["summary_limit"] == 150
    assert calls[3][1]["dedupe_seconds"] == 0
    assert calls[3][1]["report_ids"] == [10]
    assert result["exit_code"] == 0
    assert result["target_id"] == "server/sqlserver/db"
    assert result["stored"]["result_count"] == 2


def test_a_report_collects_nothing_and_starts_no_process():
    """The operator, 2026-09-26: a report does not run metrics. It used to start
    `metrics.cli collect --force` for the target first (rules R42); collecting is metrics' job."""
    source = Path(service.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "db_ops.metrics.cli" not in source


def test_the_old_windowed_flag_is_refused_with_the_command_that_replaces_it(tmp_path):
    """A bot catalogue from before 0.24.0 still sends --include-windowed for `full`. Ignoring it
    would answer a question nobody asked; argparse's "unrecognized arguments" would say nothing."""
    with pytest.raises(service.ReportWorkflowError, match="metrics.cli collect .*--include-windowed") as caught:
        service.force_hourly_report(config=config(tmp_path), server_id="ACME-192-0-2-248",
                                    include_windowed=True)
    assert caught.value.exit_code == 2


def test_force_hourly_report_stored_results_failure_stops_workflow(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "resolve_report_target", lambda **_: target())

    def fail_stored(**kwargs):
        calls.append("stored")
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(service, "stored_metric_summary", fail_stored)
    monkeypatch.setattr(service, "create_hourly_metrics_report", lambda **_: calls.append("create"))
    monkeypatch.setattr(service, "push_hourly_report_alerts", lambda **_: calls.append("push"))

    with pytest.raises(service.ReportWorkflowError, match="store unreachable"):
        service.force_hourly_report(config=config(tmp_path), target_ip="192.0.2.115")

    assert calls == ["stored"]


def test_force_hourly_report_report_creation_failure_stops_workflow(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "resolve_report_target", lambda **_: target())
    monkeypatch.setattr(service, "stored_metric_summary", lambda **_: calls.append("stored") or collect_summary())

    def fail_create(**kwargs):
        calls.append("create")
        raise RuntimeError("report failed")

    monkeypatch.setattr(service, "create_hourly_metrics_report", fail_create)
    monkeypatch.setattr(service, "push_hourly_report_alerts", lambda **_: calls.append("push"))

    with pytest.raises(service.ReportWorkflowError, match="report failed"):
        service.force_hourly_report(config=config(tmp_path), target_ip="192.0.2.115")

    assert calls == ["stored", "create"]


def test_force_hourly_report_alert_push_failure_stops_workflow(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "resolve_report_target", lambda **_: target())
    monkeypatch.setattr(service, "stored_metric_summary", lambda **_: calls.append("stored") or collect_summary())
    monkeypatch.setattr(service, "create_hourly_metrics_report", lambda **_: calls.append("create") or {"created": 1, "report_ids": [10]})

    def fail_push(**kwargs):
        calls.append("push")
        raise RuntimeError("push failed")

    monkeypatch.setattr(service, "push_hourly_report_alerts", fail_push)

    with pytest.raises(service.ReportWorkflowError, match="push failed"):
        service.force_hourly_report(config=config(tmp_path), target_ip="192.0.2.115")

    assert calls == ["stored", "create", "push"]
