"""OS-only host monitoring: collector inputs, the os_health overlay, and how a host with no
database is reported.

The fixtures under tests/fixtures/os_metrics are the rows the cmd collectors actually return
(the JSON array each .ps1/.sh prints to stdout), so these tests exercise the real collection
path shape rather than values invented for the test.
"""

import json
from pathlib import Path

import pytest

from db_ops.metrics.collector import _collector_env, _script_with_env, _shell_prelude
from db_ops.metrics.definitions import load_metric_definitions
from conftest import shipped_config

# The catalogue that *ships*, not the one this estate happens to have. Reading
# `DEFAULT_DEFINITIONS_PATH` meant the OS metric set was whatever the developer's
# `data/metric_definitions.json` said, and there is no such file in an exported tree — so
# these assertions passed here and raised StopIteration anywhere else.
DEFAULT_DEFINITIONS_PATH = shipped_config("metric_definitions.json")
from db_ops.metrics.models import MetricDefinition, MetricTarget
from db_ops.reports.inventory_health import (
    build_os_health, build_inventory_status, merged_drives, parse_os_kv,
)
from db_ops.reports.inventory_report import build_models, build_triage


FIXTURES = Path(__file__).parent / "fixtures" / "os_metrics"


def load_fixture(name: str) -> dict:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return {key: value for key, value in data.items() if not key.startswith("_")}


def code_map(fixture: dict, *, collected_at: str = "2026-07-13T07:30:00Z") -> dict:
    """The {(metric_code, metric_item): row} map inventory_health builds from metric_results."""
    out = {}
    for metric_code, rows in fixture.items():
        for row in rows:
            out[(metric_code, row["metric_item"])] = {**row, "metric_code": metric_code,
                                                      "collected_at": collected_at}
    return out


def server(fixture_name: str, *, ip: str, server_id: str, databases: list, role: str = "") -> dict:
    entry = {"server_id": server_id, "company_code": "ACME", "ip": ip, "databases": databases,
             "os_health": build_os_health(code_map(load_fixture(fixture_name)))}
    if role:
        entry["role"] = role
    return entry


def _definition(metric_code: str) -> MetricDefinition:
    return next(item for item in load_metric_definitions(DEFAULT_DEFINITIONS_PATH)
                if item.metric_code == metric_code)


def _target(metrics_config: dict) -> MetricTarget:
    return MetricTarget(
        target_id="ACME-192-0-2-116/ERP-WINHOST01", server_id="ACME-192-0-2-116",
        ip="192.0.2.116", db_type="", db_name="ERP-WINHOST01", credential_name="",
        platform="windows", metrics_config=metrics_config,
    )


# --------------------------------------------------------------------------- #
# Metric catalogue
# --------------------------------------------------------------------------- #
def test_os_metrics_have_windows_and_linux_variants_that_exist():
    definitions = [item for item in load_metric_definitions(DEFAULT_DEFINITIONS_PATH, active_only=True)
                   if item.metric_code.startswith("OS_")]
    codes = {item.metric_code for item in definitions}
    assert {"OS_INFO", "OS_CPU_USAGE", "OS_MEMORY_USAGE", "OS_DISK_USAGE", "OS_NETWORK",
            "OS_PROCESS_TOP_CPU", "OS_PROCESS_TOP_MEMORY", "OS_SERVICE_STATUS",
            "OS_EVENTLOG_CRITICAL", "OS_REBOOT_PENDING"} <= codes
    for item in definitions:
        platforms = {variant.platform for variant in item.variants}
        assert platforms == {"windows", "linux"}, item.metric_code
        for variant in item.variants:
            assert variant.path is not None and variant.path.exists(), f"{item.metric_code}/{variant.name}"


def test_os_top_processes_is_retired_not_deleted():
    """Superseded by OS_PROCESS_TOP_CPU / OS_PROCESS_TOP_MEMORY. Kept inactive so its history
    stays readable; it must not collect any more."""
    retired = _definition("OS_TOP_PROCESSES")
    assert retired.active is False
    active_codes = {item.metric_code for item in load_metric_definitions(DEFAULT_DEFINITIONS_PATH, active_only=True)}
    assert "OS_TOP_PROCESSES" not in active_codes


