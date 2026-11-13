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

from html import escape
from pathlib import Path

from dbabrain.lib import page_banner, page_kit
from dbabrain.lib.html_json import json_for_html
from dbabrain.lib.timezone import format_offset, offset_minutes
from dbabrain.reports import backup_catalog_sections

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


def render_page(*, files: dict, snapshot_date: str, report_dir: str | Path | None = None,
                recoverability: str = "") -> str:
    """The page, its rows baked in (one JSON; the fleet's tables are small next to a server's series)."""
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
        "__UTC_OFFSET_MINUTES__": str(offset_minutes()),
        "__UTC_OFFSET_LABEL__": format_offset(offset_minutes()),
        "__RECOVERABILITY__": recoverability,
        "__FILES__": json_for_html(files, separators=(",", ":")),
        "__BACKUP_FILES_JS__": BACKUP_FILES_JS.read_text(encoding="utf-8"),
    }
    for key, value in replacements.items():
        html = html.replace(key, value)
    return page_kit.inline(html)


def _utc(value) -> str:
    text = str(value or "").strip()
    return escape(text[:16].replace("T", " ") + " UTC") if text else "-"


def render_recoverability(catalog, *, now=None, entries: list[dict] | None = None) -> str:
    """The *Recoverability* section (0.35.0, 1.248 / 1.252 / 1.253): each restore drill by its tier -
    last success, the 7- and 30-day success rates, OK / OVERDUE / NEVER against the tier's deadline -
    and each server and database's coverage, with what is missing named. From the catalog alone."""
    from datetime import datetime, timedelta, timezone

    from dbabrain.lib import drill_tiers

    now = now or datetime.now(timezone.utc)
    # Back far enough to find the last success of the slowest tier: a monthly drill that succeeded
    # 31 days ago and failed yesterday read NEVER from a 30-day read, and one with no run in 30 days
    # vanished instead of reading OVERDUE (audit 2026-11-05, C3).
    since = (now - 2 * max(drill_tiers.TIERS.values())).strftime("%Y-%m-%dT%H:%M:%SZ")
    drills = backup_catalog_sections.build_drills(catalog.drill_runs(since=since), now=now, entries=entries)
    coverage = backup_catalog_sections.build_coverage(catalog.coverage())
    retention = backup_catalog_sections.build_retention(catalog.retention_by_entry())
    drill_rows = "".join(
        f"<tr><td>{escape(d['restore_id'])}</td><td>{escape(d['tier'] or '-')}</td>"
        f"<td class='st-{escape(d['state'])}'>{escape('no longer configured' if d['state'] == 'REMOVED' else d['state'])}</td>"
        f"<td>{_utc(d['last_success'])}</td>"
        f"<td>{_utc(d['last_run'])} ({escape(d['last_phase'] or '')})</td>"
        f"<td>{d['ok_7']} / {d['runs_7']}</td><td>{d['ok_30']} / {d['runs_30']}</td>"
        f"<td>{escape(d['source'] or '')} &rarr; {escape(d['target'] or '')}</td></tr>"
        for d in drills) or "<tr><td colspan='8'>No restore drill in the last 64 days.</td></tr>"
    cover_rows = "".join(
        f"<tr><td>{escape(c['server_id'])}</td><td>{escape(c['database_name'] or '-')}</td>"
        f"<td class='st-{escape(c['status'])}'>{escape(c['status'])}</td><td>{escape(', '.join(c['levels']) or '-')}</td>"
        f"<td>{_utc(c.get('newest_full'))}</td><td>{_utc(c.get('newest_diff'))}</td><td>{_utc(c.get('newest_log'))}</td>"
        f"<td>{c['offloaded']} of {c['files']}</td><td>{_utc(c['last_restored'])}</td>"
        f"<td>{escape('; '.join(c['missing']) or '-')}</td></tr>"
        for c in coverage) or "<tr><td colspan='10'>The catalog holds no backup yet.</td></tr>"
    reach_rows = "".join(
        f"<tr><td>{escape(str(r['backupId']))}</td><td>{escape(str(r['server']))}</td><td>{escape(str(r['engine']))}</td>"
        f"<td>{escape(r['windows'])}</td><td>{escape(r['reach'])}</td><td>{_utc(r['since'])}</td></tr>"
        for r in retention) or "<tr><td colspan='6'>No backup run has recorded its retention yet.</td></tr>"
    return (
        "<section class='recover'><h2>Recoverability</h2>"
        "<p class='subtitle'>Each drill against its tier's deadline (daily 26 h, weekly 7.5 days, monthly 32 "
        "days); each database against what a restore needs: a full, log or WAL backups, a copy off the host, "
        "a drill.</p>"
        "<table><thead><tr><th>Drill</th><th>Tier</th><th>State</th><th>Last success</th><th>Last run</th>"
        "<th>OK, 7 days</th><th>OK, 30 days</th><th>From &rarr; onto</th></tr></thead>"
        f"<tbody>{drill_rows}</tbody></table>"
        "<table><thead><tr><th>Server</th><th>Database</th><th>Coverage</th><th>Levels</th>"
        "<th>Newest full</th><th>Newest diff / incr.</th><th>Newest log</th>"
        "<th>Copied off host</th><th>Last restored</th><th>Missing</th></tr></thead>"
        f"<tbody>{cover_rows}</tbody></table>"
        "<p class='subtitle'>Each backup entry's retention, per file type, and how far back point-in-time "
        "recovery reaches - the shorter of the base and the log window - as its newest run recorded them.</p>"
        "<table><thead><tr><th>Backup entry</th><th>Server</th><th>Engine</th><th>Retention</th>"
        "<th>Point-in-time reach</th><th>As of</th></tr></thead>"
        f"<tbody>{reach_rows}</tbody></table></section>")


def build_page(*, sqlite_path, output_dir: str | Path, stamp: str, snapshot_date: str) -> str:
    """Write ``backup-restore.html`` (and its dated copy) from the catalog; ``""`` when the catalog
    cannot be opened - no page, so no banner offers a link to one that is not there."""
    from dbabrain.db.backup_catalog import BackupCatalogStore
    from dbabrain.reports.server_report import _write

    try:
        catalog = BackupCatalogStore(sqlite_path)
        catalog.initialize()
        per_server = {
            server_id: (catalog.server_backup_files(server_id, limit=backup_catalog_sections.ROW_LIMIT),
                        catalog.server_restore_files(server_id, limit=backup_catalog_sections.ROW_LIMIT))
            for server_id in catalog.catalogued_servers()}
        # The configured drills, so a tiered one with no run in the window is listed late (C3).
        # Read through lib.data_sources - the one reader of data/ - never the backup app.
        from dbabrain.lib import data_sources
        recoverability = render_recoverability(catalog, entries=data_sources.load_restore_entries())
        repository = backup_catalog_sections.load_repository(
            catalog, [row for rows, _restores in per_server.values() for row in rows])
        chains: dict[int, dict] = {}
        for rows, _restores in per_server.values():       # per server: each one's own newest rows
            chains.update(backup_catalog_sections.load_chains(catalog, rows))
    except Exception:  # noqa: BLE001 - the other pages are still built; this one is not offered
        return ""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html = render_page(files=backup_catalog_sections.build_fleet(per_server, repository, chains), snapshot_date=snapshot_date,
                       report_dir=out_dir, recoverability=recoverability)
    _write(out_dir, PAGE_NAME, html, stamp=stamp, archive_only=False)
    return str(out_dir / PAGE_NAME)
