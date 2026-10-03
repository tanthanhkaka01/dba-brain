"""Server metric history: one page for the fleet, answering "is this server healthy?".

The fleet inventory report answers "which server is in trouble". This answers the next question
— "what is wrong with this one, and is it getting worse" — from ``metric_results``.

A wall of charts does not answer that. A DBA opening this page has to know, in seconds: is it
healthy, what is broken now, what needs doing, and is it improving. So the page leads with a
verdict (health summary), then the current state of each health area with its threshold, then
the problems in severity order with an action, then a timeline of when things started and
cleared — and only then the charts, with the ones that matter open and the rest folded away.

Two rules the analysis follows, because breaking either produces a confident lie:

* **Status comes from the collector**, which computed it against the thresholds in its own SQL.
  The report never re-decides that a WARNING is really fine.
* Except where the collector deliberately does not judge: PERFORMANCE_IO_LATENCY and
  PAGE_LIFE_EXPECTANCY return 'OK' on every branch (they are logging-only). Their area is
  classified here instead, and the threshold shown says so, so nobody reads a report threshold
  as an alerting one.

**One page, not one page per server.** Stamping a page per server per run cost 6.5 MB every
run (18 servers, the ERP host alone 706 KB): at one inventory run every two hours that is
~2.3 GB a month of near-duplicate HTML. So the page (``server-metrics.html``) and the series
files (``server-metrics_<slug>.json``) have stable names and are overwritten each run, and the
page fetches only the series of the server being looked at.

Charts are hand-drawn SVG — the worker has no internet, so no chart library can be fetched.
"""

