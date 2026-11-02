"""A Current status tile says what its percentage is a percentage *of*.

"CPU 5.12%", "Memory 63%", "Disk space 83%": each tile carried a share and none of the whole, so a
reader opened the charts to learn whether 83% full was a crisis or a month of headroom. On the
instance that raised it (2026-10-16) the CPU tile hid the most consequential fact on the page: the
host has 60 logical CPUs and SQL Server Standard may use 48 of them, so the engine at full load
reads about 80% on a tile whose CRITICAL line is 90.

The collectors already write the totals into their messages - logical CPUs and sockets, total and
available memory, each volume's size and free space - on every engine and platform. The tiles read
them back; nothing is collected twice.

Read back wrongly, a total is worse than none. On 2026-10-20 a 32-CPU host read
``32 CPUs · 3 sockets × 17 cores`` - 51 cores: the collectors' ``cores`` is the host's total, and
the tile printed it as cores per socket. Every test here had one socket, where the two agree.
The same morning ``32 CPUs`` stood beside another server's ``48 / 60``, and could not say whether
the engine had been asked at all.

The fix for that chose between two forms - ``1 socket × 30 cores`` where the total divided by the
sockets, ``3 sockets, 17 cores`` where it did not - and on 2026-10-26 two tiles side by side read
differently for it (asked by the operator: *don't hard-code, show what is real, don't guess*). The
``×`` claimed every socket alike, and the guest behind ``3 sockets, 17 cores`` has 8 + 6 + 3. One
form now, from what the collector wrote socket by socket.
"""

from __future__ import annotations

from dbabrain.reports.server_health import build_areas


def _series(code, item, last, *, unit="percent", message="", status="OK"):
    return {"code": code, "item": item, "label": code, "unit": unit, "status": status,
            "numeric": last is not None, "static": False, "last": last,
            "lastText": "" if last is None else str(last), "message": message,
            "lastAt": 1760600000, "points": [[1760600000, last, status]]}


CPU = _series("OS_CPU_USAGE", "cpu_usage", 5.12,
              message="Average CPU usage is 5.12 percent. model=Intel(R) Xeon(R) Gold 6246 CPU @ 3.30GHz, "
                      "sockets=1, cores=30, logical_cpus=60")
MEMORY = _series("OS_MEMORY_USAGE", "memory_usage", 63.55,
                 message="Memory usage is 63.55 percent. total_gb=400, used_gb=254.2, available_gb=145.8")
SQL_MEMORY = _series("SYSTEM_CPU_MEMORY", "sql_memory", 59.89, unit="pct",
                     message="sql_physical_memory_in_use_mb=245306.45, total_physical_memory_mb=409598.94")
DISKS = [
    _series("OS_DISK_USAGE", "C:", 67.22,
            message="C: usage is 67.22 percent. total_gb=99.4, used_gb=66.81, free_gb=32.59, free_percent=32.78"),
    _series("OS_DISK_USAGE", "F:", 83.17,
            message="F: usage is 83.17 percent. total_gb=1023.98, used_gb=851.6, free_gb=172.38, free_percent=16.83"),
    # The same volume seen by the instance: one volume, not two.
    _series("STORAGE_DISK_FREE_SPACE", "F:\\", 172.38, unit="GB",
            message="drive=F:\\, free_gb=172.38, total_gb=1023.98, free_pct=16.83, used_pct=83.17"),
]
WORKLOAD = {"cpu": {"total": 60, "online": 48, "engine": "SQL Server",
                    "edition": "Standard Edition (64-bit)",
                    "limit": "SQL Server Standard uses at most the lesser of 4 sockets or 24 cores"}}


def _areas(series, workload=None):
    return {area["key"]: area for area in build_areas(series, workload=workload)}


def test_the_cpu_tile_states_the_cpus_the_engine_may_use_of_the_hosts():
    cpu = _areas([CPU], WORKLOAD)["cpu"]

    assert cpu["capacity"] == "48 / 60 logical CPUs online / total · 1 socket, 30 cores, 2 threads per core"
    # The consequence, in the tile's own terms.
    assert cpu["capacityNote"] == ("SQL Server Standard uses at most the lesser of 4 sockets or "
                                   "24 cores, so SQL Server saturated reads about 80% on this tile.")


def test_an_engine_that_may_use_every_cpu_states_the_pair_and_no_consequence():
    cpu = _areas([CPU], {"cpu": {"total": 60, "online": 60}})["cpu"]

    assert cpu["capacity"] == "60 / 60 logical CPUs online / total · 1 socket, 30 cores, 2 threads per core"
    assert cpu["capacityNote"] == ""


