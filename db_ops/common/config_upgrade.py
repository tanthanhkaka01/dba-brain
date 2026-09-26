"""``upgrade-config`` — carry an existing ``data/`` forward after dbabrain is upgraded.

A new version can rename a field or move one; every reader then takes both the old and the new
shape, and every writer writes the new one. That leaves an estate's own files - written by hand, by
the bot, by an older version's registrars - in the old shape, readable but reported ``deprecated``
by ``check-objects`` and invisible to anything that only knows the new name. This command is the
one step an operator runs after ``pip install --upgrade dbabrain`` to close that gap, instead of
learning which migrations each release carried.

**Every step is idempotent.** A file already in the new shape plans nothing, so the command can be
run after every upgrade without knowing which version the files came from.

**A plan first.** ``dry_run`` defaults to true. A write keeps a copy of every file it changes under
``runtime/config_upgrade/<UTC stamp>/`` beside ``data/`` - ``runtime/`` is generated and never
shipped - so an upgrade that went wrong is one copy back.

**A value is never chosen.** Where a record carries two spellings that disagree, the step reports a
conflict and leaves that file whole. Where a moved value is itself wrong - a restore's ``target.id``
that names no instance - it is moved as it is and ``check-references`` names it afterwards: fixing a
label is a decision about the estate, not about a file format.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_ops.common import field_migration
from db_ops.lib import config_references, shared_objects
from db_ops.lib.json_io import atomic_write_text, indent_of
from db_ops.lib.moved_commands import MOVED_COMMANDS
from db_ops.lib.paths import PACKAGED_CATALOGUE

USAGE = """\
Usage: python -m db_ops.common.cli upgrade-config '<json>'|@file|-

Run after upgrading dbabrain: move this node's data/*.json from the shapes an older version wrote
to the ones this version writes. Every reader already takes both; this moves the files.

  {}                                  // PLAN ONLY - nothing is written
  {"dry_run": false}                  // write it; each changed file is copied first to
                                      //   runtime/config_upgrade/<UTC stamp>/
  {"data_dir": "D:/other/data"}       // another root
  {"steps": ["field-names"]}          // only some steps

Steps, in order (each one idempotent - a file already moved plans nothing):
  reference-files     data/shared_config_objects.json and data/config_references.json replaced by
                      this version's copies. They are reference, identical on every node - an
                      older copy does not know the new names and reports every moved field
  field-names         enabled->active, env->environment, ord/app_ord/menu_order->sort_order,
                      database/db_name->database_name, sql_name->display_name, a restore's
                      databases->database_mappings, ... (the table: db_ops/lib/field_names.py)
  restore-machine-ids a restore's source.id / target.id -> server_id / target_server_id on the entry
  telegram-active     a Telegram group's or user's status "active" -> active true (anything else ->
                      false), the switch every other record carries
  inventory-into-reports  APP-REPORTS-INVENTORY-WORKFLOW becomes the report rp_inventory_health in
                      reports_config.json, with this node's own active and time_window; its timeout
                      is added to APP-REPORTS-CREATE's, whose pass now builds it; the app command
                      and the console's reference to it are removed
  moved-commands      a command line naming a command that moved to another CLI is pointed at it:
                      "db_ops.common.cli", "self-status" -> "db_ops.db.cli", "self-status" (the
                      last-run column is read from the store, which only db.cli opens)

A record whose two spellings DISAGREE is a conflict: its file is not written, and the answer names
the record. After a write, check-objects and check-references are run and their counts reported -
a moved value that names nothing (an old target.id label) is theirs to name, not this command's
to guess at.

data: {"dry_run", "steps": [{"step", "files": [...], "records_changed", "conflicts"}],
       "backup_dir", "check_objects", "check_references"}
