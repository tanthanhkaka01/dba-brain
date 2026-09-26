"""Docker collector is script-based (assets .../metrics/docker/*.sh emitting the JSON contract).
Transport mirrors the cmd collector: it runs locally against the worker's mounted docker socket
by default, or ships the inspect script over SSH when the target declares an ssh cmd_access (a
remote docker host). The target's container name is passed in DOCKER_CONTAINER either way."""
import types
from pathlib import Path

import pytest

from conftest import answer_metric_items
from db_ops.metrics import collector
from db_ops.metrics.models import MetricDefinition, MetricTarget


def _docker_metric():
    return MetricDefinition(
        metric_code="DOCKER_CONTAINER_STATS", db_type="multi", category="container",
        default_importance=2, active=True, collector_type="docker",
        file="docker/009_docker_container_stats.sh", default_timeout=20,
        # The shipped script: a remote item carries its text, so it has to be a file that exists.
        path=Path(collector.__file__).parent / "collectors" / "docker" / "009_docker_container_stats.sh",
    )


def _target(container_name="", cmd_access=None):
    return MetricTarget(
        target_id="t", server_id="s", ip="10.0.0.1", db_type="sqlserver", db_name="master",
        credential_name="c", port=1433, service_name="", instance_name="",
        container_name=container_name, connection_info={}, credential={"username": "u"},
        cmd_access=cmd_access or {},
    )


def _ok_execution():
    return collector.CommandExecution(rows=[{"metric_item": "c:status", "metric_value": "running",
                                             "metric_unit": None, "status": "OK", "message": "ok"}],
                                      raw_stdout="[]", raw_stderr="", exit_code=0, execution_time=0.1)


def _ok_answer(_item):
    return {"exit_code": 0, "stdout": '[{"metric_item": "c:status", "metric_value": "running", "metric_unit": null, "status": "OK", "message": "ok"}]', "stderr": "", "duration_seconds": 0.1}


def test_run_docker_metric_passes_container_env(monkeypatch):
    sent = answer_metric_items(monkeypatch, _ok_answer)
    result = collector._run_docker_metric(
        metric=_docker_metric(), target=_target("MSSQL_LAB_HA_01-primary"), secrets={})

    [item] = sent
    assert item["kind"] == "local"
    # DB_OPS_TARGET_HOST rides along on every cmd collector; with no cmd_access it falls back to
    # the target's ip, which for a local docker host is the host the containers run on.
    assert item["env"] == {"DB_OPS_TARGET_HOST": "10.0.0.1",
                           "DOCKER_CONTAINER": "MSSQL_LAB_HA_01-primary"}
    assert item["path"].endswith("009_docker_container_stats.sh")
    assert item["timeout_seconds"] == 20
    # A docker host is not a `cmd_access: local` host: nothing to refuse on its name.
    assert item["require_local_host"] is False
    assert result.rows[0]["status"] == "OK"


def test_run_docker_metric_ships_over_ssh_when_cmd_access_ssh(monkeypatch):
    """A remote docker host (ssh cmd_access) runs the inspect script on the host, not locally."""
    sent = answer_metric_items(monkeypatch, _ok_answer)
    ssh = {"enabled": True, "method": "ssh", "host": "203.0.113.188", "port": 22, "shell": "bash"}
    result = collector._run_docker_metric(
        metric=_docker_metric(), target=_target("pg_ha-primary", cmd_access=ssh), secrets={})

    [item] = sent
    assert item["kind"] == "script", "a remote docker host runs the script there, not locally"
    # cmd_access.host wins over ip: it is the address already proven reachable.
    assert item["env"] == {"DB_OPS_TARGET_HOST": "203.0.113.188",
                           "DOCKER_CONTAINER": "pg_ha-primary"}
    assert item["access"]["host"] == "203.0.113.188"
    assert result.rows[0]["status"] == "OK"


def test_run_docker_metric_stays_local_when_cmd_access_disabled(monkeypatch):
    """A disabled cmd_access (or none) falls back to the local mounted socket — old behavior."""
    sent = answer_metric_items(monkeypatch, _ok_answer)
    disabled = {"enabled": False, "method": "ssh", "host": "x"}
    collector._run_docker_metric(
        metric=_docker_metric(), target=_target("pg_ha_01-primary", cmd_access=disabled), secrets={})
    assert [item["kind"] for item in sent] == ["local"]


def test_run_docker_metric_requires_container_name():
    with pytest.raises(RuntimeError):
        collector._run_docker_metric(metric=_docker_metric(), target=_target(""), secrets={})


def test_docker_metric_skipped_for_non_container_target():
    reason = collector._metric_unsupported_reason(_docker_metric(), _target(""))
    assert "container_name" in reason  # skipped cleanly, not an error


def test_docker_metric_runs_for_container_target():
    assert collector._metric_unsupported_reason(_docker_metric(), _target("pg_ha_01-primary")) == ""