def test_a_host_whose_engine_was_not_asked_says_only_how_many_cpus_it_has():
    assert _areas([CPU])["cpu"]["capacity"] == "60 logical CPUs · 1 socket, 30 cores, 2 threads per core"


def test_the_schedulers_are_read_from_the_engine_row_when_the_workload_has_none():
    engine = _series("SYSTEM_CPU_MEMORY", "cpu", 23.0, unit="pct",
                     message="system_cpu_used_pct=23.00, sql_process_cpu_pct=21.00, system_idle_pct=77.00, "
                             "sql_visible_cpu_count=32, scheduler_count=32")
    host = _series("OS_CPU_USAGE", "cpu_usage", 22.76,
                   message="Average CPU usage is 22.76 percent. model=Intel(R) Xeon(R) Gold, "
                           "sockets=3, cores=17, logical_cpus=32")

    assert _areas([host, engine])["cpu"]["capacity"] == "32 / 32 logical CPUs online / total · 3 sockets, 17 cores"


def test_sockets_of_different_sizes_are_stated_as_the_collector_counted_them():
    host = _series("OS_CPU_USAGE", "cpu_usage", 8.0,
                   message="Average CPU usage is 8 percent. model=x, sockets=3, cores=17, "
                           "cores_per_socket=8+6+3, logical_per_socket=14+12+6, logical_cpus=32")

    assert _areas([host])["cpu"]["capacity"] == "32 logical CPUs · 3 sockets, 17 cores (8 + 6 + 3)"


def test_one_form_for_every_host_whether_or_not_the_cores_divide():
    """``2 sockets × 16 cores`` was an inference: nothing in the row said the sockets were alike."""
    host = _series("OS_CPU_USAGE", "cpu_usage", 10.0,
                   message="Average CPU usage is 10 percent. model=x, sockets=2, cores=32, logical_cpus=64")

    assert _areas([host])["cpu"]["capacity"] == "64 logical CPUs · 2 sockets, 32 cores, 2 threads per core"


def test_a_host_with_no_socket_count_gets_none_invented():
    host = _series("OS_CPU_USAGE", "cpu_usage", 10.0,
                   message="Average CPU usage is 10 percent. model=x, logical_cpus=8")

    assert _areas([host])["cpu"]["capacity"] == "8 logical CPUs"


def test_the_memory_tile_states_used_of_total_and_what_the_engine_holds():
    memory = _areas([MEMORY, SQL_MEMORY])["memory"]

    assert memory["capacity"] == "254 / 400 GB used / total · 146 GB available · SQL Server holds 240 GB"


def test_a_container_states_its_memory_against_its_limit():
    container = _series("DOCKER_CONTAINER_STATS", "pg-store:memory", 1.45,
                        message="mem_usage=464.2MiB / 31.25GiB mem_pct=1.45%")

    memory = _areas([container])["memory"]

    assert memory["capacity"] == "464.2MiB / 31.25GiB used / container limit"


def test_the_disk_tile_states_the_worst_volume_and_every_volume_together():
    disk = _areas(DISKS)["disk_space"]

    assert disk["capacity"].startswith("F: 852 / 1,024 GB used / total · 172 GB free")
    assert "2 volumes, 205 GB free of 1.10 TB" in disk["capacity"]


def test_a_tile_whose_collector_wrote_no_totals_says_nothing_rather_than_guessing():
    bare = _series("OS_CPU_USAGE", "cpu_usage", 12.0, message="Average CPU usage is 12 percent.")

    assert _areas([bare])["cpu"]["capacity"] == ""


def test_the_workload_sections_print_the_pair_wherever_the_engine_said_it():
    """The tile prints `32 / 32` since 0.30.0's 1.185; the Workload sections of both pages still
    printed the pair only when the counts differed, so the same server read `32 / 32` on its tile
    and `32 CPUs` two sections below (0.31.0)."""
    from dbabrain.reports.inventory_report import TEMPLATE_HTML as INVENTORY
    from dbabrain.reports.server_report import TEMPLATE_HTML as SERVER

    server = SERVER.read_text(encoding="utf-8")
    inventory = INVENTORY.read_text(encoding="utf-8")
    assert "c.online ? ` · ${c.online} of ${c.total} CPUs online`" in server
    assert "c.online ? `, ${c.online} of ${total} CPUs online`" in inventory
    assert "c.online < c.total ?" not in server and "c.online < total ?" not in inventory

