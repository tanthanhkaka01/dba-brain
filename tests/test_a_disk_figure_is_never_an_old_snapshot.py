"""A disk on the fleet page is graded from what was read this cycle, by the collector that read it.

On 2026-11-05 the fleet page's "Disk critically low" card listed four volumes, and three were wrong:

* a SQL Server 2008 host's T: at 5.94 % free - SQL Server 2008's `xp_fixeddrives` reports free GB and no size,
  so the page divided the 29.69 GB read that hour by the 499.87 GB the canonical inventory's
  `os_resources` had recorded in September. The volume is 49.87 GB now, 59.5 % free, and the OS
  metric read that on the same cycle.
* another host's E: at 6.45 % - true, and the collector graded it WARNING (013: CRITICAL under 5 %),
  but the page graded every drive again on its own 10 / 15 % and called it critically low.
* a third host's D: at 0.01 % - known only from that September snapshot (the server's metrics are
  off since), graded as if it were a reading.
"""

from __future__ import annotations

from dbabrain.lib.inventory_render import merged_drives
from dbabrain.reports import inventory_report
from dbabrain.reports.inventory_health import build_disk_health


def _server(**blocks):
    return {"server_id": "S1", "ip": "192.0.2.10", **blocks}


SNAPSHOT_T = {"mount_point": "T:", "logical_volume_name": "TempDB", "file_system_type": "NTFS",
              "total_gb": 499.87, "free_gb": 488.28}


def test_a_free_figure_without_a_size_takes_the_os_metrics_size_not_the_snapshots():
    server = _server(
        os_resources={"disks": [SNAPSHOT_T]},
        disk_health={"drives": {"T:": {"free_gb": 29.69, "total_gb": None, "free_percent": None, "status": "OK"}}},
        os_health={"disks": {"T:": {"total_gb": 49.87, "free_gb": 29.69, "free_percent": 59.53, "status": "OK"}}},
    )
    drive = merged_drives(server)["T:"]
    assert drive["total_gb"] == 49.87
    assert drive["free_percent"] == round(100.0 * 29.69 / 49.87, 2)
    assert drive["logical_volume_name"] == "TempDB"


def test_a_snapshot_size_smaller_than_the_free_space_read_now_is_not_used():
    server = _server(
        os_resources={"disks": [{"mount_point": "E:", "total_gb": 100.0, "free_gb": 50.0}]},
        disk_health={"drives": {"E:": {"free_gb": 299.74, "total_gb": None, "free_percent": None, "status": "OK"}}},
    )
    drive = merged_drives(server)["E:"]
    assert drive["total_gb"] is None
    assert drive["free_percent"] is None


def test_a_drive_known_only_from_the_snapshot_has_no_free_percent_and_no_verdict():
    server = _server(os_resources={"disks": [{"mount_point": "D:", "logical_volume_name": "Data",
                                              "total_gb": 2047.98, "free_gb": 0.2}]})
    drive = merged_drives(server)["D:"]
    assert drive["free_percent"] is None
    assert drive["status"] == "NO_DATA"
    assert drive["snapshot_only"] is True

    (entry,) = inventory_report._build_disks(server)
    assert entry["sev"] == "idle"
    assert entry["freeGB"] is None and entry["totalGB"] is None
    assert entry["flag"] == "not collected"


def _row(item, value, status, message):
    return {"metric_item": item, "metric_value": value, "status": status, "message": message}


def test_the_disk_status_is_the_collectors_before_any_severity_map():
    rows = [
        _row("E:\\", "240.38", "LOGGING", "drive=E:\\, free_gb=240.38, total_gb=3725.48, free_pct=6.45 "
             "Policy: severity remapped WARNING->LOGGING by metric_overrides."),
        _row("C:\\", "77.51", "OK", "drive=C:\\, free_gb=77.51, total_gb=201.89, free_pct=38.39"),
    ]
    code_map = {("STORAGE_DISK_FREE_SPACE", row["metric_item"]): row for row in rows}
    drives = build_disk_health(code_map)["drives"]
    # 6.45 % is under the page's old 10 % line, but the collector said WARNING - and a remap to
    # LOGGING silences the alert, not the page.
    assert drives["E:\\"]["status"] == "WARNING"
    assert drives["C:\\"]["status"] == "OK"


def test_the_fleet_card_lists_a_warning_drive_as_low_headroom_not_critically_low():
    disks = [{"m": "E: Data Share", "free": 6.46, "freeGB": 240.86, "totalGB": 3725.48, "st": "WARNING", "sev": "warn"}]
    model = {"backup": {"note": "", "cov": ""}, "disks": disks, "os_health": {}, "total": 3, "online": 3,
             "ple": None, "sessions": 5, "severity": {"worst": "OK"}, "osOnly": False}
    assert inventory_report._status(model) == "warn"


# --------------------------------------------------------------------------- #
# A server nothing current was read from (audit 2026-11-05, A4)
# --------------------------------------------------------------------------- #
def _model(**over):
    model = {"role": "S1", "ip": "192.0.2.10", "backup": {"note": "", "cov": ""}, "disks": [],
             "os_health": {}, "total": 3, "online": 3, "ple": None, "sessions": 5,
             "severity": {"worst": "CRITICAL"}, "osOnly": False,
             "freshness": {"stale": False, "asOf": "2026-11-05T01:00:00"}, "status": "ok",
             "cfg": {}, "ha": "none_documented", "dbs": [], "ramGB": None}
    model.update(over)
    return model


