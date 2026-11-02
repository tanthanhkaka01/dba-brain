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


def render_page(*, files: dict, snapshot_date: str, report_dir: str | Path | None = None) -> str:
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
        "__FILES__": json_for_html(files, separators=(",", ":")),
        "__BACKUP_FILES_JS__": BACKUP_FILES_JS.read_text(encoding="utf-8"),
    }
    for key, value in replacements.items():
        html = html.replace(key, value)
    return page_kit.inline(html)


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
    except Exception:  # noqa: BLE001 - the other pages are still built; this one is not offered
        return ""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html = render_page(files=backup_catalog_sections.build_fleet(per_server), snapshot_date=snapshot_date,
                       report_dir=out_dir)
    _write(out_dir, PAGE_NAME, html, stamp=stamp, archive_only=False)
    return str(out_dir / PAGE_NAME)