from __future__ import annotations
from db_ops.lib.coerce import as_float
import datetime
import json
from db_ops.lib.html_json import json_for_html
import math
import re
from pathlib import Path
from db_ops.lib import data_sources
from db_ops.lib import report_archive
from db_ops.lib import backup_policy, capacity_forecast
from db_ops.lib import engine_sections, health_model, interval_rates
from db_ops.db.metric_store import MetricStore
from db_ops.db.metric_store import _parse_as_of as metric_store_as_of
from db_ops.reports import inventory_health
from db_ops.reports import workload as workload_block
from db_ops.reports import workload_attribution
from db_ops.lib.paths import DEFAULT_DATA_DIR
from db_ops.lib import page_banner
from db_ops.lib.timezone import format_offset, offset_minutes
from db_ops.lib.time_window import window_of
from db_ops.reports.server_series import (  # noqa: F401 - re-exported: every name kept its address
    CRITICAL_STATUSES,
    DATABASE_SIZE_CODES,
    DIAGNOSTIC_CODES,
    LATE_CADENCE_MULTIPLE,
    LATE_FLOOR_SECONDS,
    MAX_DATABASE_SIZE_ITEMS,
    MAX_ITEMS_PER_METRIC,
    MAX_POINTS,
    METRIC_DEFINITIONS,
    METRIC_LABELS,
    MIN_POINTS,
    PRIMARY_CODES,
    QUERY_STORE_CODE,
    SEVERITY_RANK,
    WARNING_STATUSES,
    _SPARSE_ITEM_CODES,
    _cadence,
    _catalog_entries,
    _database_size_rows,
    _downsample,
    _drop_collect_only,
    _epoch,
    _int_or_none,
    _int_or_zero,
    _is_low_cadence,
    _is_percent_unit,
    _message_kv,
    _metric_item_limit,
    _nice_ceiling,
    _series_capacity,
    _series_scale,
    _worst_items,
    build_freshness,
    load_server_series,
    metric_catalog,
    metric_intervals,
    metric_label,
    severity_of,
    sparse_item_metric_codes,
)
from db_ops.reports.server_health import (  # noqa: F401 - re-exported: every name kept its address
    ACTIONS,
    AREAS,
    AREA_CODES,
    STALE_AFTER_SECONDS,
    _age_hint,
    _backup_area_state,
    _selector_matches,
    _value_text,
    area_members,
    build_areas,
    build_health,
    build_problems,
    build_timeline,
    metric_action,
    series_downgraded,
    series_severity,
)
from db_ops.reports.server_sections import (  # noqa: F401 - re-exported: every name kept its address
    CAPACITY_CODE,
    DATABASE_SECTION_CODES,
    DATABASE_USERS_CODE,
    JOB_INVENTORY_CODE,
    LINKED_SERVER_CODE,
    OS_VOLUME_CODE,
    QUERY_STORE_FINDINGS_HOURS,
    QUERY_STORE_ISSUES_CODE,
    SERVER_PRINCIPALS_CODE,
    VOLUME_SECTION_CODES,
    _HIGH_DATABASE_ROLES,
    _HIGH_SERVER_ROLES,
    _JOB_FIELDS,
    _LINKED_FAILURE_FIX,
    _LINKED_VERDICTS,
    _NOT_A_VOLUME,
    _QS_NUMBERS,
    _bracket_list,
    _job_fields,
    _pipe_fields,
    _query_store_finding,
    _volume_from_engine_row,
    _volume_from_os_row,
    _volume_key,
    build_access,
    build_capacity,
    build_databases,
    build_jobs,
    build_linked_servers,
    build_query_store,
    build_query_store_findings,
    build_volumes,
)
from db_ops.reports.server_oracle import (  # noqa: F401 - re-exported: every name kept its address
    DATAFILE_CODE,
    ORACLE_BUFFER_CACHE_CODE,
    ORACLE_EXTENT_LIMIT_CODE,
    ORACLE_INDEX_UNUSABLE_CODE,
    ORACLE_INSTANCE_CODES,
    ORACLE_INVALID_OBJECTS_CODE,
    ORACLE_LIBRARY_CACHE_CODE,
    ORACLE_OBJECT_CODES,
    ORACLE_POOL_CODE,
    ORACLE_PROCESS_LIMIT_CODE,
    ORACLE_REDO_CODE,
    ORACLE_ROLLBACK_CODE,
    ORACLE_SECTION_CODES,
    ORACLE_TOP_DISK_SQL_CODE,
    ORACLE_TOP_GETS_SQL_CODE,
    ORACLE_TOP_SEGMENT_CODE,
    ORACLE_TOP_SQL_CODES,
    TABLESPACE_CODE,
    TABLESPACE_SECTION_CODES,
    TEMP_SPACE_CODE,
    _ORACLE_REDO_ITEMS,
    _datafiles_by_tablespace,
    _item_of,
    _leading_float,
    _rows_of,
    _tail_field,
    _temp_usage_by_tablespace,
    build_oracle_instance,
    build_oracle_objects,
    build_oracle_redo,
    build_oracle_top_sql,
    build_tablespaces,
)


TEMPLATE_HTML = Path(__file__).resolve().parent / "templates" / "server_report.html"


PAGE_NAME = "server-metrics.html"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-") or "server"


def series_file_name(server_id: str) -> str:
    return f"server-metrics_{_slug(server_id)}.json"


def index_usage_file_name(server_id: str) -> str:
    """The index report published for this server, named from the same slug this page uses."""
    return f"index-usage_{_slug(server_id)}.html"


def page_href(server_id: str) -> str:
    return f"{PAGE_NAME}?server={_slug(server_id)}"


