"""The *Restores* table - each restore, and under it the files it applied in order - drawn one way on
the fleet page (``backup-restore.html``) and on a server's page (``server-metrics.html``) (0.39.0,
1.365).

The fleet page drew it first, in Python, from :func:`backup_catalog_sections.build_restores`. The
server page is drawn in the browser from its JSON, and a second drawing of the same table in
JavaScript would drift from this one - the reason ``backup_files.js`` is one file pasted into both
pages. So the server page carries this table as HTML, every value escaped here, and its script only
folds it under its backup section.

Two clocks, each said: a run's start and end and a file's *applied* are dbabrain's own, in UTC and
labelled so; a backup's end, a STOPAT and where a database stopped are the SOURCE server's clock, as
the backups' own headers state them, and are printed as recorded. What a run did not record reads
*not recorded* - never a blank that reads as *none*.
"""

from __future__ import annotations

from html import escape

from dbabrain.reports import backup_catalog_sections

#: How many of the newest restores a page lists - the fleet's, or one server's.
RESTORE_ROWS = 60

NOT_RECORDED = "<span class='rs-none'>not recorded</span>"


def utc_text(value) -> str:
    """One of dbabrain's own moments (UTC), to the minute and labelled; ``-`` when there is none."""
    text = str(value or "").strip()
    return escape(text[:16].replace("T", " ") + " UTC") if text else "-"


def as_recorded(value) -> str:
    """A moment the TARGET or a backup's header recorded - the source server's clock, not UTC - as it is."""
    text = str(value or "").strip()
    return escape(text[:19].replace("T", " ")) if text else "-"


def counts_text(counts: dict, order: tuple[str, ...]) -> str:
    known = [f"{counts[name]} {name}" for name in order if counts.get(name)]
    other = [f"{number} {name}" for name, number in counts.items() if name not in order and number]
    return ", ".join(known + other)


def _named(database: str, restored_as: str) -> str:
    return escape(database if not restored_as or database.lower() == restored_as.lower()
                  else f"{database} as {restored_as}")


def _landed_line(one: dict) -> str:
    where = ("stopped at " + as_recorded(one["stopAt"]) if one["byStopat"] and one["stopAt"]
             else "last backup applied ended " + as_recorded(one["lastBackupFinished"])
             if one["lastBackupFinished"] else "stop point not read")
    # One block per database. Its own class names: the pages share the server page's stylesheet,
    # where `verdict` is the big verdict block - each stop point came out with blank lines under it.
    return (f"<div class='rs-db'>{_named(one['database'], one['restoredAs'])}: {where} "
            f"<span class='rs-note'>({escape(one['verdict'])})</span></div>")


def _chain_row(row: dict) -> str:
    unknown = "" if row["inCatalog"] else " <span class='rs-none'>(not in the backup catalog)</span>"
    # A skipped log carries SQL Server's reason, a failed step its error: the reason is the result.
    result = escape(row["result"]) + (" - " + escape(row["error"][:200]) if row["error"] else "")
    last = ("STOPAT " + as_recorded(row["stopat"])) if row["stopat"] else ("recovers" if row["recovers"] else "-")
    # Only an applied file has an applied time; one the run applied and did not stamp is not recorded.
    applied = (utc_text(row["appliedAt"]) if row["appliedAt"]
               else NOT_RECORDED if row["result"] == "applied" else "-")
    return (f"<tr><td>{escape(str(row['position']))}</td><td>{escape(row['step'])}</td>"
            f"<td class='db'>{_named(row['database'], row['restoredAs'])}</td>"
            f"<td class='file'>{escape(row['file'])}{unknown}</td>"
            f"<td>{as_recorded(row['backupEnded']) if row['backupEnded'] else NOT_RECORDED}</td>"
            f"<td class='st-{escape(row['result'])}'>{result}</td><td>{applied}</td>"
            f"<td>{last}</td></tr>")


def restores_table(restores: list[dict], *, empty: str) -> str:
    """The table: ``restores`` is :func:`backup_catalog_sections.build_restores`'s, newest first;
    ``empty`` the sentence a table with no restore says."""
    rows = []
    for item in restores:
        how = "done" if item["phase"] == "done" else ("failed" if item["phase"] == "error" else item["phase"] or "?")
        landed = "".join(_landed_line(one) for one in item["landed"]) or NOT_RECORDED
        # The chain in a row of its own, across the table: inside the last column its file names
        # wrapped letter by letter at 1366 px (looked at in a browser, 2026-11-13).
        chain_row_html = ""
        if item["chain"]:
            counts = (f"<div class='rs-when'>{item['files']} file(s) - "
                      f"{escape(counts_text(item['results'], ('applied', 'skipped', 'failed', 'planned')))}</div>"
                      f"<div class='rs-run'>{escape(counts_text(item['steps'], ('full', 'diff', 'log')))}</div>")
            chain_row_html = (
                "<tr class='chain-row'><td colspan='7'><details><summary>the files, in the order they went in"
                "</summary><table class='chain'><thead><tr><th>#</th><th>Step</th><th>Database</th><th>File</th>"
                "<th>Backup finished (source's clock)</th><th>Result</th><th>Applied</th><th>Ends with</th></tr></thead>"
                f"<tbody>{''.join(_chain_row(row) for row in item['chain'])}</tbody></table></details></td></tr>")
        else:
            counts = NOT_RECORDED
        rows.append(
            f"<tr{' class=has-chain' if chain_row_html else ''}>"
            f"<td>{escape(item['restoreId'])}<div class='rs-run'>run {escape(str(item['run']))}"
            + (f" · {escape(str(item['tier']))}" if item["tier"] else "") + "</div></td>"
            f"<td class='rs-route'><div>{escape(item['source'])}</div><div>&rarr; {escape(item['target'])}</div></td>"
            f"<td class='rs-when'>{utc_text(item['started'])}</td>"
            f"<td class='st-{escape(how)}'>{escape(how)}<div class='rs-run rs-when'>{utc_text(item['ended']) if item['ended'] else 'not closed'}</div>"
            + (f"<div class='rs-why'>{escape(item['error'][:400])}</div>" if item["error"] else "") + "</td>"
            f"<td>{escape(item['moment'])}</td><td>{landed}</td><td>{counts}</td></tr>{chain_row_html}")
    body = "".join(rows) or f"<tr><td colspan='7'>{escape(empty)}</td></tr>"
    return ("<table><thead><tr><th>Restore</th><th>From &rarr; onto</th><th>Started</th><th>Ended</th>"
            "<th>Restored to</th><th>Where each database stopped</th><th>Files applied</th></tr></thead>"
            f"<tbody>{body}</tbody></table>")


def for_server(runs: list[dict]) -> dict:
    """A server page's *Restores* (``restores`` in its payload): ``runs`` is
    ``BackupCatalogStore.restore_history(server_id=...)`` - the restores that read this server's
    backups or restored onto it. The table is :func:`restores_table`, in the class names the
    stylesheet both pages share gives it (``section.restores``); the counts are for the fold's line.
    ``{}`` when the catalog holds none: the page then draws no section, as for its backup files."""
    restores = backup_catalog_sections.build_restores(runs)
    if not restores:
        return {}
    return {"shown": len(restores), "limit": RESTORE_ROWS,
            "failed": sum(1 for item in restores if item["phase"] == "error"),
            "refused": sum(1 for item in restores if item["phase"] == "refused"),
            "html": "<section class='recover restores'>"
                    + restores_table(restores, empty="The catalog holds no restore of or onto this server.")
                    + "</section>"}
