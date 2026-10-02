"""Master-side inventory operations:

- ``run_inventory_health``: trigger the reports app's ``build-inventory-health`` inside the
  worker container, copy the dated overlay back, and merge its health blocks into the
  canonical ``architecture/database-inventory.json`` (servers without metrics — e.g. lab
  VMs — are left untouched).

Rendering the summary is ``common.cli inventory-summary``, and the whole overlay-merge-render
workflow is the worker's ``reports.cli inventory-workflow``: this module's ``build_inventory_summary``
re-export and ``run_inventory_workflow`` did the same jobs a second time and went in 0.24.0
(rules R43, the operator's choice).
"""

from __future__ import annotations
from db_ops.lib.inventory_render import (  # shared with reports: one merge, one allowlist
    DEFAULT_INVENTORY,
    HEALTH_BLOCKS,
    _merge_overlay,
    _write_inventory,
)

import datetime
import json
import shlex
from pathlib import Path

from db_ops.control._support import (
    DB_OPS_ROOT,
    DEFAULT_CONTAINER,
    resolve_password,
    sftp_get,
    ssh_capture,
    ssh_connect,
)
from db_ops.lib.timezone import file_stamp

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
    stamp = date or file_stamp()
    file_name = f"{stamp}_database-inventory.json"
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    local_overlay = snapshot_dir / file_name

    password = resolve_password(password, host=host, user=user)
    client = ssh_connect(host, user, password, port)
    try:
        # Quoted (review 0.25.0, F6.3): both names come from the operator's flags.
        cmd = (f"docker exec {shlex.quote(container)} python -m db_ops.reports.cli build-inventory-health "
               f"--days {int(days)} --date {shlex.quote(str(stamp))} "
               f"--output-dir {shlex.quote(str(container_runtime))}")
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

