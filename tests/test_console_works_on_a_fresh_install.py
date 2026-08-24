"""The web console has to show the apps a fresh `db-ops init` configured.

It showed **0 apps** on a clean `pip install`, with five zeroed tiles and "nothing is failing" —
which is exactly what a healthy console with no problems looks like. Nothing was wrong with the
data: `init` wrote nine app commands, all active, all `node_role: all`.

The chain took three files to break and each looked fine alone:

1. The console reads its layout from `webhost_config.json` **through the config store**, not off
   disk. `init` did not write that file, so there were no *blocks*.
2. `app_blocks()` iterates blocks and hangs the commands off them, so with no blocks it returns
   nothing however many commands exist. Zero apps, no error.
3. The repair — `db sync-config` — refused outright, because `data/config_catalog.json` was also
   missing, and that file says which config files the store may hold.

So the console could not be populated *and* could not be told why. Both files are product data
describing the toolkit's own structure, exactly like the metric catalogue, and `init` writes them
now. The overview also says what to run when it has no blocks, because the next tool root that
predates this fix will still be empty and the page is where somebody will be looking.
"""
from __future__ import annotations

import json

from db_ops import scaffold
from db_ops.webhost import pages


def test_init_writes_what_the_console_needs(tmp_path):
    scaffold.initialise(tmp_path, app_name="probe")
    for name in ("data/config_catalog.json", "data/webhost_config.json"):
        assert (tmp_path / name).is_file(), f"{name} is missing, and the console needs it"


def test_the_shipped_console_layout_names_apps_that_have_commands(tmp_path):
    """A block owning a command id that `init` never writes renders as "missing" in the UI."""
    scaffold.initialise(tmp_path, app_name="probe")
    blocks = json.loads((tmp_path / "data" / "webhost_config.json").read_text(encoding="utf-8"))
    commands = json.loads((tmp_path / "data" / "app_commands.json").read_text(encoding="utf-8"))

    known = {entry["app_command_id"] for entry in commands["app_commands"]}
    owned = {code for block in blocks["apps"] for code in (block.get("app_command_ids") or [])}
    assert owned, "the console layout owns no commands at all"
    assert owned <= known, (
        f"the console would draw these as missing: {sorted(owned - known)}")


def test_the_config_catalog_covers_every_file_init_writes(tmp_path):
    """`sync-config` walks the catalog; a file absent from it never reaches the store."""
    scaffold.initialise(tmp_path, app_name="probe")
    catalog = json.loads((tmp_path / "data" / "config_catalog.json").read_text(encoding="utf-8"))
    catalogued = {entry["file"] for entry in catalog["config_sources"]}

    written = {path.name for path in (tmp_path / "data").glob("*.json")}
    # Not config rows, and so not the console's business:
    #   - `config_catalog.json` is the catalog itself;
    #   - `encrypted_secret_text.json` is the secret store;
    #   - `ops_status_request.json` is a saved *argument* — the payload APP-CONTROL passes to
    #     `ops-status`. It lives in a file only because the daemon runs commands through the
    #     platform's shell and single-quoted JSON does not survive cmd.exe. It holds one request,
    #     not a collection of records, so there is nothing for the console to list or edit.
    written -= {"config_catalog.json", "encrypted_secret_text.json", "ops_status_request.json"}
    missing = sorted(written - catalogued)
    assert not missing, (
        f"init writes {missing}, which the catalog does not list, so sync-config will not load "
        f"them and the console will not see them")


def test_an_empty_dashboard_says_what_to_run():
    """Rendered with no blocks, the page must not look like a healthy console with no work."""
    html = pages.overview_page(
        prefix="/db_ops", session={"csrf_token": "x"}, blocks=[],
        can_edit=False, can_run=False, generated_at="2026-08-24 00:00:00 UTC")
    assert "sync-config" in html, "the empty console has to name the command that fills it"
    assert "config_catalog" in html, "and the file whose absence stops that command working"


def test_a_populated_dashboard_does_not_nag():
    block = {"app_code": "metrics", "ord": 1, "display_name": "Metrics", "summary": "",
             "doc": "", "commands": [], "config": []}
    html = pages.overview_page(
        prefix="/db_ops", session={"csrf_token": "x"}, blocks=[block],
        can_edit=False, can_run=False, generated_at="2026-08-24 00:00:00 UTC")
    assert "sync-config" not in html
