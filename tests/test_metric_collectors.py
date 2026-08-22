import json
import sys
import types
from pathlib import Path

import pytest

from db_ops.config import DbOpsConfig
from db_ops.metrics.collector import (
    CommandExecution,
    _apply_metric_result_overrides,
    _collect_one_metric,
    _metric_enabled_for_target,
    _resolve_metric_file_path,
    collect_metrics,
)
from conftest import shipped_config
from db_ops.metrics.definitions import DEFAULT_SQL_DIR, load_metric_definitions
from db_ops.metrics.models import MetricDefinition, MetricTarget, MetricVariant


def _target(db_type="sqlserver", *, platform="windows", method="local"):
    return MetricTarget(
        target_id=f"server/{db_type}/db",
        server_id="server",
        ip="127.0.0.1",
        db_type=db_type,
        db_name="db",
        credential_name="test",
        platform=platform,
        cmd_access={"enabled": True, "method": method, "host": "127.0.0.1", "shell": "powershell" if platform == "windows" else "bash"},
        cmd_credential={"credential_name": "remote", "username": "user", "password_ref": "PASS"},
    )


def test_report_policy_disabled_metric_codes_disable_collection_case_insensitive():
    target = _target()
    target.report_policy["disabled_metric_codes"] = ["os_cpu_usage"]

    assert not _metric_enabled_for_target(_definition(metric_code="OS_CPU_USAGE"), target)
    assert _metric_enabled_for_target(_definition(metric_code="OS_MEMORY_USAGE"), target)


def test_metric_overrides_still_disable_collection():
    target = _target()
    target.metrics_config["metric_overrides"] = {"OS_CPU_USAGE": {"enabled": False}}

    assert not _metric_enabled_for_target(_definition(metric_code="OS_CPU_USAGE"), target)


def _remap(target, *, metric_code="BACKUP_AGE", metric_item=None, status="CRITICAL", metric_value=None):
    out_status, _ = _apply_metric_result_overrides(
        metric=_definition(metric_code=metric_code),
        target=target,
        metric_item=metric_item,
        metric_value=metric_value,
        status=status,
        message=None,
    )
    return out_status


def test_severity_map_metric_level_remaps_only_matching_status():
    target = _target()
    target.metrics_config["metric_overrides"] = {"BACKUP_AGE": {"severity_map": {"CRITICAL": "WARNING"}}}

    assert _remap(target, status="CRITICAL") == "WARNING"
    # A status not present in the map is left untouched.
    assert _remap(target, status="WARNING") == "WARNING"
    assert _remap(target, status="OK") == "OK"


def test_severity_map_item_level_wins_over_metric_level():
    target = _target()
    target.metrics_config["metric_overrides"] = {
        "BACKUP_AGE": {
            "severity_map": {"CRITICAL": "WARNING"},
            "metric_item_overrides": {
                "Export": {"severity_map": {"CRITICAL": "LOGGING", "WARNING": "LOGGING"}},
            },
        }
    }

    # Item with its own map fully suppresses to LOGGING.
    assert _remap(target, metric_item="Export", status="CRITICAL") == "LOGGING"
    assert _remap(target, metric_item="Export", status="WARNING") == "LOGGING"
    # Any other item falls back to the metric-level map.
    assert _remap(target, metric_item="OtherDb", status="CRITICAL") == "WARNING"


def test_severity_map_absent_is_passthrough():
    target = _target()
    target.metrics_config["metric_overrides"] = {"BACKUP_AGE": {"metric_item_overrides": {}}}

    assert _remap(target, status="CRITICAL") == "CRITICAL"


def test_target_level_severity_map_applies_to_every_metric():
    # A mounted Data Guard standby fails every SQL metric with the same ORA-01033; the map is
    # declared once on the target instead of under each of the 39 Oracle metric codes.
    target = _target(db_type="oracle")
    target.metrics_config["severity_map"] = {"WARNING": "LOGGING", "CRITICAL": "LOGGING"}

    assert _remap(target, metric_code="INSTANCE_STATUS", status="WARNING") == "LOGGING"
    assert _remap(target, metric_code="PERFORMANCE_WAIT_STATS", status="CRITICAL") == "LOGGING"
    # A metric code with no override entry of its own is still covered.
    assert _remap(target, metric_code="SOME_METRIC_ADDED_LATER", status="WARNING") == "LOGGING"
    # Statuses outside the map are untouched.
    assert _remap(target, metric_code="INSTANCE_STATUS", status="OK") == "OK"


