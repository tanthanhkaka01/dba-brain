"""A report of its own for index health, one per server_id.

Index data does not fit anywhere else. ``MAINTENANCE_INDEX_USAGE`` emits one row per index —
around 29,000 for a single large database — so it cannot go in the hourly alert report (it would
bury every real alert), and it cannot go in the per-server chart series (nothing there is a time
series). The inventory page carries the counts, which answer *how much* dead weight exists but not
*which* indexes.

So this is the third thing: one report per server, listing the indexes an operator would actually
act on, with the counts as context.

Two rules shape what is listed:

* **Never suggest dropping something that enforces a rule.** A primary key, a unique constraint and
  the clustered index can show zero seeks for years and still be doing their job. They are counted
  and, when disabled, alerted on — never listed as drop candidates.
* **A disabled clustered index is an incident, not maintenance.** The whole table is inaccessible
  until it is rebuilt, so it is reported first and separately from everything else.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from db_ops.lib import report_archive
from db_ops.db.metric_store import MetricStore
from db_ops.db import DbOpsStore
from db_ops.reports.server_report import page_href

INDEX_REPORT_CODE = "rp_index_usage_by_server"
INDEX_REPORT_TYPE = "index_usage"
INDEX_METRIC_CODE = "MAINTENANCE_INDEX_USAGE"
FRAGMENTATION_METRIC_CODE = "MAINTENANCE_INDEX_FRAGMENTATION"

#: Oracle's index rows. A separate code, not an Oracle variant of the usage metric, because Oracle
#: records no per-index usage unless each index is put into ``ALTER INDEX ... MONITORING USAGE`` —
#: a change to the database, not to the monitor. Rows filed under the usage code would render as
#: several hundred zero-seek indexes that look unused and are not, which is the reading that gets
#: an index dropped and a report query broken. The page they produce says inventory, not usage.
ORACLE_INDEX_METRIC_CODE = "INDEX_INVENTORY"

#: How many detail rows of each kind a single server's report lists. The counts stay exact; only
#: the listing is bounded, because a report nobody can scroll through is a report nobody reads.
DEFAULT_DETAIL_LIMIT = 25

#: Where the webhost publishes reports. The host serves ``<runtime>/reports`` under ``/report_dba/``
#: (see db_ops.webhost.cli), so anything written there is reachable at this prefix. Config rather
#: than a literal: the port, mount and host are all deployment facts, not code facts.
# Resolution lives in db_ops.lib.report_archive: the SLA app links to its published page too,
# and two copies of "where are the reports" is how a link starts 404ing.
from db_ops.common.data_sources import report_base_url  # noqa: E402,F401


def server_dashboard_url(server_id: str) -> str:
    """Link to this server's metrics page, built with the same helper the page itself uses.

    Reusing ``page_href`` rather than formatting the query string here means the slug can never
    drift from the one the page was published under - a link that 404s is worse than no link.
    """
    return report_base_url() + page_href(server_id)


#: The fleet inventory page, under the stable name the web host serves (``--latest`` in
#: db_ops.webhost.cli), not this run's stamped file. Same href the server metrics page links
#: back to, so the three reports agree on where "the fleet" is.
INVENTORY_PAGE = "database-inventory.html"


def inventory_url() -> str:
    """Link up to the fleet inventory report."""
    return report_base_url() + INVENTORY_PAGE


def _kv(message: str) -> dict[str, str]:
    """Parse the ``k=v, k=v`` message body the metric writes.

    The message opens with a kind prefix — ``COLD: db=APP, schema=dbo, ...`` — so a naive split on
    "," glues that prefix onto the FIRST key (``COLD: db`` instead of ``db``). It happens to be
    harmless today only because the first field is ``db``, which the detail path never reads; the
    moment a field is reordered it would silently stop matching. Strip the prefix instead.
    """
    text = str(message or "")
    head, sep, rest = text.partition(":")
    if sep and "=" not in head:
        text = rest
    fields: dict[str, str] = {}
    for part in text.split(","):
        key, sep, value = part.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


#: Uptime a usage sample needs before its "unused"/"cold"/"droppable" counts mean anything —
#: one full business week, matching @trusted_min_hours in 068_sqlserver_index_usage.sql. Below it
#: the metric still reports detail (from 12 hours), and both it and this report say so on every
#: line that recommends a DROP.
TRUSTED_SAMPLE_DAYS = 7


def _int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _is_short_sample(uptime_days: Any) -> bool:
    """True when the usage counters do not yet cover a full business week.

    Unparseable uptime is **not** treated as short: the warning has to be right when it fires, and
    a report that cries wolf on every server teaches people to skip the one line that matters.
    """
    try:
        return float(str(uptime_days).strip()) < TRUSTED_SAMPLE_DAYS
    except (TypeError, ValueError):
        return False


def collect_index_rows(store_source, *, days: int = 3,
                       as_of: str | None = None) -> dict[str, dict[str, Any]]:
    """Latest index rows per server, split into the groups the report renders.

    Only the newest collection per server is used. The metric runs on a long interval and each run
    replaces the previous picture wholly, so mixing two runs would double-count every index.
    """
    rows = MetricStore(store_source).fetch_health_metrics(
        codes=[INDEX_METRIC_CODE, ORACLE_INDEX_METRIC_CODE, FRAGMENTATION_METRIC_CODE],
        days=int(days), as_of=as_of)

    newest: dict[str, str] = {}
    # Fragmentation keeps its own clock. It is a separate metric on a separate schedule, so its
    # newest run almost never carries the same timestamp as the index inventory's — dating it
    # from `newest` would filter every fragmentation row away instead of de-duplicating them.
    newest_fragmentation: dict[str, str] = {}
    for row in rows:
        server = str(row["server_id"] or "")
        stamp = str(row["collected_at"] or "")
        if not server:
            continue
        code = str(row["metric_code"])
        if code == FRAGMENTATION_METRIC_CODE:
            if stamp > newest_fragmentation.get(server, ""):
                newest_fragmentation[server] = stamp
            continue
        # Both index codes date a server's run: an Oracle server has no usage rows at all, and
        # taking the newest stamp only from those would leave its own rows filtered out by the
        # "newest run" test below — the page would be created and then be empty.
        if code not in (INDEX_METRIC_CODE, ORACLE_INDEX_METRIC_CODE):
            continue
        if stamp > newest.get(server, ""):
            newest[server] = stamp

    servers: dict[str, dict[str, Any]] = {}
    for row in rows:
        server = str(row["server_id"] or "")
        if not server:
            continue
        entry = servers.setdefault(server, {
            "server_id": server, "ip": str(row["ip"] or ""), "collected_at": newest.get(server, ""),
            "totals": {}, "databases": [], "disabled": [], "droppable": [], "fragmented": [],
            "indexes": [],
        })
        code = str(row["metric_code"])

        if code == ORACLE_INDEX_METRIC_CODE:
            if str(row["collected_at"] or "") != newest.get(server, ""):
                continue
            entry["engine"] = "oracle"
            fields = _kv(row["message"] or "")
            if str(row["metric_unit"] or "").lower() == "summary":
                entry["totals"] = {key: _int(value) for key, value in fields.items()}
                continue
            item = str(row["metric_item"] or "")
            unusable = _int(fields.get("is_disabled"))
            entry.setdefault("indexes", []).append({
                "item": item,
                # No usage state exists on this engine, and inventing one ("COLD") would be the
                # whole reason this code is separate from the usage metric.
                "kind": "UNUSABLE" if unusable else "",
                "type": fields.get("type_desc", "?"),
                "index_id": "",
                "is_unique": _int(fields.get("is_unique")),
                "is_primary": _int(fields.get("is_primary_key")),
                "is_uq_constr": _int(fields.get("is_unique_constraint")),
                "is_disabled": unusable,
                "has_filter": 0,
                "table": fields.get("table", ""),
                "size_mb": fields.get("size_mb", ""),
                "extents": _int(fields.get("extents")),
                "last_stats": fields.get("last_stats_update", "never"),
            })
            if unusable:
                entry["disabled"].append({"item": item, "type": fields.get("type_desc", "?"),
                                          # Oracle has no clustered index; an IOT is the nearest
                                          # thing and it cannot be UNUSABLE without the table
                                          # being gone, so nothing here is ever the "incident"
                                          # kind the SQL Server section reports first.
                                          "clustered": False})
            continue

        if code == FRAGMENTATION_METRIC_CODE:
            # Same rule as the inventory rows above, and for the same reason: this is a snapshot,
            # not a series. Without it every daily sample in the window stacks up, so one index
            # sitting at 96% for three days reads as three separate fragmented indexes — which is
            # how APPDB_Prod reported "Fragmented (26)" on a day it had 4.
            if str(row["collected_at"] or "") != newest_fragmentation.get(server, ""):
                continue
            message = str(row["message"] or "")
            if "action=REBUILD" in message or "action=REORGANIZE" in message:
                entry["fragmented"].append({"item": str(row["metric_item"] or ""),
                                            "pct": str(row["metric_value"] or ""),
                                            "message": message})
            continue

        # Index-usage rows: only from that server's newest run.
        if str(row["collected_at"] or "") != newest.get(server, ""):
            continue
        fields = _kv(row["message"] or "")
        unit = str(row["metric_unit"] or "").lower()
        if unit == "summary":
            if fields.get("db"):
                entry["databases"].append({k: _int(v) for k, v in fields.items() if k != "db"}
                                          | {"database": fields["db"]})
            else:
                entry["totals"] = {k: _int(v) for k, v in fields.items()}
                # A timestamp, not a count - keep the text rather than _int()'ing it to 0.
                entry["totals"]["counters_since"] = fields.get("counters_since", "")
                entry["counters_since"] = fields.get("counters_since", "")
                # Fractional days matter here: the metric reports detail from 12 hours of uptime,
                # and _int() would render 0.5 as "0 day(s)" - which reads as no sample at all.
                entry["totals"]["uptime_days"] = fields.get("uptime_days", "")
                entry["uptime_days"] = fields.get("uptime_days", "")
            continue

        item = str(row["metric_item"] or "")
        kind = str(row["message"] or "").split(":", 1)[0].strip()
        # MISSING rows are what the optimizer WISHED existed - they are not indexes on this
        # instance. Counting them here inflated "All indexes" to 1053 against a true total of
        # 949, which is exactly the 104 missing-index suggestions.
        if kind == "MISSING":
            continue
        # Every index is kept, not only the actionable ones: the report is an inventory, and a
        # reader asking "is THIS index used?" needs the ones with nothing wrong with them too.
        entry.setdefault("indexes", []).append({
            "item": item,
            "kind": kind if kind in ("USED", "UNUSED", "COLD") else "",
            "type": fields.get("type_desc", "?"),
            # Carried per row so :func:`_recommend` can word the action for the engine that
            # produced it: PostgreSQL has no REBUILD, and its constraint-backed indexes refuse
            # DROP INDEX outright.
            "engine": "postgresql" if unit == "idx_scan" else "",
            "index_id": fields.get("index_id", ""),
            "is_unique": _int(fields.get("is_unique")),
            "is_primary": _int(fields.get("is_primary_key")),
            "is_uq_constr": _int(fields.get("is_unique_constraint")),
            "is_disabled": _int(fields.get("is_disabled")),
            "has_filter": _int(fields.get("has_filter")),
            "seeks": _int(fields.get("user_seeks")),
            "scans": _int(fields.get("user_scans")),
            "lookups": _int(fields.get("user_lookups")),
            "writes": _int(fields.get("user_updates")),
            "last_read": fields.get("last_read", "never"),
            "last_stats": fields.get("last_stats_update", "never"),
        })
        # PostgreSQL writes the same metric code with the same field names, so everything above
        # is shared. What differs is the two engine-specific words below: it has no clustered
        # index (so nothing has to be excluded for *being* the table), and its `type_desc` is the
        # access method — `btree`, `gin`, `brin`. Keying the drop rule on the literal
        # "NONCLUSTERED" would have left every PostgreSQL page reporting "droppable: 22" in its
        # totals and listing none of them.
        postgres = unit == "idx_scan"
        if postgres:
            entry["engine"] = "postgresql"
        if _int(fields.get("is_disabled")):
            entry["disabled"].append({"item": item, "type": fields.get("type_desc", "?"),
                                      # On PostgreSQL this flag carries an INVALID index, which is
                                      # never the "table is inaccessible" incident a disabled
                                      # clustered index is — it is one index the planner ignores.
                                      "clustered": (not postgres
                                                    and fields.get("type_desc") == "CLUSTERED")})
        elif ((postgres or fields.get("type_desc") == "NONCLUSTERED")
                and not _int(fields.get("is_unique"))
                and not _int(fields.get("is_primary_key"))
                and not _int(fields.get("is_unique_constraint"))
                and _int(fields.get("user_seeks")) + _int(fields.get("user_scans"))
                + _int(fields.get("user_lookups")) == 0):
            entry["droppable"].append({
                "item": item,
                "writes": _int(fields.get("user_updates")),
                "last_read": fields.get("last_read", "never"),
                "kind": "COLD" if str(row["message"] or "").startswith("COLD") else "UNUSED",
            })
    for entry in servers.values():
        _fill_totals_from_databases(entry)
    return servers


#: Summary fields that are counts, and so can be added across databases. `counters_since` is a
#: timestamp and `database` a name; neither survives a sum, and both are handled separately.
_SUMMABLE_TOTALS = ("indexes_total", "used", "unused", "cold", "disabled", "disabled_clustered",
                    "droppable", "tables")


def _fill_totals_from_databases(entry: dict[str, Any]) -> None:
    """Build a server's totals by adding up its per-database summaries, where it has none of its own.

    SQL Server emits a cluster-wide summary because one connection reaches every database. The
    PostgreSQL variant deliberately does not: it is declared ``per_database``, so the collector
    runs it once per database, and a row calling itself the total would be written once per
    database under the same ``metric_item`` — on the PGLAB cluster, three rows each claiming to be
    the server total, of which this parser would keep whichever arrived last. That is how a server
    with 66 indexes came to report 1.
    """
    if entry.get("totals") or not entry.get("databases"):
        return
    totals = {key: sum(_int(db.get(key)) for db in entry["databases"]) for key in _SUMMABLE_TOTALS}
    totals["missing_suggestions"] = 0
    totals["databases_covered"] = len(entry["databases"])
    entry["totals"] = totals


def html_file_name(server_id: str) -> str:
    """Published file name for one server's index report."""
    from db_ops.reports.server_report import _slug  # noqa: PLC0415 - same package, avoids a cycle

    return f"index-usage_{_slug(server_id)}.html"


