"""The fleet's backup & restore page - ``backup-restore.html`` (0.34.0).

Asked for on 2026-10-30, beside Fleet inventory, Server metrics, SLA and Index usage: every server's
backup files and every restore that used them, on one page. Each server page already lists its
own (1.217); this is the same table for the whole fleet, with a Server column and filter, so "which
FULLs were never restore-tested", "what changed on disk" and "what did last night's drills copy"
are one look, not one per server.

**One table, two pages.** The rows are :func:`backup_catalog_sections.build_fleet` - each server's
own rows from :func:`~backup_catalog_sections.build_files`, counted by the same ``summarize`` - and
the page draws them with the server page's own JavaScript (``templates/backup_files.js``) and its
own stylesheet, read from its template at render time. A second copy of either would drift.

Today's only, like the server page's section: the catalog has no history of what it held, so a page
rebuilt for a past day is not written from today's rows.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from dbabrain.lib import page_banner, page_kit
from dbabrain.lib.html_json import json_for_html
from dbabrain.lib.timezone import format_offset, offset_minutes
from dbabrain.reports import backup_catalog_sections, restores_table
# One moment's text, and the number of restores listed, for both pages' tables.
from dbabrain.reports.restores_table import RESTORE_ROWS, utc_text as _utc

PAGE_NAME = "backup-restore.html"
TEMPLATE_HTML = Path(__file__).resolve().parent / "templates" / "backup_restore_report.html"
SERVER_TEMPLATE = TEMPLATE_HTML.parent / "server_report.html"
# The table's code is the server page's file, one definition.
from dbabrain.reports.server_report import BACKUP_FILES_JS  # noqa: E402


def report_css() -> str:
    """The server page's stylesheet, so both pages look alike from one source: everything in its
    ``<style>`` before the banner's own block."""
    text = SERVER_TEMPLATE.read_text(encoding="utf-8")
    start = text.index("<style>") + len("<style>")
    return text[start:text.index("__BANNER_CSS__", start)].strip("\n")


def logs_file_name(server_id: str) -> str:
    """A server's log backups, beside the page (0.41.0, 1.393): ``backup-restore-logs_<slug>.json``."""
    from dbabrain.reports.server_report import _slug

    return f"backup-restore-logs_{_slug(server_id)}.json"


def render_page(*, files: dict, snapshot_date: str, report_dir: str | Path | None = None,
                recoverability: str = "", restores: str = "", stamp: str = "") -> str:
    """The page, its rows baked in - every file but the logs, whose rows are each server's own file,
    fetched when *Log backups* is opened (0.41.0, 1.393)."""
    html = TEMPLATE_HTML.read_text(encoding="utf-8")
    replacements = {
        "__REPORT_CSS__": report_css(),
        "__BANNER_CSS__": page_banner.CSS,
        "__PAGE_BANNER__": page_banner.render(
            title="Backup & restore", snapshot_at=snapshot_date, here=PAGE_NAME,
            links=None if report_dir is None else page_banner.siblings_present(
                lambda name: name == PAGE_NAME or (Path(report_dir) / name).exists(),
                index_usage=page_banner.pick_index_usage(
                    path.name for path in Path(report_dir).glob("index-usage_*.htm*")))),
        "__SNAPSHOT_DATE__": snapshot_date,
        "__STAMP__": stamp,
        "__LOG_ROW_LIMIT__": str(backup_catalog_sections.LOG_ROW_LIMIT),
        "__UTC_OFFSET_MINUTES__": str(offset_minutes()),
        "__UTC_OFFSET_LABEL__": format_offset(offset_minutes()),
        "__RECOVERABILITY__": recoverability,
        "__RESTORES__": restores,
        "__FILES__": json_for_html(files, separators=(",", ":")),
        "__BACKUP_FILES_JS__": BACKUP_FILES_JS.read_text(encoding="utf-8"),
    }
    for key, value in replacements.items():
        html = html.replace(key, value)
    return page_kit.inline(html)


