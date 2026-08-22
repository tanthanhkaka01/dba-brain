"""Local (worker-side) inventory workflow for the reports app.

This is the store-local counterpart of the master-side ``control inventory-workflow``:
it never SSHes anywhere and never re-collects metrics. It just reuses the metrics
already in SQLite to

1. build the dated ``<YYYYMMDD_HHMMSS>_database-inventory.json`` health overlay
   (via the reports app's :func:`build_inventory_health`),
2. merge that overlay into the canonical ``architecture/database-inventory.json``, and
3. render the dated ``*-summary.md`` from the freshly merged canonical JSON.

The merge/render code below is intentionally a self-contained copy of the master-side
logic in ``db_ops/control/inventory.py`` so the reports app stays independent of the
control app (no cross-app imports). The master command is kept as-is for now and will be
cleared later.
"""

from __future__ import annotations
from db_ops.common.data_sources import inventory_exclude_ip_prefixes
from db_ops.lib.inventory_render import (  # moved to common: shared with control
    DBTYPE_LABEL,
    DEFAULT_INVENTORY,
    DISK_WARN_PCT,
    HEALTH_BLOCKS,
    TOOL_ROOT,
    _backup_evidence,
    _backup_jobs_text,
    _baseline_lines,
    _fci_text,
    _findings,
    _g,
    _merge_overlay,
    _platform,
    _primary_db,
    _remote_user_text,
    _render_markdown,
    _write_inventory,
    build_inventory_summary,
)

import datetime
import json
from pathlib import Path

from db_ops.reports.inventory_health import build_inventory_health, merged_drives, merged_sql_resources

# tools/db_ops/db_ops/reports/inventory_summary.py -> parents[2] == tool root (tools/db_ops).
# Canonical inventory lives inside the tool (db_ops/data/) so db_ops is self-contained.


# --------------------------------------------------------------------------- #
# Report entry point (called by reports CLI)
# --------------------------------------------------------------------------- #
def build_inventory_workflow(*, sqlite_path, config=None, days=2, date=None,
                             output_dir=None, inventory=None, dry_run=False, beauty=0,
                             logger=None) -> dict:
    """Run the full inventory workflow locally from SQLite: build health overlay,
    merge into the canonical inventory, then render the summary. A shared ``date``
    stamp keeps both files' ``YYYYMMDD_HHMMSS`` prefix identical.

    ``beauty`` (opt-in, default off) additionally renders the styled HTML + Markdown
    inventory report (``inventory_report``) from the merged inventory. The plain
    ``*-summary.md`` is unchanged and always produced."""
    stamp = date or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(output_dir) if output_dir else (
        (config.runtime_dir / "reports") if config else Path("."))
    # The canonical inventory lives under the mounted runtime/reports so it exists in the
    # worker container (the repo's architecture/ dir is not shipped into the image). Falls
    # back to the repo path for non-container runs without a config.
    inv_path = (Path(inventory) if inventory else
                (out_dir / "database-inventory.json" if config else DEFAULT_INVENTORY))

    health = build_inventory_health(sqlite_path=sqlite_path, config=config,
                                    output_dir=out_dir, days=int(days), date=stamp, logger=logger)

    overlay = json.loads(Path(health["file"]).read_text(encoding="utf-8"))
    if dry_run:
        return {"status": "SUCCESS", "stamp": stamp, "health": health,
                "merged": 0, "dry_run": True, "summary": None}

    data = json.loads(inv_path.read_bytes().decode("utf-8-sig"))
    updated = _merge_overlay(overlay, data)
    _write_inventory(inv_path, data)
    untouched = len(data.get("servers", [])) - updated

    summary = build_inventory_summary(inventory=inv_path, output_dir=out_dir, date=stamp,
                                      exclude_ip_prefixes=inventory_exclude_ip_prefixes())
    result = {"status": "SUCCESS", "stamp": stamp, "health": health,
              "merged": updated, "untouched": untouched, "summary": summary}
    if int(beauty or 0):
        # Index pages FIRST. The server page only advertises a link when the target file is
        # already on disk, so building them the other way round hides the link on every run that
        # publishes it - the link would only appear one cycle late, forever.
        from db_ops.reports.index_report import create_index_reports
        result["index_usage"] = create_index_reports(
            sqlite_path=sqlite_path, days=int(days), output_dir=out_dir)

        # Imported lazily so the default workflow path never loads the report renderer.
        from db_ops.reports.inventory_report import build_inventory_report
        # sqlite_path also gives the report the metric history behind each server, so every
        # server row links to its own page of charts over the same window.
        result["report"] = build_inventory_report(inventory=inv_path, output_dir=out_dir, date=stamp,
                                                  sqlite_path=sqlite_path, days=int(days))
    return result


# --------------------------------------------------------------------------- #
# inventory-summary: render *-summary.md from the canonical JSON
# --------------------------------------------------------------------------- #
