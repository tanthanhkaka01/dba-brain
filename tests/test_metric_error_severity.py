"""How a metric that failed to collect is graded — per metric, and per half of the attempt.

Every collection failure used to be a flat WARNING, on the theory that CRITICAL belonged to
values that breach a threshold. For an availability metric that reasoning inverts the meaning of
the result: when INSTANCE_STATUS cannot connect, "the instance did not answer" *is* the finding,
and reporting it one level below a full transaction log understates the estate's most serious
event. So each metric now declares what its own failure is worth, split by phase —
``connection_error_severity`` (never reached the target) and ``execution_error_severity``
(reached it, the check itself broke) — because "the server is down" and "this check is broken"
are different claims and rarely deserve the same level.

The phase has to be carried by the raiser, not guessed from the message: pyodbc says "Login
timeout expired" for an unreachable host while Oracle says "ORA-12541", and message keywords are
exactly how error_type classification went wrong before (see db_ops/metrics/collector.py).
"""

import json

import pytest

from db_ops.lib.event_policy import PHASE_CONNECT, PHASE_EXECUTE, resolve_failure_phase
from db_ops.common import remote_exec
from db_ops.metrics.collector import (
    MetricCommandError,
    _collect_one_metric,
    _remote_failure_phase,
)
from db_ops.metrics.definitions import DEFAULT_DEFINITIONS_PATH, load_metric_definitions
from db_ops.metrics.executor import MetricConnectionError, MetricExecutionError
from db_ops.metrics.models import MetricDefinition, MetricTarget


def _definition(**overrides):
    values = {
        "metric_code": "INSTANCE_STATUS",
        "db_type": "sqlserver",
        "category": "availability",
        "default_importance": 5,
        "active": True,
        "collector_type": "sql",
        "connection_error_severity": "CRITICAL",
        "execution_error_severity": "CRITICAL",
    }
    values.update(overrides)
    return MetricDefinition(**values)


def _target(**overrides):
    values = {
        "target_id": "server/sqlserver/db",
        "server_id": "server",
        "ip": "127.0.0.1",
        "db_type": "sqlserver",
        "db_name": "db",
        "credential_name": "test",
    }
    values.update(overrides)
    return MetricTarget(**values)


def _collect_failing_with(exc, metric, monkeypatch, target=None):
    def fail(**_kwargs):
        raise exc

    monkeypatch.setattr("db_ops.metrics.collector._run_sql_file", fail)
    results = _collect_one_metric(
        metric=metric,
        target=target or _target(),
        importance=5,
        secrets={},
        collected_at="2026-08-14T00:00:00Z",
    )
    assert len(results) == 1
    return results[0]


def test_a_metric_that_cannot_connect_is_graded_by_its_connection_error_severity(monkeypatch):
    result = _collect_failing_with(
        MetricConnectionError("Connection failed: login timeout expired"),
        _definition(connection_error_severity="CRITICAL", execution_error_severity="WARNING"),
        monkeypatch,
    )

    assert result.status == "CRITICAL"


def test_a_metric_whose_sql_failed_is_graded_by_its_execution_error_severity(monkeypatch):
    result = _collect_failing_with(
        MetricExecutionError("SQL execution failed: invalid object name 'sys.dm_os_sys_info'"),
        _definition(connection_error_severity="CRITICAL", execution_error_severity="WARNING"),
        monkeypatch,
    )

    assert result.status == "WARNING"


def test_a_metric_that_declares_neither_severity_still_fails_as_a_warning(monkeypatch):
    """The old flat behavior is the default, so a catalog predating these fields is unchanged."""
    metric = MetricDefinition(
        metric_code="STORAGE_DISK_FREE_SPACE",
        db_type="sqlserver",
        category="capacity",
        default_importance=3,
        active=True,
    )

    connect_failed = _collect_failing_with(MetricConnectionError("Connection failed: refused"), metric, monkeypatch)
    assert connect_failed.status == "WARNING"

    run_failed = _collect_failing_with(MetricExecutionError("SQL execution failed: boom"), metric, monkeypatch)
    assert run_failed.status == "WARNING"


def test_a_target_that_is_expected_to_refuse_connects_can_still_quiet_the_metric(monkeypatch):
    """severity_map runs after the phase grading, so a mounted standby does not page nightly."""
    target = _target(metrics_config={"severity_map": {"CRITICAL": "LOGGING"}})

    result = _collect_failing_with(
        MetricConnectionError("Connection failed: ORA-01033 ORACLE initialization or shutdown in progress"),
        _definition(),
        monkeypatch,
        target=target,
    )

    assert result.status == "LOGGING"


def test_a_failure_nobody_attributed_is_graded_as_an_execution_failure():
    """Unknown means the quieter claim: never report "the server is unreachable" on a guess."""
    assert resolve_failure_phase(RuntimeError("Metric SQL path is not resolved: BACKUP_AGE")) == PHASE_EXECUTE


