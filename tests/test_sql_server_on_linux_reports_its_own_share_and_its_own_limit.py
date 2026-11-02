"""SQL Server on Linux sees the machine through SQLPAL, and two of its readings are not the host's.

Found reading one server's page on 2026-10-23 (0.31.0 sheet 1.190, 1.191), on both Linux
instances of the estate:

* **CPU.** The scheduler monitor's `SystemIdle` read 0 on 4,047 of 4,047 samples while
  `/proc/stat` read the host 83 % idle, so `100 - SystemIdle` printed `CPU 100 pct` on a host
  doing 17 %. Its `ProcessUtilization` - SQL Server's own share - agreed with `/proc/stat`.
* **Memory.** `dm_os_process_memory.physical_memory_in_use_kb` read 4.1 GB beside a Total Server
  Memory of 21.2 GB (the OS showed `sqlservr` at 22.3 GB), and `dm_os_sys_memory`'s total is the
  engine's own limit (`mssql-conf memory.memorylimitmb`) - 24 GB on a host of about 64. The tile
  printed `22.0 / 24.0 GB used / total · SQL Server holds 4.1 GB`.

So on Linux the CPU row grades the SQL share, the memory rows read `committed_kb` and grade the
low-memory flags only, and each row names its basis; the pages read the basis back. Windows is
unchanged - there `physical_memory_in_use_kb` matches Total Server Memory within a few percent on
every instance of the estate.
"""
from __future__ import annotations

from dbabrain.lib.paths import PACKAGE_DIR
from dbabrain.reports.inventory_health import build_sql_governance
from dbabrain.reports.server_health import build_areas

SQL = (PACKAGE_DIR / "metrics" / "collectors" / "sqlserver"
       / "012_sqlserver_cpu_memory.sql").read_text(encoding="utf-8")


def _series(code, item, last, *, unit="pct", message="", status="OK"):
    return {"code": code, "item": item, "label": code, "unit": unit, "status": status,
            "numeric": last is not None, "static": False, "last": last,
            "lastText": "" if last is None else str(last), "message": message,
            "lastAt": 1761200000, "points": [[1761200000, last, status]]}


def _areas(series):
    return {area["key"]: area for area in build_areas(series)}


# What the collector answered on the first Linux instance on 2026-10-23, after the change (collect --test-run).
LINUX_CPU = _series("SYSTEM_CPU_MEMORY", "cpu", 20.0, message=(
    "system_cpu_used_pct=N/A, sql_process_cpu_pct=20.00, system_idle_pct=N/A, cpu_basis=sql_process, "
    "reason=system_idle_not_reported_on_linux, sql_visible_cpu_count=32, scheduler_count=32"))
LINUX_SQL_MEMORY = _series("SYSTEM_CPU_MEMORY", "sql_memory", 88.01, message=(
    "sql_physical_memory_in_use_mb=4228.00, sql_committed_mb=21630.53, sql_memory_held_mb=21630.53, "
    "sql_memory_basis=committed_kb, total_physical_memory_mb=24576.00, memory_scope=sql_server_limit, "
    "sql_memory_pct=88.01, process_physical_memory_low=0"))
LINUX_SYSTEM_MEMORY = _series("SYSTEM_CPU_MEMORY", "system_memory", 92.33, message=(
    "system_memory_used_pct=92.33, total_physical_memory_mb=24576.00, available_physical_memory_mb=1884.45, "
    "memory_scope=sql_server_limit, system_low_memory_signal_state=0"))


def test_the_collector_tells_linux_from_windows_on_every_version_it_runs_on():
    # dm_os_host_info would say it directly, and does not exist before 2017; this file runs from 2012.
    assert "@@VERSION LIKE '% on Linux%'" in SQL
    assert "sys.dm_os_host_info" not in SQL


def test_on_linux_the_cpu_row_is_the_sql_share_and_says_so():
    assert "cpu_basis=sql_process" in SQL
    assert "reason=system_idle_not_reported_on_linux" in SQL
    # The SQL share is graded by the same lines the system figure had.
    assert "WHEN cpu.SQLProcessUtilization >= 95 THEN 'CRITICAL'" in SQL


def test_on_linux_what_sql_server_holds_is_its_committed_memory():
    assert "si.committed_kb" in SQL and "CROSS JOIN sys.dm_os_sys_info AS si" in SQL
    for field in ("sql_committed_mb=", "sql_memory_held_mb=", "sql_memory_basis=", "memory_scope="):
        assert field in SQL, field
    # The process figure is still written: it is what the DMV says, and older readers know it.
    assert "sql_physical_memory_in_use_mb=" in SQL