def test_a_stale_server_is_idle_whatever_its_last_values_said():
    stale = _model(freshness={"stale": True, "asOf": "2026-09-26T00:00:00"},
                   disks=[{"m": "D:", "free": 0.01, "st": "CRITICAL", "sev": "crit"}])
    assert inventory_report._status(stale) == "idle"
    assert inventory_report._status(_model()) == "crit"


def test_a_stale_server_feeds_no_card_but_its_own():
    stale = _model(freshness={"stale": True, "asOf": ""},
                   disks=[{"m": "D:", "free": 0.01, "st": "CRITICAL", "sev": "crit"}])
    cards = inventory_report.build_triage([stale])
    titles = [card["title"] for card in cards]
    assert any("show last known values" in title for title in titles)
    assert not any("Disk critically low" in title for title in titles)


# --------------------------------------------------------------------------- A10 / A11
def test_page_life_expectancy_is_graded_by_the_shared_rule():
    """CRITICAL under 300 s, WARNING under 2000 s - the fleet page had red under 500 and never crit."""
    healthy = {"worst": "OK"}
    assert inventory_report._status(_model(severity=healthy, ple=150)) == "crit"
    assert inventory_report._status(_model(severity=healthy, ple=400)) == "warn"
    assert inventory_report._status(_model(severity=healthy, ple=4200)) == "ok"


def test_a_failed_login_card_follows_the_collectors_verdict_not_a_count():
    def with_principal(attempts, status):
        return _model(severity={"worst": "OK"}, security={
            "principals": [{"login": "app", "attempts": attempts, "status": status}], "passwordsOld": []})

    # 500 attempts the collector flags: a card, at its WARNING.
    cards = inventory_report.build_triage([with_principal(500, "WARNING")])
    card = next(c for c in cards if "Failed logins" in c["title"])
    assert card["sev"] == "warn"
    # 1,200 attempts the collector did not flag: no card of the page's own.
    assert not any("Failed logins" in c["title"] for c in inventory_report.build_triage([with_principal(1200, "OK")]))


def test_a_sql_disk_reading_that_stopped_gives_way_to_the_os_metric():
    """A13: the SQL metric's drive always won, so one switched off days ago was still shown and graded."""
    server = _server(
        disk_health={"drives": {"E:\\": {"free_gb": 5.0, "total_gb": 300.0, "free_percent": 1.67,
                                         "status": "CRITICAL", "as_of": "2026-11-02T01:00:00Z"}}},
        os_health={"disks": {"E:": {"total_gb": 300.0, "free_gb": 250.0, "free_percent": 83.33,
                                    "status": "OK", "as_of": "2026-11-05T01:00:00Z"}}},
    )
    drive = merged_drives(server)["E:"]
    assert (drive["free_gb"], drive["status"]) == (250.0, "OK")


# --------------------------------------------------------------------------- A8 / A9 / A15 / B3
def test_facts_from_the_canonical_file_are_marked_static_on_both_pages():
    from dbabrain.reports.server_sections import build_server_detail

    server = {"server_id": "S1", "ip": "192.0.2.10", "databases": [{"db_type": "sqlserver"}],
              "os_resources": {"physical_memory_gb": 400, "os_caption": "Windows Server 2016"},
              "ha": {"mode": "FCI", "role": "primary_active_node"},
              "sqlserver_resources": {"sql_cpu": {"sql_visible_cpu_count": 24},
                                      "tempdb": {"total_mb": 13768, "file_count": 9}}}
    _scope, (model,) = inventory_report.build_models({"servers": [server]})
    assert {"ramGB", "os", "ha"} <= set(model["staticFields"])
    assert model["cfg"]["static"] is True
    assert "(file count static)" in model["cfg"]["tempdb"]
    facts = {row[0]: row for row in build_server_detail(model)["facts"]}
    assert facts["Host RAM"][-1] == "static" and facts["HA / DR"][-1] == "static"


def test_a_linux_root_keeps_its_name():
    """A15: `_norm_mount("/")` was "", so the root volume was listed with no mount."""
    from dbabrain.lib.inventory_render import _norm_mount

    assert _norm_mount("/") == "/"
    assert _norm_mount("E:\\") == "E:"
    assert _norm_mount("/var/lib/") == "/var/lib"


def test_the_fragmented_list_says_when_fragmentation_was_read():
    """E1: it runs on its own schedule and sat under the usage run's stamp."""
    from dbabrain.reports.index_report import _fragmented_section

    lines = _fragmented_section([{"item": "dbo.T.IX", "pct": "91.0", "size_mb": "10"}], None,
                                as_of="2026-11-03T18:00:00Z")
    assert "fragmentation read 2026-11-03 18:00:00 UTC" in lines[0]


def test_the_markdown_readers_list_a_drive_by_its_verdict_like_the_page():
    from dbabrain.lib.inventory_render import _findings, drive_is_low

    # 12 % free and graded OK by its collector: no line, though under the page's own 15 %.
    ok_low = _server(disk_health={"drives": {"F:": {"free_gb": 12.0, "total_gb": 100.0,
                                                     "free_percent": 12.0, "status": "OK"}}})
    warned = _server(disk_health={"drives": {"E:": {"free_gb": 6.45, "total_gb": 100.0,
                                                     "free_percent": 6.45, "status": "WARNING"}}})
    assert not any("Low disk" in line for line in _findings([ok_low]))
    assert any("Low disk" in line and "`E:`" in line for line in _findings([warned]))
    # A drive no collector graded still falls back to the percentage.
    assert drive_is_low({"free_percent": 8.0}) and not drive_is_low({"free_percent": 40.0})