# --------------------------------------------------------------------------- #
# Per-target collector inputs
# --------------------------------------------------------------------------- #
def test_collector_env_merges_target_wide_and_per_metric_config():
    target = _target({
        "collector_env": {"OS_TOP_N": "5"},
        "metric_overrides": {"OS_SERVICE_STATUS": {"collector_env": {"OS_SERVICE_NAMES": "*Dynamics AX Object Server*"}}},
    })
    # DB_OPS_TARGET_HOST is injected into every cmd collector rather than declared — it is the
    # address the inventory already reaches this host on, so a script never restates it.
    service_env = _collector_env(metric=_definition("OS_SERVICE_STATUS"), target=target)
    assert service_env == {"DB_OPS_TARGET_HOST": "192.0.2.116", "OS_TOP_N": "5",
                           "OS_SERVICE_NAMES": "*Dynamics AX Object Server*"}
    # The per-metric block must not leak into other metrics.
    assert _collector_env(metric=_definition("OS_CPU_USAGE"), target=target) == {
        "DB_OPS_TARGET_HOST": "192.0.2.116", "OS_TOP_N": "5"}


def test_collector_env_per_metric_overrides_target_wide():
    target = _target({
        "collector_env": {"OS_TOP_N": "5"},
        "metric_overrides": {"OS_PROCESS_TOP_CPU": {"collector_env": {"OS_TOP_N": "10"}}},
    })
    assert _collector_env(metric=_definition("OS_PROCESS_TOP_CPU"), target=target)["OS_TOP_N"] == "10"


def test_collector_env_refuses_to_carry_secrets():
    target = _target({"collector_env": {"DB_PASSWORD": "hunter2"}})
    with pytest.raises(RuntimeError, match="must not carry secrets"):
        _collector_env(metric=_definition("OS_CPU_USAGE"), target=target)


def test_shell_prelude_quotes_values_for_each_shell():
    env = {"OS_SERVICE_NAMES": "It's here,W32Time"}
    assert _shell_prelude(env, powershell=True) == "$env:OS_SERVICE_NAMES = 'It''s here,W32Time'\n"
    assert _shell_prelude(env, powershell=False) == "export OS_SERVICE_NAMES='It'\\''s here,W32Time'\n"
    assert _shell_prelude({}, powershell=True) == ""


def test_script_with_env_prepends_assignments_to_the_real_script(tmp_path):
    script = tmp_path / "005.ps1"
    script.write_text("$names = $env:OS_SERVICE_NAMES\n", encoding="utf-8")
    text = _script_with_env(script, {"OS_SERVICE_NAMES": "W32Time"}, powershell=True)
    assert text == "$env:OS_SERVICE_NAMES = 'W32Time'\n$names = $env:OS_SERVICE_NAMES\n"


# --------------------------------------------------------------------------- #
# os_health overlay
# --------------------------------------------------------------------------- #
def test_windows_os_only_overlay_carries_every_required_field():
    health = build_os_health(code_map(load_fixture("windows_os_only_winhost01")))

    info = health["os_info"]
    assert info["os_family"] == "Windows"
    assert info["os_name"] == "Microsoft Windows Server 2019 Datacenter"
    assert info["build"] == "17763" and info["architecture"] == "64-bit"
    assert info["hostname"] == "ERP-WINHOST01"
    assert info["timezone"] == "SE Asia Standard Time"
    assert info["last_boot_time"] == "2026-06-28T01:14:07Z"
    assert info["uptime_seconds"] == 1315853

    cpu = health["cpu"]
    assert (cpu["usage_percent"], cpu["sockets"], cpu["cores"], cpu["logical_cpus"]) == (18.42, 2, 8, 16)
    assert cpu["processor_queue_length"] == 1.0

    memory = health["memory"]
    assert (memory["total_gb"], memory["used_gb"], memory["usage_percent"]) == (64.0, 28.03, 43.8)
    assert memory["swap_total_gb"] == 8.0

    assert health["disks"]["C:"]["free_percent"] == 40.0
    assert health["disks"]["D:"]["file_system_type"] == "NTFS"
    assert "disk_queue_length" not in health["disks"]

    assert health["network"][0]["interface"] == "Ethernet0"
    assert health["network"][0]["ip_address"] == "192.0.2.116"
    assert health["network"][0]["speed_mbps"] == 10000

    assert health["services"][0]["state"] == "Running"
    assert health["top_cpu"][0] == {"process": "Ax32Serv", "cpu_percent": 42.11, "memory_mb": 11059.2,
                                    "memory_percent": None, "process_count": None}
    assert health["top_memory"][0]["memory_mb"] == 11059.2
    assert health["pending_reboot"] == "NO"