def test_on_linux_a_percentage_of_the_engines_own_limit_is_not_graded():
    # SQL Server fills its allowance by design; the flags still grade.
    assert SQL.count("WHEN mem.is_linux = 1 THEN 'OK'") == 2
    assert "WHEN mem.process_physical_memory_low = 1 THEN 'CRITICAL'" in SQL
    assert "WHEN mem.system_low_memory_signal_state = 1 THEN 'CRITICAL'" in SQL


def test_the_memory_tile_names_the_limit_for_what_it_is_and_what_sql_server_holds():
    memory = _areas([LINUX_SQL_MEMORY, LINUX_SYSTEM_MEMORY])["memory"]

    assert memory["capacity"] == ("22.2 / 24.0 GB used / SQL Server's limit · 1.8 GB available "
                                  "· SQL Server holds 21.1 GB")


def test_with_the_host_collected_the_memory_tile_reads_the_hosts_ram():
    host = _series("OS_MEMORY_USAGE", "memory_usage", 55.0, unit="percent",
                   message="Memory usage is 55 percent. total_gb=62.8, used_gb=34.5, available_gb=28.3")

    memory = _areas([host, LINUX_SQL_MEMORY, LINUX_SYSTEM_MEMORY])["memory"]

    assert memory["capacity"] == "34.5 / 62.8 GB used / total · 28.3 GB available · SQL Server holds 21.1 GB"


def test_a_row_from_before_the_change_still_states_what_it_has():
    old = _series("SYSTEM_CPU_MEMORY", "sql_memory", 17.2, message=(
        "sql_physical_memory_in_use_mb=4228.00, total_physical_memory_mb=24576.00, sql_memory_pct=17.20, "
        "process_physical_memory_low=0"))
    old_system = _series("SYSTEM_CPU_MEMORY", "system_memory", 92.82, message=(
        "system_memory_used_pct=92.82, total_physical_memory_mb=24576.00, "
        "available_physical_memory_mb=1765.61, system_low_memory_signal_state=0"))

    memory = _areas([old, old_system])["memory"]

    assert memory["capacity"] == "22.3 / 24.0 GB used / total · 1.7 GB available · SQL Server holds 4.1 GB"


def test_the_cpu_tile_says_the_engine_share_is_not_the_hosts():
    cpu = _areas([LINUX_CPU])["cpu"]

    assert cpu["value"] == "20%"
    assert cpu["capacity"] == "32 / 32 logical CPUs online / total"
    assert cpu["capacityNote"] == ("SQL Server on Linux does not report the host's idle time: this is "
                                   "SQL Server's own share of the CPU, not the host's.")


def test_with_the_host_collected_the_cpu_tile_shows_the_host_and_needs_no_such_note():
    host = _series("OS_CPU_USAGE", "cpu_usage", 16.7, unit="percent",
                   message="Average CPU usage is 16.7 percent. model=x, sockets=2, cores=16, logical_cpus=32")

    cpu = _areas([host, LINUX_CPU])["cpu"]

    assert cpu["sourceCode"] == "OS_CPU_USAGE"
    assert cpu["capacity"] == "32 / 32 logical CPUs online / total · 2 sockets, 16 cores, 2 threads per core"
    assert cpu["capacityNote"] == ""


def _code_map(rows):
    return {(row["metric_code"], row["metric_item"]): row for row in rows}


def _row(item, message):
    return {"metric_code": "SYSTEM_CPU_MEMORY", "metric_item": item, "metric_value": "0",
            "message": message, "collected_at": "2026-10-23T06:03:26Z", "status": "OK"}


def test_the_inventory_does_not_take_the_engines_limit_for_the_hosts_ram():
    gov = build_sql_governance(_code_map([_row("sql_memory", LINUX_SQL_MEMORY["message"]),
                                          _row("system_memory", LINUX_SYSTEM_MEMORY["message"])]))

    assert "host_physical_memory_gb" not in gov["memory"]
    assert gov["memory"]["sql_committed_mb"] == 21630.53


def test_on_windows_the_inventory_reads_the_host_as_before():
    windows = ("sql_physical_memory_in_use_mb=114890.68, sql_committed_mb=114689.77, "
               "sql_memory_held_mb=114890.68, sql_memory_basis=physical_memory_in_use_kb, "
               "total_physical_memory_mb=131070.94, memory_scope=host, sql_memory_pct=87.66, "
               "process_physical_memory_low=0")

    gov = build_sql_governance(_code_map([_row("sql_memory", windows)]))

    assert gov["memory"]["host_physical_memory_gb"] == 128.0
    assert gov["memory"]["sql_committed_mb"] == 114890.68
