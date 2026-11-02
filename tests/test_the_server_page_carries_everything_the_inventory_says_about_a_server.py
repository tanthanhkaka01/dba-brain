"""The per-server page carries everything the inventory says about that server - never less.

The operator, 2026-10-23: *the server metrics page is the page of one instance; it must carry more
information than the inventory, not less.* It carried less. Reading a PostgreSQL server's page there
was no version, no patch level, nothing the instance says about itself - on any engine - and no
configuration, security counts or host facts either: the inventory's Server Detail and Config tab
had them all, one page away.

The page now has a Server detail section built from the inventory's own record for the server
(`server_sections.build_server_detail`), so the two pages read one set of values and no query is
added; where the inventory prints a few fields of a block (the host: five) the page prints all of
them. The guard at the end reads the inventory's template: a per-server field it shows and this page
has no place for fails here, so the rule holds for fields added later too.
"""
from __future__ import annotations

import re

from dbabrain.reports.inventory_health import build_instance_health
from dbabrain.reports.inventory_report import TEMPLATE_HTML as INVENTORY_TEMPLATE, _build_detail
from dbabrain.reports.server_report import TEMPLATE_HTML, build_payload
from dbabrain.reports.server_sections import build_server_detail

SQLSERVER = (
    "SQL Server connection is available. server=SALESDB01; ip=192.0.2.20; port=1433; "
    "transport=TCP; protocol=TSQL; auth=SQL; version=15.0.4261.1; level=RTM; CU=CU18; "
    "update=KB5017593; edition=Standard Edition (64-bit); engine=Standard; instance=MSSQLSERVER; "
    "machine=SALESDB01; physical=SALESNODE1; clustered=Yes; pid=4512; "
    "collation=SQL_Latin1_General_CP1_CI_AS; started=2026-09-27 18:43:51"
)
POSTGRESQL = (
    "PostgreSQL connection available. version=18.6 (Debian 18.6-1.pgdg13+2); "
    "build=PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2); platform=x86_64-pc-linux-gnu; role=primary; "
    "ip=192.0.2.30; port=5432; transport=TCP; ssl=off; connected_database=appdb; "
    "database_collation=en_US.utf8; encoding=UTF8; timezone=Etc/UTC; max_connections=100; "
    "wal_level=replica; archive_mode=on; started=2026-10-22 09:01:45; uptime_seconds=81460"
)
ORACLE_8I = (
    "instance=orcl, status=OPEN, version=8.1.7.0.0, host=ORAHOST, machine=ORAHOST, "
    "edition=Standard, banner=Oracle8i Release 8.1.7.0.0 - Production, database=ORCL, "
    "open_mode=READ WRITE, log_mode=NOARCHIVELOG, charset=WE8ISO8859P1, rac=NO, logins=ALLOWED, "
    "archiver=STOPPED, started=2026-10-22 07:41:52"
)


def _health(message):
    return build_instance_health({("INSTANCE_STATUS", "I"): {
        "metric_item": "I", "metric_value": "ONLINE", "status": "OK", "message": message,
        "collected_at": "2026-10-23T07:39:41Z"}})