def test_linux_os_only_overlay_uses_load_average_and_swap():
    health = build_os_health(code_map(load_fixture("linux_os_only_ubuntu")))
    assert health["os_info"]["os_family"] == "Linux"
    assert health["os_info"]["os_name"] == "Ubuntu 22.04.4 LTS"
    assert health["cpu"]["load_average_1m"] == 0.42
    assert "processor_queue_length" not in health["cpu"]
    assert health["memory"]["swap_total_gb"] == 4.0
    assert health["disks"]["/var"]["free_percent"] == 12.0
    assert "disk_iops" not in health["disks"]
    assert health["top_memory"][0]["process_count"] == 4
    assert health["pending_reboot"] == "YES"


def test_a_renamed_service_does_not_linger_as_a_ghost_row():
    """code_map keeps the newest row per (code, item) across the whole window. When
    OS_SERVICE_NAMES changed from a wrong pattern to FabricHostSvc on the AOS hosts, the old
    NOT_FOUND row stayed in the window and kept the host WARN. Only the newest collection of a
    list-shaped metric counts."""
    fixture = load_fixture("windows_os_only_winhost01")
    stale = {("OS_SERVICE_STATUS", "*Dynamics AX Object Server*"): {
        "metric_code": "OS_SERVICE_STATUS", "metric_item": "*Dynamics AX Object Server*",
        "metric_value": "NOT_FOUND", "status": "WARN",
        "message": "Service *Dynamics AX Object Server* was not found.",
        "collected_at": "2026-07-12T07:30:00Z",  # older run, before the config was fixed
    }}
    health = build_os_health({**stale, **code_map(fixture, collected_at="2026-07-13T07:30:00Z")})

    names = [item["name"] for item in health["services"]]
    assert "*Dynamics AX Object Server*" not in names
    assert all(item["status"] == "OK" for item in health["services"])


def test_inventory_status_marks_a_host_with_no_database_not_applicable():
    os_only = build_inventory_status(code_map(load_fixture("windows_os_only_winhost01")))
    assert os_only["db_metadata"] == "not_applicable"
    assert os_only["os_resources"] == "collected_remote"

    db_host = build_inventory_status(code_map(load_fixture("sqlserver_host")))
    assert db_host["db_metadata"] == "ok"


def test_merged_drives_falls_back_to_os_disks_when_there_is_no_sql_disk_metric():
    os_only = server("windows_os_only_winhost01", ip="192.0.2.116",
                     server_id="ACME-192-0-2-116", databases=[])
    drives = merged_drives(os_only)
    assert set(drives) == {"C:", "D:"}
    assert drives["C:"]["free_percent"] == 40.0


def test_merged_drives_still_prefers_the_sql_metric_on_a_database_host():
    fixture = code_map(load_fixture("sqlserver_host"))
    from db_ops.reports.inventory_health import build_disk_health

    db_host = {"server_id": "ACME-192-0-2-115", "ip": "192.0.2.115",
               "databases": [{"db_type": "sqlserver"}],
               "disk_health": build_disk_health(fixture),
               "os_health": build_os_health(fixture)}
    drives = merged_drives(db_host)
    # The SQL metric knows E: is at 8.2 GB free of 500 GB -> CRITICAL. The OS metric has no
    # disk rows in this fixture, so nothing may overwrite that.
    assert drives["E:"]["status"] == "CRITICAL"
    assert drives["E:"]["free_gb"] == 8.2


