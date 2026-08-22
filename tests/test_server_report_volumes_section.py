"""How much room is left on this host — the question the page held every number for and never answered.

The Disk space tile reduces a whole machine to one percentage. On 192.0.2.250 that percentage
was `28%`, green, and true: the fullest of its two volumes is a 2 TB data disk at 28.4%. What it
could not say is that the other volume is 199 GB, or that "28%" of the first one is 1.4 TB of
headroom and of the second would be 143 GB. Size is what turns a percentage into a decision, and
the collectors had been sending it all along — `total_gb`, `used_gb`, `free_gb` ride in the
message of both the Windows and the Linux disk script, and the report parsed out the percentage
and threw the rest away.

Three things about this data are easy to get wrong, and each has a test below:

- **The same volume is collected twice on a SQL Server host.** The OS metric reports `C:` and
  `sys.dm_os_volume_stats` reports `C:\\`; without normalising the trailing separator every
  Windows volume is listed twice, once per source. The trailing separator only — on Linux the
  root mount *is* `/`, and trimming it away would merge root into whatever sorted next to it.
- **`OS_DISK_USAGE` is not only volumes.** Throughput, IOPS and queue length share the metric
  code with the mount points, and arrive as rows with no size at all.
- **A size the host does not know is not a size of zero.** An old SQL Server falls back to
  `xp_fixeddrives`, which returns free space and `total_gb=unknown` (all three volumes on
  192.0.2.253 read that way). Those rows still carry the column that decides anything, so
  they are listed — and the host-level ratio is withheld rather than computed from a partial sum.
"""

from db_ops.reports.server_report import build_volumes


def _os(item, value, message, status="OK"):
    return {"metric_code": "OS_DISK_USAGE", "metric_item": item, "metric_value": value,
            "metric_unit": "percent", "status": status, "message": message,
            "collected_at": "2026-08-18T04:05:11Z"}


def _engine(item, free_gb, message, status="OK"):
    return {"metric_code": "STORAGE_DISK_FREE_SPACE", "metric_item": item, "metric_value": free_gb,
            "metric_unit": "GB", "status": status, "message": message,
            "collected_at": "2026-08-18T04:05:17Z"}


WINDOWS_C = _os("C:", "14.42",
                "C: usage is 14.42 percent. total_gb=199.4, used_gb=28.74, free_gb=170.65, "
                "free_percent=85.58, filesystem=NTFS, label=")
WINDOWS_D = _os("D:", "28.4",
                "D: usage is 28.4 percent. total_gb=2047.98, used_gb=581.57, free_gb=1466.41, "
                "free_percent=71.6, filesystem=NTFS, label=DATA")
LINUX_ROOT = _os("/", "57",
                 "/ usage is 57 percent. total_gb=588.29, used_gb=317.51, free_gb=246.47, "
                 "free_percent=43.00, filesystem=ext4, device=/dev/mapper/ubuntu--vg-ubuntu--lv")
LINUX_BOOT = _os("/boot", "11",
                 "/boot usage is 11 percent. total_gb=1.90, used_gb=0.20, free_gb=1.59, "
                 "free_percent=89.00, filesystem=ext4, device=/dev/sda2")

DISK_IOPS = _os("disk_iops", "97", "Disk IO is 97 operations/s (all physical disks). "
                                   "read_iops=59, write_iops=38")
DISK_READ = _os("disk_read_kbps", "392.00", "Disk read throughput is 392.00 KB/s (all physical disks).")
DISK_QUEUE = _os("disk_queue_length", "0",
                 "Total outstanding IO requests across all 2 physical disk(s) is 0 (avg 0 per "
                 "disk). physical_disks=2, avg_per_disk=0, warn_at=4, critical_at=8")


def test_a_windows_volume_reports_its_size_used_and_free_space():
    volumes = build_volumes([WINDOWS_C, WINDOWS_D])["volumes"]
    data = next(v for v in volumes if v["name"] == "D:")
    assert (data["totalGB"], data["usedGB"], data["freeGB"]) == (2047.98, 581.57, 1466.41)
    assert data["usedPct"] == 28.4
    assert data["filesystem"] == "NTFS"
    # Windows names the volume, Linux names the device; the page has one column for both.
    assert data["device"] == "DATA"