def _model(message=SQLSERVER, **extra):
    """An inventory record for one server, every block filled, as `build_models` makes it."""
    health = _health(message)
    model = {
        "ip": "192.0.2.20", "server_id": "ACME-192-0-2-20", "company": "ACME", "osOnly": False,
        "role": "ACME-192-0-2-20", "machine": "SALESDB01", "endpoint": "SALESDB01",
        "platform": "SQL Server · Std 2019", "os": "Windows Server 2019 Standard", "ramGB": 64,
        "online": 5, "total": 6, "ple": 4200, "sessions": 37,
        "backup": {"cov": "Full+Diff+Log", "note": "5/6 DB within policy", "worstDatabases": ["appdb"]},
        "ha": "FCI · active node",
        "cfg": {"cpu": 16, "sched": 16, "cpuLive": True, "maxdop": 8, "cost": 5, "maxmemMB": 57344,
                "committedMB": 51200, "tempdb": "8192 MB / 8 files", "backupCompr": 0, "remoteDac": 0,
                "optAdhoc": 1, "blockedThr": 0, "clr": 0, "xpcmd": 1, "warns": 4},
        "disks": [{"m": "D:", "free": 21.5, "freeGB": 110, "totalGB": 512, "st": "OK", "sev": "ok"}],
        "dbs": ["appdb"], "databases": [{"database": "appdb"}],
        "inv": "last_checked 2026-10-23 07:00:00 · metric_rows 4200",
        "findings": ["3 warning metric result(s) from: LOG_FILE_SPACE", "no CHECKDB schedule"],
        "detail": _build_detail({"instance_health": health, "notes": ["moved to the new SAN"]}, {}),
        "instance": health,
        "os_health": {"hostname": "SALESDB01", "osName": "Windows Server 2019 Standard", "arch": "x64",
                      "timezone": "SE Asia Standard Time", "uptime": "26 d", "lastBoot": "2026-09-27",
                      "cpuPct": 12.5, "cpuModel": "Xeon Gold", "cores": 16, "logicalCpus": 32,
                      "loadOrQueue": 2, "memTotalGB": 64, "memUsedGB": 58, "memPct": 90.6, "swapPct": 3,
                      "services": [{"name": "MSSQLSERVER", "state": "Running", "status": "OK"},
                                   {"name": "SQLSERVERAGENT", "state": "Stopped", "status": "CRITICAL"}],
                      "topCpu": [{"process": "sqlservr", "cpu_percent": 11.0, "memory_mb": 52000}],
                      "topMemory": [{"process": "sqlservr", "memory_mb": 52000}],
                      "network": [{"interface": "Ethernet0", "ip_address": "192.0.2.20", "speed_mbps": 10000}],
                      "events": [{"log": "System", "count": 3, "detail": "disk"}],
                      "pendingReboot": "no"},
        "security": {"collected": True, "failed24h": 1520,
                     "principals": [{"login": "app_user", "attempts": 1500}],
                     "passwordsOld": [{"login": "legacy", "age_days": 900}], "passwordThresholdDays": 180,
                     "oldestPasswordDays": 900, "certsExpired": 1, "certsExpiring30d": 0},
        "freshness": {"asOf": "2026-10-23T07:39:41Z", "ageHours": 0.2, "stale": False},
        "status": "warn", "dbType": "sqlserver",
    }
    model.update(extra)
    return model


def _facts(detail):
    return {row[0]: row[1] for row in detail["facts"]}


def test_the_instance_is_named_by_its_build_patch_and_edition():
    detail = build_server_detail(_model())

    facts = _facts(detail)
    assert facts["Engine build"] == "15.0.4261.1 RTM · CU18 KB5017593"
    assert facts["Edition"] == "Standard Edition (64-bit) · Standard"
    assert facts["Instance host"] == "SALESDB01 · SALESNODE1 · clustered Yes"
    assert facts["Listening on"] == "192.0.2.20:1433 · TCP · SQL"
    assert detail["summary"] == "15.0.4261.1 RTM · CU18 KB5017593 · Standard Edition (64-bit)"


def test_each_engine_is_described_in_its_own_words():
    pg = _facts(build_server_detail(_model(POSTGRESQL)))
    assert pg["Engine build"] == "18.6 (Debian 18.6-1.pgdg13+2)"
    assert pg["Role"] == "primary" and pg["WAL"] == "wal_level replica · archive_mode on"
    assert "Edition" not in pg and "Server collation" not in pg

    ora = _facts(build_server_detail(_model(ORACLE_8I)))
    assert ora["Engine banner"] == "Oracle8i Release 8.1.7.0.0 - Production"
    # 8i states an open mode and no role: under "Role" its READ WRITE would read as one.
    assert ora["Open mode"] == "READ WRITE" and "Role" not in ora


def test_the_fleet_row_and_the_server_detail_grid_are_on_the_page():
    facts = _facts(build_server_detail(_model()))

    assert facts["Company"] == "ACME"
    assert facts["Host RAM"] == "64 GB"
    assert facts["HA / DR"] == "FCI · active node"
    assert facts["Backup"] == "Full+Diff+Log · 5/6 DB within policy · worst: appdb"
    assert facts["Page life expectancy"] == "4,200 s"
    assert facts["Active sessions"] == "37"
    assert facts["Databases online"] == "5 / 6"
    assert facts["Disks"] == "D: · 21.5% free · 110 / 512 GB"
    assert facts["Notes"] == "moved to the new SAN"


