"""Render the "beauty" Database Inventory & Health report (HTML + Markdown).

This is an *additive* output for the reports app: ``inventory-workflow --beauty 1``
calls :func:`build_inventory_report` after the normal merge step. The plain
``*-summary.md`` from ``inventory_summary`` is unchanged and still produced.

Both files are rendered from the same data model computed from the merged
``architecture/database-inventory.json``:

* ``<stamp>_database-inventory-report.html`` — fills the shipped template
  ``templates/inventory_report.html`` by injecting the ``SCOPE`` / ``SERVERS`` /
  ``TRIAGE`` data the template's own JavaScript renders from (the template's
  documented contract: "replace this object from database-inventory.json").
* ``<stamp>_database-inventory-report.md`` — the same content as static Markdown.

The ``TRIAGE`` (Priority Attention) items are auto-derived from thresholds
(stale log backups, low disk, low PLE, FULL-only coverage, config drift,
xp_cmdshell, un-collected hosts), not hand-written.
"""

from __future__ import annotations
from db_ops.lib.coerce import as_float
from db_ops.common.data_sources import inventory_exclude_ip_prefixes
from db_ops.lib.inventory_render import DEFAULT_INVENTORY, DISK_CRIT_PCT, DISK_WARN_PCT, EXCLUDE_IP_PREFIXES, _primary_db  # noqa: F401 - one definition

import datetime
import json
import re
from pathlib import Path

from db_ops.lib import backup_policy
from db_ops.reports import inventory_health
from db_ops.reports.inventory_health import merged_drives, merged_sql_resources
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module

TEMPLATE_HTML = Path(__file__).resolve().parent / "templates" / "inventory_report.html"

CPU_WARN_PCT = 90.0
MEMORY_WARN_PCT = 90.0
PLE_WARN = 2000
# A principal failing to authenticate this often in 24h is not a user mistyping a password:
# it is a credential being guessed or an integration retrying a dead one.
FAILED_LOGIN_CRIT = 1000
# SQL logins are rotated far less often than domain accounts; a year is already generous, and
# the collector's own warning threshold is 180 days.
PASSWORD_AGE_WARN_DAYS = 365
STALE_LOG_HOURS = 48
# Beyond this, a server's health blocks are "last known", not "current". The metrics run
# hourly at worst, so a day of silence means the collector is not reaching this target —
# and its numbers must not be shown as if they were taken today.
STALE_DATA_HOURS = 24
UNCAPPED_MEM_MB = 2147483647

_EDITION_SHORT = [
    ("developer", "Dev"), ("enterprise", "Ent"), ("standard", "Std"),
    ("express", "Express"), ("web", "Web"), ("business intelligence", "BI"),
]




def _short_edition(edition: str) -> str:
    low = (edition or "").lower()
    for key, short in _EDITION_SHORT:
        if key in low:
            return short
    return (edition or "").split()[0] if edition else ""