def test_merged_drives_backfills_legacy_sql_capacity_from_os_inventory():
    db_host = {
        "disk_health": {"drives": {
            "D:": {"total_gb": None, "free_gb": 19.3,
                   "free_percent": None, "status": "WARNING"},
        }},
        "os_resources": {"disks": [{
            "mount_point": "D:\\", "total_gb": 250.0, "free_gb": 20.0,
            "logical_volume_name": "Data", "file_system_type": "NTFS",
        }]},
    }

    disk = merged_drives(db_host)["D:"]

    # SQL free GB remains authoritative; only its missing capacity and derived percentage are
    # filled from the static OS inventory.
    assert disk["free_gb"] == 19.3
    assert disk["total_gb"] == 250.0
    assert disk["free_percent"] == 7.72
    assert disk["logical_volume_name"] == "Data"


# --------------------------------------------------------------------------- #
# Report model
# --------------------------------------------------------------------------- #
def _models(*servers):
    scope, models = build_models({"servers": list(servers)})
    return scope, {m["ip"]: m for m in models}


def test_os_only_host_reports_os_fields_instead_of_empty_database_fields():
    scope, models = _models(server("windows_os_only_winhost01", ip="192.0.2.116",
                                   server_id="ACME-192-0-2-116", databases=[], role="ERP-WINHOST01"))
    aos = models["192.0.2.116"]

    assert aos["osOnly"] is True
    assert scope["os_only"] == 1 and scope["sqlserver"] == 0
    assert aos["platform"] == "Windows · OS only"
    assert aos["os"] == "Microsoft Windows Server 2019 Datacenter"
    # The card is headed by server_id: it is the key the metric rows, the overlay and the
    # per-server charts URL all use, so a card headed with the engine's machine name could not be
    # matched back to the server it describes. The machine name moved to "machine".
    assert aos["role"] == "ACME-192-0-2-116"
    assert aos["machine"] == "ERP-WINHOST01"
    assert aos["ramGB"] == 64.0

    osh = aos["os_health"]
    assert osh["hostname"] == "ERP-WINHOST01"
    assert osh["uptime"] == "15 days"
    assert osh["cpuPct"] == 18.42 and osh["logicalCpus"] == 16
    assert osh["memTotalGB"] == 64.0 and osh["memUsedGB"] == 28.03 and osh["memPct"] == 43.8
    assert osh["topCpu"][0]["process"] == "Ax32Serv"
    assert osh["topMemory"][0]["memory_mb"] == 11059.2
    assert [item["name"] for item in osh["services"]][0].startswith("Microsoft Dynamics AX Object Server")

    # The database-only signals stay empty rather than being faked, and the report does not
    # render them for this host (see _os_detail_rows / the osOnly branch in the template).
    assert aos["ple"] is None and aos["sessions"] is None
    assert aos["backup"]["cov"] == "—"
    assert aos["status"] == "ok"


def test_os_only_host_with_a_stopped_service_is_critical_and_raises_a_triage_card():
    scope, models = _models(server("linux_os_only_ubuntu", ip="192.0.2.249",
                                   server_id="ACME-192-0-2-249", databases=[], role="app-ubuntu-01"))
    host = models["192.0.2.249"]
    assert host["platform"] == "Linux · OS only"
    assert host["status"] == "crit"  # chronyd is inactive

    cards = build_triage(list(models.values()))
    service_card = next(card for card in cards if "service" in card["title"])
    assert service_card["sev"] == "crit"
    assert "chronyd" in service_card["body"]


