"""A slow performance-counter provider on a Windows host must not cost that host its free space.

``OS_DISK_USAGE`` on Windows reads two things: each volume's size and free space, from
``[System.IO.DriveInfo]`` in a few milliseconds, and the disk throughput and queue, from the
``Win32_PerfRawData_PerfDisk_PhysicalDisk`` counters read twice a second apart. On one host
(2026-10-23) the first counter read in a fresh session took 41 s while the provider loaded - past
the metric's 30 s - and the second took 12 ms. The whole script was killed, so the volume rows,
already built, went with it: 33 runs out of 33 failed with "WinRM command timed out", and the one
reading the metric exists for, how full the disks are, was never stored.

So the counter reads run to a budget inside the metric's timeout - each gets what is left of it,
so a host whose provider loads in 20 s keeps its throughput - and a read that runs out is
named as what it is - the counters did not answer, a monitoring gap and not a disk fault - and
logged next to the volume rows rather than replacing them.

The note that says so is a row of ``OS_DISK_USAGE`` like the volumes. Each page builder kept its
own list of the rows that are not a volume, and the inventory's lacked the note: the host would
have been listed with a drive called ``disk_perf_counters`` and no size.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from dbabrain.reports.inventory_health import build_os_health
from dbabrain.reports.server_health import build_areas
from dbabrain.reports.server_sections import _volume_from_os_row

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "os" / "windows" / "003_os_disk_usage.ps1"
CATALOGUE = REPO_ROOT / "dbabrain" / "metrics" / "catalogue" / "metric_definitions.json"

COUNTER_READ = "Get-CimInstance Win32_PerfRawData_PerfDisk_PhysicalDisk"

# What a local CIM read that runs past -OperationTimeoutSec raises, as measured on Windows 11
# (CimException, NativeErrorCode=Failed).
CIM_TIMEOUT_MESSAGE = "Timed out"

TIMEOUT_NOTE = {
    "metric_item": "disk_perf_counters", "metric_value": "TIMEOUT", "metric_unit": "status",
    "status": "LOGGING",
    "message": "The PerfDisk performance counters did not answer within 22 s, so disk throughput "
               "and queue length were not read this time (volume free space above is unaffected).",
}
VOLUME = {
    "metric_item": "C:", "metric_value": "41.5", "metric_unit": "percent", "status": "OK",
    "message": "C: usage is 41.5 percent. total_gb=100, used_gb=41.5, free_gb=58.5, "
               "free_percent=58.5, filesystem=NTFS, label=System",
}


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _metric_timeout() -> int:
    catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    entries = catalogue if isinstance(catalogue, list) else catalogue.get("metrics", [])
    definition = next(entry for entry in entries if entry.get("metric_code") == "OS_DISK_USAGE")
    return int(definition["time_window"]["timeout"])


def _budget() -> int:
    match = re.search(r"^\$PerfBudgetSeconds\s*=\s*(\d+)\s*$", _script(), re.MULTILINE)
    assert match, "the script no longer states how long the counter reads may run"
    return int(match.group(1))


def test_every_counter_read_is_bounded_by_what_is_left_of_the_budget():
    script = _script()
    reads = [line.strip() for line in script.splitlines() if COUNTER_READ in line]

    assert len(reads) == 2, reads
    assert all("-OperationTimeoutSec (Get-PerfTimeout)" in line for line in reads), reads
    # The remainder is counted from the top of the script, not from each read.
    assert (script.index("$scriptWatch = [System.Diagnostics.Stopwatch]::StartNew()")
            < script.index("[System.IO.DriveInfo]::GetDrives()"))
    assert "$PerfBudgetSeconds - $scriptWatch.Elapsed.TotalSeconds" in script


def test_the_budget_leaves_the_remote_session_room_inside_the_metrics_timeout():
    # The budget, the one-second sample (taken inside it, but the second read always gets at
    # least one second), and room for the remote session to start and return - or the volume
    # rows are lost the same way again.
    worst_case = _budget() + 1 + 1

    assert worst_case <= _metric_timeout() - 3, (worst_case, _metric_timeout())


def test_the_volume_rows_are_built_before_the_counters_are_read_and_outside_their_try():
    script = _script()
    volumes_at = script.index("[System.IO.DriveInfo]::GetDrives()")
    counters_at = script.index(COUNTER_READ)
    inner_try_at = script.rindex("    try {", 0, counters_at)

    assert volumes_at < inner_try_at < counters_at


def test_a_read_that_runs_out_is_named_and_logged_not_reported_as_a_disk_fault():
    script = _script()
    branch = re.search(r"if \(\$message -match '([^']+)'\) \{(.*?)\n        \}", script, re.DOTALL)
    assert branch, "the counters' catch no longer has a timeout branch first"
    pattern, body = branch.group(1), branch.group(2)

    # PowerShell's -match is a case-insensitive .NET regex; these alternatives read the same in
    # Python.
    assert re.search(pattern, CIM_TIMEOUT_MESSAGE, re.IGNORECASE)
    assert not re.search(pattern, "Invalid class", re.IGNORECASE)
    assert "'disk_perf_counters' 'TIMEOUT' 'status' 'LOGGING'" in body
    assert "not a disk fault" in body


def test_the_note_is_not_listed_as_a_drive_in_the_inventory():
    code_map = {("OS_DISK_USAGE", row["metric_item"]): {**row, "metric_code": "OS_DISK_USAGE",
                                                        "collected_at": "2026-10-23T10:00:00Z"}
                for row in (VOLUME, TIMEOUT_NOTE)}

    assert set(build_os_health(code_map)["disks"]) == {"C:"}


def test_the_note_is_not_a_row_of_the_server_pages_volume_table():
    assert _volume_from_os_row(TIMEOUT_NOTE) is None
    assert _volume_from_os_row(VOLUME)["name"] == "C:"


def _series(row: dict) -> dict:
    numeric = row["metric_item"] != "disk_perf_counters"
    last = float(row["metric_value"]) if numeric else None
    return {"code": "OS_DISK_USAGE", "item": row["metric_item"], "label": "OS disk",
            "unit": row["metric_unit"], "status": row["status"], "numeric": numeric,
            "static": not numeric, "last": last, "lastText": row["metric_value"],
            "message": row["message"], "lastAt": 1761213600,
            "points": [[1761213600, last, row["status"]]]}


def test_neither_tile_shows_the_note_in_place_of_a_reading():
    areas = {area["key"]: area for area in build_areas([_series(VOLUME), _series(TIMEOUT_NOTE)])}

    # The Disk space tile is about the volumes ...
    assert areas["disk_space"]["sourceItem"] == "C:"
    # ... and Storage activity has no reading: a LOGGING note there would colour it green.
    assert areas["storage_activity"]["status"] == "UNKNOWN"
    assert areas["storage_activity"]["value"] == "not collected"