def test_an_unclassified_failure_that_reads_as_unreachable_is_still_a_connect_failure():
    """The legacy Oracle bridge runs connect and query in one call and can declare no phase, so
    its errors are classified from the message — the only signal that transport has."""
    assert resolve_failure_phase(RuntimeError("ORA-12541: TNS:no listener; cannot connect")) == PHASE_CONNECT


def test_a_rejected_login_on_a_remote_host_is_a_connect_failure_not_a_script_failure():
    assert _remote_failure_phase(remote_exec.RemoteAuthError("Authentication failed.")) == PHASE_CONNECT
    assert _remote_failure_phase(remote_exec.RemoteConnectError("Port 22 is closed.")) == PHASE_CONNECT


def test_a_script_that_ran_too_long_is_an_execution_failure_but_an_unopened_session_is_not():
    """RemoteTimeoutError covers both halves; only a command timeout names the command it ran."""
    command_timeout = remote_exec.RemoteTimeoutError("SSH command timed out.", command="uptime")
    connect_timeout = remote_exec.RemoteTimeoutError("Connect timed out.")

    assert _remote_failure_phase(command_timeout) == PHASE_EXECUTE
    assert _remote_failure_phase(connect_timeout) == PHASE_CONNECT


def test_a_command_metric_that_exited_non_zero_is_graded_as_an_execution_failure(monkeypatch):
    metric = _definition(
        metric_code="OS_CPU_USAGE",
        collector_type="cmd",
        connection_error_severity="CRITICAL",
        execution_error_severity="WARNING",
    )

    def fail(**_kwargs):
        raise MetricCommandError("Command exited with code 1.", exit_code=1)

    monkeypatch.setattr("db_ops.metrics.collector._run_command_file", fail)
    results = _collect_one_metric(
        metric=metric,
        target=_target(platform="linux", cmd_access={"enabled": True, "method": "ssh", "host": "10.0.0.1"}),
        importance=4,
        secrets={},
        collected_at="2026-08-14T00:00:00Z",
    )

    assert [item.status for item in results] == ["WARNING"]


def test_every_metric_in_the_catalog_declares_both_failure_severities():
    """The fields are optional to the loader on purpose — a catalog that will not parse stops the
    whole estate's monitoring — so the "every metric declares them" rule is enforced here."""
    catalog = json.loads(DEFAULT_DEFINITIONS_PATH.read_text(encoding="utf-8"))

    missing = [
        item.get("metric_code")
        for item in catalog["metrics"]
        if not item.get("connection_error_severity") or not item.get("execution_error_severity")
    ]

    assert missing == []


def test_the_instance_status_metric_treats_both_kinds_of_failure_as_critical():
    """An instance that cannot be reached or cannot answer is an outage, not a warning."""
    definitions = {item.metric_code: item for item in load_metric_definitions(DEFAULT_DEFINITIONS_PATH)}

    instance_status = definitions["INSTANCE_STATUS"]
    assert (instance_status.connection_error_severity, instance_status.execution_error_severity) == (
        "CRITICAL",
        "CRITICAL",
    )
    assert definitions["BACKUP_AGE"].connection_error_severity == "WARNING"


def test_a_misspelled_failure_severity_is_refused_instead_of_read_as_a_warning(tmp_path):
    """A typo would otherwise silently downgrade the one metric the operator wanted paged."""
    catalog = tmp_path / "metric_definitions.json"
    catalog.write_text(
        json.dumps(
            {
                "metrics": [
                    {
                        "metric_code": "INSTANCE_STATUS",
                        "db_type": "sqlserver",
                        "category": "availability",
                        "default_importance": 5,
                        "active": True,
                        "collector_type": "sql",
                        "connection_error_severity": "critcal",
                        "execution_error_severity": "CRITICAL",
                        "variants": [{"name": "sqlserver", "db_type": "sqlserver", "file": "x.sql"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "x.sql").write_text("SELECT 1", encoding="utf-8")

    with pytest.raises(RuntimeError, match="connection_error_severity"):
        load_metric_definitions(catalog, sql_dir=sql_dir)


def test_warn_is_accepted_as_a_spelling_of_warning(tmp_path):
    catalog = tmp_path / "metric_definitions.json"
    catalog.write_text(
        json.dumps(
            {
                "metrics": [
                    {
                        "metric_code": "BACKUP_AGE",
                        "db_type": "sqlserver",
                        "category": "backup",
                        "default_importance": 3,
                        "active": True,
                        "collector_type": "sql",
                        "connection_error_severity": "warn",
                        "execution_error_severity": "WARN",
                        "variants": [{"name": "sqlserver", "db_type": "sqlserver", "file": "x.sql"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "x.sql").write_text("SELECT 1", encoding="utf-8")

    definition = load_metric_definitions(catalog, sql_dir=sql_dir)[0]

    assert (definition.connection_error_severity, definition.execution_error_severity) == ("WARNING", "WARNING")
