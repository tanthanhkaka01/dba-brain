"""Merging a health overlay into the canonical inventory, and rendering the summary from it.

Both the master-side ``control`` app and the worker-side ``reports`` app produce this summary, and
until now each carried its own copy of the logic — 265 identical lines, because the no-cross-app-import
rule left copying as the only way for the second one to reuse the first. The rule's answer was always
to move the shared half here instead, and this module is that move.

The two copies had already drifted, which is what a duplicated rule always does: the reports side
learned to read the newer ``backup_evidence`` block, to take disk figures from ``merged_drives``
(which fuses the SQL metric, the OS metric and the static WinRM inventory) and to surface curated
``findings``, while the control side still read only the older shapes. The reports version is a strict
superset — every new path falls back to what the control version read — so it is the one kept here,
and the master-side output gains those sections rather than losing anything.

What stays in the apps is what genuinely differs: ``control`` SSHes to the worker and SFTPs the
overlay back; ``reports`` runs store-local and never touches the network.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from db_ops.lib.coerce import as_float
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module


DISK_WARN_PCT = 15.0

DISK_CRIT_PCT = 10.0

#: The ``DATABASE_STATUS`` values that mean "this database can be used right now". Every engine
#: words it differently: SQL Server reports ``sys.databases.state_desc`` and PostgreSQL follows it,
#: while Oracle reports ``v$database.open_mode``, whose healthy values are ``READ WRITE`` and
#: ``READ ONLY``. Comparing against the literal ``ONLINE`` made every Oracle instance in the fleet
#: read ``0/1 databases online`` for ever, and be graded WARNING on it — a standing false alarm on
#: a database that was open and serving, which is how an operator learns to stop reading a column.
ONLINE_DATABASE_STATES = frozenset({
    "ONLINE",                          # SQL Server sys.databases.state_desc; PostgreSQL
    "READ WRITE", "READ ONLY",         # Oracle v$database.open_mode
    "READ ONLY WITH APPLY",            # Oracle Active Data Guard standby, applying redo
})


def is_database_online(state) -> bool:
    """Whether a collected ``DATABASE_STATUS`` value means the database is usable.

    A pure reading of a value already in ``metric_results`` — nothing here collects anything.
    It lives in ``lib`` so the fleet page's ``online/total`` count and the per-server database
    table share one definition: two pages disagreeing about whether a database is up is worse
    than either of them being wrong on its own.
    """
    return str(state or "").strip().upper() in ONLINE_DATABASE_STATES


def _norm_mount(mount):
    """Canonical mount key: 'E:\\' and 'E:' collapse to 'E:' so a drive present in both the
    metrics overlay (disk_health, keyed 'E:\\') and the OS inventory (os_resources, keyed
    'E:') is one entry, not a duplicate."""
    return str(mount or "").rstrip("\\/")

def merged_drives(server):
    """Disks keyed by the normalized mount, merging three sources:
    ``disk_health.drives`` (SQL metric — fresh free%/status, needs a DB login),
    ``os_health.disks`` (OS_DISK_USAGE — the only disk source a host with no database has),
    and ``os_resources.disks`` (the static WinRM inventory: volume label + filesystem).
    The SQL overlay wins where present, then the OS metric, then the static inventory. Missing
    fields are still backfilled from the lower-priority source: a SQL 2008 ``xp_fixeddrives``
    row knows current free GB but not total GB, while the OS inventory knows the volume total.
    Deduplicated so 'E:\\' and 'E:' never both appear."""
    os_disks = (server.get("os_resources") or {}).get("disks") or []
    os_by_mount = {}
    for disk in os_disks:
        key = _norm_mount(disk.get("mount_point"))
        if key:
            os_by_mount[key] = disk
    drives = {}
    for mount, info in ((server.get("disk_health") or {}).get("drives") or {}).items():
        key = _norm_mount(mount)
        od = os_by_mount.get(key) or {}
        total = as_float(info.get("total_gb"))
        free = as_float(info.get("free_gb"))
        if total is None:
            total = as_float(od.get("total_gb"))
        if free is None:
            free = as_float(od.get("free_gb"))
        pct = as_float(info.get("free_percent"))
        if pct is None and free is not None and total:
            pct = round(100.0 * free / total, 2)
        drives[key] = {
            **info,
            "total_gb": total,
            "free_gb": free,
            "free_percent": pct,
            "logical_volume_name": od.get("logical_volume_name"),
            "file_system_type": od.get("file_system_type"),
        }
    for mount, info in ((server.get("os_health") or {}).get("disks") or {}).items():
        key = _norm_mount(mount)
        if key in drives:
            current = drives[key]
            if current.get("total_gb") is None:
                current["total_gb"] = as_float(info.get("total_gb"))
            if current.get("free_gb") is None:
                current["free_gb"] = as_float(info.get("free_gb"))
            if current.get("free_percent") is None:
                total, free = current.get("total_gb"), current.get("free_gb")
                current["free_percent"] = (round(100.0 * free / total, 2)
                                           if free is not None and total
                                           else as_float(info.get("free_percent")))
            current["logical_volume_name"] = (current.get("logical_volume_name")
                                                or info.get("logical_volume_name"))
            current["file_system_type"] = (current.get("file_system_type")
                                             or info.get("file_system_type"))
            continue
        od = os_by_mount.get(key) or {}
        drives[key] = {
            **info,
            "logical_volume_name": info.get("logical_volume_name") or od.get("logical_volume_name"),
            "file_system_type": info.get("file_system_type") or od.get("file_system_type"),
        }
    for disk in os_disks:
        key = _norm_mount(disk.get("mount_point"))
        if not key or key in drives:
            continue
        total, free = as_float(disk.get("total_gb")), as_float(disk.get("free_gb"))
        pct = round(100.0 * free / total, 2) if (free is not None and total) else None
        status = ("UNKNOWN" if pct is None else "CRITICAL" if pct < DISK_CRIT_PCT
                  else "WARNING" if pct < DISK_WARN_PCT else "OK")
        drives[key] = {"total_gb": total, "free_gb": free, "free_percent": pct, "status": status,
                       "logical_volume_name": disk.get("logical_volume_name"),
                       "file_system_type": disk.get("file_system_type")}
    return drives

def merged_sql_resources(server):
    """``sqlserver_resources`` with the fresh ``sql_governance`` health block overlaid on top,
    so live-changing values (memory, MAXDOP, cost, config flags, tempdb size, host RAM) come
    from metrics while structural facts with no metric (CPU/scheduler counts, tempdb file
    count, database sizes) fall back to the stored resources."""
    sr = {k: dict(v) if isinstance(v, dict) else v for k, v in (server.get("sqlserver_resources") or {}).items()}
    gov = server.get("sql_governance") or {}
    if not gov:
        return sr
    for block in ("sql_cpu", "memory", "important_config", "tempdb"):
        merged = dict(sr.get(block) or {})
        merged.update({k: v for k, v in (gov.get(block) or {}).items() if v is not None})
        if merged:
            sr[block] = merged
    return sr

DEFAULT_INVENTORY = TOOL_ROOT / "data" / "database-inventory.json"

HEALTH_BLOCKS = ["instance_health", "metric_severity", "metric_problems", "metric_freshness",
                 "backup_jobs", "backup_policy",
                 "database_health", "disk_health", "backup_by_database", "backup_evidence",
                 "sql_governance", "sql_agent_job_health", "performance_health", "index_health",
                 "security_health", "config_warnings", "os_health", "inventory_status",
                 # When the blocks above were collected. A server the overlay does not reach
                 # keeps its previous blocks *and* its previous stamp, so the report can say
                 # "last known 20/06" instead of presenting stale values as current.
                 # health_oldest_as_of is the other half of that answer: a composite row is only
                 # as current as its *oldest* input, and printing the newest one is how a daily
                 # 21 GB size sample passed for the 70 GB the log had actually reached.
                 "health_as_of", "health_oldest_as_of"]

#: Which server ip prefixes the inventory pages leave out. **Empty here on purpose.** This was
#: ``("192.168.18.",)`` until 2026-08-21 — one estate's management subnet, written into the
#: rendering library, silently dropping those servers from every inventory page for anyone who
#: ever ran this code. A library that hides a machine has to be *told* to, by the operator whose
#: machine it is: the value now comes from ``reports_config.json`` and arrives as an argument.
EXCLUDE_IP_PREFIXES: tuple[str, ...] = ()

DBTYPE_LABEL = {"sqlserver": "SQL Server", "oracle": "Oracle", "mysql": "MySQL", "postgresql": "PostgreSQL"}

def _merge_overlay(overlay: dict, inventory: dict) -> int:
    """Copy each health block from the overlay onto the canonical server it belongs to.

    Matched on ``server_id`` — the identity of a machine, which is unique across every db_ops
    file. The ip fallback that used to sit behind it looked harmless and was not: on 2026-08-05 a
    server was onboarded as ``ACME-192-0-2-86`` while the estate already knew it as
    ``GLOBEX-192-0-2-86``, and the fallback matched them by ip so the page rendered correctly
    while the metric store filed 448 rows under an id nothing else used. A quiet success is the
    worst outcome for a mismatch nobody has noticed yet.

    **The merge key is ``server_id`` and nothing else.** An ip that matches while the id does not
    is reported rather than silently used, but it is reported as *context*: one ip carrying several
    ids is normal here, so the message names the neighbours and leaves the judgement to the reader
    instead of asserting a uniqueness rule this function does not enforce.

    **That check runs from the overlay side, not the canonical side.** Asking "this canonical
    server has no metrics — does something else share its ip?" fires on every instance that is
    simply switched off: five of them are, and each sits on a VM alongside an enabled one, so
    every run printed five "one machine must have one server_id" warnings that were not id
    mismatches at all. Five false alarms per run is how a real one stops being read.

    Asking it the other way — "these metrics were collected under an id the canonical inventory
    has never heard of" — has no such false positive, and it catches the case that used to pass
    in silence: a host record collecting CPU/RAM/disk that no canonical row claims, so the page
    renders without it and nothing says a word.
    """
    servers = overlay.get("servers", [])
    by_sid = {s.get("server_id"): s for s in servers if s.get("server_id")}
    canonical_ids = {s.get("server_id") for s in inventory.get("servers", [])}
    # One ip can legitimately carry many server_ids here (eight lab instances on one VM), so
    # this maps to the full set, never to whichever entry a dict comprehension saw last.
    canonical_ips: dict[str, set[str]] = {}
    for entry in inventory.get("servers", []):
        if entry.get("ip"):
            canonical_ips.setdefault(entry["ip"], set()).add(entry.get("server_id"))

    updated = 0
    for server in inventory.get("servers", []):
        match = by_sid.get(server.get("server_id"))
        if not match:
            continue
        for block in HEALTH_BLOCKS:
            if block in match:
                server[block] = match[block]
        updated += 1

    for collected in servers:
        sid = collected.get("server_id")
        if not sid or sid in canonical_ids:
            continue
        twins = sorted(canonical_ips.get(collected.get("ip"), set()) - {None})
        # server_id is the key, and the only key: one ip may legitimately carry several (eight lab
        # instances share 192.0.2.249, and a VM whose database runs in a container is still one
        # machine with two ids if someone chose to model it that way). The twins are named as
        # context - "you may have meant one of these" - not as an accusation. This used to read
        # "One machine must have one server_id", which contradicted the comment six lines above it
        # and sent an operator renaming ids to satisfy a rule the merge does not have.
        detail = (
            f"the same ip is held there by {', '.join(twins)}, so this may be a second id for one "
            "machine (legitimate) or a typo in one of the two files (not)"
            if twins else
            "and its ip is not in the canonical inventory either"
        )
        # "holds metrics", not "is collecting": an instance switched off days ago still has rows
        # inside the report window, and saying it is collecting would send someone hunting for a
        # live collector that stopped on purpose.
        # stderr, not stdout: a warning is for the person watching, while stdout carries the
        # caller's answer. Printing it inline is how this module contaminated a JSON response.
        print(
            f"WARNING: the store holds metrics under '{sid}' ({collected.get('ip')}), which the "
            f"canonical inventory does not contain - {detail}. Its health blocks were NOT merged "
            "and it does not appear in the report. Add it to data/database-inventory.json, or fix "
            "the id in data/db_instances.json so the two agree.",
            file=sys.stderr, flush=True,
        )
    return updated

def _write_inventory(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8") + b"\r\n")  # CRLF, no BOM

def _g(obj, *keys, default=""):
    for key in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
        if obj is None:
            return default
    return obj

def _primary_db(server):
    dbs = server.get("databases") or []
    return dbs[0] if dbs else {}

def _backup_jobs_text(server):
    jobs = _g(server, "backup", "jobs", default=[]) or []
    return "; ".join(f"{j.get('job_name')}: {j.get('last_status')} at {j.get('last_run_datetime')}"
                     for j in jobs if j.get("job_name"))

def _fci_text(server):
    fci = server.get("fci_cluster") or {}
    if not fci:
        return ""
    parts = [f"listener {fci.get('listener_name')}" if fci.get("listener_name") else "",
             f"active {fci.get('current_node_name')} ({fci.get('current_node_ip')})"
             if fci.get("current_node_name") else ""]
    for n in fci.get("inactive_nodes", []) or []:
        parts.append(f"inactive {n.get('node_name')} ({n.get('ip')})")
    if fci.get("validation_status"):
        parts.append(f"validation {fci.get('validation_status')}")
    return ", ".join(p for p in parts if p)

def _platform(server):
    db0 = _primary_db(server)
    label = DBTYPE_LABEL.get(db0.get("db_type"), db0.get("db_type", ""))
    # SQL Server records "edition"; Oracle records "version" (e.g. "Oracle 8i").
    edition = db0.get("edition") or db0.get("version") or ""
    return " / ".join(p for p in (label, db0.get("service_name", ""), edition) if p)

def _remote_user_text(server):
    ru = server.get("remote_user")
    if isinstance(ru, dict):
        return "configured" if ru.get("configured") or ru.get("login") else "not configured"
    if isinstance(ru, str):
        return "configured" if ru.lower().startswith("configured") else (ru or "not configured")
    return "not configured"

def _backup_evidence(server):
    lbt = server.get("backup_evidence") or _g(server, "backup", "latest_by_type", default={})
    parts = []
    for btype in ("FULL", "DIFF", "LOG"):
        entry = lbt.get(btype) if isinstance(lbt, dict) else None
        if isinstance(entry, dict) and entry.get("latest_finish"):
            parts.append(f"{btype}: {entry.get('latest_status', '')} at {entry.get('latest_finish')} "
                         f"({entry.get('database_count', '?')} DB)")
    return "<br>".join(parts) or "No backup metrics in inventory window"

def _findings(servers):
    out = []
    for s in servers:
        sid = s.get("server_id")
        for drive, info in (merged_drives(s) or {}).items():
            pct = info.get("free_percent")
            if isinstance(pct, (int, float)) and pct < DISK_WARN_PCT:
                out.append(f"Low disk: `{sid}` drive `{drive}` at {pct}% free "
                           f"({info.get('free_gb')} / {info.get('total_gb')} GB).")
        bad = [d["database_name"] for d in s.get("database_health", [])
               if d.get("state") and not is_database_online(d["state"])]
        if bad:
            out.append(f"Databases not ONLINE on `{sid}`: {', '.join(bad)}.")
        nobk = [d["database_name"] for d in s.get("backup_by_database", []) if d.get("status") == "NO_FULL_BACKUP"]
        if nobk:
            out.append(f"No full backup on `{sid}`: {', '.join(nobk)}.")
        bevd = s.get("backup_evidence") or _g(s, "backup", "latest_by_type", default={})
        full = bevd.get("FULL") if isinstance(bevd, dict) else {}
        age = full.get("latest_age_hours") if isinstance(full, dict) else None
        if isinstance(age, (int, float)) and age > 48:
            out.append(f"Stale FULL backup on `{sid}`: {age} hours.")
        warns = s.get("config_warnings") or []
        if warns:
            out.append(f"Config warnings on `{sid}`: {len(warns)} ({'; '.join(w.split(';')[0] for w in warns)}).")
        # Surface curated findings recorded in the inventory itself (e.g. "SQL resource not
        # connected" for hosts without live metrics). Low-disk findings are skipped — the
        # merged_drives threshold check above already emits those (and from a live source).
        for f in (s.get("findings") or []):
            if not str(f).lower().startswith("low disk"):
                out.append(f"`{sid}`: {f}")
    return out

def _baseline_lines(server):
    """Render manually-documented baselines from the inventory (``os_resources``,
    ``oracle_resources``, ``inventory_status``) so hosts that can't be live-collected —
    e.g. legacy Oracle 8i — still report the info that exists in the inventory."""
    lines = []
    os_res = server.get("os_resources") or {}
    if os_res:
        parts = [os_res.get("os_caption")]
        if os_res.get("physical_memory_gb"):
            parts.append(f"{os_res.get('physical_memory_gb')}GB RAM")
        parts.append(os_res.get("boot_memory_option"))
        text = ", ".join(p for p in parts if p)
        if text:
            lines.append(f"- OS/RAM baseline: {text}")
    ora_res = server.get("oracle_resources") or {}
    if ora_res:
        params = ora_res.get("parameters") or {}
        bits = []
        if ora_res.get("sga_approx_mb"):
            bits.append(f"SGA about {ora_res.get('sga_approx_mb')}MB")
        for key in ("processes", "sort_area_size", "sort_area_retained_size"):
            if key in params:
                bits.append(f"{key}={params[key]}")
        if bits:
            lines.append(f"- Oracle memory baseline: {', '.join(bits)}")
    status = server.get("inventory_status")
    if isinstance(status, dict) and status:
        text = "; ".join(f"{k}: {v}" for k, v in status.items())
        lines.append(f"- Inventory status: {text}")
    return lines

def _render_markdown(data, date_iso, exclude_ip_prefixes=EXCLUDE_IP_PREFIXES):
    servers = [s for s in data.get("servers", [])
               if not any(str(s.get("ip", "")).startswith(p) for p in exclude_ip_prefixes)]
    sql = sum(1 for s in servers if _primary_db(s).get("db_type") == "sqlserver")
    ora = sum(1 for s in servers if _primary_db(s).get("db_type") == "oracle")
    with_health = sum(1 for s in servers if s.get("database_health"))
    lines = [
        f"# Database Inventory Summary - {date_iso}\n",
        "Generated from `architecture/database-inventory.json` (full inventory + merged health). "
        "Lab/test VMs and credential fields are excluded.\n",
        "## Scope\n",
        f"- Included servers: {len(servers)}",
        f"- SQL Server targets: {sql}",
        f"- Oracle targets: {ora}",
        f"- Servers with collected health: {with_health}\n",
        "## Important Findings\n",
    ]
    findings = _findings(servers)
    lines += [f"- {f}" for f in findings] if findings else ["- No threshold findings in the current inventory."]
    lines += ["", "## Backup And HA Summary\n",
              "| Server ID | Backup | HA | Backup evidence |", "| --- | --- | --- | --- |"]
    for s in servers:
        ha = s.get("ha") or {}
        ha_text = " / ".join(str(x) for x in (ha.get("mode"), ha.get("role")) if x) or "None documented"
        lines.append(f"| `{s.get('server_id')}` | {_g(s, 'backup', 'policy_summary') or 'No backup metrics'} "
                     f"| {ha_text} | {_backup_evidence(s)} |")
    lines += ["", "## Server Overview\n",
              "| Server ID | Company | DB platform | Server / OS | Remote user | Inventory status |",
              "| --- | --- | --- | --- | --- | --- |"]
    for s in servers:
        db0 = _primary_db(s)
        # OS falls back to the manual os_resources baseline (e.g. Oracle, where the OS lives
        # there, not in the db record) so the column isn't blank for non-collected hosts.
        os_text = db0.get("os") or (s.get("os_resources") or {}).get("os_caption")
        name = db0.get("server_name") or db0.get("endpoint_name")
        host = "<br>".join(p for p in (name, os_text) if p)
        status = s.get("inventory_status", "")
        if isinstance(status, dict):
            status = "<br>".join(f"{k}: {v}" for k, v in status.items())
        lines.append(f"| `{s.get('server_id')}` | `{s.get('company_code', '')}` | {_platform(s)} "
                     f"| {host} | {_remote_user_text(s)} | {status} |")
    lines += ["", "## SQL Server CPU And Memory Governance\n",
              "| Server ID | Host logical CPU | SQL visible CPU | Schedulers | NUMA sched | MAXDOP | Cost threshold | "
              "Host RAM GB | SQL min MB | SQL max MB | SQL committed MB | TempDB MB/files | Key config |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |"]
    for s in servers:
        if _primary_db(s).get("db_type") != "sqlserver":
            continue
        sr = merged_sql_resources(s)
        if not sr:
            lines.append(f"| `{s.get('server_id')}` | | | | | | | | | | | | SQL resource not collected |")
            continue
        cpu, mem, cfg, tdb = sr.get("sql_cpu", {}), sr.get("memory", {}), sr.get("important_config", {}), sr.get("tempdb", {})
        host_cpu = (sr.get("host_cpu") or {}).get("logical_processor_count", "")
        key_cfg = ", ".join(f"{k}={v}" for k, v in cfg.items())
        tempdb = f"{tdb.get('total_mb', '')} / {tdb.get('file_count', '')} files" if tdb else ""
        lines.append(
            f"| `{s.get('server_id')}` | {host_cpu} | {cpu.get('sql_visible_cpu_count', '')} | {cpu.get('scheduler_count', '')} "
            f"| {cpu.get('numa_online_scheduler_count_total', '')} | {cpu.get('max_degree_of_parallelism', '')} "
            f"| {cpu.get('cost_threshold_for_parallelism', '')} | {mem.get('host_physical_memory_gb', '')} "
            f"| {mem.get('min_server_memory_mb', '')} | {mem.get('max_server_memory_mb', '')} "
            f"| {mem.get('sql_committed_mb', '')} | {tempdb} | {key_cfg} |")
    lines += ["", "## Health Snapshot\n",
              "_Collected by db_ops metrics (logging level). PLE = page life expectancy._\n",
              "| Server ID | DBs (online/total) | CHECKDB unknown | Disk warnings | No-backup DBs | "
              "PLE | Blocking | Deadlocks 24h | Sessions | Config warnings |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for s in servers:
        dh = s.get("database_health") or []
        if not (dh or s.get("config_warnings") or s.get("performance_health")):
            continue
        online = sum(1 for d in dh if is_database_online(d.get("state")))
        checkdb_unknown = sum(1 for d in dh if d.get("last_good_checkdb") in ("", "unknown"))
        disk_warn = sum(1 for info in (merged_drives(s) or {}).values()
                        if isinstance(info.get("free_percent"), (int, float)) and info["free_percent"] < DISK_WARN_PCT)
        no_backup = sum(1 for d in s.get("backup_by_database", []) if d.get("status") == "NO_FULL_BACKUP")
        ph = s.get("performance_health") or {}
        lines.append(
            f"| `{s.get('server_id')}` | {online}/{len(dh)} | {checkdb_unknown} | {disk_warn} | {no_backup} "
            f"| {ph.get('page_life_expectancy', '')} | {ph.get('blocking_count', '')} "
            f"| {ph.get('deadlock_count_24h', '')} | {ph.get('active_session_count', '')} "
            f"| {len(s.get('config_warnings') or [])} |")
    lines += ["", "## Server Detail\n"]
    for s in servers:
        db0 = _primary_db(s)
        lines.append(f"### {s.get('server_id')}\n")
        lines.append(f"- Company: `{s.get('company_code', '')}`")
        lines.append(f"- Platform: {_platform(s)}")
        if s.get("role"):
            lines.append(f"- Role: {s.get('role')}")
        if db0.get("endpoint_name"):
            lines.append(f"- Endpoint: `{db0.get('endpoint_name')}`")
        if db0.get("server_name"):
            lines.append(f"- Server name: `{db0.get('server_name')}`")
        dep = s.get("deployment") or {}
        if dep:
            dep_bits = [dep.get("platform"), dep.get("host_os"), dep.get("container_or_node_name")]
            lines.append(f"- Deployment: {', '.join(str(b) for b in dep_bits if b)}")
            if dep.get("sql_data_root"):
                lines.append(f"- SQL data root: `{dep.get('sql_data_root')}`")
        # SQL Server uses "database_names"; Oracle uses "schemas".
        db_names = db0.get("database_names") or db0.get("schemas")
        if db_names:
            label = "Databases" if db0.get("database_names") else "Databases/schemas"
            lines.append(f"- {label}: {', '.join(str(n) for n in db_names)}")
        backup_line = f"- Backup: {_g(s, 'backup', 'policy_summary') or 'No backup metrics'}; {_backup_evidence(s)}"
        jobs_txt = _backup_jobs_text(s)
        if jobs_txt:
            backup_line += f"; jobs: {jobs_txt}"
        lines.append(backup_line)
        ha = s.get("ha") or {}
        lines.append(f"- HA: {' / '.join(str(x) for x in (ha.get('mode'), ha.get('role')) if x) or 'None documented'}")
        fci_txt = _fci_text(s)
        if fci_txt:
            lines.append(f"- FCI: {fci_txt}")
        # Manually-documented baselines for hosts where live collection is impossible
        # (e.g. legacy Oracle 8i): surfaced from the inventory so the report is complete.
        for line in _baseline_lines(s):
            lines.append(line)
        params = _g(s, "oracle_resources", "parameters", default={}) or {}
        if params:
            lines.append("- Oracle parameters: " + ", ".join(f"`{k}`={v}" for k, v in params.items()))
        sr = merged_sql_resources(s)
        if sr:
            cpu, mem, tdb = sr.get("sql_cpu") or {}, sr.get("memory") or {}, sr.get("tempdb") or {}
            host_cpu = (sr.get("host_cpu") or {}).get("logical_processor_count")
            lines.append(f"- SQL CPU: host logical `{host_cpu}`, visible `{cpu.get('sql_visible_cpu_count')}`, "
                         f"schedulers `{cpu.get('scheduler_count')}`, NUMA online `{cpu.get('numa_online_scheduler_count_total')}`, "
                         f"MAXDOP `{cpu.get('max_degree_of_parallelism')}`, cost `{cpu.get('cost_threshold_for_parallelism')}`")
            lines.append(f"- SQL memory: host `{mem.get('host_physical_memory_gb')}` GB, min `{mem.get('min_server_memory_mb')}` MB, "
                         f"max `{mem.get('max_server_memory_mb')}` MB, committed `{mem.get('sql_committed_mb')}` MB")
            if tdb:
                lines.append(f"- TempDB: `{tdb.get('total_mb')}` MB across `{tdb.get('file_count')}` files "
                             f"(`{tdb.get('data_file_count')}` data, `{tdb.get('log_file_count')}` log)")
        sizes = _g(s, "sqlserver_resources", "database_sizes", default=[]) or []
        if sizes:
            lines.append("- Database sizes: " + ", ".join(
                f"{x.get('database_name')}={x.get('size_mb')}MB" for x in sizes))
        dh = s.get("database_health") or []
        if dh:
            states = {}
            for d in dh:
                states[d.get("state", "?")] = states.get(d.get("state", "?"), 0) + 1
            lines.append(f"- Database health: {len(dh)} DB(s) — " + ", ".join(f"{k}={v}" for k, v in states.items()))
        for drive, info in (merged_drives(s) or {}).items():
            label = info.get("logical_volume_name")
            fs = info.get("file_system_type")
            name = f"{drive} {label}" if label else f"{drive}"
            fs_txt = f" ({fs})" if fs else ""
            lines.append(f"- Disk `{name}`: {info.get('free_gb')} / {info.get('total_gb')} GB free "
                         f"({info.get('free_percent')}%){fs_txt} — {info.get('status')}")
        ph = s.get("performance_health") or {}
        if ph:
            lines.append(f"- Performance: PLE={ph.get('page_life_expectancy', '')}, "
                         f"blocking={ph.get('blocking_count', '')}, deadlocks_24h={ph.get('deadlock_count_24h', '')}, "
                         f"sessions={ph.get('active_session_count', '')}, cpu={ph.get('cpu_status', '')}")
        # Counts only. The metric behind this also stores one row per index (~29k on a large
        # database); a page that listed them would be unreadable and would take minutes to build.
        ix = s.get("index_health") or {}
        tot = ix.get("totals") or {}
        if tot.get("indexes_total"):
            lines.append(
                f"- Indexes: total={tot.get('indexes_total', 0)}, used={tot.get('used', 0)}, "
                f"unused={tot.get('unused', 0)}, cold={tot.get('cold', 0)}, "
                f"disabled={tot.get('disabled', 0)}, droppable={tot.get('droppable', 0)}"
                + (f", fragmented={tot.get('fragmented')}" if tot.get("fragmented") else "")
                + (f" — **{tot.get('disabled_clustered')} DISABLED CLUSTERED "
                   f"(those tables are inaccessible until rebuilt)**"
                   if tot.get("disabled_clustered") else ""))
            # Only the databases actually carrying dead weight; a list of zeroes is noise.
            for entry in (ix.get("databases") or [])[:5]:
                if not (entry.get("droppable") or entry.get("cold") or entry.get("disabled")):
                    continue
                lines.append(
                    f"  - `{entry.get('database')}`: total={entry.get('indexes_total', 0)}, "
                    f"cold={entry.get('cold', 0)}, disabled={entry.get('disabled', 0)}, "
                    f"droppable={entry.get('droppable', 0)}")
        for w in (s.get("config_warnings") or []):
            lines.append(f"- Config warning: {w}")
        for n in (s.get("notes") or []):
            lines.append(f"- Note: {n}")
        lines.append("")
    return "\n".join(lines)

def build_inventory_summary(*, inventory: str | Path = DEFAULT_INVENTORY,
                            output_dir: str | Path = ".", date: str | None = None,
                            exclude_ip_prefixes=EXCLUDE_IP_PREFIXES) -> dict:
    data = json.loads(Path(inventory).read_bytes().decode("utf-8-sig"))
    stamp = date or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    yyyymmdd = stamp[:8]
    date_iso = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{stamp}_database-inventory-summary.md"
    out_path.write_text(_render_markdown(data, date_iso, exclude_ip_prefixes), encoding="utf-8")
    # It returns where it wrote and prints nothing. It used to `print(f"Wrote {out_path}")`, which
    # put a human line on the same stream the CLI answer goes to: `inventory-summary`'s stdout
    # parsed as neither prose nor JSON. Where the file went is in `data.file`, and the sentence is
    # the response's `message` — which is what that field is for.
    return {"file": str(out_path)}
