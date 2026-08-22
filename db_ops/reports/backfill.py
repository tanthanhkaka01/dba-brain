"""Rebuild ``server-metrics`` and ``index-usage`` pages for days that were never archived.

``?date=`` reads the dated copies written beside each published report. Those copies only start
existing the day the archiving is switched on, so every day before it is unreachable — the reader
asks for 1 August, nothing matches, and the host falls through to today's build.

The data is not missing, though: ``metric_results`` is append-only, so a past day can be rendered
from the rows that were already collected then. That is all this does — the same builders, with the
window closed at the end of that day instead of at "now".

Two rules make a backfill safe to run against a live report directory:

* **It never writes a live file name.** ``archive_only`` publishes only ``YYYYMMDD_<name>``. A
  backfill that touched ``server-metrics.html`` would put 1 August's data on the URL everybody
  reads as "now".
* **It never files store reports.** Re-inserting a past day's index findings would re-alert on
  things that were dealt with days ago.

What a backfill cannot recover is the fleet *inventory* of that day: the server list comes from
``database-inventory.json``, which is a living file with no per-day history. A server added since
will appear in the rebuilt page, and one removed will be missing. The metric data on each page is
genuinely that day's; the roster is today's.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any
from db_ops.common.data_sources import inventory_exclude_ip_prefixes


def _end_of_day(date_text: str) -> str:
    """``YYYY-MM-DD`` → the last second of that day, in the store's UTC text format.

    End of day, not start: a date-only ``?date=`` already means "the last snapshot of that day"
    everywhere else in the system, and the archive it looks for holds the last build of the day.
    """
    day = datetime.datetime.strptime(str(date_text).strip(), "%Y-%m-%d").date()
    return f"{day:%Y-%m-%d}T23:59:59Z"


def backfill_dated_reports(*, sqlite_path, dates: list[str], config=None, days: int = 7,
                           output_dir: str | Path | None = None,
                           inventory: str | Path | None = None) -> dict[str, Any]:
    """Write the ``YYYYMMDD_`` copies of both per-server reports for each date given."""
    from db_ops.reports.index_report import create_index_reports
    from db_ops.reports.inventory_report import build_models
    from db_ops.reports.server_report import build_server_pages

    out_dir = Path(output_dir) if output_dir else (
        (Path(config.runtime_dir) / "reports") if config else Path("."))
    inv_path = Path(inventory) if inventory else (out_dir / "database-inventory.json")
    data = json.loads(inv_path.read_bytes().decode("utf-8-sig"))
    _scope, models = build_models(data, exclude_ip_prefixes=inventory_exclude_ip_prefixes())

    results: list[dict[str, Any]] = []
    for date_text in dates:
        as_of = _end_of_day(date_text)
        stamp = f"{as_of[:10].replace('-', '')}_235959"
        index_result = create_index_reports(
            sqlite_path=sqlite_path, days=int(days), output_dir=out_dir,
            as_of=as_of, archive_only=True)
        links = build_server_pages(
            sqlite_path=sqlite_path, models=models, output_dir=out_dir, stamp=stamp,
            snapshot_date=date_text, days=int(days), inventory_href="database-inventory.html",
            as_of=as_of, archive_only=True)
        results.append({
            "date": date_text,
            "as_of": as_of,
            "server_pages": len(links),
            "index_pages": int(index_result.get("published") or 0),
        })
    return {"status": "SUCCESS", "dates": results}