def _markdown_tables_to_html(text: str) -> str:
    """Turn the report's pipe tables into real HTML tables, leave everything else as text.

    Deliberately tiny and local rather than a markdown dependency: the only markdown this report
    emits is pipe tables and `###` headings, and a full parser would be a new dependency for two
    constructs.
    """
    html: list[str] = []
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        is_row = stripped.startswith("|") and stripped.endswith("|")
        # The |---|---| separator carries alignment, not data.
        is_rule = is_row and set(stripped.replace("|", "").replace(" ", "")) <= {"-", ":"}
        if is_row and not is_rule:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not in_table:
                html.append("<table><thead><tr>"
                            + "".join(f"<th>{_escape(c)}</th>" for c in cells)
                            + "</tr></thead><tbody>")
                in_table = True
            else:
                html.append("<tr>" + "".join(f"<td>{_escape(c)}</td>" for c in cells) + "</tr>")
            continue
        if is_rule:
            continue
        if in_table:
            html.append("</tbody></table>")
            in_table = False
        if stripped.startswith("## ") and not stripped.startswith("### "):
            # The one line that qualifies every number on the page, so it is rendered as a banner
            # rather than a paragraph. Buried under the tables, it was read past.
            html.append(f'<div class="banner">{_escape(stripped[3:])}</div>')
        elif stripped.startswith("### "):
            html.append(f"<h3>{_escape(stripped[4:])}</h3>")
        elif stripped.startswith("http://") or stripped.startswith("https://"):
            html.append(f'<p><a href="{_escape(stripped)}">{_escape(stripped)}</a></p>')
        elif stripped:
            html.append(f"<p>{_link_urls(_escape(stripped))}</p>")
    if in_table:
        html.append("</tbody></table>")
    return chr(10).join(html)


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("`", ""))