def test_the_configuration_tab_is_on_the_page_with_its_advice():
    detail = build_server_detail(_model())

    config = dict(detail["config"])
    assert config["CPUs visible / schedulers"] == "16 visible · 16 schedulers online"
    assert config["Max degree of parallelism"] == "8"
    assert config["Max server memory"] == "57,344 MB"
    assert config["xp_cmdshell"] == "on (1)" and config["Backup compression default"] == "off (0)"
    assert "cost threshold for parallelism = 5 → raise to 30–50" in detail["configAdvice"]
    assert "xp_cmdshell = 1 (enabled) → confirm intentional / scope it" in detail["configAdvice"]


def test_every_host_field_is_on_the_page_not_only_the_five_the_inventory_prints():
    detail = build_server_detail(_model())

    host = dict(detail["host"])
    for label in ("Hostname", "Operating system", "Architecture", "Timezone", "Uptime", "Last boot",
                  "CPU", "Load / processor queue", "Memory", "Swap / page file used", "Pending reboot"):
        assert host.get(label), label
    assert host["CPU"] == "Xeon Gold · 16 cores · 32 logical CPUs · 12.5% used"
    assert [s["name"] for s in detail["services"]] == ["MSSQLSERVER", "SQLSERVERAGENT"]
    assert detail["network"] and detail["events"] and detail["topCpu"] and detail["topMemory"]


def test_security_and_findings_are_on_the_page():
    detail = build_server_detail(_model())

    security = dict(detail["security"])
    assert security["Failed logins (24 h)"] == "1,520"
    assert security["Failed logins by login"] == "app_user: 1,500"
    assert security["Certificates expired"] == "1"
    assert detail["findings"] == ["3 warning metric result(s) from: LOG_FILE_SPACE", "no CHECKDB schedule"]
    assert detail["freshness"].startswith("live metrics as of 2026-10-23T07:39:41Z")


def test_a_static_row_says_it_is_static():
    detail = build_server_detail(_model(detail=[["Database sizes", "appdb=100MB", "static"]]))

    assert ["Database sizes", "appdb=100MB", "static"] in detail["facts"]


def test_no_record_no_section_and_the_payload_defaults_to_none():
    assert build_server_detail(None) == {} and build_server_detail({}) == {}
    detail = build_server_detail(_model())
    assert build_payload([], [], server_detail=detail)["serverDetail"] == detail
    assert build_payload([], [])["serverDetail"] == {}


def test_the_page_renders_the_section_before_the_databases_and_the_build_in_its_header():
    html = TEMPLATE_HTML.read_text(encoding="utf-8")

    assert "function serverDetailBlock(s)" in html
    body = html[html.index("verdict(d.health, server)"):]
    assert re.search(r"serverDetailBlock\(d\.serverDetail\)[\s\S]*databasesBlock\(d\.databases\)", body)
    assert "d.serverDetail.summary" in html


#: Every per-server field the inventory's template reads, and where this page has it. A field the
#: inventory starts showing that is in neither list fails the next test until it has a place here.
ON_THE_SERVER_DETAIL = {
    "backup", "cfg", "company", "detail", "disks", "dbs", "endpoint", "findings", "freshness", "ha",
    "inv", "online", "os", "os_health", "platform", "ple", "ramGB", "security", "sessions", "status",
    "total",
}
ELSEWHERE_ON_THE_PAGE = {
    "databases": "the Databases section, from the same inventory_health.build_database_rows",
    "workload": "the Workload section, from the same workload builder",
    "role": "the page title", "ip": "the header line",
    "reportUrl": "the page itself", "indexUrl": "the header's index usage link",
    "dbType": "the engine's own sections", "oracle": "the Oracle sections", "osOnly": "the host-only picker group",
}


def test_every_per_server_field_the_inventory_shows_has_a_place_on_the_server_page():
    template = INVENTORY_TEMPLATE.read_text(encoding="utf-8")
    fields = set(re.findall(r"\bs\.([A-Za-z_]+)", template))

    unplaced = sorted(fields - ON_THE_SERVER_DETAIL - set(ELSEWHERE_ON_THE_PAGE))
    assert not unplaced, f"the inventory shows {unplaced} for a server and the server page does not"


def test_every_field_said_to_be_in_the_server_detail_is_read_by_it():
    import inspect

    from dbabrain.reports import server_sections

    source = inspect.getsource(server_sections.build_server_detail)
    missing = sorted(f for f in ON_THE_SERVER_DETAIL if f'"{f}"' not in source)
    assert not missing, f"build_server_detail never reads {missing}"
