import pytest

from db_ops.metrics.models import MetricTarget
from db_ops.metrics.targets import load_metric_targets, resolve_metric_target


def metric_target(target_id, ip):
    return MetricTarget(
        target_id=target_id,
        server_id=target_id.split("/", 1)[0],
        ip=ip,
        db_type="sqlserver",
        db_name=target_id.rsplit("/", 1)[-1],
        credential_name="test",
    )


def test_resolve_metric_target_by_ip(monkeypatch):
    target = metric_target("server/sqlserver/db", "192.0.2.115")
    monkeypatch.setattr("db_ops.metrics.targets.load_metric_targets", lambda **_: [target])

    resolved = resolve_metric_target(target_ip="192.0.2.115")

    assert resolved.target_id == "server/sqlserver/db"


def test_resolve_metric_target_by_ip_reports_no_match(monkeypatch):
    monkeypatch.setattr("db_ops.metrics.targets.load_metric_targets", lambda **_: [])

    with pytest.raises(ValueError, match="No matching metrics target found.*target_ip=192.0.2.115"):
        resolve_metric_target(target_ip="192.0.2.115")


def test_resolve_metric_target_by_ip_reports_ambiguity(monkeypatch):
    targets = [
        metric_target("server-a/sqlserver/db", "192.0.2.115"),
        metric_target("server-b/sqlserver/db", "192.0.2.115"),
    ]
    monkeypatch.setattr("db_ops.metrics.targets.load_metric_targets", lambda **_: targets)

    with pytest.raises(ValueError, match="Ambiguous metrics target.*server-a/sqlserver/db.*server-b/sqlserver/db"):
        resolve_metric_target(target_ip="192.0.2.115")


def test_resolve_metric_target_accepts_target_id_guard(monkeypatch):
    targets = [
        metric_target("server-a/sqlserver/db", "192.0.2.115"),
        metric_target("server-b/sqlserver/db", "192.0.2.115"),
    ]
    monkeypatch.setattr("db_ops.metrics.targets.load_metric_targets", lambda **_: targets)

    resolved = resolve_metric_target(target_ip="192.0.2.115", target_id="server-b/sqlserver/db")

    assert resolved.target_id == "server-b/sqlserver/db"