def test_a_linux_mount_point_reports_the_same_figures_from_its_own_collector():
    volumes = build_volumes([LINUX_ROOT, LINUX_BOOT])["volumes"]
    root = next(v for v in volumes if v["name"] == "/")
    assert (root["totalGB"], root["usedGB"], root["freeGB"]) == (588.29, 317.51, 246.47)
    assert root["filesystem"] == "ext4"
    assert root["device"] == "/dev/mapper/ubuntu--vg-ubuntu--lv"


def test_the_fullest_volume_is_listed_first():
    volumes = build_volumes([WINDOWS_C, WINDOWS_D])["volumes"]
    assert [v["name"] for v in volumes] == ["D:", "C:"]


def test_throughput_and_queue_rows_are_not_volumes():
    """They share OS_DISK_USAGE with the mount points and have no size to report."""
    result = build_volumes([LINUX_ROOT, DISK_IOPS, DISK_READ, DISK_QUEUE])
    assert [v["name"] for v in result["volumes"]] == ["/"]


def test_one_windows_volume_collected_by_both_sources_is_listed_once():
    """``sys.dm_os_volume_stats`` says ``C:\\`` and the OS collector says ``C:``."""
    result = build_volumes([
        WINDOWS_C,
        _engine("C:\\", "170.75", "drive=C:\\, free_gb=170.75, total_gb=199.40, "
                                  "free_pct=85.63, used_pct=14.37"),
    ])
    assert [v["name"] for v in result["volumes"]] == ["C:"]
    # The OS row wins: it is the one that knows the file system and the device.
    assert result["volumes"][0]["source"] == "os"
    assert result["volumes"][0]["filesystem"] == "NTFS"


def test_a_volume_only_the_engine_can_see_is_still_listed():
    """An instance with ``disabled_collector_types: ["cmd"]`` has no OS metric at all."""
    result = build_volumes([
        _engine("E:", "21.98", "drive=E:, free_gb=21.98, total_gb=120.00, "
                               "free_pct=18.32, used_pct=81.68"),
    ])
    assert [v["name"] for v in result["volumes"]] == ["E:"]
    assert result["volumes"][0]["source"] == "engine"
    assert result["volumes"][0]["freeGB"] == 21.98


def test_a_host_that_reports_free_space_but_no_size_keeps_its_free_space():
    """``xp_fixeddrives`` knows free megabytes and nothing else — that is still the column that counts."""
    rows = [_engine(drive, free, f"drive={drive}, free_gb={free}, source=xp_fixeddrives, "
                                 "total_gb=unknown")
            for drive, free in (("C:", "58.11"), ("D:", "20.66"), ("E:", "21.98"))]
    result = build_volumes(rows)
    assert len(result["volumes"]) == 3
    assert all(v["totalGB"] is None for v in result["volumes"])
    assert result["summary"]["freeGB"] == 100.75


def test_a_host_ratio_is_withheld_when_any_volume_has_no_size():
    """Percent-of-total computed over a partial sum would understate every host it is wrong about."""
    summary = build_volumes([
        WINDOWS_C,
        _engine("E:", "21.98", "drive=E:, free_gb=21.98, source=xp_fixeddrives, total_gb=unknown"),
    ])["summary"]
    assert summary["unsized"] == 1
    assert summary["totalGB"] is None and summary["usedPct"] is None
    # Free space is still summed: it is the host's real headroom whether the sizes are known or not.
    assert summary["freeGB"] == 192.63


def test_the_summary_totals_the_host_when_every_volume_reported_a_size():
    summary = build_volumes([LINUX_ROOT, LINUX_BOOT])["summary"]
    assert summary["count"] == 2 and summary["unsized"] == 0
    assert summary["totalGB"] == 590.19
    assert summary["freeGB"] == 248.06
    assert summary["fullest"] == "/" and summary["fullestPct"] == 57.0


def test_a_full_volume_is_counted_by_severity_so_the_section_opens_itself():
    full = _os("E:", "96.2",
               "E: usage is 96.2 percent. total_gb=120.00, used_gb=115.44, free_gb=4.56, "
               "free_percent=3.8, filesystem=NTFS, label=LOGS", status="CRITICAL")
    summary = build_volumes([WINDOWS_C, full])["summary"]
    assert summary["critical"] == 1 and summary["warning"] == 0
    assert summary["fullest"] == "E:"


def test_a_server_with_no_disk_metric_at_all_renders_nothing():
    assert build_volumes([]) == {"volumes": [], "summary": {}}
    assert build_volumes([DISK_IOPS])["volumes"] == []
