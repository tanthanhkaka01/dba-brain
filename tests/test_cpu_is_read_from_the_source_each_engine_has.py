"""The Workload table's CPU comes from whatever this engine and edition can actually count.

On 2026-10-16 a 2019 Standard ERP instance showed "CPU used 0.0%" and "CPU time 0 ms" over every
window while its host read 26% busy. The rows were differenced from
`sys.dm_resource_governor_resource_pools`, and Resource Governor is an Enterprise feature: on
Standard and Express the DMV lists the internal pool alone and its total never moves - 0, 0, 0, 1
and 3 ms since startup on the five such instances of the estate. The page subtracted a flat zero
and printed it as a measurement.

The CPU is still knowable on every engine, from different places: the engine's own total where it
keeps one (the resource pools on Enterprise, v$sys_time_model on Oracle), the host's account of
the database process (OS_CPU_COUNTERS - the only CPU PostgreSQL has, since it keeps none), or the
scheduler monitor's per-minute samples, which every SQL Server edition writes. Each row takes the
best of those this target has and names it; a missing source is a sentence on the page, never 0.

And the percentage is taken of the CPUs the engine may use. The same instance runs on 48 of the
host's 60 logical CPUs - Standard stops at 24 cores - so "% of 60" read 80 with the engine
saturated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbabrain.metrics import collector
from dbabrain.metrics.definitions import DEFAULT_DEFINITIONS_PATH, load_metric_definitions
from dbabrain.reports import workload

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
STAMPS = [f"2026-10-16T{8 + minute // 60:02d}:{minute % 60:02d}:00+00:00"
          for minute in (0, 15, 30, 45, 60)]


def _rows(code, stamp, values, **fields):
    """One collection of one metric, with the message fields its collector writes."""
    sources = fields.pop("sources", {})
    carried = ", ".join(f"{key}={value}" for key, value in fields.items())
    return [
        {"metric_code": code, "metric_item": item, "metric_value": str(value),
         "collected_at": stamp,
         "message": f"value={value}, {carried}, source={sources.get(item, 'dm_os_performance_counters')}"}
        for item, value in values.items()
    ]


def _standard_hour(**extra_per_step):
    """An hour of a 2019 Standard instance: no pool CPU, the scheduler monitor at 21.87% of the host."""
    rows = []
    for step, stamp in enumerate(STAMPS):
        values = {"batch_requests": 90_000 * step, "sql_cpu_pct_sampled": 21.87,
                  "host_cpu_pct_sampled": 23.53, "cpu_sample_minutes": 15}
        values.update({key: value * step for key, value in extra_per_step.items()})
        rows += _rows(workload.COUNTER_CODE, stamp, values,
                      counters_since="2026-09-27 18:43:51", cpu_count=60, schedulers_online=48,
                      pools_account_cpu=0, edition="Standard Edition (64-bit)",
                      sources={"sql_cpu_pct_sampled": "dm_os_ring_buffers",
                               "host_cpu_pct_sampled": "dm_os_ring_buffers",
                               "cpu_sample_minutes": "dm_os_ring_buffers"})
    return rows


def _os_hour(*, db_per_step=None, process_since="2026-09-27 18:43:51", restart_at=None,
             logical=60, process="sqlservr.exe pid 12652 on port 1433"):
    """An hour of OS_CPU_COUNTERS: the host 25% busy, the database process as given."""
    rows = []
    for step, stamp in enumerate(STAMPS):
        values = {"host_busy_ms": 900_000 * logical * step // 4,
                  "host_idle_ms": 900_000 * logical * step * 3 // 4}
        since = process_since
        if db_per_step is not None:
            values["db_cpu_ms"] = db_per_step * step
            if restart_at is not None and step >= restart_at:
                since = "2026-10-16 08:35:00"
                values["db_cpu_ms"] = db_per_step * (step - restart_at)
        rows += _rows(workload.OS_CPU_CODE, stamp, values,
                      counters_since="2026-09-27 18:35:54", logical_cpus=logical,
                      process_since=since if db_per_step is not None else "none",
                      db_process=process if db_per_step is not None else "none: no postgres process on this host",
                      sources={"host_busy_ms": "GetSystemTimes", "host_idle_ms": "GetSystemTimes",
                               "db_cpu_ms": "GetSystemTimes"})
    return rows


def _cpu_rows(block):
    return {row["key"]: row for group in block["groups"] if group["name"] == "CPU"
            for row in group["rows"]}


# --------------------------------------------------------------------------------------------- #
# The engine and its edition
# --------------------------------------------------------------------------------------------- #

def test_a_standard_instance_reads_its_cpu_from_the_scheduler_monitor_and_says_it_is_a_sample():
    block = workload.build_workload(_standard_hour())

    cpu = _cpu_rows(block)
    pct = cpu["cpu_pct"]["values"]["hour"]
    # 21.87% of 60 CPUs is 13.1 CPUs busy, which is 27.3% of the 48 the engine may use.
    assert pct["total"] == pytest.approx(21.87 * 60 / 48, abs=0.01)
    assert pct["estimate"] is True
    assert "scheduler monitor" in cpu["cpu_pct"]["source"]
    assert cpu["cpu_usage_ms"]["values"]["hour"]["perSecond"] == pytest.approx(0.2187 * 60 * 1000, rel=1e-3)
    assert "Resource Governor" in block["cpuNote"]


def test_the_percentage_is_of_the_cpus_the_engine_may_use_and_the_header_says_how_many():
    block = workload.build_workload(_standard_hour())

    assert block["cpu"]["online"] == 48
    assert block["cpu"]["total"] == 60
    assert "24 cores" in block["cpu"]["limit"]


def test_an_enterprise_instance_keeps_its_exact_pool_total():
    rows = []
    for step, stamp in enumerate(STAMPS):
        rows += _rows(workload.COUNTER_CODE, stamp,
                      {"cpu_usage_ms": 7_200_000 * step, "sql_cpu_pct_sampled": 3.0,
                       "cpu_sample_minutes": 15},
                      counters_since="2026-06-22 12:55:17", cpu_count=8, schedulers_online=8,
                      pools_account_cpu=1, edition="Enterprise Edition (64-bit)",
                      sources={"cpu_usage_ms": "dm_resource_governor_resource_pools",
                               "sql_cpu_pct_sampled": "dm_os_ring_buffers",
                               "cpu_sample_minutes": "dm_os_ring_buffers"})

    cpu = _cpu_rows(workload.build_workload(rows))

    assert cpu["cpu_pct"]["values"]["hour"]["total"] == 100.0
    assert "estimate" not in cpu["cpu_pct"]["values"]["hour"]
    assert "resource pools" in cpu["cpu_usage_ms"]["source"]


def test_the_hosts_count_of_the_process_wins_over_the_sample_where_both_exist():
    rows = _standard_hour() + _os_hour(db_per_step=10_000_000)

    block = workload.build_workload(rows)
    cpu = _cpu_rows(block)

    assert cpu["cpu_usage_ms"]["values"]["hour"]["total"] == 40_000_000
    assert "estimate" not in cpu["cpu_usage_ms"]["values"]["hour"]
    assert "sqlservr.exe" in cpu["cpu_usage_ms"]["source"]
    # 40,000,000 ms over an hour on 48 CPUs.
    assert cpu["cpu_pct"]["values"]["hour"]["total"] == pytest.approx(40_000_000 / (3600 * 48 * 10), abs=0.01)
    assert cpu["host_cpu_pct"]["values"]["hour"]["total"] == 25.0
    assert block["cpuNote"] == ""


def test_a_service_restart_costs_the_process_pair_and_not_the_hosts():
    rows = _os_hour(db_per_step=10_000_000, restart_at=2)

    cpu = _cpu_rows(workload.build_workload(rows))

    # The process's hour reaches only as far back as the restart - two quarters, not four - while
    # the host, which did not restart, still has all of it.
    assert cpu["cpu_usage_ms"]["values"]["hour"]["total"] == 20_000_000
    assert cpu["cpu_usage_ms"]["values"]["hour"]["perSecond"] == round(20_000_000 / 1800, 3)
    assert cpu["host_cpu_pct"]["values"]["hour"]["total"] == 25.0


def test_an_oracle_instance_reads_its_own_cpu_and_its_hosts_from_the_engine():
    rows = []
    for step, stamp in enumerate(STAMPS):
        rows += _rows(workload.COUNTER_CODE, stamp,
                      {"cpu_usage_ms": 3_600_000 * step, "host_busy_ms": 1_800_000 * step,
                       "host_idle_ms": 5_400_000 * step},
                      counters_since="2026-10-02 09:38:03", cpu_count=32, schedulers_online=16,
                      edition="Oracle Database 19c Standard Edition 2 Release 19.0.0.0.0 - Production",
                      sources={"cpu_usage_ms": "v$sys_time_model", "host_busy_ms": "v$osstat",
                               "host_idle_ms": "v$osstat"})

    block = workload.build_workload(rows)
    cpu = _cpu_rows(block)

    assert block["cpu"]["engine"] == "Oracle"
    assert "16 CPU threads" in block["cpu"]["limit"]
    assert cpu["cpu_pct"]["values"]["hour"]["total"] == pytest.approx(14_400_000 / (3600 * 16 * 10), abs=0.01)
    assert "v$sys_time_model" in cpu["cpu_usage_ms"]["source"]
    assert cpu["host_cpu_pct"]["values"]["hour"]["total"] == 25.0
    assert "v$osstat" in cpu["host_cpu_pct"]["source"]


def test_postgresql_has_a_workload_block_made_of_its_hosts_count_alone():
    rows = _os_hour(db_per_step=5_000_000, logical=8, process="postgres pid 1071890",
                    process_since="2026-08-19 00:30:59")

    block = workload.build_workload(rows)

    assert block["available"] is True
    assert block["cpu"]["engine"] == "PostgreSQL"
    assert block["cpu"]["online"] == block["cpu"]["total"] == 8
    assert set(_cpu_rows(block)) == {"cpu_pct", "cpu_usage_ms", "host_cpu_pct"}


def test_a_host_that_cannot_tell_which_process_is_the_database_says_so_and_shows_the_host():
    rows = _os_hour(logical=8)
    for row in rows:
        row["message"] = row["message"].replace(
            "db_process=none: no postgres process on this host",
            "db_process=none: 3 postgres root processes and none names port 5433")

    block = workload.build_workload(rows)

    assert set(_cpu_rows(block)) == {"host_cpu_pct"}
    assert "3 postgres root processes" in block["cpuNote"]


def test_nothing_collected_is_not_waiting_for_a_second_collection():
    block = workload.build_workload([])

    assert block["available"] is False
    assert block["samples"] == 0


def test_a_flat_zero_left_by_the_old_collector_does_not_outlive_the_first_new_collection():
    """Two days of stored rows hold cpu_usage_ms=0 from before the collector stopped writing it on
    Standard. An item missing from the newer collections is not differenced at all, so the first
    collection without it is enough for the row to come from a real source."""
    rows = _standard_hour()
    for stamp in ("2026-10-16T07:30:00+00:00", "2026-10-16T07:45:00+00:00"):
        rows += _rows(workload.COUNTER_CODE, stamp, {"cpu_usage_ms": 0, "batch_requests": 0},
                      counters_since="2026-09-27 18:43:51", cpu_count=60,
                      sources={"cpu_usage_ms": "dm_resource_governor_resource_pools"})

    cpu = _cpu_rows(workload.build_workload(rows))

    assert cpu["cpu_pct"]["values"]["hour"]["estimate"] is True


# --------------------------------------------------------------------------------------------- #
# The collectors
# --------------------------------------------------------------------------------------------- #

def _sql(relative: str) -> str:
    return (COLLECTORS / relative).read_text(encoding="utf-8")


def test_the_sql_server_collector_writes_pool_cpu_only_where_the_pools_count_it():
    text = _sql("sqlserver/072_sqlserver_workload_counters.sql")
    assert "WHERE name = 'default'" in text
    assert "WHERE @cpu_ms IS NOT NULL AND @pools_account_cpu = '1'" in text
    assert "schedulers_online=" in text and "status = 'VISIBLE ONLINE'" in text
    # The scheduler monitor's host half is wrong on Linux and is not written there.
    assert "@@VERSION NOT LIKE '%on Linux%'" in text


def test_the_oracle_collectors_read_the_views_their_version_has():
    modern = _sql("oracle/081_oracle_workload_counters.sql")
    assert "v$sys_time_model" in modern and "v$osstat" in modern
    legacy = _sql("oracle/legacy_8i/081_oracle_workload_counters.sql")
    code = "\n".join(line for line in legacy.splitlines() if not line.lstrip().startswith("--"))
    assert "v$sysstat" in code and "v$osstat" not in code and "v$sys_time_model" not in code
    comments = [line for line in legacy.splitlines() if line.lstrip().startswith("--")]
    # The 8i bridge finds the end of a statement itself; a quote or semicolon in a comment breaks it.
    assert not any("'" in line or ";" in line for line in comments)


def test_the_os_collector_ships_for_both_platforms_with_unix_line_endings_for_linux():
    linux = (COLLECTORS / "os" / "linux" / "015_os_cpu_counters.sh").read_bytes()
    assert b"\r\n" not in linux
    assert linux.startswith(b"#!/bin/bash")
    windows = (COLLECTORS / "os" / "windows" / "016_os_cpu_counters.ps1").read_text(encoding="utf-8")
    assert "GetSystemTimes" in windows and "sqlservr.exe" in windows


def test_the_catalogue_wires_the_new_collectors_and_keeps_them_ungraded():
    definitions = {item.metric_code: item for item in load_metric_definitions(DEFAULT_DEFINITIONS_PATH)}
    counters = definitions["OS_CPU_COUNTERS"]
    assert {variant.platform for variant in counters.variants} == {"windows", "linux"}
    assert counters.report_policy.get("collect_only") is True
    assert set(counters.target_env) == {"DBABRAIN_DB_TYPE", "DBABRAIN_DB_PORT", "DBABRAIN_DB_CONTAINER"}
    oracle = [variant for variant in definitions["PERFORMANCE_WORKLOAD_COUNTERS"].variants
              if variant.db_type == "oracle"]
    assert {variant.path.name for variant in oracle if variant.path} == {"081_oracle_workload_counters.sql"}


# --------------------------------------------------------------------------------------------- #
# What a cmd collector is told about the database
# --------------------------------------------------------------------------------------------- #

class _Target:
    target_id = "ACME-192-0-2-115/sqlserver/SALESDB-PROD"
    ip = "192.0.2.115"
    cmd_access = {"enabled": True, "method": "winrm", "host": "192.0.2.115"}
    metrics_config: dict = {}
    db_type = "sqlserver"
    port = 1433
    container_name = ""


class _Metric:
    def __init__(self, code, target_env=()):
        self.metric_code = code
        self.target_env = target_env


def test_only_a_collector_that_declares_it_is_told_which_database_is_on_the_host():
    declared = collector._collector_env(
        metric=_Metric("OS_CPU_COUNTERS", ("DBABRAIN_DB_TYPE", "DBABRAIN_DB_PORT", "DBABRAIN_DB_CONTAINER")),
        target=_Target())
    undeclared = collector._collector_env(metric=_Metric("OS_CPU_USAGE"), target=_Target())

    assert declared["DBABRAIN_DB_TYPE"] == "sqlserver"
    assert declared["DBABRAIN_DB_PORT"] == "1433"
    # An empty fact is absent, so the script tells "not set" by presence.
    assert "DBABRAIN_DB_CONTAINER" not in declared
    assert undeclared == {"DBABRAIN_TARGET_HOST": "192.0.2.115"}


def test_a_catalogue_cannot_hand_a_script_a_target_field_nobody_reviewed(tmp_path):
    catalogue = json.loads(Path(DEFAULT_DEFINITIONS_PATH).read_text(encoding="utf-8"))
    for metric in catalogue["metrics"]:
        if metric["metric_code"] == "OS_CPU_COUNTERS":
            metric["target_env"] = ["DBABRAIN_DB_TYPE", "DBABRAIN_DB_PASSWORD"]
    path = tmp_path / "metric_definitions.json"
    path.write_text(json.dumps(catalogue), encoding="utf-8")

    with pytest.raises(Exception, match="target_env"):
        load_metric_definitions(path)