def test_load_metric_targets_skips_metrics_disabled_target(tmp_path, monkeypatch):
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
              "service_name": "ERP",
              "enabled": true,
              "metrics": {"enabled": false}
            },
            {
              "site": "ACME",
              "ip": "192.0.2.116",
              "db_type": "sqlserver",
              "service_name": "ERP2",
              "enabled": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr("db_ops.metrics.targets.load_database_inventory", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("db_ops.metrics.targets.load_credentials_file", lambda *_args, **_kwargs: [])

    targets = load_metric_targets(data_dir=data_dir)

    assert [target.ip for target in targets] == ["192.0.2.116"]


def test_load_metric_targets_infers_platform_from_os_and_loads_local_cmd_access(tmp_path, monkeypatch):
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
              "service_name": "ERP",
              "os": "Windows Server 2019 Datacenter",
              "enabled": true,
              "metrics": {"enabled": true},
              "cmd_access": {"enabled": true, "method": "local", "shell": "powershell"}
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr("db_ops.metrics.targets.load_database_inventory", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("db_ops.metrics.targets.load_credentials_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("db_ops.metrics.targets.load_remote_credentials_file", lambda *_args, **_kwargs: [])

    targets = load_metric_targets(data_dir=data_dir)

    assert targets[0].platform == "windows"
    assert targets[0].cmd_access["method"] == "local"


def test_load_metric_targets_rejects_unknown_cmd_access_method(tmp_path, monkeypatch):
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
              "service_name": "ERP",
              "platform": "windows",
              "enabled": true,
              "metrics": {"enabled": true},
              "cmd_access": {"enabled": true, "method": "telnet", "shell": "powershell"}
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr("db_ops.metrics.targets.load_database_inventory", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("db_ops.metrics.targets.load_credentials_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("db_ops.metrics.targets.load_remote_credentials_file", lambda *_args, **_kwargs: [])

    # A bad cmd_access is still refused — but on the target that has it, not on the whole scan.
    # Raising here aborted `load_metric_targets`, and with it every metric on every target
    # (database ones included): one mis-configured entry took the estate's monitoring down.
    # Seen 2026-08-01 when a host was moved off method=local before it had an SSH credential.
    targets = load_metric_targets(data_dir=data_dir)

    assert len(targets) == 1, "the target must still load, carrying its config error"
    assert "cmd_access.method" in targets[0].cmd_access["error"]


def test_a_broken_cmd_access_does_not_take_the_other_targets_down(tmp_path, monkeypatch):
    """The property that matters: one bad entry costs one target, never the scan."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text(
        """
        {
          "db_instances": [
            {"site": "ACME", "ip": "10.0.0.1", "db_type": "sqlserver", "service_name": "BAD",
             "platform": "windows", "enabled": true, "metrics": {"enabled": true},
             "cmd_access": {"enabled": true, "method": "telnet", "shell": "powershell"}},
            {"site": "ACME", "ip": "10.0.0.2", "db_type": "sqlserver", "service_name": "GOOD",
             "platform": "windows", "enabled": true, "metrics": {"enabled": true},
             "cmd_access": {"enabled": true, "method": "local", "shell": "powershell"}}
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr("db_ops.metrics.targets.load_database_inventory", lambda *_a, **_k: [])
    monkeypatch.setattr("db_ops.metrics.targets.load_credentials_file", lambda *_a, **_k: [])
    monkeypatch.setattr("db_ops.metrics.targets.load_remote_credentials_file", lambda *_a, **_k: [])

    targets = load_metric_targets(data_dir=data_dir)

    by_service = {t.service_name: t for t in targets}
    assert set(by_service) == {"BAD", "GOOD"}
    assert by_service["BAD"].cmd_access.get("error")
    assert not by_service["GOOD"].cmd_access.get("error")


def test_load_metric_targets_rejects_unknown_platform(tmp_path, monkeypatch):
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
              "service_name": "ERP",
              "platform": "aix",
              "enabled": true,
              "metrics": {"enabled": true}
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr("db_ops.metrics.targets.load_database_inventory", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("db_ops.metrics.targets.load_credentials_file", lambda *_args, **_kwargs: [])

    with pytest.raises(RuntimeError, match="Unsupported platform"):
        load_metric_targets(data_dir=data_dir)


def test_load_metric_targets_resolves_remote_cmd_credential(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text(
        """
        {
          "db_instances": [
            {
              "site": "ACME",
              "ip": "192.0.2.108",
              "db_type": "sqlserver",
              "service_name": "DW",
              "platform": "windows",
              "enabled": true,
              "metrics": {"enabled": true},
              "cmd_access": {
                "enabled": true,
                "method": "winrm",
                "host": "192.0.2.108",
                "credential_name": "remote_2.108_admin",
                "shell": "powershell"
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr("db_ops.metrics.targets.load_database_inventory", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("db_ops.metrics.targets.load_credentials_file", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "db_ops.metrics.targets.load_remote_credentials_file",
        lambda *_args, **_kwargs: [
            {
                "server_id": "ACME-192-0-2-108",
                "host": "192.0.2.108",
                "credentials": [
                    {
                        "credential_name": "remote_2.108_admin",
                        "username": "admin",
                        "password_ref": "REMOTE_2_108_ADMIN",
                    }
                ],
            }
        ],
    )

    targets = load_metric_targets(data_dir=data_dir)

    assert targets[0].cmd_credential["username"] == "admin"
    assert targets[0].cmd_credential["password_ref"] == "REMOTE_2_108_ADMIN"
    assert targets[0].cmd_access["port"] == 5985
    assert targets[0].cmd_access["ssl"] is False


def test_load_metric_targets_preserves_report_policy_disabled_metric_codes(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text(
        """
        {
          "db_instances": [
            {
              "site": "ACME",
              "ip": "192.0.2.108",
              "db_type": "sqlserver",
              "service_name": "DW",
              "platform": "windows",
              "enabled": true,
              "metrics": {"enabled": true},
              "report_policy": {
                "enabled": true,
                "severity_overrides": {},
                "disabled_metric_codes": ["OS_CPU_USAGE", "OS_DISK_USAGE"],
                "disabled_event_codes": [],
                "disabled_report_events": []
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr("db_ops.metrics.targets.load_database_inventory", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("db_ops.metrics.targets.load_credentials_file", lambda *_args, **_kwargs: [])

    targets = load_metric_targets(data_dir=data_dir)

    assert targets[0].report_policy["disabled_metric_codes"] == ["OS_CPU_USAGE", "OS_DISK_USAGE"]
