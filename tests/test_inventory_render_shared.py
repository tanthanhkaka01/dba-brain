"""Why the inventory merge/render logic lives in `common` and not in two apps.

Both the master-side `control` app and the worker-side `reports` app produce the inventory summary.
Each used to carry its own copy — 265 identical lines — because the no-cross-app-import rule left
copying as the only way for the second one to reuse the first. The copies then drifted, exactly as a
duplicated rule always does: the reports side learned to read the newer `backup_evidence` block and
to surface curated `findings`, while the control side still read only the older shapes.

These tests pin the thing that keeps that from happening again: there is now one implementation, both
apps reach the same object, and the surviving app-side code is only what genuinely differs.
"""

import ast
from pathlib import Path

from db_ops.lib import inventory_render


def test_both_apps_use_the_same_render_functions_not_copies():
    """Identity, not equality: if either app ever re-declares one of these, this fails."""
    from db_ops.control import inventory as control_inventory
    from db_ops.reports import inventory_summary as reports_inventory

    for name in ("build_inventory_summary", "_merge_overlay", "_render_markdown",
                 "_findings", "_baseline_lines", "_backup_evidence"):
        shared = getattr(inventory_render, name)
        assert getattr(control_inventory, name) is shared, f"control re-declared {name}"
        assert getattr(reports_inventory, name) is shared, f"reports re-declared {name}"


def test_neither_app_defines_the_shared_functions_any_more():
    """Importing the shared one while also defining a local copy would still pass an identity check
    on the import name, so the source is what gets asserted."""
    moved = {"build_inventory_summary", "_merge_overlay", "_render_markdown", "_findings",
             "_baseline_lines", "_backup_evidence", "_g", "_platform", "_remote_user_text",
             "_primary_db", "_write_inventory"}
    for module in ("db_ops/control/inventory.py", "db_ops/reports/inventory_summary.py"):
        tree = ast.parse(Path(module).read_text(encoding="utf-8"))
        declared = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert not (declared & moved), f"{module} still defines {sorted(declared & moved)}"


def test_each_app_keeps_only_what_genuinely_differs():
    """control reaches the worker over SSH/SFTP; reports runs store-local. That difference is the
    reason they are separate apps at all, so it stays app-side."""
    control = ast.parse(Path("db_ops/control/inventory.py").read_text(encoding="utf-8"))
    reports = ast.parse(Path("db_ops/reports/inventory_summary.py").read_text(encoding="utf-8"))

    control_fns = {n.name for n in control.body if isinstance(n, ast.FunctionDef)}
    reports_fns = {n.name for n in reports.body if isinstance(n, ast.FunctionDef)}

    assert control_fns == {"run_inventory_health", "run_inventory_workflow"}
    assert reports_fns == {"build_inventory_workflow"}


def test_the_module_still_lives_where_the_purity_guard_looks() -> None:
    """It moved to ``db_ops/lib/`` on 2026-08-15, and the check that matters moved with it.

    This file used to assert that ``common/inventory_render.py`` imports no app. That rule still
    holds and is now enforced far more strictly: ``tests/test_lib_is_pure.py`` requires every
    ``lib`` module to import **nothing** from ``db_ops`` at all, app or otherwise. Re-checking the
    weaker rule here would only be a second place to update — and a path this file hardcodes is
    exactly what broke when the module moved.

    What is worth keeping is the link: if the module ever leaves ``lib``, the strict guard stops
    covering it silently, so fail here instead.
    """
    assert Path("db_ops/lib/inventory_render.py").exists(), (
        "inventory_render moved out of db_ops/lib/ — tests/test_lib_is_pure.py no longer covers "
        "it. Either move it back or restate its import rule wherever it now lives."
    )



# ---------------------------------------------------------------------------
# The superset behaviour that only one of the two copies had
# ---------------------------------------------------------------------------
def test_backup_evidence_reads_the_new_block_and_still_falls_back_to_the_old_one():
    """The reports copy learned `backup_evidence`; the control copy only knew
    `backup.latest_by_type`. Keeping the fallback is what makes the merged version safe for both."""
    new_shape = {"backup_evidence": {"FULL": {"latest_age_hours": 3}}}
    old_shape = {"backup": {"latest_by_type": {"FULL": {"latest_age_hours": 3}}}}

    assert inventory_render._backup_evidence(new_shape)
    assert inventory_render._backup_evidence(old_shape)


def test_findings_surfaces_curated_entries_but_not_duplicate_low_disk_ones():
    """Low-disk findings are already emitted from the live merged_drives threshold check, so
    repeating the stored one would report the same disk twice."""
    server = {"server_id": "S1", "findings": ["Low disk on C:", "SQL resource not connected"]}

    out = inventory_render._findings([server])

    assert any("SQL resource not connected" in line for line in out)
    assert not any("Low disk on C:" in line for line in out)


def test_merged_drives_prefers_the_sql_metric_and_backfills_from_the_os_inventory():
    """A SQL 2008 xp_fixeddrives row knows free GB but not total; the static WinRM inventory knows
    the total. Neither source alone can compute free_percent."""
    server = {
        "disk_health": {"drives": {"E:\\": {"free_gb": 20.0, "status": "OK"}}},
        "os_resources": {"disks": [{"mount_point": "E:", "total_gb": 100.0,
                                    "logical_volume_name": "DATA"}]},
    }

    drives = inventory_render.merged_drives(server)

    assert len(drives) == 1, "'E:\\' and 'E:' must not both appear"
    only = next(iter(drives.values()))
    assert only["total_gb"] == 100.0 and only["free_gb"] == 20.0
    assert only["free_percent"] == 20.0
    assert only["logical_volume_name"] == "DATA"


def test_merged_sql_resources_lets_live_governance_win_over_stored_structure():
    server = {"sqlserver_resources": {"memory": {"max_server_memory_mb": 8192, "cpu_count": 8}},
              "sql_governance": {"memory": {"max_server_memory_mb": 16384}}}

    merged = inventory_render.merged_sql_resources(server)

    assert merged["memory"]["max_server_memory_mb"] == 16384   # live metric wins
    assert merged["memory"]["cpu_count"] == 8                  # structural fact survives
