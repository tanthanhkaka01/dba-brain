"""A deadlock is a finding on Oracle and PostgreSQL too, by SQL Server's rule (1.149's port).

LOCK_DEADLOCK_RECENT was SQL Server's alone - the system_health ring buffer, any deadlock in 24 hours
WARNING, five CRITICAL. The other two engines keep their deadlock history in their logs: Oracle's
alert log carries an ORA-00060 line for each, readable from SQL through v$diag_alert_ext (11g+);
PostgreSQL writes "ERROR:  deadlock detected" to its server log, which POSTGRES_SERVER_LOG already
reads. Both now grade by the same rule.

The Oracle read is an external-table scan of the whole XML alert log, so its variant states its own
interval - hourly, as LOG_RECENT_CRITICAL reads the same view - instead of the metric's 150 s, set by
SQL Server's cheap ring buffer. A variant interval is honoured by the scheduler for that target only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dbabrain.metrics import collector
from dbabrain.metrics.definitions import DEFAULT_DEFINITIONS_PATH, load_metric_definitions

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
BASH = shutil.which("bash")


def _definition(code):
    return next(item for item in load_metric_definitions(DEFAULT_DEFINITIONS_PATH) if item.metric_code == code)


def _code(relative: str) -> str:
    text = (COLLECTORS / relative).read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", " ".join(line.split("--", 1)[0] for line in text.splitlines()))


# --------------------------------------------------------------------------------------------- #
# Oracle
# --------------------------------------------------------------------------------------------- #

def test_oracle_counts_ora_60_in_the_alert_log_by_sql_servers_rule():
    code = _code("oracle/015_oracle_deadlock_recent.sql")
    assert "FROM v$diag_alert_ext a" in code
    assert "a.message_text LIKE '%ORA-00060%'" in code
    # At any message level: ORA-00060 is written at 16, which LOG_RECENT_CRITICAL never reads.
    assert "message_level" not in code
    assert "WHEN d.deadlocks >= p.critical_count THEN 'CRITICAL' WHEN d.deadlocks > 0 THEN 'WARNING' ELSE 'OK'" in code
    assert "SELECT 24 AS window_hours, 5 AS critical_count FROM dual" in code
    sql_server = _code("sqlserver/015_sqlserver_deadlock_recent.sql")
    assert "WHEN deadlock_count_24h >= 5 THEN 'CRITICAL'" in sql_server


def test_the_oracle_variant_runs_hourly_and_only_from_11g():
    metric = _definition("LOCK_DEADLOCK_RECENT")
    oracle = {variant.name: variant for variant in metric.variants if variant.db_type == "oracle"}
    assert oracle["oracle_11g_plus"].repeat_interval == 3600
    assert oracle["oracle_11g_plus"].min_major_version == 11
    assert oracle["oracle_pre_11g_unsupported"].supported is False
    assert metric.interval_seconds == 150


class _Target:
    metrics_config: dict = {}
    cmd_access: dict = {}
    container_name = ""
    platform = ""

    def __init__(self, db_type, version):
        self.target_id = f"ACME-192-0-2-1/{db_type}/X"
        self.db_type = db_type
        self.major_version = version
        self.connection_info = {"major_version": version}


def test_the_scheduler_takes_the_interval_of_the_variant_that_runs_on_the_target(monkeypatch):
    metric = _definition("LOCK_DEADLOCK_RECENT")
    paths = {variant.db_type: variant.path for variant in metric.variants if variant.supported and variant.path}
    monkeypatch.setattr(collector, "_resolve_metric_file_path", lambda m, t: paths.get(t.db_type))

    assert collector._repeat_interval_for(metric, _Target("oracle", 19)) == 3600
    assert collector._repeat_interval_for(metric, _Target("sqlserver", 16)) == 150


def test_a_variant_interval_must_be_seconds(tmp_path):
    catalogue = json.loads(Path(DEFAULT_DEFINITIONS_PATH).read_text(encoding="utf-8"))
    for metric in catalogue["metrics"]:
        if metric["metric_code"] == "LOCK_DEADLOCK_RECENT":
            metric["variants"][0]["repeat_interval"] = 0
    path = tmp_path / "metric_definitions.json"
    path.write_text(json.dumps(catalogue), encoding="utf-8")

    with pytest.raises(Exception, match="repeat_interval must be a number of seconds"):
        load_metric_definitions(path)


# --------------------------------------------------------------------------------------------- #
# PostgreSQL, through the server log
# --------------------------------------------------------------------------------------------- #

def _run_log(tmp_path: Path, lines: list[str]) -> dict:
    logs = tmp_path / "log"
    logs.mkdir()
    (logs / "postgresql.log").write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    script = COLLECTORS / "postgresql" / "023_postgresql_server_log.sh"
    runnable = tmp_path / "collector.sh"
    runnable.write_bytes(script.read_bytes().replace(b"\r\n", b"\n"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PG_", "DOCKER_"))}
    env["PG_LOG_DIRECTORY"] = str(logs)
    out = subprocess.run([BASH, str(runnable)], capture_output=True, env=env, timeout=60)
    rows = json.loads(out.stdout.decode("utf-8"))
    return {row["metric_item"]: row for row in rows}


def _at(hours_ago: float) -> str:
    zone = timezone(timedelta(hours=7))
    return (datetime.now(zone) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S.000 +07")


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_one_postgresql_deadlock_in_the_day_warns(tmp_path):
    rows = _run_log(tmp_path, [f"{_at(6)} [311] ERROR:  deadlock detected",
                               f"{_at(6)} [311] DETAIL:  Process 311 waits for ShareLock on transaction 9001"])

    assert rows["server_log"]["status"] == "WARNING"
    assert "deadlocks=1" in rows["server_log"]["message"]
    assert rows["server_log :: deadlock"]["status"] == "LOGGING"


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_five_postgresql_deadlocks_in_the_day_are_critical_however_old(tmp_path):
    """SQL Server's rule counts the day; it has no 'recent' gate, so neither does this."""
    rows = _run_log(tmp_path, [f"{_at(h)} [{h}] ERROR:  deadlock detected" for h in (5, 6, 7, 8, 9)])

    assert rows["server_log"]["status"] == "CRITICAL"
    assert "deadlocks=5" in rows["server_log"]["message"]


def test_postgresqls_deadlock_metric_says_where_it_is_read():
    metric = _definition("LOCK_DEADLOCK_RECENT")
    postgres = next(variant for variant in metric.variants if variant.db_type == "postgresql")
    assert postgres.supported is False
    assert "POSTGRES_SERVER_LOG" in postgres.unsupported_reason