def render_recoverability(catalog, *, now=None, entries: list[dict] | None = None,
                          recovery_models: dict[tuple[str, str], str] | None = None,
                          servers: set[str] | None = None) -> str:
    """The *Recoverability* section (0.35.0, 1.248 / 1.252 / 1.253): each restore drill by its tier -
    last success, the 7- and 30-day success rates, OK / OVERDUE / NEVER against the tier's deadline -
    and each server and database's coverage, with what is missing named. From the catalog alone.

    0.41.0 (1.395 / 1.396 - the operator, 2026-11-17, reading the page): a drill no longer configured
    and a server no longer in the register (``servers``) are history, folded under their table, not
    among the live rows - scratch drills on a copy of the data filled it; the Tier and State columns
    are drawn once a drill states a tier (none did: every row read ``-`` / UNTIERED); a database in
    SIMPLE recovery (``recovery_models``, as collected) is asked for no log backup."""
    from datetime import datetime, timezone

    from dbabrain.lib import drill_tiers

    now = now or datetime.now(timezone.utc)
    # Back far enough to find the last success of the slowest tier: a monthly drill that succeeded
    # 31 days ago and failed yesterday read NEVER from a 30-day read, and one with no run in 30 days
    # vanished instead of reading OVERDUE (audit 2026-11-05, C3).
    since = (now - 2 * max(drill_tiers.TIERS.values())).strftime("%Y-%m-%dT%H:%M:%SZ")
    drills = backup_catalog_sections.build_drills(catalog.drill_runs(since=since), now=now, entries=entries)
    coverage = backup_catalog_sections.build_coverage(catalog.coverage(), recovery_models=recovery_models)
    retention = backup_catalog_sections.build_retention(catalog.retention_by_entry())
    tiered = any(d["tier"] for d in drills)

    def drill_row(d: dict) -> str:
        tier = (f"<td>{escape(d['tier'] or '-')}</td><td class='st-{escape(d['state'])}'>"
                f"{escape('no longer configured' if d['state'] == 'REMOVED' else d['state'])}</td>") if tiered else ""
        return (f"<tr><td>{escape(d['restore_id'])}</td>{tier}"
                f"<td>{_utc(d['last_success'])}</td>"
                f"<td>{_utc(d['last_run'])} ({escape(d['last_phase'] or '')})</td>"
                f"<td>{d['ok_7']} / {d['runs_7']}</td><td>{d['ok_30']} / {d['runs_30']}</td>"
                f"<td>{escape(d['source'] or '')} &rarr; {escape(d['target'] or '')}</td></tr>")

    def cover_row(c: dict) -> str:
        levels = escape(", ".join(c["levels"]) or "-")
        if str(c.get("recovery_model") or "").upper() == "SIMPLE":
            levels += " <span class='rc-note'>(SIMPLE recovery: no log)</span>"
        return (f"<tr><td>{escape(c['server_id'])}</td><td>{escape(c['database_name'] or '-')}</td>"
                f"<td class='st-{escape(c['status'])}'>{escape(c['status'])}</td><td>{levels}</td>"
                f"<td>{_utc(c.get('newest_full'))}</td><td>{_utc(c.get('newest_diff'))}</td><td>{_utc(c.get('newest_log'))}</td>"
                f"<td>{c['offloaded']} of {c['files']}</td><td>{_utc(c['last_restored'])}</td>"
                f"<td>{escape('; '.join(c['missing']) or '-')}</td></tr>")

    def folded(rows: list[str], what: str, head: str) -> str:
        return (f"<details class='rc-gone'><summary>{len(rows)} {what} - history, not counted above</summary>"
                f"<table><thead>{head}</thead><tbody>{''.join(rows)}</tbody></table></details>") if rows else ""

    width = 8 if tiered else 6
    drill_head = ("<tr><th>Drill</th>" + ("<th>Tier</th><th>State</th>" if tiered else "")
                  + "<th>Last success</th><th>Last run</th><th>OK, 7 days</th><th>OK, 30 days</th>"
                  "<th>From &rarr; onto</th></tr>")
    drill_rows = ("".join(drill_row(d) for d in drills if d["state"] != "REMOVED")
                  or f"<tr><td colspan='{width}'>No configured restore drill ran in the last 64 days.</td></tr>")
    drills_gone = folded([drill_row(d) for d in drills if d["state"] == "REMOVED"],
                         "drill(s) no longer configured", drill_head)
    cover_head = ("<tr><th>Server</th><th>Database</th><th>Coverage</th><th>Levels</th>"
                  "<th>Newest full</th><th>Newest diff / incr.</th><th>Newest log</th>"
                  "<th>Copied off host</th><th>Last restored</th><th>Missing</th></tr>")
    registered = {s.lower() for s in servers} if servers is not None else None
    live = [c for c in coverage if registered is None or c["server_id"].lower() in registered]
    cover_rows = "".join(cover_row(c) for c in live) or "<tr><td colspan='10'>The catalog holds no backup yet.</td></tr>"
    cover_gone = folded([cover_row(c) for c in coverage if c not in live],
                        "database(s) of servers no longer in the register", cover_head)
    reach_rows = "".join(
        f"<tr><td>{escape(str(r['backupId']))}</td><td>{escape(str(r['server']))}</td><td>{escape(str(r['engine']))}</td>"
        f"<td>{escape(r['windows'])}</td><td>{escape(r['reach'])}</td><td>{_utc(r['since'])}</td></tr>"
        for r in retention) or "<tr><td colspan='6'>No backup run has recorded its retention yet.</td></tr>"
    return (
        "<section class='recover'><h2>Recoverability</h2>"
        "<p class='subtitle'>Each drill against its tier's deadline (daily 26 h, weekly 7.5 days, monthly 32 "
        "days)" + ("" if tiered else " - no drill states a tier yet, so none is held to a deadline") + "; each "
        "database against what a restore needs: a full, log or WAL backups (none for a database in SIMPLE "
        "recovery), a copy off the host, a drill.</p>"
        f"<table><thead>{drill_head}</thead><tbody>{drill_rows}</tbody></table>{drills_gone}"
        f"<table><thead>{cover_head}</thead><tbody>{cover_rows}</tbody></table>{cover_gone}"
        "<p class='subtitle'>Each backup entry's retention, per file type, and how far back point-in-time "
        "recovery reaches - the shorter of the base and the log window - as its newest run recorded them.</p>"
        "<table><thead><tr><th>Backup entry</th><th>Server</th><th>Engine</th><th>Retention</th>"
        "<th>Point-in-time reach</th><th>As of</th></tr></thead>"
        f"<tbody>{reach_rows}</tbody></table></section>")


