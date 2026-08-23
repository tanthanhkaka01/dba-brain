"""Master-side inventory operations:

- ``run_inventory_health``: trigger the reports app's ``build-inventory-health`` inside the
  worker container, copy the dated overlay back, and merge its health blocks into the
  canonical ``architecture/database-inventory.json`` (servers without metrics — e.g. lab
  VMs — are left untouched).
- ``build_inventory_summary``: render the dated ``*-summary.md`` from the canonical JSON.

Ported from the standalone update_inventory_health.py / build_inventory_summary.py.
"""

from __future__ import annotations
from db_ops.common.data_sources import inventory_exclude_ip_prefixes
from db_ops.lib.inventory_render import (  # moved to common: shared with reports
    DBTYPE_LABEL,
    DEFAULT_INVENTORY,
    DISK_WARN_PCT,
    HEALTH_BLOCKS,
    _backup_evidence,
    _baseline_lines,
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

from db_ops.control._support import (
    DB_OPS_ROOT,
    DEFAULT_CONTAINER,
    resolve_password,
    sftp_get,
    ssh_capture,
    ssh_connect,
)

# Canonical inventory now lives inside the tool (db_ops/data/) so db_ops is self-contained.
# Reports are written inside the db_ops tool only (never outside it).
DEFAULT_SNAPSHOT_DIR = DB_OPS_ROOT / "runtime" / "reports"
DEFAULT_CONTAINER_RUNTIME = "/app/tools/db_ops/runtime/reports"
DEFAULT_HOST_RUNTIME = "/opt/db_ops/runtime/reports"

# NOTE: this list has drifted from the worker-side one in reports/inventory_summary.py, which
# also carries security_health and os_health. Any block missing here is silently dropped from the
# merge, so add to both.


# --------------------------------------------------------------------------- #
# inventory-health: trigger in container, fetch, merge
# --------------------------------------------------------------------------- #
def run_inventory_health(*, host: str, user: str, password: str | None, port: int = 22,
                         container: str = DEFAULT_CONTAINER, days: int = 2, date: str | None = None,
                         container_runtime: str = DEFAULT_CONTAINER_RUNTIME,
                         host_runtime: str = DEFAULT_HOST_RUNTIME,
                         inventory: str | Path = DEFAULT_INVENTORY,
                         snapshot_dir: str | Path = DEFAULT_SNAPSHOT_DIR,
                         dry_run: bool = False) -> dict:
    stamp = date or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"{stamp}_database-inventory.json"
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    local_overlay = snapshot_dir / file_name

    password = resolve_password(password, host=host, user=user)
    client = ssh_connect(host, user, password, port)
    try:
        cmd = (f"docker exec {container} python -m db_ops.reports.cli build-inventory-health "
               f"--days {int(days)} --date {stamp} --output-dir {container_runtime}")
        print(f"[remote] $ {cmd}", flush=True)
        rc, _out, err = ssh_capture(client, cmd)
        if rc != 0:
            raise SystemExit(f"build-inventory-health failed (exit {rc}): {err.strip()[:400]}")
        print(f"Fetching {host_runtime}/{file_name} -> {local_overlay}", flush=True)
        sftp_get(client, f"{host_runtime}/{file_name}", local_overlay)
    finally:
        client.close()

    overlay = json.loads(local_overlay.read_text(encoding="utf-8"))
    print(f"Overlay: {len(overlay.get('servers', []))} server(s) -> {local_overlay}", flush=True)
    if dry_run:
        print("Dry run - canonical inventory not merged.", flush=True)
        return {"overlay": str(local_overlay), "merged": 0, "dry_run": True}

    inv_path = Path(inventory)
    data = json.loads(inv_path.read_bytes().decode("utf-8-sig"))
    updated = _merge_overlay(overlay, data)
    _write_inventory(inv_path, data)
    untouched = len(data.get("servers", [])) - updated
    print(f"Merged health into {updated} server(s); {untouched} left untouched. Updated {inv_path}", flush=True)
    return {"overlay": str(local_overlay), "merged": updated, "untouched": untouched}


def run_inventory_workflow(*, host: str, user: str, password: str | None, port: int = 22,
                           container: str = DEFAULT_CONTAINER, days: int = 2, date: str | None = None,
                           container_runtime: str = DEFAULT_CONTAINER_RUNTIME,
                           host_runtime: str = DEFAULT_HOST_RUNTIME,
                           inventory: str | Path = DEFAULT_INVENTORY,
                           snapshot_dir: str | Path = DEFAULT_SNAPSHOT_DIR,
                           output_dir: str | Path = DEFAULT_SNAPSHOT_DIR,
                           dry_run: bool = False) -> dict:
    """inventory-health then inventory-summary in one shot. The health step builds + merges
    the overlay; the summary step renders the markdown from the freshly merged canonical JSON.
    A shared ``date`` stamp keeps both files' ``YYYYMMDD_HHMMSS`` prefix identical."""
    stamp = date or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    print("=== inventory-health ===", flush=True)
    health = run_inventory_health(host=host, user=user, password=password, port=port,
                                  container=container, days=days, date=stamp,
                                  container_runtime=container_runtime, host_runtime=host_runtime,
                                  inventory=inventory, snapshot_dir=snapshot_dir, dry_run=dry_run)
    print("\n=== inventory-summary ===", flush=True)
    summary = build_inventory_summary(inventory=inventory, output_dir=output_dir, date=stamp,
                                      exclude_ip_prefixes=inventory_exclude_ip_prefixes())
    return {"stamp": stamp, "health": health, "summary": summary}


# --------------------------------------------------------------------------- #
# inventory-summary: render *-summary.md from the canonical JSON
# --------------------------------------------------------------------------- #
