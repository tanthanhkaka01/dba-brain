"""``app-command-set`` — the one command that edits ``data/app_commands.json``.

Nothing edited an app command until 2026-09-22. Every other kind of record had a registrar — a SQL
task has two, an instance has one, a credential has one — and the nine records that decide *when
every app runs* could only be changed by opening the file on whichever node you happened to be
looking at.

That is not a missing convenience. It is why the estate, the 0.21.0 soak node and the shipped
catalogue held **three different schedules** for ``APP-BACKUP-RESTORE`` at the same time (60/300,
30/30, 300/300): the documented "300 → 30" fix had been applied by hand on one node, so a new
install still got 300 and nobody could see the disagreement from anywhere. §1.11 of
``audits/TEST_VERSION_BEFORE_RELEASE_DETAIL_0_21_0.md`` measured all three.

**What it will not do.** It does not create or delete an app command, and it does not rename one.
The nine are the nine: they correspond to code that exists, ``command_text`` names a module to run,
and a tenth record invented through a CLI would schedule something that cannot run. Adding one is a
release that ships the app it names.

**The fields it accepts are read from the reference, not listed here.** ``app_command`` is described
field by field in ``data/shared_config_objects.json`` (§1.10), so this module asks that file what a
field is called and what it may hold. A second list would be a second opinion, and the reason the
reference was written as data rather than prose is that prose cannot be asked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db_ops.common.config_admin import (
    DEFAULT_DATA_DIR,
    ConfigAdminError,
    _atomic_write,
    _dump_json,
    _read_json,
    normalize_time_window,
)
from db_ops.lib import field_names, shared_objects
from db_ops.lib.json_io import indent_of

FILE_NAME = "app_commands.json"
LIST_KEY = "app_commands"

#: The key an app command is addressed by. ``app_code`` rather than ``app_command_id``: it is what
#: every log line, every ``job_runs`` row and every operator says out loud (``APP-BACKUP-RESTORE``),
#: and the id is an ordinal that differs between nodes.
KEY_FIELD = "app_code"

#: Fields this command refuses to change, and why each one is not an oversight.
#:
#: They are the record's *identity* and its *wiring*. Changing an identity field silently
#: re-addresses a record that other files point at by name; changing the wiring points a schedule at
#: different code, which is a release, not a configuration edit. What is left — the schedule, whether
#: it runs, how many at once, and the note — is exactly the set an operator needs between releases.
FROZEN_FIELDS: tuple[str, ...] = (
    "app_command_id",   # the node's own ordinal key; other rows join on it
    "app_code",         # the address this command takes; renaming it would orphan the request
    "app_name",         # paired with app_code in the catalogue
    "log_scope",        # names the log file; changing it splits one app's history in two
    "command_text",     # the code that runs. A different module is a different release
    "working_dir",      # resolved against the tool root; a wrong value fails at run time only
)


class AppCommandAdminError(ConfigAdminError):
    """The request cannot be applied, with the reason a person can act on."""


def editable_fields(*, data_dir: str | Path | None = None) -> list[str]:
    """Which fields ``app-command-set`` will change, from the reference minus the frozen ones."""
    declared = shared_objects.field_names("app_command", data_dir=data_dir)
    return [name for name in declared if name not in FROZEN_FIELDS]


def _load(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get(LIST_KEY), list):
        raise AppCommandAdminError(
            f"{path} does not look like an app command file: expected an object with a "
            f"'{LIST_KEY}' list.")
    return payload


def set_app_command(request: dict[str, Any], *,
                    data_dir: str | Path | None = None) -> dict[str, Any]:
    """Apply one edit to one app command and return what changed, field by field.

    The answer names the **old and the new value of every field it touched**, because the reason this
    command exists is that a schedule changed somewhere and nothing said so. An edit that reports
    only "ok" would reproduce the fault in a new place.
    """
    if not isinstance(request, dict):
        raise AppCommandAdminError("request must be a JSON object.")

    root = Path(data_dir or request.get("data_dir") or DEFAULT_DATA_DIR)
    path = root / FILE_NAME
    if not path.is_file():
        raise AppCommandAdminError(f"{path} does not exist; nothing to edit.")

    key = str(request.get(KEY_FIELD) or "").strip()
    if not key:
        raise AppCommandAdminError(
            f"'{KEY_FIELD}' is required and names which app command to edit, e.g. "
            '{"app_code": "APP-BACKUP-RESTORE", "time_window": {"repeat_interval": 30}}.')

    # `data_dir` addresses the FILE, not the record, so it is not a field being set.
    changes = {name: value for name, value in request.items()
               if name not in (KEY_FIELD, "data_dir")}
    if not changes:
        raise AppCommandAdminError(
            f"nothing to change. Give at least one field besides '{KEY_FIELD}'; "
            f"editable: {', '.join(editable_fields(data_dir=root))}.")

    allowed = set(editable_fields(data_dir=root))
    for name in changes:
        if name in FROZEN_FIELDS:
            raise AppCommandAdminError(
                f"'{name}' cannot be changed here. It is part of what this app command IS, not how "
                f"it is scheduled: editing it would re-address or re-point a record other files "
                f"already name. Change it in the release that ships it. "
                f"Editable: {', '.join(sorted(allowed))}.")
        if name not in allowed:
            raise AppCommandAdminError(
                f"'{name}' is not a field of an app command. "
                f"Editable: {', '.join(sorted(allowed))}.")

    payload = _load(path)
    rows = payload[LIST_KEY]
    matches = [row for row in rows
               if isinstance(row, dict) and str(row.get(KEY_FIELD, "")).strip() == key]
    if not matches:
        known = sorted(str(r.get(KEY_FIELD)) for r in rows if isinstance(r, dict))
        raise AppCommandAdminError(
            f"no app command with {KEY_FIELD} '{key}'. This command does not create one - the "
            f"records correspond to code that exists. Known: {', '.join(known)}.")
    if len(matches) > 1:
        raise AppCommandAdminError(
            f"{len(matches)} app commands share {KEY_FIELD} '{key}', so an edit cannot say which "
            "one it means. Fix the duplicate first.")
    row = matches[0]

    applied: list[dict[str, Any]] = []
    for name, value in changes.items():
        before = row.get(name, None)
        if name == "time_window":
            if not isinstance(value, dict):
                raise AppCommandAdminError(
                    "'time_window' must be an object, e.g. {\"repeat_interval\": 30}.")
            # A PARTIAL edit: the fields given are changed and the rest of the window is kept.
            # Replacing the whole block would silently drop `weekdays` or `timeout` from a record
            # whose caller only meant to retune the interval - which is the shape of the fault this
            # command exists to stop, applied to the command itself.
            merged = dict(before or {})
            merged.update(value)
            try:
                after = normalize_time_window(merged)
                # The STORED block is normalised too before the comparison. Without this every edit
                # reports a change, because a stored partial window (three keys) never equals the
                # canonical fourteen-field form even when the value asked for is already in place -
                # and "written: true" for a no-op is exactly the noise this command exists to remove.
                before_normal = normalize_time_window(dict(before or {}))
            except ConfigAdminError as exc:
                raise AppCommandAdminError(f"time_window: {exc}") from exc
            inner = [
                {"field": f"time_window.{key}",
                 "from": before_normal.get(key), "to": after.get(key)}
                for key in sorted(set(before_normal) | set(after))
                if before_normal.get(key) != after.get(key)
            ]
            if not inner:
                continue
            row[name] = after
            # Reported field by field rather than as two blocks. `time_window <14 keys> -> <14 keys>`
            # is technically the change and tells nobody which number moved.
            applied.extend(inner)
            continue
        # Stage C (0.22.0 section 1.4): the standard name is written, and a legacy spelling still on
        # the record is taken off in the same edit - leaving `app_ord` beside `sort_order` would be
        # one record saying the same thing twice, and the stale half is the one a reader finds.
        legacy = [old for old, standard in field_names.RENAMES["app_command"].items()
                  if standard == name and old in row]
        before = row.get(name, row.get(legacy[0]) if legacy else None)
        after = value
        if before == after and not legacy:
            continue
        rebuilt = {(name if key in legacy else key): (after if key in legacy else item)
                   for key, item in row.items()}
        rebuilt[name] = after
        row.clear()
        row.update(rebuilt)
        applied.append({"field": name, "from": before, "to": after})

    if not applied:
        return {"ok": True, "app_code": key, "written": False, "changes": [],
                "message": f"{key} already carries those values; nothing written."}

    # Validate the edited record against the reference before it reaches disk, so a bad value is
    # refused rather than written and found by the next daemon sweep.
    # Notices are counted, never refused: a `deprecated` spelling is one the daemon still reads, and
    # a record written before stage C may still carry one on a field this edit did not touch.
    findings = [item for item in shared_objects.check_record(
        "app_command", row, where=f"{FILE_NAME}[{key}]", data_dir=root)
        if item["kind"] not in shared_objects.NOTICE_KINDS]
    if findings:
        detail = "; ".join(f"{f.get('field')}: {f.get('detail')}" for f in findings)
        raise AppCommandAdminError(
            f"the edit would make {key} invalid, so nothing was written: {detail}")

    _atomic_write(path, _dump_json(payload, indent=indent_of(path)))
    return {"ok": True, "app_code": key, "written": True, "file": str(path),
            "changes": applied,
            "message": f"{key}: " + ", ".join(
                f"{c['field']} {_short(c['from'])} -> {_short(c['to'])}" for c in applied)}


def _short(value: Any) -> str:
    text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= 60 else text[:57] + "..."