def test_metric_level_severity_map_wins_over_target_level():
    target = _target(db_type="oracle")
    target.metrics_config["severity_map"] = {"CRITICAL": "LOGGING"}
    target.metrics_config["metric_overrides"] = {"BACKUP_AGE": {"severity_map": {"CRITICAL": "WARNING"}}}

    # The metric-level entry overrides the target default for this code only.
    assert _remap(target, metric_code="BACKUP_AGE", status="CRITICAL") == "WARNING"
    assert _remap(target, metric_code="INSTANCE_STATUS", status="CRITICAL") == "LOGGING"


def test_collect_metrics_skips_report_policy_disabled_metric_codes(tmp_path, monkeypatch):
    target = _target()
    target.report_policy["disabled_metric_codes"] = ["OS_CPU_USAGE"]
    executed = []

    def fake_collect_one_metric(**kwargs):
        executed.append(kwargs["metric"].metric_code)
        return []

    monkeypatch.setattr(
        "db_ops.metrics.collector.load_metric_definitions",
        lambda *_, **__: [_definition(metric_code="OS_CPU_USAGE"), _definition(metric_code="OS_MEMORY_USAGE")],
    )
    monkeypatch.setattr("db_ops.metrics.collector.load_metric_importance_overrides", lambda *_, **__: [])
    monkeypatch.setattr("db_ops.metrics.collector.load_metric_targets", lambda **_: [target])
    monkeypatch.setattr("db_ops.metrics.collector.data_sources.load_secret_text", lambda *_, **__: {})
    monkeypatch.setattr("db_ops.metrics.collector._collect_one_metric", fake_collect_one_metric)

    summary = collect_metrics(
        config=DbOpsConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime", sqlite_path=tmp_path / "runtime.sqlite"),
        dry_run=False,
        force=True,
    )

    assert executed == ["OS_MEMORY_USAGE"]
    assert summary.disabled_count == 1
    assert "SKIP disabled OS_CPU_USAGE" in summary.message


def _definition(**overrides):
    values = {
        "metric_code": "INSTANCE_STATUS",
        "db_type": "sqlserver",
        "category": "availability",
        "default_importance": 5,
        "active": True,
        "collector_type": "sql",
        "default_timeout": 5,
    }
    values.update(overrides)
    return MetricDefinition(**values)