def _uptime_text(seconds) -> str:
    value = as_float(seconds)
    if not value or value <= 0:
        return ""
    days = int(value // 86400)
    hours = int((value % 86400) // 3600)
    if days:
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _build_os(os_health: dict) -> dict:
    """Flatten the os_health overlay into what the report renders for a host: identity,
    live CPU/memory, the services that were asked for, and the processes actually burning
    CPU and RAM. Empty when the host has no OS metrics in the window."""
    if not os_health:
        return {}
    info = os_health.get("os_info") or {}
    cpu = os_health.get("cpu") or {}
    memory = os_health.get("memory") or {}
    top_cpu = os_health.get("top_cpu") or []
    top_memory = os_health.get("top_memory") or []
    events = os_health.get("events") or []
    return {
        "hostname": info.get("hostname") or "",
        "osName": info.get("os_name") or "",
        "arch": info.get("architecture") or "",
        "timezone": info.get("timezone") or "",
        "uptime": _uptime_text(info.get("uptime_seconds")),
        "lastBoot": info.get("last_boot_time") or "",
        "cpuPct": cpu.get("usage_percent"),
        "cpuModel": cpu.get("model") or "",
        "cores": cpu.get("cores") or None,
        "logicalCpus": cpu.get("logical_cpus") or None,
        "loadOrQueue": cpu.get("load_average_1m") if cpu.get("load_average_1m") is not None
                       else cpu.get("processor_queue_length"),
        "memTotalGB": memory.get("total_gb"),
        "memUsedGB": memory.get("used_gb"),
        "memPct": memory.get("usage_percent"),
        "swapPct": memory.get("swap_usage_percent"),
        "services": [{"name": item.get("name"), "state": item.get("state"), "status": item.get("status")}
                     for item in (os_health.get("services") or [])],
        "topCpu": top_cpu[:3],
        "topMemory": top_memory[:3],
        "network": os_health.get("network") or [],
        "events": [item for item in events if int(item.get("count") or 0) > 0],
        "pendingReboot": os_health.get("pending_reboot") or "",
    }


def _platform(db0, os_info: dict | None = None) -> str:
    dt = db0.get("db_type")
    if not db0:
        # Host with no database (e.g. an ERP AOS app VM): OS metrics only.
        family = (os_info or {}).get("os_family") or ""
        return f"{family} · OS only" if family else "OS only"
    if dt != "sqlserver":
        # Oracle records "version" (e.g. "Oracle 8i"); other engines record db_type.
        return db0.get("version") or {"oracle": "Oracle", "mysql": "MySQL",
                                       "postgresql": "PostgreSQL"}.get(dt, dt or "")
    short = _short_edition(db0.get("edition", ""))
    year = ""
    m = re.search(r"SQL Server (\d{4})", db0.get("version", "") or "")
    if m and short in ("Std", "Ent"):
        year = f" {m.group(1)}"
    return "SQL Server · " + (short + year if short else "")




def _build_backup(server) -> dict:
    """The Backup column, decided by the per-database policy rather than by newest evidence.

    Coverage used to be inferred from "does a FULL/DIFF/LOG row exist anywhere on this instance",
    which labelled a server Full+Diff+Log whose DIFF evidence covered one database out of six, and
    the note said "LOG chain stale" — an assertion about chain continuity that no collected metric
    supports. Both now come from :mod:`db_ops.lib.backup_policy`: what each database is
    *required* to have, how many comply, and "LOG RPO violated" instead of "chain broken".
    """
    if not _primary_db(server):
        return {"cov": "—", "full": None, "diff": None, "log": None,
                "fullAgeHours": None, "diffAgeHours": None, "logAgeHours": None,
                "logStale": False, "compliant": None, "eligible": None, "worstDatabases": [],
                "note": "No database on this host"}
    # Prefer the fresh metric-derived backup_evidence health block; fall back to the
    # semi-static backup.latest_by_type only when the overlay has not been merged yet.
    lbt = server.get("backup_evidence") or ((server.get("backup") or {}).get("latest_by_type")) or {}
    full_row, diff_row, log_row = (lbt.get("FULL") or {}), (lbt.get("DIFF") or {}), (lbt.get("LOG") or {})
    full = full_row.get("latest_finish")
    diff = diff_row.get("latest_finish")
    log = log_row.get("latest_finish")
    full_age = as_float(full_row.get("latest_age_hours"))
    diff_age = as_float(diff_row.get("latest_age_hours"))
    log_age = as_float(log_row.get("latest_age_hours"))
    summary = ((server.get("backup_policy") or {}).get("summary")) or {}

    if not (full or diff or log):
        return {"cov": "No metrics", "full": None, "diff": None, "log": None,
                "fullAgeHours": None, "diffAgeHours": None, "logAgeHours": None,
                "logStale": False, "compliant": None, "eligible": None, "worstDatabases": [],
                "note": "No backup metrics in window"}

    if summary.get("eligible"):
        cov = backup_policy.coverage_text(summary)
        note = f"{summary.get('compliant', 0)}/{summary['eligible']} DB within policy"
        if summary.get("reason"):
            note = summary["reason"]
    else:
        # No policy verdict (an older overlay, or a server whose backup metrics did not run):
        # say what evidence exists and no more.
        cov = "Full+Diff+Log" if (full and log and diff) else ("Full+Log" if (full and log)
              else ("Full only" if (full or diff) else "Log only"))
        note = f"{full_row.get('database_count')} DB" if full_row.get("database_count") else ""

    log_violated = (summary.get("byType") or {}).get("LOG", {}).get("state") == "VIOLATED"
    return {"cov": cov, "full": full, "diff": diff, "log": log,
            "fullAgeHours": full_age, "diffAgeHours": diff_age, "logAgeHours": log_age,
            "status": summary.get("status") or "",
            "compliant": summary.get("compliant"), "eligible": summary.get("eligible"),
            "worstDatabases": summary.get("worstDatabases") or [],
            # Kept under the old name because the template and markdown read it; the *meaning*
            # is now "the LOG RPO this policy sets is violated", not "the chain is broken".
            "logStale": bool(log_violated),
            "note": note}


def _build_cfg(server) -> dict:
    sr = merged_sql_resources(server)
    if not sr:
        return {"warns": len(server.get("config_warnings") or []), "govMissing": True}
    cpu = sr.get("sql_cpu") or {}
    mem = sr.get("memory") or {}
    ic = sr.get("important_config") or {}
    tdb = sr.get("tempdb") or {}
    tempdb = f"{tdb.get('total_mb', '')} MB / {tdb.get('file_count', '')} files" if tdb else "—"
    return {
        "cpu": cpu.get("sql_visible_cpu_count"),
        "sched": cpu.get("scheduler_count"),
        "maxdop": cpu.get("max_degree_of_parallelism"),
        "cost": cpu.get("cost_threshold_for_parallelism"),
        "maxmemMB": mem.get("max_server_memory_mb"),
        "committedMB": mem.get("sql_committed_mb"),
        "tempdb": tempdb,
        "backupCompr": ic.get("backup_compression_default"),
        "remoteDac": ic.get("remote_admin_connections"),
        "optAdhoc": ic.get("optimize_for_ad_hoc_workloads"),
        "blockedThr": ic.get("blocked_process_threshold_s"),
        "clr": ic.get("clr_enabled"),
        "xpcmd": ic.get("xp_cmdshell"),
        "warns": len(server.get("config_warnings") or []),
    }


def _build_disks(server) -> list:
    out = []
    for mount, info in merged_drives(server).items():
        label = info.get("logical_volume_name")
        m = f"{mount} {label}" if label else mount
        pct = info.get("free_percent")
        # st = the status the metric/inventory-health layer already computed for this drive
        # (OK/WARNING/CRITICAL). Severity decisions downstream use this, not a re-hardcoded %.
        status = str(info.get("status") or "").upper()
        if status in ("CRITICAL", "ERROR"):
            severity = "crit"
        elif status in ("WARNING", "WARN"):
            severity = "warn"
        elif status in ("UNKNOWN", "NO_DATA"):
            severity = "idle"
        elif status:
            severity = "ok"
        elif pct is None:
            severity = "idle"
        elif pct < DISK_CRIT_PCT:
            severity = "crit"
        elif pct < DISK_WARN_PCT:
            severity = "warn"
        else:
            severity = "ok"
        entry = {
            "m": m,
            "free": pct,
            "freeGB": info.get("free_gb"),
            "totalGB": info.get("total_gb"),
            "st": info.get("status"),
            "sev": severity,
        }
        if pct is None and info.get("status") and info["status"] != "OK":
            entry["flag"] = info["status"]
        out.append(entry)
    return out


def _build_detail(server, db0) -> list:
    """Extra structured fields surfaced in Server Detail that the template's base schema
    doesn't already render (database sizes, FCI mapping, governance extras, deployment,
    oracle params, backup jobs, notes). Each entry is a ``[label, value]`` pair."""
    out = []
    sr = merged_sql_resources(server)

    # Engine build first. "RTM" on its own reads as an unpatched instance, so the cumulative
    # update and its KB are shown next to it - SQL Server 2022 RTM-CU26 is 26 CUs in, not fresh.
    inst = server.get("instance_health") or {}
    if inst:
        build = " ".join(x for x in (inst.get("product_version"), inst.get("product_level")) if x)
        patch = " ".join(x for x in (inst.get("cumulative_update"), inst.get("update_reference")) if x)
        if build or patch:
            out.append(["Engine build", " · ".join(x for x in (build, patch) if x)])
        edition = " · ".join(x for x in (inst.get("edition"), inst.get("engine_edition")) if x)
        if edition:
            out.append(["Edition", edition])
        host_bits = [inst.get("machine_name"), inst.get("physical_name")]
        host = " · ".join(dict.fromkeys(x for x in host_bits if x))
        if inst.get("clustered"):
            host += f" · clustered {inst['clustered']}"
        if host.strip(" ·"):
            out.append(["Instance host", host])
        endpoint = ":".join(x for x in (inst.get("listen_ip"), inst.get("listen_port")) if x)
        endpoint_bits = [endpoint, inst.get("transport"), inst.get("auth_scheme")]
        endpoint_txt = " · ".join(x for x in endpoint_bits if x)
        if endpoint_txt:
            out.append(["Listening on", endpoint_txt])
        if inst.get("collation"):
            out.append(["Server collation", inst["collation"]])
        if inst.get("started_at"):
            out.append(["Engine started", inst["started_at"]])

    if db0.get("server_name"):
        out.append(["Server name", db0["server_name"]])
    dep = server.get("deployment") or {}
    if dep:
        bits = [dep.get("platform"), dep.get("host_os"), dep.get("container_or_node_name")]
        txt = ", ".join(str(b) for b in bits if b)
        if dep.get("sql_data_root"):
            txt += f" · data {dep['sql_data_root']}"
        out.append(["Deployment", txt])
    fci = server.get("fci_cluster") or {}
    if fci:
        parts = []
        if fci.get("listener_name"):
            parts.append(f"listener {fci['listener_name']}")
        if fci.get("current_node_name"):
            parts.append(f"active {fci['current_node_name']} ({fci.get('current_node_ip')})")
        for n in fci.get("inactive_nodes", []) or []:
            parts.append(f"inactive {n.get('node_name')} ({n.get('ip')})")
        if fci.get("validation_status"):
            parts.append(f"validation {fci['validation_status']}")
        out.append(["FCI cluster", ", ".join(parts)])
    if sr:
        cpu, mem = sr.get("sql_cpu") or {}, sr.get("memory") or {}
        host_cpu = (sr.get("host_cpu") or {}).get("logical_processor_count")
        out.append(["Governance (extra)",
                    f"host logical CPU {host_cpu}, SQL min {mem.get('min_server_memory_mb')} MB, "
                    f"NUMA online schedulers {cpu.get('numa_online_scheduler_count_total')}"])
    # Database sizes are now a column of the per-database table, taken from live metrics.
    # The static list stays only when metrics carried no database at all, and says so —
    # showing both would put two different numbers for the same file on one page.
    sizes = sr.get("database_sizes") or []
    if sizes and not (server.get("database_health") or []):
        out.append(["Database sizes", ", ".join(
            f"{x.get('database_name')}={x.get('size_mb')}MB" for x in sizes), "static"])
    params = (server.get("oracle_resources") or {}).get("parameters") or {}
    if params:
        out.append(["Oracle parameters", ", ".join(f"{k}={v}" for k, v in params.items())])
    # Live per-job outcomes from BACKUP_JOB_STATUS. The canonical inventory carries the same
    # shape under backup.jobs but is never refreshed, so it was reporting runs from June next to
    # current backup evidence. The static copy is the fallback for a server with no such metric.
    live_jobs = server.get("backup_jobs") or []
    if live_jobs:
        out.append(["Backup jobs", "; ".join(
            f"{j['job']}: {j['last_status']}" + (f" at {j['last_run']}" if j.get("last_run") else "")
            for j in live_jobs)])
    jobs = [] if live_jobs else ((server.get("backup") or {}).get("jobs") or [])
    if jobs:
        out.append(["Backup jobs", "; ".join(
            f"{j.get('job_name')}: {j.get('last_status')} at {j.get('last_run_datetime')}"
            for j in jobs if j.get("job_name")), "static"])
    notes = server.get("notes") or []
    if notes:
        out.append(["Notes", " · ".join(str(n) for n in notes)])
    return out


def _build_databases(server) -> list:
    """One row per database for this server's detail block.

    The join itself lives in :func:`inventory_health.build_database_rows` because the per-server
    page renders the same table from the same two blocks; only the static-size fallback is this
    report's own, since it is the only one with a canonical inventory file to fall back to.
    """
    return inventory_health.build_database_rows(
        server.get("database_health"),
        server.get("backup_by_database"),
        static_sizes={str(x.get("database_name")): as_float(x.get("size_mb"))
                      for x in (merged_sql_resources(server).get("database_sizes") or [])},
    )


def _build_security(server) -> dict:
    """Failed logins, stale passwords and certificate expiry for one server.

    The report had no security section at all, so an instance taking ~10k failed logins a day
    against a single principal read as healthy. ``worst`` is what the fleet row shows; the
    lists are what the detail block expands to.
    """
    sec = server.get("security_health") or {}
    principals = [p for p in (sec.get("failed_login_principals") or []) if (p.get("attempts") or 0) > 0]
    passwords = sec.get("passwords_over_threshold") or []
    worst_login = principals[0] if principals else None
    return {
        "failed24h": sec.get("failed_logins_24h"),
        "principals": principals,
        "worstLogin": worst_login,
        "passwordsOld": passwords,
        "passwordThresholdDays": sec.get("password_threshold_days"),
        "oldestPasswordDays": (passwords[0].get("age_days") if passwords else None),
        "certsExpired": sec.get("certificates_expired"),
        "certsExpiring30d": sec.get("certificates_expiring_30d"),
        "asOf": sec.get("as_of") or "",
        "collected": bool(sec),
    }


def _build_freshness(server) -> dict:
    """How current this server's blocks are, so the page can say which and how old.

    ``health_as_of`` is stamped by the metrics overlay; a server the overlay never reached
    keeps its previous blocks and an older stamp (or none). That difference is the whole
    point: values from a June collection must not render the same as values from an hour ago.
    """
    st = server.get("inventory_status") or {}
    as_of = str(server.get("health_as_of") or "").strip()
    age_hours = None
    if as_of:
        try:
            # metric_results stores UTC (docs/14 timezone convention), so compare in UTC.
            stamp = datetime.datetime.strptime(as_of[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc)
            # Clamped at 0: a sample stamped slightly ahead of this node's clock is clock
            # skew, and "-3h ago" reads like a bug in the report rather than one in NTP.
            age_hours = max(0.0, round(
                (datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds() / 3600.0, 1))
        except ValueError:
            age_hours = None
    db_meta = str(st.get("db_metadata") or "")
    return {
        "asOf": as_of,
        "ageHours": age_hours,
        "lastChecked": str(st.get("last_checked") or ""),
        # No live metrics at all, or metrics that could not read the database: either way the
        # numbers on this row are the last known ones, not the current ones.
        "stale": bool(as_of == "" or (age_hours is not None and age_hours > STALE_DATA_HOURS)),
        "collectFailed": db_meta not in ("", "ok", "not_applicable"),
        "collectReason": str(st.get("reason") or "")[:200],
    }


def _build_inv(server) -> str:
    st = server.get("inventory_status") or {}
    parts = [f"last_checked {st.get('last_checked', '')}"]
    if st.get("db_metadata") and st["db_metadata"] not in ("ok", "not_applicable"):
        parts.append(f"db_metadata {st['db_metadata']}")
    elif st.get("os_resources"):
        parts.append(str(st["os_resources"]))
    if st.get("reason"):
        parts.append(str(st["reason"])[:160])
    if st.get("metric_rows") is not None:
        parts.append(f"metric_rows {st['metric_rows']}")
    return " · ".join(parts)


def _status(model) -> str:
    """Worst-severity status: crit > warn > idle (no data) > ok.

    Critical conditions (broken log chain, a disk at/over the critical threshold) win even
    when a host has no live DB metrics — e.g. 2-101 has no DB login but a near-full disk, so
    it is **crit**, not 'no data'. This keeps the fleet KPI counts consistent with the crit
    items shown in Priority Attention. A host is 'idle' only when nothing actionable at all
    was collected. Disk severity follows the metric-computed ``st`` (CRITICAL/WARNING)."""
    bk = model["backup"]
    disks = model["disks"]
    osh = model.get("os_health") or {}
    # The collectors' own verdict comes first. Each collector computed its status against the
    # thresholds in its own SQL, and the per-server detail page reports exactly that. The rules
    # below only ever looked at a hand-picked subset of signals, so a server whose collectors
    # reported CRITICAL - 1533 stack dumps in LOG_RECENT_CRITICAL, sessions pinning locks - read
    # healthy on the fleet page and critical on its own. The two pages now rest on the same fact.
    severity = str((model.get("severity") or {}).get("worst") or "").upper()
    crit_disk = any((str(d.get("st") or "").upper() == "CRITICAL")
                    or (d.get("free") is not None and d["free"] < DISK_CRIT_PCT) for d in disks)
    warn_disk = any((str(d.get("st") or "").upper() == "WARNING")
                    or (d.get("free") is not None and d["free"] < DISK_WARN_PCT) for d in disks)
    # A configured service that is not running is the OS equivalent of a database offline:
    # it is the reason the host is monitored at all.
    stopped_service = any(str(item.get("status") or "").upper() == "CRITICAL"
                          for item in (osh.get("services") or []))
    missing_service = any(str(item.get("state") or "").upper() == "NOT_FOUND"
                          for item in (osh.get("services") or []))

    # --- CRITICAL ---
    if severity == "CRITICAL":
        return "crit"
    if str(bk.get("status") or "").upper() == "CRITICAL" or crit_disk or stopped_service:
        return "crit"
    # --- WARNING ---
    if severity == "WARNING":
        return "warn"
    if model["total"] and model["online"] is not None and model["online"] < model["total"]:
        return "warn"
    if str(bk.get("status") or "").upper() == "WARNING":
        return "warn"
    if model["ple"] and 0 < model["ple"] < PLE_WARN:
        return "warn"
    if warn_disk or missing_service:
        return "warn"
    if (osh.get("cpuPct") or 0) >= CPU_WARN_PCT or (osh.get("memPct") or 0) >= MEMORY_WARN_PCT:
        return "warn"
    if model.get("oracle") and (model["total"] or 0) > (model["online"] or 0):
        return "warn"
    # --- IDLE (no actionable data) vs OK ---
    # A host with no database is fully inventoried once its OS metrics land; DB-level
    # signals (databases online, PLE, sessions) never exist for it.
    has_metrics = bool(model["disks"] or osh) if model.get("osOnly") else bool(
        model["total"] or model["ple"] or model["sessions"])
    return "ok" if has_metrics else "idle"


# --------------------------------------------------------------------------- #
# Model + triage
# --------------------------------------------------------------------------- #
def _merge_findings(server) -> list:
    """Static findings, the metric codes the collectors flagged, and the config warnings.

    The severity line comes first and names the metric codes responsible. A status badge with no
    stated reason is what sent a reader to the detail page to find out why the two disagreed, so
    the fleet page now carries the same answer.
    """
    merged = []
    severity = server.get("metric_severity") or {}
    critical = severity.get("critical_codes") or []
    warning = severity.get("warning_codes") or []
    if critical:
        merged.append(
            f"{severity.get('critical_rows', 0)} critical metric result(s) from: "
            f"{', '.join(critical[:6])}{', ...' if len(critical) > 6 else ''}"
        )
    elif warning:
        merged.append(
            f"{severity.get('warning_rows', 0)} warning metric result(s) from: "
            f"{', '.join(warning[:6])}{', ...' if len(warning) > 6 else ''}"
        )
    for item in list(server.get("findings") or []) + list(server.get("config_warnings") or []):
        if item not in merged:
            merged.append(item)
    return merged


def build_models(data: dict, *, exclude_ip_prefixes=EXCLUDE_IP_PREFIXES):
    servers_raw = [s for s in data.get("servers", [])
                   if not any(str(s.get("ip", "")).startswith(p) for p in exclude_ip_prefixes)]
    models = []
    for idx, s in enumerate(servers_raw):
        db0 = _primary_db(s)
        dt = db0.get("db_type")
        dh = s.get("database_health") or []
        ph = s.get("performance_health") or {}
        os_res = s.get("os_resources") or {}
        mem = merged_sql_resources(s).get("memory") or {}

        oh = s.get("os_health") or {}
        os_info = oh.get("os_info") or {}
        os_cpu = oh.get("cpu") or {}
        os_mem = oh.get("memory") or {}

        total = len(dh) if dh else None
        online = sum(1 for d in dh if inventory_health.is_database_online(d.get("state"))) if dh else None
        model = {
            "ip": s.get("ip"),
            "server_id": s.get("server_id", ""),
            "company": s.get("company_code", ""),
            "osOnly": not db0,
            # Identify by server_id. It is the key everything else is addressed by - the metric
            # rows, the overlay, the per-server charts URL - so a card headed "SALESCLUSTER"
            # (the engine's machine name) could not be matched back to the server it describes.
            # The machine name is still shown, as the endpoint line under it.
            "role": s.get("server_id") or db0.get("machine_name") or db0.get("server_name")
                    or os_info.get("hostname") or s.get("role"),
            "machine": db0.get("machine_name") or db0.get("server_name") or os_info.get("hostname") or "",
            "endpoint": db0.get("endpoint_name") or db0.get("server_name") or os_info.get("hostname") or "—",
            "platform": _platform(db0, os_info),
            # The live OS metric wins over the static WinRM inventory and the DB's view of it.
            "os": os_info.get("os_name") or os_res.get("os_caption") or db0.get("os") or "—",
            "ramGB": os_mem.get("total_gb") or mem.get("host_physical_memory_gb") or os_res.get("physical_memory_gb"),
            "online": online,
            "total": total,
            "ple": ph.get("page_life_expectancy") if ph else None,
            "sessions": ph.get("active_session_count") if ph else None,
            "backup": _build_backup(s),
            "ha": " · ".join(str(x) for x in ((s.get("ha") or {}).get("mode"),
                                              (s.get("ha") or {}).get("role")) if x) or "none_documented",
            "cfg": _build_cfg(s),
            "disks": _build_disks(s),
            "dbs": db0.get("database_names") or db0.get("schemas") or [],
            "databases": _build_databases(s),
            "inv": _build_inv(s),
            # config_warnings previously reached the HTML only as a *count* in the Config
            # column - the text existed solely in the Markdown summary, so a reader of the page
            # could see "3 warnings" and had no way to learn what they were. The template already
            # renders a findings list; these belong in it.
            "findings": _merge_findings(s),
            "detail": _build_detail(s, db0),
            "instance": s.get("instance_health") or {},
            "severity": s.get("metric_severity") or {},
            # The per-server page's own problem list, carried through the overlay. Priority
            # Attention is rendered from this, so the two pages cannot describe the same server
            # differently — which they did, in both directions.
            "problems": s.get("metric_problems") or [],
            "freshness_detail": s.get("metric_freshness") or {},
            "os_health": _build_os(oh),
            "dbType": dt or "",
            "freshness": _build_freshness(s),
            "security": _build_security(s),
        }
        if dt == "oracle":
            model["oracle"] = True
        model["status"] = _status(model)
        # Display order. A server may set "server_order" in the inventory JSON to pin its
        # position; otherwise it defaults to its position in the file (1, 2, 3 ...). Every
        # tab (Fleet Matrix, Backup, Config, Server Detail) renders in this order.
        raw_order = s.get("server_order")
        model["server_order"] = int(raw_order) if isinstance(raw_order, (int, float)) else idx + 1
        models.append(model)

    models.sort(key=lambda m: (m["server_order"], str(m.get("server_id") or "")))

    # Counted by engine, not by "everything that is not Oracle": the PostgreSQL HA instances
    # would otherwise be reported as SQL Servers.
    sql = sum(1 for m in models if m.get("dbType") == "sqlserver")
    ora = sum(1 for m in models if m.get("oracle"))
    pg = sum(1 for m in models if m.get("dbType") in ("postgresql", "postgres"))
    os_only = sum(1 for m in models if m.get("osOnly"))
    health = sum(1 for m in models if m["total"] is not None)
    scope = {"servers": len(models), "sqlserver": sql, "oracle": ora, "postgresql": pg,
             "os_only": os_only, "health": health}
    return scope, models


def _ip_tag(m):
    # short tag like "2-115" from the last two IP octets, matching the template style.
    octets = str(m["ip"]).split(".")
    return "-".join(octets[-2:]) if len(octets) >= 2 else str(m["ip"])


#: Conditions Priority Attention already explains in its own words, with its own action. Their
#: metric rows would otherwise arrive a second time as a generic card.
_TRIAGE_OWNED_CODES = {
    "OS_SERVICE_STATUS", "BACKUP_AGE", "BACKUP_LAST_RESULT",
    "SECURITY_FAILED_LOGINS", "SECURITY_LOGIN_HEALTH",
    "STORAGE_DISK_FREE_SPACE", "OS_DISK_USAGE", "PAGE_LIFE_EXPECTANCY",
}
_SEV_FROM_SEVERITY = {"CRITICAL": "crit", "WARNING": "warn"}


def _metric_problem_cards(models: list) -> list:
    """One card per metric condition that is true **now**, at the severity the classifier gave it.

    The overlay carries the same grouped problem list the per-server page renders
    (``metric_problems``, built by :func:`inventory_health.build_metric_problems`), so a finding
    cannot exist on one page and not the other. Findings are merged across servers by metric code
    so the section stays readable on an 18-server fleet, and every line names its server, its
    worst item and when the sample was taken.
    """
    by_code: dict[str, dict] = {}
    for model in models:
        for group in model.get("problems") or []:
            code = str(group.get("code") or "")
            if code in _TRIAGE_OWNED_CODES:
                continue
            severity = str(group.get("severity") or "")
            if severity not in _SEV_FROM_SEVERITY:
                continue
            entry = by_code.setdefault(code, {
                "label": group.get("label") or code,
                "action": group.get("action") or "",
                "severity": severity,
                "lines": [], "tags": [], "items": 0,
            })
            if severity == "CRITICAL":
                entry["severity"] = "CRITICAL"
            entry["items"] += int(group.get("count") or 0)
            entry["tags"].append(_ip_tag(model))
            when = str(group.get("collectedAt") or "")
            entry["lines"].append(
                f"{model['role']} ({model['ip']}) — {group.get('headline') or ''}"
                + (f" [sampled {when} UTC]" if when else ""))

    cards = []
    for code, entry in by_code.items():
        cards.append({
            "sev": _SEV_FROM_SEVERITY[entry["severity"]],
            "title": f"{entry['label']} — {entry['items']} item(s) on "
                     f"{len(entry['lines'])} server(s)",
            "body": "; ".join(entry["lines"][:8])
                    + (f" (+{len(entry['lines']) - 8} more server(s))" if len(entry["lines"]) > 8 else "")
                    + f". Metric: {code}.",
            "action": entry["action"],
            "tags": sorted(set(entry["tags"])),
        })
    return cards


def _monitoring_gap_cards(models: list) -> list:
    """Metrics that are failing, late, or produced nothing at all.

    Monitoring that stopped is not a healthy server, and until this existed nothing said so: the
    fleet page took its overall age from the freshest metric on a server, so a target could read
    "3 minutes old" with a metric that had not returned in two days.
    """
    failing, late, missing = [], [], []
    for model in models:
        freshness = model.get("freshness_detail") or {}
        if freshness.get("failed"):
            failing.append(f"{model['role']} ({model['ip']}) — "
                           + ", ".join(freshness["failed"][:6]))
        if freshness.get("late"):
            late.append(f"{model['role']} ({model['ip']}) — " + ", ".join(freshness["late"][:6]))
        not_collected = [entry["code"] for entry in freshness.get("notCollected") or []]
        if not_collected:
            missing.append(f"{model['role']} ({model['ip']}) — " + ", ".join(not_collected[:6]))

    cards = []
    if failing:
        cards.append({"sev": "crit",
                      "title": f"Metrics currently failing on {len(failing)} server(s)",
                      "body": "The newest run of these metrics did not return a usable result, so "
                              "whatever the page shows for them is the last value from before they "
                              "broke: " + "; ".join(failing) + ".",
                      "action": "Read the collector error on the server page, then fix the "
                                "credential, permission or connectivity behind it. Until it "
                                "returns, treat that area as unknown, not healthy.",
                      "tags": [_ip_tag(m) for m in models if (m.get("freshness_detail") or {}).get("failed")]})
    if late:
        cards.append({"sev": "warn",
                      "title": f"Metrics overdue on {len(late)} server(s)",
                      "body": "No attempt for at least three of the metric's own intervals: "
                              + "; ".join(late) + ".",
                      "action": "Check the scheduler and the metric's time_window; a windowed "
                                "metric outside its hours is expected, a five-minute metric hours "
                                "behind is not.",
                      "tags": [_ip_tag(m) for m in models if (m.get("freshness_detail") or {}).get("late")]})
    if missing:
        cards.append({"sev": "warn",
                      "title": f"Catalog metrics with no evidence on {len(missing)} server(s)",
                      "body": "Active in the metric catalog for this engine but no rows in the "
                              "window — either never collected here, or switched off for this "
                              "target: " + "; ".join(missing) + ".",
                      "action": "Confirm each is deliberately disabled (metrics.metric_overrides "
                                "in db_instances.json). Anything not deliberately off is a "
                                "monitoring blind spot, not a healthy signal.",
                      "tags": [_ip_tag(m) for m in models
                               if (m.get("freshness_detail") or {}).get("notCollected")]})
    return cards


def build_triage(models: list) -> list:
    cards = []

    stopped = [
        (m, item)
        for m in models
        for item in ((m.get("os_health") or {}).get("services") or [])
        if str(item.get("status") or "").upper() == "CRITICAL" or str(item.get("state") or "").upper() == "NOT_FOUND"
    ]
    if stopped:
        down = [pair for pair in stopped if str(pair[1].get("status") or "").upper() == "CRITICAL"]
        cards.append({"sev": "crit" if down else "warn",
                      "title": f"{len(stopped)} monitored service(s) are not running",
                      "body": "A service the host is monitored for is stopped or missing: "
                              + "; ".join(f"{m['role']} ({m['ip']}) — {item.get('name')}: {item.get('state')}"
                                          for m, item in stopped)
                              + ". On an application host this is the workload itself being down.",
                      "action": "Start the service and check why it stopped (event log, service recovery "
                                "settings). If the name is wrong, fix OS_SERVICE_NAMES for that target.",
                      "tags": sorted({m["ip"] for m, _ in stopped})})

    violated = [m for m in models if m["backup"].get("logStale")]
    if violated:
        cards.append({"sev": "crit",
                      "title": f"Transaction-log RPO violated on {len(violated)} server(s)",
                      "body": "The newest LOG backup is past the RPO these databases are held to: "
                              + "; ".join(
                                  f"{m['role']} ({m['ip']}) — {m['backup']['note']}"
                                  + (f" [{', '.join(m['backup']['worstDatabases'][:5])}]"
                                     if m['backup'].get('worstDatabases') else "")
                                  for m in violated)
                              + ". Point-in-time recovery is unavailable for the period since that "
                                "backup. Whether the log *chain* is intact is a separate question: "
                                "it needs backup LSN and recovery-fork evidence, which is not collected.",
                      "action": "Check the LOG backup job, its destination and its credentials, and "
                                "confirm no database silently switched to SIMPLE. Verify LSN continuity "
                                "before deciding a fresh FULL is required — taking one when the chain "
                                "was intact discards a working restore path.",
                      "tags": [m["ip"] for m in violated]})

    aging = [m for m in models
             if str(m["backup"].get("status") or "").upper() == "WARNING" and not m["backup"].get("logStale")]
    if aging:
        cards.append({"sev": "warn",
                      "title": f"Backups approaching their policy limit on {len(aging)} server(s)",
                      "body": "; ".join(f"{m['role']} ({m['ip']}) — {m['backup']['note']}" for m in aging) + ".",
                      "action": "Check the schedule before the next run misses; confirm the job is enabled.",
                      "tags": [m["ip"] for m in aging]})

    idle = [m for m in models if m["status"] == "idle"]
    if idle:
        cards.append({"sev": "warn",
                      "title": f"{len(idle)} target(s) cannot be fully inventoried (auth / connectivity)",
                      "body": "Backup and health posture is unverifiable on: "
                              + "; ".join(f"{_ip_tag(m)} ({m['inv']})" for m in idle)
                              + ". These are blind spots, not confirmed-healthy.",
                      "action": "Fix monitoring credentials / firewall / legacy client so collection succeeds; "
                                "re-run before trusting any 'OK'.",
                      "tags": [_ip_tag(m) for m in idle]})

    # "No LOG required" is a policy fact, not a defect — but on a production database it is a
    # decision worth re-reading, because it means the RPO is one whole FULL cycle.
    full_only = [m for m in models
                 if (m["backup"].get("eligible") or 0)
                 and m["backup"]["cov"].startswith("Full")
                 and "LOG not required" in m["backup"]["cov"]]
    if full_only:
        cards.append({"sev": "warn",
                      "title": f"{len(full_only)} server(s) have no LOG backup requirement",
                      "body": "Policy requires no transaction-log backup on: "
                              + ", ".join(f"{m['role']} ({m['ip']})" for m in full_only)
                              + ". Worst-case data loss is a whole FULL cycle.",
                      "action": "Confirm the recovery model is deliberately SIMPLE. If it is not, "
                                "move the databases to FULL recovery and schedule log backups.",
                      "tags": [m["ip"] for m in full_only]})

    # Security. A login being hammered thousands of times a day is either an attack or an
    # integration holding a dead credential; both are findings, and both were invisible here.
    hammered = [m for m in models
                if (m.get("security") or {}).get("worstLogin")
                and (m["security"]["worstLogin"].get("attempts") or 0) >= FAILED_LOGIN_CRIT]
    if hammered:
        cards.append({"sev": "crit",
                      "title": f"Sustained failed logins on {len(hammered)} server(s)",
                      "body": "A single principal is failing to authenticate thousands of times a day: "
                              + "; ".join(
                                  f"{m['role']} ({m['ip']}) — {m['security']['worstLogin']['login']}: "
                                  f"{int(m['security']['worstLogin']['attempts']):,} attempts/24h"
                                  for m in hammered)
                              + ". Either the credential is being guessed, or an integration is retrying "
                                "a dead one — and the error log is being filled either way.",
                      "action": "Identify the source host of the attempts, then disable/fix the principal "
                                "or block the source. Do not simply raise the alert threshold.",
                      "tags": [m["ip"] for m in hammered]})

    stale_pw = [m for m in models
                if (m.get("security") or {}).get("oldestPasswordDays")
                and m["security"]["oldestPasswordDays"] >= PASSWORD_AGE_WARN_DAYS]
    if stale_pw:
        cards.append({"sev": "warn",
                      "title": f"SQL logins with passwords older than {PASSWORD_AGE_WARN_DAYS} days",
                      "body": "; ".join(
                          f"{m['role']} ({m['ip']}) — {len(m['security']['passwordsOld'])} login(s), "
                          f"oldest {int(m['security']['oldestPasswordDays'])} days"
                          for m in stale_pw) + ".",
                      "action": "Rotate the service logins on a schedule; start with sa and anything with "
                                "db_owner.",
                      "tags": [m["ip"] for m in stale_pw]})

    low_ple = [m for m in models if m["ple"] and 0 < m["ple"] < PLE_WARN]
    if low_ple:
        cards.append({"sev": "warn",
                      "title": "Memory pressure — low PLE under session load",
                      "body": "Page Life Expectancy is low (buffer pool churning) on: "
                              + "; ".join(f"{m['role']} (PLE {m['ple']} · {m['sessions']} sessions)" for m in low_ple) + ".",
                      "action": "Review max server memory vs. host RAM, top missing indexes, plan-cache bloat "
                                "(enable optimize-for-ad-hoc).",
                      "tags": [_ip_tag(m) for m in low_ple]})

    # Disk severity follows the status the metric layer already computed (CRITICAL/WARNING),
    # falling back to the collected free% only when a status is absent. A near-full volume
    # (CRITICAL, e.g. 2-101 D: at 0.01%) becomes its own crit item, not a lumped warning.
    crit_disk, warn_disk = [], []
    for m in models:
        for d in m["disks"]:
            st = str(d.get("st") or "").upper()
            free = d.get("free")
            label = (f"{_ip_tag(m)} {d['m']} ({free}% free)" if free is not None
                     else f"{_ip_tag(m)} {d['m']} ({d.get('flag') or st})")
            if st == "CRITICAL" or (free is not None and free < DISK_CRIT_PCT):
                crit_disk.append(label)
            elif st == "WARNING" or (free is not None and free < DISK_WARN_PCT):
                warn_disk.append(label)
    if crit_disk:
        cards.append({"sev": "crit",
                      "title": f"Disk critically low (near full) on {len(crit_disk)} volume(s)",
                      "body": "Volumes at/near 0% free — imminent risk of data/log write failure: "
                              + "; ".join(crit_disk) + ".",
                      "action": "Free space / extend the volume now; archive old backups.",
                      "tags": crit_disk})
    if warn_disk:
        cards.append({"sev": "warn",
                      "title": "Low disk headroom on production volumes",
                      "body": "Approaching the danger zone for data/log growth: " + "; ".join(warn_disk) + ".",
                      "action": "Plan capacity / archive old backups; set disk alerts at 15%.",
                      "tags": warn_disk})

    xpcmd = [m for m in models if (m["cfg"].get("xpcmd") == 1)]
    if xpcmd:
        cards.append({"sev": "info",
                      "title": "Security — xp_cmdshell enabled",
                      "body": "xp_cmdshell is ON for: " + ", ".join(f"{m['role']} ({m['ip']})" for m in xpcmd)
                              + ". Often needed by legacy jobs but widens the attack surface.",
                      "action": "Confirm intentional / scope it; otherwise disable.",
                      "tags": [m["ip"] for m in xpcmd]})

    standalone = [m for m in models if m["total"] and not m.get("oracle")
                  and "none_documented" in m["ha"] and "standalone" in m["ha"]]
    if standalone:
        cards.append({"sev": "info",
                      "title": "No documented HA/DR for standalone production",
                      "body": "Core data on standalone servers without a documented DR strategy: "
                              + ", ".join(f"{m['role']} ({_ip_tag(m)})" for m in standalone) + ".",
                      "action": "Document or stand up a DR strategy for these servers.",
                      "tags": [_ip_tag(m) for m in standalone]})

    cards.extend(_metric_problem_cards(models))
    cards.extend(_monitoring_gap_cards(models))

    # Curated findings recorded directly in the inventory (low disk on hosts without live
    # metrics, "SQL resource not connected", stale DIFF, …). Low-disk ones are skipped — the
    # low_disk card above already covers them from the merged disk source.
    #
    # These are notes a person wrote once, not current measurements, so they sit at the bottom
    # and stay 'info'. Packing live CRITICAL findings in with them — which is what happened to
    # everything build_triage had no hand-coded card for — is how a server with 42 blocked
    # sessions reached Priority Attention as one sentence inside an informational paragraph.
    documented = []
    for m in models:
        extra = [f for f in m.get("findings", []) if not str(f).lower().startswith("low disk")]
        if extra:
            documented.append(f"{m['role']} ({_ip_tag(m)}): " + "; ".join(extra))
    if documented:
        cards.append({"sev": "info",
                      "title": "Documented inventory notes (not live measurements)",
                      "body": "Recorded in the inventory (incl. hosts without live DB metrics): "
                              + " · ".join(documented) + ".",
                      "action": "Review each; fix collection where a finding is 'not connected'.",
                      "tags": [_ip_tag(m) for m in models if [f for f in m.get('findings', [])
                                                              if not str(f).lower().startswith('low disk')]]})

    drift = [m for m in models if not m.get("oracle") and not m["cfg"].get("govMissing")
             and (m["cfg"].get("backupCompr") == 0 or m["cfg"].get("remoteDac") == 0
                  or m["cfg"].get("optAdhoc") == 0 or (m["cfg"].get("cost") or 99) < 30)]
    if drift:
        cards.append({"sev": "info",
                      "title": "Fleet-wide config baseline drift (quick wins)",
                      "body": f"Across {len(drift)} server(s): backup compression OFF, remote DAC OFF, "
                              "optimize-for-ad-hoc OFF, blocked-process-threshold 0, and/or "
                              "cost-threshold-for-parallelism at the default 5.",
                      "action": "Apply a documented SQL config baseline via script; re-scan to confirm.",
                      "tags": [f"{len(drift)} servers", "baseline"]})

    # Order so the most severe items lead Priority Attention: crit, then warn, then info.
    return sorted(cards, key=lambda c: {"crit": 0, "warn": 1, "info": 2}.get(c["sev"], 3))


# --------------------------------------------------------------------------- #
# HTML render (inject data into the shipped template)
# --------------------------------------------------------------------------- #
def build_fleet_linked_servers(sqlite_path, *, days: int, as_of: str | None = None) -> list[dict]:
    """Every linked server on the estate, one row each, with the same verdict the server page gives.

    The per-server page answers "should THIS instance's linked servers still exist". The fleet has
    a different question that no per-server page can answer: a dead target is usually referenced
    from several instances at once, and the drop list is a piece of work somebody schedules once —
    both need the whole estate on one screen.

    Reuses :func:`server_report.build_linked_servers` rather than re-deriving the rules, so the two
    pages cannot disagree about whether something is droppable.
    """
    from db_ops.lib import health_model
    from db_ops.db.metric_store import MetricStore
    from db_ops.reports.server_report import LINKED_SERVER_CODE, build_linked_servers

    by_server: dict[str, list[dict]] = {}
    for row in MetricStore(sqlite_path).fetch_health_metrics(
            codes=[LINKED_SERVER_CODE], days=int(days), as_of=as_of):
        by_server.setdefault(str(row.get("server_id") or ""), []).append(row)

    rows: list[dict] = []
    for server_id in sorted(by_server):
        for entry in build_linked_servers(health_model.latest_snapshot(by_server[server_id])):
            rows.append({**entry, "server_id": server_id})
    # Worst first, then heaviest: a FIX is an outage waiting to happen, and among the DROPs the one
    # with the most objects behind it is the biggest piece of work.
    order = {"FIX": 0, "DROP": 1, "REVIEW": 2, "KEEP": 3}
    rows.sort(key=lambda r: (order[r["verdict"]], -r["objects"], r["server_id"], r["name"].casefold()))
    return rows


def render_html(scope, models, triage, date_iso, linked_servers=None) -> str:
    template = TEMPLATE_HTML.read_text(encoding="utf-8")
    return (template
            .replace("__SNAPSHOT_DATE__", date_iso)
            .replace("__SCOPE__", json.dumps(scope, ensure_ascii=False))
            .replace("__SERVERS__", json.dumps(models, ensure_ascii=False, indent=2))
            .replace("__TRIAGE__", json.dumps(triage, ensure_ascii=False, indent=2))
            .replace("__LINKED_SERVERS__",
                     json.dumps(linked_servers or [], ensure_ascii=False, indent=2)))


# --------------------------------------------------------------------------- #
# Markdown render (same content, static)
# --------------------------------------------------------------------------- #
_SEV_RANK = {"crit": 0, "warn": 1, "idle": 2, "ok": 3}
_STATUS_EMOJI = {"crit": "🔴", "warn": "🟠", "ok": "🟢", "idle": "⚪"}


def _fmt(n):
    return "—" if n is None else (f"{n:,}" if isinstance(n, (int, float)) else str(n))


def _max_mem(mb):
    if mb is None:
        return "—"
    if mb == UNCAPPED_MEM_MB:
        return "∞ uncapped"
    return f"{round(mb / 1024)} GB"


def _disk_text(d: dict) -> str:
    mount = f"`{d['m']}`"
    free_gb, total_gb, free_pct = d.get("freeGB"), d.get("totalGB"), d.get("free")
    if free_gb is not None and total_gb is not None:
        capacity = f"{_fmt(free_gb)}/{_fmt(total_gb)} GB free"
        return f"{mount} {capacity} ({_fmt(free_pct)}%)" if free_pct is not None else f"{mount} {capacity}"
    if free_gb is not None:
        capacity = f"{_fmt(free_gb)} GB free (capacity unknown)"
        return f"{mount} {capacity} ({_fmt(free_pct)}%)" if free_pct is not None else f"{mount} {capacity}"
    if free_pct is not None:
        return f"{mount} {_fmt(free_pct)}%"
    return f"{mount} {d.get('flag') or 'unknown'}"


def _lowest_disk(m):
    have = [d for d in m["disks"] if d.get("free") is not None]
    if have:
        return _disk_text(min(have, key=lambda x: x["free"]))
    flagged = next((d for d in m["disks"] if d.get("flag")), None)
    return _disk_text(flagged) if flagged else "—"


def _backup_assess(m):
    """The Backup verdict, from the policy. Never "chain broken" — see db_ops.lib.backup_policy:
    a stale LOG backup proves an RPO gap and nothing about LSN continuity, and a DBA who reads
    "broken" takes a fresh FULL that may have destroyed a working restore path."""
    bk = m["backup"]
    if bk["cov"] == "No metrics":
        return "⚪ Unverified"
    status = str(bk.get("status") or "").upper()
    if status == "CRITICAL":
        return f"🔴 Policy violated · {bk.get('compliant', 0)}/{bk.get('eligible', 0)} DB"
    if status == "WARNING":
        return f"🟠 Backups aging · {bk.get('compliant', 0)}/{bk.get('eligible', 0)} DB"
    if status == "OK":
        return f"🟢 Compliant · {bk.get('eligible', 0)} DB"
    return "⚪ Unverified"


def render_md(scope, models, triage, date_iso) -> str:
    # models is already ordered by server_order (build_models); keep that order in every section.
    sorted_m = list(models)
    crit = sum(1 for m in models if m["status"] == "crit")
    warn = sum(1 for m in models if m["status"] == "warn")
    idle = sum(1 for m in models if m["status"] == "idle")
    online_dbs = sum(m["online"] or 0 for m in models)

    L = ["# 🗄️ Database Inventory & Health Report\n",
         f"> **Snapshot:** {date_iso}",
         "> **Source:** `architecture/database-inventory.json` (full inventory + merged health)",
         "> **Scope note:** Lab/test VMs and credential fields excluded.\n",
         "## 📊 At a Glance\n",
         "| Servers | SQL Server / Oracle / PostgreSQL | OS only | Databases online | 🔴 Critical | 🟠 Warning | ⚪ No data |",
         "|:-------:|:-------------------------------:|:-------:|:----------------:|:-----------:|:----------:|:---------:|",
         f"| **{scope['servers']}** | {scope['sqlserver']} / {scope['oracle']} / {scope.get('postgresql', 0)} "
         f"| {scope.get('os_only', 0)} | **{online_dbs}** | **{crit}** | **{warn}** | {idle} |\n",
         "---\n",
         "## 1. 🔺 Priority Attention\n",
         "*Auto-derived from thresholds. Each item names the fix.*\n"]

    sev_label = {"crit": "### 🔴 Critical", "warn": "### 🟠 Warning", "info": "### 🔵 Notes (lower priority)"}
    last_sev = None
    n = 0
    for card in triage:
        if card["sev"] != last_sev:
            L.append("")
            L.append(sev_label.get(card["sev"], "### Notes"))
            last_sev = card["sev"]
        n += 1
        L.append(f"\n**{n}. {card['title']}**")
        L.append(card["body"])
        L.append(f"→ **Action:** {card['action']}")
    if not triage:
        L.append("- No threshold findings in the current inventory.")

    L += ["\n---\n", "## 2. 🖥️ Fleet Health Matrix\n",
          "*One row per target, sorted worst-first.*\n",
          "| Status | Server | Platform | DBs online | Backup | HA / DR | PLE | Sessions | Lowest disk | Cfg ⚠ |",
          "|:------:|--------|----------|:----------:|--------|---------|----:|---------:|-------------|:-----:|"]
    for m in sorted_m:
        dbs = "—" if m["total"] is None else f"{m['online']}/{m['total']}"
        ha_short = m["ha"].split(" · ")[0] if "FCI" in m["ha"] else (m["ha"].split(" · ")[-1])
        L.append(f"| {_STATUS_EMOJI[m['status']]} | **{m['role']}** `{m['ip']}` | {m['platform']}"
                 f"{(' · ' + str(m['ramGB']) + ' GB') if m['ramGB'] else ''} | {dbs} | {m['backup']['cov']} "
                 f"| {ha_short} | {_fmt(m['ple'])} | {_fmt(m['sessions'])} | {_lowest_disk(m)} "
                 f"| {m['cfg'].get('warns') or 0} |")

    L += ["\n---\n", "## 3. 💾 Backup & Recovery Posture\n",
          "*Last successful backup evidence within the inventory window.*\n",
          "| Server | Coverage | Last FULL | Last DIFF | Last LOG | Assessment |",
          "|--------|----------|-----------|-----------|----------|------------|"]
    for m in sorted_m:
        if m.get("dbType") != "sqlserver":
            continue
        bk = m["backup"]
        L.append(f"| **{m['role']}** `{m['ip']}` | {bk['cov']} | {bk['full'] or '—'} | {bk['diff'] or '—'} "
                 f"| {bk['log'] or '—'} | {_backup_assess(m)} |")

    L += ["\n---\n", "## 4. ⚙️ SQL Configuration Baseline\n",
          "*✓ = aligned to baseline · ✗ = deviates · ! = security review. (Oracle & uncollected hosts omitted.)*\n",
          "| Server | vCPU | MAXDOP | Cost thr. | Max mem | Backup compr. | Remote DAC | Opt. adhoc | Blocked thr. | xp_cmdshell |",
          "|--------|:----:|:------:|:---------:|:-------:|:-------------:|:----------:|:----------:|:------------:|:-----------:|"]

    def yn(v, good):
        return "—" if v is None else ("✓" if v == good else "✗")

    for m in sorted_m:
        c = m["cfg"]
        if m.get("oracle") or c.get("govMissing"):
            continue
        cost = c.get("cost")
        cost_txt = f"✗ {cost}" if (cost is not None and cost < 30) else _fmt(cost)
        blocked = "—" if c.get("blockedThr") is None else ("✓" if c["blockedThr"] > 0 else "✗")
        xp = "—" if c.get("xpcmd") is None else ("! on" if c["xpcmd"] == 1 else "off")
        L.append(f"| **{m['role']}** `{m['ip']}` | {_fmt(c.get('cpu'))} | {_fmt(c.get('maxdop'))} | {cost_txt} "
                 f"| {_max_mem(c.get('maxmemMB'))} | {yn(c.get('backupCompr'), 1)} | {yn(c.get('remoteDac'), 1)} "
                 f"| {yn(c.get('optAdhoc'), 1)} | {blocked} | {xp} |")

    L += ["\n---\n", "## 5. 📋 Server Detail\n"]
    for m in sorted_m:
        c = m["cfg"]
        L.append("<details>")
        L.append(f"<summary>{_STATUS_EMOJI[m['status']]} <b>{m['role']}</b> — {m['ip']} · {m['endpoint']}</summary>\n")
        L.append("| | |")
        L.append("|---|---|")
        L.append(f"| **Company** | {m['company']} |")
        L.append(f"| **Platform / OS** | {m['platform']} · {m['os']} |")
        if m.get("osOnly"):
            # No database on this host: PLE, sessions, backup and HA do not exist for it.
            L += _os_detail_rows(m)
        else:
            if m["ramGB"]:
                L.append(f"| **Host RAM** | {m['ramGB']} GB |")
            L.append(f"| **HA / DR** | {m['ha']} |")
            L.append(f"| **Backup** | {m['backup']['cov']}{(' — ' + m['backup']['note']) if m['backup']['note'] else ''} |")
            if m["ple"] is not None or m["sessions"] is not None:
                L.append(f"| **PLE / Sessions** | {_fmt(m['ple'])} / {_fmt(m['sessions'])} |")
        if not c.get("govMissing") and not m.get("oracle"):
            L.append(f"| **CPU / MAXDOP / Cost** | {_fmt(c.get('cpu'))} / {_fmt(c.get('maxdop'))} / {_fmt(c.get('cost'))} |")
            L.append(f"| **Max / committed mem** | {_max_mem(c.get('maxmemMB'))} / {_max_mem(c.get('committedMB'))} |")
            L.append(f"| **TempDB** | {c.get('tempdb', '—')} |")
        if m["disks"]:
            disk_txt = " · ".join(_disk_text(d) for d in m["disks"])
            L.append(f"| **Disks** | {disk_txt} |")
        for line in _db_table_md(m):
            L.append(line)
        if m["dbs"]:
            L.append(f"| **Databases ({len(m['dbs'])})** | {', '.join(str(x) for x in m['dbs'])} |")
        if m.get("findings"):
            L.append(f"| **Findings** | {'<br>'.join(str(f) for f in m['findings'])} |")
        for row in m.get("detail", []):
            k, v = row[0], row[1]
            src = row[2] if len(row) > 2 else ""
            L.append(f"| **{k}** | {v}{' _(static inventory — not refreshed by metrics)_' if src == 'static' else ''} |")
        if m.get("reportUrl"):
            L.append(f"| **Metric history** | [one chart per metric item]({m['reportUrl']}) |")
        L.append(f"| **Inventory** | {m['inv']} |")
        L.append("\n</details>\n")

    L.append("*Database Inventory Report · auto-generated by db_ops reports · "
             "source `architecture/database-inventory.json`*")
    return "\n".join(L)


def _process_text(process: dict) -> str:
    parts = [str(process.get("process") or "")]
    if process.get("cpu_percent") is not None:
        parts.append(f"CPU {process['cpu_percent']}%")
    memory_mb = process.get("memory_mb")
    if memory_mb is not None:
        parts.append(f"RAM {round(memory_mb / 1024, 1)} GB" if memory_mb >= 1024 else f"RAM {memory_mb} MB")
    return " · ".join(parts)


def _age_text(hours) -> str:
    if hours is None:
        return "—"
    if hours < 48:
        return f"{round(hours)}h"
    return f"{round(hours / 24)}d"


def _db_table_md(model: dict) -> list:
    """The per-database table for the markdown report: same columns as the HTML one."""
    rows = model.get("databases") or []
    if not rows:
        return []
    out = ["| **Databases** | recovery · size · log · protection |", "| --- | --- |"]
    for d in rows:
        size = f"{d['dataGB']} GB" if d.get("dataGB") is not None else "—"
        log = f"{d['logGB']} GB" if d.get("logGB") is not None else "—"
        used = f" ({d['logUsedPct']}% used)" if d.get("logUsedPct") is not None else ""
        checkdb = d.get("checkdb") or "—"
        out.append(
            f"| `{d['name']}` | {d.get('state') or '—'} · {d.get('recovery') or '—'} · data {size} · "
            f"log {log}{used} · compat {d.get('compat') or '—'} · {d.get('pageVerify') or '—'} · "
            f"CHECKDB {checkdb} · FULL {_age_text(d.get('fullAgeHours'))} / "
            f"DIFF {_age_text(d.get('diffAgeHours'))} / LOG {_age_text(d.get('logAgeHours'))} |"
        )
    return out


def _os_detail_rows(model: dict) -> list:
    """Server-detail rows for a host with no database: what it actually has."""
    osh = model.get("os_health") or {}
    rows = []
    if osh.get("hostname"):
        rows.append(f"| **Hostname** | {osh['hostname']} |")
    if osh.get("uptime"):
        rows.append(f"| **Uptime** | {osh['uptime']} |")
    if osh.get("cpuPct") is not None:
        cores = f"{osh['logicalCpus']} vCPU · " if osh.get("logicalCpus") else ""
        rows.append(f"| **CPU** | {cores}{osh['cpuPct']}% |")
    if osh.get("memPct") is not None:
        total = f"{osh['memTotalGB']} GB · " if osh.get("memTotalGB") else ""
        used = f"{osh['memUsedGB']} GB used · " if osh.get("memUsedGB") is not None else ""
        rows.append(f"| **Memory** | {total}{used}{osh['memPct']}% |")
    if osh.get("services"):
        rows.append("| **Services** | "
                    + "<br>".join(f"{item['name']}: {item['state']}" for item in osh["services"]) + " |")
    if osh.get("topCpu"):
        rows.append(f"| **Top process (CPU)** | {_process_text(osh['topCpu'][0])} |")
    if osh.get("topMemory"):
        rows.append(f"| **Top process (memory)** | {_process_text(osh['topMemory'][0])} |")
    if osh.get("events"):
        rows.append("| **Event log** | "
                    + "<br>".join(f"{item['log']}: {item['count']} error events" for item in osh["events"]) + " |")
    if osh.get("pendingReboot"):
        rows.append(f"| **Pending reboot** | {osh['pendingReboot']} |")
    return rows


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build_inventory_report(*, inventory: str | Path = DEFAULT_INVENTORY,
                           output_dir: str | Path = ".", date: str | None = None,
                           sqlite_path: str | Path | None = None, days: int = 7) -> dict:
    """Render the fleet report. With ``sqlite_path``, also render one metric-history page per
    server (charts over the same window) and link every server row to its page."""
    data = json.loads(Path(inventory).read_bytes().decode("utf-8-sig"))
    stamp = date or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # Displayed datetime is parsed from this file's own stamp (the YYYYMMDD_HHMMSS prefix
    # of its filename), so a snapshot served via webhost ?date= always shows the moment
    # that file belongs to — not the current wall-clock time of whoever is viewing it.
    yyyymmdd = stamp[:8]
    date_iso = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
    hhmmss = stamp[9:15]  # time part after the 'YYYYMMDD_' prefix, when present
    if len(hhmmss) == 6 and hhmmss.isdigit():
        date_iso = f"{date_iso} {hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}"

    scope, models = build_models(data, exclude_ip_prefixes=inventory_exclude_ip_prefixes())
    triage = build_triage(models)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{stamp}_database-inventory-report.html"
    md_path = out_dir / f"{stamp}_database-inventory-report.md"

    server_pages: dict[str, str] = {}
    if sqlite_path:
        # Imported here so the fleet report still renders from the JSON alone (no SQLite).
        from db_ops.reports.server_report import build_server_pages

        # The charts page has a stable name (see server_report), so it links back to the stable
        # inventory link the web host serves, not to this run's stamped file.
        server_pages = build_server_pages(
            sqlite_path=sqlite_path, models=models, output_dir=out_dir, stamp=stamp,
            snapshot_date=date_iso, days=int(days), inventory_href="database-inventory.html",
        )
        for model in models:
            model["reportUrl"] = server_pages.get(str(model.get("server_id") or ""), "")
            # Same rule as the server page: only advertise the index report when its file exists,
            # so a server whose index metric has not run yet shows no link instead of a 404.
            from db_ops.reports.server_report import index_usage_file_name
            index_file = index_usage_file_name(str(model.get("server_id") or ""))
            model["indexUrl"] = index_file if (Path(output_dir) / index_file).exists() else ""

    linked_servers = build_fleet_linked_servers(sqlite_path, days=int(days)) if sqlite_path else []
    html_path.write_text(render_html(scope, models, triage, date_iso, linked_servers),
                         encoding="utf-8")
    md_path.write_text(render_md(scope, models, triage, date_iso), encoding="utf-8")
    print(f"Wrote {html_path}")
    print(f"Wrote {md_path}")
    if server_pages:
        print(f"Wrote the shared metric-history page + {len(server_pages)} server series file(s)")
    return {"html": str(html_path), "md": str(md_path),
            "servers": scope["servers"], "triage_items": len(triage),
            "server_pages": len(server_pages)}