def build_payload(series: list[dict], omitted: list[dict], *, now: int | None = None,
                  backup: dict | None = None, freshness: dict | None = None,
                  linked_rows: list[dict] | None = None,
                  capacity_rows: list[dict] | None = None, server_id: str = "",
                  access_rows: list[dict] | None = None,
                  query_store_rows: list[dict] | None = None,
                  query_store_issue_rows: list[dict] | None = None,
                  job_rows: list[dict] | None = None,
                  tablespace_rows: list[dict] | None = None,
                  volume_rows: list[dict] | None = None,
                  oracle_rows: list[dict] | None = None,
                  workload_rows: list[dict] | None = None,
                  database_code_map: dict | None = None) -> dict:
    """Everything the page renders for one server.

    ``backup`` and ``freshness`` are computed from the raw store rows rather than from ``series``
    (see :func:`load_server_context`): both need rows the chart pipeline deliberately drops — the
    per-database backup evidence is not a time series, and a metric that produced no rows at all
    has no series to be late.
    """
    now = now if now is not None else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    problems = build_problems(series, now=now)
    # Worst first inside every group: with 23 database files a "disk latency" section is 23
    # charts, and the one at 94 ms must not be the twentieth one down the page.
    def worst_first(entry):
        if entry["code"] in DATABASE_SIZE_CODES:
            kind = 0 if entry["code"] == "DATABASE_DATA_SIZE" else 1
            return (-SEVERITY_RANK[series_severity(entry)], 1, entry["item"].casefold(), kind)
        return (-SEVERITY_RANK[series_severity(entry)], 0, entry["label"], entry["item"])

    charts = sorted((entry for entry in series if not entry["static"]), key=worst_first)
    # Linked servers get their own table, so they are kept out of the generic status chips: a
    # chip saying "PRODSRV: REACHABLE" beside a table row that says REACHABLE / KEEP / 14
    # procedures is the same fact twice, and the chip is the useless half.
    cards = sorted((entry for entry in series
                    if entry["static"] and entry["code"] != LINKED_SERVER_CODE), key=worst_first)
    workload = workload_block.build_workload(workload_rows or [])
    return {
        "health": build_health(series, problems, now=now, freshness=freshness),
        "areas": build_areas(series, backup=backup, freshness=freshness),
        "problems": problems,
        "timeline": build_timeline(series),
        "cards": cards,
        "linkedServers": build_linked_servers(linked_rows or []),
        "capacity": build_capacity(capacity_rows or [], server_id=server_id),
        "volumes": build_volumes(volume_rows or []),
        "access": build_access(access_rows or []),
        "queryStore": build_query_store(query_store_rows or []),
        "queryStoreFindings": build_query_store_findings(query_store_issue_rows or []),
        "databases": build_databases(database_code_map or {}, backup),
        "tablespaces": build_tablespaces(tablespace_rows or []),
        # Four Oracle sections. Each renders nothing when its metrics produced no rows, which is
        # how a SQL Server or PostgreSQL page stays exactly as it was without the builder being
        # told which engine it is looking at.
        "oracleInstance": build_oracle_instance(oracle_rows or []),
        "oracleObjects": build_oracle_objects(oracle_rows or []),
        "oracleRedo": build_oracle_redo(oracle_rows or []),
        "oracleTopSql": build_oracle_top_sql(oracle_rows or []),
        # How much work the instance did, from cumulative counters differenced across two
        # collections. Built from the raw store rows, not from `series`: an interval needs
        # two samples of the same counter, and the chart pipeline reduces each metric to
        # one point per item per collection with the message — the baseline marker — gone.
        "workload": workload,
        # Which database wrote the log and which statements did the work, over the same windows.
        # From the same rows: the fetch below carries both code lists, and each builder reads its
        # own codes and ignores the rest.
        "attribution": workload_attribution.build_attribution(workload_rows or [],
                                                              workload=workload),
        "jobs": build_jobs(job_rows or []),
        "series": charts,
        "omitted": omitted,
        "backup": backup or {},
        "freshness": freshness or {},
    }