def _write_catalog(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_metric_definitions_reads_new_variants(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "metric.sql").write_text("select 1", encoding="utf-8")
    catalog = tmp_path / "metric_definitions.json"
    _write_catalog(
        catalog,
        {
            "metrics": [
                {
                    "metric_code": "INSTANCE_STATUS",
                    "collector_type": "sql",
                    "db_type": "multi",
                    "category": "availability",
                    "default_importance": 5,
                    "active": True,
                    "variants": [
                        {
                            "name": "sqlserver_current",
                            "db_type": "sqlserver",
                            "file": "metric.sql",
                        }
                    ],
                }
            ]
        },
    )

    definitions = load_metric_definitions(catalog, sql_dir=sql_dir)

    assert definitions[0].collector_type == "sql"
    assert definitions[0].variants[0].file == "metric.sql"


def test_long_waiting_or_rollback_metric_resolves_sqlserver_version_variants():
    definitions = load_metric_definitions(shipped_config("metric_definitions.json"), sql_dir=DEFAULT_SQL_DIR)
    metric = next(
        item for item in definitions if item.metric_code == "QUERY_LONG_WAITING_OR_ROLLBACK_REQUESTS"
    )
    legacy_target = _target()
    legacy_target.connection_info["sqlserver_major_version"] = 10
    modern_target = _target()
    modern_target.connection_info["sqlserver_major_version"] = 11

    legacy_path = _resolve_metric_file_path(metric, legacy_target)
    modern_path = _resolve_metric_file_path(metric, modern_target)

    # 150s since 2026-08-07: this metric answers 'is anything stuck right now', and a
    # 5-minute cadence is why the 09:16 incident on 192.0.2.115 was seen once.
    assert metric.interval_seconds == 150
    assert metric.empty_result_is_ok is True
    assert legacy_path == DEFAULT_SQL_DIR / "sqlserver/legacy_2008r2/026_long_waiting_or_rollback_requests.sql"
    assert modern_path == DEFAULT_SQL_DIR / "sqlserver/026_long_waiting_or_rollback_requests.sql"


def test_load_metric_definitions_converts_sql_variants_with_warning(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "metric.sql").write_text("select 1", encoding="utf-8")
    catalog = tmp_path / "metric_definitions.json"
    _write_catalog(
        catalog,
        {
            "metrics": [
                {
                    "metric_code": "INSTANCE_STATUS",
                    "db_type": "multi",
                    "category": "availability",
                    "default_importance": 5,
                    "active": True,
                    "sql_variants": [
                        {
                            "name": "sqlserver_current",
                            "db_type": "sqlserver",
                            "sql_file": "metric.sql",
                        }
                    ],
                }
            ]
        },
    )

    with pytest.warns(DeprecationWarning, match="sql_variants is deprecated"):
        definitions = load_metric_definitions(catalog, sql_dir=sql_dir)

    assert definitions[0].collector_type == "sql"
    assert definitions[0].variants[0].file == "metric.sql"


def test_unsupported_collector_type_rejected(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    catalog = tmp_path / "metric_definitions.json"
    _write_catalog(
        catalog,
        {
            "metrics": [
                {
                    "metric_code": "BROKER_STATUS",
                    "collector_type": "dgmgrl",
                    "db_type": "oracle",
                    "category": "availability",
                    "default_importance": 5,
                    "active": True,
                    "variants": [{"name": "oracle", "db_type": "oracle", "file": "broker.sh"}],
                }
            ]
        },
    )

    with pytest.raises(RuntimeError, match="collector_type"):
        load_metric_definitions(catalog, sql_dir=sql_dir)


def test_extension_validation_by_collector_type(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "metric.py").write_text("[]", encoding="utf-8")
    catalog = tmp_path / "metric_definitions.json"
    _write_catalog(
        catalog,
        {
            "metrics": [
                {
                    "metric_code": "INSTANCE_STATUS",
                    "collector_type": "sql",
                    "db_type": "sqlserver",
                    "category": "availability",
                    "default_importance": 5,
                    "active": True,
                    "variants": [{"name": "sqlserver", "db_type": "sqlserver", "file": "metric.py"}],
                }
            ]
        },
    )

    with pytest.raises(RuntimeError, match="extension"):
        load_metric_definitions(catalog, sql_dir=sql_dir)


def test_unsupported_variant_may_omit_file(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    catalog = tmp_path / "metric_definitions.json"
    _write_catalog(
        catalog,
        {
            "metrics": [
                {
                    "metric_code": "BACKUP_AGE",
                    "collector_type": "sql",
                    "db_type": "multi",
                    "category": "backup",
                    "default_importance": 5,
                    "active": True,
                    "variants": [
                        {
                            "name": "oracle",
                            "db_type": "oracle",
                            "supported": False,
                            "unsupported_reason": "Not available.",
                        }
                    ],
                }
            ]
        },
    )

    definitions = load_metric_definitions(catalog, sql_dir=sql_dir)

    assert definitions[0].variants[0].supported is False
    assert definitions[0].variants[0].file == ""


def test_sql_collector_dispatch_and_result_validation(tmp_path, monkeypatch):
    sql_path = tmp_path / "metric.sql"
    sql_path.write_text("select 1", encoding="utf-8")
    metric = _definition(path=sql_path)
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
        target=_target(),
        importance=5,
        secrets={},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert "select 1" in captured["sql_text"]
    assert results[0].metric_item == "server"


def test_sql_result_missing_required_field_fails(tmp_path, monkeypatch):
    sql_path = tmp_path / "metric.sql"
    sql_path.write_text("select 1", encoding="utf-8")
    metric = _definition(path=sql_path)
    monkeypatch.setattr(
        "db_ops.metrics.collector.execute_metric_sql",
        lambda **_kwargs: [{"metric_item": "server", "metric_value": "1"}],
    )

    results = _collect_one_metric(
        metric=metric,
        target=_target(),
        importance=5,
        secrets={},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].status == "WARNING"
    assert "missing required field" in str(results[0].message)


def test_cmd_collector_dispatch_and_json_validation(tmp_path):
    script = tmp_path / "metric.py"
    script.write_text(
        "import json\n"
        "print(json.dumps([{'metric_item':'cluster_status','metric_value':'ONLINE','metric_unit':'status','status':'OK','message':'Online.'}]))\n",
        encoding="utf-8",
    )
    metric = _definition(collector_type="cmd", path=script, default_timeout=5)

    results = _collect_one_metric(
        metric=metric,
        target=_target(),
        importance=5,
        secrets={},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].status == "OK"
    assert results[0].raw_stdout
    assert results[0].exit_code == 0


def test_cmd_invalid_json_failure(tmp_path):
    script = tmp_path / "metric.py"
    script.write_text("print('not json')\n", encoding="utf-8")
    metric = _definition(collector_type="cmd", path=script, default_timeout=5)

    results = _collect_one_metric(
        metric=metric,
        target=_target(),
        importance=5,
        secrets={},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].status == "WARNING"
    assert "valid JSON" in str(results[0].message)
    assert results[0].raw_stdout.strip() == "not json"


def test_cmd_timeout_failure(tmp_path):
    script = tmp_path / "metric.py"
    script.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
    metric = _definition(collector_type="cmd", path=script, default_timeout=1)

    results = _collect_one_metric(
        metric=metric,
        target=_target(),
        importance=5,
        secrets={},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].status == "WARNING"
    assert "timed out" in str(results[0].message)


def test_cmd_non_zero_exit_code_failure(tmp_path):
    script = tmp_path / "metric.py"
    script.write_text("import sys\nprint('[]')\nsys.exit(3)\n", encoding="utf-8")
    metric = _definition(collector_type="cmd", path=script, default_timeout=5)

    results = _collect_one_metric(
        metric=metric,
        target=_target(),
        importance=5,
        secrets={},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].status == "WARNING"
    assert results[0].exit_code == 3


def test_cmd_required_field_validation(tmp_path):
    script = tmp_path / "metric.py"
    script.write_text(
        "import json\nprint(json.dumps([{'metric_item':'cluster_status'}]))\n",
        encoding="utf-8",
    )
    metric = _definition(collector_type="cmd", path=script, default_timeout=5)

    results = _collect_one_metric(
        metric=metric,
        target=_target(),
        importance=5,
        secrets={},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].status == "WARNING"
    assert "missing required field" in str(results[0].message)


def test_cmd_runner_uses_local_python_for_py_files(tmp_path):
    script = tmp_path / "metric.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps([{'metric_item':'python','metric_value':sys.executable,'metric_unit':'path','status':'OK','message':'Python runner.'}]))\n",
        encoding="utf-8",
    )
    metric = _definition(collector_type="cmd", path=script, default_timeout=5)

    results = _collect_one_metric(
        metric=metric,
        target=_target(),
        importance=5,
        secrets={},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].metric_value == sys.executable


def test_cmd_metric_selects_variant_by_target_platform(tmp_path):
    windows_script = tmp_path / "windows.py"
    linux_script = tmp_path / "linux.py"
    windows_script.write_text(
        "import json\nprint(json.dumps([{'metric_item':'platform','metric_value':'windows','metric_unit':'status','status':'OK','message':'Windows.'}]))\n",
        encoding="utf-8",
    )
    linux_script.write_text(
        "import json\nprint(json.dumps([{'metric_item':'platform','metric_value':'linux','metric_unit':'status','status':'OK','message':'Linux.'}]))\n",
        encoding="utf-8",
    )
    metric = _definition(
        collector_type="cmd",
        variants=[
            MetricVariant(name="windows", db_type="multi", platform="windows", file="windows.py", path=windows_script),
            MetricVariant(name="linux", db_type="multi", platform="linux", file="linux.py", path=linux_script),
        ],
    )

    results = _collect_one_metric(
        metric=metric,
        target=_target(platform="linux"),
        importance=5,
        secrets={},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].metric_value == "linux"


def test_cmd_ssh_execution_dispatch(tmp_path, monkeypatch):
    script = tmp_path / "metric.sh"
    script.write_text("echo []", encoding="utf-8")
    metric = _definition(collector_type="cmd", path=script)
    captured = {}

    def fake_execute_ssh(path, *, target, secrets, timeout_seconds, collector_env=None):
        captured.update({"path": path, "target": target, "secrets": secrets,
                         "timeout_seconds": timeout_seconds, "collector_env": collector_env})
        return CommandExecution(
            rows=[{"metric_item": "ssh", "metric_value": "1", "metric_unit": "count", "status": "OK", "message": "SSH."}],
            raw_stdout="[]",
            raw_stderr="",
            exit_code=0,
            execution_time=0.1,
        )

    monkeypatch.setattr("db_ops.metrics.collector.execute_ssh", fake_execute_ssh)

    results = _collect_one_metric(
        metric=metric,
        target=_target(platform="linux", method="ssh"),
        importance=5,
        secrets={"PASS": "secret"},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert captured["path"] == script
    assert captured["target"].cmd_credential["username"] == "user"
    assert results[0].metric_item == "ssh"


def test_cmd_winrm_execution_dispatch(tmp_path, monkeypatch):
    script = tmp_path / "metric.ps1"
    script.write_text("'[]'", encoding="utf-8")
    metric = _definition(collector_type="cmd", path=script)

    def fake_execute_winrm(path, *, target, secrets, timeout_seconds, collector_env=None):
        return CommandExecution(
            rows=[{"metric_item": "winrm", "metric_value": "1", "metric_unit": "count", "status": "OK", "message": "WinRM."}],
            raw_stdout="[]",
            raw_stderr="",
            exit_code=0,
            execution_time=0.1,
        )

    monkeypatch.setattr("db_ops.metrics.collector.execute_winrm", fake_execute_winrm)

    results = _collect_one_metric(
        metric=metric,
        target=_target(platform="windows", method="winrm"),
        importance=5,
        secrets={"PASS": "secret"},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].metric_item == "winrm"


def test_winrm_execution_uses_cmd_access_port_and_ssl(tmp_path, monkeypatch):
    from db_ops.metrics.collector import execute_winrm

    script = tmp_path / "metric.ps1"
    script.write_text(
        "ConvertTo-Json -InputObject @(@{metric_item='x';metric_value='1';metric_unit='count';status='OK';message='ok'})",
        encoding="utf-8",
    )
    captured = {}

    class FakeClient:
        def __init__(self, host, **kwargs):
            captured["host"] = host
            captured.update(kwargs)

        def execute_ps(self, _script):
            return (
                '[{"metric_item":"x","metric_value":"1","metric_unit":"count","status":"OK","message":"ok"}]',
                "",
                0,
            )

    fake_client_module = types.ModuleType("pypsrp.client")
    fake_client_module.Client = FakeClient
    fake_package = types.ModuleType("pypsrp")
    fake_package.client = fake_client_module
    monkeypatch.setitem(sys.modules, "pypsrp", fake_package)
    monkeypatch.setitem(sys.modules, "pypsrp.client", fake_client_module)

    target = _target(platform="windows", method="winrm")
    target.cmd_access.update({"host": "192.0.2.108", "port": 5985, "ssl": False})

    result = execute_winrm(script, target=target, secrets={"PASS": "secret"}, timeout_seconds=12)

    assert captured["host"] == "192.0.2.108"
    assert captured["username"] == "192.0.2.108\\user"
    assert captured["port"] == 5985
    assert captured["ssl"] is False
    assert captured["connection_timeout"] == 12
    assert result.rows[0]["metric_item"] == "x"


def test_winrm_non_string_streams_are_coerced_to_text(tmp_path, monkeypatch):
    from db_ops.metrics.collector import execute_winrm

    script = tmp_path / "metric.ps1"
    script.write_text("Write-Output nope", encoding="utf-8")

    class FakeStreams:
        def __str__(self):
            return "stream detail"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def execute_ps(self, _script):
            return ("not json", FakeStreams(), 0)

    fake_client_module = types.ModuleType("pypsrp.client")
    fake_client_module.Client = FakeClient
    fake_package = types.ModuleType("pypsrp")
    fake_package.client = fake_client_module
    monkeypatch.setitem(sys.modules, "pypsrp", fake_package)
    monkeypatch.setitem(sys.modules, "pypsrp.client", fake_client_module)

    target = _target(platform="windows", method="winrm")

    results = _collect_one_metric(
        metric=_definition(collector_type="cmd", path=script),
        target=target,
        importance=5,
        secrets={"PASS": "secret"},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].status == "WARNING"
    assert results[0].raw_stdout == "not json"
    assert results[0].raw_stderr == "stream detail"


def test_cmd_unknown_platform_rejected(tmp_path):
    script = tmp_path / "metric.py"
    script.write_text("print('[]')", encoding="utf-8")
    metric = _definition(collector_type="cmd", variants=[MetricVariant(name="linux", db_type="multi", platform="linux", file="metric.py", path=script)])

    results = _collect_one_metric(
        metric=metric,
        target=_target(platform="aix"),
        importance=5,
        secrets={},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].status == "WARNING"
    assert "Unsupported target platform" in str(results[0].message)


def test_cmd_unknown_access_method_rejected(tmp_path):
    script = tmp_path / "metric.py"
    script.write_text("print('[]')", encoding="utf-8")
    metric = _definition(collector_type="cmd", path=script)

    results = _collect_one_metric(
        metric=metric,
        target=_target(method="telnet"),
        importance=5,
        secrets={},
        collected_at="2026-06-08T00:00:00Z",
    )

    assert results[0].status == "WARNING"
    assert "Unsupported cmd_access.method" in str(results[0].message)


# ---------------------------------------------------------------------------
# SSH execution tests
# ---------------------------------------------------------------------------

def _make_fake_paramiko(monkeypatch, *, stdout_data: bytes = b"", exit_code: int = 0, connect_raises=None):
    """Register a fake paramiko in sys.modules and return a captured-calls dict."""
    captured: dict = {}

    class _FakeChannel:
        def recv_exit_status(self):
            return exit_code

        def shutdown_write(self):
            pass

    class _FakeFile:
        def __init__(self, data: bytes = b""):
            self._data = data
            self.channel = _FakeChannel()

        def read(self) -> bytes:
            return self._data

        def write(self, data: bytes) -> None:
            captured.setdefault("stdin_written", b"")
            captured["stdin_written"] += data

    class _FakeSSHClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, hostname, **kwargs):
            captured["hostname"] = hostname
            captured["connect_kwargs"] = kwargs
            if connect_raises is not None:
                raise connect_raises

        def exec_command(self, command, **kwargs):
            captured["command"] = command
            return _FakeFile(), _FakeFile(stdout_data), _FakeFile()

        def close(self):
            pass

    fake_ssh_exc = types.ModuleType("paramiko.ssh_exception")
    fake_ssh_exc.NoValidConnectionsError = OSError

    fake_paramiko = types.ModuleType("paramiko")
    fake_paramiko.SSHClient = _FakeSSHClient
    fake_paramiko.AutoAddPolicy = lambda: None
    fake_paramiko.AuthenticationException = PermissionError
    fake_paramiko.ssh_exception = fake_ssh_exc

    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setitem(sys.modules, "paramiko.ssh_exception", fake_ssh_exc)
    return captured


def test_ssh_windows_ps1_uses_encoded_command(tmp_path, monkeypatch):
    """SSH to Windows sends PS1 content as -EncodedCommand base64, never as a file path."""
    from db_ops.metrics.collector import execute_ssh

    script = tmp_path / "metric.ps1"
    script.write_text("Get-Date", encoding="utf-8")

    ok_json = b'[{"metric_item":"t","metric_value":"1","metric_unit":"u","status":"OK","message":"ok"}]'
    captured = _make_fake_paramiko(monkeypatch, stdout_data=ok_json)

    target = _target(platform="windows", method="ssh")
    result = execute_ssh(script, target=target, secrets={}, timeout_seconds=10)

    assert "-EncodedCommand" in captured["command"]
    assert str(script) not in captured["command"], "file path must not appear in SSH command"
    assert result.rows[0]["status"] == "OK"


def test_ssh_linux_sh_uses_bash_stdin(tmp_path, monkeypatch):
    """SSH to Linux pipes .sh content via bash -s stdin instead of sending a file path."""
    from db_ops.metrics.collector import execute_ssh

    script = tmp_path / "metric.sh"
    script.write_text("echo hi", encoding="utf-8")

    ok_json = b'[{"metric_item":"t","metric_value":"1","metric_unit":"u","status":"OK","message":"ok"}]'
    captured = _make_fake_paramiko(monkeypatch, stdout_data=ok_json)

    target = _target(platform="linux", method="ssh")
    execute_ssh(script, target=target, secrets={}, timeout_seconds=10)

    assert captured["command"] == "bash -s"
    assert captured.get("stdin_written") == b"echo hi"


def test_ssh_uses_port_22_by_default(tmp_path, monkeypatch):
    """SSH resolves to port 22 when no explicit port is set in cmd_access."""
    from db_ops.metrics.collector import execute_ssh

    script = tmp_path / "metric.ps1"
    script.write_text("$true", encoding="utf-8")

    ok_json = b'[{"metric_item":"t","metric_value":"1","metric_unit":"u","status":"OK","message":"ok"}]'
    captured = _make_fake_paramiko(monkeypatch, stdout_data=ok_json)

    target = _target(platform="windows", method="ssh")
    execute_ssh(script, target=target, secrets={}, timeout_seconds=10)

    assert captured["connect_kwargs"].get("port") == 22


def test_ssh_auth_failure_gives_clear_message(tmp_path, monkeypatch):
    """Authentication failure produces an ERROR result with an informative message."""
    from db_ops.metrics.collector import execute_ssh, MetricCommandError

    script = tmp_path / "metric.ps1"
    script.write_text("$true", encoding="utf-8")
    _make_fake_paramiko(monkeypatch, connect_raises=PermissionError("auth fail"))

    target = _target(platform="windows", method="ssh")
    with pytest.raises(MetricCommandError, match="authentication failed"):
        execute_ssh(script, target=target, secrets={}, timeout_seconds=10)


def test_ssh_connection_refused_gives_clear_message(tmp_path, monkeypatch):
    """Port-closed / unreachable error produces a MetricCommandError describing port and host."""
    from db_ops.metrics.collector import execute_ssh, MetricCommandError

    script = tmp_path / "metric.ps1"
    script.write_text("$true", encoding="utf-8")
    _make_fake_paramiko(monkeypatch, connect_raises=ConnectionRefusedError("refused"))

    target = _target(platform="windows", method="ssh")
    with pytest.raises(MetricCommandError, match="port 22 is open"):
        execute_ssh(script, target=target, secrets={}, timeout_seconds=10)


def test_an_os_metric_is_skipped_on_a_target_with_no_cmd_access():
    """A database in a container has no OS to log into — the engine is the whole target. An OS
    metric there is not a failure, it is not applicable. Before this, every cmd metric produced a
    WARNING row on the PostgreSQL lab targets on every cycle (61 rows a day), and each new OS
    metric added another one until someone remembered to list its code in a per-target disable
    list."""
    from db_ops.metrics.collector import _metric_unsupported_reason

    container_target = MetricTarget(                      # a lab database in Docker: no OS access
        target_id="ACME-192-0-2-249-PGLAB-5433/postgresql/pg_ha_01_primary",
        server_id="ACME-192-0-2-249-PGLAB-5433", ip="192.0.2.249", db_type="postgresql",
        db_name="pg_ha_01_primary", credential_name="c", platform="linux", cmd_access={},
    )

    reason = _metric_unsupported_reason(
        _definition(metric_code="OS_INFO", collector_type="cmd", db_type="multi"), container_target)
    assert "no cmd_access" in reason

    # A SQL metric on the same target is unaffected.
    assert _metric_unsupported_reason(
        _definition(metric_code="INSTANCE_STATUS", collector_type="sql", db_type="postgresql"),
        container_target) == ""


# --------------------------------------------------------------------------- #
# metrics.disabled_collector_types — turn off a whole class of collector on one target.
# --------------------------------------------------------------------------- #
def test_disabled_collector_types_turns_off_that_whole_class():
    """Listing every OS metric code in disabled_metric_codes was the only way to say "this
    target has no OS to look at" — so every OS metric added later started producing noise until
    someone extended the list. The switch belongs on what they have in common."""
    target = _target()
    target.metrics_config["disabled_collector_types"] = ["cmd"]

    assert not _metric_enabled_for_target(_definition(metric_code="OS_CPU_USAGE", collector_type="cmd"), target)
    assert not _metric_enabled_for_target(_definition(metric_code="OS_NETWORK", collector_type="cmd"), target)
    # A metric added tomorrow is covered too, without touching this target's config again.
    assert not _metric_enabled_for_target(_definition(metric_code="OS_SOMETHING_NEW", collector_type="cmd"), target)
    # SQL metrics keep running.
    assert _metric_enabled_for_target(_definition(metric_code="INSTANCE_STATUS", collector_type="sql"), target)


def test_disabled_collector_types_can_turn_off_sql_instead():
    target = _target()
    target.metrics_config["disabled_collector_types"] = ["sql"]

    assert not _metric_enabled_for_target(_definition(metric_code="INSTANCE_STATUS", collector_type="sql"), target)
    assert _metric_enabled_for_target(_definition(metric_code="OS_CPU_USAGE", collector_type="cmd"), target)


def test_an_unknown_collector_type_is_an_error_not_a_silent_no_op():
    """["command"] instead of ["cmd"] would collect everything while the operator believed it
    was off. A config typo must fail loudly."""
    target = _target()
    target.metrics_config["disabled_collector_types"] = ["command"]

    with pytest.raises(RuntimeError, match="unknown value"):
        _metric_enabled_for_target(_definition(metric_code="OS_CPU_USAGE", collector_type="cmd"), target)


def test_no_switch_means_everything_runs():
    target = _target()
    assert _metric_enabled_for_target(_definition(metric_code="OS_CPU_USAGE", collector_type="cmd"), target)
    assert _metric_enabled_for_target(_definition(metric_code="INSTANCE_STATUS", collector_type="sql"), target)


# ---------------------------------------------------------------------------
# A metric that fails outright must honour the same severity overrides
# ---------------------------------------------------------------------------

def _fail_collection(monkeypatch, message):
    """Make the SQL collector raise, the way a refused connect does."""
    def boom(**_kwargs):
        raise RuntimeError(message)
    monkeypatch.setattr("db_ops.metrics.collector._run_sql_file", boom)


ORA_01033 = "ORA-01033: ORACLE initialization or shutdown in progress"


def test_target_severity_map_applies_to_a_failed_metric(monkeypatch):
    """Regression: the map only reached results that had already succeeded.

    A mounted Data Guard standby refuses every connect, so every metric took the exception
    path — which built its result directly and skipped the overrides. The standby kept
    alerting at full severity however its severity_map was configured.
    """
    _fail_collection(monkeypatch, ORA_01033)
    target = _target(db_type="oracle")
    target.metrics_config["severity_map"] = {"WARNING": "LOGGING", "CRITICAL": "LOGGING"}

    results = _collect_one_metric(
        metric=_definition(metric_code="INSTANCE_STATUS", db_type="oracle"),
        target=target, importance=5, secrets={}, collected_at="2026-07-25T00:00:00Z",
    )

    assert [item.status for item in results] == ["LOGGING"]
    assert ORA_01033 in (results[0].message or "")


def test_a_failed_metric_without_a_map_keeps_its_severity(monkeypatch):
    _fail_collection(monkeypatch, ORA_01033)

    results = _collect_one_metric(
        metric=_definition(metric_code="INSTANCE_STATUS", db_type="oracle"),
        target=_target(db_type="oracle"), importance=5, secrets={},
        collected_at="2026-07-25T00:00:00Z",
    )

    assert results[0].status != "LOGGING"


def test_metric_level_map_still_wins_on_the_failure_path(monkeypatch):
    _fail_collection(monkeypatch, ORA_01033)
    target = _target(db_type="oracle")
    target.metrics_config["severity_map"] = {"WARNING": "LOGGING"}
    target.metrics_config["metric_overrides"] = {
        "INSTANCE_STATUS": {"severity_map": {"WARNING": "CRITICAL"}}
    }

    results = _collect_one_metric(
        metric=_definition(metric_code="INSTANCE_STATUS", db_type="oracle"),
        target=target, importance=5, secrets={}, collected_at="2026-07-25T00:00:00Z",
    )

    assert results[0].status == "CRITICAL"