def _link_urls(text: str) -> str:
    """Make bare URLs clickable.

    A lambda rather than a backreference template: the replacement kept losing its escape on
    the way into this file and produced an empty href - a link to nowhere, which is worse than
    plain text because it looks like it works.
    """
    import re as _re

    return _re.sub(r"(https?://\S+)",
                   lambda m: '<a href="' + m.group(1) + '">' + m.group(1) + '</a>', text)

def peer_status(entry: dict[str, Any]) -> str:
    """The worst thing found on one server, as the picker colours it.

    Deliberately the same rule ``create_index_reports`` uses for the stored report level: a dot
    that disagrees with the report it links to is worse than no dot.
    """
    if any(d.get("clustered") for d in entry.get("disabled", [])):
        return "crit"
    if entry.get("disabled") or entry.get("droppable") or entry.get("fragmented"):
        return "warn"
    return "ok"


def build_peer_links(servers: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """The picker entries for every server that gets a report in this run.

    Built from the same dict the reports are rendered from, so the picker can only ever offer
    pages that are being written beside it — the rule the server metrics page follows too, for
    the same reason: a link to a 404 looks like it should work.
    """
    return [
        {"server_id": name,
         "file": html_file_name(name),
         "status": peer_status(servers[name])}
        for name in sorted(servers)
    ]


#: Picker colours, matching server-metrics.html so one estate does not get two colour languages.
_PEER_COLORS = {"crit": "#dc2626", "warn": "#c2700a", "ok": "#15803d"}


def _peer_nav_html(peers: list[dict[str, str]], current: str) -> str:
    """A row of links to every other server's index report — the picker, in static HTML.

    This page is one file per server, so it has no JavaScript to switch servers with. What it can
    do is carry the same list of links on every page, which is what makes the set navigable at all
    instead of reachable only from whoever remembers the file naming rule.
    """
    if len(peers) < 2:
        return ""
    chips: list[str] = []
    for peer in peers:
        dot = (f'<span class="dot" style="background:'
               f'{_PEER_COLORS.get(peer["status"], _PEER_COLORS["ok"])}"></span>')
        label = dot + _escape(peer["server_id"])
        chips.append(f'<span class="chip on">{label}</span>' if peer["server_id"] == current
                     else f'<a class="chip" href="{_escape(peer["file"])}">{label}</a>')
    # These pages are archived once a day and served back by `?date=`, so a reader who arrived at
    # a dated snapshot has to stay in it when they click to another server — otherwise the picker
    # silently drops them back into today, on a page that still looks like the one they chose.
    # Static HTML, so the rewrite happens in the browser from this page's own query string.
    keep_date = (
        "<script>(function(){var d=new URLSearchParams(location.search).get('date');"
        "if(!d)return;document.querySelectorAll('.picker a.chip,a[data-keep-date]')"
        ".forEach(function(a){var h=a.getAttribute('href');"
        "a.setAttribute('href',h+(h.indexOf('?')<0?'?':'&')+'date='+encodeURIComponent(d));});"
        "})();</script>")
    return ('<div class="picker"><span class="picker-label">Index reports</span>'
            + "".join(chips) + "</div>" + keep_date)


def write_index_report_html(entry: dict[str, Any], text: str, out_dir: Path,
                            peers: list[dict[str, str]] | None = None,
                            *, stamp: str | None = None,
                            archive_only: bool = False) -> Path:
    """Publish one server's report where the webhost already serves files.

    ``stamp`` also writes that day's dated copy, which is what ``?date=`` serves. ``archive_only``
    writes *only* the dated copy — a backfill rebuilding a past day must not publish it as the
    live report.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / html_file_name(entry["server_id"])
    page = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>Index Usage — {_escape(entry['server_id'])}</title>"
        "<style>body{font:14px/1.5 system-ui,sans-serif;margin:24px;max-width:1200px}"
        "table{border-collapse:collapse;margin:12px 0;width:100%}"
        "th,td{border:1px solid #ccc;padding:6px 8px;text-align:left;font-size:13px}"
        "th{background:#f4f4f4}tr:nth-child(even){background:#fafafa}"
        "h3{margin-top:28px}a{color:#0645ad}"
        ".picker{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 18px;"
        "padding-bottom:14px;border-bottom:1px solid #e5e9ef}"
        ".picker-label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;"
        "color:#64748b;font-weight:700;margin-right:4px}"
        ".chip{border:1px solid #e5e9ef;border-radius:999px;padding:4px 12px;font-size:12px;"
        "color:#64748b;text-decoration:none;background:#fff}"
        "a.chip:hover{border-color:#2563eb;color:#2563eb}"
        ".chip.on{background:#0f2540;border-color:#0f2540;color:#fff;font-weight:600}"
        ".dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:6px;"
        "vertical-align:middle}"
        ".banner{font-size:20px;font-weight:700;line-height:1.35;margin:18px 0 6px;"
        "padding:14px 16px;border-left:6px solid #b45309;background:#fff7ed;color:#7c2d12;"
        "border-radius:4px}</style>"
        + _peer_nav_html(peers or [], str(entry["server_id"]))
        + _markdown_tables_to_html(text)
    )
    if stamp:
        (out_dir / report_archive.archive_name(
            stamp, html_file_name(entry["server_id"]))).write_text(page, encoding="utf-8")
    if archive_only:
        return path
    path.write_text(page, encoding="utf-8")
    return path


def _recommend(row: dict[str, Any]) -> str:
    """The action for one index, by the same rules the summary tables use.

    PostgreSQL differs on the two lines that name a command. It has no REBUILD — an invalid index
    is dropped and re-created — and an index a constraint owns refuses ``DROP INDEX`` outright, so
    telling somebody to drop it is an instruction that fails when they run it.
    """
    postgres = str(row.get("engine") or "") == "postgresql"
    if row["is_disabled"] and postgres:
        # `is_disabled` carries INVALID here: the planner ignores it while every write still
        # maintains it. Never the table-inaccessible incident a disabled clustered index is.
        return "**DROP and re-create — INVALID**"
    if row["is_disabled"] and row["type"] == "CLUSTERED":
        return "**REBUILD now — table inaccessible**"
    if row["is_disabled"]:
        return "REBUILD or DROP"
    if row["is_primary"]:
        return "KEEP — primary key"
    if row["is_uq_constr"]:
        return "KEEP — constraint owns it" if postgres else "KEEP — unique constraint"
    if row["type"] == "CLUSTERED":
        return "KEEP — table storage"
    if row["seeks"] + row["scans"] + row["lookups"] > 0:
        return "keep — in use"
    if row["is_unique"]:
        return "review carefully — unique"
    return "review, then DROP"


def _cap(rows: list, limit: int | None) -> tuple[list, int]:
    """Apply a cap, reporting how many were left out. ``limit=None`` means show everything."""
    if limit is None or len(rows) <= limit:
        return rows, 0
    return rows[:limit], len(rows) - limit


def _format_oracle_index_report(entry: dict[str, Any], *, limit: int | None) -> str:
    """The Oracle page: an index **inventory**, and it says so in its own title.

    A separate renderer rather than branches through the SQL Server one, because almost every
    sentence there is about a number Oracle does not have. "user_seeks = 0" cannot appear on a
    page where nothing counts seeks; "drop candidates" cannot be offered where nothing knows what
    is read. Sharing the layout while quietly leaving those columns empty would produce the one
    outcome this page exists to avoid — an index dropped because a report showed a zero that was
    never a measurement.

    What Oracle can state without being asked to start recording is here: what exists, how big it
    is, whether it still works, and how old its statistics are.
    """
    totals = entry.get("totals") or {}
    every = entry.get("indexes") or []
    out: list[str] = [f"Index Inventory Report — {entry['server_id']}"]
    if entry.get("ip"):
        out.append(f"Host: {entry['ip']}")
    if entry.get("collected_at"):
        out.append(f"Collected: {entry['collected_at']}")
    out.append(f"Server dashboard: {server_dashboard_url(entry['server_id'])}")
    out.append(f"Fleet inventory: {inventory_url()}")
    out.append("")
    # First, and in the reader's way. Someone arriving from the SQL Server version of this page
    # will look for the usage columns, and the honest answer is that this instance is not
    # recording them — not that every index here is idle.
    out.append("## This instance does not record index usage")
    out.append("")
    out.append("Oracle keeps no per-index seek/scan counters unless each index is placed into"
               " `ALTER INDEX ... MONITORING USAGE`, which is a change to the database rather than"
               " to the monitor. So there are no usage columns and no drop candidates here: an"
               " index that is never read looks exactly like one read a thousand times a minute."
               " Everything below is what Oracle states on its own — what exists, its size,"
               " whether it is still usable, and how old its statistics are.")
    out.append("")
    out += ["| Metric | Count | Meaning |",
            "| --- | ---: | --- |",
            f"| indexes_total | {totals.get('indexes_total', 0)} | every index on a user table |",
            f"| unusable | {totals.get('unusable', 0)} | UNUSABLE — queries full-scan, DML raises ORA-01502 |",
            f"| unique_indexes | {totals.get('unique_indexes', 0)} | enforcing uniqueness; never drop candidates |",
            f"| never_analyzed | {totals.get('never_analyzed', 0)} | no statistics at all |",
            f"| stale_stats_30d | {totals.get('stale_stats_30d', 0)} | statistics older than 30 days |",
            ""]

    unusable = entry.get("disabled") or []
    if unusable:
        out += [f"### CRITICAL — {len(unusable)} UNUSABLE index(es)",
                "",
                "The definition is kept and the structure is gone: queries fall back to full"
                " scans and any DML on the table fails with ORA-01502.",
                "",
                "| Index | Type | Recommend action |",
                "| --- | --- | --- |"]
        shown, hidden = _cap(unusable, limit)
        for row in shown:
            out.append(f"| `{row['item']}` | {row['type']} | **ALTER INDEX ... REBUILD now** |")
        if hidden:
            out.append(f"| _... and {hidden} more_ | | |")
        out.append("")

    stale = [row for row in every if str(row.get("last_stats", "")) == "never"]
    if stale:
        out += [f"### Never analyzed ({len(stale)})",
                "",
                "No statistics were ever gathered for these, so the optimizer is costing them"
                " from defaults. On 8i that is the rule-based path in practice.",
                "",
                "| Index | Table | Recommend action |",
                "| --- | --- | --- |"]
        shown, hidden = _cap(sorted(stale, key=lambda r: r["item"]), limit)
        for row in shown:
            out.append(f"| `{row['item']}` | {row.get('table', '')} | ANALYZE INDEX ... COMPUTE STATISTICS |")
        if hidden:
            out.append(f"| _... and {hidden} more_ | | |")
        out.append("")

    if every:
        # Biggest first: without a usage column, size is the only cost this page can show, and
        # cost is what makes one row worth stopping on rather than the next.
        def by_size(row):
            try:
                return -float(str(row.get("size_mb") or 0))
            except ValueError:
                return 0.0

        shown, hidden = _cap(sorted(every, key=lambda r: (by_size(r), r["item"])), limit)
        out += [f"### All indexes ({len(every)})",
                "",
                "Every index on the instance, largest first.",
                "",
                "| Index | Table | Type | Uniq | PK | Constr | Unusable | Size MB | Extents "
                "| last_stats_update |",
                "| --- | --- | --- | :-: | :-: | :-: | :-: | ---: | ---: | --- |"]
        for row in shown:
            out.append(
                "| `{item}` | {table} | {type} | {uq} | {pk} | {ct} | {dis} | {size} | {ext} "
                "| {stats} |".format(
                    item=row["item"], table=row.get("table", ""), type=row["type"],
                    uq="Y" if row["is_unique"] else "", pk="Y" if row["is_primary"] else "",
                    ct="Y" if row["is_uq_constr"] else "", dis="Y" if row["is_disabled"] else "",
                    size=row.get("size_mb", ""), ext=row.get("extents", ""),
                    stats=row.get("last_stats", "never")))
        if hidden:
            out.append(f"| _... and {hidden} more_ |" + " |" * 9)
        out.append("")

    if not (unusable or stale):
        out.append("No unusable indexes, and every index has statistics.")
    return "\n".join(out).rstrip() + "\n"


def format_index_report(entry: dict[str, Any], *, limit: int | None = DEFAULT_DETAIL_LIMIT) -> str:
    """Render one server's index report as tables, each row carrying its own recommended action.

    Tables rather than prose: every row here is a decision about one index, and a decision needs
    the facts side by side. The action column is per row and not per section, because the right
    action genuinely differs within a section - a disabled clustered index must be rebuilt now,
    a disabled nonclustered one can equally well be dropped.

    An Oracle server is rendered by :func:`_format_oracle_index_report` instead: it has no usage
    counters at all, and every heading here is about one.
    """
    if str(entry.get("engine") or "") == "oracle":
        return _format_oracle_index_report(entry, limit=limit)
    totals = entry.get("totals") or {}
    out: list[str] = [f"Index Usage Report — {entry['server_id']}"]
    if entry.get("ip"):
        out.append(f"Host: {entry['ip']}")
    if entry.get("collected_at"):
        out.append(f"Collected: {entry['collected_at']}")
    out.append(f"Server dashboard: {server_dashboard_url(entry['server_id'])}")
    # Up, not sideways. A "This report:" line pointing at the page the reader already has open
    # answers a question nobody asked; the link that is missing from here is the one back to the
    # fleet, which is where a reader goes next after deciding about this instance.
    out.append(f"Fleet inventory: {inventory_url()}")
    out.append("")

    # First, and loudly. Every number below is relative to this instant, and a reader who misses
    # it will read "user_seeks = 0" as "never used" and drop an index that is simply idle since
    # the last restart. Putting it under the tables was burying the one fact that qualifies them.
    since = (totals.get("counters_since") or entry.get("counters_since") or "").strip()
    uptime = totals.get("uptime_days") or ""
    if since or uptime:
        headline = "Usage counters cover " + (f"{uptime} day(s)" if uptime else "the period")
        if since:
            headline += f", since the instance restarted at {since}"
        out.append("## " + headline)
        out.append("")
        out.append("They are zeroed by a restart, so `user_seeks = 0` means \"not used since that"
                   " instant\", not \"never used\". `last_stats_update` is a different clock again:"
                   " it is set by UPDATE STATISTICS / auto-update, not by usage, so a heavily used"
                   " index can still carry statistics from months ago.")
        out.append("")
        # The metric reports detail from 12 hours of uptime, so this report can now be read long
        # before the sample is safe to act on. The counts below are the same either way; what
        # changes is whether "droppable" means anything yet, and that has to be stated where the
        # counts are, not left to whoever remembers the collector's rule.
        if _is_short_sample(uptime):
            out.append(f"> **Short sample — do not drop anything on the strength of this report.**"
                       f" The counters cover only {uptime} day(s) of the {TRUSTED_SAMPLE_DAYS} that"
                       " make a full business week. A weekly report, a month-end close or a"
                       " quarterly batch has not necessarily run yet, so an index serving one of"
                       " them still reads as cold. Use this to see the shape of the instance;"
                       " re-read it after the sample is complete before dropping anything.")
            out.append("")

    out += ["| Metric | Count | Meaning |",
            "| --- | ---: | --- |",
            f"| indexes_total | {totals.get('indexes_total', 0)} | every index on a user table |",
            f"| used | {totals.get('used', 0)} | had a seek, scan or lookup |",
            f"| unused | {totals.get('unused', 0)} | written but never read |",
            f"| cold | {totals.get('cold', 0)} | never read AND never written since restart |",
            f"| disabled | {totals.get('disabled', 0)} | ALTER INDEX ... DISABLE was run |",
            f"| droppable | {totals.get('droppable', 0)} | nonclustered, not unique, no constraint, never read |"]
    if totals.get("fragmented") or entry.get("fragmented"):
        out.append(f"| fragmented | {len(entry.get('fragmented') or [])} | above the rebuild threshold |")
    out.append("")
    clustered = [d for d in entry.get("disabled", []) if d.get("clustered")]
    if clustered:
        out += [f"### CRITICAL — {len(clustered)} disabled CLUSTERED index(es)",
                "",
                "| Index | Type | Impact | Recommend action |",
                "| --- | --- | --- | --- |"]
        shown, hidden = _cap(clustered, limit)
        for row in shown:
            out.append(f"| `{row['item']}` | CLUSTERED | table is inaccessible | "
                       "**ALTER INDEX ... REBUILD now** |")
        out.append("")

    other_disabled = [d for d in entry.get("disabled", []) if not d.get("clustered")]
    if other_disabled:
        out += [f"### Disabled indexes ({len(other_disabled)})",
                "",
                "| Index | Type | Impact | Recommend action |",
                "| --- | --- | --- | --- |"]
        shown, hidden = _cap(other_disabled, limit)
        for row in shown:
            out.append(f"| `{row['item']}` | {row['type']} | structure gone, definition kept | "
                       "REBUILD to restore, or DROP if not wanted |")
        if hidden:
            out.append(f"| _... and {hidden} more_ | | | |")
        out.append("")

    per_db = sorted(entry.get("databases", []),
                    key=lambda d: (d.get("droppable", 0), d.get("cold", 0)), reverse=True)
    listed = [d for d in per_db if d.get("droppable") or d.get("cold") or d.get("disabled")]
    if listed:
        out += ["### By database",
                "",
                "| Database | Total | Cold | Disabled | Droppable | Recommend action |",
                "| --- | ---: | ---: | ---: | ---: | --- |"]
        shown, _hidden = _cap(listed, limit)
        for row in shown:
            drop = row.get("droppable", 0)
            action = ("review the drop candidates above" if drop
                      else "no action — cold indexes here are constraints or clustered")
            out.append(f"| {row['database']} | {row.get('indexes_total', 0)} | {row.get('cold', 0)} "
                       f"| {row.get('disabled', 0)} | {drop} | {action} |")
        out.append("")

    droppable = sorted(entry.get("droppable", []), key=lambda r: r["writes"], reverse=True)
    if droppable:
        out += [f"### Drop candidates ({len(droppable)})",
                "",
                "Nonclustered, not unique, not enforcing a constraint, never read. "
                "Highest write cost first — that is what the index is charging for.",
                "",
                "| Index | user_updates | last_read | State | Recommend action |",
                "| --- | ---: | --- | --- | --- |"]
        shown, hidden = _cap(droppable, limit)
        for row in shown:
            out.append(f"| `{row['item']}` | {row['writes']} | {row['last_read']} | {row['kind']} | "
                       "review, then DROP |")
        if hidden:
            out.append(f"| _... and {hidden} more_ | | | | |")
        out.append("")

    fragmented = entry.get("fragmented") or []
    if fragmented:
        out += [f"### Fragmented ({len(fragmented)})",
                "",
                "| Index | Fragmentation | Recommend action |",
                "| --- | ---: | --- |"]
        shown, hidden = _cap(sorted(fragmented, key=lambda r: r.get("pct") or "", reverse=True), limit)
        for row in shown:
            pct = row.get("pct") or "?"
            action = "REBUILD" if _int(str(pct).rstrip("%")) >= 30 else "REORGANIZE"
            out.append(f"| `{row['item']}` | {pct}% | {action} in the maintenance window |")
        if hidden:
            out.append(f"| _... and {hidden} more_ | | |")
        out.append("")

    every = entry.get("indexes") or []
    if every:
        shown, hidden = _cap(sorted(every, key=lambda r: r["item"]), limit)
        out += [f"### All indexes ({len(every)})",
                "",
                "Every index on the instance, including the healthy ones — the answer to "
                "\"is THIS index used?\" has to be here, not only the problems.",
                "",
                "| Index | id | Type | Uniq | PK | Constr | Filt | Disabled | seeks | scans | "
                "lookups | updates | last_read | last_stats_update | State | Recommend action |",
                "| --- | ---: | --- | :-: | :-: | :-: | :-: | :-: | ---: | ---: | ---: | ---: "
                "| --- | --- | --- | --- |"]
        for row in shown:
            out.append(
                "| `{item}` | {index_id} | {type} | {uq} | {pk} | {ct} | {fl} | {dis} | {seeks} "
                "| {scans} | {lookups} | {writes} | {last_read} | {last_stats} | {state} "
                "| {action} |".format(
                    item=row["item"], index_id=row["index_id"], type=row["type"],
                    uq="Y" if row["is_unique"] else "", pk="Y" if row["is_primary"] else "",
                    ct="Y" if row["is_uq_constr"] else "", fl="Y" if row["has_filter"] else "",
                    dis="Y" if row["is_disabled"] else "",
                    seeks=row["seeks"], scans=row["scans"], lookups=row["lookups"],
                    writes=row["writes"], last_read=row["last_read"],
                    last_stats=row.get("last_stats", "never"),
                    state=row["kind"] or "-", action=_recommend(row)))
        if hidden:
            out.append(f"| _... and {hidden} more_ |" + " |" * 15)
        out.append("")

    if not (clustered or other_disabled or droppable or fragmented):
        out.append("No disabled indexes, no drop candidates and nothing fragmented.")
    return "\n".join(out).rstrip() + "\n"


def create_index_reports(*, sqlite_path: str | Path, days: int = 3,
                         limit: int = DEFAULT_DETAIL_LIMIT,
                         server_id: str | None = None,
                         output_dir: str | Path | None = None,
                         config=None,
                         display_name: str | None = None,
                         as_of: str | None = None,
                         archive_only: bool = False) -> dict[str, Any]:
    """One report per server that has index data. Returns the created report ids."""
    store = DbOpsStore(sqlite_path)
    # Publish by default. A report that exists only as a store row is the state the operator was
    # complaining about: nowhere to click. <runtime>/reports is exactly what the webhost serves.
    if output_dir is None and config is not None:
        output_dir = Path(config.runtime_dir) / "reports"
    servers = collect_index_rows(sqlite_path, days=int(days), as_of=as_of)
    # A server whose metric has not run yet has no totals; an empty report is noise. Deciding that
    # up front rather than inside the loop is what lets every page carry the full picker: the list
    # has to be the servers that WILL have a file, and page one cannot know that from page one.
    fleet = {name: entry for name, entry in servers.items()
             if entry.get("totals") or entry.get("databases")}
    # The picker is the whole fleet even when only one page is being rebuilt (`--server-id`): the
    # other pages are already on disk from the last full run, and dropping them from the one page
    # an operator happens to open is how a set of reports stops being navigable. Anything not on
    # disk and not being written now is left out — a link to a 404 looks like it should work.
    peers = [peer for peer in build_peer_links(fleet)
             if not server_id or peer["server_id"] == server_id
             or (output_dir and (Path(output_dir) / peer["file"]).exists())]
    rendered = {k: v for k, v in fleet.items() if not server_id or k == server_id}
    if not rendered:
        return {"created": 0, "report_ids": [],
                "skipped": [{"reason": "no index metric data in the window"}]}

    generated_at = datetime.now().isoformat(timespec="seconds")
    # A backfill is dated by the day it describes, not by the day it is run.
    day = (as_of or "")[:10].replace("-", "") or datetime.now().strftime("%Y%m%d")
    report_ids: list[int] = []
    published_count = 0
    for name in sorted(rendered):
        entry = rendered[name]
        # Two renderings on purpose. The published page is the one an operator reads and scrolls,
        # so it lists EVERYTHING - a truncated inventory cannot answer "is this index used?" for
        # the index they came to look up. The stored copy stays capped because it is a database
        # row that also feeds Telegram, where a 29k-row body is neither storable nor sendable.
        text = format_index_report(entry, limit=int(limit))
        published = ""
        if output_dir:
            full_text = format_index_report(entry, limit=None)
            published = str(write_index_report_html(entry, full_text, Path(output_dir), peers,
                                                    stamp=day, archive_only=archive_only))
            published_count += 1
        totals = entry.get("totals") or {}
        # The report is per server, so its level is the worst thing found on THAT server: a
        # disabled clustered index means a table is unreadable right now.
        level = "critical" if any(d.get("clustered") for d in entry.get("disabled", [])) else "logging"
        # A backfill republishes a past day as a page; it must not also file that day's findings
        # as new store reports, which would re-alert on things already dealt with days ago.
        if archive_only:
            continue
        report_ids.append(store.insert_report(
            report_code=INDEX_REPORT_CODE,
            report_name=display_name or f"Index Usage — {name}",
            report_type=INDEX_REPORT_TYPE,
            report_level=level,
            report_text=text,
            source_type="metric_results",
            source_id=f"index_usage:{name}",
            metadata={
                "server_id": name, "ip": entry.get("ip", ""),
                "collected_at": entry.get("collected_at", ""),
                "indexes_total": totals.get("indexes_total", 0),
                "droppable": len(entry.get("droppable", [])),
                "disabled": len(entry.get("disabled", [])),
                "fragmented": len(entry.get("fragmented", [])),
                "url": report_base_url() + html_file_name(name),
                "published_file": published,
                "generated_at": generated_at,
            },
        ))
    # `created` counts stored reports; `published` counts pages on disk. A backfill writes pages
    # without filing reports, so the two deliberately differ there.
    return {"created": len(report_ids), "published": published_count,
            "report_ids": report_ids, "skipped": []}