def render_restores(catalog, *, limit: int = RESTORE_ROWS) -> str:
    """The *Restores* section (0.39.0, 1.365): each of the newest restores - how it ended, the moment
    asked, where the target says each database stopped, and under it the files it applied, in the
    order they went in. From the catalog alone (``restore_history``); what a run did not record is
    said, not left blank. The table is :func:`restores_table.restores_table`, the one a server page
    folds under its backup section."""
    restores = backup_catalog_sections.build_restores(catalog.restore_history(limit=limit))
    return (
        "<section class='recover restores'><h2>Restores</h2>"
        f"<p class='subtitle'>The newest {int(limit)} restores, newest first: how each ended, the moment it was asked "
        "to restore to, where the target itself says each database stopped (in the source server's clock, as the "
        "backups' own headers are), and the files it applied in the order they went in. <i>not recorded</i>: "
        "a restore from before 0.39.0, one that failed before its first file, or an engine that lays down a "
        "whole instance.</p>"
        f"{restores_table.restores_table(restores, empty='The catalog holds no restore yet.')}</section>")


def _recovery_models(source) -> dict[tuple[str, str], str] | None:
    """Each database's recovery model as collected in the last 3 days (``DATABASE_CONFIG``, daily) -
    ``None`` when the metric store cannot be read: the coverage then assumes nothing (1.396)."""
    from datetime import datetime, timedelta, timezone

    from dbabrain.db.metric_store import MetricStore

    since = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return MetricStore(source).latest_values(metric_code="DATABASE_CONFIG", since=since)
    except Exception:  # noqa: BLE001 - the page is still built; log backups stay asked for
        return None