def test_database_host_is_unchanged_by_the_os_work():
    scope, models = _models(server(
        "sqlserver_host", ip="192.0.2.115", server_id="ACME-192-0-2-115",
        databases=[{"db_type": "sqlserver", "server_name": "SALESCLUSTER",
                    "machine_name": "ACMESQL01", "database_names": ["SALESDB", "SALESDW"]}],
    ))
    db_host = models["192.0.2.115"]

    assert db_host["osOnly"] is False
    assert scope["sqlserver"] == 1 and scope["os_only"] == 0
    assert db_host["platform"].startswith("SQL Server")
    assert db_host["role"] == "ACME-192-0-2-115"
    assert db_host["machine"] == "ACMESQL01"
    assert db_host["ple"] is None  # performance_health is not in this fixture; no OS value leaks in
    assert db_host["backup"]["cov"] == "No metrics"  # not the OS-only "—"
    # OS metrics still enrich a database host: they are collected there too.
    assert db_host["os_health"]["cpuPct"] == 22.1
    assert db_host["os"] == "Microsoft Windows Server 2019 Datacenter"


# --------------------------------------------------------------------------- #
# The four numbers a DBA reads first: % CPU, RAM (% and MB), disk KB/s, network Mbps.
# Percent alone cannot be charted against capacity, and a cumulative byte counter is not a rate.
# --------------------------------------------------------------------------- #
def test_capacity_and_rate_rows_are_collected_as_their_own_series():
    for name in ("windows_os_only_winhost01", "linux_os_only_ubuntu"):
        fixture = load_fixture(name)
        rows = {(code, row["metric_item"]): row
                for code, entries in fixture.items() for row in entries}

        cpu = rows[("OS_CPU_USAGE", "cpu_usage")]
        assert cpu["metric_unit"] == "percent"

        used_mb = rows[("OS_MEMORY_USAGE", "memory_used_mb")]
        assert used_mb["metric_unit"] == "MB" and float(used_mb["metric_value"]) > 0

        read = rows[("OS_DISK_USAGE", "disk_read_kbps")]
        write = rows[("OS_DISK_USAGE", "disk_write_kbps")]
        assert read["metric_unit"] == write["metric_unit"] == "KB/s"

        send = next(row for (code, item), row in rows.items()
                    if code == "OS_NETWORK" and item.endswith(" send"))
        receive = next(row for (code, item), row in rows.items()
                       if code == "OS_NETWORK" and item.endswith(" receive"))
        assert send["metric_unit"] == receive["metric_unit"] == "Mbps"


def test_the_disk_queue_row_says_how_many_disks_it_was_summed_over():
    """`disk_queue_length` is the PhysicalDisk `_Total` counter — a sum over every physical disk,
    not one disk. An operator reading "queue length is 16" off a Telegram alert cannot tell
    whether that is 16 on one disk (stalled) or 16 across eight (idle), so the collector now
    carries the disk count, the per-disk average, the per-disk breakdown, and the thresholds it
    judged against. Losing any of them puts the reader back to guessing."""
    fixture = load_fixture("windows_os_only_winhost01")
    row = next(r for r in fixture["OS_DISK_USAGE"] if r["metric_item"] == "disk_queue_length")

    kv = parse_os_kv(row["message"])
    assert kv["physical_disks"] == "2"
    assert kv["avg_per_disk"] == "4.5"
    assert "Per disk: 1 D:=7; 0 C:=2" in row["message"]

    # The threshold scales with the disk count (~2 outstanding requests per disk), so the same
    # raw number is judged differently on a 1-disk VM and an 8-disk array.
    assert (kv["warn_at"], kv["critical_at"]) == ("4", "8")
    assert float(row["metric_value"]) >= float(kv["warn_at"]) and row["status"] == "WARN"

    # The per-disk breakdown must not be readable as k=v drive facts by the overlay builder.
    assert "free_percent" not in kv and "total_gb" not in kv


def test_rate_rows_are_not_mistaken_for_drives_or_interfaces():
    """They are rows of OS_DISK_USAGE and OS_NETWORK, so a reader that does not know better
    lists disk_read_kbps as a mount point and an interface whose IP address is "0.04"."""
    health = build_os_health(code_map(load_fixture("windows_os_only_winhost01")))

    assert set(health["disks"]) == {"C:", "D:"}                       # not disk_read_kbps
    assert [n["interface"] for n in health["network"]] == ["Ethernet0"]  # not "Ethernet0 send"
    # ... and the throughput is attached to the interface it belongs to.
    assert health["network"][0]["send_mbps"] == 0.04
    assert health["network"][0]["receive_mbps"] == 0.28