Exit code 1 when a conflict left a file unwritten.
"""

STEPS = ("reference-files", "field-names", "restore-machine-ids", "telegram-active",
         "inventory-into-reports", "moved-commands")


#: The app command that was a report (0.22.0), and the report it becomes.
INVENTORY_APP = "APP-REPORTS-INVENTORY-WORKFLOW"
INVENTORY_REPORT = "rp_inventory_health"
REPORTS_APP = "APP-REPORTS-CREATE"


def _inventory_into_reports(root: Path, *, dry_run: bool, before_write: Any) -> dict[str, Any]:
    """The inventory is a report, not an app: move the app command into reports_config.json.

    Idempotent - a node with no such app command plans nothing. A reports_config that already
    carries the report keeps its own entry; the app command still goes, because two schedulers
    for one build is the double run this step exists to prevent.
    """
    commands_path, reports_path = root / "app_commands.json", root / "reports_config.json"
    if not commands_path.is_file() or not reports_path.is_file():
        return {"files": []}
    commands = json.loads(commands_path.read_bytes().decode("utf-8-sig"))
    rows = commands.get("app_commands") or []
    app = next((r for r in rows if isinstance(r, dict) and r.get("app_code") == INVENTORY_APP), None)
    if app is None:
        return {"files": []}
    reports = json.loads(reports_path.read_bytes().decode("utf-8-sig"))
    entries = reports.setdefault("reports", [])
    days = re.search(r"--days\s+(\d+)", str(app.get("command_text") or ""))
    window = dict(app.get("time_window") or {})
    files: list[dict[str, Any]] = []
    if not any(isinstance(e, dict) and e.get("report_code") == INVENTORY_REPORT for e in entries):
        entries.append({
            "report_code": INVENTORY_REPORT,
            "display_name": "Inventory health + summary",
            "metric_max_age_seconds": int(days.group(1) if days else 2) * 86400,
            "active": bool(app.get("active", True)),
            "time_window": window,
            "note": "Moved from the app command " + INVENTORY_APP + " by upgrade-config (0.22.0): the "
                    "inventory is a report, built by the reports app's own pass.",
        })
        files.append({"file": reports_path.name, "records_changed": 1, "conflicts": [],
                      "changes": [{"field": "reports[]", "to": INVENTORY_REPORT, "action": "added"}]})
    reports_app = next((r for r in rows if isinstance(r, dict) and r.get("app_code") == REPORTS_APP), None)
    changes = [{"field": INVENTORY_APP, "to": "reports_config.json:" + INVENTORY_REPORT, "action": "removed"}]
    if reports_app is not None and window.get("timeout"):
        # The reports pass now builds the inventory too, and a timeout is the budget for the WHOLE
        # pass: kept as it was, the first inventory build would be killed at the old limit.
        own = dict(reports_app.get("time_window") or {})
        before = int(own.get("timeout") or 0)
        own["timeout"] = before + int(window["timeout"])
        reports_app["time_window"] = own
        changes.append({"field": REPORTS_APP + ".time_window.timeout", "to": own["timeout"],
                        "action": f"raised from {before}"})
    commands["app_commands"] = [r for r in rows if r is not app]
    files.append({"file": commands_path.name, "records_changed": 1, "conflicts": [], "changes": changes})
    web_path = root / "webhost_config.json"
    web = json.loads(web_path.read_bytes().decode("utf-8-sig")) if web_path.is_file() else None
    web_changed = False
    for block in (web or {}).get("apps") or []:
        ids = block.get("app_command_ids") if isinstance(block, dict) else None
        if isinstance(ids, list) and INVENTORY_APP in ids:
            block["app_command_ids"] = [i for i in ids if i != INVENTORY_APP]
            web_changed = True
    if web_changed:
        files.append({"file": web_path.name, "records_changed": 1, "conflicts": [],
                      "changes": [{"field": "apps[].app_command_ids", "to": "-", "action": "removed " + INVENTORY_APP}]})
    if not dry_run:
        written = [(reports_path, reports)] if files[0]["file"] == reports_path.name else []
        written += [(commands_path, commands)] + ([(web_path, web)] if web_changed else [])
        for path, document in written:
            before_write(path)
            atomic_write_text(path, json.dumps(document, ensure_ascii=False, indent=indent_of(path)) + "\n")
    for item in files:
        item["written"] = not dry_run
    return {"files": files}

#: The files whose records switched on with a STRING status, and the list each keeps them in.
_STATUS_FILES = (("telegram_groups.json", "telegram_groups"), ("telegram_users.json", "telegram_users"))


def _telegram_active(root: Path, *, dry_run: bool, before_write: Any) -> dict[str, Any]:
    """``status: "active"`` -> ``active: true``, in place; a record carrying both is left alone
    when they disagree, because which one the operator meant is not a format question."""
    from db_ops.lib.target_flags import is_record_active

    files: list[dict[str, Any]] = []
    for base, list_key in _STATUS_FILES:
        for name in (base, base.replace(".json", ".example.json")):
            path = root / name
            if not path.is_file():
                continue
            document = json.loads(path.read_bytes().decode("utf-8-sig"))
            records = document.get(list_key) if isinstance(document, dict) else None
            if not isinstance(records, list):
                continue
            changes: list[dict[str, Any]] = []
            conflicts: list[dict[str, Any]] = []
            for index, record in enumerate(records):
                if not isinstance(record, dict) or "status" not in record:
                    continue
                where = f"{list_key}[{index}]"
                as_bool = str(record["status"]).strip().lower() == "active"
                if "active" in record and is_record_active({"active": record["active"]}) != as_bool:
                    conflicts.append({"where": where, "standard": "active",
                                      "values": {"status": record["status"],
                                                 "active": record["active"]}})
                    continue
                records[index] = {("active" if key == "status" else key):
                                  (as_bool if key == "status" else value)
                                  for key, value in record.items() if not (
                                      key == "active" and "status" in record)}
                changes.append({"where": where, "field": "status", "to": "active",
                                "value": as_bool})
            written = bool(changes) and not conflicts and not dry_run
            if written:
                before_write(path)
                # The file's own text first, so a hand-formatted example keeps its layout; trusted
                # only when it parses to exactly the migrated document.
                raw = path.read_bytes().decode("utf-8-sig")
                text = re.sub(r'"status"(\s*):(\s*)"active"', r'"active"\1:\2true', raw)
                if json.loads(text) != document:
                    text = json.dumps(document, ensure_ascii=False,
                                      indent=indent_of(path, default=2)) + "\n"
                atomic_write_text(path, text)
            files.append({"file": name, "records_changed": len(changes), "changes": changes,
                          "conflicts": conflicts, "written": written})
    return {"files": files}

def _moved_commands(root: Path, *, dry_run: bool, before_write: Any) -> dict[str, Any]:
    """Point every command line that names a moved command at the CLI that has it now.

    Only the argv form - ``["{python}", "-m", "db_ops.common.cli", "self-status", ...]`` - which is
    how a bot command and an app command state what they run. The rewrite is on the file's own
    text, so its layout survives, and it is trusted only when the result parses to exactly the
    document the same rewrite gives structurally.
    """
    files: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        if path.name in REFERENCE_FILES:
            continue
        raw = path.read_bytes().decode("utf-8-sig")
        if not any(f'"{module}"' in raw for module, _command in MOVED_COMMANDS):
            continue
        try:
            document = json.loads(raw)
        except ValueError:
            continue
        changes: list[dict[str, Any]] = []

        def walk(value: Any, where: str) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    walk(item, f"{where}.{key}" if where else str(key))
            elif isinstance(value, list):
                for index in range(len(value) - 1):
                    new_module = (MOVED_COMMANDS.get((value[index], value[index + 1]))
                                  if isinstance(value[index], str) and isinstance(value[index + 1], str)
                                  else None)
                    if new_module:
                        changes.append({"where": f"{where}[{index}]", "command": value[index + 1],
                                        "from": value[index], "to": new_module})
                        value[index] = new_module
                for index, item in enumerate(value):
                    walk(item, f"{where}[{index}]")

        walk(document, "")
        written = bool(changes) and not dry_run
        if written:
            before_write(path)
            text = raw
            for (module, command), new_module in MOVED_COMMANDS.items():
                text = re.sub(rf'"{re.escape(module)}"(\s*,\s*)"{re.escape(command)}"',
                              lambda match, new=new_module, cmd=command: f'"{new}"{match.group(1)}"{cmd}"',
                              text)
            if json.loads(text) != document:
                text = json.dumps(document, ensure_ascii=False, indent=indent_of(path, default=2)) + "\n"
            atomic_write_text(path, text)
        if changes:
            files.append({"file": path.name, "records_changed": len(changes), "changes": changes,
                          "conflicts": [], "written": written})
    return {"files": files}


#: The files this version ships as REFERENCE rather than configuration: nobody edits them, every node
#: holds the same bytes, and the other steps are checked against them. They go first, because an
#: older copy calls every field the next step moves an unknown one.
REFERENCE_FILES = ("shared_config_objects.json", "config_references.json")


def _same_reference(path: Path, shipped: Path) -> bool:
    """Whether a node's reference file says what this version ships.

    Compared as documents, not bytes: `init` writes these through `json.dumps` in text mode, so a
    root this version created on Windows holds the same reference with CRLF and a different indent,
    and a byte comparison called it an older version's on the day it was written - the 0.22.0 soak
    node's `init` announced three records to move, two of them these. A file that does not parse is
    not the shipped one.
    """
    try:
        return (json.loads(path.read_bytes().decode("utf-8-sig"))
                == json.loads(shipped.read_bytes().decode("utf-8-sig")))
    except (OSError, ValueError):
        return False


def _reference_files(root: Path, *, dry_run: bool, before_write: Any) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for name in REFERENCE_FILES:
        shipped = PACKAGED_CATALOGUE / name
        for candidate in (name, name.replace(".json", ".example.json")):
            path = root / candidate
            if not path.is_file() or not shipped.is_file():
                continue
            if _same_reference(path, shipped):
                continue
            written = not dry_run
            if written:
                before_write(path)
                atomic_write_text(path, shipped.read_bytes().decode("utf-8"))
            files.append({"file": candidate, "records_changed": 1,
                          "changes": [{"field": "(whole file)", "to": f"this version's {name}"}],
                          "conflicts": [], "written": written})
    return {"files": files}

#: Where a restore entry's machines live since 0.22.0, and the legacy key inside each block.
_RESTORE_IDS = (("source", "server_id"), ("target", "target_server_id"))


def _restore_machine_ids(root: Path, *, dry_run: bool,
                         before_write: Any) -> dict[str, Any]:
    """Move ``source.id`` / ``target.id`` onto the restore entry as ``server_id`` / ``target_server_id``."""
    files: list[dict[str, Any]] = []
    for name in ("restore_config.json", "restore_config.example.json"):
        path = root / name
        if not path.is_file():
            continue
        document = json.loads(path.read_bytes().decode("utf-8-sig"))
        changes: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        records = 0
        for label, entry in shared_objects.walk_records(document, "backup_restore.restores[]"):
            if not isinstance(entry, dict):
                continue
            moves = []
            for block_name, standard in _RESTORE_IDS:
                block = entry.get(block_name)
                if not isinstance(block, dict) or "id" not in block:
                    continue
                if standard in entry and entry[standard] != block["id"]:
                    conflicts.append({"where": label, "standard": standard,
                                      "values": {f"{block_name}.id": block["id"],
                                                 standard: entry[standard]}})
                    continue
                moves.append((block_name, standard))
            if not moves:
                continue
            records += 1
            rebuilt: dict[str, Any] = {}
            for key, value in entry.items():
                if key in {block for block, _ in moves}:
                    value = {k: v for k, v in value.items() if k != "id"}
                rebuilt[key] = value
                if key == "restore_id":
                    for block_name, standard in moves:
                        rebuilt[standard] = entry.get(standard, entry[block_name]["id"])
            for block_name, standard in moves:
                rebuilt.setdefault(standard, entry[block_name]["id"])
                changes.append({"where": label, "field": f"{block_name}.id", "to": standard,
                                "value": entry[block_name]["id"]})
            entry.clear()
            entry.update(rebuilt)
        written = bool(changes) and not conflicts and not dry_run
        if written:
            before_write(path)
            atomic_write_text(path, json.dumps(document, ensure_ascii=False,
                                               indent=indent_of(path)) + "\n")
        files.append({"file": name, "records_changed": records, "changes": changes,
                      "conflicts": conflicts, "written": written})
    return {"files": files}


def upgrade(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    root = shared_objects.data_root(request.get("data_dir") or None)
    dry_run = request.get("dry_run", True) is not False
    wanted = request.get("steps") or list(STEPS)
    unknown = [step for step in wanted if step not in STEPS]
    if unknown:
        raise ValueError(f"unknown step(s) {unknown}; the steps are {list(STEPS)}.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = root.parent / "runtime" / "config_upgrade" / stamp
    backed_up: list[str] = []

    def before_write(path: Path) -> None:
        # One copy per file per run, of the file as it was before THIS run touched it.
        if path.name in backed_up:
            return
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_dir / path.name)
        backed_up.append(path.name)

    results: list[dict[str, Any]] = []
    for step in STEPS:
        if step not in wanted:
            continue
        if step == "reference-files":
            files = _reference_files(root, dry_run=dry_run, before_write=before_write)["files"]
        elif step == "field-names":
            if not dry_run:
                # Back up what the step is about to write, before it writes.
                _, plan = field_migration.standardize({"data_dir": str(root)})
                for item in plan["files"]:
                    if item["changes"] and not item["conflicts"]:
                        before_write(root / item["file"])
            _, data = field_migration.standardize({"data_dir": str(root), "dry_run": dry_run})
            files = data["files"]
        elif step == "restore-machine-ids":
            files = _restore_machine_ids(root, dry_run=dry_run, before_write=before_write)["files"]
        elif step == "telegram-active":
            files = _telegram_active(root, dry_run=dry_run, before_write=before_write)["files"]
        elif step == "moved-commands":
            files = _moved_commands(root, dry_run=dry_run, before_write=before_write)["files"]
        else:
            files = _inventory_into_reports(root, dry_run=dry_run, before_write=before_write)["files"]
        results.append({
            "step": step, "files": [item for item in files if item["changes"] or item["conflicts"]],
            "records_changed": sum(item["records_changed"] for item in files),
            "conflicts": sum(len(item["conflicts"]) for item in files)})

    records = sum(item["records_changed"] for item in results)
    conflicts = sum(item["conflicts"] for item in results)
    # The checks read the node's own reference. While that is still an older version's copy - a
    # plan, or a run without the reference-files step - their answer would be about the version
    # being left, so they wait for it.
    stale_reference = any(
        (root / name).is_file() and (PACKAGED_CATALOGUE / name).is_file()
        and not _same_reference(root / name, PACKAGED_CATALOGUE / name)
        for name in REFERENCE_FILES)
    checks: dict[str, Any] = {"check_objects": None, "check_references": None}
    if not stale_reference:
        objects = shared_objects.check_data_dir(root)
        references = config_references.check(root)
        checks = {
            "check_objects": {"violations": len(objects["violations"]),
                              "deprecated": len(objects["deprecated"])},
            "check_references": {"dangling": len(references["dangling"]),
                                 "dangling_values": [f"{item['where']} -> {item['value']}"
                                                     for item in references["dangling"]]},
        }
    verb = "would change" if dry_run else "changed"
    message = (f"{verb} {records} record(s)"
               + (f"; {conflicts} CONFLICT(S) - those files were not written" if conflicts else "")
               + (f"; backup in {backup_dir}" if backed_up else "")
               + ("; dry run, nothing written - pass dry_run false to write" if dry_run else ""))
    if stale_reference:
        message += (". check-objects / check-references not run: this node's reference files are "
                    "an older version's - the reference-files step replaces them")
    else:
        message += (f". check-objects: {checks['check_objects']['violations']} violation(s), "
                    f"{checks['check_objects']['deprecated']} deprecated; check-references: "
                    f"{checks['check_references']['dangling']} dangling")
    return message, {"dry_run": dry_run, "data_dir": str(root), "steps": results,
                     "records_changed": records, "conflicts": conflicts,
                     "backup_dir": str(backup_dir) if backed_up else None,
                     "backed_up": backed_up, **checks}