def render_page(*, servers: list[dict], snapshot_date: str, stamp: str,
                days: int, inventory_href: str, report_dir=None) -> str:
    """The shared page. It carries only the server index; the series are fetched per server."""
    html = TEMPLATE_HTML.read_text(encoding="utf-8")
    replacements = {
        "__SNAPSHOT_DATE__": snapshot_date,
        "__WINDOW_DAYS__": str(int(days)),
        "__STAMP__": stamp,
        # The page renders every time itself, in JS, so it needs the clock as a value rather than
        # a rendered string. Baked in at build time and not read from the browser: a report is a
        # file that gets shared, and it must say the same hour to everyone who opens it.
        "__UTC_OFFSET_MINUTES__": str(offset_minutes()),
        "__UTC_OFFSET_LABEL__": format_offset(offset_minutes()),
        "__BANNER_CSS__": page_banner.CSS,
        # snapshot_date is the day the numbers are for, which is what a page rebuilt
        # for a past day must say - never today.
        # The head used to name three pages and never the index reports - so an estate could
        # publish one per server every night with nothing linking to any of them. `report_dir` is
        # optional: without one there is nothing to filter against, and the three stable siblings
        # are offered exactly as before.
        "__PAGE_BANNER__": page_banner.render(
            title="Server metrics", snapshot_at=snapshot_date,
            here="server-metrics.html",
            links=None if report_dir is None else page_banner.siblings_present(
                lambda name: name == "server-metrics.html" or (Path(report_dir) / name).exists(),
                index_usage=page_banner.pick_index_usage(
                    path.name for path in Path(report_dir).glob("index-usage_*.htm*")))),
        "__INVENTORY_HREF__": inventory_href,
        "__SERVERS__": json_for_html(servers, separators=(",", ":")),
        # The picker's sections in page order, so the template does not hold its own copy of it.
        "__ENGINE_SECTIONS__": json_for_html([list(section) for section in engine_sections.SECTIONS],
                                             separators=(",", ":")),
    }
    for key, value in replacements.items():
        html = html.replace(key, value)
    return html