def build_page(*, sqlite_path, output_dir: str | Path, stamp: str, snapshot_date: str) -> str:
    """Write ``backup-restore.html`` (and its dated copy) from the catalog; ``""`` when the catalog
    cannot be opened - no page, so no banner offers a link to one that is not there."""
    from dbabrain.db.backup_catalog import BackupCatalogStore
    from dbabrain.reports.server_report import _write

    try:
        catalog = BackupCatalogStore(sqlite_path)
        catalog.initialize()
        # The logs apart (0.41.0, 1.393): the newest 500 files of a server with a log every 15 minutes
        # were its last hours' logs, and the full every restore of them needs was not on the page. Every
        # other file is embedded; the logs are each server's own file, read when they are asked for.
        is_log = backup_catalog_sections.is_log
        per_server: dict[str, tuple[list[dict], list[dict]]] = {}
        logs_of: dict[str, tuple[list[dict], list[dict]]] = {}
        for server_id in catalog.catalogued_servers():
            restore_rows = catalog.server_restore_files(server_id, limit=backup_catalog_sections.ROW_LIMIT)
            per_server[server_id] = (
                catalog.server_backup_files(server_id, limit=backup_catalog_sections.ROW_LIMIT, logs=False),
                [row for row in restore_rows if not is_log(row)])
            log_rows = catalog.server_backup_files(server_id, limit=backup_catalog_sections.LOG_ROW_LIMIT, logs=True)
            if log_rows:
                logs_of[server_id] = (log_rows, [row for row in restore_rows if is_log(row)])
        # The configured drills, so a tiered one with no run in the window is listed late (C3).
        # Read through lib.data_sources - the one reader of data/ - never the backup app.
        from dbabrain.lib import data_sources
        recoverability = render_recoverability(catalog, entries=data_sources.load_restore_entries(),
                                               recovery_models=_recovery_models(sqlite_path),
                                               servers={str(item.get("server_id") or "")
                                                        for item in data_sources.load_db_instances()
                                                        if isinstance(item, dict)})
        restores = render_restores(catalog)
        repository = backup_catalog_sections.load_repository(
            catalog, [row for group in (per_server, logs_of) for rows, _r in group.values() for row in rows])
        chains: dict[int, dict] = {}
        for rows, _restores in per_server.values():       # per server: each one's own newest rows
            chains.update(backup_catalog_sections.load_chains(catalog, rows))
        for rows, _restores in logs_of.values():
            chains.update(backup_catalog_sections.load_chains(catalog, rows,
                                                              limit=backup_catalog_sections.LOG_ROW_LIMIT))
    except Exception:  # noqa: BLE001 - the other pages are still built; this one is not offered
        return ""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = backup_catalog_sections.for_the_page(backup_catalog_sections.build_fleet(per_server, repository, chains))
    files["logs"] = []
    for server_id, rows in sorted(logs_of.items()):
        table = backup_catalog_sections.for_the_page(
            backup_catalog_sections.build_fleet({server_id: rows}, repository, chains,
                                                limit=backup_catalog_sections.LOG_ROW_LIMIT))
        name = logs_file_name(server_id)
        _write(out_dir, name, json.dumps(table, separators=(",", ":"), default=str), stamp=stamp, archive_only=False)
        files["logs"].append({"server": server_id, "file": name, "count": table["shown"]})
    html = render_page(files=files, snapshot_date=snapshot_date, report_dir=out_dir,
                       recoverability=recoverability, restores=restores, stamp=stamp)
    _write(out_dir, PAGE_NAME, html, stamp=stamp, archive_only=False)
    return str(out_dir / PAGE_NAME)