def build_server_pages(*, sqlite_path: str | Path, models: list[dict], output_dir: str | Path,
                       stamp: str, snapshot_date: str, days: int, inventory_href: str,
                       as_of: str | None = None,
                       archive_only: bool = False) -> dict[str, str]:
    """Write the shared page plus one series file per server, all with stable names (they are
    overwritten each run, so the report directory does not grow with every build).

    Returns {server_id: href} so the inventory report can link each server row to its charts.
    Keyed by server_id, not by IP: the three PostgreSQL instances share one host, and keying
    on the IP would give all three the same link."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Two fleet-wide queries instead of two per server: both answer questions about rows the
    # chart pipeline drops (per-database backup evidence is not a series; a metric that returned
    # nothing at all has no series to be late), and neither is worth a round trip per server.
    store = MetricStore(sqlite_path)
    freshness_rows = store.fetch_metric_freshness(days=int(days), as_of=as_of)
    backup_rows: dict[str, list[dict]] = {}
    # Every engine's backup-result code, not SQL Server's alone: the policy reads
    # POSTGRES_BACKUP_LAST_RESULT the same way, and with only BACKUP_LAST_RESULT fetched a
    # PostgreSQL server's Backup area said "not collected" over a collected backup (2026-10-02).
    for row in store.fetch_health_metrics(codes=[*backup_policy.BACKUP_LAST_RESULT_CODES, "BACKUP_AGE"],
                                          days=int(days), as_of=as_of):
        backup_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Linked servers, same shape and same reason: they are dropped by the chart pipeline, so the
    # section reads the store rows directly. Only the newest collection per server counts — a
    # linked server that was removed last week must not linger in the table.
    linked_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=[LINKED_SERVER_CODE], days=int(days), as_of=as_of):
        linked_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Capacity wants the whole history, not the latest snapshot: a slope needs samples.
    capacity_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=[CAPACITY_CODE], days=int(days), as_of=as_of):
        capacity_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Logins and database users: inventories, so only the newest collection counts. A login
    # dropped last week must not linger in the table any more than a removed linked server does.
    access_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=[SERVER_PRINCIPALS_CODE, DATABASE_USERS_CODE],
                                          days=int(days), as_of=as_of):
        access_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Query Store coverage: a once-a-day inventory of every database, so the newest collection is
    # the whole truth and a database dropped last week must not linger in the table.
    query_store_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=[QUERY_STORE_CODE], days=int(days), as_of=as_of):
        query_store_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Query Store findings: one row per regressed or heavy query plan over the last day, each
    # marked still-happening or cleared against the newest collection.
    # A day of them, not the page's window and not the newest run alone: see
    # QUERY_STORE_FINDINGS_HOURS. A 15-minute metric over seven days would load 672 runs per server.
    query_store_issue_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=[QUERY_STORE_ISSUES_CODE],
                                          days=max(1, QUERY_STORE_FINDINGS_HOURS // 24), as_of=as_of):
        query_store_issue_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Scheduled jobs: a nightly inventory, so the newest collection is the whole truth — a job
    # deleted last week must not linger in the table any more than a removed linked server does.
    job_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=[JOB_INVENTORY_CODE], days=int(days), as_of=as_of):
        job_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Oracle tablespaces and their datafiles: an inventory of what exists, so the newest
    # collection is the whole truth — a datafile added this morning must appear, and one dropped
    # last week must not linger.
    tablespace_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=TABLESPACE_SECTION_CODES, days=int(days),
                                          as_of=as_of):
        tablespace_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Host storage: what volumes exist, how large they are, how full. An inventory of the
    # current state, so the newest collection is the whole truth — a volume unmounted this
    # morning must stop being listed, and a new one must appear.
    volume_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=VOLUME_SECTION_CODES, days=int(days), as_of=as_of):
        volume_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # The Oracle-only sections: pools and limits, segments and objects, redo and archiving, top
    # SQL. All four are inventories of the current state, so the newest collection is the whole
    # truth — an object made valid this morning must stop being listed.
    oracle_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=ORACLE_SECTION_CODES, days=int(days), as_of=as_of):
        oracle_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # The workload counters, and the only section that wants *every* sample rather than the
    # newest: a rate is the difference between two of them. Capped at two days regardless of the
    # report window — the widest reading the page shows is 24 hours, and a seven-day fetch of a
    # 15-minute metric would load five days of rows nothing renders.
    # The attribution codes ride on the same fetch. They are this page's alone — the fleet overlay
    # loads WORKLOAD_CODES for every server and renders no per-statement or per-database rows.
    workload_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=workload_block.WORKLOAD_CODES
                                          + workload_attribution.ATTRIBUTION_CODES,
                                          days=min(int(days), workload_block.WORKLOAD_DAYS),
                                          as_of=as_of):
        workload_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # The per-database section. Indexed by the same helper the fleet overlay uses, which reduces
    # each metric to its newest collection per server — a database table must be a snapshot, not
    # a week of samples stacked up (the mistake the fragmentation list made on 2026-08-13).
    database_index = inventory_health.index_by_server(
        store.fetch_health_metrics(codes=DATABASE_SECTION_CODES, days=int(days), as_of=as_of))
    # "Now" is the moment the report describes. Rebuilding 1 August against today's clock would
    # mark every one of that day's collections stale by two days and paint the whole fleet
    # UNKNOWN — a rebuilt past report has to be judged by its own date.
    now = int((metric_store_as_of(as_of) or datetime.datetime.now(datetime.timezone.utc)).timestamp())

    index: list[dict] = []
    links: dict[str, str] = {}
    for model in models:
        server_id = str(model.get("server_id") or "")
        if not server_id:
            continue
        series, omitted = load_server_series(sqlite_path, server_id=server_id, days=days,
                                             as_of=as_of)
        freshness = build_freshness(freshness_rows.get(server_id, []), now=now, days=int(days))
        backup = backup_policy.evaluate_backup_policy(
            health_model.latest_snapshot(backup_rows.get(server_id, [])), server_id=server_id,
            policy=data_sources.load_backup_policy())
        payload = build_payload(series, omitted, now=now, backup=backup, freshness=freshness,
                                linked_rows=health_model.latest_snapshot(
                                    linked_rows.get(server_id, [])),
                                capacity_rows=capacity_rows.get(server_id, []),
                                server_id=server_id,
                                access_rows=health_model.latest_snapshot(
                                    access_rows.get(server_id, [])),
                                query_store_rows=health_model.latest_snapshot(
                                    query_store_rows.get(server_id, [])),
                                # Not latest_snapshot: the section keeps a day (Q10).
                                query_store_issue_rows=query_store_issue_rows.get(server_id, []),
                                job_rows=health_model.latest_snapshot(
                                    job_rows.get(server_id, [])),
                                tablespace_rows=health_model.latest_snapshot(
                                    tablespace_rows.get(server_id, [])),
                                volume_rows=health_model.latest_snapshot(
                                    volume_rows.get(server_id, [])),
                                oracle_rows=health_model.latest_snapshot(
                                    oracle_rows.get(server_id, [])),
                                # Not latest_snapshot: this is the one section that needs the
                                # history, because a rate is two samples subtracted.
                                workload_rows=workload_rows.get(server_id, []),
                                database_code_map=database_index.get(server_id, [None, {}])[1])
        file_name = series_file_name(server_id)
        # archive_only is a backfill: it produces the dated copy of a past day and must leave the
        # live file alone. Writing the stable name would publish 1 August's data as "now".
        _write(out_dir, file_name,
               json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
               stamp=stamp, archive_only=archive_only)
        index.append({
            "slug": _slug(server_id),
            # server_id, not the role name: role names are not unique. The FCI pair
            # 192.0.2.115 / .113 both report server_name SALESCLUSTER, so the picker had two
            # identical buttons and the page title could not say which node you were reading.
            "name": server_id,
            "role": str(model.get("role") or ""),
            "ip": str(model.get("ip") or ""),
            "platform": str(model.get("platform") or ""),
            # The picker's section. One flat row of every target - SQL Server instances, Oracle and
            # PostgreSQL databases, bare hosts - made "the Oracle servers" a search; the operator
            # asked for one row per engine (2026-10-02), grouped by the rule every page shares.
            "engine": engine_sections.section_of(model.get("dbType"),
                                                 os_only=bool(model.get("osOnly"))),
            # The picker dot is the server's own verdict, not the fleet report's: this page is
            # what the DBA is looking at, so the two must not disagree.
            "status": payload["health"]["status"],
            "score": payload["health"]["score"],
            "file": file_name,
            # Only advertise the page when it is actually on disk. The index report is published
            # by the same workflow, but a server whose index metric has not run yet has no page,
            # and a link to a 404 is worse than no link at all.
            "index_usage_file": (
                index_usage_file_name(server_id)
                if (out_dir / index_usage_file_name(server_id)).exists() else ""
            ),
        })
        links[server_id] = page_href(server_id)

    _write(out_dir, PAGE_NAME,
           render_page(
               servers=index,
               snapshot_date=snapshot_date, stamp=stamp, days=days, inventory_href=inventory_href,
               report_dir=out_dir,
           ),
           stamp=stamp, archive_only=archive_only)
    return links


def _write(out_dir: Path, name: str, text: str, *, stamp: str, archive_only: bool) -> None:
    """Publish one file under its live name and its day-stamped one.

    The dated copy is what makes `?date=` reach these pages at all: unlike the fleet inventory
    they keep a stable name and are overwritten every run, so without it there is no history to
    serve (see :mod:`db_ops.lib.report_archive` for why this is daily and not per run).
    """
    dated = out_dir / report_archive.archive_name(stamp, name)
    dated.write_text(text, encoding="utf-8")
    if not archive_only:
        (out_dir / name).write_text(text, encoding="utf-8")
